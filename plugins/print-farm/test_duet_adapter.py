"""Loopback contract tests for the Duet / RRF / DSF adapter.

All servers are in-process emulators bound to 127.0.0.1 on an ephemeral port.
Fixtures in duet_fixtures/synthetic are hand-built from public documentation
(no hardware capture exists; see duet_fixtures/captured/README.md).
"""

import copy
import http.server
import json
import logging
import shutil
import tempfile
import threading
import time
import unittest
import urllib.parse
import zlib
from pathlib import Path
from unittest import mock

import duet_adapter

FIXTURES = Path(__file__).resolve().parent / "duet_fixtures"
PASSWORD = "secret"
RRF_KEY = 4711
DSF_KEY = "dsf-session"


def fixture(name):
    with (FIXTURES / "synthetic" / (name + ".json")).open(encoding="utf-8") as handle:
        data = json.load(handle)
    assert data["_provenance"]["kind"] == "synthetic"
    return data


def model_fixture(name):
    return copy.deepcopy(fixture(name)["model"])


class Emu:
    """Isolated Duet emulator. mode: 'rrf' | 'dsf' | 'dsf_rr' (DSF + rr emulation)."""

    DEFAULTS = dict(
        rr_connect_status=None,      # force an HTTP status on /rr_connect
        rr_connect_raw=None,         # force a raw body on /rr_connect
        redirect_connect=False,
        dsf_key=None,                # override the DSF sessionKey in /machine/connect
        use_session_key=True,
        upload_err=0,
        dsf_put_status=201,
        write_status=None,           # force HTTP status on write endpoints
        drop_on_write=False,
        hang_on_write=False,
        fileinfo_status=None,
        filelist_status=None,
        fileinfo_hide=False,         # rr_fileinfo answers err 1
        size_delta=0,                # report a different size than stored
        page_size=100,
        code_reply="",
        model_bytes=None,
        model_no_length=False,
        model_drip=False,
        model_hang=False,
    )

    def __init__(self, mode="rrf", model="model_idle_single", password=PASSWORD, **cfg):
        self.mode = mode
        self.password = password
        self.model = model_fixture(model) if isinstance(model, str) else model
        self.files = {}
        self.seen = []
        self.codes = []
        for key, value in self.DEFAULTS.items():
            setattr(self, key, cfg.pop(key, value))
        if cfg:
            raise TypeError(cfg)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.daemon_threads = True
        self.server.emu = self
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def paths(self):
        return [entry["path"] for entry in self.seen]

    def count(self, path):
        return sum(1 for p in self.paths() if p == path)

    def reached(self, prefix):
        return any(p.startswith(prefix) for p in self.paths())


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    # -- helpers ----------------------------------------------------------
    def _body(self):
        left = int(self.headers.get("Content-Length", "0"))
        chunks = []
        while left:
            data = self.rfile.read(min(4096, left))
            if not data:
                break
            chunks.append(data)
            left -= len(data)
        return b"".join(chunks)

    def _send(self, status, body=b"", ctype="application/json", length=True):
        self.send_response(status)
        if body or status not in (204,):
            self.send_header("Content-Type", ctype)
        if length:
            self.send_header("Content-Length", str(len(body)))
        else:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, obj, status=200):
        self._send(status, json.dumps(obj).encode())

    def _write_gate(self):
        """Common failure injection for mutating endpoints. True if handled."""
        emu = self.server.emu
        if emu.hang_on_write:
            time.sleep(1.5)
            self.close_connection = True
            return True
        if emu.drop_on_write:
            self.close_connection = True
            return True
        if emu.write_status is not None:
            self._send(emu.write_status)
            return True
        return False

    # -- routing ----------------------------------------------------------
    def _route(self, method):
        emu = self.server.emu
        parts = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
        entry = {"method": method, "path": parts.path, "query": query,
                 "raw": self.path, "session": self.headers.get("X-Session-Key"),
                 "ctype": self.headers.get("Content-Type")}
        emu.seen.append(entry)
        path = parts.path
        if path.startswith("/rr_"):
            self._rr(method, path, query, entry)
        elif path.startswith("/machine/"):
            self._dsf(method, path, query, entry)
        else:
            self._send(404)

    # -- standalone / emulated rr_* ----------------------------------------
    def _rr(self, method, path, query, entry):
        emu = self.server.emu
        if path == "/rr_connect":
            if emu.redirect_connect:
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:1/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if emu.rr_connect_status is not None:
                self._send(emu.rr_connect_status)
                return
            if emu.mode == "dsf":
                self._send(404)
                return
            if emu.rr_connect_raw is not None:
                self._send(200, emu.rr_connect_raw)
                return
            wrong = query.get("password", [""])[0] != emu.password
            if emu.mode == "dsf_rr":
                key = "connect_dsf_rr_emulation_wrong_password" if wrong else "connect_dsf_rr_emulation_ok"
            else:
                key = "connect_wrong_password" if wrong else "connect_rrf_ok"
            reply = copy.deepcopy(fixture(key)["reply"])
            if not wrong and emu.mode == "rrf" and not emu.use_session_key:
                reply.pop("sessionKey", None)
            self._json(reply)
            return
        if emu.mode != "rrf":
            self._send(500)          # adapter must not use rr_* beyond rr_connect/rr_disconnect
            return
        if emu.use_session_key and entry["session"] != str(RRF_KEY):
            self._send(401)
            return
        if path == "/rr_disconnect":
            self._json({"err": 0})
        elif path == "/rr_model":
            self._rr_model(query)
        elif path == "/rr_upload" and method == "POST":
            body = self._body()
            if self._write_gate():
                return
            wanted = query.get("crc32", [None])[0]
            if emu.upload_err or (wanted and wanted != "%08x" % (zlib.crc32(body) & 0xFFFFFFFF)):
                self._json({"err": 1})
                return
            name = query["name"][0]
            assert name.startswith("0:/gcodes/")
            emu.files[name[len("0:/gcodes/"):]] = body
            self._json({"err": 0})
        elif path == "/rr_fileinfo":
            if emu.fileinfo_status is not None:
                self._send(emu.fileinfo_status)
                return
            name = query["name"][0].replace("0:/gcodes/", "", 1)
            if name in emu.files and not emu.fileinfo_hide:
                self._json({"err": 0, "size": len(emu.files[name]) + emu.size_delta})
            else:
                self._json({"err": 1})
        elif path == "/rr_filelist":
            if emu.filelist_status is not None:
                self._send(emu.filelist_status)
                return
            first = int(query["first"][0])
            names = sorted(emu.files)
            page = names[first:first + emu.page_size]
            nxt = first + emu.page_size if first + emu.page_size < len(names) else 0
            self._json({"dir": query["dir"][0], "first": first, "err": 0, "next": nxt,
                        "files": [{"type": "f", "name": n, "size": len(emu.files[n]) + emu.size_delta} for n in page]})
        elif path == "/rr_gcode":
            if self._write_gate():
                return
            emu.codes.append(query["gcode"][0])
            self._json({"buff": 255})
        else:
            self._send(404)

    def _rr_model(self, query):
        emu = self.server.emu
        key = query["key"][0]
        if emu.model_hang:
            time.sleep(1.5)
            self.close_connection = True
            return
        if emu.model_bytes is not None:
            body = emu.model_bytes
            if emu.model_drip:
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    for i in range(len(body)):
                        self.wfile.write(body[i:i + 1])
                        self.wfile.flush()
                        time.sleep(0.15)
                except OSError:
                    pass
                return
            self._send(200, body, length=not emu.model_no_length)
            return
        node = emu.model
        for piece in key.split("."):
            node = node.get(piece) if isinstance(node, dict) else None
        self._json({"key": key, "flags": query.get("flags", [""])[0], "result": node})

    # -- DSF ----------------------------------------------------------------
    def _dsf(self, method, path, query, entry):
        emu = self.server.emu
        if emu.mode == "rrf":
            self._send(404)
            return
        if path == "/machine/connect":
            if query.get("password", [""])[0] != emu.password:
                self._send(403)
            else:
                self._json({"sessionKey": emu.dsf_key if emu.dsf_key is not None else DSF_KEY})
            return
        if entry["session"] != DSF_KEY:
            self._send(403)
            return
        if path == "/machine/disconnect":
            self._send(204)
        elif path == "/machine/model":
            if emu.model_bytes is not None:
                self._send(200, emu.model_bytes, length=not emu.model_no_length)
            else:
                self._json(emu.model)
        elif path.startswith("/machine/file/") and method == "PUT":
            body = self._body()
            if self._write_gate():
                return
            name = urllib.parse.unquote(path[len("/machine/file/"):])
            assert name.startswith("0:/gcodes/")
            emu.files[name[len("0:/gcodes/"):]] = body
            self._send(emu.dsf_put_status)
        elif path.startswith("/machine/fileinfo/"):
            if emu.fileinfo_status is not None:
                self._send(emu.fileinfo_status)
                return
            name = urllib.parse.unquote(path[len("/machine/fileinfo/0:/gcodes/"):])
            if name in emu.files:
                self._json({"size": len(emu.files[name]) + emu.size_delta})
            else:
                self._send(404)
        elif path == "/machine/code" and method == "POST":
            body = self._body()
            if self._write_gate():
                return
            emu.codes.append(body.decode())
            self._send(200, emu.code_reply.encode(), ctype="text/plain")
        else:
            self._send(404)


