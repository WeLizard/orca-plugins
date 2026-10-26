"""Duet / RepRapFirmware (standalone) / Duet Software Framework adapter.

One connection type ("duet") speaks two wire protocols; the protocol is chosen
*inside* this module by a non-actuating probe and is reported as ``protocol``:

* ``rrf``  - standalone RepRapFirmware HTTP API (``rr_connect``, ``rr_model``,
  ``rr_upload``, ``rr_fileinfo``, ``rr_filelist``, ``rr_gcode``,
  ``rr_disconnect``).
* ``dsf``  - Duet Software Framework REST API (``/machine/*``). This also
  covers a DSF that answers ``rr_connect`` in rr-emulation mode: such a reply
  carries ``isEmulated: true`` and is NOT proof of standalone firmware, so the
  adapter switches to ``/machine/*``.

Protocol selection rules (fail closed):

* ``rr_connect`` is probed first. Only a plain HTTP 404 there allows trying
  ``/machine/connect``. 401/403/5xx, a timeout, or a non-JSON reply never lets
  the adapter try another protocol (no silent route change for writes).
* Callers can pin the protocol they polled with ``backend="rrf"|"dsf"``; a
  differing detection result yields ``protocol_mismatch`` and nothing is
  written.

Writes: ``upload`` (to ``0:/gcodes/``) and ``start`` (``M32``, built from a
strictly validated file name only). Pause/resume/cancel are *not* implemented:
no first-hand source for their exact wire form has been verified, so those
functions return ``unsupported_unverified`` without any I/O.

A transport failure after a mutating request was dispatched is reported as
``unknown_outcome`` and the request is never repeated.

Secrets (the password, session keys) are never placed in exceptions, error
codes, status dictionaries or logs; the module does not log.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import ssl
import time
import urllib.parse
import zlib
from pathlib import Path


TIMEOUT = 8.0                       # per socket operation
POLL_DEADLINE = 20.0                # total budget for one poll()
UPLOAD_DEADLINE = 1800.0            # total budget for one upload()
OPERATION_DEADLINE = 30.0           # total budget for start()
MAX_MODEL_BYTES = 8 * 1024 * 1024
MAX_SMALL_BYTES = 64 * 1024
CHUNK_SIZE = 64 * 1024
READ_CHUNK = 16 * 1024
MAX_FILELIST_PAGES = 50
MAX_NAME_LENGTH = 100

PROTOCOL_RRF = "rrf"
PROTOCOL_DSF = "dsf"
_BACKEND_NAMES = {
    "rrf": PROTOCOL_RRF, "duet_rrf": PROTOCOL_RRF,
    "dsf": PROTOCOL_DSF, "duet_dsf": PROTOCOL_DSF,
}

CAPABILITIES = {
    "status": "implemented",
    "upload": "implemented",
    "start": "implemented_requires_explicit_confirmation",
    "pause": "unsupported_unverified",
    "resume": "unsupported_unverified",
    "cancel": "unsupported_unverified",
}

_UPLOAD_NAME_FORBIDDEN = re.compile(r'[/\\\x00-\x1f\x7f:*?"<>|]')
_START_NAME = re.compile(r"^[A-Za-z0-9._()\- ]+$")
_SESSION_KEY = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")
_REDIRECTS = (301, 302, 303, 307, 308)

# state.status values of the RRF object model (Object Model Documentation).
_STATE_MAP = {
    "idle": "idle",
    "processing": "printing",
    "simulating": "simulating",
    "busy": "busy",
    "changingtool": "busy",
    "paused": "paused",
    "pausing": "pausing",
    "resuming": "resuming",
    "cancelling": "cancelling",
    "halted": "halted",
    "off": "off",
    "updating": "updating",
    "starting": "starting",
    "disconnected": "disconnected",
}
_UNKNOWN_STATE_CODES = {
    "malformed_model", "malformed_response", "unrecognized_dsf_model",
    "unrecognized_rrf_model", "protocol_unrecognized",
}


class _AdapterError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _Deadline:
    def __init__(self, seconds: float):
        self._end = time.monotonic() + seconds

    def remaining(self) -> float:
        return self._end - time.monotonic()

    def check(self) -> None:
        if self.remaining() <= 0:
            raise _AdapterError("timeout")

    def socket_timeout(self) -> float:
        self.check()
        return max(0.05, min(TIMEOUT, self.remaining()))


class _Session:
    def __init__(self, parsed, ssl_context, kind, key, deadline, rr_emulated=False):
        self.parsed = parsed
        self.ssl_context = ssl_context
        self.kind = kind
        self.key = key
        self.deadline = deadline
        self.rr_emulated = rr_emulated

    def headers(self, accept="application/json"):
        headers = [("Accept", accept)]
        if self.key is not None:
            headers.append(("X-Session-Key", self.key))
        return headers


# --------------------------------------------------------------------------
# Public interface
# --------------------------------------------------------------------------

def poll(base: str, ssl_context: ssl.SSLContext | None, password: str = "", *, backend: str | None = None) -> dict:
    """Return a normalized status. Never actuates; never raises."""
    status = _empty_status()
    session = None
    try:
        deadline = _Deadline(POLL_DEADLINE)
        session = _open_session(base, ssl_context, password, deadline, backend)
        model = _read_model(session)
        if not _recognizable_model(session.kind, model):
            status["state"] = "unknown"
            status["job_state"] = "unknown"
            status["error_code"] = "unrecognized_dsf_model" if session.kind == PROTOCOL_DSF else "unrecognized_rrf_model"
            status.update(_protocol_fields(session))
            return status
        status.update(_normalize_model(model))
        status.update(_protocol_fields(session))
        status["online"] = True
        idle = status["state"] == "idle"
        status["upload_ready"] = idle
        status["upload_reason"] = "" if idle else "state_%s" % status["state"]
        status["can_start"] = idle
        return status
    except _AdapterError as exc:
        status["error_code"] = exc.code
        if exc.code in _UNKNOWN_STATE_CODES:
            status["state"] = "unknown"
            status["job_state"] = "unknown"
        if session is not None:
            status.update(_protocol_fields(session))
        return status
    except (OSError, http.client.HTTPException, ValueError, TypeError):
        status["error_code"] = "invalid_request"
        return status
    finally:
        _close_session(session)


def upload(
    base: str,
    ssl_context: ssl.SSLContext | None,
    password: str,
    file_path: str | os.PathLike[str],
    filename: str,
    *,
    backend: str | None = None,
) -> dict:
    """Upload a G-code file to ``0:/gcodes/`` and verify it by size.

    Preconditions checked in the same session: protocol pin, machine state
    ``idle`` (never overwrite or upload while a job runs). The result carries
    ``verified`` (True only after a matching size was read back) and
    ``verification`` (``size_match`` / ``unavailable``). Unknown outcomes are
    reported as ``unknown_outcome`` and must not be retried blindly.
    """
    session = None
    try:
        safe_name = _validate_upload_name(filename)
        path = Path(file_path)
        info = path.stat()
        if not path.is_file() or info.st_size <= 0:
            raise ValueError("source must be a non-empty file")
        size = info.st_size
        crc = _crc32_of(path, size)
        deadline = _Deadline(UPLOAD_DEADLINE)
        session = _open_session(base, ssl_context, password, deadline, backend)
        model = _read_model(session)
        if not _recognizable_model(session.kind, model):
            return _failure("status_unavailable", "not_sent", session)
        if _normalize_model(model)["state"] != "idle":
            return _failure("device_not_idle", "not_sent", session)

        if session.kind == PROTOCOL_DSF:
            target = "/machine/file/0:/gcodes/" + urllib.parse.quote(safe_name, safe="")
            status_code, body = _exchange(
                session, "PUT", target, session.headers(), MAX_SMALL_BYTES,
                body_file=(path, size), mutating=True,
                content_type="application/octet-stream")
            accepted = _write_status(status_code)
            if accepted:
                return accepted
            if status_code not in (200, 201, 204):
                return _failure("upload_rejected", "rejected", session)
            evidence = "dsf-http-%d" % status_code
        else:
            query = _qs([
                ("name", "0:/gcodes/" + safe_name),
                ("time", time.strftime("%Y-%m-%dT%H:%M:%S")),
                ("crc32", "%08x" % crc),
            ])
            status_code, body = _exchange(
                session, "POST", "/rr_upload?" + query, session.headers(), MAX_SMALL_BYTES,
                body_file=(path, size), mutating=True,
                content_type="application/octet-stream")
            accepted = _write_status(status_code)
            if accepted:
                return accepted
            if status_code != 200:
                return _failure("upload_rejected", "rejected", session)
            reply = _parse_json(body)
            err = reply.get("err") if isinstance(reply, dict) else None
            if not isinstance(err, int) or isinstance(err, bool):
                return _failure("unknown_outcome", "unknown", session)
            if err != 0:
                return _failure("upload_rejected", "rejected", session)
            evidence = "rrf-upload-err0"

        verdict, remote_size = _remote_file_size(session, safe_name)
        result = {
            "ok": True, "evidence_ref": evidence, "protocol": session.kind,
            "outcome": "completed", "size": size,
            "verified": False, "verification": "unavailable",
        }
        if verdict == "found" and remote_size == size:
            result.update(verified=True, verification="size_match")
        elif verdict == "found" and remote_size is not None:
            result.update(ok=False, error_code="verification_failed", outcome="applied_unverified",
                          verification="size_mismatch")
        elif verdict == "missing":
            result.update(ok=False, error_code="verification_failed", outcome="applied_unverified",
                          verification="missing")
        return result
    except _AdapterError as exc:
        return {"ok": False, "error_code": exc.code, "outcome": _outcome_of(exc.code)}
    except (OSError, ValueError, TypeError):
        return {"ok": False, "error_code": "invalid_request", "outcome": "not_sent"}
    finally:
        _close_session(session)


def start(
    base: str,
    ssl_context: ssl.SSLContext | None,
    password: str,
    filename: str,
    *,
    backend: str | None = None,
    expected_size: int | None = None,
) -> dict:
    """Start printing an already-uploaded file via ``M32 "0:/gcodes/<name>"``.

    Must only be called after explicit user confirmation. The G-code is built
    from a strictly validated name (ASCII letters, digits, ``._()- `` and a
    ``.gcode`` suffix); no caller-supplied G-code is ever sent. Preconditions:
    protocol pin, state ``idle``, file present (and size equal to
    ``expected_size`` when given). ``ok=True`` means *accepted*
    (``outcome="accepted_pending_reconcile"``): confirm by polling for a
    non-idle state. One request is sent, never repeated.
    """
    session = None
    try:
        name = _validate_start_name(filename)
        if expected_size is not None and (not isinstance(expected_size, int) or isinstance(expected_size, bool) or expected_size < 0):
            raise ValueError("invalid expected size")
        deadline = _Deadline(OPERATION_DEADLINE)
        session = _open_session(base, ssl_context, password, deadline, backend)
        model = _read_model(session)
        if not _recognizable_model(session.kind, model):
            return _failure("status_unavailable", "not_sent", session)
        if _normalize_model(model)["state"] != "idle":
            return _failure("device_not_idle", "not_sent", session)
        verdict, remote_size = _remote_file_size(session, name)
        if verdict == "missing":
            return _failure("file_not_found", "not_sent", session)
        if verdict != "found":
            return _failure("file_unverified", "not_sent", session)
        if expected_size is not None and remote_size != expected_size:
            return _failure("file_size_mismatch", "not_sent", session)

        code = 'M32 "0:/gcodes/%s"' % name
        if session.kind == PROTOCOL_DSF:
            headers = session.headers("text/plain")
            status_code, body = _exchange(
                session, "POST", "/machine/code", headers, MAX_SMALL_BYTES,
                body_bytes=code.encode("ascii"), mutating=True, content_type="text/plain")
            accepted = _write_status(status_code)
            if accepted:
                return accepted
            if not 200 <= status_code < 300:
                return _failure("start_rejected", "rejected", session)
            text = body.decode("utf-8", "replace").lstrip().lower()
            if text.startswith("error"):
                return _failure("start_rejected", "rejected", session)
        else:
            target = "/rr_gcode?" + _qs([("gcode", code)])
            status_code, body = _exchange(
                session, "GET", target, session.headers(), MAX_SMALL_BYTES, mutating=True)
            accepted = _write_status(status_code)
            if accepted:
                return accepted
            if status_code != 200:
                return _failure("start_rejected", "rejected", session)
            reply = _parse_json(body)
            if not isinstance(reply, dict):
                return _failure("unknown_outcome", "unknown", session)
            err = reply.get("err")
            if err not in (None, 0) or isinstance(err, bool):
                return _failure("start_rejected", "rejected", session)
        return {"ok": True, "outcome": "accepted_pending_reconcile", "protocol": session.kind,
                "evidence_ref": "%s-gcode-accepted" % session.kind}
    except _AdapterError as exc:
        return {"ok": False, "error_code": exc.code, "outcome": _outcome_of(exc.code)}
    except (OSError, ValueError, TypeError):
        return {"ok": False, "error_code": "invalid_request", "outcome": "not_sent"}
    finally:
        _close_session(session)


def pause(*_args, **_kwargs) -> dict:
    return _unsupported()


def resume(*_args, **_kwargs) -> dict:
    return _unsupported()


def cancel(*_args, **_kwargs) -> dict:
    return _unsupported()


def _unsupported() -> dict:
    return {"ok": False, "error_code": "unsupported_unverified", "outcome": "not_sent"}


# --------------------------------------------------------------------------
# Session / protocol detection
# --------------------------------------------------------------------------

def _empty_status() -> dict:
    return {
        "online": False,
        "state": "offline",
        "job_state": "unknown",
        "raw_state": None,
        "nozzle": None,
        "target_nozzle": None,
        "bed": None,
        "target_bed": None,
        "tools": [],
        "active_tool": None,
        "progress": None,
        "filename": "",
        "thumbnail": "",
        "remaining_seconds": None,
        "layer": None,
        "total_layers": None,
        "upload_ready": False,
        "upload_reason": "",
        "upload_permission": "unverified",
        "can_start": False,
        "can_control": False,
        "control_reason": "pause_resume_cancel_unverified",
        "protocol": None,
        "rr_emulation": False,
        "capabilities": dict(CAPABILITIES),
    }


def _protocol_fields(session) -> dict:
    return {
        "protocol": session.kind,
        "rr_emulation": bool(session.rr_emulated),
        "backend": "duet_dsf" if session.kind == PROTOCOL_DSF else "duet_rrf",
    }


def _check_password(password):
    if not isinstance(password, str) or len(password) > 256 or any(c in password for c in "\r\n\x00"):
        raise _AdapterError("invalid_credentials")


def _open_session(base, ssl_context, password, deadline, backend=None):
    pin = None
    if backend is not None:
        pin = _BACKEND_NAMES.get(str(backend).lower())
        if pin is None:
            raise ValueError("unknown backend pin")
    parsed = _parse_base(base)
    _check_password(password)

    probe = _qs([("password", password), ("sessionKey", "yes")], safe="")
    probe_session = _Session(parsed, ssl_context, PROTOCOL_RRF, None, deadline)
    status, body = _exchange(probe_session, "GET", "/rr_connect?" + probe,
                             [("Accept", "application/json")], MAX_SMALL_BYTES)
    if status == 404:
        if pin == PROTOCOL_RRF:
            # Pinned to standalone RRF but /rr_connect is absent: never hand
            # the password to another API.
            raise _AdapterError("protocol_mismatch")
        session = _connect_dsf(parsed, ssl_context, password, deadline, rr_emulated=False)
    elif status in (401, 403):
        raise _AdapterError("authentication_failed")
    elif not 200 <= status < 300:
        raise _AdapterError(_error_for_status(status))
    else:
        reply = _parse_json(body)
        err = reply.get("err") if isinstance(reply, dict) else None
        if not isinstance(err, int) or isinstance(err, bool):
            raise _AdapterError("protocol_unrecognized")
        if err == 1:
            raise _AdapterError("authentication_failed")
        if err == 2:
            raise _AdapterError("no_sessions_available")
        if err != 0:
            raise _AdapterError("protocol_unrecognized")
        if reply.get("isEmulated") is True:
            # DSF in rr-emulation: release the emulated session (best effort)
            # and talk to the native /machine API instead.
            _best_effort_get(probe_session, "/rr_disconnect")
            session = _connect_dsf(parsed, ssl_context, password, deadline, rr_emulated=True)
        else:
            key = reply.get("sessionKey")
            if isinstance(key, bool) or not isinstance(key, (str, int)):
                key = None
            if key is not None:
                key = str(key)
                if not _SESSION_KEY.fullmatch(key):
                    # The firmware opened a session we cannot address: release it.
                    _best_effort_get(probe_session, "/rr_disconnect")
                    raise _AdapterError("protocol_unrecognized")
            session = _Session(parsed, ssl_context, PROTOCOL_RRF, key, deadline)
    if pin is not None and session.kind != pin:
        _close_session(session)
        raise _AdapterError("protocol_mismatch")
    return session


def _connect_dsf(parsed, ssl_context, password, deadline, rr_emulated):
    probe = _Session(parsed, ssl_context, PROTOCOL_DSF, None, deadline)
    status, body = _exchange(
        probe, "GET", "/machine/connect?" + _qs([("password", password)], safe=""),
        [("Accept", "application/json")], MAX_SMALL_BYTES)
    if status in (401, 403):
        raise _AdapterError("authentication_failed")
    if not 200 <= status < 300:
        raise _AdapterError(_error_for_status(status))
    reply = _parse_json(body)
    key = reply.get("sessionKey") if isinstance(reply, dict) else None
    if not isinstance(key, str) or not _SESSION_KEY.fullmatch(key):
        if (isinstance(key, str) and 0 < len(key) <= 256 and key.isascii()
                and key.isprintable()):
            # A session may exist on the device; close it with the key it gave.
            _close_session(_Session(parsed, ssl_context, PROTOCOL_DSF, key, deadline))
        raise _AdapterError("authentication_failed")
    return _Session(parsed, ssl_context, PROTOCOL_DSF, key, deadline, rr_emulated=rr_emulated)


def _best_effort_get(session, path):
    try:
        _exchange(session, "GET", path, session.headers(), MAX_SMALL_BYTES)
    except _AdapterError:
        pass


def _close_session(session):
    if session is None:
        return
    path = "/machine/disconnect" if session.kind == PROTOCOL_DSF else "/rr_disconnect"
    try:
        # A fresh short deadline so cleanup works after the main one expired.
        cleanup = _Session(session.parsed, session.ssl_context, session.kind, session.key, _Deadline(TIMEOUT))
        _exchange(cleanup, "GET", path, cleanup.headers(), MAX_SMALL_BYTES)
    except Exception:
        pass


# --------------------------------------------------------------------------
# HTTP transport
# --------------------------------------------------------------------------

def _parse_base(base):
    if not isinstance(base, str):
        raise ValueError("invalid base URL")
    parsed = urllib.parse.urlsplit(base.strip().rstrip("/"))
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise ValueError("invalid base URL")
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ValueError("credentials, query and fragment are forbidden in base URL")
    if parsed.path not in ("", "/"):
        raise ValueError("base URL must not contain a path")
    parsed.port  # validate malformed ports
    return parsed


def _connection(parsed, ssl_context, timeout):
    options = {"timeout": timeout}
    if parsed.scheme.lower() == "https":
        options["context"] = ssl_context or ssl.create_default_context()
        return http.client.HTTPSConnection(parsed.hostname, parsed.port, **options)
    return http.client.HTTPConnection(parsed.hostname, parsed.port, **options)


def _exchange(session, method, target, headers, max_bytes, *, body_bytes=None,
              body_file=None, mutating=False, content_type=None):
    """One HTTP exchange. Returns (status, body). Raises _AdapterError(code).

    For ``mutating`` requests any failure after the request was dispatched
    becomes ``unknown_outcome``; the request is never repeated.
    """
    deadline = session.deadline
    source = None
    conn = None
    dispatched = False
    try:
        if body_file is not None:
            source = body_file[0].open("rb")
        conn = _connection(session.parsed, session.ssl_context, deadline.socket_timeout())
        conn.putrequest(method, target)
        for name, value in headers:
            conn.putheader(name, value)
        conn.putheader("Connection", "close")
        if body_file is not None:
            conn.putheader("Content-Length", str(body_file[1]))
        elif body_bytes is not None:
            conn.putheader("Content-Length", str(len(body_bytes)))
        if content_type and (body_file is not None or body_bytes is not None):
            conn.putheader("Content-Type", content_type)
        conn.endheaders()
        dispatched = True
        if body_bytes is not None:
            conn.send(body_bytes)
        elif body_file is not None:
            remaining = body_file[1]
            while remaining:
                deadline.check()
                chunk = source.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    raise OSError("source file changed during upload")
                conn.send(chunk)
                remaining -= len(chunk)
        response = conn.getresponse()
        status = response.status
        if status in _REDIRECTS:
            raise _AdapterError("redirect_rejected")
        length = response.getheader("Content-Length")
        if length and length.isdigit() and int(length) > max_bytes:
            raise _AdapterError("response_too_large")
        data = bytearray()
        while True:
            deadline.check()
            chunk = response.read1(READ_CHUNK)
            if not chunk:
                break
            data += chunk
            if len(data) > max_bytes:
                raise _AdapterError("response_too_large")
        return status, bytes(data)
    except _AdapterError as exc:
        if mutating and dispatched and exc.code in ("timeout", "response_too_large"):
            raise _AdapterError("unknown_outcome") from None
        raise
    except (OSError, http.client.HTTPException) as exc:
        if mutating and dispatched:
            raise _AdapterError("unknown_outcome") from None
        raise _AdapterError("timeout" if isinstance(exc, TimeoutError) else "connection_failed") from None
    finally:
        if conn is not None:
            conn.close()
        if source is not None:
            source.close()


def _error_for_status(status):
    if status == 401:
        return "authentication_failed"
    if status == 403:
        return "authorization_denied"
    if status == 404:
        return "not_found"
    if status == 502:
        return "dcs_incompatible"
    if status == 503:
        return "device_busy"
    return "http_error"


def _qs(items, safe=":/"):
    """Percent-encode a query (no '+' for spaces; ':' and '/' kept literal in paths)."""
    return "&".join(
        "%s=%s" % (key, urllib.parse.quote(value, safe=safe)) for key, value in items)


def _parse_json(body):
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeError, ValueError):
        return None


def _get_json(session, path, max_bytes=MAX_SMALL_BYTES):
    status, body = _exchange(session, "GET", path, session.headers(), max_bytes)
    if not 200 <= status < 300:
        raise _AdapterError(_error_for_status(status))
    value = _parse_json(body)
    if value is None:
        raise _AdapterError("malformed_response")
    return value


def _write_status(status):
    """Map write-time statuses that are definitive rejections."""
    if status in (401, 403, 404, 502, 503):
        return {"ok": False, "error_code": _error_for_status(status), "outcome": "rejected"}
    return None


def _failure(code, outcome, session=None):
    result = {"ok": False, "error_code": code, "outcome": outcome}
    if session is not None:
        result["protocol"] = session.kind
    return result


def _outcome_of(code):
    if code == "unknown_outcome":
        return "unknown"
    return "not_sent"


# --------------------------------------------------------------------------
# Object model
# --------------------------------------------------------------------------

def _read_model(session):
    if session.kind == PROTOCOL_DSF:
        model = _get_json(session, "/machine/model", MAX_MODEL_BYTES)
        if not isinstance(model, dict):
            raise _AdapterError("malformed_model")
        return model
    model = {}
    for key in ("state", "job", "heat", "tools"):
        reply = _get_json(session, "/rr_model?key=%s&flags=d99o" % key, MAX_MODEL_BYTES)
        if not isinstance(reply, dict):
            raise _AdapterError("malformed_model")
        model[key] = reply.get("result")
    state = model.get("state")
    if isinstance(state, dict) and not _is_int(state.get("currentTool")):
        reply = _get_json(session, "/rr_model?key=move.motionSystems&flags=d99o", MAX_MODEL_BYTES)
        if isinstance(reply, dict) and isinstance(reply.get("result"), list):
            model["move"] = {"motionSystems": reply["result"]}
    return model


def _recognizable_model(kind, model):
    if not isinstance(model, dict):
        return False
    state = model.get("state")
    if not (isinstance(state, dict) and isinstance(state.get("status"), str)):
        return False
    if kind == PROTOCOL_DSF:
        directories = model.get("directories")
        return isinstance(directories, dict) and isinstance(directories.get("gCodes"), str)
    return True


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _number_or_none(value):
    if isinstance(value, dict):
        value = value.get("value")
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _dict(value):
    return value if isinstance(value, dict) else {}


def _normalize_model(model):
    state = _dict(model.get("state"))
    job = _dict(model.get("job"))
    raw = state.get("status")
    normalized = _STATE_MAP.get(raw.lower(), "unknown") if isinstance(raw, str) else "unknown"

    heaters = _dict(model.get("heat")).get("heaters")
    heaters = heaters if isinstance(heaters, list) else None
    heat = _dict(model.get("heat"))

    active_tool = _active_tool_number(model)
    tools = _normalize_tools(model.get("tools"), heaters, active_tool)
    primary = None
    for entry in tools:
        if entry["index"] == active_tool:
            primary = entry
            break
    if primary is None and tools:
        primary = tools[0]

    bed, target_bed = _bed_values(heat, heaters)

    file_info = job.get("file")
    filename = ""
    size = None
    if isinstance(file_info, dict):
        if isinstance(file_info.get("fileName"), str):
            filename = file_info["fileName"]
        size = file_info.get("size")
    elif isinstance(job.get("fileName"), str):
        filename = job["fileName"]

    return {
        "state": normalized,
        "job_state": normalized,
        "raw_state": raw if isinstance(raw, str) else None,
        "nozzle": primary["current"] if primary else None,
        "target_nozzle": primary["target"] if primary else None,
        "bed": bed,
        "target_bed": target_bed,
        "tools": tools,
        "active_tool": active_tool,
        "progress": _progress(job, size, normalized),
        "filename": filename,
        "thumbnail": "",
        **_job_stats(job, file_info, normalized),
    }


_MAX_REMAINING_SECONDS = 30 * 86400
_MAX_LAYERS = 100000


def _whole_number(value, low, high):
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return value if _is_int(value) and low <= value <= high else None


def _job_stats(job, file_info, normalized):
    """Remaining seconds and layers from the object model (printing/paused only).

    RRF object model: job.timesLeft.{file,filament,slicer} (seconds, null when
    unknown), job.layer (current layer, null when unknown) and job.file.numLayers
    (0 when unknown). Anything missing or implausible stays None."""
    stats = {"remaining_seconds": None, "layer": None, "total_layers": None}
    if normalized not in ("printing", "paused"):
        return stats
    left = _dict(job.get("timesLeft"))
    for key in ("file", "filament", "slicer"):
        value = left.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value == value                 and 0 <= value <= _MAX_REMAINING_SECONDS:
            stats["remaining_seconds"] = int(round(value))
            break
    total = _whole_number(_dict(file_info).get("numLayers"), 1, _MAX_LAYERS)
    layer = _whole_number(job.get("layer"), 0, _MAX_LAYERS)
    if total is not None and layer is not None and layer <= total:
        stats["layer"], stats["total_layers"] = layer, total
    return stats


def _progress(job, size, normalized):
    position = job.get("filePosition")
    if (isinstance(position, (int, float)) and not isinstance(position, bool)
            and isinstance(size, (int, float)) and not isinstance(size, bool) and size > 0):
        return max(0, min(100, int(position * 100 / size)))
    if normalized == "idle":
        return 0
    return None


def _active_tool_number(model):
    state = _dict(model.get("state"))
    current = state.get("currentTool")
    if _is_int(current):
        return current if current >= 0 else None
    systems = _dict(model.get("move")).get("motionSystems")
    if isinstance(systems, list) and systems and isinstance(systems[0], dict):
        current = systems[0].get("currentTool")
        if _is_int(current) and current >= 0:
            return current
    return None


def _heater_at(heaters, index):
    if heaters is None or not _is_int(index) or index < 0 or index >= len(heaters):
        return None
    heater = heaters[index]
    return heater if isinstance(heater, dict) else None


def _heater_current(heater):
    if heater is None:
        return None
    if heater.get("state") in ("fault", "offline"):
        return None
    if heater.get("sensor") == -1:
        return None
    return _number_or_none(heater.get("current"))


def _first_number(values):
    if isinstance(values, list) and values:
        return _number_or_none(values[0])
    return None


def _normalize_tools(raw_tools, heaters, active_tool):
    if not isinstance(raw_tools, list):
        return []
    result = []
    for tool in raw_tools:
        if not isinstance(tool, dict) or not _is_int(tool.get("number")):
            continue
        tool_heaters = [i for i in (tool.get("heaters") or []) if _is_int(i)] if isinstance(tool.get("heaters"), list) else []
        current = _heater_current(_heater_at(heaters, tool_heaters[0])) if tool_heaters else None
        tool_state = tool.get("state") if isinstance(tool.get("state"), str) else None
        if tool_state == "active":
            target = _first_number(tool.get("active"))
        elif tool_state == "standby":
            target = _first_number(tool.get("standby"))
        else:
            target = None
        result.append({
            "index": tool["number"],
            "name": tool.get("name") if isinstance(tool.get("name"), str) else "",
            "current": current,
            "target": target,
            "state": tool_state,
            "active": tool["number"] == active_tool,
            "heaters": tool_heaters,
        })
    return result


def _bed_values(heat, heaters):
    indices = []
    legacy = heat.get("bedHeaters")
    if isinstance(legacy, list):
        indices = [i for i in legacy if _is_int(i) and i >= 0]
    if not indices:
        mapping = heat.get("bedHeaterMapping")
        if isinstance(mapping, list):
            for item in mapping:
                values = item if isinstance(item, list) else [item]
                indices = [i for i in values if _is_int(i) and i >= 0]
                if indices:
                    break
    if not indices:
        return None, None
    heater = _heater_at(heaters, indices[0])
    if heater is None:
        return None, None
    state = heater.get("state")
    if state == "active":
        target = _number_or_none(heater.get("active"))
    elif state == "standby":
        target = _number_or_none(heater.get("standby"))
    else:
        target = None
    return _heater_current(heater), target


# --------------------------------------------------------------------------
# Remote file checks
# --------------------------------------------------------------------------

def _remote_file_size(session, name):
    """Return ("found", size|None) / ("missing", None) / ("unavailable", None)."""
    try:
        if session.kind == PROTOCOL_DSF:
            target = "/machine/fileinfo/0:/gcodes/" + urllib.parse.quote(name, safe="")
            try:
                info = _get_json(session, target, MAX_SMALL_BYTES)
            except _AdapterError as exc:
                if exc.code == "not_found":
                    return "missing", None
                return "unavailable", None
            if isinstance(info, dict):
                size = info.get("size")
                return "found", size if _is_int(size) else None
            return "unavailable", None

        query = _qs([("name", "0:/gcodes/" + name)])
        try:
            info = _get_json(session, "/rr_fileinfo?" + query, MAX_SMALL_BYTES)
        except _AdapterError:
            info = None
        if isinstance(info, dict) and info.get("err") == 0 and _is_int(info.get("size")):
            return "found", info["size"]
        return _rrf_listing_size(session, name)
    except _AdapterError:
        return "unavailable", None


def _rrf_listing_size(session, name):
    first = 0
    for _ in range(MAX_FILELIST_PAGES):
        query = _qs([("dir", "0:/gcodes"), ("first", str(first))])
        try:
            page = _get_json(session, "/rr_filelist?" + query, MAX_MODEL_BYTES)
        except _AdapterError:
            return "unavailable", None
        if not isinstance(page, dict) or page.get("err") not in (None, 0) or not isinstance(page.get("files"), list):
            return "unavailable", None
        for entry in page["files"]:
            if isinstance(entry, dict) and entry.get("name") == name and entry.get("type") == "f":
                size = entry.get("size")
                return "found", size if _is_int(size) else None
        nxt = page.get("next")
        if not _is_int(nxt) or nxt == 0:
            return "missing", None
        if nxt <= first:
            return "unavailable", None
        first = nxt
    return "unavailable", None


# --------------------------------------------------------------------------
# Names / files
# --------------------------------------------------------------------------

def _validate_upload_name(filename):
    if (not isinstance(filename, str) or not filename or len(filename) > MAX_NAME_LENGTH
            or _UPLOAD_NAME_FORBIDDEN.search(filename)):
        raise ValueError("unsafe filename")
    if filename in (".", "..") or filename != filename.strip() or filename.endswith("."):
        raise ValueError("unsafe filename")
    if not filename.lower().endswith(".gcode"):
        raise ValueError("only a plain .gcode filename is accepted")
    return filename


def _validate_start_name(filename):
    name = _validate_upload_name(filename)
    if not _START_NAME.fullmatch(name) or ".." in name or name.startswith("."):
        raise ValueError("filename not allowed for start")
    return name


def _crc32_of(path, size):
    crc = 0
    remaining = size
    with path.open("rb") as source:
        while remaining:
            chunk = source.read(min(CHUNK_SIZE, remaining))
            if not chunk:
                raise OSError("source file changed")
            crc = zlib.crc32(chunk, crc)
            remaining -= len(chunk)
    return crc & 0xFFFFFFFF
