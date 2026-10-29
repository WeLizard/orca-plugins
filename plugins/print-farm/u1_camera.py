"""Explicit stock Snapmaker U1 snapshot monitoring, owned by an open overlay.

The RPC is websocket-only in Snapmaker/u1-moonraker repeater.py. A correlated
response acknowledges transport; only the subsequently fetched JPEG is shown.
No URL from a response is used, and no global stop-monitor command is sent.
"""
import base64
import email.utils
import hashlib
import http.client
import json
import os
import socket
import ssl
import threading
import time
import urllib.parse
import uuid

MAX_JPEG = 2 * 1024 * 1024
TIMEOUT = 4.0
KEEPALIVE = 2.0
MAX_MISSING_FRAMES = 3
_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class CameraError(Exception):
    pass


def _parts(base):
    if not isinstance(base, str) or any(ord(c) < 33 or ord(c) == 127 for c in base):
        raise CameraError("invalid_address")
    try:
        parsed = urllib.parse.urlsplit(base)
        port = parsed.port
    except ValueError as exc:
        raise CameraError("invalid_address") from exc
    decoded = urllib.parse.unquote(parsed.path)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or "\\" in decoded
            or any(ord(c) < 32 or ord(c) == 127 for c in decoded)
            or any(part in (".", "..") for part in decoded.split("/"))):
        raise CameraError("invalid_address")
    return parsed, port


