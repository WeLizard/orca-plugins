"""Loopback-only tests for the FlashForge adapter (5M / 5M Pro / AD5X).

Two isolated emulators bind 127.0.0.1 on ephemeral ports inside the test
process: an HTTP API emulator (``/detail``, ``/printGcode``, ``/uploadGcode``)
and a legacy TCP text emulator (port 8899 protocol). No printer or LAN
traffic. Fixtures under flashforge_fixtures/synthetic are hand-written from
public docs (see flashforge_fixtures/MANIFEST.json); nothing is a capture.
"""

import copy
import http.server
import json
import socket
import socketserver
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import flashforge_adapter as adapter

FIXTURES = Path(__file__).resolve().parent / "flashforge_fixtures" / "synthetic"
SERIAL = "SNTEST-FAKE-0001"
CHECK = "fakecheck-7731"
WRITES_5M = {0x23: {"upload": True, "start": True}, 0x24: {"upload": True, "start": True}}


def fixture(name):
    data = json.loads((FIXTURES / (name + ".json")).read_text(encoding="utf-8"))
    assert data["_provenance"]["kind"] == "synthetic"
    return copy.deepcopy(data["response"])


def tcp_fixture(name):
    return (FIXTURES / "tcp" / (name + ".txt")).read_bytes()


# --------------------------------------------------------------------------
# HTTP API emulator
# --------------------------------------------------------------------------

class _ApiHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_a):
        pass

    def do_POST(self):
        cfg = self.server.cfg
        length = int(self.headers.get("Content-Length", "0"))
        body = b""
        while len(body) < length:
            chunk = self.rfile.read(min(65536, length - len(body)))
            if not chunk:
                break
            body += chunk
        cfg["requests"].append({"path": self.path, "headers": dict(self.headers), "body": body})
        route = self.path
        mode = cfg.get("mode", {}).get(route, {})
        if mode.get("stall"):
            threading.Event().wait(mode["stall"])
            self.close_connection = True
            return
        if mode.get("drop"):
            self.close_connection = True
            return
        status = mode.get("status", 200)
        payload = mode.get("raw")
        if payload is None:
            payload = json.dumps(self._reply(route, body, cfg)).encode()
        self.send_response(status)
        if mode.get("location"):
            self.send_header("Location", mode["location"])
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _reply(self, route, body, cfg):
        creds = None
        if route in ("/detail", "/printGcode"):
            try:
                parsed = json.loads(body)
                creds = (parsed.get("serialNumber"), parsed.get("checkCode"))
            except ValueError:
                return {"code": -1, "message": "Parameters is error"}
        else:
            creds = (self.headers.get("serialNumber"), self.headers.get("checkCode"))
        if creds[0] != cfg["serial"]:
            return {"code": 1, "message": "SN is different: " + str(creds[0])}
        if creds[1] != cfg["check"]:
            return {"code": 1, "message": "Access code is different: " + str(creds[1])}
        return cfg["replies"].get(route, {"code": 0, "message": "Success"})


class _ApiServer(http.server.ThreadingHTTPServer):
    daemon_threads = True


# --------------------------------------------------------------------------
# Legacy TCP emulator (text, \r\n, commands prefixed with ~)
# --------------------------------------------------------------------------

class _TcpHandler(socketserver.BaseRequestHandler):
    def handle(self):
        cfg = self.server.cfg
        self.request.settimeout(5)
        buffer = b""
        try:
            while True:
                while b"\r\n" not in buffer:
                    chunk = self.request.recv(1024)
                    if not chunk:
                        return
                    buffer += chunk
                line, _, buffer = buffer.partition(b"\r\n")
                text = line.decode("ascii", "replace")
                cfg["commands"].append(text)
                name = text.lstrip("~").split(" ")[0]
                if cfg.get("drop_on") == name:
                    return
                if cfg.get("stall_on") == name:
                    threading.Event().wait(cfg.get("stall", 2))
                    return
                if cfg.get("huge_on") == name:
                    self.request.sendall(b"x" * (64 * 1024))
                    continue
                reply = cfg["replies"].get(name)
                if reply is None:
                    reply = b"Error: Invalid command\r\n"
                self.request.sendall(reply)
        except OSError:
            return


