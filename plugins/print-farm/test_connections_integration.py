"""Duet, Creality and FlashForge through the real Print Farm path, on loopback emulators.

UI messages -> request checks -> coordinator -> driver -> stored result -> DTO.
Only 127.0.0.1 servers are used (the emulators of the driver tests); the plugin
runs unmocked except for the host (``orca``) itself.
"""

import hashlib
import json
import os
import pathlib
import re
import shutil
import threading
import unittest
from unittest import mock

from test_creality_adapter import _HttpServer, _UploadHandler, _WsHandler, _WsServer, frame
from test_creality_adapter import fixture as creality_fixture
from test_duet_adapter import DSF_KEY, Emu
from test_flashforge_adapter import CHECK as FF_CHECK, SERIAL as FF_SERIAL
from test_flashforge_adapter import _ApiHandler as _FfApiHandler, _ApiServer as _FfApiServer
from test_flashforge_adapter import fixture as ff_fixture
from test_printers_plugin import _load_plugin

PASSWORD = "hunter2-pw"
PLUGIN_DIR = pathlib.Path(__file__).resolve().parent


class _Base(unittest.TestCase):
    def setUp(self):
        self.plugin = _load_plugin()
        self.dashboard = self.plugin.PrintersDashboard()
        self.dashboard._post = mock.Mock(return_value=True)
        self.root = pathlib.Path(self.plugin.PLUGIN_STORAGE_DIR)
        self.addCleanup(shutil.rmtree, str(self.root), True)
        self.addCleanup(self.dashboard._stop_action_scheduler)
        self.closers = []
        self.addCleanup(self._close_all)

    def _close_all(self):
        for closer in reversed(self.closers):
            closer()
        for handle in list(self.plugin._STORAGE_OWNER_HANDLES.values()):
            handle.close()
        self.plugin._STORAGE_OWNER_HANDLES.clear()

    # -- fixtures -----------------------------------------------------------
    def duet_emu(self, mode="rrf", model="model_idle_single", **cfg):
        emu = Emu(mode, model, password=PASSWORD, **cfg)
        self.closers.append(emu.close)
        return emu

    def add_printer(self, **entry):
        base = {"name": "Card", "type": "duet", "apikey": "", "serial": "",
                "access_code": "", "insecure": False, "source": "manual"}
        base.update(entry)
        stored = self.dashboard._add_manual(base)
        self.assertIsNotNone(stored)
        return self.current(stored)

    def current(self, printer):
        return next(p for p in self.plugin.dashboard_printers()
                    if p["target_uuid"] == printer["target_uuid"])

    @staticmethod
    def identity(printer):
        return {"targetUuid": printer["target_uuid"],
                "ownerScope": printer["owner_scope_uuid"],
                "targetSelector": printer["target_selector"],
                "bindingRevision": printer["binding_revision"],
                "url": printer["url"], "connectionType": printer["type"]}

    def stage_gcode(self, name="job.gcode", body=b"G28\nG1 X10\n", token="token"):
        incoming = self.root / "incoming"
        incoming.mkdir(exist_ok=True)
        staged = incoming / ("pf-artifact-" + "c" * 32 + ".gcode")
        staged.write_bytes(body)
        artifact = self.plugin._pf_artifact.inspect_staged_path(
            staged, name, incoming, expected_sha256=hashlib.sha256(body).hexdigest())
        self.dashboard._received_artifacts[token] = artifact
        return artifact

    def run_submit(self, printer, *, start, grants=None):
        message = {"artifactToken": "token", "plate": None, "use_ams": False,
                   "start": start, "targets": [self.identity(printer)]}
        if start:
            message["approvals"] = grants
        self.dashboard._submit_mass_print(message)
        scheduler = self.dashboard._action_scheduler
        if scheduler is not None:
            self.assertTrue(scheduler.wait_for_idle(15))

    def approve_start(self, printer):
        message = {"artifactToken": "token", "plate": None, "use_ams": False,
                   "targets": [self.identity(printer)]}
        self.dashboard._prepare_mass_start(message)
        prepared = next(call.args[0]["massStartApprovals"]
                        for call in reversed(self.dashboard._post.call_args_list)
                        if "massStartApprovals" in call.args[0])
        grant = prepared["targets"][0]
        self.assertNotEqual(grant["verdict"], "incompatible", grant)
        return prepared, [{"targetUuid": grant["targetUuid"], "nonce": grant["nonce"],
                           "bedClear": True, "operatorChecked": True}]

    def statuses(self):
        return [call.args[0]["status"] for call in self.dashboard._post.call_args_list
                if "status" in call.args[0]]

    def operations(self):
        database = os.path.join(os.path.realpath(self.plugin.PLUGIN_STORAGE_DIR),
                                "operations.sqlite3")
        if not os.path.exists(database):
            return []
        journal = self.plugin._pf_journal.OperationJournal(database)
        try:
            return journal.list_operations(self.dashboard._journal_account_id(), 50)
        finally:
            journal.close()