class AdapterTestCase(unittest.TestCase):
    def setUp(self):
        self._emus = []
        self._tmp = tempfile.mkdtemp(prefix="duet-adapter-")
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self.addCleanup(self._close_emus)

    def _close_emus(self):
        for emu in self._emus:
            emu.close()

    def emu(self, *args, **kwargs):
        emu = Emu(*args, **kwargs)
        self._emus.append(emu)
        return emu

    def source(self, data=b"G28\nG1 X10\n"):
        path = Path(self._tmp) / "source.gcode"
        path.write_bytes(data)
        return path

    def poll(self, emu, password=PASSWORD, **kwargs):
        return duet_adapter.poll(emu.base, None, password, **kwargs)

    def upload(self, emu, name="part.gcode", data=b"G28\nG1 X10\n", password=PASSWORD, **kwargs):
        return duet_adapter.upload(emu.base, None, password, self.source(data), name, **kwargs)

    def start(self, emu, name="part.gcode", password=PASSWORD, **kwargs):
        return duet_adapter.start(emu.base, None, password, name, **kwargs)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class FixtureTests(unittest.TestCase):
    def test_every_fixture_is_labelled_synthetic_and_no_capture_is_claimed(self):
        files = sorted((FIXTURES / "synthetic").glob("*.json"))
        self.assertGreaterEqual(len(files), 8)
        for path in files:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["_provenance"]["kind"], "synthetic", path.name)
            self.assertFalse(data["_provenance"]["hardware"], path.name)
        self.assertEqual(list((FIXTURES / "captured").glob("*.json")), [])


# ---------------------------------------------------------------------------
# Status: standalone RRF
# ---------------------------------------------------------------------------