class _TcpServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class _Base(unittest.TestCase):
    def setUp(self):
        self.api_cfg = {"requests": [], "mode": {}, "serial": SERIAL, "check": CHECK,
                        "replies": {"/detail": fixture("detail_5m_ready")}}
        self.api = _ApiServer(("127.0.0.1", 0), _ApiHandler)
        self.api.cfg = self.api_cfg
        threading.Thread(target=self.api.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.api.server_address[1]
        self.tcp_cfg = {"commands": [], "replies": self.healthy_tcp()}
        self.tcp = _TcpServer(("127.0.0.1", 0), _TcpHandler)
        self.tcp.cfg = self.tcp_cfg
        threading.Thread(target=self.tcp.serve_forever, daemon=True).start()
        self.tcp_base = "tcp://127.0.0.1:%d" % self.tcp.server_address[1]
        root = Path(__file__).resolve().parent.parent / ".worktmp"
        root.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=str(root))
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self._stop)

    def _stop(self):
        for server in (self.api, self.tcp):
            server.shutdown()
            server.server_close()

    @staticmethod
    def healthy_tcp(status="m119_ready", progress="m27_idle", info="m115_5m", temps="m105_new"):
        return {"M601": tcp_fixture("m601_ok"), "M602": tcp_fixture("m602_ok"),
                "M115": tcp_fixture(info), "M119": tcp_fixture(status),
                "M105": tcp_fixture(temps), "M27": tcp_fixture(progress)}

    def reply(self, name, route="/detail"):
        self.api_cfg["replies"][route] = fixture(name) if isinstance(name, str) else name

    def poll(self, **kw):
        return adapter.poll(self.base, None, kw.pop("secret", CHECK), serial_number=kw.pop("serial_number", SERIAL))

    def source(self, data=b"G28\nG1 X10\n"):
        path = Path(self.tmp.name) / "part.gcode"
        path.write_bytes(data)
        return path

    def posts(self, route):
        return [r for r in self.api_cfg["requests"] if r["path"] == route]

    def assert_no_secret(self, value):
        text = json.dumps(value, default=str)
        self.assertNotIn(CHECK, text)
        self.assertNotIn(SERIAL, text)


# --------------------------------------------------------------------------
# poll (HTTP)
# --------------------------------------------------------------------------