class DuetStatusTest(_Base):
    def test_standalone_idex_card_reaches_the_dto_without_secrets(self):
        emu = self.duet_emu("rrf", "model_idex_idle")
        printer = self.add_printer(url=emu.base, apikey=PASSWORD)
        self.dashboard._poll_into(printer)

        payload = self.dashboard._post.call_args.args[0]["printerStatus"]["status"]
        self.assertEqual(payload["backend"], "duet")
        self.assertEqual(payload["protocol"], "rrf")
        self.assertIs(payload["rr_emulation"], False)
        self.assertTrue(payload["online"])
        self.assertEqual(payload["authorization"], "accepted")
        self.assertIs(payload["upload_ready"], True)
        self.assertIs(payload["can_start"], True)
        self.assertGreaterEqual(len(payload["tools"]), 2)
        self.assertEqual({"index", "name", "current", "target", "state", "active"},
                         set(payload["tools"][0]))
        self.assertEqual(payload["capabilities"]["pause"], "unsupported_unverified")
        dto = self.plugin.public_printer_record({**printer, "status": payload})
        self.assertEqual(dto["status"]["tools"], payload["tools"])
        for blob in (json.dumps(self.dashboard._post.call_args_list[-1].args[0]),
                     json.dumps(dto)):
            self.assertNotIn(PASSWORD, blob)
            self.assertNotIn(DSF_KEY, blob)
            self.assertNotIn("apikey", blob)

    def test_dsf_card_and_protocol_pin_is_stored_without_a_revision_bump(self):
        emu = self.duet_emu("dsf")
        printer = self.add_printer(url=emu.base, apikey=PASSWORD)
        revision = printer["binding_revision"]
        status = self.plugin.poll_printer(printer)
        self.assertEqual((status["protocol"], status["backend"]), ("dsf", "duet"))
        stored = self.current(printer)
        self.assertEqual(stored["protocol"], "dsf")
        self.assertEqual(stored["binding_revision"], revision)
        # The pinned protocol is what later polls ask for.
        with mock.patch.object(self.plugin._pf_duet, "poll",
                               wraps=self.plugin._pf_duet.poll) as poll:
            self.plugin.poll_printer(stored)
        self.assertEqual(poll.call_args.kwargs["backend"], "dsf")
        self.assertNotIn("protocol", self.plugin.public_printer_record(stored))

    def test_dsf_rr_emulation_is_reported_and_labelled_dsf(self):
        emu = self.duet_emu("dsf_rr")
        status = self.plugin.poll_printer(self.add_printer(url=emu.base, apikey=PASSWORD))
        self.assertEqual(status["protocol"], "dsf")
        self.assertIs(status["rr_emulation"], True)

    def test_a_changed_protocol_is_refused_instead_of_silently_rerouted(self):
        emu = self.duet_emu("rrf")
        printer = self.add_printer(url=emu.base, apikey=PASSWORD, protocol="dsf")
        status = self.plugin.poll_printer(printer)
        self.assertFalse(status["online"])
        self.assertEqual(status["error_code"], "protocol_mismatch")
        self.assertFalse(self.plugin.status_can_upload(status, printer))
        self.assertEqual(emu.files, {})

    def test_saving_a_pinned_card_again_clears_the_pin(self):
        emu = self.duet_emu("rrf")
        printer = self.add_printer(url=emu.base, apikey=PASSWORD)
        self.plugin.poll_printer(printer)
        pinned = self.current(printer)
        self.assertEqual(pinned["protocol"], "rrf")
        again = self.dashboard._add_manual({
            **{k: pinned[k] for k in ("name", "url", "type", "apikey", "source")},
            "target_uuid": pinned["target_uuid"],
            "owner_scope_uuid": pinned["owner_scope_uuid"],
            "target_selector": pinned["target_selector"],
            "binding_revision": pinned["binding_revision"],
        })
        self.assertNotIn("protocol", again)

    def test_wrong_password_is_an_authorization_denial_like_other_backends(self):
        emu = self.duet_emu("rrf")
        printer = self.add_printer(url=emu.base, apikey="wrong-" + PASSWORD)
        status = self.plugin.poll_printer(printer)
        self.assertEqual(status["authorization"], "rejected")
        self.assertEqual(status["error_code"], "authorization_denied")
        self.assertTrue(status["transport_online"])
        self.assertNotIn(PASSWORD, json.dumps(status))
        self.assertFalse(self.plugin.status_can_upload(status, printer))

    def test_busy_duet_blocks_upload_start_and_every_control(self):
        emu = self.duet_emu("rrf", "model_idex_printing")
        printer = self.add_printer(url=emu.base, apikey=PASSWORD)
        status = self.plugin.poll_printer(printer)
        self.assertEqual(status["state"], "printing")
        self.assertFalse(status["upload_ready"])
        self.assertFalse(self.plugin.status_can_upload(status, printer))
        self.assertFalse(self.plugin.status_can_start(status, printer))
        for action in ("pause", "resume", "cancel"):
            self.assertFalse(self.plugin.status_can_control(status, action, printer))
            self.assertEqual(
                self.plugin.printer_command(printer, action, status)["error_code"], "not_ready")
        self.stage_gcode()
        self.run_submit(printer, start=False)
        self.assertEqual(emu.files, {})
        self.assertFalse(emu.reached("/rr_upload"))


