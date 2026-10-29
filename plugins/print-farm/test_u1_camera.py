"""U1 camera protocol emulation; never connects to a printer."""
import base64
import email.utils
import hashlib
import http.server
import json
import socket
import ssl
import struct
import threading
import time
from unittest import mock

import pytest
import creality_adapter
import u1_camera as camera

JPEG = b"\xff\xd8synthetic-frame\xff\xd9"


def server_frame(value):
    payload = json.dumps(value).encode()
    return bytes((0x81, len(payload))) + payload if len(payload) < 126 else b"\x81\x7e" + struct.pack("!H", len(payload)) + payload


def receive(sock, count):
    result = b""
    while len(result) < count:
        part = sock.recv(count-len(result))
        if not part:
            raise EOFError()
        result += part
    return result


def client_frame(sock):
    first, second = receive(sock, 2)
    assert first == 0x81 and second & 0x80
    size = second & 127
    if size == 126:
        size = struct.unpack("!H", receive(sock, 2))[0]
    assert size < 4096
    mask = receive(sock, 4); payload = receive(sock, size)
    return json.loads(bytes(c ^ mask[i % 4] for i, c in enumerate(payload)))


@pytest.fixture
def emulator():
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            srv = self.server
            srv.requests.append((self.path, dict(self.headers)))
            if self.path == "/proxy/websocket":
                if srv.handshake_status != 101:
                    self.send_response(srv.handshake_status)
                    self.send_header("Content-Length", "0"); self.end_headers(); return
                key = self.headers["Sec-WebSocket-Key"]
                accept = base64.b64encode(hashlib.sha1((key + camera._GUID.decode()).encode()).digest()).decode()
                self.protocol_version = "HTTP/1.1"
                self.send_response(101); self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade"); self.send_header("Sec-WebSocket-Accept", accept)
                self.end_headers(); self.wfile.flush()
                self.connection.settimeout(1)
                srv.sockets += 1
                try:
                    while True:
                        request = client_frame(self.connection); srv.commands.append(request)
                        if srv.notify:
                            self.connection.sendall(server_frame({"jsonrpc": "2.0", "method": "notify_camera_status_change",
                                "params": [{"monitoring": True, "monitor_domain": "lan"}]}))
                        reply = {"jsonrpc": "2.0", "id": request["id"] + (1 if srv.wrong_id else 0), "result": srv.result}
                        if srv.rpc_error:
                            reply.pop("result"); reply["error"] = {"code": -32601, "message": "not available"}
                        self.connection.sendall(server_frame(reply))
                except (OSError, EOFError):
                    pass
                finally:
                    srv.sockets -= 1
            elif self.path == "/proxy/server/files/camera/monitor.jpg":
                srv.frames += 1
                code = 404 if srv.frames <= srv.missing else srv.frame_status
                self.send_response(code); self.send_header("Content-Type", srv.mime)
                if srv.modified:
                    self.send_header("Last-Modified", srv.modified)
                self.send_header("Content-Length", str(len(srv.jpeg))); self.end_headers()
                if srv.stall_frame:
                    srv.frame_started.set()
                    srv.release_frame.wait(2)
                try:
                    self.wfile.write(srv.jpeg)
                except OSError:
                    pass
            else:
                self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    srv.base = "http://127.0.0.1:%d/proxy" % srv.server_port
    srv.requests, srv.commands = [], []
    srv.jpeg, srv.mime, srv.frame_status, srv.modified = JPEG, "image/jpeg", 200, None
    srv.handshake_status, srv.result, srv.rpc_error, srv.wrong_id, srv.notify = 101, {}, False, False, True
    srv.frames = srv.sockets = srv.missing = 0
    srv.stall_frame = False
    srv.frame_started, srv.release_frame = threading.Event(), threading.Event()
    thread = threading.Thread(target=srv.serve_forever, daemon=True); thread.start()
    try:
        yield srv
    finally:
        srv.release_frame.set()
        srv.shutdown(); srv.server_close(); thread.join()


def session(emulator, monkeypatch, *, frames=1, guard=lambda: True):
    monkeypatch.setattr(camera, "KEEPALIVE", 0.01)
    monkeypatch.setattr(camera, "TIMEOUT", 1.0)
    received = []
    def publish(value):
        received.append(value)
        if len(received) >= frames:
            client.cancel()
    client = camera.Session(emulator.base, "synthetic-key", None, creality_adapter, guard, publish)
    return client, received


def test_exact_websocket_contract_fixed_api_paths_and_key_header(emulator, monkeypatch):
    client, received = session(emulator, monkeypatch, frames=2)
    client.run()
    assert len(emulator.commands) == 2
    assert all(c == {"jsonrpc": "2.0", "method": "camera.start_monitor", "id": i,
                     "params": {"domain": "lan", "interval": 0, "req_id": i}}
               for i, c in enumerate(emulator.commands, 1))
    assert [f["sequence"] for f in received] == [1, 2]
    assert all(base64.b64decode(f["image"].split(",", 1)[1]) == JPEG for f in received)
    assert all(f["captured_at"] is None for f in received)
    assert all(r[1]["X-Api-Key"] == "synthetic-key" and "synthetic-key" not in r[0] for r in emulator.requests)
    assert not any("stop_monitor" in json.dumps(c) for c in emulator.commands)
    assert client.sock is None and client.http is None


def test_rpc_returned_url_is_never_followed(emulator, monkeypatch):
    emulator.result = {"state": "success", "url": "http://elsewhere.invalid/stolen"}
    client, received = session(emulator, monkeypatch)
    client.run()
    assert received and all(path in ("/proxy/websocket", "/proxy/server/files/camera/monitor.jpg")
                            for path, _ in emulator.requests)


