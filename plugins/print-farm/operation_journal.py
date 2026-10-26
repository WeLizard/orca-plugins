"""Crash-safe local journal for Print Farm operation intents and outcomes.

This module records a durable intent before a caller sends any network bytes.
It does not perform transport work and cannot promise exactly-once effects from
printers. An interrupted dispatch is conservatively held as unknown.
"""

import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path


SCHEMA_VERSION = 3
DEFAULT_MAX_RECORDS = 5000
MAX_BINDING_BYTES = 8192
MAX_EVIDENCE_LENGTH = 256
_ACTIONS = frozenset(("upload", "start", "pause", "resume", "cancel"))
_TRANSITIONS = {
    "planned": frozenset(("dispatching", "cancelled_before_dispatch")),
    "dispatching": frozenset(("submitted", "acknowledged", "verified", "rejected", "unknown_outcome")),
    "submitted": frozenset(("acknowledged", "verified", "rejected", "unknown_outcome",
                            "manually_resolved")),
    "acknowledged": frozenset(("verified", "rejected", "unknown_outcome",
                               "manually_resolved")),
    "unknown_outcome": frozenset(("verified", "rejected", "manually_resolved")),
    "verified": frozenset(),
    "rejected": frozenset(),
    "cancelled_before_dispatch": frozenset(),
    "manually_resolved": frozenset(),
}


def _locked(method):
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


class JournalError(RuntimeError):
    """Base error for invalid or unavailable journal operations."""


class JournalCorruptError(JournalError):
    """The journal cannot be trusted; preserve it and fail closed."""


class ConflictError(JournalError):
    """An operation or target conflicts with an existing durable intent."""


class TransitionError(JournalError):
    """A requested lifecycle transition is not allowed."""


def _identity(value, label):
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError("invalid " + label)
    if any(ord(char) < 32 for char in value):
        raise ValueError("invalid " + label)
    return value


def _sha256(value, label, required):
    if value is None and not required:
        return None
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(label + " must be a lowercase SHA-256 digest")
    return value


def _artifact_name(value):
    if value is None:
        return None
    if (not isinstance(value, str) or not value or value != value.strip() or len(value) > 255 or
            value in (".", "..") or any(char in value for char in ("/", "\\", ":")) or
            any(ord(char) < 32 for char in value)):
        raise ValueError("artifact_name must be a sanitized filename without a path")
    return value


def _json_object(value, label):
    if not isinstance(value, dict) or not value:
        raise ValueError(label + " must be a non-empty object")
    try:
        forbidden = ("password", "token", "secret", "credential", "accesscode", "privatekey")
        def inspect(item):
            if isinstance(item, dict):
                for key, child in item.items():
                    if not isinstance(key, str) or any(word in re.sub(r"[^a-z]", "", key.lower())
                                                        for word in forbidden):
                        raise ValueError(label + " must not contain credentials")
                    inspect(child)
            elif isinstance(item, list):
                for child in item:
                    inspect(child)
        inspect(value)
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except (TypeError, ValueError) as exc:
        raise ValueError(label + " is not JSON serializable") from exc
    if len(encoded.encode("utf-8")) > MAX_BINDING_BYTES:
        raise ValueError(label + " is too large")
    return encoded