class PollTests(_Base):
    def test_healthy_idle_5m_identity_and_fail_closed_writes(self):
        result = self.poll()
        self.assertTrue(result["online"])
        self.assertEqual(result["state"], "idle")
        self.assertEqual(result["job_state"], "idle")
        self.assertEqual((result["model"], result["model_family"]), ("Adventurer 5M", "adventurer_5m"))
        self.assertTrue(result["identity_verified"])
        self.assertEqual(result["firmware"], "v3.2.7")
        self.assertEqual((result["nozzle"], result["target_nozzle"], result["bed"], result["target_bed"]), (25, 0, 24, 0))
        self.assertIsNone(result["progress"])
        self.assertEqual(result["filename"], "")
        self.assertFalse(result["upload_ready"])
        self.assertEqual(result["upload_reason"], "hardware_unverified")
        self.assertFalse(result["can_control"])
        self.assertFalse(result["can_start"])
        self.assertEqual(result["error_code"], "")
        self.assert_no_secret(result)

    def test_request_is_single_read_only_detail_post_without_extra_fields(self):
        self.poll()
        self.assertEqual(len(self.api_cfg["requests"]), 1)
        sent = self.api_cfg["requests"][0]
        self.assertEqual(sent["path"], "/detail")
        self.assertEqual(json.loads(sent["body"]), {"serialNumber": SERIAL, "checkCode": CHECK})
        self.assertEqual(sent["headers"]["Content-Type"], "application/json")

    def test_printing_maps_fields_and_progress_ratio(self):
        self.reply("detail_5m_printing")
        result = self.poll()
        self.assertEqual(result["state"], "printing")
        self.assertEqual(result["progress"], 45.0)
        self.assertEqual(result["filename"], "Benchy.gcode")
        self.assertEqual((result["nozzle"], result["target_nozzle"], result["bed"], result["target_bed"]), (209, 210, 58, 60))
        self.assertEqual((result["elapsed_seconds"], result["remaining_seconds"]), (1800, 3600.0))
        self.assertEqual((result["layer"], result["total_layers"]), (50, 100))
        self.assertFalse(result["upload_ready"])

    def test_pro_and_ad5x_identity_from_pid(self):
        self.reply("detail_5m_pro_printing")
        self.assertEqual(self.poll()["model"], "Adventurer 5M Pro")
        self.reply("detail_ad5x_ready")
        result = self.poll()
        self.assertEqual((result["model"], result["model_family"]), ("AD5X", "ad5x"))
        self.assertEqual(result["firmware"], "1.2.1-1.0.2")

    def test_state_table(self):
        cases = {"detail_5m_heating": ("printing", None), "detail_5m_paused": ("paused", None),
                 "detail_5m_error": ("error", None), "detail_5m_busy": ("busy", None),
                 "detail_5m_completed": ("complete", 100.0)}
        for name, (state, progress) in cases.items():
            self.reply(name)
            result = self.poll()
            self.assertTrue(result["online"], name)
            self.assertEqual(result["state"], state, name)
            if progress is not None:
                self.assertEqual(result["progress"], progress, name)
        self.reply("detail_5m_error")
        self.assertEqual(self.poll()["printer_error_code"], "E0048")

    def test_estimated_time_only_exposed_while_printing(self):
        for name in ("detail_5m_heating", "detail_5m_paused"):
            self.reply(name)
            self.assertIsNone(self.poll()["remaining_seconds"], name)

    def test_unmapped_status_is_unknown_not_idle(self):
        self.reply("detail_unmapped_status")
        result = self.poll()
        self.assertTrue(result["online"])
        self.assertEqual(result["state"], "unknown")
        self.assertFalse(result["upload_ready"])

    def test_empty_partial_and_missing_detail_are_unknown(self):
        for name, code in (("detail_empty", "unrecognized_response"), ("detail_missing", "unrecognized_response")):
            self.reply(name)
            result = self.poll()
            self.assertFalse(result["online"], name)
            self.assertEqual(result["state"], "unknown", name)
            self.assertEqual(result["error_code"], code, name)
        self.reply("detail_no_status")
        result = self.poll()
        self.assertEqual(result["state"], "unknown")
        self.assertIsNone(result["progress"])
        self.assertFalse(result["upload_ready"])

    def test_identity_rules(self):
        self.reply("detail_no_pid")
        result = self.poll()
        self.assertEqual((result["online"], result["state"], result["error_code"]), (False, "unknown", "identity_unverified"))
        for name in ("detail_creator5_pid", "detail_guider4_pid", "detail_5m_lookalike_name"):
            self.reply(name)
            result = self.poll()
            self.assertEqual(result["error_code"], "unsupported_model", name)
            self.assertFalse(result["online"], name)
            self.assertIsNone(result["model"], name)

    def test_pid_encodings(self):
        for pid, expected in (("0023", "Adventurer 5M"), (35, "Adventurer 5M"), ("0x24", None), (True, None),
                              ("23", "Adventurer 5M"), ("35", None), ("", None), (None, None), (3.5, None), (["0023"], None)):
            body = fixture("detail_5m_ready")
            body["detail"]["pid"] = pid
            self.reply(body)
            result = self.poll()
            self.assertEqual(result["model"], expected, repr(pid))
            self.assertEqual(result["online"], expected is not None, repr(pid))

    def test_bad_numbers_become_none(self):
        self.reply("detail_bad_numbers")
        result = self.poll()
        self.assertEqual(result["state"], "printing")
        self.assertIsNone(result["progress"])      # 45 is not a 0..1 ratio
        self.assertIsNone(result["nozzle"])         # string
        self.assertIsNone(result["bed"])            # bool
        self.assertIsNone(result["target_nozzle"])  # -108 sentinel
        self.assertIsNone(result["target_bed"])     # absurd

    def test_numeric_string_code_accepted_but_not_garbage(self):
        body = fixture("detail_5m_ready")
        body["code"] = "0"
        self.reply(body)
        self.assertTrue(self.poll()["online"])
        for bad in ("zero", None, True, [], "0.5"):
            body["code"] = bad
            self.reply(body)
            self.assertEqual(self.poll()["error_code"], "unrecognized_response", repr(bad))

    def test_auth_and_envelope_failures(self):
        self.assertEqual(self.poll(secret="wrong-code")["error_code"], "authentication_failed")
        self.assertEqual(self.poll(serial_number="SNOTHER")["error_code"], "authentication_failed")
        result = self.poll(secret="wrong-code")
        self.assertEqual((result["online"], result["state"]), (False, "unknown"))
        self.assertNotIn("wrong-code", json.dumps(result))
        for name, code in (("lan_mode_off", "lan_mode_disabled"), ("bad_parameters", "invalid_request"),
                           ("busy_code_2", "printer_busy"), ("file_missing_code_3", "printer_rejected")):
            self.reply(name)
            self.assertEqual(self.poll()["error_code"], code, name)

    def test_printer_message_echoing_secret_never_reaches_result(self):
        self.reply({"code": 1, "message": "Access code is different: " + CHECK})
        result = self.poll()
        self.assertEqual(result["error_code"], "authentication_failed")
        self.assert_no_secret(result)

    def test_refused_connection_is_offline(self):
        result = adapter.poll("http://127.0.0.1:%d" % _free_port(), None, CHECK, serial_number=SERIAL)
        self.assertFalse(result["online"])
        self.assertEqual((result["state"], result["error_code"]), ("offline", "connection_failed"))

    def test_dropped_connection_and_http_errors(self):
        self.api_cfg["mode"]["/detail"] = {"drop": True}
        result = self.poll()
        self.assertEqual((result["online"], result["error_code"]), (False, "connection_failed"))
        self.api_cfg["mode"]["/detail"] = {"status": 500, "raw": b"{}"}
        self.assertEqual(self.poll()["error_code"], "http_error")
        self.api_cfg["mode"]["/detail"] = {"status": 302, "location": "http://127.0.0.1:1/", "raw": b""}
        self.assertEqual(self.poll()["error_code"], "redirect_rejected")

    def test_malformed_and_oversized_replies(self):
        for raw, code in ((b'{"code":0,"detail":', "malformed_response"), (b"[1,2]", "unrecognized_response"),
                          (b"\xff\xfe", "malformed_response"), (b"null", "malformed_response")):
            self.api_cfg["mode"]["/detail"] = {"raw": raw}
            self.assertEqual(self.poll()["error_code"], code, raw)
        self.api_cfg["mode"]["/detail"] = {"raw": b" " * (adapter.MAX_BODY_BYTES + 10)}
        self.assertEqual(self.poll()["error_code"], "response_too_large")

    def test_silent_printer_times_out(self):
        self.api_cfg["mode"]["/detail"] = {"stall": 3}
        with mock.patch.object(adapter, "STATUS_TIMEOUT", 0.4):
            result = self.poll()
        self.assertEqual(result["error_code"], "timeout")
        self.assertFalse(result["online"])

    def test_invalid_bases_and_missing_credentials_send_nothing(self):
        for base in ("https://127.0.0.1:9", "http://user:%s@127.0.0.1:9" % CHECK, "ws://127.0.0.1:9",
                     "http://127.0.0.1:9/x", "http://127.0.0.1:9?a=1", "127.0.0.1", 7, None):
            result = adapter.poll(base, None, CHECK, serial_number=SERIAL)
            self.assertEqual(result["error_code"], "invalid_request", repr(base))
            self.assert_no_secret(result)
        for serial, secret in (("", CHECK), (SERIAL, ""), ("  ", CHECK)):
            self.assertEqual(adapter.poll(self.base, None, secret, serial_number=serial)["error_code"], "credentials_required")
        for serial, secret in ((SERIAL + "\r\nX: y", CHECK), (SERIAL, "a" * 100), ("серийный", CHECK)):
            self.assertEqual(adapter.poll(self.base, None, secret, serial_number=serial)["error_code"], "invalid_request")
        self.assertEqual(self.api_cfg["requests"], [])

    def test_sensitive_detail_fields_are_not_copied(self):
        self.reply("detail_5m_pro_printing")
        text = json.dumps(self.poll())
        for forbidden in ("FAKE-FLASH-CODE", "FAKE-POLAR-CODE", "00:00:5E", "192.0.2.10", "Test Pro"):
            self.assertNotIn(forbidden, text)

    def test_control_characters_in_strings_are_stripped(self):
        body = fixture("detail_5m_printing")
        body["detail"]["printFileName"] = "../../evil\r\nname.gcode"
        body["detail"]["firmwareVersion"] = "v1\x00\x1b[31m"
        self.reply(body)
        result = self.poll()
        self.assertNotIn("\n", result["filename"])
        self.assertFalse(any(ord(c) < 32 for c in result["filename"] + result["firmware"]))
        self.assertEqual(result["filename"], "evilname.gcode")


