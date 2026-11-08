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
import urllib.parse
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
            self.server.handlers.add(threading.current_thread())
            try:
                self.handle_get()
            finally:
                self.server.handlers.discard(threading.current_thread())
        def handle_get(self):
            srv = self.server
            srv.requests.append((self.path, dict(self.headers)))
            path = urllib.parse.urlsplit(self.path).path
            if path == "/proxy/access/oneshot_token":
                payload = json.dumps(srv.token_result).encode() if srv.token_body is None else srv.token_body
                self.send_response(srv.token_status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload) + srv.token_length_extra)); self.end_headers()
                if srv.stall_token:
                    srv.token_started.set()
                    srv.release_token.wait(2)
                try:
                    self.wfile.write(payload)
                except OSError:
                    pass
            elif path == "/proxy/websocket":
                srv.websocket_times.append(time.monotonic())
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
            elif path == "/proxy/server/files/camera/monitor.jpg":
                srv.frames += 1
                srv.frame_times.append(time.monotonic())
                if srv.drip:
                    head = ("HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(srv.jpeg)).encode()
                    slow = head if srv.drip == "headers" else srv.jpeg
                    if srv.drip == "body":
                        self.connection.sendall(head)
                    for byte in slow:
                        try:
                            self.connection.sendall(bytes((byte,)))
                        except OSError:
                            break
                        if srv.drip_stop.wait(0.02):
                            break
                    return
                code = 404 if srv.frames <= srv.missing else srv.frame_status
                self.send_response(code); self.send_header("Content-Type", srv.mime)
                if srv.modified:
                    self.send_header("Last-Modified", srv.modified)
                if srv.chunked:
                    self.send_header("Transfer-Encoding", "chunked")
                else:
                    self.send_header("Content-Length", str(len(srv.jpeg) + srv.frame_length_extra))
                self.end_headers()
                if srv.stall_frame:
                    srv.frame_started.set()
                    srv.release_frame.wait(2)
                try:
                    self.wfile.write((b"%x\r\n" % len(srv.jpeg)) + srv.jpeg + b"\r\n0\r\n\r\n"
                                     if srv.chunked else srv.jpeg)
                except OSError:
                    pass
            else:
                self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    srv.base = "http://127.0.0.1:%d/proxy" % srv.server_port
    srv.requests, srv.commands = [], []
    srv.handlers = set()
    srv.jpeg, srv.mime, srv.frame_status, srv.modified = JPEG, "image/jpeg", 200, None
    srv.handshake_status, srv.result, srv.rpc_error, srv.wrong_id, srv.notify = 101, {}, False, False, True
    srv.frames = srv.sockets = srv.missing = 0
    srv.token_result, srv.token_body, srv.token_status = {"result": "one/shot?+&=value"}, None, 200
    srv.token_length_extra = srv.frame_length_extra = 0
    srv.chunked = srv.stall_token = False
    srv.token_started, srv.release_token = threading.Event(), threading.Event()
    srv.websocket_times, srv.frame_times = [], []
    srv.stall_frame = False
    srv.drip, srv.drip_stop = None, threading.Event()
    srv.frame_started, srv.release_frame = threading.Event(), threading.Event()
    thread = threading.Thread(target=srv.serve_forever, daemon=True); thread.start()
    try:
        yield srv
    finally:
        srv.drip_stop.set()
        srv.release_token.set()
        srv.release_frame.set()
        srv.shutdown(); srv.server_close(); thread.join()


def session(emulator, monkeypatch, *, frames=1, guard=lambda: True):
    monkeypatch.setattr(camera, "KEEPALIVE", 0.1)
    monkeypatch.setattr(camera, "FRAME_INTERVAL", 0.005)
    monkeypatch.setattr(camera, "MISSING_GRACE", 0.3)
    monkeypatch.setattr(camera, "TIMEOUT", 1.0)
    received = []
    def publish(value):
        received.append(value)
        client.acknowledge(value["sequence"])
        if len(received) >= frames:
            client.cancel()
    client = camera.Session(emulator.base, "synthetic-key", None, creality_adapter, guard, publish)
    return client, received


def test_exact_websocket_contract_fixed_api_paths_and_key_header(emulator, monkeypatch):
    client, received = session(emulator, monkeypatch, frames=2)
    client.run()
    assert emulator.commands
    assert all(c == {"jsonrpc": "2.0", "method": "camera.start_monitor", "id": i,
                     "params": {"domain": "lan", "interval": 0, "req_id": i}}
               for i, c in enumerate(emulator.commands, 1))
    assert [f["sequence"] for f in received] == [1, 2]
    assert all(base64.b64decode(f["image"].split(",", 1)[1]) == JPEG for f in received)
    assert all(f["captured_at"] is None for f in received)
    assert all(r[1].get("X-Api-Key") == "synthetic-key" for r in emulator.requests if "/websocket" not in r[0])
    assert all("synthetic-key" not in r[0] for r in emulator.requests)
    websocket = next(r for r in emulator.requests if "/websocket" in r[0])
    assert "X-Api-Key" not in websocket[1]
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(websocket[0]).query) == {"token": ["one/shot?+&=value"]}
    assert not any("stop_monitor" in json.dumps(c) for c in emulator.commands)
    assert client.sock is None and client.http is None