class OperationJournal:
    """SQLite-backed journal stored at a path inside private plugin storage.

    The embedding plugin must pass its account-scoped private storage path.
    SQLite uses FULL synchronous commits and an append-only event table. All
    methods are safe across SQLite connections/processes; no network call is
    made by this class.
    """

    def __init__(self, path, max_records=DEFAULT_MAX_RECORDS, timeout=5.0):
        if isinstance(max_records, bool) or not isinstance(max_records, int) or max_records < 1:
            raise ValueError("max_records must be a positive integer")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_records = max_records
        self._lock = threading.RLock()
        try:
            self._db = sqlite3.connect(str(self.path), timeout=timeout, isolation_level=None,
                                       check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA busy_timeout=%d" % max(1, int(timeout * 1000)))
            self._db.execute("PRAGMA synchronous=FULL")
            self._initialize()
            self._db.execute("PRAGMA journal_mode=WAL")
            self._validate()
        except JournalError:
            db = getattr(self, "_db", None)
            if db is not None:
                db.close()
            raise
        except (sqlite3.DatabaseError, OSError) as exc:
            db = getattr(self, "_db", None)
            if db is not None:
                db.close()
            raise JournalCorruptError("operation journal unavailable; preserve the file") from exc

    @_locked
    def close(self):
        self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def _initialize(self):
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, SCHEMA_VERSION):
            raise JournalCorruptError("unknown operation journal schema; preserve the file")
        tables = self._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if version == 0 and tables:
            raise JournalCorruptError("unrecognized non-empty operation journal; preserve the file")
        if version == 0:
            self._db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE operations (
                    operation_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    binding_json TEXT NOT NULL,
                    action TEXT NOT NULL,
                    artifact_sha256 TEXT,
                    plate_entry TEXT,
                    artifact_name TEXT,
                    fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    evidence_ref TEXT
                );
                CREATE INDEX operations_account_created
                    ON operations(account_id, created_at DESC, operation_id DESC);
                CREATE UNIQUE INDEX one_unresolved_start_per_target
                    ON operations(account_id, target_id)
                    WHERE action='start' AND state IN
                    ('planned','dispatching','submitted','acknowledged','unknown_outcome');
                CREATE UNIQUE INDEX one_unresolved_upload_per_artifact
                    ON operations(account_id, target_id, artifact_sha256, ifnull(plate_entry,''))
                    WHERE action='upload' AND state IN
                    ('planned','dispatching','submitted','acknowledged','unknown_outcome');
                CREATE TABLE operation_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT NOT NULL REFERENCES operations(operation_id) ON DELETE CASCADE,
                    state TEXT NOT NULL,
                    at REAL NOT NULL,
                    evidence_ref TEXT,
                    note TEXT,
                    actor TEXT NOT NULL
                );
                PRAGMA user_version=3;
                COMMIT;
            """)
        elif version == 1:
            if {row[0] for row in tables} != {"operations", "operation_events"}:
                raise JournalCorruptError("unrecognized journal schema; preserve the file")
            self._db.executescript("""
                BEGIN IMMEDIATE;
                CREATE UNIQUE INDEX one_unresolved_upload_per_artifact
                    ON operations(account_id, target_id, artifact_sha256, ifnull(plate_entry,''))
                    WHERE action='upload' AND state IN
                    ('planned','dispatching','submitted','acknowledged','unknown_outcome');
                ALTER TABLE operations ADD COLUMN artifact_name TEXT;
                PRAGMA user_version=3;
                COMMIT;
            """)
        elif version == 2:
            if {row[0] for row in tables} != {"operations", "operation_events"}:
                raise JournalCorruptError("unrecognized journal schema; preserve the file")
            self._db.executescript("""
                BEGIN IMMEDIATE;
                ALTER TABLE operations ADD COLUMN artifact_name TEXT;
                PRAGMA user_version=3;
                COMMIT;
            """)

    def _validate(self):
        integrity = self._db.execute("PRAGMA quick_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise JournalCorruptError("operation journal integrity check failed; preserve the file")
        expected = {"operations", "operation_events"}
        tables = {row[0] for row in self._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )}
        if tables != expected:
            raise JournalCorruptError("operation journal schema is incomplete or unexpected")
        bad = self._db.execute("""
            SELECT 1 FROM operations o WHERE o.state NOT IN
            ('planned','dispatching','submitted','acknowledged','verified','rejected',
             'unknown_outcome','cancelled_before_dispatch','manually_resolved')
            OR NOT EXISTS (SELECT 1 FROM operation_events e WHERE e.operation_id=o.operation_id)
            LIMIT 1
        """).fetchone()
        if bad:
            raise JournalCorruptError("operation journal contains invalid records")

    def _tx(self):
        self._db.execute("BEGIN IMMEDIATE")

    def _get_row(self, operation_id):
        row = self._db.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
        if row is None:
            raise KeyError(operation_id)
        return row

    @staticmethod
    def _public(row):
        result = dict(row)
        result["target_binding"] = json.loads(result.pop("binding_json"))
        return result

    @_locked
    def get(self, operation_id):
        return self._public(self._get_row(_identity(operation_id, "operation_id")))

    @_locked
    def list_operations(self, account_id, limit=100, *, before=None):
        account_id = _identity(account_id, "account_id")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        if before is None:
            rows = self._db.execute(
                "SELECT * FROM operations WHERE account_id=? ORDER BY created_at DESC,operation_id DESC LIMIT ?",
                (account_id, limit),
            ).fetchall()
        else:
            if not isinstance(before, tuple) or len(before) != 2:
                raise ValueError("before must be a (created_at, operation_id) cursor")
            rows = self._db.execute(
                "SELECT * FROM operations WHERE account_id=? AND (created_at,operation_id)<(?,?) "
                "ORDER BY created_at DESC,operation_id DESC LIMIT ?",
                (account_id, before[0], before[1], limit),
            ).fetchall()
        return [self._public(row) for row in rows]

    @_locked
    def list_unresolved(self, account_id, limit=100):
        """Show holds independently of the normal recent-history page."""
        account_id = _identity(account_id, "account_id")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        rows = self._db.execute(
            "SELECT * FROM operations WHERE account_id=? AND state IN "
            "('planned','dispatching','submitted','acknowledged','unknown_outcome') "
            "ORDER BY created_at DESC,operation_id DESC LIMIT ?", (account_id, limit),
        ).fetchall()
        return [self._public(row) for row in rows]

    @_locked
    def events(self, operation_id, limit=100):
        operation_id = _identity(operation_id, "operation_id")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        rows = self._db.execute(
            "SELECT state,at,evidence_ref,note,actor FROM operation_events WHERE operation_id=? "
            "ORDER BY sequence LIMIT ?", (operation_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def _trim_for_insert(self):
        count = self._db.execute("SELECT count(*) FROM operations").fetchone()[0]
        excess = count - self.max_records + 1
        if excess <= 0:
            return
        rows = self._db.execute(
            "SELECT operation_id FROM operations WHERE state NOT IN "
            "('planned','dispatching','submitted','acknowledged','unknown_outcome') "
            "ORDER BY updated_at,operation_id LIMIT ?", (excess,),
        ).fetchall()
        if len(rows) < excess:
            raise JournalError("operation journal is full of unresolved operations")
        self._db.executemany("DELETE FROM operations WHERE operation_id=?", [(row[0],) for row in rows])

    @_locked
    def prepare(self, *, account_id, target_id, target_binding, action,
                artifact_sha256=None, plate_entry=None, artifact_name=None, operation_id=None):
        """Persist an immutable planned intent; this method never dispatches."""
        account_id = _identity(account_id, "account_id")
        target_id = _identity(target_id, "target_id")
        if action not in _ACTIONS:
            raise ValueError("unsupported action")
        binding_json = _json_object(target_binding, "target_binding")
        artifact_sha256 = _sha256(artifact_sha256, "artifact_sha256", action in ("upload", "start"))
        artifact_name = _artifact_name(artifact_name)
        if plate_entry is not None:
            plate_entry = _identity(plate_entry, "plate_entry")
            components = plate_entry.split("/")
            if (len(plate_entry) > 512 or "\\" in plate_entry or plate_entry.startswith("/") or
                    ":" in plate_entry or any(part in ("", ".", "..") for part in components) or
                    "\x00" in plate_entry):
                raise ValueError("invalid plate_entry")
        operation_id = _identity(operation_id or str(uuid.uuid4()), "operation_id")
        try:
            if str(uuid.UUID(operation_id)) != operation_id:
                raise ValueError
        except (ValueError, AttributeError) as exc:
            raise ValueError("operation_id must be a canonical UUID") from exc
        immutable = {
            "account_id": account_id,
            "target_id": target_id,
            "target_binding": json.loads(binding_json),
            "action": action,
            "artifact_sha256": artifact_sha256,
            "plate_entry": plate_entry,
            "artifact_name": artifact_name,
        }
        fingerprint = hashlib.sha256(json.dumps(
            immutable, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")).hexdigest()
        now = time.time()
        try:
            self._tx()
            existing = self._db.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise ConflictError("operation ID reused with changed intent")
                self._db.execute("COMMIT")
                return self._public(existing)
            self._trim_for_insert()
            self._db.execute("""
                INSERT INTO operations(operation_id,account_id,target_id,binding_json,action,
                    artifact_sha256,plate_entry,artifact_name,fingerprint,state,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,'planned',?,?)
            """, (operation_id, account_id, target_id, binding_json, action,
                  artifact_sha256, plate_entry, artifact_name, fingerprint, now, now))
            self._db.execute(
                "INSERT INTO operation_events(operation_id,state,at,actor) VALUES(?,?,?,'local')",
                (operation_id, "planned", now),
            )
            self._db.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise ConflictError("an unresolved operation already protects this target or upload artifact") from exc
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise
        return self.get(operation_id)

    @_locked
    def begin_dispatch(self, operation_id):
        """Durably claim the one allowed dispatch before the first network byte."""
        return self._transition(operation_id, "dispatching", actor="dispatcher", expected="planned")

    @_locked
    def report(self, operation_id, state, *, evidence_ref=None, actor="transport"):
        if state not in _TRANSITIONS:
            raise ValueError("unsupported state")
        return self._transition(operation_id, state, evidence_ref=evidence_ref, actor=actor)

    @_locked
    def _transition(self, operation_id, state, *, evidence_ref=None, note=None, actor, expected=None):
        operation_id = _identity(operation_id, "operation_id")
        actor = _identity(actor, "actor")
        if evidence_ref is not None:
            if (not isinstance(evidence_ref, str) or not evidence_ref or
                    len(evidence_ref) > MAX_EVIDENCE_LENGTH or
                    re.fullmatch(r"[A-Za-z0-9_.:/-]+", evidence_ref) is None):
                raise ValueError("evidence_ref must be a short opaque redacted reference")
        if state in ("verified", "rejected", "acknowledged") and not evidence_ref:
            raise ValueError("matching protocol evidence is required for this state")
        self._tx()
        try:
            old = self._get_row(operation_id)
            if expected is not None and old["state"] != expected:
                self._db.execute("COMMIT")
                return False
            if state not in _TRANSITIONS.get(old["state"], frozenset()):
                raise TransitionError("invalid transition %s -> %s" % (old["state"], state))
            now = time.time()
            self._db.execute("UPDATE operations SET state=?,updated_at=?,evidence_ref=? WHERE operation_id=?",
                             (state, now, evidence_ref, operation_id))
            self._db.execute("INSERT INTO operation_events(operation_id,state,at,evidence_ref,note,actor) "
                             "VALUES(?,?,?,?,?,?)", (operation_id, state, now, evidence_ref, note, actor))
            self._db.execute("COMMIT")
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise
        return self.get(operation_id)

    @_locked
    def recover_interrupted(self, account_id, *, exclusive_session_owner=False):
        """Conservatively mark in-flight calls unknown (and never-sent plans cancelled)
        after exclusive ownership.

        Caller must establish exclusive account runtime ownership first; a timer
        or process age is not proof that another writer stopped.
        """
        account_id = _identity(account_id, "account_id")
        if exclusive_session_owner is not True:
            raise PermissionError("exclusive session ownership must be established first")
        self._tx()
        try:
            rows = self._db.execute(
                "SELECT operation_id FROM operations WHERE account_id=? AND state IN "
                "('dispatching','submitted','acknowledged')", (account_id,),
            ).fetchall()
            planned = self._db.execute(
                "SELECT operation_id FROM operations WHERE account_id=? AND state='planned'",
                (account_id,),
            ).fetchall()
            now = time.time()
            for row in rows:
                op_id = row[0]
                self._db.execute("UPDATE operations SET state='unknown_outcome',updated_at=? WHERE operation_id=?",
                                 (now, op_id))
                self._db.execute("INSERT INTO operation_events(operation_id,state,at,actor) "
                                 "VALUES(?,'unknown_outcome',?,'recovery')", (op_id, now))
            # A plan that never reached dispatching sent nothing: it only
            # holds its target, so it is cancelled rather than left unknown.
            for row in planned:
                op_id = row[0]
                self._db.execute("UPDATE operations SET state='cancelled_before_dispatch',updated_at=? WHERE operation_id=?",
                                 (now, op_id))
                self._db.execute("INSERT INTO operation_events(operation_id,state,at,actor) "
                                 "VALUES(?,'cancelled_before_dispatch',?,'recovery')", (op_id, now))
            rows = list(rows) + list(planned)
            self._db.execute("COMMIT")
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise
        return len(rows)

    @_locked
    def reconcile(self, operation_id, outcome, *, evidence_ref, actor="reconciler"):
        """Record read-only, target-matched observation; never calls a device."""
        if outcome not in ("verified", "rejected"):
            raise ValueError("reconciliation outcome must be verified or rejected")
        row = self._get_row(_identity(operation_id, "operation_id"))
        if row["state"] not in ("dispatching", "submitted", "acknowledged", "unknown_outcome"):
            raise TransitionError("only an in-flight or unknown operation can be reconciled")
        return self._transition(operation_id, outcome, evidence_ref=evidence_ref, actor=actor)

    @_locked
    def resolve_unknown_manually(self, operation_id, *, reason, actor):
        """Release a completed or uncertain transport hold without claiming success.

        An actively dispatching call is never released here: the caller must
        first establish exclusive session ownership and recover it as unknown.
        """
        if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 500:
            raise ValueError("a concise manual resolution reason is required")
        actor = _identity(actor, "actor")
        # Keep a bounded human note in the local audit event; expose only its
        # digest as the opaque evidence reference.
        safe_reason = re.sub(r"[^A-Za-z0-9 _.,:;()/-]", "?", reason.strip())
        evidence = "manual:" + hashlib.sha256((actor + "\n" + safe_reason).encode("utf-8")).hexdigest()
        return self._transition(operation_id, "manually_resolved", evidence_ref=evidence,
                                note=safe_reason[:500], actor=actor)
