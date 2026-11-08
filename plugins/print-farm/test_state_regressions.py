"""Account and destination boundaries must survive update and repeated actions."""
import json
from html.parser import HTMLParser
import pathlib
import tempfile
import types
import unittest
from unittest import mock

from test_printers_plugin import _load_plugin


class StateRegressionTest(unittest.TestCase):
    def setUp(self):
        self.p = _load_plugin(lifecycle=True, pages=True)
        self.addCleanup(self.p._test_storage.cleanup)

    def test_new_host_permission_refusal_is_not_a_certificate_failure(self):
        with mock.patch.object(self.p, "_connect_tcp", side_effect=PermissionError("private details")):
            with self.assertRaises(self.p.BambuTlsError) as raised:
                self.p._probe_bambu_certificate({"url": "http://printer.local"}, "mqtt")
        self.assertEqual(raised.exception.code, "tls_permission_required")

    def test_viewer_template_markers_in_address_never_create_html_attributes(self):
        class Elements(HTMLParser):
            def __init__(self):
                super().__init__()
                self.items = {}
            def handle_starttag(self, tag, attrs):
                if tag in ("input", "iframe"):
                    self.items[tag] = dict(attrs)
        url = "http://printer.local/__COPIED__ onload=alert(1) x=__COPIED__"
        parser = Elements()
        parser.feed(self.p.render_viewer_page(url))
        self.assertEqual(parser.items["input"]["value"], url)
        self.assertEqual(parser.items["iframe"]["src"], url)
        self.assertNotIn("onload", parser.items["iframe"])
        self.assertNotIn("onload", parser.items["input"])

    def test_safe_tls_reason_survives_status_without_exception_text(self):
        for code in ("tls_trust_required", "tls_certificate_changed"):
            status = self.p._finalize_status({"error_code": code, "error": "private details"}, "bambu")
            self.assertEqual(status["error"], self.p.ui_text(self.p._BAMBU_TLS_ERRORS[code]))
        self.assertEqual(self.p._finalize_status({"error": "private details"}, "bambu")["error"],
                         self.p.ui_text("genericError"))

    def test_global_clear_token_never_clears_another_account_or_old_unscoped_request(self):
        p = self.p
        p.record_orca_job(None, "keep", "sent")
        token = p._history_clear_scope() + ":one"
        with tempfile.TemporaryDirectory() as other:
            with mock.patch.object(p, "PLUGIN_STORAGE_DIR", other):
                p._HISTORY_TOKEN_SEEN = None
                with mock.patch.object(p, "save_print_history") as save:
                    self.assertFalse(p.apply_history_clear_request({"clear_history_token": token}))
                    save.assert_not_called()
        self.assertFalse(p.apply_history_clear_request({"clear_history_token": "old-unscoped"}))
        self.assertEqual(len(p.load_print_history()), 1)
        self.assertTrue(p.apply_history_clear_request({"clear_history_token": token}))
        p.record_orca_job(None, "later", "sent")
        p._HISTORY_TOKEN_SEEN = None
        self.assertFalse(p.apply_history_clear_request({"clear_history_token": token}))
        self.assertEqual(len(p.load_print_history()), 1)

    def test_identical_unassigned_bytes_keep_distinct_slice_destinations(self):
        p = self.p
        source = pathlib.Path(p.PLUGIN_STORAGE_DIR) / "source.gcode"
        source.write_text("G28\n", encoding="utf-8")
        with mock.patch.object(p, "dashboard_printers", return_value=[]):
            first = p.outbox_add(str(source), identity={"print_host": "http://one.local"})
            second = p.outbox_add(str(source), identity={"print_host": "http://two.local"})
            repeat = p.outbox_add(str(source), identity={"print_host": "http://one.local"})
        self.assertNotEqual(first["file"], second["file"])
        self.assertEqual(first["file"], repeat["file"])
        self.assertEqual(len(p.load_outbox_index()), 2)

    def test_host_created_empty_storage_imports_nested_files_only_after_consent(self):
        p = self.p
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            legacy, target = root / "legacy", root / "account"
            (legacy / "outbox" / "printer-uuid").mkdir(parents=True)
            (legacy / "outbox" / "unassigned").mkdir()
            (legacy / "outbox" / "printer-uuid" / "one.gcode").write_bytes(b"first")
            (legacy / "outbox" / "unassigned" / "two.gcode").write_bytes(b"second")
            (legacy / "printers.json").write_text('[{"id":1,"url":"http://old.local","type":"moonraker"}]', encoding="utf-8")
            (legacy / "settings_state.json").write_text('{"history_cleared_token":"old"}', encoding="utf-8")
            def storage():
                target.mkdir(exist_ok=True)
                return str(target)
            with mock.patch.object(p, "PLUGIN_DIR", str(legacy)), mock.patch.object(
                    p.orca.host, "plugin", types.SimpleNamespace(storage=storage), create=True), mock.patch.object(
                    p, "start_outbox_pruning"), mock.patch.object(p, "recover_interrupted_operations") as recover:
                p.start_plugin_runtime()
                try:
                    recover.assert_not_called()
                    self.assertEqual(list(target.iterdir()), [])
                    self.assertTrue(p._STORAGE_DENIED)
                    self.assertEqual(p.load_manual_printers(), [])
                    self.assertEqual(p.load_outbox_index(), [])
                    self.assertEqual(p.load_print_history(), [])
                    preview = p.legacy_import_preview()
                    self.assertEqual(preview["outbox_files"], 2)
                    self.assertTrue(p.import_legacy_storage(preview["confirmation"]))
                    self.assertEqual((target / "outbox" / "printer-uuid" / "one.gcode").read_bytes(), b"first")
                    self.assertEqual((target / "outbox" / "unassigned" / "two.gcode").read_bytes(), b"second")
                    self.assertEqual(json.loads((target / "settings_state.json").read_text())["history_cleared_token"], "old")
                    self.assertTrue((legacy / "printers.json").exists())
                    self.assertFalse(p._STORAGE_DENIED)
                    self.assertEqual(p.load_manual_printers()[0]["url"], "http://old.local")
                    self.assertTrue(json.loads((target / "printers.json").read_text())[0]["target_uuid"])
                    recover.assert_called_once()
                finally:
                    p.stop_plugin_runtime()

    def test_scoped_credentials_and_journal_block_partial_import(self):
        p = self.p
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            p._LEGACY_IMPORT_SOURCE = str(root)
            (root / "printers.json").write_text('[{"credential_refs":{"apikey":"ref"}}]', encoding="utf-8")
            self.assertTrue(p.legacy_import_preview()["blocked"])
            (root / "printers.json").write_text("[]", encoding="utf-8")
            (root / "operations.sqlite3").write_bytes(b"journal preserved")
            self.assertTrue(p.legacy_import_preview()["blocked"])
            self.assertEqual((root / "operations.sqlite3").read_bytes(), b"journal preserved")

    def test_existing_native_store_wins_over_two_old_stores(self):
        p = self.p
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            native, fallback, legacy = (root / name for name in ("native", "fallback", "legacy"))
            for folder, name in ((native, "current"), (fallback, "old-one"), (legacy, "old-two")):
                folder.mkdir()
                (folder / "printers.json").write_text(json.dumps([{"name": name}]), encoding="utf-8")
            with mock.patch.object(p, "PLUGIN_DIR", str(legacy)), mock.patch.object(
                    p, "fallback_plugin_storage_dir", return_value=str(fallback)), mock.patch.object(
                    p.orca.host, "plugin", types.SimpleNamespace(storage=lambda: str(native)), create=True):
                self.assertTrue(p.configure_plugin_storage())
                self.assertFalse(p._STORAGE_DENIED)
                self.assertFalse(p._LEGACY_IMPORT_SOURCE)
                self.assertFalse(p._LEGACY_IMPORT_BLOCKED)
                self.assertEqual(p.load_manual_printers()[0]["name"], "current")
                self.assertEqual(json.loads((fallback / "printers.json").read_text())[0]["name"], "old-one")