# --------------------------------------------------------------------------
# upload / start
# --------------------------------------------------------------------------

class WriteGateTests(_Base):
    def test_disabled_by_default_and_nothing_is_sent(self):
        path = self.source()
        self.assertEqual(adapter.upload(self.base, None, CHECK, path, "part.gcode", serial_number=SERIAL),
                         {"ok": False, "error_code": "unsupported_unverified", "outcome": "not_sent"})
        self.assertEqual(adapter.start(self.base, None, CHECK, "part.gcode", serial_number=SERIAL),
                         {"ok": False, "error_code": "unsupported_unverified", "outcome": "not_sent"})
        for func in (adapter.pause, adapter.resume, adapter.cancel, adapter.upload_tcp, adapter.start_tcp):
            self.assertEqual(func(self.base, None, CHECK)["error_code"], "unsupported_unverified")
        self.assertEqual(self.api_cfg["requests"], [])
        self.assertEqual(self.tcp_cfg["commands"], [])

    def test_poll_reports_readiness_only_with_hardware_evidence(self):
        with mock.patch.dict(adapter.HARDWARE_VERIFIED, WRITES_5M, clear=True):
            idle = self.poll()
            self.assertTrue(idle["upload_ready"] and idle["can_start"])
            self.assertEqual(idle["upload_reason"], "")
            self.assertFalse(idle["can_control"])
            self.reply("detail_5m_printing")
            busy = self.poll()
            self.assertFalse(busy["upload_ready"] or busy["can_start"])
            self.assertEqual(busy["upload_reason"], "state_printing")
            self.reply("detail_ad5x_ready")
            ad5x = self.poll()
            self.assertFalse(ad5x["upload_ready"])
            self.assertEqual(ad5x["upload_reason"], "hardware_unverified")

    def test_ad5x_start_never_enabled(self):
        self.reply("detail_ad5x_ready")
        with mock.patch.dict(adapter.HARDWARE_VERIFIED, {0x26: {"upload": True, "start": True}}, clear=True):
            self.assertFalse(self.poll()["can_start"])
            result = adapter.start(self.base, None, CHECK, "part.gcode", serial_number=SERIAL)
        self.assertEqual(result["error_code"], "unsupported_unverified")
        self.assertEqual(self.posts("/printGcode"), [])