class StandaloneStatusTests(AdapterTestCase):
    def test_healthy_standalone_status_session_and_disconnect(self):
        emu = self.emu("rrf")
        result = self.poll(emu)
        self.assertTrue(result["online"])
        self.assertEqual(result["protocol"], "rrf")
        self.assertEqual(result["backend"], "duet_rrf")
        self.assertFalse(result["rr_emulation"])
        self.assertEqual(result["state"], "idle")
        self.assertEqual(result["job_state"], "idle")
        self.assertNotIn("error_code", result)
        self.assertTrue(result["upload_ready"])
        self.assertTrue(result["can_start"])
        self.assertFalse(result["can_control"])
        self.assertEqual(result["nozzle"], 24.0)
        self.assertEqual(result["bed"], 22.5)
        self.assertIsNone(result["target_nozzle"])
        self.assertEqual(result["progress"], 0)
        self.assertEqual(emu.paths()[0], "/rr_connect")
        self.assertEqual(emu.paths()[-1], "/rr_disconnect")
        self.assertFalse(emu.reached("/machine/"))
        for entry in emu.seen[1:]:
            self.assertEqual(entry["session"], str(RRF_KEY))
        keys = [e["query"]["key"][0] for e in emu.seen if e["path"] == "/rr_model"]
        self.assertEqual(keys, ["state", "job", "heat", "tools"])

    def test_empty_password_is_sent_as_given_and_never_replaced(self):
        emu = self.emu("rrf", password="")
        result = self.poll(emu, password="")
        self.assertTrue(result["online"])
        self.assertIn("password=&", emu.seen[0]["raw"])
        self.assertNotIn("reprap", emu.seen[0]["raw"])
        wrong = self.emu("rrf", password="reprap")
        self.assertEqual(self.poll(wrong, password="")["error_code"], "authentication_failed")

    def test_password_with_special_characters_round_trips(self):
        password = "p&a ss+#%"
        emu = self.emu("rrf", password=password)
        self.assertTrue(self.poll(emu, password=password)["online"])

    def test_no_session_key_firmware_still_works(self):
        emu = self.emu("rrf", use_session_key=False)
        self.assertTrue(self.poll(emu)["online"])
        self.assertTrue(all(e["session"] is None for e in emu.seen))

    def test_wrong_password_stops_before_any_other_request(self):
        emu = self.emu("rrf")
        result = self.poll(emu, password="wrong")
        self.assertFalse(result["online"])
        self.assertEqual(result["error_code"], "authentication_failed")
        self.assertEqual(emu.paths(), ["/rr_connect"])

    def test_auth_http_errors_and_odd_replies_never_fall_back_to_dsf(self):
        for status, code in ((401, "authentication_failed"), (403, "authentication_failed"),
                             (503, "device_busy"), (500, "http_error")):
            emu = self.emu("rrf", rr_connect_status=status)
            result = self.poll(emu)
            self.assertEqual(result["error_code"], code, status)
            self.assertEqual(emu.paths(), ["/rr_connect"], status)
        for raw in (b"not json", b"[]", b'{"nope":1}'):
            emu = self.emu("rrf", rr_connect_raw=raw)
            self.assertEqual(self.poll(emu)["error_code"], "protocol_unrecognized")
            self.assertEqual(emu.paths(), ["/rr_connect"])

    def test_no_sessions_available(self):
        emu = self.emu("rrf", rr_connect_raw=b'{"err":2}')
        self.assertEqual(self.poll(emu)["error_code"], "no_sessions_available")

    def test_redirect_is_not_followed(self):
        emu = self.emu("rrf", redirect_connect=True)
        result = self.poll(emu)
        self.assertEqual(result["error_code"], "redirect_rejected")
        self.assertEqual(emu.paths(), ["/rr_connect"])

    def test_partial_and_empty_models_are_unknown_never_idle(self):
        for model in (model_fixture("model_partial"), {}, {"state": None}):
            emu = self.emu("rrf", model=model)
            result = self.poll(emu)
            self.assertEqual(result["state"], "unknown")
            self.assertEqual(result["job_state"], "unknown")
            self.assertFalse(result["upload_ready"])
            self.assertFalse(result["can_start"])
            self.assertEqual(result["error_code"], "unrecognized_rrf_model")
            self.assertFalse(result["online"])

    def test_malformed_json_model_is_unknown(self):
        emu = self.emu("rrf", model_bytes=b'{"result":')
        result = self.poll(emu)
        self.assertEqual(result["error_code"], "malformed_response")
        self.assertEqual(result["state"], "unknown")
        self.assertFalse(result["upload_ready"])

    def test_state_vocabulary(self):
        cases = {
            "idle": "idle", "processing": "printing", "paused": "paused", "pausing": "pausing",
            "resuming": "resuming", "cancelling": "cancelling", "halted": "halted", "off": "off",
            "updating": "updating", "starting": "starting", "busy": "busy",
            "changingTool": "busy", "simulating": "simulating", "disconnected": "disconnected",
            "somethingNew": "unknown",
        }
        for raw, expected in cases.items():
            model = model_fixture("model_idle_single")
            model["state"]["status"] = raw
            result = self.poll(self.emu("rrf", model=model))
            self.assertEqual(result["state"], expected, raw)
            self.assertEqual(result["job_state"], expected, raw)
            self.assertEqual(result["raw_state"], raw)
            self.assertEqual(result["upload_ready"], expected == "idle", raw)
            self.assertEqual(result["can_start"], expected == "idle", raw)
            self.assertFalse(result["can_control"])

    def test_printing_progress_and_filename(self):
        result = self.poll(self.emu("rrf", model="model_idex_printing"))
        self.assertEqual(result["state"], "printing")
        self.assertEqual(result["progress"], 25)
        self.assertEqual(result["filename"], "0:/gcodes/synthetic-part.gcode")
        self.assertFalse(result["upload_ready"])
        self.assertEqual(result["upload_reason"], "state_printing")

    def test_remaining_time_and_layers_come_from_the_object_model(self):
        for mode in ("rrf", "dsf"):
            result = self.poll(self.emu(mode, model="model_idex_printing"))
            self.assertEqual(result["remaining_seconds"], 1500, mode)
            self.assertEqual((result["layer"], result["total_layers"]), (10, 40), mode)

    def test_remaining_falls_back_and_unknown_values_stay_none(self):
        model = model_fixture("model_idex_printing")
        model["job"]["timesLeft"] = {"file": None, "filament": 900.4, "slicer": 5}
        self.assertEqual(self.poll(self.emu("rrf", model=model))["remaining_seconds"], 900)
        model["job"]["timesLeft"] = {"file": -1, "filament": True, "slicer": "x"}
        model["job"]["file"]["numLayers"] = 0
        result = self.poll(self.emu("rrf", model=model))
        self.assertEqual((result["remaining_seconds"], result["layer"], result["total_layers"]),
                         (None, None, None))
        model["job"]["file"]["numLayers"] = 5
        model["job"]["layer"] = 9
        self.assertIsNone(self.poll(self.emu("rrf", model=model))["layer"])

    def test_no_job_statistics_outside_printing_or_paused(self):
        model = model_fixture("model_idex_printing")
        model["state"]["status"] = "idle"
        result = self.poll(self.emu("rrf", model=model))
        self.assertEqual((result["remaining_seconds"], result["layer"], result["total_layers"]),
                         (None, None, None))

    def test_progress_unknown_without_size(self):
        model = model_fixture("model_paused")
        model["job"]["file"].pop("size")
        self.assertIsNone(self.poll(self.emu("rrf", model=model))["progress"])

    def test_paused_keeps_state(self):
        result = self.poll(self.emu("rrf", model="model_paused"))
        self.assertEqual(result["state"], "paused")
        self.assertEqual(result["progress"], 50)

    def test_current_tool_fallback_uses_motion_system(self):
        model = model_fixture("model_idex_printing")
        model["state"].pop("currentTool")
        emu = self.emu("rrf", model=model)
        result = self.poll(emu)
        self.assertEqual(result["active_tool"], 0)
        self.assertEqual(emu.count("/rr_model"), 5)