class Session:
    """One websocket and at most one HTTP request, closed on every exit."""
    def __init__(self, base, key, context, primitives, is_current, publish):
        self.parsed, self.port = _parts(base)
        if (not isinstance(key, str) or len(key.encode("utf-8")) > 512
                or any(ord(c) < 32 or ord(c) == 127 for c in key)):
            raise CameraError("invalid_key")
        self.key, self.context, self.ws = key, context, primitives
        self.is_current, self.publish = is_current, publish
        self.id = uuid.uuid4().hex
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.sock = self.http = self.http_socket = None
        self.sequence = 0

    def _check(self):
        if self.stop.is_set() or not self.is_current():
            raise CameraError("session_expired")

    def cancel(self):
        self.stop.set()
        with self.lock:
            sock, connection, http_socket = self.sock, self.http, self.http_socket
            self.sock = self.http = self.http_socket = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        if connection is not None:
            active_socket = http_socket or connection.sock
            if active_socket is not None:
                try:
                    active_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            connection.close()

    def _connect(self):
        self._check()
        parsed = self.parsed
        raw = socket.create_connection((parsed.hostname, self.port or (443 if parsed.scheme == "https" else 80)), TIMEOUT)
        try:
            self._check()
            sock = ((self.context or ssl.create_default_context()).wrap_socket(raw, server_hostname=parsed.hostname)
                    if parsed.scheme == "https" else raw)
        except Exception:
            raw.close()
            raise
        with self.lock:
            self.sock = sock
        self._check()
        reader = self.ws._Reader(sock, time.monotonic() + TIMEOUT)
        nonce = base64.b64encode(os.urandom(16))
        host = parsed.hostname if ":" not in parsed.hostname else "[" + parsed.hostname + "]"
        if self.port is not None:
            host += ":" + str(self.port)
        path = parsed.path.rstrip("/") + "/websocket"
        request = ("GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                   "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n" %
                   (path, host, nonce.decode("ascii")))
        if self.key:
            request += "X-Api-Key: " + self.key + "\r\n"
        self._check()
        sock.sendall((request + "\r\n").encode("ascii"))
        lines = reader.read_headers().decode("latin-1").split("\r\n")
        if not lines[0].startswith("HTTP/1.1 101 ") and not lines[0].startswith("HTTP/1.0 101 "):
            raise CameraError("websocket_rejected")
        headers = {}
        for line in lines[1:]:
            name, sep, value = line.partition(":")
            if not sep or name.lower() in headers:
                raise CameraError("malformed_handshake")
            headers[name.lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1(nonce + _GUID).digest()).decode("ascii")
        if (headers.get("sec-websocket-accept") != expected
                or headers.get("upgrade", "").lower() != "websocket"
                or "upgrade" not in headers.get("connection", "").lower().split(", ")):
            raise CameraError("malformed_handshake")
        return sock, reader

    def _start(self, sock, reader, correlation):
        self._check()
        reader.deadline = time.monotonic() + TIMEOUT
        payload = {"jsonrpc": "2.0", "method": "camera.start_monitor", "id": correlation,
                   "params": {"domain": "lan", "interval": 0, "req_id": correlation}}
        self.ws._send_frame(sock, 1, json.dumps(payload, separators=(",", ":")).encode(), reader.deadline)
        for _ in range(32):
            self._check()
            raw = self.ws._read_message(sock, reader)
            if len(raw) > 64 * 1024:
                raise CameraError("response_too_large")
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise CameraError("malformed_rpc")
            if value.get("id") != correlation or type(value.get("id")) is not int:
                continue
            if (value.get("jsonrpc") != "2.0" or "error" in value or "result" not in value
                    or value["result"] is not None and not isinstance(value["result"], dict)):
                raise CameraError("rpc_rejected")
            result = value["result"] or {}
            if result.get("state") == "failed" or result.get("error"):
                raise CameraError("rpc_rejected")
            return
        raise CameraError("rpc_unmatched")

    def _jpeg(self):
        self._check()
        options = {"timeout": TIMEOUT}
        connection_type = http.client.HTTPConnection
        if self.parsed.scheme == "https":
            connection_type = http.client.HTTPSConnection
            options["context"] = self.context or ssl.create_default_context()
        owner = self
        class GuardedConnection(connection_type):
            def connect(self):
                owner._check()
                super().connect()
                with owner.lock:
                    owner.http_socket = self.sock
                # DNS/TCP/TLS can outlive a page or binding. Recheck before
                # HTTP headers containing the key are allowed onto the socket.
                owner._check()
            def send(self, data):
                owner._check()
                return super().send(data)
        connection = GuardedConnection(self.parsed.hostname, self.port, **options)
        with self.lock:
            self.http = connection
        try:
            self._check()
            path = self.parsed.path.rstrip("/") + "/server/files/camera/monitor.jpg"
            headers = {"Accept": "image/jpeg", "Cache-Control": "no-cache", "Connection": "close"}
            if self.key:
                headers["X-Api-Key"] = self.key
            connection.request("GET", path, headers=headers)
            with self.lock:
                self.http_socket = connection.sock
            self._check()
            response = connection.getresponse()
            if response.status == 404:
                return None
            if response.status != 200 or response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() != "image/jpeg":
                raise CameraError("frame_rejected")
            chunks, size = [], 0
            deadline = time.monotonic() + TIMEOUT
            while size <= MAX_JPEG:
                self._check()
                if time.monotonic() >= deadline:
                    raise CameraError("frame_timeout")
                chunk = response.read1(min(64 * 1024, MAX_JPEG + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            payload = b"".join(chunks)
            if len(payload) > MAX_JPEG or len(payload) < 4 or not payload.startswith(b"\xff\xd8") or not payload.endswith(b"\xff\xd9"):
                raise CameraError("invalid_jpeg")
            captured_at = None
            modified = response.getheader("Last-Modified")
            if modified:
                try:
                    captured_at = email.utils.parsedate_to_datetime(modified).timestamp()
                except (ValueError, TypeError, OverflowError):
                    captured_at = None
            if captured_at is not None and not -60 <= time.time() - captured_at <= 15:
                raise CameraError("stale_frame")
            self._check()
            return {"image": "data:image/jpeg;base64," + base64.b64encode(payload).decode("ascii"),
                    "received_at": time.time(), "captured_at": captured_at}
        finally:
            connection.close()
            with self.lock:
                if self.http is connection:
                    self.http = None
                    self.http_socket = None

    def run(self):
        try:
            sock, reader = self._connect()
            correlation, missing = 1, 0
            while True:
                started = time.monotonic()
                self._start(sock, reader, correlation)
                correlation = correlation % (2**31 - 1) + 1
                frame = self._jpeg()
                if frame is None:
                    missing += 1
                    if missing > MAX_MISSING_FRAMES:
                        raise CameraError("frame_unavailable")
                else:
                    missing = 0
                    self.sequence += 1
                    self._check()
                    self.publish({**frame, "sessionId": self.id, "sequence": self.sequence})
                if self.stop.wait(max(0, KEEPALIVE - (time.monotonic() - started))):
                    return
        except CameraError:
            raise
        except (OSError, ValueError, http.client.HTTPException, self.ws._AdapterError) as exc:
            raise CameraError("camera_unavailable") from exc
        finally:
            self.cancel()