@mock.patch.dict(adapter.HARDWARE_VERIFIED, WRITES_5M, clear=True)
class UploadTests(_Base):
    def upload(self, data=b"G28\nG1 X10\n", name="part.gcode", **kw):
        return adapter.upload(self.base, None, CHECK, self.source(data), name, serial_number=SERIAL, **kw)

    def test_wire_format_matches_documented_contract(self):
        data = b"G28\nG1 X10\n" * 50
        result = self.upload(data)
        self.assertEqual(result["outcome"], "completed_unverified")
        self.assertTrue(result["ok"])
        self.assertFalse(result["verified"])
        self.assertEqual(result["verification"], "unavailable")
        self.assertEqual(result["size"], len(data))
        self.assertEqual(len(self.posts("/detail")), 1)
        (sent,) = self.posts("/uploadGcode")
        headers = {k.lower(): v for k, v in sent["headers"].items()}
        self.assertEqual(headers["serialnumber"], SERIAL)
        self.assertEqual(headers["checkcode"], CHECK)
        self.assertEqual(headers["filesize"], str(len(data)))
        self.assertEqual(headers["printnow"], "false")
        self.assertEqual(headers["levelingbeforeprint"], "false")
        self.assertNotIn("expect", headers)
        for optional in ("flowcalibration", "usematlstation", "gcodetoolcnt", "materialmappings"):
            self.assertNotIn(optional, headers)
        self.assertTrue(headers["content-type"].startswith("multipart/form-data; boundary="))
        body = sent["body"]
        self.assertIn(b'name="gcodeFile"; filename="part.gcode"', body)
        self.assertIn(data, body)
        self.assertEqual(int(headers["content-length"]), len(body))
        self.assertEqual(self.posts("/printGcode"), [])

    def test_never_requests_print_now(self):
        self.upload()
        self.assertNotIn(b"printNow: true", json.dumps(self.api_cfg["requests"][-1]["headers"]).encode())

    def test_requires_idle_and_known_model(self):
        for name, code in (("detail_5m_printing", "printer_busy"), ("detail_5m_completed", "printer_busy"),
                           ("detail_5m_error", "printer_busy"), ("detail_unmapped_status", "printer_busy"),
                           ("detail_creator5_pid", "unsupported_model"), ("detail_no_pid", "identity_unverified")):
            self.reply(name)
            result = self.upload()
            self.assertEqual(result["error_code"], code, name)
            self.assertEqual(result["outcome"], "not_sent", name)
        self.assertEqual(self.posts("/uploadGcode"), [])

    def test_model_must_itself_be_verified(self):
        self.reply("detail_ad5x_ready")  # only 5M family is enabled in this class
        result = self.upload()
        self.assertEqual(result["error_code"], "unsupported_unverified")
        self.assertEqual(self.posts("/uploadGcode"), [])

    def test_ad5x_upload_uses_only_required_headers_when_verified(self):
        self.reply("detail_ad5x_ready")
        with mock.patch.dict(adapter.HARDWARE_VERIFIED, {0x26: {"upload": True}}, clear=True):
            self.assertTrue(self.upload()["ok"])
        (sent,) = self.posts("/uploadGcode")
        self.assertNotIn("materialmappings", {k.lower() for k in sent["headers"]})

    def test_printer_rejections_are_mapped(self):
        for reply, code in (({"code": 1, "message": "x"}, "authentication_failed"),
                            ({"code": -2, "message": "x"}, "lan_mode_disabled"),
                            ({"code": 2, "message": "x"}, "printer_busy"),
                            ({"code": 9, "message": "x"}, "upload_rejected")):
            self.api_cfg["replies"]["/uploadGcode"] = reply
            result = self.upload()
            self.assertEqual((result["ok"], result["error_code"], result["outcome"]), (False, code, "rejected"))

    def test_unreadable_ack_after_send_is_unknown_outcome_without_retry(self):
        for mode in ({"raw": b"not json"}, {"raw": b"{}"}, {"raw": b'{"code":"x"}'}, {"drop": True}, {"raw": b" " * 70000}):
            self.api_cfg["requests"].clear()
            self.api_cfg["mode"]["/uploadGcode"] = mode
            result = self.upload()
            self.assertFalse(result["ok"])
            self.assertEqual((result["error_code"], result["outcome"]), ("unknown_outcome", "unknown"), mode)
            self.assertEqual(len(self.posts("/uploadGcode")), 1, mode)

    def test_timeout_after_send_is_unknown_outcome_without_retry(self):
        self.api_cfg["mode"]["/uploadGcode"] = {"stall": 3}
        with mock.patch.object(adapter, "UPLOAD_TIMEOUT", 0.4):
            result = self.upload()
        self.assertEqual((result["error_code"], result["outcome"]), ("unknown_outcome", "unknown"))
        self.assertEqual(len(self.posts("/uploadGcode")), 1)

    def test_http_error_after_send_is_rejection_without_secrets(self):
        self.api_cfg["mode"]["/uploadGcode"] = {"status": 500, "raw": ("boom " + CHECK).encode()}
        result = self.upload()
        self.assertEqual((result["error_code"], result["outcome"]), ("upload_rejected", "rejected"))
        self.assert_no_secret(result)

    def test_invalid_inputs_send_nothing(self):
        for name in ("../x.gcode", "a b.gcode", "x.txt", "", ".gcode", "a" * 70 + ".gcode", "a\r\nb.gcode", "x.gcode/", 5):
            self.assertEqual(self.upload(name=name)["error_code"], "invalid_filename", repr(name))
        self.assertEqual(self.upload(data=b"")["error_code"], "invalid_request")
        missing = adapter.upload(self.base, None, CHECK, Path(self.tmp.name) / "nope.gcode", "a.gcode", serial_number=SERIAL)
        self.assertEqual(missing["error_code"], "invalid_request")
        self.assertEqual(adapter.upload(self.base, None, "", self.source(), "a.gcode", serial_number=SERIAL)["error_code"],
                         "credentials_required")
        self.assertEqual(self.api_cfg["requests"], [])

    def test_connection_failure_before_send(self):
        result = adapter.upload("http://127.0.0.1:%d" % _free_port(), None, CHECK, self.source(), "a.gcode", serial_number=SERIAL)
        self.assertEqual((result["error_code"], result["outcome"]), ("connection_failed", "not_sent"))

    def test_secrets_never_in_any_result(self):
        for mode in ({}, {"drop": True}, {"raw": ("{" + CHECK).encode()}):
            self.api_cfg["mode"]["/uploadGcode"] = mode
            self.assert_no_secret(self.upload())


