"""Loopback-only tests for the Creality stock (Ender 5 Max) adapter.

The WebSocket/HTTP emulator binds 127.0.0.1 on an ephemeral port inside the
test process; no printer or LAN traffic. Fixtures are synthetic (see
creality_fixtures/MANIFEST.json).
"""

import base64
import hashlib
import http.server
import json
import socket
import socketserver
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import creality_adapter as adapter

FIXTURES = Path(__file__).resolve().parent / "creality_fixtures" / "synthetic"
SECRET = "s3cret-token-xyz"
_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def fixture(name):
    data = json.loads((FIXTURES / (name + ".json")).read_text(encoding="utf-8"))
    data.pop("_provenance")
    return data


def frame(opcode, payload, fin=True, mask=False, rsv=0):
    head = bytes([(0x80 if fin else 0) | rsv | opcode])
    n = len(payload)
    flag = 0x80 if mask else 0
    if n < 126:
        head += bytes([flag | n])
    elif n < 65536:
        head += bytes([flag | 126]) + struct.pack("!H", n)
    else:
        head += bytes([flag | 127]) + struct.pack("!Q", n)
    return head + (b"\0\0\0\0" if mask else b"") + payload


class _WsHandler(socketserver.BaseRequestHandler):
    def _recv_exact(self, n):
        data = b""
        while len(data) < n:
            chunk = self.request.recv(n - len(data))
            if not chunk:
                raise ConnectionError
            data += chunk
        return data

    def handle(self):
        cfg = self.server.cfg
        self.request.settimeout(5)
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = self.request.recv(1024)
            if not chunk:
                return
            raw += chunk
        headers = {}
        for line in raw.decode("latin-1").split("\r\n")[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        cfg["handshakes"].append(headers)
        if cfg.get("http_status"):
            self.request.sendall(("HTTP/1.1 %d X\r\nContent-Length: 0\r\n\r\n" % cfg["http_status"]).encode())
            return
        key = headers["sec-websocket-key"].encode()
        accept = base64.b64encode(hashlib.sha1(key + _GUID).digest()).decode()
        if cfg.get("bad_accept"):
            accept = "AAAA"
        self.request.sendall((
            "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Accept: %s\r\n\r\n" % accept).encode())
        if cfg.get("drop_after_handshake"):
            return
        b0, b1 = self._recv_exact(2)
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        mask = self._recv_exact(4)
        body = bytes(c ^ mask[i % 4] for i, c in enumerate(self._recv_exact(length)))
        cfg["received"].append({"opcode": b0 & 0x0F, "masked": bool(b1 & 0x80), "payload": body})
        for out in cfg["reply"](body):
            self.request.sendall(out)
        if cfg.get("hold"):
            threading.Event().wait(cfg["hold"])


class _WsServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _UploadHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_a):
        pass

    def do_POST(self):
        cfg = self.server.cfg
        length = int(self.headers.get("Content-Length", "0"))
        body = b""
        while len(body) < length:
            chunk = self.rfile.read(min(4096, length - len(body)))
            if not chunk:
                break
            body += chunk
        cfg["posts"].append((self.path, dict(self.headers), body))
        if cfg.get("drop"):
            self.close_connection = True
            return
        if cfg.get("stall"):
            threading.Event().wait(cfg["stall"])
            self.close_connection = True
            return
        self.send_response(cfg.get("code", 200))
        self.send_header("Content-Length", "0")
        self.end_headers()


class _HttpServer(http.server.ThreadingHTTPServer):
    daemon_threads = True


class CrealityAdapterTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {"handshakes": [], "received": [], "reply": self.json_reply(fixture("idle"))}
        self.ws = _WsServer(("127.0.0.1", 0), _WsHandler)
        self.ws.cfg = self.cfg
        threading.Thread(target=self.ws.serve_forever, daemon=True).start()
        self.base = "ws://127.0.0.1:%d" % self.ws.server_address[1]
        self.up_cfg = {"posts": []}
        self.http = _HttpServer(("127.0.0.1", 0), _UploadHandler)
        self.http.cfg = self.up_cfg
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.upload_url = "http://127.0.0.1:%d/upload" % self.http.server_address[1]
        root = Path(__file__).resolve().parent.parent / ".worktmp"
        root.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=str(root))
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self._stop)

    def _stop(self):
        for server in (self.ws, self.http):
            server.shutdown()
            server.server_close()

    @staticmethod
    def json_reply(obj):
        return lambda _body: [frame(0x1, json.dumps(obj).encode())]

    def source(self, data=b"G28\n"):
        path = Path(self.tmp.name) / "part.gcode"
        path.write_bytes(data)
        return path

    def poll(self):
        return adapter.poll(self.base, None, SECRET)

    # --- status ---------------------------------------------------------

    def test_idle_empty_filename_means_idle_and_fails_closed_on_writes(self):
        result = self.poll()
        self.assertTrue(result["online"])
        self.assertEqual(result["state"], "idle")
        self.assertIsNone(result["progress"])
        self.assertIsNone(result["nozzle"])
        self.assertIsNone(result["bed"])
        self.assertFalse(result["upload_ready"])
        self.assertFalse(result["can_control"])
        self.assertEqual(result["error_code"], "writable_target_unverified")

    def test_request_is_masked_read_only_get_and_secret_not_sent(self):
        self.poll()
        self.assertEqual(len(self.cfg["received"]), 1)
        sent = self.cfg["received"][0]
        self.assertTrue(sent["masked"])
        self.assertEqual(json.loads(sent["payload"]), {"method": "get", "params": {"ReqPrinterPara": 1}})
        self.assertNotIn(SECRET, json.dumps(self.cfg["handshakes"]))

    def test_printing_status_maps_fields(self):
        self.cfg["reply"] = self.json_reply(fixture("printing"))
        result = self.poll()
        self.assertEqual(result["state"], "printing")
        self.assertEqual(result["filename"], "part.gcode")
        self.assertEqual(result["progress"], 42)
        self.assertEqual((result["layer"], result["total_layers"]), (10, 50))
        self.assertEqual((result["elapsed_seconds"], result["remaining_seconds"]), (600, 900))
        self.assertIsNone(result["nozzle"])  # temperatures are not in the confirmed schema

    def test_job_states_busy_and_error(self):
        for name, expected in (("paused", "paused"), ("complete", "complete"),
                               ("failed", "failed"), ("aborted", "aborted"),
                               ("unmapped_state", "unknown")):
            self.cfg["reply"] = self.json_reply(fixture(name))
            result = self.poll()
            self.assertEqual(result["state"], expected, name)
            self.assertEqual(result["job_state"], expected, name)
            self.assertTrue(result["online"])

    def test_partial_payloads_are_unknown_not_idle_or_zero(self):
        self.cfg["reply"] = self.json_reply(fixture("no_filename_key"))
        result = self.poll()
        self.assertEqual(result["state"], "unknown")
        self.assertIsNone(result["progress"])
        self.cfg["reply"] = self.json_reply(fixture("progress_out_of_range"))
        self.assertIsNone(self.poll()["progress"])
        self.cfg["reply"] = self.json_reply({"state": 1, "printFileName": "a.gcode"})
        partial = self.poll()
        self.assertIsNone(partial["progress"])
        self.assertIsNone(partial["layer"])

    def test_missing_state_or_non_object_is_unrecognized(self):
        self.cfg["reply"] = self.json_reply(fixture("no_state_key"))
        result = self.poll()
        self.assertFalse(result["online"])
        self.assertEqual(result["error_code"], "unrecognized_response")
        self.cfg["reply"] = lambda _b: [frame(0x1, b"[1,2]")]
        self.assertEqual(self.poll()["error_code"], "unrecognized_response")

    def test_malformed_json_and_binary_frames(self):
        self.cfg["reply"] = lambda _b: [frame(0x1, b'{"state":')]
        self.assertEqual(self.poll()["error_code"], "malformed_response")
        self.cfg["reply"] = lambda _b: [frame(0x2, b"{}")]
        self.assertEqual(self.poll()["error_code"], "malformed_frame")

    def test_oversized_frame_rejected_before_payload_is_read(self):
        header = bytes([0x81, 127]) + struct.pack("!Q", 50 * 1024 * 1024)
        self.cfg["reply"] = lambda _b: [header]
        self.cfg["hold"] = 1
        self.assertEqual(self.poll()["error_code"], "frame_too_large")

    def test_masked_server_frame_and_reserved_bits_rejected(self):
        self.cfg["reply"] = lambda _b: [frame(0x1, b"{}", mask=True)]
        self.assertEqual(self.poll()["error_code"], "malformed_frame")
        self.cfg["reply"] = lambda _b: [frame(0x1, b"{}", rsv=0x40)]
        self.assertEqual(self.poll()["error_code"], "malformed_frame")

    def test_fragmented_message_and_ping_are_handled(self):
        body = json.dumps(fixture("printing")).encode()
        self.cfg["reply"] = lambda _b: [
            frame(0x9, b"hi"), frame(0x1, body[:10], fin=False), frame(0x0, body[10:])]
        result = self.poll()
        self.assertEqual(result["state"], "printing")

    def test_fragment_total_over_limit_rejected(self):
        chunk = b"x" * (adapter.MAX_FRAME_BYTES - 1)
        self.cfg["reply"] = lambda _b: [frame(0x1, chunk, fin=False), frame(0x0, chunk)]
        self.assertEqual(self.poll()["error_code"], "frame_too_large")

    def test_close_frame_and_dropped_connection(self):
        self.cfg["reply"] = lambda _b: [frame(0x8, b"")]
        self.assertEqual(self.poll()["error_code"], "connection_closed")
        self.cfg["drop_after_handshake"] = True
        result = self.poll()
        self.assertFalse(result["online"])
        self.assertEqual(result["state"], "offline")
        self.assertIn(result["error_code"], ("connection_closed", "connection_failed"))

    def test_refused_connection_is_offline(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        result = adapter.poll("ws://127.0.0.1:%d" % port, None, SECRET)
        self.assertFalse(result["online"])
        self.assertEqual(result["error_code"], "connection_failed")

    def test_silent_printer_times_out(self):
        self.cfg["reply"] = lambda _b: []
        self.cfg["hold"] = 2
        with mock.patch.object(adapter, "STATUS_TIMEOUT", 0.3):
            result = self.poll()
        self.assertEqual(result["error_code"], "timeout")
        self.assertFalse(result["online"])

    def test_handshake_failures(self):
        self.cfg["bad_accept"] = True
        self.assertEqual(self.poll()["error_code"], "handshake_failed")
        self.cfg["bad_accept"] = False
        for code, expected in ((401, "authentication_failed"), (403, "authentication_failed"),
                               (302, "redirect_rejected"), (500, "handshake_failed")):
            self.cfg["http_status"] = code
            self.assertEqual(self.poll()["error_code"], expected)

    def test_invalid_bases_rejected_without_connecting(self):
        for base in ("wss://127.0.0.1:9999", "ws://user:%s@127.0.0.1:9999" % SECRET,
                     "http://127.0.0.1:9999", "ws://127.0.0.1", "ws://127.0.0.1:9999/x", 5):
            result = adapter.poll(base, None, SECRET)
            self.assertFalse(result["online"])
            self.assertIn(result["error_code"], ("invalid_request", "connection_failed"))
            self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(self.cfg["handshakes"], [])

    def test_ws_base_follows_source_rule(self):
        self.assertEqual(adapter.ws_base("http://192.0.2.10/upload", 9999), "ws://192.0.2.10:9999")
        with self.assertRaises(ValueError):
            adapter.ws_base("http://192.0.2.10/upload", 0)

    # --- upload ---------------------------------------------------------

    def upload(self, name="part.gcode", **kw):
        return adapter.upload(self.upload_url, None, SECRET, self.source(), name,
                              status_base=kw.get("status_base", self.base))

    def test_upload_posts_multipart_with_part_content_type_and_does_not_start(self):
        result = self.upload()
        self.assertEqual(result, {"ok": True, "evidence_ref": "creality-http-200"})
        path, headers, body = self.up_cfg["posts"][0]
        self.assertEqual(path, "/upload/part.gcode")
        self.assertTrue(headers["Content-Type"].startswith("multipart/form-data; boundary="))
        self.assertIn(b'name="file"; filename="part.gcode"', body)
        self.assertIn(b"Content-Type: application/octet-stream\r\n\r\nG28\n\r\n--", body)
        self.assertNotIn(SECRET.encode(), body)
        self.assertNotIn(SECRET, json.dumps(headers))
        # Only the single status read touched the WebSocket; no "set" command.
        self.assertEqual([json.loads(r["payload"])["method"] for r in self.cfg["received"]], ["get"])

    def test_upload_refused_when_busy_or_status_unavailable(self):
        self.cfg["reply"] = self.json_reply(fixture("printing"))
        self.assertEqual(self.upload(), {"ok": False, "error_code": "printer_busy"})
        self.cfg["reply"] = self.json_reply(fixture("no_filename_key"))
        self.assertEqual(self.upload()["error_code"], "printer_busy")  # unknown is not idle
        self.cfg["reply"] = lambda _b: [frame(0x1, b"{")]
        self.assertEqual(self.upload()["error_code"], "malformed_response")
        self.assertEqual(self.up_cfg["posts"], [])

    def test_upload_http_rejections_are_definitive(self):
        for code, expected in ((500, "upload_rejected"), (403, "authentication_failed"),
                               (302, "redirect_rejected")):
            self.up_cfg["code"] = code
            self.assertEqual(self.upload(), {"ok": False, "error_code": expected})

    def test_connection_drop_after_dispatch_is_unknown_outcome(self):
        self.up_cfg["drop"] = True
        self.assertEqual(self.upload(), {"ok": False, "error_code": "unknown_outcome"})
        self.assertEqual(len(self.up_cfg["posts"]), 1)  # never retried

    def test_timeout_after_send_is_unknown_outcome_and_not_retried(self):
        self.up_cfg["stall"] = 1.5
        with mock.patch.object(adapter, "UPLOAD_TIMEOUT", 0.3):
            result = self.upload()
        self.assertEqual(result, {"ok": False, "error_code": "unknown_outcome"})
        self.assertEqual(len(self.up_cfg["posts"]), 1)
        self.assertEqual([json.loads(r["payload"])["method"] for r in self.cfg["received"]], ["get"])

    def test_upload_to_closed_port_is_connection_failed_not_unknown(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        result = adapter.upload("http://127.0.0.1:%d/upload" % port, None, SECRET,
                                self.source(), "part.gcode", status_base=self.base)
        self.assertEqual(result, {"ok": False, "error_code": "connection_failed"})

    def test_unsafe_names_urls_and_files_rejected(self):
        for name in ("../evil.gcode", 'a"b.gcode', "a\r\nb.gcode", "a/b.gcode", "part.txt", "", ".gcode", "é.gcode"):
            self.assertEqual(self.upload(name)["error_code"], "invalid_filename", name)
        src = self.source()
        for url in ("http://user:%s@127.0.0.1/upload" % SECRET, "ftp://127.0.0.1/", "http://127.0.0.1/u?x=1"):
            result = adapter.upload(url, None, SECRET, src, "part.gcode", status_base=self.base)
            self.assertEqual(result["error_code"], "invalid_request")
            self.assertNotIn(SECRET, json.dumps(result))
        empty = Path(self.tmp.name) / "empty.gcode"
        empty.write_bytes(b"")
        self.assertEqual(adapter.upload(self.upload_url, None, SECRET, empty, "part.gcode",
                                        status_base=self.base)["error_code"], "invalid_request")
        self.assertEqual(self.up_cfg["posts"], [])

    def test_secret_never_appears_in_any_result(self):
        outcomes = [self.poll(), self.upload()]
        self.cfg["drop_after_handshake"] = True
        outcomes.append(self.poll())
        self.up_cfg["drop"] = True
        self.cfg["drop_after_handshake"] = False
        outcomes.append(self.upload())
        self.assertNotIn(SECRET, json.dumps(outcomes))


if __name__ == "__main__":
    unittest.main()