class IdexTests(AdapterTestCase):
    def test_idex_tools_bed_and_decoy_heater(self):
        for mode in ("rrf", "dsf"):
            result = self.poll(self.emu(mode, model="model_idex_printing"))
            self.assertEqual(result["bed"], 60.0)
            self.assertEqual(result["target_bed"], 60)
            self.assertEqual(result["active_tool"], 0)
            self.assertEqual(result["nozzle"], 209.5)
            self.assertEqual(result["target_nozzle"], 210)
            tools = result["tools"]
            self.assertEqual([t["index"] for t in tools], [0, 1])
            t0, t1 = tools
            self.assertEqual((t0["name"], t0["current"], t0["target"], t0["state"], t0["active"]),
                             ("T0", 209.5, 210, "active", True))
            self.assertEqual((t1["name"], t1["current"], t1["target"], t1["state"], t1["active"]),
                             ("T1", 148.0, 140, "standby", False))
            self.assertNotIn(77.0, [t["current"] for t in tools] + [result["nozzle"], result["bed"]])

    def test_idle_idex_has_no_active_tool_and_null_targets(self):
        result = self.poll(self.emu("rrf", model="model_idex_idle"))
        self.assertIsNone(result["active_tool"])
        self.assertEqual(result["nozzle"], 24.0)      # first tool, sensor reading kept
        self.assertIsNone(result["target_nozzle"])
        self.assertEqual([t["target"] for t in result["tools"]], [None, None])
        self.assertEqual([t["current"] for t in result["tools"]], [24.0, 25.0])
        self.assertEqual(result["tools"][1]["state"], "off")
        self.assertEqual(result["bed"], 60.0)
        self.assertEqual(result["target_bed"], 60)

    def test_missing_heater_is_none_not_zero(self):
        result = self.poll(self.emu("rrf", model="model_idex_missing_heater"))
        t1 = result["tools"][1]
        self.assertIsNone(t1["current"])
        self.assertIsNotNone(result["tools"][0]["current"])
        self.assertEqual(result["nozzle"], 209.5)

    def test_faulted_heater_reading_is_not_trusted(self):
        model = model_fixture("model_idex_printing")
        model["heat"]["heaters"][2]["state"] = "fault"
        result = self.poll(self.emu("rrf", model=model))
        self.assertIsNone(result["tools"][0]["current"])

    def test_tool_numbers_are_not_array_positions_and_nulls_are_skipped(self):
        model = model_fixture("model_idex_printing")
        model["tools"] = [None, {"number": 3, "name": "X", "heaters": [4], "state": "standby", "active": [0], "standby": [99]}]
        model["state"]["currentTool"] = 3
        result = self.poll(self.emu("rrf", model=model))
        self.assertEqual([t["index"] for t in result["tools"]], [3])
        self.assertEqual(result["active_tool"], 3)
        self.assertEqual(result["nozzle"], 148.0)
        self.assertEqual(result["target_nozzle"], 99)

    def test_absent_tools_and_heaters_give_none(self):
        model = model_fixture("model_idle_single")
        model.pop("tools")
        model["heat"] = {}
        result = self.poll(self.emu("rrf", model=model))
        self.assertEqual(result["tools"], [])
        for key in ("nozzle", "target_nozzle", "bed", "target_bed"):
            self.assertIsNone(result[key], key)

    def test_bed_from_mapping_when_legacy_field_absent(self):
        model = model_fixture("model_idex_printing")
        model["heat"].pop("bedHeaters")
        model["heat"]["bedHeaterMapping"] = [[0]]
        self.assertEqual(self.poll(self.emu("rrf", model=model))["bed"], 60.0)

    def test_heater_zero_is_never_assumed_to_be_a_nozzle_or_bed(self):
        model = model_fixture("model_idex_printing")
        model["heat"].pop("bedHeaters")
        result = self.poll(self.emu("rrf", model=model))
        self.assertIsNone(result["bed"])
        self.assertNotEqual(result["nozzle"], 60.0)

    def test_idex_does_not_touch_mirror_or_duplicate_modes(self):
        emu = self.emu("rrf", model="model_idex_printing")
        self.poll(emu)
        self.assertFalse(emu.reached("/rr_gcode"))
        self.assertEqual(emu.codes, [])