@mock.patch.dict(adapter.HARDWARE_VERIFIED, WRITES_5M, clear=True)
class StartTests(_Base):
    def start(self, name="part.gcode", **kw):
        return adapter.start(self.base, None, CHECK, name, serial_number=SERIAL, **kw)

    def test_documented_body_and_pending_outcome(self):
        result = self.start()
        self.assertEqual((result["ok"], result["outcome"]), (True, "accepted_pending_reconcile"))
        (sent,) = self.posts("/printGcode")
        self.assertEqual(json.loads(sent["body"]), {"serialNumber": SERIAL, "checkCode": CHECK, "fileName": "part.gcode",
                                                     "levelingBeforePrint": False})
        self.assertTrue(self.start(leveling_before_print=True)["ok"])
        self.assertTrue(json.loads(self.posts("/printGcode")[-1]["body"])["levelingBeforePrint"])

    def test_requires_ready_state_not_completed(self):
        for name in ("detail_5m_printing", "detail_5m_completed", "detail_5m_paused", "detail_5m_error", "detail_5m_busy"):
            self.reply(name)
            self.assertEqual(self.start()["error_code"], "printer_busy", name)
        self.assertEqual(self.posts("/printGcode"), [])

    def test_unknown_model_or_other_family_not_started(self):
        for name, code in (("detail_creator5_pid", "unsupported_model"), ("detail_ad5x_ready", "unsupported_unverified")):
            self.reply(name)
            self.assertEqual(self.start()["error_code"], code, name)
        self.assertEqual(self.posts("/printGcode"), [])

    def test_printer_codes_mapped(self):
        for reply, code in (({"code": 3, "message": "x"}, "file_not_found"), ({"code": 2, "message": "x"}, "printer_busy"),
                            ({"code": 1, "message": "x"}, "authentication_failed"), ({"code": 7, "message": "x"}, "start_rejected")):
            self.api_cfg["replies"]["/printGcode"] = reply
            result = self.start()
            self.assertEqual((result["ok"], result["error_code"], result["outcome"]), (False, code, "rejected"))

    def test_timeout_or_garbage_after_send_is_unknown_without_retry(self):
        self.api_cfg["mode"]["/printGcode"] = {"stall": 3}
        with mock.patch.object(adapter, "OPERATION_TIMEOUT", 1.0):
            result = self.start()
        self.assertEqual((result["error_code"], result["outcome"]), ("unknown_outcome", "unknown"))
        self.assertEqual(len(self.posts("/printGcode")), 1)
        for mode in ({"raw": b"nope"}, {"drop": True}, {"raw": b"{}"}):
            self.api_cfg["requests"].clear()
            self.api_cfg["mode"]["/printGcode"] = mode
            result = self.start()
            self.assertEqual((result["error_code"], result["outcome"]), ("unknown_outcome", "unknown"), mode)
            self.assertEqual(len(self.posts("/printGcode")), 1)

    def test_invalid_inputs_and_no_secret_leak(self):
        for name in ("../a.gcode", "a.3mf", "a b.gcode", ""):
            self.assertEqual(self.start(name)["error_code"], "invalid_filename")
        self.assertEqual(self.start(leveling_before_print="yes")["error_code"], "invalid_request")
        self.assertEqual(self.posts("/printGcode"), [])
        self.api_cfg["replies"]["/printGcode"] = {"code": 1, "message": CHECK}
        self.assert_no_secret(self.start())


