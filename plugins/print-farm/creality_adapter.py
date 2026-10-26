"""Creality stock firmware adapter for the Ender 5 Max (DWIN-style WebSocket).

Wire facts come only from the 3D Vault ``creality-websocket`` driver plugin
(commit dc217534c74177048d715c68fc091fed33d565b9, see creality/SOURCE_NOTES.md):
an unauthenticated ``ws://host:port/`` text-frame JSON channel and a plain HTTP
multipart upload. Nothing here is transferred from the K1/K2 code in Orca.

Not confirmed by that source and therefore reported as ``None``/``False``:
temperatures, pause/cancel, print start acknowledgement, authentication, and
any proof that the upload target is writable. ``upload_ready`` and
``can_control`` stay ``False`` until hardware evidence exists; print start is
deliberately not implemented.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import math
import os
import re
import socket
import ssl
import struct
import time
import urllib.parse
from pathlib import Path


STATUS_TIMEOUT = 4.0          # overall deadline for one status exchange
UPLOAD_TIMEOUT = 30.0         # per socket operation while streaming
MAX_FRAME_BYTES = 256 * 1024  # status JSON is tiny; larger frames are hostile
MAX_HANDSHAKE_BYTES = 8 * 1024
MAX_CONTROL_FRAMES = 8
MAX_UPLOAD_BYTES = 512 * 1024 * 1024
CHUNK_SIZE = 64 * 1024
_WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_STATUS_REQUEST = {"method": "get", "params": {"ReqPrinterPara": 1}}
# Source STATE_MAP; only meaningful while printFileName is non-empty.
_STATE_MAP = {1: "printing", 5: "paused", 2: "complete", 3: "failed", 4: "aborted"}
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ +()\-]*$")
_UPLOAD_OK_STATES = {"idle", "complete", "failed", "aborted"}


class _AdapterError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def ws_base(upload_url: str, ws_port: int) -> str:
    """Build the status base the way the source driver does: upload host + ws_port."""
    parsed = _parse_http(upload_url)
    port = int(ws_port)
    if not 1 <= port <= 65535:
        raise ValueError("invalid WebSocket port")
    host = parsed.hostname
    return "ws://%s:%d" % ("[%s]" % host if ":" in host else host, port)


def poll(base: str, ssl_context: ssl.SSLContext | None = None, secret: str = "") -> dict:
    """Read printer status over the stock WebSocket; never sends a mutating command.

    ``base`` is ``ws://host:port``. ``ssl_context`` and ``secret`` are accepted
    for interface parity: the confirmed protocol has no TLS and no credentials,
    so neither is used or echoed anywhere.
    """
    status = _empty_status()
    try:
        message = _ws_request(base, _STATUS_REQUEST, STATUS_TIMEOUT)
        status.update(_normalize(message))
        status["online"] = True
        status["backend"] = "creality_stock"
        status["can_control"] = False
        status["upload_ready"] = False
        status["error_code"] = "writable_target_unverified"
        return status
    except _AdapterError as exc:
        status["error_code"] = exc.code
        return status
    except (OSError, ValueError):
        status["error_code"] = "connection_failed"
        return status


def upload(
    base: str,
    ssl_context: ssl.SSLContext | None,
    secret: str,
    file_path: str | os.PathLike[str],
    filename: str,
    *,
    status_base: str,
) -> dict:
    """POST a G-code as multipart to ``<base>/<filename>``; never starts a print.

    ``base`` is the configured ``upload_url``; ``status_base`` (``ws://host:port``)
    is required so the printer can be checked idle first. A failure after the
    request headers were sent is ``unknown_outcome`` and must not be retried
    blindly.
    """
    try:
        safe_name = _validate_filename(filename)
        parsed = _parse_http(base)
        path = Path(file_path)
        size = path.stat().st_size
        if not path.is_file() or size <= 0 or size > MAX_UPLOAD_BYTES:
            return {"ok": False, "error_code": "invalid_request"}
        current = poll(status_base, None, "")
        if not current["online"]:
            return {"ok": False, "error_code": current.get("error_code") or "status_unavailable"}
        if current["state"] not in _UPLOAD_OK_STATES:
            return {"ok": False, "error_code": "printer_busy"}
        target = (parsed.path.rstrip("/") or "") + "/" + urllib.parse.quote(safe_name, safe="")
        boundary = "pf" + hashlib.sha256(os.urandom(16)).hexdigest()[:24]
        head = (
            "--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n" % (boundary, safe_name)
        ).encode("ascii")
        tail = ("\r\n--%s--\r\n" % boundary).encode("ascii")
        conn = _http_connection(parsed, ssl_context)
        dispatched = False
        try:
            conn.putrequest("POST", target)
            conn.putheader("Content-Type", "multipart/form-data; boundary=" + boundary)
            conn.putheader("Content-Length", str(len(head) + size + len(tail)))
            conn.putheader("Connection", "close")
            conn.endheaders()
            dispatched = True
            conn.send(head)
            remaining = size
            with path.open("rb") as source:
                while remaining:
                    chunk = source.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        raise OSError("source file changed during upload")
                    conn.send(chunk)
                    remaining -= len(chunk)
            conn.send(tail)
            response = conn.getresponse()
            response.read(64 * 1024)
            code = response.status
            if 200 <= code < 300:
                return {"ok": True, "evidence_ref": "creality-http-%d" % code}
            if 300 <= code < 400:
                return {"ok": False, "error_code": "redirect_rejected"}
            if code in (401, 403):
                return {"ok": False, "error_code": "authentication_failed"}
            return {"ok": False, "error_code": "upload_rejected"}
        except (OSError, http.client.HTTPException):
            return {"ok": False, "error_code": "unknown_outcome" if dispatched else "connection_failed"}
        finally:
            conn.close()
    except _AdapterError as exc:
        return {"ok": False, "error_code": exc.code}
    except (OSError, ValueError, TypeError):
        return {"ok": False, "error_code": "invalid_request"}


def _empty_status() -> dict:
    return {
        "online": False,
        "state": "offline",
        "job_state": "unknown",
        "nozzle": None,
        "target_nozzle": None,
        "bed": None,
        "target_bed": None,
        "progress": None,
        "filename": "",
        "thumbnail": "",
        "upload_ready": False,
        "can_control": False,
        "elapsed_seconds": None,
        "remaining_seconds": None,
        "layer": None,
        "total_layers": None,
    }


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value


def _normalize(data: dict) -> dict:
    name = data.get("printFileName")
    out = {"nozzle": None, "target_nozzle": None, "bed": None, "target_bed": None, "thumbnail": ""}
    if not isinstance(name, str):
        # Missing/odd file name: the idle-vs-job rule cannot be applied.
        out.update(state="unknown", job_state="unknown", progress=None, filename="")
        return out
    name = name.strip()
    if name == "":
        # Source rule: no file name means no job, regardless of the state field.
        out.update(state="idle", job_state="idle", progress=None, filename="")
        return out
    raw = data.get("state")
    mapped = _STATE_MAP.get(raw) if isinstance(raw, int) and not isinstance(raw, bool) else None
    state = mapped or "unknown"
    progress = _number(data.get("printProgress"))
    if progress is not None and not 0 <= progress <= 100:
        progress = None
    out.update(
        state=state,
        job_state=state,
        progress=progress,
        filename=name.rsplit("/", 1)[-1],
        elapsed_seconds=_non_negative(data.get("printJobTime")),
        remaining_seconds=_non_negative(data.get("printLeftTime")),
        layer=_non_negative(data.get("layer")),
        total_layers=_non_negative(data.get("TotalLayer")),
    )
    return out


def _non_negative(value):
    value = _number(value)
    return value if value is not None and value >= 0 else None


def _validate_filename(filename):
    if (
        not isinstance(filename, str)
        or len(filename) > 128
        or not _SAFE_FILENAME.fullmatch(filename)
        or not filename.lower().endswith(".gcode")
    ):
        raise _AdapterError("invalid_filename")
    return filename


def _parse_http(base):
    if not isinstance(base, str):
        raise ValueError("invalid base URL")
    parsed = urllib.parse.urlsplit(base.strip())
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise ValueError("invalid base URL")
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ValueError("credentials, query and fragment are forbidden")
    parsed.port  # validate malformed ports
    return parsed


def _http_connection(parsed, ssl_context):
    options = {"timeout": UPLOAD_TIMEOUT}
    if parsed.scheme.lower() == "https":
        options["context"] = ssl_context or ssl.create_default_context()
        return http.client.HTTPSConnection(parsed.hostname, parsed.port, **options)
    return http.client.HTTPConnection(parsed.hostname, parsed.port, **options)


# --- minimal RFC 6455 client -------------------------------------------------

def _ws_request(base, payload, timeout):
    if not isinstance(base, str):
        raise _AdapterError("invalid_request")
    parsed = urllib.parse.urlsplit(base.strip())
    if parsed.scheme.lower() != "ws" or not parsed.hostname or parsed.port is None:
        raise _AdapterError("invalid_request")
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise _AdapterError("invalid_request")
    if parsed.path not in ("", "/"):
        raise _AdapterError("invalid_request")
    deadline = time.monotonic() + timeout
    try:
        sock = socket.create_connection((parsed.hostname, parsed.port), timeout=timeout)
    except socket.timeout:
        raise _AdapterError("timeout") from None
    except OSError:
        raise _AdapterError("connection_failed") from None
    try:
        reader = _Reader(sock, deadline)
        _handshake(sock, reader, parsed)
        _send_frame(sock, 0x1, json.dumps(payload).encode("utf-8"), deadline)
        raw = _read_message(sock, reader)
    except socket.timeout:
        raise _AdapterError("timeout") from None
    except _AdapterError:
        raise
    except OSError:
        raise _AdapterError("connection_failed") from None
    finally:
        try:
            sock.close()
        except OSError:
            pass
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise _AdapterError("malformed_response") from None
    if not isinstance(value, dict) or "state" not in value:
        raise _AdapterError("unrecognized_response")
    return value


class _Reader:
    def __init__(self, sock, deadline):
        self.sock = sock
        self.deadline = deadline
        self.buffer = b""

    def _wait(self):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise socket.timeout()
        self.sock.settimeout(left)

    def read(self, count):
        while len(self.buffer) < count:
            self._wait()
            chunk = self.sock.recv(min(4096, max(count - len(self.buffer), 1)))
            if not chunk:
                raise _AdapterError("connection_closed")
            self.buffer += chunk
        data, self.buffer = self.buffer[:count], self.buffer[count:]
        return data

    def read_headers(self):
        while b"\r\n\r\n" not in self.buffer:
            if len(self.buffer) > MAX_HANDSHAKE_BYTES:
                raise _AdapterError("handshake_failed")
            self._wait()
            chunk = self.sock.recv(1024)
            if not chunk:
                raise _AdapterError("connection_closed")
            self.buffer += chunk
        head, _, self.buffer = self.buffer.partition(b"\r\n\r\n")
        if len(head) > MAX_HANDSHAKE_BYTES:
            raise _AdapterError("handshake_failed")
        return head


def _handshake(sock, reader, parsed):
    key = base64.b64encode(os.urandom(16))
    host = parsed.hostname if ":" not in parsed.hostname else "[%s]" % parsed.hostname
    request = (
        "GET / HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n"
        % (host, parsed.port, key.decode("ascii"))
    ).encode("ascii")
    reader._wait()
    sock.sendall(request)
    lines = reader.read_headers().decode("latin-1").split("\r\n")
    parts = lines[0].split(" ", 2)
    if len(parts) < 2 or not parts[0].startswith("HTTP/1."):
        raise _AdapterError("handshake_failed")
    if parts[1] in ("401", "403"):
        raise _AdapterError("authentication_failed")
    if parts[1] in ("301", "302", "303", "307", "308"):
        raise _AdapterError("redirect_rejected")
    if parts[1] != "101":
        raise _AdapterError("handshake_failed")
    headers = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if not sep:
            raise _AdapterError("handshake_failed")
        headers[name.strip().lower()] = value.strip()
    expected = base64.b64encode(hashlib.sha1(key + _WS_GUID).digest()).decode("ascii")
    if (
        headers.get("upgrade", "").lower() != "websocket"
        or "upgrade" not in headers.get("connection", "").lower()
        or headers.get("sec-websocket-accept") != expected
    ):
        raise _AdapterError("handshake_failed")


def _send_frame(sock, opcode, payload, deadline):
    mask = os.urandom(4)
    length = len(payload)
    if length < 126:
        head = struct.pack("!BB", 0x80 | opcode, 0x80 | length)
    elif length < 65536:
        head = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, length)
    else:
        head = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, length)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    left = deadline - time.monotonic()
    if left <= 0:
        raise socket.timeout()
    sock.settimeout(left)
    sock.sendall(head + mask + masked)


def _read_frame(reader):
    b0, b1 = reader.read(2)
    fin, rsv, opcode = b0 & 0x80, b0 & 0x70, b0 & 0x0F
    if rsv or b1 & 0x80:  # reserved bits or a masked server frame
        raise _AdapterError("malformed_frame")
    length = b1 & 0x7F
    if length == 126:
        length = struct.unpack("!H", reader.read(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", reader.read(8))[0]
    if length > MAX_FRAME_BYTES:
        raise _AdapterError("frame_too_large")
    if opcode >= 0x8 and (length > 125 or not fin):
        raise _AdapterError("malformed_frame")
    return bool(fin), opcode, reader.read(length)


def _read_message(sock, reader):
    parts = []
    total = 0
    started = False
    controls = 0
    while True:
        fin, opcode, payload = _read_frame(reader)
        if opcode == 0x8:
            raise _AdapterError("connection_closed")
        if opcode == 0x9:
            controls += 1
            if controls > MAX_CONTROL_FRAMES:
                raise _AdapterError("malformed_frame")
            _send_frame(sock, 0xA, payload, reader.deadline)
            continue
        if opcode == 0xA:
            controls += 1
            if controls > MAX_CONTROL_FRAMES:
                raise _AdapterError("malformed_frame")
            continue
        if opcode == 0x1 and not started:
            started = True
        elif opcode == 0x0 and started:
            pass
        else:  # binary data, unknown opcodes, or bad continuation
            raise _AdapterError("malformed_frame")
        total += len(payload)
        if total > MAX_FRAME_BYTES:
            raise _AdapterError("frame_too_large")
        parts.append(payload)
        if fin:
            return b"".join(parts)
