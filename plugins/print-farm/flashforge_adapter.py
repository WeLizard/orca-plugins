"""FlashForge adapter: Adventurer 5M / 5M Pro / AD5X over the LAN HTTP API.

Wire facts come only from the sources pinned in flashforge/SOURCE_NOTES.md
(Parallel-7 flashforge-api-docs, OrcaSlicer Flashforge.cpp, ff-5mp-api-py).
No source was checked against our own hardware, so:

* ``poll`` is read-only (``POST :8898/detail``) and identifies the model from
  ``detail.pid`` (never from the user-editable ``name``). An empty, partial or
  unrecognized reply is ``unknown``, never ``idle``.
* ``upload`` and ``start`` are implemented to the documented wire format but
  stay OFF until a model is listed in ``HARDWARE_VERIFIED``; until then they
  return ``unsupported_unverified`` without any I/O and ``poll`` reports
  ``upload_ready=False`` / ``can_control=False`` with a reason.
* Pause/resume/cancel, thumbnails, AD5X start / material-station mapping,
  Creator 5 and the legacy Adventurer 3/4 families are not implemented.
* ``poll_tcp`` is an opt-in read-only fallback over TCP 8899. It takes the
  single printer control session (``~M601 S1``), which can lock out other
  clients while it runs, and always releases it (``~M602``).

Credentials are the printer serial number and the check code (printer
settings menu). They are one secret pair: neither appears in any returned
dict, error code or exception text; printer ``message`` strings are never
propagated. The module does not log.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import math
import os
import re
import socket
import ssl
import time
import urllib.parse
from pathlib import Path


HTTP_PORT = 8898
TCP_PORT = 8899
STATUS_TIMEOUT = 6.0            # total budget for one poll
UPLOAD_TIMEOUT = 30.0           # per socket operation while streaming
UPLOAD_DEADLINE = 1800.0        # total budget for one upload
OPERATION_TIMEOUT = 15.0        # total budget for start
MAX_BODY_BYTES = 256 * 1024     # /detail is ~2 KB; larger replies are hostile
MAX_SMALL_BYTES = 64 * 1024
MAX_TCP_REPLY = 16 * 1024
MAX_UPLOAD_BYTES = 512 * 1024 * 1024
CHUNK_SIZE = 64 * 1024
MAX_CREDENTIAL_LENGTH = 64

# Models whose /detail identity we accept. Key: numeric pid (see Printer-PIDs).
# Creator 5 (0x28) / 5 Pro (0x29) and every other pid are deliberately absent.
MODELS = {
    0x23: {"model": "Adventurer 5M", "family": "adventurer_5m"},
    0x24: {"model": "Adventurer 5M Pro", "family": "adventurer_5m"},
    0x26: {"model": "AD5X", "family": "ad5x"},
}
# Writes stay disabled until hardware evidence exists. Per pid enable
# {"upload": True} and/or {"start": True}. "start" is only honoured for the
# 5M family (AD5X needs a material mapping that is not implemented).
HARDWARE_VERIFIED: dict = {}

_STATE_MAP = {
    "ready": "idle",
    "completed": "complete",
    "printing": "printing",
    "heating": "printing",
    "pausing": "printing",
    "pause": "paused",
    "error": "error",
    "busy": "busy",
    "canceling": "busy",
    "cancel": "busy",
    "calibrate_doing": "busy",
    "downloading": "busy",
    "cloud_slicing": "busy",
    "sending": "busy",
    "unzipping": "busy",
}
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_PID_HEX = re.compile(r"^[0-9A-Fa-f]{1,4}$")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_HTTP_CODE_MAP = {1: "authentication_failed", -2: "lan_mode_disabled", -1: "invalid_request", 2: "printer_busy", 5: "printer_busy"}

CAPABILITIES = {
    "status": "implemented",
    "upload": "implemented_disabled_until_hardware_verified",
    "start": "implemented_disabled_until_hardware_verified_5m_only",
    "pause": "unsupported_unverified",
    "resume": "unsupported_unverified",
    "cancel": "unsupported_unverified",
    "tcp_status": "implemented_opt_in",
    "tcp_upload": "unsupported_unverified",
    "tcp_start": "unsupported_unverified",
}


class _AdapterError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# --------------------------------------------------------------------------
# Public interface
# --------------------------------------------------------------------------

def poll(base: str, ssl_context: ssl.SSLContext | None = None, secret: str = "", *, serial_number: str = "") -> dict:
    """Read-only status via ``POST /detail``. Never raises, never actuates.

    ``base`` is ``http://host`` (port 8898 implied) or ``http://host:port``.
    ``secret`` is the check code; ``serial_number`` is the printer serial.
    ``ssl_context`` is accepted for interface parity and unused: the printer
    API is plain HTTP.
    """
    status = _empty_status()
    try:
        deadline = time.monotonic() + STATUS_TIMEOUT
        target = _parse_base(base)
        creds = _credentials(serial_number, secret)
        reply = _json_call(target, "/detail", {"serialNumber": creds[0], "checkCode": creds[1]},
                           deadline, MAX_BODY_BYTES)
        detail = _checked_detail(reply)
        pid = _read_pid(detail)
        if pid is None:
            raise _AdapterError("identity_unverified")
        if pid not in MODELS:
            raise _AdapterError("unsupported_model")
        status.update(_normalize(detail))
        status.update(model=MODELS[pid]["model"], model_family=MODELS[pid]["family"], identity_verified=True)
        status["online"] = True
        _apply_write_gate(status, pid)
        return status
    except _AdapterError as exc:
        status["error_code"] = exc.code
        if exc.code not in ("connection_failed", "timeout"):
            status["state"] = "unknown"
        return status
    except (OSError, ValueError, TypeError):
        status["error_code"] = "invalid_request"
        return status


def upload(
    base: str,
    ssl_context: ssl.SSLContext | None,
    secret: str,
    file_path: str | os.PathLike[str],
    filename: str,
    *,
    serial_number: str = "",
) -> dict:
    """``POST /uploadGcode`` with ``printNow: "false"``; never starts a print.

    Disabled (``unsupported_unverified``, nothing sent) until the identified
    model is listed in ``HARDWARE_VERIFIED`` with ``upload: True``. The printer
    is polled first and must be ``idle`` and a recognized model. A failure or
    unreadable reply after the request was dispatched is ``unknown_outcome``
    and must not be retried blindly. Verification is reported as unavailable.
    """
    if not any(entry.get("upload") for entry in HARDWARE_VERIFIED.values()):
        return _result(False, "unsupported_unverified", "not_sent")
    try:
        safe_name = _validate_filename(filename)
        path = Path(file_path)
        size = path.stat().st_size
        if not path.is_file() or size <= 0 or size > MAX_UPLOAD_BYTES:
            return _result(False, "invalid_request", "not_sent")
        target = _parse_base(base)
        creds = _credentials(serial_number, secret)
        gate = _preflight(base, secret, serial_number, "upload")
        if gate is not None:
            return gate
        boundary = "pf" + hashlib.sha256(os.urandom(16)).hexdigest()[:24]
        head = (
            "--%s\r\nContent-Disposition: form-data; name=\"gcodeFile\"; filename=\"%s\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n" % (boundary, safe_name)
        ).encode("ascii")
        tail = ("\r\n--%s--\r\n" % boundary).encode("ascii")
        headers = [
            ("Content-Type", "multipart/form-data; boundary=" + boundary),
            ("Content-Length", str(len(head) + size + len(tail))),
            ("serialNumber", creds[0]),
            ("checkCode", creds[1]),
            ("fileSize", str(size)),
            ("printNow", "false"),
            ("levelingBeforePrint", "false"),
        ]
        conn = http.client.HTTPConnection(target[0], target[1], timeout=UPLOAD_TIMEOUT)
        dispatched = False
        deadline = time.monotonic() + UPLOAD_DEADLINE
        try:
            conn.putrequest("POST", "/uploadGcode", skip_accept_encoding=True)
            for name, value in headers:
                conn.putheader(name, value)
            conn.putheader("Connection", "close")
            conn.endheaders()
            dispatched = True
            conn.send(head)
            remaining = size
            with path.open("rb") as source:
                while remaining:
                    if time.monotonic() > deadline:
                        raise TimeoutError()
                    chunk = source.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        raise OSError("source file changed during upload")
                    conn.send(chunk)
                    remaining -= len(chunk)
            conn.send(tail)
            response = conn.getresponse()
            body = _read_response(response, MAX_SMALL_BYTES, deadline)
            if response.status != 200:
                return _result(False, "upload_rejected", "rejected")
            verdict = _envelope_code(_parse_json(body))
            if verdict == 0:
                return {"ok": True, "outcome": "completed_unverified", "evidence_ref": "flashforge-http-code-0",
                        "size": size, "verified": False, "verification": "unavailable"}
            if verdict is None:
                return _result(False, "unknown_outcome", "unknown")
            return _result(False, _HTTP_CODE_MAP.get(verdict, "upload_rejected"), "rejected")
        except _AdapterError as exc:
            if dispatched and exc.code in ("timeout", "response_too_large", "malformed_response", "redirect_rejected"):
                return _result(False, "unknown_outcome", "unknown")
            return _result(False, exc.code, "unknown" if dispatched else "not_sent")
        except (OSError, http.client.HTTPException):
            return _result(False, "unknown_outcome" if dispatched else "connection_failed",
                           "unknown" if dispatched else "not_sent")
        finally:
            conn.close()
    except _AdapterError as exc:
        return _result(False, exc.code, "not_sent")
    except (OSError, ValueError, TypeError):
        return _result(False, "invalid_request", "not_sent")


def start(
    base: str,
    ssl_context: ssl.SSLContext | None,
    secret: str,
    filename: str,
    *,
    serial_number: str = "",
    leveling_before_print: bool = False,
) -> dict:
    """``POST /printGcode`` {fileName, levelingBeforePrint} for an uploaded file.

    Only for the 5M family (pid 0x23/0x24) and only after the model is marked
    ``start: True`` in ``HARDWARE_VERIFIED``; AD5X and everything else return
    ``unsupported_unverified``. Requires state ``ready`` (a ``completed``
    printer may still need its plate cleared). Must follow explicit user
    confirmation. One request is sent and never repeated; ``ok=True`` means
    accepted (``accepted_pending_reconcile``) and must be confirmed by polling.
    """
    if not any(entry.get("start") for pid, entry in HARDWARE_VERIFIED.items()
               if MODELS.get(pid, {}).get("family") == "adventurer_5m"):
        return _result(False, "unsupported_unverified", "not_sent")
    try:
        safe_name = _validate_filename(filename)
        if not isinstance(leveling_before_print, bool):
            raise ValueError("invalid leveling flag")
        target = _parse_base(base)
        creds = _credentials(serial_number, secret)
        gate = _preflight(base, secret, serial_number, "start")
        if gate is not None:
            return gate
        deadline = time.monotonic() + OPERATION_TIMEOUT
        body = {"serialNumber": creds[0], "checkCode": creds[1], "fileName": safe_name,
                "levelingBeforePrint": leveling_before_print}
        try:
            reply = _json_call(target, "/printGcode", body, deadline, MAX_SMALL_BYTES, mutating=True)
        except _AdapterError as exc:
            return _result(False, exc.code, "unknown" if exc.code == "unknown_outcome" else "not_sent")
        verdict = _envelope_code(reply)
        if verdict == 0:
            return {"ok": True, "outcome": "accepted_pending_reconcile", "evidence_ref": "flashforge-http-code-0"}
        if verdict is None:
            return _result(False, "unknown_outcome", "unknown")
        mapped = "file_not_found" if verdict == 3 else _HTTP_CODE_MAP.get(verdict, "start_rejected")
        return _result(False, mapped, "rejected")
    except _AdapterError as exc:
        return _result(False, exc.code, "not_sent")
    except (OSError, ValueError, TypeError):
        return _result(False, "invalid_request", "not_sent")


def pause(*_args, **_kwargs) -> dict:
    return _result(False, "unsupported_unverified", "not_sent")


def resume(*_args, **_kwargs) -> dict:
    return _result(False, "unsupported_unverified", "not_sent")


def cancel(*_args, **_kwargs) -> dict:
    return _result(False, "unsupported_unverified", "not_sent")


def upload_tcp(*_args, **_kwargs) -> dict:
    """TCP M28/M29 upload: chunk acknowledgements are not confirmed."""
    return _result(False, "unsupported_unverified", "not_sent")


def start_tcp(*_args, **_kwargs) -> dict:
    """TCP ``~M23`` start: not confirmed for any model we can identify."""
    return _result(False, "unsupported_unverified", "not_sent")


# --------------------------------------------------------------------------
# Status normalization
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
        "progress": None,
        "filename": "",
        "thumbnail": "",
        "upload_ready": False,
        "upload_reason": "",
        "can_start": False,
        "can_control": False,
        "control_reason": "pause_resume_cancel_unverified",
        "elapsed_seconds": None,
        "remaining_seconds": None,
        "layer": None,
        "total_layers": None,
        "model": None,
        "model_family": None,
        "firmware": "",
        "identity_verified": False,
        "printer_error_code": "",
        "backend": "flashforge_http",
        "error_code": "",
        "capabilities": dict(CAPABILITIES),
    }


def _apply_write_gate(status: dict, pid: int) -> None:
    entry = HARDWARE_VERIFIED.get(pid, {})
    idle = status["state"] == "idle"
    if not entry.get("upload"):
        status["upload_ready"] = False
        status["upload_reason"] = "hardware_unverified"
    elif not idle:
        status["upload_ready"] = False
        status["upload_reason"] = "state_%s" % status["state"]
    else:
        status["upload_ready"] = True
        status["upload_reason"] = ""
    start_ok = bool(entry.get("start")) and MODELS[pid]["family"] == "adventurer_5m" and idle
    status["can_start"] = start_ok


def _normalize(detail: dict) -> dict:
    raw = detail.get("status")
    if not isinstance(raw, str):
        return {"state": "unknown", "job_state": "unknown"}
    state = _STATE_MAP.get(raw.strip().lower(), "unknown")
    out = {
        "state": state,
        "job_state": state,
        "raw_state": _clean(raw, 32),
        "nozzle": _temperature(detail.get("rightTemp")),
        "target_nozzle": _temperature(detail.get("rightTargetTemp")),
        "bed": _temperature(detail.get("platTemp")),
        "target_bed": _temperature(detail.get("platTargetTemp")),
        "firmware": _clean(detail.get("firmwareVersion"), 32),
        "printer_error_code": _clean(detail.get("errorCode"), 32),
        "layer": _non_negative_int(detail.get("printLayer")),
        "total_layers": _non_negative_int(detail.get("targetPrintLayer")),
    }
    if state == "unknown":
        return out
    name = detail.get("printFileName")
    if state != "idle" and isinstance(name, str):
        out["filename"] = _clean(name.replace("\\", "/").rsplit("/", 1)[-1], 128)
    ratio = _number(detail.get("printProgress"))
    if state != "idle" and ratio is not None and 0 <= ratio <= 1:
        out["progress"] = round(ratio * 100, 2)
    out["elapsed_seconds"] = _non_negative(detail.get("printDuration")) if state != "idle" else None
    # estimatedTime is frozen by the firmware while paused/heating/error:
    # expose it as remaining time only while actively printing.
    if raw.strip().lower() == "printing":
        out["remaining_seconds"] = _non_negative(detail.get("estimatedTime"))
    return out


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value


def _non_negative(value):
    value = _number(value)
    return value if value is not None and value >= 0 else None


def _non_negative_int(value):
    value = _non_negative(value)
    return int(value) if value is not None else None


def _temperature(value):
    value = _number(value)
    return value if value is not None and -20 <= value <= 500 else None


def _clean(value, limit):
    if not isinstance(value, str):
        return ""
    return _CONTROL_CHARS.sub("", value).strip()[:limit]


def _read_pid(detail: dict):
    """String -> base-16 (firmware form), int -> decimal; anything else unknown."""
    pid = detail.get("pid")
    if isinstance(pid, bool):
        return None
    if isinstance(pid, int):
        return pid if pid >= 0 else None
    if isinstance(pid, str) and _PID_HEX.fullmatch(pid.strip()):
        return int(pid.strip(), 16)
    return None


def _checked_detail(reply) -> dict:
    code = _envelope_code(reply)
    if code is None:
        raise _AdapterError("unrecognized_response")
    if code != 0:
        raise _AdapterError(_HTTP_CODE_MAP.get(code, "printer_rejected"))
    detail = reply.get("detail")
    if not isinstance(detail, dict) or not detail:
        raise _AdapterError("unrecognized_response")
    return detail


def _envelope_code(reply):
    """Integer ``code`` (int or numeric string, as the Orca client accepts)."""
    if not isinstance(reply, dict):
        return None
    value = reply.get("code")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d{1,6}", value.strip()):
        return int(value.strip())
    return None


def _result(ok: bool, error_code: str, outcome: str) -> dict:
    return {"ok": ok, "error_code": error_code, "outcome": outcome}


# --------------------------------------------------------------------------
# Validation helpers
# --------------------------------------------------------------------------

def _validate_filename(filename):
    if (
        not isinstance(filename, str)
        or len(filename) > 64
        or not _SAFE_FILENAME.fullmatch(filename)
        or not filename.lower().endswith(".gcode")
    ):
        raise _AdapterError("invalid_filename")
    return filename


def _credentials(serial_number, secret):
    for value in (serial_number, secret):
        if not isinstance(value, str) or not value.strip():
            raise _AdapterError("credentials_required")
        if len(value) > MAX_CREDENTIAL_LENGTH or _CONTROL_CHARS.search(value) or not value.isascii():
            raise _AdapterError("invalid_request")
    return serial_number.strip(), secret.strip()


def _parse_base(base):
    if not isinstance(base, str):
        raise _AdapterError("invalid_request")
    parsed = urllib.parse.urlsplit(base.strip())
    if parsed.scheme.lower() != "http" or not parsed.hostname:
        raise _AdapterError("invalid_request")
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise _AdapterError("invalid_request")
    if parsed.path not in ("", "/"):
        raise _AdapterError("invalid_request")
    try:
        port = parsed.port or HTTP_PORT
    except ValueError:
        raise _AdapterError("invalid_request") from None
    return parsed.hostname, port


def _preflight(base, secret, serial_number, action):
    """Identify the printer and require a state that allows the write."""
    current = poll(base, None, secret, serial_number=serial_number)
    if not current["online"] or not current["identity_verified"]:
        return _result(False, current.get("error_code") or "status_unavailable", "not_sent")
    pid = next(p for p, m in MODELS.items() if m["model"] == current["model"])
    entry = HARDWARE_VERIFIED.get(pid, {})
    if not entry.get(action) or (action == "start" and MODELS[pid]["family"] != "adventurer_5m"):
        return _result(False, "unsupported_unverified", "not_sent")
    if current["state"] != "idle":
        return _result(False, "printer_busy", "not_sent")
    return None


# --------------------------------------------------------------------------
# HTTP transport
# --------------------------------------------------------------------------

def _json_call(target, path, body, deadline, max_bytes, *, mutating=False):
    payload = json.dumps(body).encode("utf-8")
    conn = http.client.HTTPConnection(target[0], target[1], timeout=_left(deadline))
    dispatched = False
    try:
        conn.putrequest("POST", path, skip_accept_encoding=True)
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(len(payload)))
        conn.putheader("Connection", "close")
        conn.endheaders(payload)
        dispatched = True
        response = conn.getresponse()
        data = _read_response(response, max_bytes, deadline)
        if response.status != 200:
            raise _AdapterError("http_error")
        reply = _parse_json(data)
        if reply is None:
            raise _AdapterError("malformed_response")
        return reply
    except _AdapterError as exc:
        if mutating and dispatched and exc.code in ("timeout", "response_too_large", "malformed_response"):
            raise _AdapterError("unknown_outcome") from None
        raise
    except socket.timeout:
        raise _AdapterError("unknown_outcome" if mutating and dispatched else "timeout") from None
    except http.client.RemoteDisconnected:
        raise _AdapterError("unknown_outcome" if mutating and dispatched else "connection_failed") from None
    except http.client.HTTPException:
        raise _AdapterError("unknown_outcome" if mutating and dispatched else "malformed_response") from None
    except OSError:
        raise _AdapterError("unknown_outcome" if mutating and dispatched else "connection_failed") from None
    finally:
        conn.close()


def _read_response(response, max_bytes, deadline):
    if 300 <= response.status < 400:
        raise _AdapterError("redirect_rejected")
    length = response.getheader("Content-Length")
    if length and length.isdigit() and int(length) > max_bytes:
        raise _AdapterError("response_too_large")
    data = bytearray()
    while True:
        if time.monotonic() > deadline:
            raise _AdapterError("timeout")
        chunk = response.read1(16 * 1024)
        if not chunk:
            break
        data += chunk
        if len(data) > max_bytes:
            raise _AdapterError("response_too_large")
    return bytes(data)


def _parse_json(body):
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeError, ValueError):
        return None


def _left(deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        raise _AdapterError("timeout")
    return left


# --------------------------------------------------------------------------
# Opt-in read-only TCP fallback (port 8899)
# --------------------------------------------------------------------------

_TCP_TYPES = {
    "flashforge adventurer 5m": 0x23,
    "flashforge adventurer 5m pro": 0x24,
}
_TCP_STATE = {
    "READY": "idle",
    "IDLE": "idle",
    "BUILDING_COMPLETED": "complete",
    "PAUSED": "paused",
    "BUSY": "busy",
}
_NUM = r"(-?\d+(?:\.\d+)?)"


def poll_tcp(base: str, *, serial_number: str = "", timeout: float | None = None) -> dict:
    """Read-only status over TCP 8899 (``M601`` session, ``M115``, ``M119``, ``M105``, ``M27``, ``M602``).

    ``base`` is ``tcp://host[:8899]``. Takes the printer's single control
    session while running (other clients may be refused) and always releases
    it. Identity comes from the ``M115`` "Machine Type" line (exact match for
    the 5M / 5M Pro); AD5X and legacy models are not confirmed for TCP and
    report ``unsupported_model``. ``serial_number`` is optional: when given it
    must equal the ``SN:`` reported by ``M115`` (``identity_mismatch``).
    The TCP path never sets ``upload_ready`` or ``can_control``.
    """
    status = _empty_status()
    status["backend"] = "flashforge_tcp"
    status["upload_reason"] = "tcp_read_only"
    status["control_reason"] = "tcp_read_only"
    sock = None
    acquired = False
    try:
        host, port = _parse_tcp(base)
        expected = None
        if serial_number:
            expected = _credentials(serial_number, "x")[0]
        deadline = time.monotonic() + (STATUS_TIMEOUT if timeout is None else timeout)
        try:
            sock = socket.create_connection((host, port), timeout=_left(deadline))
        except socket.timeout:
            raise _AdapterError("timeout") from None
        except OSError:
            raise _AdapterError("connection_failed") from None
        reader = _TcpReader(sock, deadline)
        first = reader.command("M601 S1")
        if "control failed" in first.lower():
            raise _AdapterError("control_session_busy")
        if "control success" not in first.lower():
            raise _AdapterError("unrecognized_response")
        acquired = True
        info = reader.command("M115")
        machine = _tcp_field(info, "Machine Type")
        pid = _TCP_TYPES.get(machine.strip().lower()) if machine else None
        if pid is None:
            raise _AdapterError("unsupported_model" if machine else "identity_unverified")
        if expected is not None and _tcp_field(info, "SN").strip() != expected:
            raise _AdapterError("identity_mismatch")
        endstop = reader.command("M119")
        temps = reader.command("M105")
        progress = reader.command("M27")
        machine_status = _tcp_field(endstop, "MachineStatus").strip().upper()
        if not machine_status:
            raise _AdapterError("unrecognized_response")
        state = _tcp_state(machine_status)
        status.update(state=state, job_state=state, raw_state=_clean(machine_status, 32),
                      model=MODELS[pid]["model"], model_family=MODELS[pid]["family"],
                      identity_verified=True, firmware=_clean(_tcp_field(info, "Firmware"), 32))
        status.update(_tcp_temperatures(temps))
        if state != "idle":
            status["filename"] = _clean(_tcp_field(endstop, "CurrentFile").replace("\\", "/").rsplit("/", 1)[-1], 128)
            status.update(_tcp_progress(progress))
        status["online"] = True
        return status
    except _AdapterError as exc:
        status["error_code"] = exc.code
        if exc.code not in ("connection_failed", "timeout"):
            status["state"] = "unknown"
        return status
    except (OSError, ValueError, TypeError):
        status["error_code"] = "invalid_request"
        return status
    finally:
        if sock is not None:
            if acquired:
                try:
                    sock.settimeout(1.0)
                    sock.sendall(b"~M602\r\n")
                    sock.recv(512)
                except OSError:
                    pass
            try:
                sock.close()
            except OSError:
                pass


class _TcpReader:
    def __init__(self, sock, deadline):
        self.sock = sock
        self.deadline = deadline
        self.buffer = b""

    def command(self, text):
        self.sock.settimeout(_left(self.deadline))
        try:
            self.sock.sendall(("~" + text + "\r\n").encode("ascii"))
            while True:
                reply = self._complete()
                if reply is not None:
                    return reply
                self.sock.settimeout(_left(self.deadline))
                chunk = self.sock.recv(2048)
                if not chunk:
                    raise _AdapterError("connection_closed")
                self.buffer += chunk
                if len(self.buffer) > MAX_TCP_REPLY:
                    raise _AdapterError("response_too_large")
        except socket.timeout:
            raise _AdapterError("timeout") from None
        except OSError:
            raise _AdapterError("connection_failed") from None

    def _complete(self):
        text = self.buffer.decode("ascii", "replace")
        last = text.rstrip().rsplit("\n", 1)[-1].strip()
        done = (
            last == "ok"
            or (text.endswith("\n") and re.search(r"control failed", text, re.IGNORECASE))
            or (text.endswith("\n") and last.startswith("Error"))
        )
        if not done:
            return None
        self.buffer = b""
        return text


def _parse_tcp(base):
    if not isinstance(base, str):
        raise _AdapterError("invalid_request")
    parsed = urllib.parse.urlsplit(base.strip())
    if parsed.scheme.lower() != "tcp" or not parsed.hostname or parsed.path not in ("", "/") \
            or parsed.username is not None or parsed.query or parsed.fragment:
        raise _AdapterError("invalid_request")
    try:
        return parsed.hostname, parsed.port or TCP_PORT
    except ValueError:
        raise _AdapterError("invalid_request") from None


def _tcp_field(text, name):
    match = re.search(r"^%s:[ \t]*(.*?)\r?$" % re.escape(name), text, re.MULTILINE)
    return match.group(1) if match else ""


def _tcp_state(machine_status):
    if machine_status in _TCP_STATE:
        return _TCP_STATE[machine_status]
    if machine_status.startswith("BUILDING") and machine_status != "BUILDING_COMPLETED":
        return "printing"
    return "unknown"


def _tcp_temperatures(text):
    out = {}
    for key, label in (("nozzle", "T0"), ("bed", "B")):
        match = re.search(r"\b%s:\s*%s\s*/\s*%s" % (label, _NUM, _NUM), text)
        if match:
            current, target = _temperature(float(match.group(1))), _temperature(float(match.group(2)))
            out[key] = current
            out["target_" + key] = target
    return out


def _tcp_progress(text):
    out = {}
    match = re.search(r"SD printing byte\s+(\d+)\s*/\s*(\d+)", text)
    if match and int(match.group(2)) > 0 and int(match.group(1)) <= int(match.group(2)):
        out["progress"] = round(int(match.group(1)) * 100 / int(match.group(2)), 2)
    layer = re.search(r"Layer:\s*(\d+)\s*/\s*(\d+)", text)
    if layer:
        out["layer"], out["total_layers"] = int(layer.group(1)), int(layer.group(2))
    return out