# ---------------------------------------------------------------------------
# Status: DSF and DSF in rr-emulation
# ---------------------------------------------------------------------------

class DsfStatusTests(AdapterTestCase):
    def test_native_dsf_after_rr_connect_404(self):
        emu = self.emu("dsf")
        result = self.poll(emu)
        self.assertTrue(result["online"])
        self.assertEqual((result["protocol"], result["backend"]), ("dsf", "duet_dsf"))
        self.assertFalse(result["rr_emulation"])
        self.assertEqual(result["state"], "idle")
        self.assertTrue(result["upload_ready"])
        self.assertNotIn("error_code", result)
        self.assertEqual(emu.paths(), ["/rr_connect", "/machine/connect", "/machine/model", "/machine/disconnect"])
        self.assertEqual(emu.seen[2]["session"], DSF_KEY)

    def test_dsf_rr_emulation_is_not_treated_as_standalone(self):
        emu = self.emu("dsf_rr")
        result = self.poll(emu)
        self.assertTrue(result["online"])
        self.assertEqual(result["protocol"], "dsf")
        self.assertTrue(result["rr_emulation"])
        self.assertFalse(emu.reached("/rr_model"))
        self.assertEqual(emu.paths()[:3], ["/rr_connect", "/rr_disconnect", "/machine/connect"])
        self.assertEqual(emu.paths()[-1], "/machine/disconnect")

    def test_dsf_rr_emulation_wrong_password_never_tries_machine_api(self):
        emu = self.emu("dsf_rr")
        result = self.poll(emu, password="wrong")
        self.assertEqual(result["error_code"], "authentication_failed")
        self.assertEqual(emu.paths(), ["/rr_connect"])

    def test_pinned_rrf_never_sends_the_password_to_the_dsf_api(self):
        emu = self.emu("dsf")
        result = self.poll(emu, backend="rrf")
        self.assertEqual(result["error_code"], "protocol_mismatch")
        self.assertEqual(emu.paths(), ["/rr_connect"])
        self.assertFalse(emu.reached("/machine/"))
        emu = self.emu("dsf")
        self.assertEqual(self.upload(emu, backend="duet_rrf")["error_code"], "protocol_mismatch")
        self.assertFalse(emu.reached("/machine/"))

    def test_invalid_rrf_session_key_closes_the_opened_session(self):
        emu = self.emu("rrf", rr_connect_raw=b'{"err":0,"sessionKey":"bad key","boardType":"x"}')
        result = self.poll(emu)
        self.assertEqual(result["error_code"], "protocol_unrecognized")
        self.assertEqual(emu.paths(), ["/rr_connect", "/rr_disconnect"])

    def test_invalid_dsf_session_key_closes_the_opened_session(self):
        emu = self.emu("dsf", dsf_key="bad key!")
        result = self.poll(emu)
        self.assertEqual(result["error_code"], "authentication_failed")
        self.assertEqual(emu.paths(), ["/rr_connect", "/machine/connect", "/machine/disconnect"])
        self.assertFalse(emu.reached("/machine/model"))

    def test_native_dsf_wrong_password(self):
        emu = self.emu("dsf")
        result = self.poll(emu, password="wrong")
        self.assertEqual(result["error_code"], "authentication_failed")
        self.assertFalse(emu.reached("/machine/model"))

    def test_dsf_partial_and_malformed_models(self):
        emu = self.emu("dsf", model={"state": {"status": "idle"}})
        result = self.poll(emu)
        self.assertEqual(result["error_code"], "unrecognized_dsf_model")
        self.assertEqual(result["state"], "unknown")
        self.assertFalse(result["upload_ready"])
        emu = self.emu("dsf", model_bytes=b'{"state":')
        self.assertEqual(self.poll(emu)["error_code"], "malformed_response")

    def test_dsf_printing_is_not_upload_ready(self):
        result = self.poll(self.emu("dsf", model="model_idex_printing"))
        self.assertEqual(result["state"], "printing")
        self.assertFalse(result["upload_ready"])


# ---------------------------------------------------------------------------
# Transport limits
# ---------------------------------------------------------------------------

