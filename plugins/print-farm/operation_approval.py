"""Single-use in-memory approvals for starting an exact print plan.

Approvals are deliberately ephemeral: they are bound to one session epoch,
expire quickly, and are consumed before the comparison result is returned.
This module does not establish printer compatibility; callers must only create
an approval after making that decision from trusted binding evidence.
"""

import hashlib
import hmac
import json
import math
import re
import secrets
import threading
import time
from collections import OrderedDict


DEFAULT_MAX_ENTRIES = 256
MAX_TTL_SECONDS = 300.0
MAX_PLAN_BYTES = 32 * 1024
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_PLAN_KEYS = frozenset((
    "target_identity", "binding_revision", "artifact_sha256", "plate_entry",
    "action", "use_ams", "metadata_digest",
))


def _canonical_json(value, label):
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError(label + " must be finite JSON data") from exc
    if len(encoded.encode("ascii")) > MAX_PLAN_BYTES:
        raise ValueError(label + " is too large")
    return encoded


def _validate_plan(plan):
    if not isinstance(plan, dict) or set(plan) != _PLAN_KEYS:
        raise ValueError("plan has missing or unexpected fields")
    target = plan["target_identity"]
    if not isinstance(target, str) or not target or target != target.strip() or len(target) > 1024:
        raise ValueError("target_identity must be a stable non-empty identity")
    revision = plan["binding_revision"]
    if (isinstance(revision, bool) or not isinstance(revision, (int, str)) or
            isinstance(revision, int) and revision < 0 or
            isinstance(revision, str) and (not revision or revision != revision.strip() or len(revision) > 256)):
        raise ValueError("binding_revision must be a non-negative integer or stable string")
    for key in ("artifact_sha256", "metadata_digest"):
        if not isinstance(plan[key], str) or _DIGEST_RE.fullmatch(plan[key]) is None:
            raise ValueError(key + " must be a lowercase SHA-256 digest")
    plate = plan["plate_entry"]
    if plate is not None and (not isinstance(plate, str) or not plate or len(plate) > 1024):
        raise ValueError("plate_entry must be a selected entry or None")
    if plan["action"] != "start":
        raise ValueError("approval is only valid for the start action")
    if not isinstance(plan["use_ams"], bool):
        raise ValueError("use_ams must be a boolean")
    encoded = _canonical_json(plan, "plan")
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def _epoch_key(epoch):
    if isinstance(epoch, bool) or not isinstance(epoch, (str, int)):
        raise ValueError("epoch must be a non-empty string or integer")
    if isinstance(epoch, str) and (not epoch or len(epoch) > 256):
        raise ValueError("epoch must be a non-empty string or integer")
    return _canonical_json(epoch, "epoch")


class OperationApprovalStore:
    """Thread-safe, bounded store of short-lived one-use print approvals."""

    def __init__(self, max_entries=DEFAULT_MAX_ENTRIES, clock=None):
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries < 1:
            raise ValueError("max_entries must be a positive integer")
        self.max_entries = max_entries
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        self._entries = OrderedDict()

    def _purge_expired(self, now):
        for nonce, entry in tuple(self._entries.items()):
            if entry["expires_at"] <= now:
                del self._entries[nonce]

    def create(self, plan, ttl, epoch):
        """Create an approval and return ``(nonce, plan_digest)``.

        Raises ``ValueError`` for malformed plans, epochs, or TTLs and
        ``RuntimeError`` when the bounded store is full of live approvals.
        """
        digest = _validate_plan(plan)
        epoch_key = _epoch_key(epoch)
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)):
            raise ValueError("ttl must be a positive finite number")
        ttl = float(ttl)
        if not math.isfinite(ttl) or ttl <= 0 or ttl > MAX_TTL_SECONDS:
            raise ValueError("ttl must be between 0 and %s seconds" % MAX_TTL_SECONDS)
        now = self._clock()
        with self._lock:
            self._purge_expired(now)
            if len(self._entries) >= self.max_entries:
                raise RuntimeError("approval store is full")
            nonce = secrets.token_urlsafe(32)
            while nonce in self._entries:
                nonce = secrets.token_urlsafe(32)
            self._entries[nonce] = {
                "digest": digest,
                "epoch": epoch_key,
                "expires_at": now + ttl,
            }
        return nonce, digest

    def consume(self, nonce, exact_plan, epoch):
        """Consume an approval exactly once; return false for any mismatch."""
        if not isinstance(nonce, str) or not nonce:
            return False
        with self._lock:
            entry = self._entries.pop(nonce, None)
            if entry is None or entry["expires_at"] <= self._clock():
                return False
            try:
                plan_digest = _validate_plan(exact_plan)
                epoch_key = _epoch_key(epoch)
            except (TypeError, ValueError):
                return False
            return (hmac.compare_digest(entry["digest"], plan_digest) and
                    hmac.compare_digest(entry["epoch"], epoch_key))

    def __len__(self):
        with self._lock:
            self._purge_expired(self._clock())
            return len(self._entries)