class DuetSendTest(_Base):
    def sent(self, mode, **cfg):
        emu = self.duet_emu(mode, **cfg)
        printer = self.add_printer(url=emu.base, apikey=PASSWORD)
        self.plugin.poll_printer(printer)       # first good poll pins the protocol
        return emu, self.current(printer)

    def check_upload_only(self, mode):
        emu, printer = self.sent(mode)
        self.stage_gcode()
        self.run_submit(printer, start=False)
        self.assertEqual(emu.files["job.gcode"], b"G28\nG1 X10\n")
        self.assertEqual(emu.codes, [])
        self.assertIn("Sent to 1 printer", self.statuses()[-1])
        self.assertEqual(self.operations()[0]["state"], "verified")
        self.assertTrue(self.operations()[0]["evidence_ref"].startswith("duet:" + mode))

    def test_upload_only_rrf_lands_in_the_emulator_and_is_journaled_verified(self):
        self.check_upload_only("rrf")

    def test_upload_only_dsf_lands_in_the_emulator_and_is_journaled_verified(self):
        self.check_upload_only("dsf")

    def check_start(self, mode):
        emu, printer = self.sent(mode)
        self.stage_gcode()
        prepared, grants = self.approve_start(printer)
        self.assertEqual(prepared["targets"][0]["verdict"], "unknown")
        self.run_submit(printer, start=True, grants=grants)
        self.assertEqual(emu.files["job.gcode"], b"G28\nG1 X10\n")
        self.assertEqual(emu.codes, ['M32 "0:/gcodes/job.gcode"'])
        writes = [e for e in emu.seen if e["path"] == "/rr_upload"
                  or (e["method"] == "PUT" and e["path"].startswith("/machine/file/"))]
        self.assertEqual(len(writes), 1)
        operation = self.operations()[0]
        self.assertEqual((operation["action"], operation["state"]), ("start", "submitted"))
        self.assertIn("Sent to 1 printer", self.statuses()[-1])

    def test_start_rrf_uploads_then_starts_once_with_the_approved_plan(self):
        self.check_start("rrf")

    def test_start_dsf_uploads_then_starts_once_with_the_approved_plan(self):
        self.check_start("dsf")

    def test_start_needs_a_fresh_approval_and_nothing_is_sent_without_it(self):
        emu, printer = self.sent("rrf")
        self.stage_gcode()
        self.run_submit(printer, start=True, grants=[
            {"targetUuid": printer["target_uuid"], "nonce": "forged", "bedClear": True,
             "operatorChecked": True}])
        self.assertEqual(emu.files, {})
        self.assertEqual(emu.codes, [])

    def test_start_is_not_attempted_when_the_upload_cannot_be_verified(self):
        emu, printer = self.sent("rrf", fileinfo_status=500, filelist_status=500)
        self.stage_gcode()
        _prepared, grants = self.approve_start(printer)
        self.run_submit(printer, start=True, grants=grants)
        self.assertIn("job.gcode", emu.files)
        self.assertEqual(emu.codes, [])
        self.assertIn("not started", self.statuses()[-1])
        # The print was provably not started: no hold is left on the target.
        self.assertEqual(self.operations()[0]["state"], "rejected")

    def test_unknown_outcome_is_reported_held_and_never_retried(self):
        emu, printer = self.sent("rrf", drop_on_write=True)
        self.stage_gcode()
        self.run_submit(printer, start=False)
        self.assertEqual(emu.count("/rr_upload"), 1)
        self.assertEqual(self.operations()[0]["state"], "unknown_outcome")
        self.assertIn("may or may not", self.statuses()[-1])
        # A second try is blocked by the hold and says why; no second upload.
        self.stage_gcode()
        self.run_submit(printer, start=False)
        self.assertEqual(emu.count("/rr_upload"), 1)
        self.assertIn("unresolved", self.statuses()[-1])
        self.assertNotIn(PASSWORD, "".join(self.statuses()))

    def test_rejected_write_leaves_no_hold_and_names_the_reason(self):
        emu, printer = self.sent("rrf", write_status=403)
        self.stage_gcode()
        self.run_submit(printer, start=False)
        self.assertEqual(self.operations()[0]["state"], "rejected")
        self.assertIn("authentication rejected", self.statuses()[-1])

    def test_a_non_ascii_name_is_refused_before_anything_is_uploaded(self):
        emu, printer = self.sent("rrf")
        status = self.plugin.poll_printer(printer)
        source = self.root / "x.gcode"
        source.write_bytes(b"G28\n")
        result = self.plugin.send_file_path(
            printer, "деталь.gcode", str(source), True,
            status=status)
        self.assertEqual(result["error_code"], "start_name_invalid")
        self.assertEqual(result["journal_state"], "rejected")
        self.assertFalse(emu.reached("/rr_upload"))
        self.assertEqual(emu.files, {})

    def test_the_stored_pin_and_the_live_protocol_must_agree_before_a_write(self):
        emu, printer = self.sent("rrf")
        status = self.plugin.poll_printer(printer)
        source = self.root / "x.gcode"
        source.write_bytes(b"G28\n")
        result = self.plugin.send_file_path(
            dict(printer, protocol="dsf"), "x.gcode", str(source), False, status=status)
        self.assertEqual(result["error_code"], "protocol_mismatch")
        self.assertEqual(emu.files, {})