class TransportLimitTests(AdapterTestCase):
    def test_oversized_model_with_and_without_content_length(self):
        body = json.dumps({"result": {"status": "idle", "pad": "x" * 5000}}).encode()
        with mock.patch.object(duet_adapter, "MAX_MODEL_BYTES", 1000):
            emu = self.emu("rrf", model_bytes=body)
            self.assertEqual(self.poll(emu)["error_code"], "response_too_large")
            emu = self.emu("rrf", model_bytes=body, model_no_length=True)
            self.assertEqual(self.poll(emu)["error_code"], "response_too_large")
            emu = self.emu("dsf", model_bytes=body, model_no_length=True)
            self.assertEqual(self.poll(emu)["error_code"], "response_too_large")

    def test_silent_server_times_out_with_stable_error(self):
        emu = self.emu("rrf", model_hang=True)
        started = time.monotonic()
        with mock.patch.object(duet_adapter, "TIMEOUT", 0.3):
            result = self.poll(emu)
        self.assertFalse(result["online"])
        self.assertEqual(result["error_code"], "timeout")
        self.assertEqual(result["state"], "offline")
        self.assertLess(time.monotonic() - started, 3)

    def test_slow_drip_hits_total_deadline(self):
        body = json.dumps({"result": {"status": "idle"}}).encode()
        emu = self.emu("rrf", model_bytes=body, model_drip=True)
        started = time.monotonic()
        with mock.patch.object(duet_adapter, "TIMEOUT", 0.4), mock.patch.object(duet_adapter, "POLL_DEADLINE", 0.8):
            result = self.poll(emu)
        self.assertEqual(result["error_code"], "timeout")
        self.assertLess(time.monotonic() - started, 5)

    def test_connection_refused(self):
        emu = self.emu("rrf")
        base = emu.base
        emu.close()
        self._emus.remove(emu)
        result = duet_adapter.poll(base, None, PASSWORD)
        self.assertEqual(result["error_code"], "connection_failed")
        self.assertEqual(result["state"], "offline")

    def test_invalid_base_and_credentials(self):
        self.assertEqual(duet_adapter.poll("http://u:p@127.0.0.1", None, PASSWORD)["error_code"], "invalid_request")
        self.assertEqual(duet_adapter.poll("ftp://127.0.0.1", None, PASSWORD)["error_code"], "invalid_request")
        emu = self.emu("rrf")
        self.assertEqual(self.poll(emu, password="a\nb")["error_code"], "invalid_credentials")
        self.assertEqual(self.poll(emu, password=None)["error_code"], "invalid_credentials")
        self.assertEqual(emu.seen, [])


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