# --------------------------------------------------------------------------
# TCP (opt-in, read-only)
# --------------------------------------------------------------------------

class TcpPollTests(_Base):
    def poll_tcp(self, **kw):
        return adapter.poll_tcp(self.tcp_base, **kw)

    def test_healthy_idle_and_exact_command_sequence(self):
        result = self.poll_tcp(serial_number=SERIAL)
        self.assertTrue(result["online"])
        self.assertEqual((result["state"], result["model"], result["backend"]), ("idle", "Adventurer 5M", "flashforge_tcp"))
        self.assertEqual(self.tcp_cfg["commands"], ["~M601 S1", "~M115", "~M119", "~M105", "~M27", "~M602"])
        self.assertEqual((result["nozzle"], result["target_nozzle"], result["bed"], result["target_bed"]), (17.9, 0.0, 18.5, 0.0))
        self.assertIsNone(result["progress"])
        self.assertFalse(result["upload_ready"] or result["can_control"] or result["can_start"])
        self.assertEqual(result["upload_reason"], "tcp_read_only")
        self.assert_no_secret(result)

    def test_printing_paused_and_old_temperature_format(self):
        self.tcp_cfg["replies"] = self.healthy_tcp("m119_printing", "m27_printing", "m115_5m_pro", "m105_hot")
        result = self.poll_tcp()
        self.assertEqual((result["state"], result["model"], result["filename"]), ("printing", "Adventurer 5M Pro", "Benchy.gcode"))
        self.assertEqual((result["progress"], result["layer"], result["total_layers"]), (36.64, 23, 100))
        self.assertEqual((result["nozzle"], result["target_nozzle"], result["bed"], result["target_bed"]), (209.5, 210.0, 58.0, 60.0))
        self.tcp_cfg["replies"] = self.healthy_tcp("m119_paused", "m27_printing", temps="m105_old")
        result = self.poll_tcp()
        self.assertEqual(result["state"], "paused")
        self.assertEqual((result["nozzle"], result["target_nozzle"], result["bed"]), (22, 0, 11))

    def test_empty_machine_status_is_unknown(self):
        self.tcp_cfg["replies"] = self.healthy_tcp("m119_empty_status")
        result = self.poll_tcp()
        self.assertFalse(result["online"])
        self.assertEqual((result["state"], result["error_code"]), ("unknown", "unrecognized_response"))
        self.assertEqual(self.tcp_cfg["commands"][-1], "~M602")

    def test_other_model_is_not_taken_for_5m_and_session_is_released(self):
        self.tcp_cfg["replies"] = self.healthy_tcp(info="m115_other_model")
        result = self.poll_tcp()
        self.assertEqual((result["online"], result["error_code"], result["model"]), (False, "unsupported_model", None))
        self.assertEqual(self.tcp_cfg["commands"], ["~M601 S1", "~M115", "~M602"])

    def test_serial_mismatch_rejected(self):
        result = self.poll_tcp(serial_number="SNOTHER-0002")
        self.assertEqual((result["online"], result["error_code"]), (False, "identity_mismatch"))
        self.assertEqual(self.tcp_cfg["commands"][-1], "~M602")
        self.assertNotIn("SNOTHER", json.dumps(result))

    def test_control_session_busy_does_not_release_foreign_session(self):
        self.tcp_cfg["replies"]["M601"] = tcp_fixture("m601_failed")
        result = self.poll_tcp()
        self.assertEqual((result["online"], result["error_code"]), (False, "control_session_busy"))
        self.assertEqual(self.tcp_cfg["commands"], ["~M601 S1"])

    def test_error_reply_dropped_connection_timeout_and_oversize(self):
        self.tcp_cfg["replies"]["M601"] = b"Error: Invalid command\r\n"
        self.assertEqual(self.poll_tcp()["error_code"], "unrecognized_response")
        self.tcp_cfg["replies"] = self.healthy_tcp()
        self.tcp_cfg["drop_on"] = "M105"
        result = self.poll_tcp()
        self.assertEqual((result["online"], result["error_code"]), (False, "connection_closed"))
        self.tcp_cfg.pop("drop_on")
        self.tcp_cfg.update(stall_on="M119", stall=3)
        result = self.poll_tcp(timeout=0.5)
        self.assertEqual((result["online"], result["error_code"]), (False, "timeout"))
        self.tcp_cfg.pop("stall_on")
        self.tcp_cfg["huge_on"] = "M115"
        self.assertEqual(self.poll_tcp()["error_code"], "response_too_large")

    def test_refused_and_invalid_bases(self):
        result = adapter.poll_tcp("tcp://127.0.0.1:%d" % _free_port())
        self.assertEqual((result["state"], result["error_code"]), ("offline", "connection_failed"))
        for base in ("http://127.0.0.1:8899", "tcp://u:p@127.0.0.1:8899", "127.0.0.1", "tcp://127.0.0.1:8899/x", 3):
            self.assertEqual(adapter.poll_tcp(base)["error_code"], "invalid_request", repr(base))
        self.assertEqual(self.tcp_cfg["commands"], [])

    def test_tcp_never_sends_mutating_commands(self):
        self.poll_tcp()
        allowed = {"~M601 S1", "~M115", "~M119", "~M105", "~M27", "~M602"}
        self.assertTrue(set(self.tcp_cfg["commands"]) <= allowed)


if __name__ == "__main__":
    unittest.main()