@pytest.mark.parametrize("result", [None, {}])
def test_correlated_rpc_null_or_object_is_transport_only(emulator, monkeypatch, result):
    emulator.result = result
    client, received = session(emulator, monkeypatch); client.run()
    assert received


@pytest.mark.parametrize("failure", ["rpc_error", "wrong_id", "failed_state"])
def test_rpc_rejection_or_unmatched_id_never_fetches_jpeg(emulator, monkeypatch, failure):
    if failure == "failed_state":
        emulator.result = {"state": "failed"}
    else:
        setattr(emulator, failure, True)
    client, received = session(emulator, monkeypatch)
    with pytest.raises(Exception):
        client.run()
    assert not received and emulator.frames == 0 and client.sock is None


@pytest.mark.parametrize("status", [401, 403, 302])
def test_handshake_denial_or_redirect_has_no_fallback(emulator, monkeypatch, status):
    emulator.handshake_status = status
    client, received = session(emulator, monkeypatch)
    with pytest.raises(camera.CameraError):
        client.run()
    assert len(emulator.requests) == 1 and not received and client.sock is None


@pytest.mark.parametrize("mime,payload,status", [("text/html", JPEG, 200), ("image/jpeg", b"invalid", 200),
    ("image/jpeg", b"\xff\xd8" + b"x" * camera.MAX_JPEG + b"\xff\xd9", 200), ("image/jpeg", JPEG, 302),
    ("image/jpeg", JPEG, 401)], ids=["wrong-type", "bad-signature", "oversized", "redirect", "denied"])
def test_invalid_or_oversized_jpeg_or_redirect_closes_session(emulator, monkeypatch, mime, payload, status):
    emulator.mime, emulator.jpeg, emulator.frame_status = mime, payload, status
    client, received = session(emulator, monkeypatch)
    with pytest.raises(camera.CameraError):
        client.run()
    assert not received and client.sock is None and client.http is None


def test_bounded_transient_404_then_frame(emulator, monkeypatch):
    emulator.missing = 2
    client, received = session(emulator, monkeypatch); client.run()
    assert received and emulator.frames == 3


def test_permanent_404_stops_at_the_retry_bound(emulator, monkeypatch):
    emulator.missing = 99
    client, received = session(emulator, monkeypatch)
    with pytest.raises(camera.CameraError):
        client.run()
    assert not received and emulator.frames == camera.MAX_MISSING_FRAMES + 1


def test_stale_last_modified_is_rejected_and_recent_timestamp_preserved(emulator, monkeypatch):
    emulator.modified = email.utils.formatdate(time.time()-60, usegmt=True)
    client, received = session(emulator, monkeypatch)
    with pytest.raises(camera.CameraError, match="stale_frame"):
        client.run()
    assert not received
    emulator.modified = email.utils.formatdate(time.time(), usegmt=True)
    client, received = session(emulator, monkeypatch); client.run()
    assert received[0]["captured_at"] is not None


def test_expired_or_cancelled_before_connect_has_no_network(emulator, monkeypatch):
    for expired in (False, True):
        client, received = session(emulator, monkeypatch, guard=lambda: not expired)
        if not expired:
            client.cancel()
        with pytest.raises(camera.CameraError):
            client.run()
    assert not emulator.requests


@pytest.mark.parametrize("change", ["close", "lease"])
def test_cancel_or_lease_expiry_during_body_read_ends_worker_and_socket(emulator, monkeypatch, change):
    emulator.stall_frame = True
    valid = threading.Event(); valid.set()
    client, frames = session(emulator, monkeypatch, guard=valid.is_set)
    failures = []
    def run():
        try:
            client.run()
        except Exception as exc:
            failures.append(type(exc).__name__)
    worker = threading.Thread(target=run)
    worker.start()
    assert emulator.frame_started.wait(1)
    valid.clear()
    if change == "close":
        client.cancel()
    worker.join(2)
    emulator.release_frame.set()
    assert not worker.is_alive()
    assert not frames and client.sock is None and client.http is None
    deadline = time.monotonic()+1
    while emulator.sockets and time.monotonic() < deadline:
        time.sleep(0.01)
    assert emulator.sockets == 0


def test_repeated_open_frame_close_returns_owned_socket_count_to_zero(emulator, monkeypatch):
    for _ in range(3):
        client, frames = session(emulator, monkeypatch)
        client.run()
        assert frames and client.sock is None and client.http is None
    deadline = time.monotonic()+1
    while emulator.sockets and time.monotonic() < deadline:
        time.sleep(0.01)
    assert emulator.sockets == 0


def test_wss_uses_provided_tls_context_before_any_api_key_is_sent():
    context = mock.Mock()
    context.wrap_socket.side_effect = ssl.SSLCertVerificationError("synthetic certificate rejection")
    raw = mock.Mock()
    client = camera.Session("https://synthetic.invalid:7443/proxy", "synthetic-key", context,
                            creality_adapter, lambda: True, mock.Mock())
    with mock.patch.object(camera.socket, "create_connection", return_value=raw):
        with pytest.raises(camera.CameraError):
            client.run()
    context.wrap_socket.assert_called_once_with(raw, server_hostname="synthetic.invalid")
    raw.sendall.assert_not_called(); raw.close.assert_called()


def test_binding_expiring_during_http_connect_sends_no_key_headers(emulator, monkeypatch):
    valid = threading.Event(); valid.set()
    client, frames = session(emulator, monkeypatch, guard=valid.is_set)
    original = camera.http.client.HTTPConnection.connect
    def connect(connection):
        original(connection)
        valid.clear()
    with mock.patch.object(camera.http.client.HTTPConnection, "connect", connect):
        with pytest.raises(camera.CameraError):
            client._jpeg()
    assert not frames and not emulator.requests
    assert client.http is None