class UploadTests(AdapterTestCase):
    def test_standalone_upload_verified_by_size_and_never_starts(self):
        emu = self.emu("rrf")
        data = b"G28\n" * 100
        result = self.upload(emu, data=data)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["verified"])
        self.assertEqual(result["verification"], "size_match")
        self.assertEqual(result["protocol"], "rrf")
        self.assertEqual(result["outcome"], "completed")
        self.assertEqual(emu.files["part.gcode"], data)
        post = [e for e in emu.seen if e["method"] == "POST"]
        self.assertEqual(len(post), 1)
        self.assertEqual(post[0]["query"]["name"], ["0:/gcodes/part.gcode"])
        self.assertEqual(post[0]["query"]["crc32"], ["%08x" % (zlib.crc32(data) & 0xFFFFFFFF)])
        self.assertEqual(post[0]["ctype"], "application/octet-stream")
        self.assertFalse(emu.reached("/rr_gcode"))
        self.assertEqual(emu.codes, [])
        self.assertEqual(emu.paths()[-1], "/rr_disconnect")
        self.assertTrue(all(e["session"] == str(RRF_KEY) for e in emu.seen[1:]))

    def test_filename_with_space_and_parentheses(self):
        emu = self.emu("rrf")
        result = self.upload(emu, name="my part (v2).gcode")
        self.assertTrue(result["ok"])
        self.assertIn("my part (v2).gcode", emu.files)

    def test_crc_rejection_is_definitive(self):
        emu = self.emu("rrf", upload_err=1)
        result = self.upload(emu)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "upload_rejected")
        self.assertEqual(result["outcome"], "rejected")
        self.assertEqual(emu.files, {})

    def test_upload_refused_while_printing_or_state_unknown(self):
        emu = self.emu("rrf", model="model_idex_printing")
        result = self.upload(emu)
        self.assertEqual(result["error_code"], "device_not_idle")
        self.assertEqual(result["outcome"], "not_sent")
        self.assertFalse(any(e["method"] == "POST" for e in emu.seen))
        emu = self.emu("rrf", model="model_partial")
        self.assertEqual(self.upload(emu)["error_code"], "status_unavailable")
        self.assertFalse(any(e["method"] == "POST" for e in emu.seen))

    def test_size_mismatch_after_upload_is_reported(self):
        emu = self.emu("rrf", size_delta=7)
        result = self.upload(emu)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "verification_failed")
        self.assertEqual(result["outcome"], "applied_unverified")
        self.assertEqual(result["verification"], "size_mismatch")

    def test_verification_unavailable_is_explicit(self):
        emu = self.emu("rrf", fileinfo_status=500, filelist_status=500)
        result = self.upload(emu)
        self.assertTrue(result["ok"])
        self.assertFalse(result["verified"])
        self.assertEqual(result["verification"], "unavailable")

    def test_verification_falls_back_to_paged_filelist(self):
        emu = self.emu("rrf", fileinfo_hide=True, page_size=1)
        emu.files.update({"a.gcode": b"1", "b.gcode": b"22", "z.gcode": b"3"})
        result = self.upload(emu, name="m.gcode")
        self.assertTrue(result["verified"])
        self.assertGreaterEqual(emu.count("/rr_filelist"), 3)

    def test_missing_file_after_upload_fails_verification(self):
        emu = self.emu("rrf", fileinfo_hide=True, page_size=1)
        # Server swallows the upload (err 0) but never stores it.

        class Sink(dict):
            def __setitem__(self, key, value):
                pass
        emu.files = Sink()
        result = self.upload(emu)
        self.assertEqual(result["error_code"], "verification_failed")
        self.assertEqual(result["verification"], "missing")

    def test_connection_drop_after_dispatch_is_unknown_outcome_without_retry(self):
        emu = self.emu("rrf", drop_on_write=True)
        result = self.upload(emu)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "unknown_outcome")
        self.assertEqual(result["outcome"], "unknown")
        self.assertEqual(sum(1 for e in emu.seen if e["method"] == "POST"), 1)

    def test_timeout_after_dispatch_is_unknown_outcome_without_retry(self):
        for mode in ("rrf", "dsf"):
            emu = self.emu(mode, hang_on_write=True)
            with mock.patch.object(duet_adapter, "TIMEOUT", 0.3):
                result = self.upload(emu)
            self.assertEqual(result["error_code"], "unknown_outcome", mode)
            self.assertEqual(sum(1 for e in emu.seen if e["method"] in ("POST", "PUT")), 1, mode)

    def test_write_auth_errors(self):
        for mode in ("rrf", "dsf"):
            emu = self.emu(mode, write_status=403)
            result = self.upload(emu)
            self.assertEqual(result["error_code"], "authorization_denied", mode)
            emu = self.emu(mode, write_status=401)
            self.assertEqual(self.upload(emu)["error_code"], "authentication_failed", mode)

    def test_unsafe_names_and_inputs_never_reach_the_network(self):
        emu = self.emu("rrf")
        bad = ["../evil.gcode", "a/b.gcode", "a\\b.gcode", "..\\x.gcode", "evil.txt", "x:y.gcode",
               "..", ".", "", "a\x00.gcode", "a\nb.gcode", "q?.gcode", "a" * 200 + ".gcode",
               " lead.gcode", "trail.gcode.", "0:/gcodes/x.gcode", None, 5]
        for name in bad:
            result = duet_adapter.upload(emu.base, None, PASSWORD, self.source(), name)
            self.assertEqual(result["error_code"], "invalid_request", repr(name))
        self.assertEqual(duet_adapter.upload("http://user:pass@127.0.0.1", None, PASSWORD, self.source(), "p.gcode")["error_code"], "invalid_request")
        self.assertEqual(duet_adapter.upload(emu.base, None, PASSWORD, Path(self._tmp) / "missing.gcode", "p.gcode")["error_code"], "invalid_request")
        empty = Path(self._tmp) / "empty.gcode"
        empty.write_bytes(b"")
        self.assertEqual(duet_adapter.upload(emu.base, None, PASSWORD, empty, "p.gcode")["error_code"], "invalid_request")
        self.assertEqual(duet_adapter.upload(emu.base, None, PASSWORD, self._tmp, "p.gcode")["error_code"], "invalid_request")
        self.assertEqual(emu.seen, [])

    def test_dsf_upload_streams_to_gcodes_and_verifies(self):
        emu = self.emu("dsf")
        result = self.upload(emu)
        self.assertTrue(result["ok"])
        self.assertEqual(result["evidence_ref"], "dsf-http-201")
        self.assertTrue(result["verified"])
        self.assertEqual(result["protocol"], "dsf")
        put = [e for e in emu.seen if e["method"] == "PUT"]
        self.assertEqual([e["path"] for e in put], ["/machine/file/0:/gcodes/part.gcode"])
        self.assertEqual(put[0]["session"], DSF_KEY)
        self.assertFalse(emu.reached("/machine/code"))
        self.assertEqual(emu.paths()[-1], "/machine/disconnect")

    def test_dsf_upload_accepts_204_and_rejects_other_codes(self):
        self.assertTrue(self.upload(self.emu("dsf", dsf_put_status=204))["ok"])
        self.assertEqual(self.upload(self.emu("dsf", dsf_put_status=500))["error_code"], "upload_rejected")

    def test_dsf_size_mismatch_and_unavailable(self):
        self.assertEqual(self.upload(self.emu("dsf", size_delta=1))["error_code"], "verification_failed")
        result = self.upload(self.emu("dsf", fileinfo_status=500))
        self.assertTrue(result["ok"])
        self.assertFalse(result["verified"])

    def test_dsf_rr_emulation_uploads_through_machine_api(self):
        emu = self.emu("dsf_rr")
        result = self.upload(emu)
        self.assertTrue(result["ok"])
        self.assertEqual(result["protocol"], "dsf")
        self.assertFalse(emu.reached("/rr_upload"))

    def test_upload_refused_while_dsf_printing(self):
        emu = self.emu("dsf", model="model_idex_printing")
        self.assertEqual(self.upload(emu)["error_code"], "device_not_idle")
        self.assertFalse(any(e["method"] == "PUT" for e in emu.seen))

    def test_protocol_pin_blocks_silent_route_change(self):
        emu = self.emu("rrf")
        result = self.upload(emu, backend="dsf")
        self.assertEqual(result["error_code"], "protocol_mismatch")
        self.assertFalse(any(e["method"] in ("POST", "PUT") for e in emu.seen))
        self.assertEqual(self.start(self.emu("dsf"), backend="duet_rrf")["error_code"], "protocol_mismatch")
        self.assertEqual(self.poll(self.emu("dsf"), backend="rrf")["error_code"], "protocol_mismatch")
        emu = self.emu("dsf")
        self.assertTrue(self.upload(emu, backend="duet_dsf")["ok"])
        self.assertEqual(self.upload(self.emu("rrf"), backend="bogus")["error_code"], "invalid_request")


# ---------------------------------------------------------------------------
# Start / control
# ---------------------------------------------------------------------------