class CrealityTest(_Base):
    def setUp(self):
        super().setUp()
        self.cfg = {"handshakes": [], "received": []}
        self.set_status("idle")
        ws = _WsServer(("127.0.0.1", 0), _WsHandler)
        ws.cfg = self.cfg
        threading.Thread(target=ws.serve_forever, daemon=True).start()
        self.up_cfg = {"posts": []}
        http = _HttpServer(("127.0.0.1", 0), _UploadHandler)
        http.cfg = self.up_cfg
        threading.Thread(target=http.serve_forever, daemon=True).start()
        for server in (ws, http):
            self.closers.append(server.server_close)
            self.closers.append(server.shutdown)
        self.ws_port = ws.server_address[1]
        self.upload_url = "http://127.0.0.1:%d/upload" % http.server_address[1]

    def set_status(self, name):
        payload = json.dumps(creality_fixture(name)).encode()
        self.cfg["reply"] = lambda _body: [frame(0x1, payload)]

    def printer(self, **extra):
        return self.add_printer(type="crealityprint", url=self.upload_url,
                                ws_port=self.ws_port, **extra)

    def test_the_type_is_selectable_and_asks_only_for_an_optional_port(self):
        page = self.plugin.render_page()
        self.assertIn('<option value="crealityprint">Creality</option>', page)
        self.assertNotIn("Creality — coming later", page)
        self.assertIn('id="f-wsport"', page)
        self.assertIn("crealityprint", json.loads(
            re.search(r"var ADAPTER_TYPES = (\[.*?\]);", page).group(1)))
        self.assertTrue(self.plugin.backend_is_active("crealityprint"))
        self.assertEqual(self.plugin.creality_ws_port({}), 9999)
        self.assertEqual(self.plugin.creality_ws_port({"ws_port": "bad"}), 9999)

    def test_ws_port_is_validated_before_it_is_stored(self):
        parse = self.plugin.parse_ws_port
        self.assertIsNone(parse(""))
        self.assertIsNone(parse(None))
        self.assertEqual(parse(" 8080 "), 8080)
        for bad in ("abc", "0", "65536", "-1", "1e3", True, "99999", "٣"):
            self.assertIs(parse(bad), False, bad)
        self.dashboard.on_message({"type": "add", "kind": "crealityprint",
                                   "url": self.upload_url, "ws_port": "99999"})
        self.assertEqual(self.plugin.dashboard_printers(), [])

    def test_status_is_readable_while_upload_stays_closed_with_a_reason(self):
        printer = self.printer()
        status = self.plugin.poll_printer(printer)
        self.assertTrue(status["online"])
        self.assertEqual((status["backend"], status["state"]), ("crealityprint", "idle"))
        self.assertEqual(status["authorization"], "accepted")
        self.assertEqual(status["error"], "")
        self.assertNotIn("error_code", status)
        self.assertEqual(status["upload_reason"], "writable_target_unverified")
        self.assertIs(status["upload_ready"], False)
        self.assertIsNone(status["nozzle"])
        self.assertIsNone(status["bed"])
        self.assertFalse(self.plugin.status_can_upload(status, printer))
        self.assertFalse(self.plugin.status_can_start(status, printer))
        self.assertFalse(self.plugin.status_can_control(status, "cancel", printer))
        dto = self.plugin.public_printer_record({**printer, "status": status})
        self.assertEqual(dto["status"]["upload_reason"], "writable_target_unverified")

    def test_job_states_are_visible_like_other_types(self):
        printer = self.printer()
        for name, state in (("printing", "printing"), ("paused", "paused"),
                            ("complete", "complete"), ("failed", "failed"),
                            ("aborted", "aborted")):
            with self.subTest(name=name):
                self.set_status(name)
                status = self.plugin.poll_printer(printer)
                self.assertEqual(status["state"], state)
                self.assertTrue(status["online"])
                self.assertIsNone(status["nozzle"])
        self.set_status("printing")
        status = self.plugin.poll_printer(printer)
        self.assertEqual((status["progress"], status["filename"]), (42, "part.gcode"))

    def test_the_default_port_is_used_when_none_is_given(self):
        printer = self.add_printer(type="crealityprint", url=self.upload_url)
        self.assertIsNone(printer.get("ws_port"))
        ws_base = self.plugin._pf_creality.ws_base
        seen = []

        def fake_ws_base(url, port):
            seen.append((url, port))
            return ws_base(url, self.ws_port)       # never touch a real port 9999

        with mock.patch.object(self.plugin._pf_creality, "ws_base", fake_ws_base):
            status = self.plugin.poll_printer(printer)
        self.assertEqual(seen, [(self.upload_url, 9999)])
        self.assertTrue(status["online"])

    def test_upload_through_the_coordinator_is_refused_and_sends_nothing(self):
        printer = self.printer()
        self.stage_gcode()
        self.run_submit(printer, start=False)
        self.assertEqual(self.up_cfg["posts"], [])
        self.assertIn("not reachable", self.statuses()[-1])
        self.assertEqual(self.operations(), [])
        # A start is blocked at the approval step: no nonce for an unsupported type.
        artifact = self.stage_gcode()
        self.assertEqual(self.plugin.start_compatibility(printer, artifact, None),
                         ("incompatible", "startReasonUnsupported"))

    def test_the_upload_branch_is_wired_to_the_driver_for_when_the_gate_opens(self):
        printer = self.printer()
        status = self.plugin.poll_printer(printer)
        source = self.root / "part.gcode"
        source.write_bytes(b"G28\n")
        with mock.patch.object(self.plugin, "status_can_upload", return_value=True),                 mock.patch.object(self.plugin, "status_can_start", return_value=True):
            result = self.plugin.send_file_path(printer, "part.gcode", str(source), False,
                                                status=status)
            started = self.plugin.send_file_path(printer, "part.gcode", str(source), True,
                                                 status=status)
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.up_cfg["posts"]), 1)
        self.assertEqual(self.up_cfg["posts"][0][0], "/upload/part.gcode")
        self.assertFalse(started["ok"])
        self.assertEqual(len(self.up_cfg["posts"]), 1)    # start never reaches the network
        self.assertEqual(started["journal_state"], "rejected")


