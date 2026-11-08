"""Explicit stock Snapmaker U1 snapshot monitoring, owned by an open overlay.

The RPC is websocket-only in Snapmaker/u1-moonraker repeater.py. A correlated
response acknowledges transport; only the subsequently fetched JPEG is shown.
No URL from a response is used, and no global stop-monitor command is sent.

Protocol reference: lanwen/orca-snapmaker-u1-camera-plugin v0.5.0,
df55c9124ebe038f85cb1d1180592d14951773db. The one-shot authentication
flow follows that implementation. Its MIT notice is retained below.

Copyright (c) 2026 lanwen

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
import base64
import email.utils
import hashlib
import http.client
import ipaddress
import io
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
FRAME_INTERVAL = 0.25
MISSING_GRACE = 6.0
MAX_TOKEN_RESPONSE = 4096
_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _connect_tcp(host, port, timeout):
    """A TCP connection that OrcaSlicer can attribute to the host the user saved.

    The host asks the user before a plugin connects. For a host name,
    ``socket.create_connection`` reports the address the name resolved to, which
    no saved permission names; ``http.client`` reports the name itself and the
    host covers the connection it opens. Literal addresses connect directly.
    """
    try:
        ipaddress.ip_address(host)
    except ValueError:
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
        connection.connect()
        raw, connection.sock = connection.sock, None
        connection.close()
        return raw
    return socket.create_connection((host, port), timeout=timeout)


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
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.sock = self.http = self.http_socket = None
        self.sequence = 0
        self.pending_sequence = None

    def acknowledge(self, sequence):
        with self.lock:
            if type(sequence) is not int or sequence != self.pending_sequence:
                return False
            self.pending_sequence = None
        self.wake.set()
        return True

    def _check(self):
        if self.stop.is_set() or not self.is_current():
            raise CameraError("session_expired")

    def cancel(self):
        self.stop.set()
        self.wake.set()
        with self.lock:
            sock, connection, http_socket = self.sock, self.http, self.http_socket
            self.sock = None
            self.pending_sequence = None
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
            # The HTTP worker owns final response/file/connection close.
            # Cancellation only interrupts its current read.

    def _connect(self):
        self._check()
        parsed = self.parsed
        token = self._one_shot_token() if self.key else None
        self._check()
        raw = _connect_tcp(parsed.hostname, self.port or (443 if parsed.scheme == "https" else 80), TIMEOUT)
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
        if token is not None:
            path += "?token=" + urllib.parse.quote(token, safe="")
        request = ("GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                   "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n" %
                   (path, host, nonce.decode("ascii")))
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

    def _get(self, path, accept, limit, deadline):
        self._check()
        def remaining():
            value = deadline - time.monotonic()
            if value <= 0:
                raise CameraError("frame_timeout")
            return value
        options = {"timeout": remaining()}
        connection_type = http.client.HTTPConnection
        if self.parsed.scheme == "https":
            connection_type = http.client.HTTPSConnection
            options["context"] = self.context or ssl.create_default_context()
        owner = self
        class GuardedConnection(connection_type):
            def connect(self):
                owner._check()
                self.timeout = remaining()
                super().connect()
                with owner.lock:
                    owner.http_socket = self.sock
                # DNS/TCP/TLS can outlive a page or binding. Recheck before
                # HTTP headers containing the key are allowed onto the socket.
                owner._check()
                remaining()
            def send(self, data):
                owner._check()
                if self.sock is None:
                    self.connect()
                owner._check()
                self.sock.settimeout(remaining())
                owner._check()
                return super().send(data)
        connection = GuardedConnection(self.parsed.hostname, self.port, **options)
        with self.lock:
            self.http = connection
        response = None
        try:
            self._check()
            headers = {"Accept": accept, "Cache-Control": "no-cache", "Connection": "close"}
            if self.key:
                headers["X-Api-Key"] = self.key
            connection.request("GET", path, headers=headers)
            with self.lock:
                self.http_socket = connection.sock
            self._check()
            # Keep ownership of the socket until the response is consumed.
            # HTTPConnection.getresponse() otherwise closes its socket object
            # on Connection: close, while HTTPResponse still owns its file.
            # Checking every raw read also bounds slowly dribbled headers.
            with self.lock:
                active_socket = self.http_socket
            self._check()
            if active_socket is None:
                raise CameraError("camera_unavailable")

            class DeadlineRaw(io.RawIOBase):
                def __init__(self, source):
                    super().__init__()
                    self.source = source

                def readable(self):
                    return True

                def fileno(self):
                    return self.source.fileno()

                def readinto(self, buffer):
                    owner._check()
                    active_socket.settimeout(remaining())
                    count = self.source.readinto(buffer)
                    owner._check()
                    remaining()
                    return count

                def close(self):
                    if not self.closed:
                        try:
                            self.source.close()
                        finally:
                            super().close()

            class ResponseSocket:
                def makefile(self, mode):
                    return io.BufferedReader(DeadlineRaw(active_socket.makefile(mode, buffering=0)))

            response = http.client.HTTPResponse(ResponseSocket())
            response.begin()
            self._check()
            remaining()
            if response.status == 404:
                return None, None
            if response.status != 200 or response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() != accept:
                raise CameraError("request_rejected")
            chunks, size = [], 0
            while size <= limit:
                self._check()
                remaining()
                chunk = response.read1(min(64 * 1024, limit + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            payload = b"".join(chunks)
            if response.length not in (None, 0):
                raise CameraError("incomplete_response")
            if len(payload) > limit:
                raise CameraError("response_too_large")
            self._check()
            remaining()
            return payload, response.getheader("Last-Modified")
        except TimeoutError as exc:
            raise CameraError("frame_timeout") from exc
        finally:
            try:
                if response is not None:
                    response.close()
            finally:
                connection.close()
                with self.lock:
                    if self.http is connection:
                        self.http = None
                        self.http_socket = None

    def _one_shot_token(self):
        path = self.parsed.path.rstrip("/") + "/access/oneshot_token"
        payload, _ = self._get(path, "application/json", MAX_TOKEN_RESPONSE, time.monotonic() + TIMEOUT)
        try:
            value = json.loads(payload.decode("utf-8"))
            token = value.get("result") if isinstance(value, dict) else None
        except (AttributeError, ValueError, UnicodeError) as exc:
            raise CameraError("invalid_token") from exc
        if (not isinstance(token, str) or not token or len(token.encode("utf-8")) > 512
                or any(ord(c) < 33 or ord(c) == 127 for c in token)):
            raise CameraError("invalid_token")
        self._check()
        return token

    def _jpeg(self, deadline=None):
        path = self.parsed.path.rstrip("/") + "/server/files/camera/monitor.jpg?cacheBust=" + str(time.monotonic_ns())
        payload, modified = self._get(path, "image/jpeg", MAX_JPEG,
                                     deadline if deadline is not None else time.monotonic() + TIMEOUT)
        if payload is None:
            return None
        if len(payload) < 4 or not payload.startswith(b"\xff\xd8") or not payload.endswith(b"\xff\xd9"):
            raise CameraError("invalid_jpeg")
        captured_at = None
        if modified:
            try:
                captured_at = email.utils.parsedate_to_datetime(modified).timestamp()
            except (ValueError, TypeError, OverflowError):
                pass
        if captured_at is not None and not -60 <= time.time() - captured_at <= 15:
            raise CameraError("stale_frame")
        self._check()
        return {"image": "data:image/jpeg;base64," + base64.b64encode(payload).decode("ascii"),
                "received_at": time.time(), "captured_at": captured_at}

    def run(self):
        try:
            sock, reader = self._connect()
            correlation, missing_since = 1, None
            next_monitor = next_frame = time.monotonic()
            while True:
                self.wake.clear()
                self._check()
                now = time.monotonic()
                if now >= next_monitor:
                    next_monitor = now + KEEPALIVE
                    self._start(sock, reader, correlation)
                    correlation = correlation % (2**31 - 1) + 1
                    if next_monitor <= time.monotonic():
                        next_monitor = time.monotonic() + KEEPALIVE
                self._check()
                with self.lock:
                    ready = self.pending_sequence is None
                if ready and time.monotonic() >= next_frame:
                    attempted_at = time.monotonic()
                    try:
                        frame = self._jpeg(min(next_monitor, time.monotonic() + TIMEOUT))
                    except CameraError as exc:
                        if str(exc) != "frame_timeout":
                            raise
                        frame = None
                    now = time.monotonic()
                    next_frame = now + FRAME_INTERVAL
                    if frame is None:
                        if missing_since is None:
                            missing_since = attempted_at
                        if now - missing_since >= MISSING_GRACE:
                            raise CameraError("frame_unavailable")
                    else:
                        missing_since = None
                        self._check()
                        with self.lock:
                            self.sequence += 1
                            self.pending_sequence = self.sequence
                        self.publish({**frame, "sessionId": self.id, "sequence": self.sequence})
                with self.lock:
                    ready = self.pending_sequence is None
                until = min(next_monitor, next_frame) if ready else next_monitor
                self.wake.wait(max(0, until - time.monotonic()))
                if self.stop.is_set():
                    return
        except CameraError:
            raise
        except (OSError, ValueError, http.client.HTTPException, self.ws._AdapterError) as exc:
            raise CameraError("camera_unavailable") from exc
        finally:
            self.cancel()