class StartTests(AdapterTestCase):
    def test_standalone_start_sends_exactly_one_m32_for_verified_name(self):
        emu = self.emu("rrf")
        emu.files["part.gcode"] = b"G28\n"
        result = self.start(emu, expected_size=4)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["outcome"], "accepted_pending_reconcile")
        self.assertEqual(emu.codes, ['M32 "0:/gcodes/part.gcode"'])
        self.assertEqual(emu.count("/rr_gcode"), 1)

    def test_dsf_start_posts_plain_text_code(self):
        emu = self.emu("dsf")
        emu.files["my part (1).gcode"] = b"G28\n"
        result = self.start(emu, name="my part (1).gcode")
        self.assertTrue(result["ok"], result)
        self.assertEqual(emu.codes, ['M32 "0:/gcodes/my part (1).gcode"'])
        post = [e for e in emu.seen if e["method"] == "POST"]
        self.assertEqual([e["path"] for e in post], ["/machine/code"])
        self.assertEqual(post[0]["ctype"], "text/plain")

    def test_dsf_error_reply_is_rejection(self):
        emu = self.emu("dsf", code_reply="Error: Could not find file")
        emu.files["part.gcode"] = b"G28\n"
        result = self.start(emu)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "start_rejected")

    def test_start_refuses_unsafe_names_without_any_request(self):
        emu = self.emu("rrf")
        bad = ['a";M112;".gcode', "a;b.gcode", "a\nb.gcode", "a$b.gcode", "a'b.gcode", "ä.gcode",
               "a{b}.gcode", "../a.gcode", "a/b.gcode", "a..b.gcode", ".hidden.gcode", "a.txt",
               "a%22b.gcode", "a&b.gcode", "a,b.gcode", "", None]
        for name in bad:
            result = duet_adapter.start(emu.base, None, PASSWORD, name)
            self.assertEqual(result["error_code"], "invalid_request", repr(name))
        self.assertEqual(emu.seen, [])
        self.assertEqual(emu.codes, [])

    def test_start_preconditions(self):
        emu = self.emu("rrf", model="model_idex_printing")
        emu.files["part.gcode"] = b"G28\n"
        self.assertEqual(self.start(emu)["error_code"], "device_not_idle")
        self.assertEqual(emu.codes, [])
        emu = self.emu("rrf")
        self.assertEqual(self.start(emu)["error_code"], "file_not_found")
        emu = self.emu("rrf", fileinfo_hide=True, page_size=1)
        emu.files["part.gcode"] = b"G28\n"
        self.assertTrue(self.start(emu)["ok"])
        emu = self.emu("rrf")
        emu.files["part.gcode"] = b"G28\n"
        self.assertEqual(self.start(emu, expected_size=999)["error_code"], "file_size_mismatch")
        emu = self.emu("rrf", fileinfo_status=500, filelist_status=500)
        emu.files["part.gcode"] = b"G28\n"
        self.assertEqual(self.start(emu)["error_code"], "file_unverified")
        emu = self.emu("rrf", model="model_partial")
        emu.files["part.gcode"] = b"G28\n"
        self.assertEqual(self.start(emu)["error_code"], "status_unavailable")
        self.assertEqual(emu.codes, [])
        emu = self.emu("rrf")
        emu.files["part.gcode"] = b"G28\n"
        self.assertEqual(self.start(emu, expected_size=-1)["error_code"], "invalid_request")

    def test_start_drop_and_timeout_are_unknown_outcome_with_single_request(self):
        for mode, path in (("rrf", "/rr_gcode"), ("dsf", "/machine/code")):
            for cfg in ({"drop_on_write": True}, {"hang_on_write": True}):
                emu = self.emu(mode, **cfg)
                emu.files["part.gcode"] = b"G28\n"
                with mock.patch.object(duet_adapter, "TIMEOUT", 0.3):
                    result = self.start(emu)
                self.assertEqual(result["error_code"], "unknown_outcome", (mode, cfg))
                self.assertEqual(result["outcome"], "unknown")
                self.assertEqual(emu.count(path), 1, (mode, cfg))

    def test_pause_resume_cancel_are_unsupported_and_do_no_io(self):
        emu = self.emu("rrf")
        for func in (duet_adapter.pause, duet_adapter.resume, duet_adapter.cancel):
            result = func(emu.base, None, PASSWORD)
            self.assertEqual(result, {"ok": False, "error_code": "unsupported_unverified", "outcome": "not_sent"})
        self.assertEqual(emu.seen, [])
        caps = self.poll(emu)["capabilities"]
        self.assertEqual(caps["pause"], "unsupported_unverified")
        self.assertEqual(caps["start"], "implemented_requires_explicit_confirmation")


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

class SecretHandlingTests(AdapterTestCase):
    def test_password_and_session_keys_never_leak_into_results_or_logs(self):
        password = "Tr0ub4dor&3 secret"
        secrets = [password, urllib.parse.quote(password, safe=""), str(RRF_KEY), DSF_KEY]
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Capture(level=logging.DEBUG)
        root = logging.getLogger()
        old_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.removeHandler, handler)
        self.addCleanup(root.setLevel, old_level)

        outputs = []
        scenarios = [
            dict(mode="rrf", password=password), dict(mode="dsf", password=password),
            dict(mode="rrf", password="other"), dict(mode="rrf", password=password, drop_on_write=True),
            dict(mode="dsf", password=password, hang_on_write=True),
            dict(mode="rrf", password=password, model="model_partial"),
        ]
        for cfg in scenarios:
            emu = self.emu(**cfg)
            emu.files["part.gcode"] = b"G28\n"
            with mock.patch.object(duet_adapter, "TIMEOUT", 0.3):
                outputs.append(self.poll(emu, password=password))
                outputs.append(self.upload(emu, password=password))
                outputs.append(self.start(emu, password=password))
        outputs.append(duet_adapter.poll("http://user:%s@127.0.0.1" % "x", None, password))
        text = repr(outputs) + " ".join(records)
        for secret in secrets:
            self.assertNotIn(secret, text)
        self.assertEqual(records, [])


if __name__ == "__main__":
    unittest.main()