class FlashForgeTest(_Base):
    """FlashForge through the real plugin path against the loopback API emulator.

    Status is read-only; the write gate (HARDWARE_VERIFIED) stays empty in the
    driver, so upload/start/controls are closed with a reason.
    """

    def setUp(self):
        super().setUp()
        self.ff_cfg = {"requests": [], "mode": {}, "serial": FF_SERIAL, "check": FF_CHECK,
                       "replies": {"/detail": ff_fixture("detail_5m_ready")}}
        server = _FfApiServer(("127.0.0.1", 0), _FfApiHandler)
        server.cfg = self.ff_cfg
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.closers.append(server.server_close)
        self.closers.append(server.shutdown)
        self.base = "http://127.0.0.1:%d" % server.server_address[1]

    def printer(self, **extra):
        entry = {"type": "flashforge", "url": self.base, "serial": FF_SERIAL,
                 "apikey": FF_CHECK}
        entry.update(extra)
        return self.add_printer(**entry)

    def reply(self, name):
        self.ff_cfg["replies"]["/detail"] = ff_fixture(name)

    def requests(self, route):
        return [r for r in self.ff_cfg["requests"] if r["path"] == route]

    def test_the_type_is_selectable_and_asks_for_serial_and_check_code(self):
        page = self.plugin.render_page()
        self.assertIn('<option value="flashforge">FlashForge</option>', page)
        self.assertNotIn("FlashForge — coming later", page)
        self.assertTrue(self.plugin.backend_is_active("flashforge"))
        for variable in ("ADAPTER_TYPES", "CREDENTIAL_TYPES", "ACTIVE_TYPES"):
            self.assertIn("flashforge", json.loads(
                re.search(r"var %s = (\[.*?\]);" % variable, page).group(1)), variable)
        for key in ("flashforgeCheckCodePlaceholder", "flashforgeSerialPlaceholder"):
            self.assertIn(key, page)
        self.assertEqual(self.plugin.UI_COPY["ru"]["flashforgeCheckCodePlaceholder"],
                         "Check code (на экране принтера)")
        self.assertEqual(self.plugin.UI_COPY["en"]["flashforgeCheckCodePlaceholder"],
                         "Check code (on the printer screen)")

    def test_add_requires_serial_and_keeps_the_check_code_untouched(self):
        long_code = " " + "9" * 62 + " "
        with mock.patch.object(self.dashboard, "_capture_discovered_printers",
                               return_value=[]),                 mock.patch.object(self.plugin.ACTION_WORKER, "submit") as submit:
            for serial, apikey in (("", FF_CHECK), ("   ", FF_CHECK), (FF_SERIAL, "")):
                self.dashboard.on_message({"type": "add", "kind": "flashforge",
                                           "url": self.base, "serial": serial,
                                           "apikey": apikey})
            submit.assert_not_called()
            self.dashboard.on_message({"type": "add", "kind": "flashforge", "url": self.base,
                                       "serial": "  " + FF_SERIAL + " ", "apikey": long_code,
                                       "access_code": "not-kept"})
            entry = submit.call_args.args[1]
        self.assertEqual((entry["serial"], entry["apikey"]), (FF_SERIAL, long_code))
        self.assertEqual(entry["access_code"], "")
        stored = self.dashboard._add_manual(entry)
        self.assertEqual((stored["serial"], stored["apikey"]), (FF_SERIAL, long_code))

    def test_status_is_read_through_the_real_path_and_reaches_the_dto_without_secrets(self):
        printer = self.printer()
        self.dashboard._poll_into(printer)
        payload = self.dashboard._post.call_args.args[0]["printerStatus"]["status"]
        self.assertEqual(payload["backend"], "flashforge")
        self.assertTrue(payload["online"])
        self.assertEqual(payload["authorization"], "accepted")
        self.assertEqual(payload["state"], "idle")
        self.assertEqual((payload["model"], payload["model_family"]),
                         ("Adventurer 5M", "adventurer_5m"))
        self.assertIs(payload["identity_verified"], True)
        self.assertEqual(payload["firmware"], "v3.2.7")
        self.assertEqual((payload["nozzle"], payload["bed"]), (25, 24))
        self.assertIs(payload["upload_ready"], False)
        self.assertEqual(payload["upload_reason"], "hardware_unverified")
        self.assertIs(payload["can_control"], False)
        self.assertEqual(payload["capabilities"]["pause"], "unsupported_unverified")
        self.assertEqual(payload["error"], "")
        self.assertEqual(json.loads(self.requests("/detail")[0]["body"]),
                         {"serialNumber": FF_SERIAL, "checkCode": FF_CHECK})
        dto = self.plugin.public_printer_record({**printer, "status": payload})
        self.assertEqual(dto["serial"], FF_SERIAL)       # not a secret
        for blob in (json.dumps(self.dashboard._post.call_args_list[-1].args[0]),
                     json.dumps(dto)):
            self.assertNotIn(FF_CHECK, blob)
            self.assertNotIn("apikey", blob)

    def test_a_printing_card_shows_progress_and_stays_busy(self):
        printer = self.printer()
        self.reply("detail_5m_printing")
        status = self.plugin.poll_printer(printer)
        self.assertEqual(status["state"], "printing")
        self.assertIsNotNone(status["progress"])
        self.assertNotEqual(status["filename"], "")
        self.assertFalse(self.plugin.status_can_upload(status, printer))
        for action in ("pause", "resume", "cancel"):
            self.assertFalse(self.plugin.status_can_control(status, action, printer))
            self.assertEqual(
                self.plugin.printer_command(printer, action, status)["error_code"], "not_ready")

    def test_the_three_supported_models_are_recognised(self):
        printer = self.printer()
        for name, model, family in (
                ("detail_5m_ready", "Adventurer 5M", "adventurer_5m"),
                ("detail_5m_pro_printing", "Adventurer 5M Pro", "adventurer_5m"),
                ("detail_ad5x_ready", "AD5X", "ad5x")):
            with self.subTest(model=model):
                self.reply(name)
                status = self.plugin.poll_printer(printer)
                self.assertTrue(status["online"])
                self.assertEqual((status["model"], status["model_family"]), (model, family))
                self.assertIs(status["upload_ready"], False)
                self.assertEqual(status["upload_reason"], "hardware_unverified")
                self.assertFalse(self.plugin.status_can_upload(status, printer))
                self.assertFalse(self.plugin.status_can_start(status, printer))

    def test_a_foreign_model_is_unsupported_and_never_writable(self):
        printer = self.printer()
        for name in ("detail_creator5_pid", "detail_guider4_pid"):
            with self.subTest(name=name):
                self.reply(name)
                status = self.plugin.poll_printer(printer)
                self.assertEqual(status["error_code"], "unsupported_model")
                self.assertFalse(status["online"])
                self.assertFalse(status.get("identity_verified"))
                self.assertFalse(self.plugin.status_can_upload(status, printer))

    def test_error_codes_are_stable_and_carry_no_printer_text_or_secrets(self):
        printer = self.printer()
        for name, code in (("detail_no_pid", "identity_unverified"),
                           ("lan_mode_off", "lan_mode_disabled")):
            with self.subTest(code=code):
                self.reply(name)
                status = self.plugin.poll_printer(printer)
                self.assertEqual(status["error_code"], code)
                self.assertFalse(status["online"])
                self.assertEqual(status["error"], self.plugin.ui_text("genericError"))
        for key in ("flashforgeErrAuth", "flashforgeErrLanMode", "flashforgeErrUnsupportedModel",
                    "flashforgeErrIdentity", "flashforgeErrCredentials"):
            self.assertIn(key, self.plugin.UI_COPY["en"])
        for bad in (dict(printer, apikey="wrong-" + FF_CHECK),
                    dict(printer, serial="SNWRONG-0000")):
            self.reply("detail_5m_ready")
            status = self.plugin.poll_printer(bad)
            self.assertEqual(status["error_code"], "authorization_denied")
            self.assertEqual(status["authorization"], "rejected")
            self.assertTrue(status["transport_online"])
            blob = json.dumps(status)
            for secret in (FF_CHECK, "wrong-" + FF_CHECK, FF_SERIAL, "SNWRONG-0000"):
                self.assertNotIn(secret, blob)
            self.assertFalse(self.plugin.status_can_upload(status, bad))

    def test_missing_credentials_are_reported_without_a_request(self):
        printer = self.printer()
        before = len(self.ff_cfg["requests"])
        status = self.plugin.poll_printer(dict(printer, apikey=""))
        self.assertEqual(status["error_code"], "credentials_required")
        self.assertEqual(len(self.ff_cfg["requests"]), before)

    def test_upload_through_the_coordinator_is_closed_and_sends_nothing(self):
        printer = self.printer()
        self.stage_gcode()
        self.run_submit(printer, start=False)
        self.assertEqual(self.requests("/uploadGcode"), [])
        self.assertEqual(self.operations(), [])
        status = self.plugin.poll_printer(printer)
        self.assertFalse(self.plugin.status_can_upload(status, printer))
        self.assertFalse(self.plugin.status_can_start(status, printer))
        artifact = self.stage_gcode()
        verdict, _ = self.plugin.start_compatibility(printer, artifact, None)
        self.assertEqual(verdict, "incompatible")
        blob = json.dumps([call.args[0] for call in self.dashboard._post.call_args_list],
                          default=str)
        self.assertNotIn(FF_CHECK, blob)

    def test_the_send_branch_is_wired_to_the_driver_and_the_driver_gate_stays_closed(self):
        printer = self.printer()
        status = self.plugin.poll_printer(printer)
        source = self.root / "part.gcode"
        source.write_bytes(b"G28\n")
        self.assertEqual(self.plugin._pf_flashforge.HARDWARE_VERIFIED, {})
        with mock.patch.object(self.plugin, "status_can_upload", return_value=True), \
                mock.patch.object(self.plugin, "status_can_start", return_value=True), \
                mock.patch.object(self.plugin._pf_flashforge, "upload",
                                  wraps=self.plugin._pf_flashforge.upload) as upload:
            uploaded = self.plugin.send_file_path(printer, "part.gcode", str(source), False,
                                                  status=status)
            started = self.plugin.send_file_path(printer, "part.gcode", str(source), True,
                                                 status=status)
        self.assertEqual(upload.call_count, 2)
        self.assertEqual(upload.call_args.kwargs["serial_number"], FF_SERIAL)
        for result in (uploaded, started):
            self.assertFalse(result["ok"])
            self.assertEqual(result["error_code"], "unsupported_unverified")
            self.assertEqual(result["journal_state"], "rejected")
        self.assertEqual(self.requests("/uploadGcode"), [])
        self.assertEqual(self.requests("/printGcode"), [])

    def test_the_wiring_reaches_the_emulator_once_a_gate_is_opened_in_the_test(self):
        printer = self.printer()
        status = self.plugin.poll_printer(printer)
        source = self.root / "part.gcode"
        source.write_bytes(b"G28\n")
        gate = {0x23: {"upload": True, "start": True}}
        with mock.patch.dict(self.plugin._pf_flashforge.HARDWARE_VERIFIED, gate), \
                mock.patch.object(self.plugin, "status_can_upload", return_value=True):
            result = self.plugin.send_file_path(printer, "part.gcode", str(source), False,
                                                status=status)
        self.assertTrue(result["ok"], result)
        uploads = self.requests("/uploadGcode")
        self.assertEqual(len(uploads), 1)
        self.assertEqual(uploads[0]["headers"]["printNow"], "false")
        self.assertNotIn(FF_CHECK, json.dumps(result))
        self.assertEqual(self.plugin._pf_flashforge.HARDWARE_VERIFIED, {})

    def test_the_tcp_fallback_is_not_used_by_the_plugin(self):
        printer = self.printer()
        with mock.patch.object(self.plugin._pf_flashforge, "poll_tcp") as tcp:
            self.plugin.poll_printer(printer)
            self.reply("lan_mode_off")
            self.plugin.poll_printer(printer)
        tcp.assert_not_called()

    def test_public_status_keeps_only_safe_flashforge_fields(self):
        dirty = {"model": "x" * 200, "model_family": 7, "firmware": "v1",
                 "identity_verified": "yes", "raw_state": None, "printer_error_code": "E1",
                 "serialNumber": FF_SERIAL, "checkCode": FF_CHECK}
        public = self.plugin.public_status(dirty)
        self.assertEqual(len(public["model"]), 64)
        self.assertIsNone(public["model_family"])
        self.assertIs(public["identity_verified"], False)
        self.assertNotIn("printer_error_code", public)
        self.assertNotIn(FF_CHECK, json.dumps(public))


