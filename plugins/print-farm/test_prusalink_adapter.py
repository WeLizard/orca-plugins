"""Loopback-only protocol tests for PrusaLink; no printer or LAN traffic."""

import http.server
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import prusalink_adapter as adapter


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    responses = {}
    requests = []
    delay_path = None
    api_key = "secret-test-key"

    def log_message(self, *_args):
        pass

    def _reply(self, status, payload=b"", headers=None):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload).encode("utf-8")
            headers = {**(headers or {}), "Content-Type": "application/json"}
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _handle(self):
        body = b""
        length = int(self.headers.get("Content-Length", "0"))
        while len(body) < length:
            block = self.rfile.read(min(65536, length - len(body)))
            if not block:
                break
            body += block
        type(self).requests.append((self.command, self.path, dict(self.headers), body))
        if self.path == type(self).delay_path:
            time.sleep(0.25)
            self._reply(200, {"api": "1.0"})
            return
        if type(self).api_key and self.path != "/api/version":
            if self.headers.get("X-Api-Key") != type(self).api_key:
                self._reply(401, {"title": "Unauthorized", "text": "no"})
                return
        status, payload, headers = type(self).responses.get(
            (self.command, self.path), (404, b"missing", {})
        )
        self._reply(status, payload, headers)

    do_GET = _handle
    do_PUT = _handle


def _fixture_server():
    _Handler.requests = []
    _Handler.delay_path = None
    _Handler.api_key = "secret-test-key"
    _Handler.responses = {
        ("GET", "/api/version"): (200, {
            "api": "1.0", "version": "0.7", "capabilities": {
                "upload-by-put": True, "x-api-key": True,
            },
        }, {}),
        ("GET", "/api/v1/status"): (200, {
            "printer": {
                "state": "IDLE", "temp_nozzle": 21.5,
                "target_nozzle": 0, "temp_bed": 22, "target_bed": 0,
            },
            "job": {"progress": 0},
            "transfer": {"type": "NO_TRANSFER"},
        }, {}),
        ("GET", "/api/v1/job"): (204, b"", {}),
        ("GET", "/api/v1/storage"): (200, {"storage_list": [
            {"path": "/local", "type": "LOCAL", "available": True,
             "read_only": False},
        ]}, {}),
        ("PUT", "/api/v1/files/local/part.gcode"): (201, b"", {}),
    }
    server = _Server(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % server.server_port
    return server, thread, base


class PrusaLinkAdapterTests(unittest.TestCase):
    def setUp(self):
        self.server, self.thread, self.base = _fixture_server()
        self.temp = tempfile.TemporaryDirectory()
        self.file_path = Path(self.temp.name) / "part.gcode"
        self.file_path.write_bytes(b"G28\nG1 X1\n")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        self.temp.cleanup()

    def test_valid_poll_and_upload_use_advertised_api_key_and_never_start_print(self):
        status = adapter.poll(self.base, None, "secret-test-key")
        self.assertTrue(status["online"])
        self.assertTrue(status["upload_ready"])
        self.assertFalse(status["can_control"])
        self.assertEqual(status["nozzle"], 21.5)
        result = adapter.upload(
            self.base, None, "secret-test-key", self.file_path, "part.gcode"
        )
        self.assertEqual(result, {
            "ok": True, "evidence_ref": "PUT /api/v1/files/local/part.gcode",
        })
        put = next(item for item in _Handler.requests if item[0] == "PUT")
        self.assertEqual(put[2]["X-Api-Key"], "secret-test-key")
        self.assertEqual(put[2]["Print-After-Upload"], "?0")
        self.assertEqual(put[2]["Overwrite"], "?0")
        self.assertEqual(put[3], b"G28\nG1 X1\n")
        self.assertFalse(any(item[0] == "POST" for item in _Handler.requests))

    def test_partial_telemetry_stays_online_but_unknown_values_are_none(self):
        _Handler.responses[("GET", "/api/v1/status")] = (
            200, {"printer": {"state": "READY"}}, {}
        )
        status = adapter.poll(self.base, None, "secret-test-key")
        self.assertTrue(status["online"])
        self.assertIsNone(status["nozzle"])
        self.assertIsNone(status["progress"])
        self.assertEqual(status["filename"], "")

    def test_api_key_fails_closed_when_server_does_not_advertise_it(self):
        _Handler.responses[("GET", "/api/version")] = (
            200, {"api": "1.0", "capabilities": {"upload-by-put": True}}, {}
        )
        result = adapter.upload(
            self.base, None, "secret-test-key", self.file_path, "part.gcode"
        )
        self.assertEqual(result, {"ok": False, "error_code": "api_key_unsupported"})
        self.assertFalse(any(item[0] == "PUT" for item in _Handler.requests))

    def test_unauthorized_poll_and_upload_fail_closed(self):
        _Handler.api_key = "different"
        status = adapter.poll(self.base, None, "secret-test-key")
        self.assertFalse(status["online"])
        self.assertEqual(status["error_code"], "auth_required")
        result = adapter.upload(
            self.base, None, "secret-test-key", self.file_path, "part.gcode"
        )
        self.assertEqual(result, {"ok": False, "error_code": "unauthorized"})

    def test_busy_printer_blocks_upload_before_storage_or_put(self):
        body = dict(_Handler.responses[("GET", "/api/v1/status")][1])
        body["printer"] = {"state": "PRINTING"}
        _Handler.responses[("GET", "/api/v1/status")] = (200, body, {})
        result = adapter.upload(
            self.base, None, "secret-test-key", self.file_path, "part.gcode"
        )
        self.assertEqual(result, {"ok": False, "error_code": "printer_busy"})
        self.assertFalse(any(item[0] == "PUT" for item in _Handler.requests))
        self.assertFalse(any(item[1] == "/api/v1/storage" for item in _Handler.requests))

    def test_malformed_json_does_not_claim_online_or_upload(self):
        _Handler.responses[("GET", "/api/v1/status")] = (200, b"{broken", {})
        status = adapter.poll(self.base, None, "secret-test-key")
        self.assertFalse(status["online"])
        self.assertEqual(status["error_code"], "malformed_response")
        result = adapter.upload(
            self.base, None, "secret-test-key", self.file_path, "part.gcode"
        )
        self.assertEqual(result, {"ok": False, "error_code": "malformed_response"})

    def test_timeout_after_put_is_reported_as_unknown_and_not_retried(self):
        _Handler.delay_path = "/api/v1/files/local/part.gcode"
        with mock.patch.object(adapter, "_TIMEOUT", 0.05):
            result = adapter.upload(
                self.base, None, "secret-test-key", self.file_path, "part.gcode"
            )
        self.assertEqual(result, {"ok": False, "error_code": "outcome_unknown"})
        puts = [item for item in _Handler.requests if item[0] == "PUT"]
        self.assertEqual(len(puts), 1)

    def test_unknown_capability_and_unsafe_filename_are_rejected(self):
        _Handler.responses[("GET", "/api/version")] = (
            200, {"api": "1.0", "capabilities": {}}, {}
        )
        result = adapter.upload(
            self.base, None, "", self.file_path, "../part.gcode"
        )
        self.assertEqual(result, {"ok": False, "error_code": "invalid_filename"})
        result = adapter.upload(
            self.base, None, "", self.file_path, "part.gcode"
        )
        self.assertEqual(result, {"ok": False, "error_code": "upload_capability_missing"})
        self.assertFalse(any(item[0] == "PUT" for item in _Handler.requests))


if __name__ == "__main__":
    unittest.main()