def test_rpc_returned_url_is_never_followed(emulator, monkeypatch):
    emulator.result = {"state": "success", "url": "http://elsewhere.invalid/stolen"}
    client, received = session(emulator, monkeypatch)
    client.run()
    assert received and all(urllib.parse.urlsplit(path).path in ("/proxy/access/oneshot_token", "/proxy/websocket", "/proxy/server/files/camera/monitor.jpg")
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
    assert len(emulator.requests) == 2 and not received and client.sock is None


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


def test_permanent_404_stops_after_elapsed_grace(emulator, monkeypatch):
    emulator.missing = 99
    client, received = session(emulator, monkeypatch)
    started = time.monotonic()
    with pytest.raises(camera.CameraError):
        client.run()
    assert not received and emulator.frames >= 1
    assert camera.MISSING_GRACE <= time.monotonic() - started < 1


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
    for _ in range(20):  # One warm-up and nineteen repeated open/use/close cycles.
        client, frames = session(emulator, monkeypatch)
        client.run()
        assert frames and client.sock is None and client.http is None
        deadline = time.monotonic()+1
        while (emulator.sockets or emulator.handlers) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert emulator.sockets == 0 and not emulator.handlers


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


def test_key_uses_one_shot_token_before_websocket(emulator, monkeypatch):
    client, frames = session(emulator, monkeypatch)
    client.run()
    assert emulator.requests[0][0] == "/proxy/access/oneshot_token"
    assert frames
    websocket = next(request for request in emulator.requests if "/websocket" in request[0])
    assert "X-Api-Key" not in websocket[1]
    assert "synthetic-key" not in websocket[0]


class VirtualEvent:
    def __init__(self, clock):
        self.clock, self.value = clock, False

    def set(self):
        self.value = True

    def clear(self):
        self.value = False

    def is_set(self):
        return self.value

    def wait(self, duration):
        if not self.value:
            self.clock[0] += duration
        return self.value


def virtual_session(monkeypatch, publish):
    clock, starts, fetches = [0.0], [], []
    monkeypatch.setattr(camera.time, "monotonic", lambda: clock[0])
    client = camera.Session("http://synthetic.invalid", "", None, creality_adapter,
                            lambda: True, publish)
    client.stop = VirtualEvent(clock)
    if hasattr(client, "wake"):
        client.wake = VirtualEvent(clock)
    monkeypatch.setattr(client, "_connect", lambda: (None, None))
    monkeypatch.setattr(client, "_start", lambda *args: starts.append(clock[0]))
    def jpeg(*args, **kwargs):
        fetches.append(clock[0])
        return {"image": "data:image/jpeg;base64,/9j/2Q==", "received_at": 1}
    monkeypatch.setattr(client, "_jpeg", jpeg)
    return client, clock, starts, fetches


def test_snapshot_cadence_is_independent_of_monitor_keepalive(monkeypatch):
    frames = []
    def publish(frame):
        frames.append(frame)
        if hasattr(client, "acknowledge"):
            client.acknowledge(frame["sequence"])
        if len(frames) == 6:
            client.cancel()
    client, clock, starts, fetches = virtual_session(monkeypatch, publish)
    client.run()
    assert fetches == pytest.approx([0, .25, .5, .75, 1, 1.25])
    assert starts == [0]


def test_unacknowledged_frame_stops_gets_but_not_monitor_keepalive(monkeypatch):
    frames = []
    client, clock, starts, fetches = virtual_session(monkeypatch, frames.append)
    def start(*args):
        starts.append(clock[0])
        if len(starts) == 3:
            client.cancel()
    monkeypatch.setattr(client, "_start", start)
    try:
        client.run()
    except camera.CameraError as exc:
        assert str(exc) == "session_expired"
    assert starts == [0, 2, 4]
    assert fetches == [0] and len(frames) == 1


@pytest.mark.parametrize("token", [None, {}, [], "", "bad\r\ntoken", "x" * 513])
def test_invalid_one_shot_token_has_no_websocket_or_key_fallback(emulator, monkeypatch, token):
    emulator.token_result = {"result": token}
    client, frames = session(emulator, monkeypatch)
    with pytest.raises(camera.CameraError, match="invalid_token"):
        client.run()
    assert len(emulator.requests) == 1 and not emulator.commands and not frames
    assert client.sock is None and client.http is None


@pytest.mark.parametrize("status", [301, 302, 401, 403, 404])
def test_token_denial_or_redirect_never_opens_websocket(emulator, monkeypatch, status):
    emulator.token_status = status
    client, frames = session(emulator, monkeypatch)
    with pytest.raises(camera.CameraError):
        client.run()
    assert len(emulator.requests) == 1 and not frames


@pytest.mark.parametrize("body", [b"not-json", b"x" * (camera.MAX_TOKEN_RESPONSE + 1)])
def test_token_response_is_bounded_and_must_be_json(emulator, monkeypatch, body):
    emulator.token_body = body
    client, frames = session(emulator, monkeypatch)
    with pytest.raises(camera.CameraError):
        client.run()
    assert len(emulator.requests) == 1 and not frames


def test_keyless_camera_never_requests_token_or_sends_auth(emulator):
    frames = []
    def publish(frame):
        frames.append(frame)
        client.cancel()
    client = camera.Session(emulator.base, "", None, creality_adapter, lambda: True, publish)
    client.run()
    assert frames
    assert emulator.requests[0][0] == "/proxy/websocket"
    assert all("X-Api-Key" not in headers and "token=" not in path for path, headers in emulator.requests)


@pytest.mark.parametrize("part", ["headers", "body"])
def test_dribbled_http_response_obeys_absolute_monitor_deadline(emulator, part):
    emulator.drip = part
    client = camera.Session(emulator.base, "", None, creality_adapter, lambda: True, lambda _: None)
    started = time.monotonic()
    try:
        with pytest.raises(camera.CameraError, match="frame_timeout"):
            client._jpeg(started + 0.12)
        assert time.monotonic() - started < 0.6
        assert client.http is None and client.http_socket is None
    finally:
        client.cancel()


@pytest.mark.parametrize("failure", ["404", "timeout"])
def test_missing_frame_grace_uses_elapsed_time_and_preserves_keepalive(monkeypatch, failure):
    client, clock, starts, fetches = virtual_session(monkeypatch, lambda _: None)
    def missing(deadline):
        fetches.append(clock[0])
        if failure == "timeout":
            clock[0] = deadline
            raise camera.CameraError("frame_timeout")
        return None
    monkeypatch.setattr(client, "_jpeg", missing)
    with pytest.raises(camera.CameraError, match="frame_unavailable"):
        client.run()
    assert clock[0] == pytest.approx(6)
    assert starts[:3] == [0, 2, 4]
    assert len(fetches) > 3 if failure == "404" else len(fetches) == 3


def test_only_exact_pending_sequence_acknowledges_frame():
    client = camera.Session("http://synthetic.invalid", "", None, creality_adapter,
                            lambda: True, lambda _: None)
    client.pending_sequence = 7
    for wrong in (None, True, "7", 6, 8):
        assert client.acknowledge(wrong) is False
        assert client.pending_sequence == 7 and not client.wake.is_set()
    assert client.acknowledge(7) is True
    assert client.pending_sequence is None and client.wake.is_set()
    assert client.acknowledge(7) is False


@pytest.mark.parametrize("kind", ["token", "frame"])
def test_early_eof_rejects_partial_content_length_even_with_valid_payload(emulator, monkeypatch, kind):
    setattr(emulator, kind + "_length_extra", 10)
    client, frames = session(emulator, monkeypatch)
    with pytest.raises(camera.CameraError, match="incomplete_response"):
        client.run()
    assert not frames and client.http is None
    if kind == "token":
        assert not emulator.commands


def test_chunked_jpeg_response_uses_the_standard_http_parser(emulator, monkeypatch):
    emulator.chunked = True
    client, frames = session(emulator, monkeypatch)
    client.run()
    assert frames and base64.b64decode(frames[0]["image"].split(",", 1)[1]) == JPEG


def test_cancel_during_token_body_never_opens_websocket(emulator, monkeypatch):
    emulator.stall_token = True
    client, frames = session(emulator, monkeypatch)
    failures = []
    def run():
        try:
            client.run()
        except camera.CameraError:
            failures.append(True)
    worker = threading.Thread(target=run)
    try:
        worker.start()
        assert emulator.token_started.wait(1)
        cancelled_at = time.monotonic()
        client.cancel()
        worker.join(camera.TIMEOUT + 0.3)
        if worker.is_alive():
            import sys
            import traceback
            pytest.fail("Cancelled token worker still running:\n" + "".join(
                traceback.format_stack(sys._current_frames()[worker.ident])))
        assert failures
        assert time.monotonic() - cancelled_at < camera.TIMEOUT + 0.3
        assert not frames and not emulator.commands and len(emulator.requests) == 1
        assert client.http is None and client.sock is None
    finally:
        client.cancel()
        emulator.release_token.set()
        worker.join(2)


def test_twenty_token_cancellations_finish_within_transport_and_cleanup_bound(emulator, monkeypatch):
    emulator.stall_token = True
    for _ in range(20):
        emulator.token_started.clear()
        emulator.release_token.clear()
        client, frames = session(emulator, monkeypatch)
        failures = []
        def run():
            try:
                client.run()
            except camera.CameraError:
                failures.append(True)
        worker = threading.Thread(target=run)
        try:
            before = len(emulator.requests)
            worker.start()
            assert emulator.token_started.wait(1)
            started = time.monotonic()
            client.cancel()
            worker.join(camera.TIMEOUT + 0.3)
            assert not worker.is_alive()
            assert time.monotonic() - started < camera.TIMEOUT + 0.3
            assert failures and not frames and not emulator.commands
            assert len(emulator.requests) == before + 1
            assert client.http is None and client.http_socket is None and client.sock is None
        finally:
            emulator.release_token.set()
            client.cancel()
            worker.join(2)
        deadline = time.monotonic() + 1
        while emulator.handlers and time.monotonic() < deadline:
            time.sleep(.005)
        assert not emulator.handlers and emulator.sockets == 0


def test_expired_http_connect_sends_no_api_key_headers(emulator, monkeypatch):
    import types
    clock = [time.monotonic()]
    actual_time = camera.time
    monkeypatch.setattr(camera, "time", types.SimpleNamespace(
        monotonic=lambda: clock[0], monotonic_ns=actual_time.monotonic_ns, time=actual_time.time))
    original = camera.http.client.HTTPConnection.connect
    def connect(connection):
        original(connection)
        clock[0] += 2
    client, frames = session(emulator, monkeypatch)
    with mock.patch.object(camera.http.client.HTTPConnection, "connect", connect):
        with pytest.raises(camera.CameraError, match="frame_timeout"):
            client._one_shot_token()
    assert not frames and not emulator.requests and client.http is None