class InterruptedOperationTest(_Base):
    def printer(self):
        return self.add_printer(type="moonraker", url="http://127.0.0.1:9")

    def interrupted(self, printer):
        dispatch = self.dashboard._journal_begin(
            printer, "upload", artifact_sha256="a" * 64, artifact_name="job.gcode")
        self.assertIsNotNone(dispatch)
        dispatch[0].close()                      # the process "dies" mid-send
        self.assertEqual(self.operations()[0]["state"], "dispatching")
        return dispatch[1]

    def reset_recovery(self):
        self.plugin._RECOVERY_DONE.clear()
        self.plugin._acquire_storage_owner = self.plugin._real_acquire_storage_owner

    def test_start_of_the_runtime_recovers_dispatching_as_unknown_without_resending(self):
        printer = self.printer()
        operation_id = self.interrupted(printer)
        self.reset_recovery()
        with mock.patch.object(self.plugin, "configure_plugin_storage", return_value=True), \
                mock.patch.object(self.plugin.ACTION_WORKER, "activate"), \
                mock.patch.object(self.plugin.POLL_WORKERS, "activate"), \
                mock.patch.object(self.plugin, "send_file_path") as send, \
                mock.patch.object(self.plugin, "_PLUGIN_RUNTIME_ACTIVE", False):
            self.assertTrue(self.plugin.start_plugin_runtime())
        send.assert_not_called()
        operation = self.operations()[0]
        self.assertEqual((operation["operation_id"], operation["state"]),
                         (operation_id, "unknown_outcome"))
        # Once per process: a later load must not touch operations of this run.
        self.assertEqual(self.plugin.recover_interrupted_operations(), 0)

    def test_the_recovered_hold_blocks_the_card_says_so_and_can_be_released(self):
        printer = self.printer()
        operation_id = self.interrupted(printer)
        self.reset_recovery()
        self.assertEqual(self.plugin.recover_interrupted_operations(), 1)
        self.assertIsNone(self.dashboard._journal_begin(
            printer, "upload", artifact_sha256="a" * 64, artifact_name="job.gcode"))
        self.assertIn("unresolved", self.dashboard._journal_failure_text())
        self.dashboard._post_print_history_all({"requestId": 1})
        holds = next(call.args[0]["printHistory"]["holds"]
                     for call in self.dashboard._post.call_args_list
                     if "printHistory" in call.args[0])
        self.assertEqual([(h["operation_id"], h["state"]) for h in holds],
                         [(operation_id, "unknown_outcome")])
        self.dashboard._resolve_journal_hold({"operationId": operation_id})
        self.assertEqual(self.operations()[0]["state"], "manually_resolved")
        reopened = self.dashboard._journal_begin(
            printer, "upload", artifact_sha256="a" * 64, artifact_name="job.gcode")
        self.assertIsNotNone(reopened)
        reopened[0].close()

    def test_failed_begin_dispatch_cancels_the_planned_operation(self):
        printer = self.printer()
        real = self.plugin._pf_journal.OperationJournal.begin_dispatch
        with mock.patch.object(self.plugin._pf_journal.OperationJournal, "begin_dispatch",
                               side_effect=OSError("disk")):
            self.assertIsNone(self.dashboard._journal_begin(
                printer, "upload", artifact_sha256="a" * 64, artifact_name="job.gcode"))
        self.assertEqual([o["state"] for o in self.operations()], ["cancelled_before_dispatch"])
        with mock.patch.object(self.plugin._pf_journal.OperationJournal, "begin_dispatch",
                               return_value=False):
            self.assertIsNone(self.dashboard._journal_begin(
                printer, "upload", artifact_sha256="a" * 64, artifact_name="job.gcode"))
        self.assertNotIn("planned", [o["state"] for o in self.operations()])
        again = self.dashboard._journal_begin(
            printer, "upload", artifact_sha256="a" * 64, artifact_name="job.gcode")
        self.assertIsNotNone(again)
        again[0].close()

    def test_runtime_recovery_cancels_a_never_dispatched_plan(self):
        printer = self.printer()
        with mock.patch.object(self.plugin._pf_journal.OperationJournal, "begin_dispatch",
                               side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.dashboard._journal_begin(
                    printer, "upload", artifact_sha256="a" * 64, artifact_name="job.gcode")
        self.assertEqual(self.operations()[0]["state"], "planned")
        self.reset_recovery()
        self.assertEqual(self.plugin.recover_interrupted_operations(), 1)
        self.assertEqual(self.operations()[0]["state"], "cancelled_before_dispatch")
        again = self.dashboard._journal_begin(
            printer, "upload", artifact_sha256="a" * 64, artifact_name="job.gcode")
        self.assertIsNotNone(again)
        again[0].close()

    def test_another_running_instance_prevents_recovery(self):
        printer = self.printer()
        self.interrupted(printer)
        self.reset_recovery()
        other = self.plugin._real_acquire_storage_owner(
            os.path.realpath(self.plugin.PLUGIN_STORAGE_DIR))
        self.assertIsNotNone(other)
        self.closers.append(other.close)
        self.assertEqual(self.plugin.recover_interrupted_operations(), 0)
        self.assertEqual(self.operations()[0]["state"], "dispatching")


if __name__ == "__main__":
    unittest.main()
