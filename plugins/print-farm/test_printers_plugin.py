import ast
import importlib.util
import csv
import hashlib
import http.server
import io
import inspect
import json
import os
import pathlib
import re
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import types
import unittest
import urllib.error
import zipfile
from unittest import mock


PLUGIN_PATH = pathlib.Path(__file__).with_name("printers_plugin.py")
BUILD_PATH = pathlib.Path(__file__).with_name("build_package.py")
LOCALE_VALIDATOR_PATH = pathlib.Path(__file__).with_name("validate_locales.py")


class _ExecutionResult:
    @staticmethod
    def success(message):
        return ("success", message)

    @staticmethod
    def skipped(message):
        return ("skipped", message)

    @staticmethod
    def failure(kind, message):
        return ("failure", kind, message)


class _MemoryCredentials:
    def __init__(self):
        self.values = {}

    def set_password(self, service, user, value):
        self.values[service, user] = value

    def get_password(self, service, user):
        return self.values.get((service, user))


def _load_plugin(*, lifecycle=False, pages=False, registered=None,
                 initialize_test_storage=True):
    def capability_base(name, *, page=False):
        namespace = {}
        if lifecycle:
            namespace.update({
                "on_load": lambda self: None,
                "on_unload": lambda self: None,
            })
        if page:
            namespace["post_message"] = lambda self, data: None
        return type(name, (object,), namespace) if namespace else object

    fake_orca = types.ModuleType("orca")
    fake_orca.base = object
    fake_orca.plugin = lambda cls: cls
    fake_orca.register_capability = (
        registered.append if registered is not None else lambda capability: None
    )
    fake_orca.ExecutionResult = _ExecutionResult
    fake_orca.PluginResult = types.SimpleNamespace(RecoverableError="recoverable")
    fake_orca.script = types.SimpleNamespace(
        ScriptPluginCapabilityBase=capability_base("ScriptCapabilityBase")
    )
    if pages:
        fake_orca.pages = types.SimpleNamespace(
            PagesPluginCapabilityBase=capability_base(
                "PagesCapabilityBase",
                page=True,
            )
        )
    fake_orca.host = types.SimpleNamespace(ui=types.SimpleNamespace())
    fake_orca.slicing = types.SimpleNamespace(
        SlicingPipelineCapabilityBase=capability_base("SlicingCapabilityBase"),
        Step=types.SimpleNamespace(psGCodePostProcess="post-process"),
    )
    sys.modules["orca"] = fake_orca

    module_name = "printers_plugin_under_test_%s" % id(fake_orca)
    spec = importlib.util.spec_from_file_location(module_name, PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # The real OS owner lock keeps a file open for the whole process, which
    # would block temp-directory cleanup. Tests that exercise recovery opt in.
    module._real_acquire_storage_owner = module._acquire_storage_owner
    module._acquire_storage_owner = lambda root: None
    # Tests never reach the operating-system credential store: a developer
    # machine would collect synthetic printer keys and CI runners have none.
    credentials = _MemoryCredentials()
    module._CREDENTIAL_VAULT = module._pf_credentials.Vault(lambda: credentials)
    if initialize_test_storage:
        test_storage = tempfile.TemporaryDirectory(prefix="print-farm-test-storage-")
        module._test_storage = test_storage
        root = pathlib.Path(test_storage.name)
        module.PLUGIN_STORAGE_DIR = str(root)
        module.PRINTERS_FILE = str(root / "printers.json")
        module.PRINT_HISTORY_FILE = str(root / "print_history.json")
        module.OUTBOX_DIR = str(root / "outbox")
        module.OUTBOX_INDEX = str(root / "outbox_index.json")
        module._STORAGE_DENIED = False
    return module


def _trusted_bambu_card(testcase, plugin, card, certificate=b"synthetic Bambu TLS certificate"):
    storage = tempfile.TemporaryDirectory(prefix="print-farm-bambu-tls-test-")
    testcase.addCleanup(storage.cleanup)
    patcher = mock.patch.object(plugin, "PRINTERS_FILE",
                               str(pathlib.Path(storage.name) / "printers.json"))
    patcher.start()
    testcase.addCleanup(patcher.stop)
    printer = dict(card, type="bambu")
    plugin._ensure_printer_identity(printer)
    digest = hashlib.sha256(certificate).hexdigest()
    printer["tls_pins"] = {"mqtt": digest, "ftps": digest}
    printer["tls_trust_revision"] = 1
    if not plugin.save_manual_printers([printer]):
        raise AssertionError("test fixture could not save trusted Bambu record")
    return printer


def _fresh_status(plugin, backend, state, job_state=None):
    return plugin._finalize_status({
        "online": True,
        "state": state,
        "job_state": job_state,
    }, backend)


class PrintersOutboxTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin = _load_plugin()

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.temp_dir.name)
        self.plugin.OUTBOX_DIR = str(root / "outbox")
        self.plugin.OUTBOX_INDEX = str(root / "outbox_index.json")
        self.plugin.PLUGIN_STORAGE_DIR = str(root)
        self.gcode = root / "fresh.gcode"
        self.gcode.write_text(
            "; printer_settings_id = Voron 2.4 350\n"
            "; printer_model = Voron 2.4\n"
            "G28\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def execute(self, host):
        ctx = types.SimpleNamespace(
            step="post-process",
            gcode_path=str(self.gcode),
            output_name="fresh.gcode",
            host=host,
        )
        return self.plugin.PrintersOutbox().execute(ctx)

    def test_file_target_is_queued(self):
        result = self.execute("File")

        self.assertEqual(result[0], "success")
        record = self.plugin.load_outbox_index()[0]
        self.assertFalse(record["host_handled"])
        self.assertEqual(record["host"], "File")

    def test_execute_keeps_gcode_bytes_and_index_hash(self):
        source = (b"; printer_settings_id = Voron 2.4 350\r\n"
                  b"; printer_model = Voron 2.4\r\nG28\n\xff\x00\r\n")
        self.gcode.write_bytes(source)
        self.assertEqual(self.execute("File")[0], "success")
        self.assertEqual(self.gcode.read_bytes(), source)
        record = self.plugin.load_outbox_index()[0]
        retained = pathlib.Path(self.plugin._safe_outbox_path(record["file"]))
        self.assertEqual(retained.read_bytes(), source)
        self.assertEqual(record["sha256"], hashlib.sha256(source).hexdigest())

    def test_slicer_temp_working_name_is_replaced_by_readable_name(self):
        temp = pathlib.Path(self.temp_dir.name) / ".48540.0.gcode"
        temp.write_text("; printer_settings_id = Voron 2.4 350\nG28\n", encoding="utf-8")
        for output_name in ("", ".48540.0.gcode", str(temp), "C:\\Users\\x\\AppData\\.48540.0.gcode"):
            record = self.plugin.outbox_add(str(temp), output_name)
            self.assertFalse(record["name"].startswith("."), record["name"])
            self.assertRegex(record["name"], r"^[^<>:\"/\\|?*]+ \d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2}\.gcode$")
            self.assertTrue(os.path.basename(record["file"]).split("__", 1)[1] == record["name"])

    def test_real_export_name_is_kept(self):
        record = self.plugin.outbox_add(str(self.gcode), "C:\\models\\Benchy_0.2mm.gcode")
        self.assertEqual(record["name"], "Benchy_0.2mm.gcode")
        self.gcode.write_text("; printer_settings_id = Other\nG1 X1\n", encoding="utf-8")
        self.assertEqual(self.plugin.outbox_add(str(self.gcode))["name"], "fresh.gcode")

    def test_outbox_remove_rejects_path_traversal(self):
        outside = pathlib.Path(self.temp_dir.name) / "outside.gcode"
        outside.write_text("keep", encoding="utf-8")

        self.assertFalse(self.plugin.outbox_remove("../outside.gcode"))
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep")

    def test_outbox_remove_failure_keeps_file_and_index_for_retry(self):
        result = self.execute("File")
        self.assertEqual(result[0], "success")
        record = self.plugin.load_outbox_index()[0]
        path = pathlib.Path(self.plugin._safe_outbox_path(record["file"]))
        original_index = self.plugin.load_outbox_index()
        with mock.patch.object(self.plugin.os, "remove", side_effect=PermissionError("locked")):
            self.assertFalse(self.plugin.outbox_remove(record["file"]))
        self.assertEqual(self.plugin.load_outbox_index(), original_index)
        self.assertTrue(path.exists())

    def test_physical_usage_counts_orphans_recovery_and_backups_without_index(self):
        root = pathlib.Path(self.temp_dir.name)
        target_uuid = str(self.plugin.uuid.uuid4())
        printer_dir = pathlib.Path(self.plugin.OUTBOX_DIR) / target_uuid
        printer_dir.mkdir(parents=True)
        (printer_dir / "orphan.gcode").write_bytes(b"12345")
        unassigned = pathlib.Path(self.plugin.OUTBOX_DIR) / "unassigned"
        unassigned.mkdir()
        (unassigned / "unindexed.gcode").write_bytes(b"1234567")
        recovery = root / "outbox-legacy-recovery"
        recovery.mkdir()
        (recovery / "old.gcode").write_bytes(b"123456789")
        pathlib.Path(self.plugin.OUTBOX_INDEX + ".layout-backup").write_bytes(b"backup")
        printer = {"target_uuid": target_uuid, "name": "Local Printer"}

        usage = self.plugin.outbox_physical_usage([printer])

        self.assertTrue(usage["ok"])
        groups = {group["path"]: group for group in usage["groups"]}
        self.assertEqual(groups["outbox/" + target_uuid]["label"], "Local Printer")
        self.assertEqual((groups["outbox/" + target_uuid]["count"],
                          groups["outbox/" + target_uuid]["size"]), (1, 5))
        self.assertEqual((groups["outbox/unassigned"]["count"],
                          groups["outbox/unassigned"]["size"]), (1, 7))
        self.assertEqual((groups["outbox-legacy-recovery"]["count"],
                          groups["outbox-legacy-recovery"]["size"]), (1, 9))
        self.assertEqual((groups["outbox_index.json.layout-backup"]["count"],
                          groups["outbox_index.json.layout-backup"]["size"]), (1, 6))

    def test_physical_usage_reports_scan_failure_instead_of_partial_success(self):
        with mock.patch.object(self.plugin.os, "scandir", side_effect=PermissionError("denied")):
            usage = self.plugin.outbox_physical_usage([])
        self.assertFalse(usage["ok"])
        self.assertTrue(usage["errors"])

    def test_file_target_is_case_insensitive(self):
        result = self.execute(" file ")

        self.assertEqual(result[0], "success")
        self.assertEqual(len(self.plugin.load_outbox_index()), 1)

    def _ctx(self, config=None, **extra):
        asked = []

        def config_value(key):
            asked.append(key)
            value = (config or {}).get(key)
            if isinstance(value, Exception):
                raise value
            return value

        ctx = types.SimpleNamespace(
            step="post-process", gcode_path=str(self.gcode),
            output_name="fresh.gcode", host="File", **extra)
        if config is not None:
            ctx.config_value = config_value
        return ctx, asked

    def test_storage_failure_skips_instead_of_aborting_export(self):
        ctx, _ = self._ctx()
        with mock.patch.object(self.plugin, "outbox_add", side_effect=OSError("disk full")):
            result = self.plugin.PrintersOutbox().execute(ctx)
        self.assertEqual(result[0], "skipped")
        self.assertIn("disk full", result[1])
        self.plugin._STORAGE_DENIED, denied = True, self.plugin._STORAGE_DENIED
        try:
            self.assertEqual(self.plugin.PrintersOutbox().execute(ctx)[0], "skipped")
        finally:
            self.plugin._STORAGE_DENIED = denied

    def test_slice_identity_prefers_context_config_without_secrets(self):
        ctx, asked = self._ctx({
            "printer_settings_id": "Preset From Config", "printer_model": "M1",
            "print_host": "192.168.1.5:7125", "printhost_apikey": "TOP-SECRET",
        })
        self.assertEqual(self.plugin.PrintersOutbox().execute(ctx)[0], "success")
        record = self.plugin.load_outbox_index()[0]
        self.assertEqual(record["printer_settings_id"], "Preset From Config")
        self.assertEqual(record["match_endpoint"], "endpoint:http://192.168.1.5:7125")
        self.assertNotIn("printhost_apikey", asked)
        self.assertNotIn("apikey", asked)
        self.assertNotIn("TOP-SECRET", json.dumps(self.plugin.load_outbox_index()))
        with open(self.plugin.OUTBOX_INDEX, encoding="utf-8") as fh:
            self.assertNotIn("TOP-SECRET", fh.read())

    def test_slice_identity_falls_back_to_gcode_when_config_missing_or_failing(self):
        for config in (None, {"printer_settings_id": RuntimeError("boom"),
                              "printer_model": RuntimeError("boom")}):
            ctx, _ = self._ctx(config)
            self.assertEqual(self.plugin.PrintersOutbox().execute(ctx)[0], "success")
            record = self.plugin.load_outbox_index()[-1]
            self.assertEqual(record["printer_settings_id"], "Voron 2.4 350")
            self.assertNotIn("match_endpoint", record)

    def test_address_and_serial_match_cards_only_when_unique(self):
        one = {"id": 1, "name": "A", "url": "http://192.168.1.5:7125"}
        two = {"id": 2, "name": "B", "url": "http://192.168.1.6"}
        ff = {"id": 3, "name": "FF", "url": "http://10.0.0.9", "serial": "SN123"}
        match = self.plugin.outbox_target_match
        by_addr = {"match_endpoint": "endpoint:http://192.168.1.5:7125",
                   "printer_settings_id": "Unrelated"}
        self.assertIs(match(by_addr, [one, two, ff]), one)
        self.assertIs(match({"match_serial": "sn123"}, [one, two, ff]), ff)
        # Ambiguous address: unassigned, even though the name would match.
        dup = dict(one, id=9, name="Unrelated")
        self.assertIsNone(match(by_addr, [one, dup]))
        # No address match falls through to the existing name match.
        self.assertIs(match({"match_endpoint": "endpoint:http://1.1.1.1",
                             "printer_settings_id": "B"}, [one, two]), two)
        # "File"/empty hosts never create an address.
        self.assertEqual(self.plugin._slice_host_endpoint("File"), "")
        self.assertEqual(self.plugin._slice_host_endpoint(""), "")
        self.assertIsNone(match({"match_endpoint": "", "printer_settings_id": ""}, [one]))

    def test_slice_with_print_host_is_assigned_to_card_by_address(self):
        printer = self.plugin._ensure_printer_identity({
            "id": 1, "name": "Renamed card", "url": "http://192.168.1.5:7125"})
        ctx, _ = self._ctx({"print_host": "192.168.1.5:7125",
                            "printer_settings_id": "Preset name differs"})
        with mock.patch.object(self.plugin, "dashboard_printers", return_value=[printer]):
            self.assertEqual(self.plugin.PrintersOutbox().execute(ctx)[0], "success")
        self.assertEqual(self.plugin.load_outbox_index()[0]["target_uuid"],
                         printer["target_uuid"])

    def test_identical_file_for_same_target_is_stored_once(self):
        first = self.plugin.outbox_add(str(self.gcode))
        for _ in range(2):
            again = self.plugin.outbox_add(str(self.gcode))
        index = self.plugin.load_outbox_index()
        self.assertEqual(len(index), 1)
        self.assertEqual(again["file"], first["file"])
        self.assertEqual(len(list(pathlib.Path(self.plugin.OUTBOX_DIR).rglob("*.gcode"))), 1)
        # Same bytes for another target, or other bytes, are separate entries.
        printer = self.plugin._ensure_printer_identity({
            "id": 1, "name": "Voron 2.4 350", "url": "http://voron.local"})
        with mock.patch.object(self.plugin, "dashboard_printers", return_value=[printer]):
            self.plugin.outbox_add(str(self.gcode))
        self.gcode.write_text("; printer_settings_id = X\nG1\n", encoding="utf-8")
        self.plugin.outbox_add(str(self.gcode))
        self.assertEqual(len(self.plugin.load_outbox_index()), 3)

    def test_outbox_records_exact_unique_target_identity(self):
        printer = self.plugin._ensure_printer_identity({
            "id": 1, "name": "Voron 2.4 350", "url": "http://voron.local",
        })
        with mock.patch.object(self.plugin, "dashboard_printers", return_value=[printer]):
            record = self.plugin.outbox_add(str(self.gcode))

        self.assertEqual(record["target_uuid"], printer["target_uuid"])
        self.assertEqual(record["binding_revision"], printer["binding_revision"])
        self.assertEqual(record["file"].split("/", 1)[0], printer["target_uuid"])

    def test_same_url_printers_store_in_separate_uuid_directories(self):
        one = self.plugin._ensure_printer_identity({
            "id": 1, "name": "Voron 2.4 350", "url": "http://same.local",
        })
        two = self.plugin._ensure_printer_identity({
            "id": 2, "name": "Voron 2.4 350B", "url": "http://same.local",
        })
        with mock.patch.object(self.plugin, "dashboard_printers", return_value=[one]):
            first = self.plugin.outbox_add(str(self.gcode))
        self.gcode.write_text("; printer_settings_id = Voron 2.4 350B\nG1 X2\n", encoding="utf-8")
        with mock.patch.object(self.plugin, "dashboard_printers", return_value=[two]):
            second = self.plugin.outbox_add(str(self.gcode))
        self.assertEqual(first["file"].split("/")[0], one["target_uuid"])
        self.assertEqual(second["file"].split("/")[0], two["target_uuid"])
        self.assertNotEqual(first["file"].split("/")[0], second["file"].split("/")[0])

    def test_printer_rename_keeps_uuid_folder(self):
        printer = self.plugin._ensure_printer_identity({
            "id": 1, "name": "Voron 2.4 350", "url": "http://voron.local",
        })
        folder = self.plugin._outbox_folder(printer["target_uuid"])
        renamed = dict(printer, name="Voron renamed")
        self.assertEqual(self.plugin._outbox_folder(renamed["target_uuid"]), folder)

    def test_ambiguous_record_is_copied_to_unassigned_during_legacy_migration(self):
        root = pathlib.Path(self.temp_dir.name)
        legacy = root / "outbox" / "legacy.gcode"
        legacy.parent.mkdir()
        legacy.write_bytes(b"legacy bytes")
        pathlib.Path(self.plugin.OUTBOX_INDEX).write_text(json.dumps([{
            "file": "legacy.gcode", "name": "legacy.gcode",
            "printer_settings_id": "Voron 2.4 350", "size": 12,
        }]), encoding="utf-8")
        one = {"name": "Voron 2.4 350"}
        two = {"name": "Voron 2.4 350"}
        self.plugin.attach_outbox([one, two])
        record = self.plugin.load_outbox_index()[0]
        self.assertTrue(record["file"].startswith("unassigned/"))
        self.assertEqual((root / "outbox" / record["file"]).read_bytes(), b"legacy bytes")
        self.assertFalse(legacy.exists())
        self.assertEqual((root / "outbox-legacy-recovery" / next(
            item.name for item in (root / "outbox-legacy-recovery").iterdir()
        )).read_bytes(), b"legacy bytes")

    def test_legacy_migration_failure_keeps_flat_index_and_retry_succeeds(self):
        root = pathlib.Path(self.temp_dir.name)
        source = root / "outbox" / "legacy.gcode"
        source.parent.mkdir()
        source.write_bytes(b"recoverable")
        index_path = pathlib.Path(self.plugin.OUTBOX_INDEX)
        original_index = [{"file": "legacy.gcode", "name": "legacy.gcode", "size": 11}]
        index_path.write_text(json.dumps(original_index), encoding="utf-8")
        with mock.patch.object(self.plugin, "_copy_exclusive", side_effect=OSError("simulated crash")):
            self.plugin.attach_outbox([])
        self.assertEqual(self.plugin.load_outbox_index(), original_index)
        self.assertEqual(source.read_bytes(), b"recoverable")
        self.plugin.attach_outbox([])
        self.assertEqual(self.plugin.load_outbox_index()[0]["file"].split("/")[0], "unassigned")
        self.assertFalse(source.exists())
        self.assertIn(b"recoverable", [p.read_bytes() for p in (root / "outbox-legacy-recovery").iterdir()])

    def test_outbox_path_rejects_traversal_and_symlink(self):
        root = pathlib.Path(self.temp_dir.name)
        self.assertIsNone(self.plugin._safe_outbox_path("../outside"))
        link = pathlib.Path(self.plugin.OUTBOX_DIR) / "unassigned"
        link.parent.mkdir(parents=True, exist_ok=True)
        real_islink = self.plugin.os.path.islink
        def is_symlink(path):
            return os.path.abspath(path) == os.path.abspath(link) or real_islink(path)
        with mock.patch.object(self.plugin.os.path, "islink", side_effect=is_symlink):
            self.assertIsNone(self.plugin._safe_outbox_path("unassigned/escape.gcode"))

    def test_outbox_usage_counts_bytes_and_never_auto_deletes(self):
        self.execute("File")
        printer = {"name": "Voron 2.4 350"}
        self.plugin.attach_outbox([printer])
        self.assertEqual(printer["outbox_count"], 1)
        self.assertEqual(printer["outbox_bytes"], self.gcode.stat().st_size)
        self.assertEqual(len(self.plugin.load_outbox_index()), 1)
        source = self.plugin._safe_outbox_path(self.plugin.load_outbox_index()[0]["file"])
        self.assertTrue(os.path.isfile(source))
        self.assertIn("outboxStorageWarning", self.plugin.PAGE)

    def test_ambiguous_outbox_preset_name_stays_unassigned(self):
        one = self.plugin._ensure_printer_identity({
            "id": 1, "name": "Voron 2.4 350", "url": "http://one.local",
        })
        two = self.plugin._ensure_printer_identity({
            "id": 2, "name": "Voron 2.4 350", "url": "http://two.local",
        })
        with mock.patch.object(self.plugin, "dashboard_printers", return_value=[one, two]):
            record = self.plugin.outbox_add(str(self.gcode))

        self.assertNotIn("target_uuid", record)

    def test_legacy_empty_target_is_queued(self):
        result = self.execute("")

        self.assertEqual(result[0], "success")
        self.assertEqual(len(self.plugin.load_outbox_index()), 1)

    def test_network_upload_is_also_queued(self):
        result = self.execute("OctoPrint")

        self.assertEqual(result[0], "success")
        record = self.plugin.load_outbox_index()[0]
        self.assertTrue(record["host_handled"])
        self.assertEqual(record["host"], "OctoPrint")

    def test_outbox_uses_only_unique_exact_preset_name(self):
        self.execute("File")
        matching = {"name": "Voron 2.4 350"}
        similar = {"name": "Voron 2.4 350 backup"}
        printers = [similar, matching]

        self.assertEqual(self.plugin.attach_outbox(printers), 0)
        self.assertEqual(len(matching["queued"]), 1)
        self.assertEqual(similar["queued"], [])

        duplicate = {"name": "Voron 2.4 350"}
        self.assertEqual(self.plugin.attach_outbox([matching, duplicate]), 1)
        self.assertEqual(matching["queued"], [])
        self.assertEqual(duplicate["queued"], [])

    def test_outbox_does_not_guess_from_similar_name(self):
        self.execute("File")
        printers = [{"name": "My Voron 2.4 350"}]

        self.assertEqual(self.plugin.attach_outbox(printers), 1)
        self.assertEqual(printers[0]["queued"], [])

    def test_damaged_outbox_index_is_preserved(self):
        index = pathlib.Path(self.plugin.OUTBOX_INDEX)
        index.write_text('[{"file":"existing.gcode"},null]', encoding="utf-8")

        result = self.execute("File")

        self.assertEqual(result[0], "skipped")
        self.assertEqual(index.read_text(encoding="utf-8"),
                         '[{"file":"existing.gcode"},null]')
        self.assertEqual(list(pathlib.Path(self.plugin.OUTBOX_DIR).glob("*")), [])

    def test_hub_version_matches_wheel_project(self):
        source = PLUGIN_PATH.read_text(encoding="utf-8")
        match = re.search(r'^# version = "([0-9]+\.[0-9]+\.[0-9]+)"$', source, re.MULTILINE)
        self.assertIsNotNone(match)
        project = tomllib.loads(PLUGIN_PATH.with_name("pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(project["project"]["dynamic"], ["version"])
        self.assertEqual(
            project["tool"]["setuptools"]["dynamic"]["version"],
            {"attr": "printers_plugin.PLUGIN_VERSION"},
        )
        self.assertEqual(project["tool"]["setuptools"]["py-modules"], ["printers_plugin"])
        self.assertNotIn("packages", project["tool"]["setuptools"])
        self.assertEqual(match.group(1), self.plugin.PLUGIN_VERSION)
        self.assertIn('# name = "Print Farm"', source)
        self.assertEqual(self.plugin.PrintersDashboard().get_name(), "Print Farm")

    def test_package_builder_stages_source_and_icon(self):
        spec = importlib.util.spec_from_file_location("printers_build_under_test", BUILD_PATH)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)

        with tempfile.TemporaryDirectory() as temp:
            package = builder.build(pathlib.Path(temp), wheel=False)

            self.assertEqual(package.name, f"printers-{self.plugin.PLUGIN_VERSION}")
            staged_source = (package / "printers_plugin.py").read_text(encoding="utf-8")
            self.assertNotIn(builder.EMBEDDED_UI_COPY_TOKEN, staged_source)
            self.assertIn('_EMBEDDED_UI_COPY = {"ca":', staged_source)
            ast.parse(staged_source)
            self.assertEqual(
                (package / "printers.svg").read_bytes(),
                PLUGIN_PATH.with_name("printers.svg").read_bytes(),
            )
            self.assertEqual(
                (package / "description.md").read_bytes(),
                PLUGIN_PATH.with_name("description.md").read_bytes(),
            )
            self.assertEqual(
                {path.name for path in (package / "printers_locales").glob("*.json")},
                {"%s.json" % locale for locale in self.plugin.ORCA_UI_LOCALES},
            )
            metadata = json.loads(
                (package / "package-metadata.json").read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["locales"], sorted(self.plugin.ORCA_UI_LOCALES))

    def test_wheel_is_reproducible_and_covers_exact_package(self):
        spec = importlib.util.spec_from_file_location("printers_build_under_test", BUILD_PATH)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)

        with tempfile.TemporaryDirectory() as temp:
            first_root = pathlib.Path(temp) / "first"
            second_root = pathlib.Path(temp) / "second"
            builder.build(first_root)
            builder.build(second_root)
            wheel = first_root / "wheels" / (
                f"printers-{self.plugin.PLUGIN_VERSION}-py3-none-any.whl"
            )
            second_wheel = second_root / "wheels" / wheel.name
            metadata_path = (
                f"printers-{self.plugin.PLUGIN_VERSION}.dist-info/METADATA"
            )
            wheel_metadata_path = (
                f"printers-{self.plugin.PLUGIN_VERSION}.dist-info/WHEEL"
            )
            record_path = (
                f"printers-{self.plugin.PLUGIN_VERSION}.dist-info/RECORD"
            )

            with zipfile.ZipFile(wheel) as archive:
                normalized = archive.read(metadata_path)
                wheel_metadata = archive.read(wheel_metadata_path)
                rows = list(csv.reader(io.StringIO(
                    archive.read(record_path).decode("utf-8"),
                    newline="",
                )))
                names = set(archive.namelist())
                compression_types = {
                    entry.compress_type for entry in archive.infolist()
                }
                timestamps = {entry.date_time for entry in archive.infolist()}
            self.assertNotIn(b"\r", normalized)
            self.assertIn(
                b"Generator: print-farm build_package.py\n", wheel_metadata
            )
            self.assertEqual(compression_types, {zipfile.ZIP_STORED})
            self.assertEqual(timestamps, {(1980, 1, 1, 0, 0, 0)})
            self.assertEqual({row[0] for row in rows}, names)
            self.assertEqual(second_wheel.read_bytes(), wheel.read_bytes())

    def test_default_output_and_release_metadata_are_versioned(self):
        spec = importlib.util.spec_from_file_location("printers_build_under_test", BUILD_PATH)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)

        self.assertEqual(
            builder.default_release_output_root(),
            BUILD_PATH.parent / "dist" / f"release-{self.plugin.PLUGIN_VERSION}",
        )
        with tempfile.TemporaryDirectory() as temp:
            output_root = pathlib.Path(temp) / f"release-{self.plugin.PLUGIN_VERSION}"
            wheel = output_root / "wheels" / (
                f"printers-{self.plugin.PLUGIN_VERSION}-py3-none-any.whl"
            )
            wheel.parent.mkdir(parents=True)
            wheel.write_bytes(b"owner-test-candidate")
            notes, checksums = builder.stage_release_metadata(
                output_root, wheel, self.plugin.PLUGIN_VERSION
            )
            self.assertIn(
                f"## Print Farm {self.plugin.PLUGIN_VERSION}",
                notes.read_text(encoding="utf-8"),
            )
            digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
            self.assertEqual(
                checksums.read_text(encoding="utf-8"),
                f"{digest}  wheels/{wheel.name}\n",
            )

    def test_release_notes_use_only_current_changelog_section(self):
        spec = importlib.util.spec_from_file_location("printers_build_under_test", BUILD_PATH)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)

        current_version = self.plugin.PLUGIN_VERSION
        previous_version = "9.8.7" if current_version != "9.8.7" else "9.8.6"
        with tempfile.TemporaryDirectory() as temp:
            changelog = pathlib.Path(temp) / "CHANGELOG.md"
            changelog.write_text(
                f"## {current_version}\n\nCurrent release marker.\n\n"
                f"## {previous_version}\n\nPrevious release marker.\n",
                encoding="utf-8",
            )
            with mock.patch.object(builder, "CHANGELOG", changelog):
                notes = builder.render_release_notes(current_version)

        self.assertIn(f"## Print Farm {current_version}", notes)
        self.assertIn("Current release marker.", notes)
        self.assertNotIn("Previous release marker.", notes)


class PluginStorageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin = _load_plugin()

    def test_source_directory_is_not_writable_storage_before_host_initialization(self):
        plugin = _load_plugin(initialize_test_storage=False)
        self.assertTrue(plugin._STORAGE_DENIED)
        self.assertFalse(plugin.save_manual_printers([]))
        self.assertFalse(plugin.save_print_history([]))
        self.assertEqual(plugin.load_outbox_index(), [])
        with tempfile.TemporaryDirectory() as temp:
            missing = str(pathlib.Path(temp) / "missing.svg")
            with mock.patch.object(plugin, "PACKAGED_ICON_PATH", missing), \
                 mock.patch.object(plugin, "ICON_PATH", missing), \
                 mock.patch.object(plugin, "write_bytes_atomic") as write_icon:
                self.assertEqual(plugin.ensure_icon(), "")
                write_icon.assert_not_called()

    def test_corrupt_printer_store_cannot_be_overwritten(self):
        plugin = _load_plugin()
        with tempfile.TemporaryDirectory() as temp:
            store = pathlib.Path(temp, "printers.json")
            store.write_text('{"broken": true}', encoding="utf-8")
            with mock.patch.object(plugin, "PRINTERS_FILE", str(store)):
                self.assertEqual(plugin.load_manual_printers(), [])
                self.assertFalse(plugin.save_manual_printers([{"id": 1}]))
            self.assertEqual(store.read_text(encoding="utf-8"), '{"broken": true}')

    def test_partial_printer_store_cannot_be_overwritten(self):
        plugin = _load_plugin()
        with tempfile.TemporaryDirectory() as temp:
            store = pathlib.Path(temp, "printers.json")
            original = '[{"id":1,"url":"http://printer"},null]'
            store.write_text(original, encoding="utf-8")
            with mock.patch.object(plugin, "PRINTERS_FILE", str(store)):
                self.assertEqual(plugin.load_manual_printers(), [])
                self.assertFalse(plugin.save_manual_printers([]))
            self.assertEqual(store.read_text(encoding="utf-8"), original)

    def test_denied_host_storage_does_not_use_global_fallback(self):
        plugin = _load_plugin()
        host = types.SimpleNamespace(storage=mock.Mock(side_effect=PermissionError()))
        with mock.patch.object(plugin.orca.host, "plugin", host, create=True), \
                mock.patch.object(plugin, "fallback_plugin_storage_dir",
                                  return_value="F:/shared-print-farm"):
            self.assertFalse(plugin.configure_plugin_storage())

    def test_native_storage_signout_fails_closed_but_missing_api_allows_fallback(self):
        plugin = _load_plugin()
        with mock.patch.object(plugin.orca.host, "plugin", types.SimpleNamespace(
                storage=lambda: None), create=True):
            self.assertFalse(plugin.configure_plugin_storage())
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(plugin, "PLUGIN_DIR", str(pathlib.Path(temp) / "orca_plugins" / "printers")), \
                    mock.patch.object(plugin, "fallback_plugin_storage_dir",
                                      return_value=str(pathlib.Path(temp) / "fallback")), \
                    mock.patch.object(plugin.orca.host, "plugin", types.SimpleNamespace(), create=True):
                self.assertTrue(plugin.configure_plugin_storage())

    def test_denied_storage_blocks_previous_account_state_and_workers(self):
        plugin = _load_plugin(lifecycle=True)
        host = types.SimpleNamespace(storage=mock.Mock(side_effect=PermissionError()))
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            old_store = root / "printers.json"
            old_store.write_text('[{"id":1,"name":"Old account"}]', encoding="utf-8")
            old_index = root / "outbox_index.json"
            old_index.write_text('[{"file":"old.gcode"}]', encoding="utf-8")
            gcode = root / "new.gcode"
            gcode.write_text("G28", encoding="utf-8")
            with mock.patch.object(plugin.orca.host, "plugin", host, create=True), \
                 mock.patch.object(plugin, "PRINTERS_FILE", str(old_store)), \
                 mock.patch.object(plugin, "OUTBOX_INDEX", str(old_index)), \
                 mock.patch.object(plugin, "OUTBOX_DIR", str(root / "outbox")):
                self.assertFalse(plugin.start_plugin_runtime())
                self.assertFalse(plugin._PLUGIN_RUNTIME_ACTIVE)
                self.assertEqual(plugin.load_manual_printers(), [])
                self.assertFalse(plugin.save_manual_printers([]))
                self.assertEqual(plugin.load_outbox_index(), [])
                with self.assertRaises(OSError):
                    plugin.outbox_add(str(gcode))
                with self.assertRaises(RuntimeError):
                    plugin.ACTION_WORKER.submit(lambda: None).wait(1)
            self.assertEqual(old_store.read_text(encoding="utf-8"),
                             '[{"id":1,"name":"Old account"}]')
            self.assertEqual(old_index.read_text(encoding="utf-8"),
                             '[{"file":"old.gcode"}]')

    def test_host_storage_never_auto_imports_and_requires_validated_confirmation(self):
        plugin = _load_plugin(lifecycle=True, pages=True)
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            legacy = root / "orca_plugins" / "printers"
            storage = root / "plugin_data" / "printers"
            legacy.mkdir(parents=True)
            (legacy / "printers.json").write_text(
                '[{"id":1,"apikey":"  Sëcret  "}]', encoding="utf-8"
            )
            (legacy / "outbox_index.json").write_text(
                '[{"file":"fixture.gcode"}]', encoding="utf-8"
            )
            (legacy / "print_history.json").write_text(
                '[{"id":"history-1","started_at":1}]', encoding="utf-8"
            )
            (legacy / "outbox").mkdir()
            (legacy / "outbox" / "fixture.gcode").write_text("G28", encoding="utf-8")

            in_callback = False

            def host_storage():
                if not in_callback:
                    raise RuntimeError("storage requires a plugin callback")
                return str(storage)

            plugin_host = types.SimpleNamespace(storage=mock.Mock(side_effect=host_storage))
            with mock.patch.object(plugin, "PLUGIN_DIR", str(legacy)), \
                 mock.patch.object(plugin, "PLUGIN_STORAGE_DIR", str(legacy)), \
                 mock.patch.object(plugin, "PRINTERS_FILE", str(legacy / "printers.json")), \
                 mock.patch.object(plugin, "PRINT_HISTORY_FILE", str(legacy / "print_history.json")), \
                 mock.patch.object(plugin, "OUTBOX_DIR", str(legacy / "outbox")), \
                 mock.patch.object(plugin, "OUTBOX_INDEX", str(legacy / "outbox_index.json")), \
                 mock.patch.object(plugin.orca.host, "plugin", plugin_host, create=True):
                plugin.PrintersPlugin().register_capabilities()
                plugin_host.storage.assert_not_called()

                in_callback = True
                dashboard = plugin.PrintersDashboard()
                dashboard.on_load()

                self.assertEqual(plugin.PLUGIN_STORAGE_DIR, str(storage))
                self.assertEqual(plugin.PRINTERS_FILE, str(storage / "printers.json"))
                self.assertEqual(
                    plugin.PRINT_HISTORY_FILE,
                    str(storage / "print_history.json"),
                )
                self.assertEqual(plugin.OUTBOX_DIR, str(storage / "outbox"))
                self.assertFalse(storage.exists())
                preview = plugin.legacy_import_preview()
                self.assertEqual(preview["printers"], 1)
                self.assertEqual(preview["history"], 1)
                self.assertEqual(preview["outbox_files"], 1)
                self.assertNotIn("Sëcret", json.dumps(preview))
                self.assertFalse(plugin.import_legacy_storage(False))
                self.assertTrue(plugin.import_legacy_storage(preview["confirmation"]))
                self.assertEqual(plugin.load_manual_printers()[0]["apikey"], "  Sëcret  ")
                self.assertFalse(plugin.legacy_import_preview()["available"])
                self.assertEqual((storage / "outbox" / "fixture.gcode").read_text(), "G28")
                self.assertEqual(json.loads((storage / "print_history.json").read_text())[0]["id"],
                                 "history-1")
                self.assertTrue(list(storage.parent.glob("printers.legacy-backup-*")))
                dashboard.on_unload()

            self.assertTrue((legacy / "printers.json").exists())
            self.assertTrue((legacy / "outbox" / "fixture.gcode").exists())

    def test_old_host_fallback_does_not_auto_import_plugin_directory(self):
        plugin = _load_plugin()
        with tempfile.TemporaryDirectory() as temp:
            data_root = pathlib.Path(temp)
            legacy = data_root / "orca_plugins" / "printers"
            legacy.mkdir(parents=True)
            (legacy / "printers.json").write_text('[{"id":7}]', encoding="utf-8")
            expected = data_root / ".print-farm" / "orca-plugin"

            with mock.patch.object(plugin, "PLUGIN_DIR", str(legacy)), \
                 mock.patch.object(
                     plugin.orca.host,
                     "plugin",
                     types.SimpleNamespace(),
                     create=True,
                 ):
                self.assertTrue(plugin.configure_plugin_storage())

            self.assertEqual(plugin.PLUGIN_STORAGE_DIR, str(expected))
            self.assertFalse((expected / "printers.json").exists())
            self.assertEqual(plugin.legacy_import_preview()["printers"], 1)

    def test_native_accounts_do_not_share_printer_state(self):
        plugin = _load_plugin()
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            legacy = root / "orca_plugins" / "printers"
            legacy.mkdir(parents=True)
            account_a, account_b = root / "account-a", root / "account-b"
            with mock.patch.object(plugin, "PLUGIN_DIR", str(legacy)):
                with mock.patch.object(plugin.orca.host, "plugin",
                                       types.SimpleNamespace(storage=lambda: str(account_a)), create=True):
                    self.assertTrue(plugin.configure_plugin_storage())
                    self.assertTrue(plugin.save_manual_printers([{"id": 1, "url": "http://a"}]))
                with mock.patch.object(plugin.orca.host, "plugin",
                                       types.SimpleNamespace(storage=lambda: str(account_b)), create=True):
                    self.assertTrue(plugin.configure_plugin_storage())
                    self.assertEqual(plugin.load_manual_printers(), [])
                    self.assertTrue(plugin.save_manual_printers([{"id": 2}]))
                with mock.patch.object(plugin.orca.host, "plugin",
                                       types.SimpleNamespace(storage=lambda: str(account_a)), create=True):
                    self.assertTrue(plugin.configure_plugin_storage())
                    self.assertEqual(plugin.load_manual_printers()[0]["id"], 1)

    def test_legacy_import_refuses_to_merge_into_a_populated_account_store(self):
        plugin = _load_plugin()
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            legacy = root / "orca_plugins" / "printers"
            legacy.mkdir(parents=True)
            (legacy / "printers.json").write_text('[{"id":1}]', encoding="utf-8")
            destination = root / "account"
            destination.mkdir()
            (destination / "print_history.json").write_text("[]", encoding="utf-8")
            with mock.patch.object(plugin, "PLUGIN_DIR", str(legacy)), \
                    mock.patch.object(plugin.orca.host, "plugin",
                                      types.SimpleNamespace(storage=lambda: str(destination)), create=True):
                self.assertTrue(plugin.configure_plugin_storage())
                self.assertFalse(plugin.legacy_import_preview()["available"])
                preview = plugin.legacy_import_preview()
                self.assertFalse(plugin.import_legacy_storage(preview.get("confirmation")))
            self.assertEqual((destination / "print_history.json").read_text(encoding="utf-8"), "[]")
            self.assertFalse((destination / "printers.json").exists())

    def test_corrupt_history_is_preserved_and_blocks_writes(self):
        plugin = _load_plugin()
        with tempfile.TemporaryDirectory() as temp:
            store = pathlib.Path(temp, "print_history.json")
            original = '[{"id":"good"}, null]'
            store.write_text(original, encoding="utf-8")
            with mock.patch.object(plugin, "PRINT_HISTORY_FILE", str(store)):
                self.assertEqual(plugin.load_print_history(), [])
                self.assertFalse(plugin.save_print_history([]))
            self.assertEqual(store.read_text(encoding="utf-8"), original)

    def test_denied_store_stat_is_not_treated_as_missing(self):
        plugin = _load_plugin()
        denied = pathlib.Path(tempfile.gettempdir()) / "denied-store.json"
        real_stat = plugin.os.stat
        def deny_selected(path, *args, **kwargs):
            if str(path) in {plugin.PRINTERS_FILE, plugin.PRINT_HISTORY_FILE}:
                raise PermissionError("denied")
            return real_stat(path, *args, **kwargs)
        with mock.patch.object(plugin, "PRINTERS_FILE", str(denied)), \
             mock.patch.object(plugin, "PRINT_HISTORY_FILE", str(denied)), \
             mock.patch.object(plugin.os, "stat", side_effect=deny_selected):
            self.assertEqual(plugin.load_manual_printers(), [])
            self.assertFalse(plugin.save_manual_printers([{"id": 1}]))
            self.assertEqual(plugin.load_print_history(), [])
            self.assertFalse(plugin.save_print_history([]))

    def test_import_binds_confirmation_to_preview_and_source_digest(self):
        plugin = _load_plugin()
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            legacy = root / "legacy"
            legacy.mkdir()
            source = legacy / "printers.json"
            source.write_text('[{"id":1}]', encoding="utf-8")
            target = root / "account"
            with mock.patch.object(plugin, "PLUGIN_DIR", str(legacy)), \
                 mock.patch.object(plugin.orca.host, "plugin",
                                   types.SimpleNamespace(storage=lambda: str(target)), create=True):
                plugin.configure_plugin_storage()
                preview = plugin.legacy_import_preview()
                source.write_text('[{"id":2}]', encoding="utf-8")
                self.assertFalse(plugin.import_legacy_storage(preview["confirmation"]))
                self.assertFalse((target / "printers.json").exists())

    def test_partial_old_backup_does_not_block_retryable_import(self):
        plugin = _load_plugin()
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            legacy = root / "legacy"
            legacy.mkdir()
            (legacy / "printers.json").write_text('[{"id":1}]', encoding="utf-8")
            target = root / "account"
            pathlib.Path(str(target) + ".legacy-backup").mkdir()
            with mock.patch.object(plugin, "PLUGIN_DIR", str(legacy)), \
                 mock.patch.object(plugin.orca.host, "plugin",
                                   types.SimpleNamespace(storage=lambda: str(target)), create=True):
                plugin.configure_plugin_storage()
                preview = plugin.legacy_import_preview()
                self.assertTrue(plugin.import_legacy_storage(preview["confirmation"]))
                self.assertTrue((target / "printers.json").exists())

    def test_source_mutation_during_stage_copy_fails_then_fresh_preview_imports(self):
        plugin = _load_plugin()
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            legacy = root / "legacy"
            legacy.mkdir()
            source = legacy / "printers.json"
            source.write_text('[{"id":1}]', encoding="utf-8")
            target = root / "account"
            with mock.patch.object(plugin, "PLUGIN_DIR", str(legacy)), \
                 mock.patch.object(plugin.orca.host, "plugin",
                                   types.SimpleNamespace(storage=lambda: str(target)), create=True):
                plugin.configure_plugin_storage()
                preview = plugin.legacy_import_preview()
                real_copy2 = plugin.shutil.copy2
                mutated = False
                def copy_then_mutate(src, dst, *args, **kwargs):
                    nonlocal mutated
                    result = real_copy2(src, dst, *args, **kwargs)
                    if str(src) == str(source) and not mutated:
                        source.write_text('[{"id":2}]', encoding="utf-8")
                        mutated = True
                    return result
                with mock.patch.object(plugin.shutil, "copy2", side_effect=copy_then_mutate):
                    self.assertFalse(plugin.import_legacy_storage(preview["confirmation"]))
                self.assertFalse((target / "printers.json").exists())
                fresh = plugin.legacy_import_preview()
                self.assertTrue(plugin.import_legacy_storage(fresh["confirmation"]))

    def test_single_file_install_materializes_the_tab_icon(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            missing = root / "not-packaged.svg"
            with mock.patch.object(self.plugin, "PACKAGED_ICON_PATH", str(missing)), \
                 mock.patch.object(self.plugin, "ICON_PATH", str(missing)), \
                 mock.patch.object(self.plugin, "PLUGIN_STORAGE_DIR", str(root)):
                icon = self.plugin.ensure_icon()

            self.assertEqual(icon, str(root / "printers.svg"))
            self.assertEqual((root / "printers.svg").read_bytes(), self.plugin._ICON_SVG)

    def test_repeated_orca_picker_choice_upserts_one_printer(self):
        dashboard = self.plugin.PrintersDashboard()
        with tempfile.TemporaryDirectory() as temp:
            store = pathlib.Path(temp) / "printers.json"
            with mock.patch.object(self.plugin, "PRINTERS_FILE", str(store)):
                first = dashboard._add_manual({
                    "name": "Voron",
                    "url": "192.168.0.122",
                    "type": "moonraker",
                    "source": "orca",
                    "discovery_ref": "orca:same-preset",
                })
                second = dashboard._add_manual({
                    "name": "Voron renamed",
                    "url": "http://192.168.0.122/",
                    "type": "moonraker",
                    "source": "orca",
                    "discovery_ref": "orca:same-preset",
                })
                stored = self.plugin.load_manual_printers()

            self.assertEqual(first["id"], second["id"])
            self.assertEqual(len(stored), 1)
            self.assertEqual(stored[0]["name"], "Voron renamed")

    def test_orca_picker_keeps_distinct_presets_on_same_server(self):
        first = {
            "name": "First", "url": "http://same.local", "type": "moonraker",
            "source": "orca", "discovery_ref": "orca:first",
        }
        second = dict(first, name="Second", discovery_ref="orca:second")
        with mock.patch.object(self.plugin, "load_manual_printers", return_value=[first]):
            available = self.plugin.available_orca_printers([first, second])
        self.assertEqual(available, [self.plugin.public_printer_record(second)])

        source = PLUGIN_PATH.read_text(encoding="utf-8")
        self.assertIn("if (p.source === 'orca') return !activeRefs[p.discovery_ref];", source)
        self.assertIn("pendingAdoptRef ? p.discovery_ref === pendingAdoptRef", source)

    def test_legacy_duplicate_picker_records_are_preserved_without_plaintext_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            store = pathlib.Path(temp) / "printers.json"
            store.write_text(json.dumps([
                {"id": 1, "name": "First", "url": "http://printer"},
                {"id": 2, "name": "Latest", "url": "http://printer/"},
            ]), encoding="utf-8")
            with mock.patch.object(self.plugin, "PRINTERS_FILE", str(store)):
                stored = self.plugin.load_manual_printers()

            self.assertEqual(len(stored), 2)
            self.assertEqual([item["name"] for item in stored], ["First", "Latest"])
            self.assertNotEqual(stored[0]["target_uuid"], stored[1]["target_uuid"])
            backup = pathlib.Path(str(store) + ".identity-migration.bak")
            self.assertFalse(backup.exists())

    def test_same_server_targets_keep_distinct_case_sensitive_routes_and_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            store = pathlib.Path(temp) / "printers.json"
            store.write_text(json.dumps([
                {"id": 1, "name": "Lower", "url": "http://printer.local/base", "serial": "SERIAL"},
                {"id": 2, "name": "Upper", "url": "http://printer.local/Base", "serial": "SERIAL"},
            ]), encoding="utf-8")
            with mock.patch.object(self.plugin, "PRINTERS_FILE", str(store)):
                targets = self.plugin.load_manual_printers()

            self.assertEqual(len(targets), 2)
            self.assertNotEqual(targets[0]["target_uuid"], targets[1]["target_uuid"])
            self.assertEqual(targets[0]["server_uuid"], targets[1]["server_uuid"])
            self.assertNotEqual(targets[0]["target_selector"], targets[1]["target_selector"])
            self.assertNotEqual(targets[0]["url"], targets[1]["url"])

    def test_ambiguous_legacy_url_action_fails_closed(self):
        plugin = self.plugin
        targets = [
            plugin._ensure_printer_identity({"url": "http://printer.local/base"}, ""),
            plugin._ensure_printer_identity({"url": "http://printer.local/base"}, ""),
        ]

        self.assertIsNone(plugin.resolve_printer_target(
            targets, {"url": "http://printer.local/base"}
        ))

    def test_unique_legacy_url_also_cannot_authorize_a_write(self):
        printer = self.plugin._ensure_printer_identity({
            "url": "http://printer.local/base", "type": "moonraker",
        })
        self.assertIsNone(self.plugin.resolve_printer_target(
            [printer], {"url": printer["url"]}
        ))

    def test_identity_message_cannot_select_another_targets_route(self):
        plugin = self.plugin
        first = plugin._ensure_printer_identity({
            "url": "http://printer.local/base", "type": "moonraker",
        }, "")
        second = plugin._ensure_printer_identity({
            "url": "http://printer.local/base", "type": "moonraker",
        }, "")
        forged = {
            "targetUuid": second["target_uuid"],
            "ownerScope": first["owner_scope_uuid"],
            "targetSelector": first["target_selector"],
            "bindingRevision": first["binding_revision"],
            "url": first["url"],
            "connectionType": first["type"],
        }

        self.assertIsNone(plugin.resolve_printer_target([first, second], forged))
        wrong_route = {
            "targetUuid": first["target_uuid"],
            "ownerScope": first["owner_scope_uuid"],
            "targetSelector": first["target_selector"],
            "bindingRevision": first["binding_revision"],
            "url": "http://other.local/base",
        }
        self.assertIsNone(plugin.resolve_printer_target([first, second], wrong_route))

    def test_rename_preserves_binding_but_rebind_invalidates_stale_message(self):
        dashboard = self.plugin.PrintersDashboard()
        with tempfile.TemporaryDirectory() as temp:
            store = pathlib.Path(temp) / "printers.json"
            with mock.patch.object(self.plugin, "PRINTERS_FILE", str(store)):
                original = dashboard._add_manual({
                    "name": "Voron", "url": "http://printer.local/base",
                    "type": "moonraker", "serial": "SERIAL",
                })
                renamed = dashboard._add_manual({
                    "target_uuid": original["target_uuid"],
                    "owner_scope_uuid": original["owner_scope_uuid"],
                    "target_selector": original["target_selector"],
                    "binding_revision": original["binding_revision"],
                    "name": "Voron 2", "url": "http://printer.local/base",
                    "type": "moonraker", "serial": "SERIAL",
                })
                self.assertEqual(original["binding_revision"], renamed["binding_revision"])
                rebound = dashboard._add_manual({
                    "target_uuid": renamed["target_uuid"],
                    "owner_scope_uuid": renamed["owner_scope_uuid"],
                    "target_selector": renamed["target_selector"],
                    "binding_revision": renamed["binding_revision"],
                    "name": "Voron 2", "url": "http://printer.local/new-base",
                    "type": "moonraker", "serial": "SERIAL",
                })
                self.assertEqual(rebound["binding_revision"], renamed["binding_revision"] + 1)
                stale = {
                    "targetUuid": renamed["target_uuid"],
                    "ownerScope": renamed["owner_scope_uuid"],
                    "targetSelector": renamed["target_selector"],
                    "bindingRevision": renamed["binding_revision"],
                    "url": renamed["url"],
                    "connectionType": renamed["type"],
                }
                self.assertIsNone(self.plugin.resolve_printer_target([rebound], stale))
                with mock.patch.object(self.plugin, "dashboard_printers", return_value=[rebound]), \
                        mock.patch.object(self.plugin, "poll_printer") as poll, \
                        mock.patch.object(self.plugin, "printer_command") as command:
                    dashboard._do_printer_cmd({**stale, "action": "cancel"})
                poll.assert_not_called()
                command.assert_not_called()

    def test_add_same_route_creates_a_distinct_target(self):
        dashboard = self.plugin.PrintersDashboard()
        with tempfile.TemporaryDirectory() as temp:
            store = pathlib.Path(temp) / "printers.json"
            with mock.patch.object(self.plugin, "PRINTERS_FILE", str(store)):
                first = dashboard._add_manual({
                    "name": "Repetier target one", "url": "http://printer.local",
                    "type": "moonraker",
                })
                second = dashboard._add_manual({
                    "name": "Repetier target two", "url": "http://printer.local",
                    "type": "moonraker",
                })
                self.assertNotEqual(first["target_uuid"], second["target_uuid"])
                self.assertEqual(first["server_uuid"], second["server_uuid"])
                self.assertEqual(len(self.plugin.load_manual_printers()), 2)

    def test_history_identity_survives_binding_revision_change(self):
        printer = self.plugin._ensure_printer_identity({
            "url": "http://printer.local", "type": "moonraker",
        })
        rebound = dict(printer, url="http://new-printer.local",
                       binding_revision=printer["binding_revision"] + 1)
        self.assertEqual(
            self.plugin.printer_history_key(printer),
            self.plugin.printer_history_key(rebound),
        )

    def test_picker_waits_for_python_acknowledgement(self):
        source = PLUGIN_PATH.read_text(encoding="utf-8")
        self.assertIn("function typeLabel(value, status){", source)
        self.assertNotIn("function typeLabel(t){", source)
        self.assertIn("pendingAdoptUrl = p.url || '';", source)
        self.assertIn("e.target.disabled = true;", source)
        self.assertIn("if (data.adoptResult)", source)
        self.assertIn("activeUrls[normalizedPrinterUrl(p.url)]", source)
        self.assertIn("var pendingFormAdd = false;", source)
        self.assertIn("orca.postMessage({ type:'adopt', discoveryRef:p.discovery_ref });", source)
        self.assertNotIn("orca.postMessage({ type:'adopt', printer:p });", source)
        self.assertIn("p.owner_scope_uuid || '', p.target_uuid || '', p.binding_revision || ''", source)
        self.assertIn("var pendingTargets = null;", source)
        self.assertIn("type:'mass-print'", source)
        self.assertTrue("slice.arrayBuffer()" in source)
        self.assertTrue("type:'artifact-begin'" in source)
        self.assertTrue("if (data.artifactChunk && transfer" in source)
        self.assertNotIn("reader.readAsDataURL(file)", source)
        self.assertNotIn("pendingUrls", source)

        dashboard = self.plugin.PrintersDashboard()
        dashboard._post = mock.Mock(return_value=True)
        entry = {"name": "Voron", "url": "http://printer", "type": "moonraker"}
        with mock.patch.object(dashboard, "_add_manual", return_value=dict(entry, id=1)), \
             mock.patch.object(dashboard, "_refresh_worker"):
            dashboard._add_and_refresh(entry, [])

        dashboard._post.assert_called_once_with({
            "adoptResult": {"url": "http://printer", "ok": True},
        })

    def test_saved_credentials_never_reach_the_dashboard_page(self):
        dashboard = self.plugin.PrintersDashboard()
        dashboard._post = mock.Mock(return_value=True)
        private = {
            "id": 1,
            "discovery_ref": "orca:fixture",
            "name": "Authenticated OctoPrint",
            "url": "http://octoprint.local",
            "type": "octoprint",
            "apikey": "octoprint-secret",
            "access_code": "bambu-secret",
        }

        self.assertEqual(
            self.plugin.public_printer_record(private),
            {
                "id": 1,
                "discovery_ref": "orca:fixture",
                "name": "Authenticated OctoPrint",
                "url": "http://octoprint.local",
                "type": "octoprint",
            },
        )
        with mock.patch.object(
            self.plugin,
            "load_manual_printers",
            return_value=[],
        ):
            self.assertEqual(
                self.plugin.available_orca_printers([private]),
                [self.plugin.public_printer_record(private)],
            )

        with mock.patch.object(
            self.plugin,
            "dashboard_printers",
            return_value=[dict(private)],
        ), mock.patch.object(
            self.plugin,
            "attach_outbox",
            return_value=0,
        ), mock.patch.object(
            dashboard,
            "_poll_and_push",
        ):
            dashboard._refresh_worker([dict(private)], False)

        payload = dashboard._post.call_args.args[0]
        serialized = json.dumps(payload)
        self.assertNotIn("octoprint-secret", serialized)
        self.assertNotIn("bambu-secret", serialized)
        self.assertEqual(payload["available"][0]["discovery_ref"], "orca:fixture")

        dashboard._post.reset_mock()
        with mock.patch.object(self.plugin, "autoname_printers"), mock.patch.object(
            self.plugin,
            "attach_outbox",
        ):
            dashboard._finish_poll_batch([dict(private)])
        serialized = json.dumps(dashboard._post.call_args.args[0])
        self.assertNotIn("octoprint-secret", serialized)
        self.assertNotIn("bambu-secret", serialized)

    def test_public_printer_record_projects_ui_fields_and_readiness_status(self):
        printer = {
            "id": 7,
            "name": "Printer",
            "url": "http://printer.local",
            "type": "octoprint",
            "serial": "device-serial",
            "source": "manual",
            "discovery_ref": "orca:fixture",
            "insecure": True,
            "password": "legacy-secret",
            "future_private_field": "must-not-escape",
            "status": {
                "supported": True,
                "online": True,
                "state": "Operational",
                "backend": "octoprint",
                "job_state": "Operational",
                "progress": 25,
                "filename": "part.gcode",
                "error": "",
                "token": "nested-secret",
                "future_status_field": "must-not-escape",
            },
            "queued": [{
                "file": "fixture.gcode",
                "name": "Fixture",
                "host": "private-host",
                "future_queue_field": "must-not-escape",
            }],
        }

        public = self.plugin.public_printer_record(printer)

        self.assertEqual(
            public,
            {
                "id": 7,
                "name": "Printer",
                "url": "http://printer.local",
                "type": "octoprint",
                "serial": "device-serial",
                "source": "manual",
                "discovery_ref": "orca:fixture",
                "status": {
                    "supported": True,
                    "online": True,
                    "state": "Operational",
                    "backend": "octoprint",
                    "job_state": "Operational",
                    "progress": 25,
                    "filename": "part.gcode",
                    "error": "",
                },
                "queued": [{"file": "fixture.gcode", "name": "Fixture"}],
            },
        )

    def test_orca_picker_resolves_credentials_only_from_python_snapshot(self):
        dashboard = self.plugin.PrintersDashboard()
        private = {
            "discovery_ref": "orca:trusted",
            "name": "OctoPrint",
            "url": "http://octoprint.local",
            "type": "octoprint",
            "apikey": "trusted-secret",
        }
        dashboard._discovered_printers = {"orca:trusted": private}

        with mock.patch.object(
            dashboard,
            "_capture_discovered_printers",
            return_value=[private],
        ), mock.patch.object(self.plugin.ACTION_WORKER, "submit") as submit:
            dashboard.on_message({
                "type": "adopt",
                "discoveryRef": "orca:trusted",
                "printer": {
                    "url": "http://attacker.invalid",
                    "apikey": "attacker-secret",
                },
            })

            submit.assert_called_once()
            entry = submit.call_args.args[1]
            self.assertEqual(entry["url"], "http://octoprint.local")
            self.assertEqual(entry["apikey"], "trusted-secret")

            submit.reset_mock()
            dashboard.on_message({
                "type": "adopt",
                "discoveryRef": "orca:unknown",
                "printer": private,
            })
            submit.assert_not_called()

    def test_manual_octoprint_api_key_is_private_and_type_scoped(self):
        source = PLUGIN_PATH.read_text(encoding="utf-8")
        self.assertIn('id="f-apikey" type="password" autocomplete="off"', source)
        self.assertIn("document.getElementById('f-apikey').value = '';", source)
        dashboard = self.plugin.PrintersDashboard()

        with mock.patch.object(
            dashboard,
            "_capture_discovered_printers",
            return_value=[],
        ), mock.patch.object(self.plugin.ACTION_WORKER, "submit") as submit:
            dashboard.on_message({
                "type": "add",
                "name": "OctoPrint",
                "url": "octoprint.local",
                "kind": "octoprint",
                "apikey": "  secret-key  ",
                "access_code": "must-not-be-stored",
            })

            entry = submit.call_args.args[1]
            self.assertEqual(entry["apikey"], "  secret-key  ")
            self.assertEqual(entry["access_code"], "")

            submit.reset_mock()
            dashboard.on_message({
                "type": "add",
                "name": "Moonraker",
                "url": "moonraker.local",
                "kind": "moonraker",
                "apikey": "must-not-be-stored",
            })
            self.assertEqual(submit.call_args.args[1]["apikey"], "")

    def test_plugin_does_not_forge_host_owned_process_manifest_refs(self):
        source = PLUGIN_PATH.read_text(encoding="utf-8")
        self.assertNotIn("OUTBOX_REF", source)
        self.assertNotIn("outbox-toggle", source)
        self.assertNotIn("set_outbox_binding", source)

    def test_credentials_round_trip_without_trimming_and_reject_invalid_inputs(self):
        for value in (" code ", "🔐secret", "0"):
            self.assertEqual(self.plugin.private_credential(value), value)
        self.assertIsNone(self.plugin.private_credential("bad\nsecret"))
        self.assertIsNone(self.plugin.private_credential("x" * 513))

    def test_authenticated_http_redirect_is_rejected_before_second_origin(self):
        target_calls = []

        class TargetHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                target_calls.append(dict(self.headers))
                self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
            def log_message(self, *args):
                pass

        target = http.server.HTTPServer(("127.0.0.1", 0), TargetHandler)
        class RedirectHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:%d/steal" % target.server_port)
                self.end_headers()
            def log_message(self, *args):
                pass

        source = http.server.HTTPServer(("127.0.0.1", 0), RedirectHandler)
        threads = [threading.Thread(target=server.serve_forever, daemon=True)
                   for server in (source, target)]
        for thread in threads: thread.start()
        try:
            with self.assertRaises(urllib.error.HTTPError):
                self.plugin._get_json("http://127.0.0.1:%d/" % source.server_port,
                                      None, {"X-Api-Key": "secret"})
            self.assertEqual(target_calls, [])
        finally:
            source.shutdown(); target.shutdown()
            source.server_close(); target.server_close()

    def test_ftps_data_channel_reuses_the_control_tls_session(self):
        import ftplib

        ftp = object.__new__(self.plugin._implicit_ftps_class())
        ftp._prot_p = True
        ftp.host = "printer"
        ftp._bambu_printer = self._bambu_printer()
        ftp.sock = types.SimpleNamespace(session="control-session")
        ftp.context = mock.Mock()
        tls_data = mock.Mock()
        tls_data.getpeercert.return_value = b"synthetic Bambu TLS certificate"
        ftp.context.wrap_socket.return_value = tls_data

        with mock.patch.object(
            ftplib.FTP,
            "ntransfercmd",
            return_value=("raw-data", 123),
        ):
            self.assertEqual(ftp.ntransfercmd("STOR job.gcode"), (tls_data, 123))

        ftp.context.wrap_socket.assert_called_once_with(
            "raw-data",
            server_hostname="printer",
            session="control-session",
        )

    def _fake_ftps(self, fail_on=None):
        events = []

        class FakeFtps:
            def __init__(self, context=None):
                events.append(("context", context))

            def connect(self, host, port, timeout=None):
                events.append(("connect", host, port))

            def login(self, user, password):
                events.append(("login", user, password))

            def prot_p(self):
                events.append(("prot_p",))

            def storbinary(self, command, stream, blocksize=8192):
                events.append(("stor", command, stream.read()))
                if fail_on == "stor":
                    raise OSError("data connection failed")

            def quit(self):
                events.append(("quit",))

            def close(self):
                events.append(("close",))

        return events, FakeFtps

    def _job_file(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        source = pathlib.Path(temp.name) / "job.gcode"
        source.write_bytes(b"G28\n")
        return source

    def _bambu_printer(self):
        return _trusted_bambu_card(self, self.plugin, {
            "url": "http://192.0.2.7", "access_code": "code-1", "serial": "SERIAL1"})

    def test_bambu_send_uploads_over_native_ftps_then_publishes_start(self):
        events, fake = self._fake_ftps()
        source = self._job_file()
        with mock.patch.object(self.plugin, "_implicit_ftps_class", return_value=fake),                 mock.patch.object(self.plugin, "bambu_start_print", return_value="") as start:
            result = self.plugin._bambu_send_file(
                self._bambu_printer(), "job.gcode", str(source), True, False, None)
        self.assertEqual(result, {"ok": True, "evidence_ref": "ftps:complete"})
        kinds = [event[0] for event in events]
        self.assertEqual(kinds, ["context", "connect", "login", "prot_p", "stor", "quit"])
        self.assertEqual(events[1], ("connect", "192.0.2.7", 990))
        self.assertEqual(events[2], ("login", "bblp", "code-1"))
        stor = events[4]
        self.assertTrue(stor[1].startswith("STOR pf-") and stor[1].endswith(".gcode"))
        self.assertEqual(stor[2], b"G28\n")
        start.assert_called_once()
        self.assertEqual(start.call_args.args[:4],
                         ("192.0.2.7", "code-1", "SERIAL1", stor[1][len("STOR "):]))

    def test_bambu_send_upload_failure_never_starts_the_print(self):
        events, fake = self._fake_ftps(fail_on="stor")
        source = self._job_file()
        with mock.patch.object(self.plugin, "_implicit_ftps_class", return_value=fake),                 mock.patch.object(self.plugin, "bambu_start_print") as start:
            result = self.plugin._bambu_send_file(
                self._bambu_printer(), "job.gcode", str(source), True, False, None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "unknown_outcome")
        start.assert_not_called()

    def test_bambu_send_start_failure_is_reported_after_a_complete_upload(self):
        events, fake = self._fake_ftps()
        source = self._job_file()
        with mock.patch.object(self.plugin, "_implicit_ftps_class", return_value=fake),                 mock.patch.object(self.plugin, "bambu_start_print", return_value="refused"):
            result = self.plugin._bambu_send_file(
                self._bambu_printer(), "job.gcode", str(source), True, False, None)
        self.assertEqual(result["error_key"], "errorPrintCommand")
        self.assertEqual(result["error_code"], "unknown_outcome")

    def test_plugin_source_has_no_system_program_fallback_for_uploads(self):
        source = PLUGIN_PATH.read_text(encoding="utf-8")
        for token in ("subprocess", "curl", "os.environ", "getenv"):
            self.assertNotIn(token, source)

    def test_host_push_tolerates_a_window_closing_mid_job(self):
        posted = []

        class Window:
            def is_open(self):
                return True

            def post(self, payload):
                posted.append(payload)

        self.assertTrue(self.plugin.post_window(Window(), {"status": "ok"}))
        self.assertEqual(posted, [{"status": "ok"}])
        self.assertFalse(self.plugin.post_window(None, {}))

    def test_reusable_pool_keeps_bursty_jobs_on_one_worker(self):
        pool = self.plugin.ReusableDaemonPool(
            "printers-test",
            max_workers=1,
            idle_timeout=0.2,
        )
        thread_ids = []
        done = threading.Event()

        def record():
            thread_ids.append(threading.get_ident())
            if len(thread_ids) == 2:
                done.set()

        pool.submit(record)
        pool.submit(record)

        self.assertTrue(done.wait(2))
        self.assertEqual(len(set(thread_ids)), 1)

    def test_pool_shutdown_discards_queued_printer_work(self):
        pool = self.plugin.ReusableDaemonPool(
            "printers-cancel-test",
            max_workers=1,
            idle_timeout=0.2,
        )
        started = threading.Event()
        release = threading.Event()
        ran = []

        def blocking_job():
            started.set()
            release.wait(2)

        first = pool.submit(blocking_job)
        self.assertTrue(started.wait(1))
        queued = pool.submit(lambda: ran.append("printer-command"))

        pool.shutdown(wait_timeout=0)
        pool.activate()
        current = pool.submit(lambda: ran.append("new-generation"))
        release.set()
        first.wait(1)
        current.wait(1)

        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            queued.wait(1)
        self.assertEqual(ran, ["new-generation"])

    def test_stale_worker_generation_cannot_post_to_window(self):
        plugin = _load_plugin()
        pool = plugin.ACTION_WORKER
        pool.activate()
        started = threading.Event()
        release = threading.Event()
        posted = []

        class Window:
            def is_open(self):
                return True

            def post(self, payload):
                posted.append(payload)

        def stale_post():
            started.set()
            release.wait(2)
            plugin.post_window(Window(), {"status": "stale"})

        job = pool.submit(stale_post)
        self.assertTrue(started.wait(1))
        pool.shutdown(wait_timeout=0)
        release.set()
        job.wait(1)

        self.assertEqual(posted, [])

    def test_dashboard_lifecycle_drops_queued_physical_command(self):
        plugin = _load_plugin(lifecycle=True)
        dashboard = plugin.PrintersDashboard()
        started = threading.Event()
        release = threading.Event()
        command = mock.Mock()

        with mock.patch.object(plugin, "configure_plugin_storage"), \
             mock.patch.object(dashboard, "_do_printer_cmd", command):
            dashboard.on_load()

            def blocking_job():
                started.set()
                release.wait(2)

            plugin.ACTION_WORKER.submit(blocking_job)
            self.assertTrue(started.wait(1))
            dashboard.on_message({
                "type": "printer-cmd",
                "url": "http://printer",
                "action": "cancel",
            })
            dashboard.on_cancelled()
            release.set()

        command.assert_not_called()

    def test_finished_upload_cannot_mutate_state_after_unload(self):
        plugin = _load_plugin(lifecycle=True)
        dashboard = plugin.PrintersDashboard()
        started = threading.Event()
        release = threading.Event()
        printer = plugin._ensure_printer_identity({
            "name": "Printer",
            "url": "http://printer",
            "type": "moonraker",
        })
        record = {"file": "fixture.gcode", "name": "fixture.gcode",
                  "printer_settings_id": "Printer"}

        def blocking_send(*args, **kwargs):
            started.set()
            release.wait(2)
            return {"ok": True}

        with tempfile.TemporaryDirectory() as temp:
            plugin.OUTBOX_DIR = temp
            pathlib.Path(temp, "fixture.gcode").write_bytes(b"G28")
            with mock.patch.object(plugin, "configure_plugin_storage"), \
                 mock.patch.object(plugin, "dashboard_printers", return_value=[printer]), \
                 mock.patch.object(plugin, "load_outbox_index", return_value=[record]), \
                 mock.patch.object(plugin, "poll_printer", return_value={
                     **_fresh_status(plugin, "moonraker", "standby", "standby"),
                 }), \
                 mock.patch.object(plugin, "send_file_path", side_effect=blocking_send), \
                 mock.patch.object(plugin, "record_print_started") as history, \
                 mock.patch.object(plugin, "outbox_remove") as remove:
                dashboard.on_load()
                job = plugin.ACTION_WORKER.submit(
                    dashboard._do_outbox_send,
                    {
                        "file": "fixture.gcode",
                        "targetUuid": printer["target_uuid"],
                        "ownerScope": printer["owner_scope_uuid"],
                        "targetSelector": printer["target_selector"],
                        "bindingRevision": printer["binding_revision"],
                        "connectionType": printer["type"],
                        "url": printer["url"],
                        "start": False,
                    },
                )
                self.assertTrue(started.wait(1))
                dashboard.on_unload()
                release.set()
                job.wait(1)

        history.assert_not_called()
        remove.assert_not_called()

    def test_outbox_start_rejects_a_different_printer(self):
        plugin = _load_plugin(lifecycle=True)
        dashboard = plugin.PrintersDashboard()
        printer = plugin._ensure_printer_identity({
            "name": "Printer B", "url": "http://printer-b", "type": "moonraker",
        })
        record = {"file": "fixture.gcode", "name": "fixture.gcode",
                  "printer_settings_id": "Printer A"}

        with tempfile.TemporaryDirectory() as temp:
            plugin.OUTBOX_DIR = temp
            pathlib.Path(temp, "fixture.gcode").write_bytes(b"G28")
            with mock.patch.object(plugin, "configure_plugin_storage"), \
                 mock.patch.object(plugin, "dashboard_printers", return_value=[printer]), \
                 mock.patch.object(plugin, "load_outbox_index", return_value=[record]), \
                 mock.patch.object(plugin, "poll_printer") as poll, \
                 mock.patch.object(plugin, "send_file_path") as send:
                dashboard.on_load()
                job = plugin.ACTION_WORKER.submit(
                    dashboard._do_outbox_send,
                    {"file": "fixture.gcode", "url": "http://printer-b", "start": True},
                )
                job.wait(2)
                dashboard.on_unload()

        poll.assert_not_called()
        send.assert_not_called()

    def test_dashboard_uses_reusable_workers_instead_of_per_refresh_threads(self):
        source = PLUGIN_PATH.read_text(encoding="utf-8")
        self.assertEqual(source.count("threading.Thread("), 1)
        self.assertIn("POLL_WORKERS.submit", source)
        self.assertIn("ACTION_WORKER.submit", source)

    def test_network_discovery_runs_on_open_and_explicit_refresh(self):
        dashboard = self.plugin.PrintersDashboard()
        dashboard._post = mock.Mock(return_value=True)
        discovered = [{
            "discovery_ref": "orca:configured",
            "name": "Configured",
            "url": "http://printer",
            "type": "moonraker",
            "apikey": "private-secret",
        }]

        with mock.patch.object(
            self.plugin,
            "discover_orca_printers",
            return_value=discovered,
        ), mock.patch.object(
            self.plugin,
            "start_plugin_runtime",
        ) as start_runtime, mock.patch.object(
            self.plugin.ACTION_WORKER,
            "submit",
        ) as submit:
            dashboard.on_message({"type": "ready"})
            start_runtime.assert_called_once_with()
            dashboard._post.assert_called_once_with({
                "available": [{
                    "discovery_ref": "orca:configured",
                    "name": "Configured",
                    "url": "http://printer",
                    "type": "moonraker",
                }],
            })
            submit.assert_called_once()
            self.assertEqual(submit.call_args.args[0].__wrapped__, dashboard._refresh_worker)
            self.assertEqual(submit.call_args.args[1:], (discovered, True))

            start_runtime.reset_mock()
            dashboard._post.reset_mock()
            submit.reset_mock()
            dashboard.on_message({"type": "refresh"})
            start_runtime.assert_called_once_with()
            dashboard._post.assert_called_once()
            submit.assert_called_once()
            self.assertEqual(submit.call_args.args[0].__wrapped__, dashboard._refresh_worker)
            self.assertEqual(submit.call_args.args[1:], (discovered, True))

    def test_page_ready_reactivates_a_stopped_runtime(self):
        plugin = _load_plugin()
        dashboard = plugin.PrintersDashboard()
        dashboard._post = mock.Mock(return_value=True)
        refreshed = threading.Event()

        with mock.patch.object(plugin, "refresh_ui_language"), mock.patch.object(
            plugin,
            "configure_plugin_storage",
        ), mock.patch.object(
            plugin,
            "discover_orca_printers",
            return_value=[],
        ), mock.patch.object(
            dashboard,
            "_refresh_worker",
            side_effect=lambda *_args: refreshed.set(),
        ):
            plugin.start_plugin_runtime()
            plugin.stop_plugin_runtime()

            dashboard.on_message({"type": "ready"})

            self.assertTrue(refreshed.wait(1))
            self.assertTrue(plugin._PLUGIN_RUNTIME_ACTIVE)
            plugin.stop_plugin_runtime()

    def test_orca_machine_presets_feed_the_available_picker(self):
        plugin = _load_plugin()

        class Preset:
            name = "Voron 2.4 350"

            @staticmethod
            def config_value(key):
                return {
                    "print_host": "192.168.0.42:7125",
                    "host_type": "moonraker",
                    "printhost_apikey": "private-key",
                }.get(key, "")

        presets = types.SimpleNamespace(
            size=lambda: 1,
            preset=lambda index: Preset() if index == 0 else None,
        )
        bundle = types.SimpleNamespace(printers=presets)
        with mock.patch.object(
            plugin.orca.host,
            "preset_bundle",
            create=True,
            return_value=bundle,
        ):
            discovered = plugin.discover_orca_printers()

        self.assertEqual(len(discovered), 1)
        self.assertEqual(discovered[0]["name"], "Voron 2.4 350")
        self.assertEqual(discovered[0]["url"], "http://192.168.0.42:7125")
        self.assertEqual(discovered[0]["type"], "moonraker")
        self.assertEqual(discovered[0]["apikey"], "private-key")
        self.assertTrue(discovered[0]["discovery_ref"].startswith("orca:"))

    def test_empty_network_discovery_clears_stale_picker_entries(self):
        dashboard = self.plugin.PrintersDashboard()
        dashboard._post = mock.Mock(return_value=True)

        with mock.patch.object(self.plugin, "ssdp_bambu_names", return_value={}):
            dashboard._discover_and_push([])

        dashboard._post.assert_called_once_with({"discovered": []})

    def test_top_bar_keeps_manual_add_fields_compact_and_collapsed(self):
        source = PLUGIN_PATH.read_text(encoding="utf-8")

        self.assertIn('id="manual-form" class="manual-form" aria-hidden="true"', source)
        self.assertIn('id="manual-toggle" class="ghost"', source)
        self.assertIn("setManualFormOpen(true);", source)
        self.assertIn('class="compact-option"', source)
        self.assertIn('class="disclosure-icon" viewBox="0 0 12 8"', source)
        self.assertIn(
            '#manual-toggle[aria-expanded="true"] .disclosure-icon',
            source,
        )
        self.assertIn('<span aria-hidden="true">TLS</span>', source)
        self.assertIn("document.getElementById('f-insecure-l').style.display = ''", source)
        self.assertIn("printer credentials may be exposed", source)
        self.assertNotIn('id="f-insecure-text"', source)
        self.assertLess(source.index('id="f-apikey"'), source.index('id="f-type"'))
        self.assertLess(source.index('id="f-type"'), source.index('id="add"'))

    def test_removing_a_printer_rechecks_lan_availability(self):
        dashboard = self.plugin.PrintersDashboard()
        discovered = [{"name": "Configured", "url": "http://printer"}]

        with mock.patch.object(dashboard, "_remove_manual") as remove, mock.patch.object(
            dashboard,
            "_refresh_worker",
        ) as refresh:
            dashboard._remove_and_refresh(7, discovered)

        remove.assert_called_once_with(7)
        refresh.assert_called_once_with(discovered, discover_network=True)

    def test_each_printer_status_is_pushed_without_waiting_for_slower_peers(self):
        dashboard = self.plugin.PrintersDashboard()
        dashboard._post = mock.Mock(return_value=True)
        printer = self.plugin._ensure_printer_identity({"url": "http://printer"})
        status = {"online": True, "state": "ready"}

        with mock.patch.object(self.plugin, "poll_printer", return_value=status):
            dashboard._poll_into(printer)

        self.assertEqual(printer["status"], status)
        dashboard._post.assert_called_once_with({
            "printerStatus": {
                "url": "http://printer",
                "targetUuid": printer["target_uuid"],
                "ownerScope": printer["owner_scope_uuid"],
                "targetSelector": printer["target_selector"],
                "bindingRevision": printer["binding_revision"],
                "connectionType": printer.get("type"),
                "status": status,
            },
        })

    def test_stale_poll_cannot_restore_a_removed_printer(self):
        dashboard = self.plugin.PrintersDashboard()
        dashboard._list_generation = 2
        dashboard._post = mock.Mock(return_value=True)
        completed = []
        stale_printers = [{"id": 4, "url": "http://removed"}]

        with mock.patch.object(self.plugin, "autoname_printers") as autoname, \
             mock.patch.object(self.plugin, "attach_outbox") as attach:
            dashboard._finish_poll_batch(
                stale_printers,
                on_complete=lambda: completed.append(True),
                generation=1,
            )

        autoname.assert_not_called()
        attach.assert_not_called()
        dashboard._post.assert_not_called()
        self.assertEqual(completed, [True])

    def test_stale_status_result_is_not_cached_or_pushed(self):
        dashboard = self.plugin.PrintersDashboard()
        dashboard._list_generation = 2
        dashboard._post = mock.Mock(return_value=True)
        printer = self.plugin._ensure_printer_identity({"url": "http://removed"})

        with mock.patch.object(
            self.plugin,
            "poll_printer",
            return_value={"online": True, "state": "ready"},
        ):
            dashboard._poll_into(printer, generation=1)

        self.assertNotIn(self.plugin.printer_binding_key(printer), dashboard._status_cache)
        dashboard._post.assert_not_called()

    def test_dashboard_shows_unknown_status_as_checking(self):
        source = PLUGIN_PATH.read_text(encoding="utf-8")
        self.assertIn("data.printerStatus", source)
        self.assertIn("s.online === true", source)
        self.assertIn("'checking'", source)

    def test_bambu_connection_reset_keeps_diagnostic_out_of_card_state(self):
        reset = ConnectionResetError(10054, "Connection reset by peer")
        printer = _trusted_bambu_card(self, self.plugin, {
            "url": "http://192.168.0.42", "access_code": "local-code"})
        with mock.patch.object(
            self.plugin.socket, "create_connection", side_effect=reset
        ):
            status = self.plugin.poll_bambu(printer)

        self.assertIs(status["online"], False)
        self.assertEqual(status["state"], "offline")
        self.assertEqual(status["error_code"], "network_error")
        self.assertNotIn("10054", status["error"])
        self.assertIn('title="\' + esc(s.error || stateText)', self.plugin.PAGE)

    def test_bambu_self_signed_transport_requires_its_stored_certificate_pin(self):
        self.assertIs(self.plugin.ssl_ctx_for({"type": "bambu"}), self.plugin._SSL_INSECURE)
        self.assertIs(self.plugin.ssl_ctx_for({"type": "moonraker"}), self.plugin._SSL_VERIFY)
        self.assertIs(
            self.plugin.ssl_ctx_for({"type": "moonraker", "insecure": True}),
            self.plugin._SSL_INSECURE,
        )
        printer = _trusted_bambu_card(self, self.plugin, {
            "url": "http://printer.local", "access_code": "code", "serial": "serial"})
        wrapped = mock.Mock()
        wrapped.getpeercert.return_value = b"synthetic Bambu TLS certificate"
        raw = mock.Mock()
        raw.sendall = mock.Mock()
        context = mock.Mock()
        context.wrap_socket.return_value = wrapped
        wrapped.sendall = mock.Mock()
        wrapped.close = mock.Mock()
        with mock.patch.object(self.plugin, "_SSL_INSECURE", context), \
             mock.patch.object(self.plugin.socket, "create_connection", return_value=raw), \
             mock.patch.object(self.plugin, "_mqtt_read_packet", return_value=(0x20, b"\x00\x00")):
            self.assertEqual(self.plugin._bambu_publish(
                "printer.local", "code", "device/serial/request", {}, printer=printer), "")
        context.wrap_socket.assert_called_once_with(raw, server_hostname="printer.local")

    def test_only_end_to_end_backends_are_active(self):
        self.assertEqual(self.plugin.backend_for_type("moonraker"), "moonraker")
        self.assertEqual(self.plugin.backend_for_type("octoprint"), "octoprint")
        self.assertEqual(self.plugin.backend_for_type("bambu"), "bambu")
        self.assertEqual(self.plugin.backend_for_type("prusalink"), "prusalink")
        self.assertEqual(self.plugin.backend_for_type("duet"), "duet")
        self.assertEqual(self.plugin.backend_for_type("repetier"), "repetier")
        self.assertTrue(self.plugin.backend_is_active("duet"))
        self.assertTrue(self.plugin.backend_is_active("prusalink"))
        self.assertTrue(self.plugin.backend_is_active("repetier"))

        status = self.plugin.poll_printer({
            "url": "http://printer",
            "type": "repetier",
        })
        self.assertFalse(status["online"])
        self.assertTrue(status["supported"])
        self.assertEqual(status["error_code"], "credential_unavailable")

    def test_planned_backends_are_visible_but_disabled(self):
        source = self.plugin.render_page()
        self.assertIn(
            '<option value="repetier">Repetier</option>',
            source,
        )
        self.assertIn('<option value="prusalink">PrusaLink</option>', source)
        self.assertIn('<option value="duet">Duet (RRF/DSF)</option>', source)
        self.assertEqual(
            self.plugin.UI_COPY["en"]["integrationUnavailable"],
            "Printer integration is not available yet",
        )

    def test_refresh_preserves_known_status_while_a_new_printer_checks(self):
        dashboard = self.plugin.PrintersDashboard()
        dashboard._post = mock.Mock(return_value=True)
        known = {"online": True, "state": "ready"}
        printers = [
            self.plugin._ensure_printer_identity({"url": "http://known", "name": "Known"}),
            self.plugin._ensure_printer_identity({"url": "http://new", "name": "New"}),
        ]
        dashboard._status_cache[self.plugin.printer_binding_key(printers[0])] = known

        with mock.patch.object(
            self.plugin,
            "dashboard_printers",
            return_value=printers,
        ), mock.patch.object(
            self.plugin,
            "attach_outbox",
            return_value=0,
        ), mock.patch.object(
            dashboard,
            "_poll_and_push",
        ):
            dashboard._refresh_worker([], False)

        payload = dashboard._post.call_args.args[0]
        self.assertEqual(payload["printers"][0]["status"], known)
        self.assertEqual(payload["printers"][1]["status"]["state"], "checking")

    def test_poll_batch_does_not_block_the_action_worker(self):
        source = inspect.getsource(self.plugin.PrintersDashboard._poll_and_push)
        self.assertNotIn(".wait(", source)
        completed = []
        batch = self.plugin.PollBatch(2, lambda: completed.append(True))
        batch.finish_one()
        self.assertEqual(completed, [])
        batch.finish_one()
        self.assertEqual(completed, [True])

    def test_busy_main_print_action_is_disabled_and_no_empty_control_slot(self):
        source = PLUGIN_PATH.read_text(encoding="utf-8")
        self.assertIn("var printAttrs = selectable", source)
        self.assertIn("t('printerBusy')", source)
        self.assertIn("function cardActionsHtml(parts)", source)
        self.assertIn("'<div class=\"ctl-row\">' + parts.history + (parts.tls || '') + parts.ctl + '</div>'", source)
        self.assertIn('class="ctl-slot"', source)
        # An empty control slot or a hidden spacer button would leave a hole.
        self.assertNotIn("ctl spacer", source)
        self.assertNotIn("open ghost spacer", source)

    def test_remove_is_quiet_in_the_header_and_requires_a_second_click(self):
        source = PLUGIN_PATH.read_text(encoding="utf-8")

        self.assertIn('.tile .remove { flex:0 0 auto; width:26px; height:26px;', source)
        self.assertIn('.tile .remove.armed { width:auto; min-width:58px;', source)
        self.assertIn('<span class="name-wrap"><span class="name"', source)
        self.assertIn('<button class="remove ghost"', source)
        self.assertLess(
            source.index('<button class="remove ghost"'),
            source.index('<span class="src">'),
        )
        self.assertIn("if (!removeBtn._armed)", source)
        self.assertIn("removeBtn.textContent = t('sure');", source)
        self.assertIn("setTimeout(resetRemove, 3000)", source)
        self.assertIn("removeBtn.addEventListener('blur'", source)
        self.assertNotIn(
            "tile.querySelector('.remove').addEventListener('click', function(){ orca.postMessage",
            source,
        )

    def test_card_actions_size_to_translated_text_and_wrap(self):
        source = PLUGIN_PATH.read_text(encoding="utf-8")

        self.assertIn('.ctl-row { display:flex; flex-wrap:wrap;', source)
        self.assertIn('.row { display:flex; flex-wrap:wrap;', source)
        self.assertIn('width:max-content; max-width:100%; white-space:normal;', source)
        self.assertIn('.row .print { min-width:72px; }', source)
        self.assertIn('.row .open { min-width:60px; }', source)
        self.assertIn('width:auto; min-width:126px; max-width:100%;', source)
        self.assertIn('width:max-content; min-width:60px;', source)
        self.assertNotIn('.row .print, .row .open { width:60px; }', source)
        self.assertNotIn('.ctl-slot button { width:60px;', source)

    def test_unavailable_select_control_is_visibly_muted(self):
        source = PLUGIN_PATH.read_text(encoding="utf-8")

        self.assertIn('.row label.select-control.disabled {', source)
        self.assertIn('opacity:.45; cursor:not-allowed;', source)
        self.assertIn('class="select-control\' + (selectable ? \'\' : \' disabled\')', source)
        self.assertIn('aria-disabled="true" title="', source)
        self.assertIn("esc(printDisabledReason)", source)

    def test_orca_language_drives_page_and_native_messages(self):
        with mock.patch.object(
            self.plugin.orca.host,
            "app_language",
            return_value="ru_RU",
            create=True,
        ):
            rendered = self.plugin.render_page()

        self.assertIn("var hostLanguage = 'ru';", rendered)
        self.assertIn("Отправить G-code на выбранные", rendered)
        self.assertEqual(
            self.plugin.ui_text("dashboardOpened"),
            "Панель Print Farm открыта.",
        )

    def test_all_orca_locales_are_preserved_with_per_key_english_fallback(self):
        for locale in self.plugin.ORCA_UI_LOCALES:
            self.assertEqual(self.plugin.normalize_ui_language(locale), locale)

        original = self.plugin.UI_COPY
        try:
            self.plugin.UI_COPY = {
                "en": {"shared": "English", "englishOnly": "Fallback"},
                "de": {"shared": "Deutsch"},
            }
            self.assertEqual(
                self.plugin.resolved_ui_catalog("de_DE"),
                {"shared": "Deutsch", "englishOnly": "Fallback"},
            )
            self.assertEqual(
                self.plugin.resolved_ui_catalog("pt_BR"),
                {"shared": "English", "englishOnly": "Fallback"},
            )
        finally:
            self.plugin.UI_COPY = original

    def test_bundled_locale_catalogs_are_valid(self):
        spec = importlib.util.spec_from_file_location(
            "printers_locale_validator_test", LOCALE_VALIDATOR_PATH
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        validator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(validator)

        self.assertEqual(validator.validate_catalogs(), [])

    def test_every_orca_locale_has_a_complete_catalog(self):
        locale_dir = PLUGIN_PATH.with_name("printers_locales")
        english_keys = set(json.loads(
            (locale_dir / "en.json").read_text(encoding="utf-8")
        ))

        self.assertEqual(
            {path.stem for path in locale_dir.glob("*.json")},
            set(self.plugin.ORCA_UI_LOCALES),
        )
        for locale in self.plugin.ORCA_UI_LOCALES:
            with self.subTest(locale=locale):
                catalog = json.loads(
                    (locale_dir / (locale + ".json")).read_text(encoding="utf-8")
                )
                self.assertEqual(set(catalog), english_keys)

    def test_mass_print_start_still_requires_an_idle_printer(self):
        dashboard = self.plugin.PrintersDashboard()
        dashboard._post = mock.Mock(return_value=True)
        printer = self.plugin._ensure_printer_identity({
            "name": "Busy", "url": "http://printer", "type": "moonraker",
        })
        identity = {"targetUuid": printer["target_uuid"], "ownerScope": printer["owner_scope_uuid"],
            "targetSelector": printer["target_selector"], "bindingRevision": printer["binding_revision"],
            "url": printer["url"], "connectionType": printer["type"]}
        artifact = types.SimpleNamespace(filename="job.gcode", path=pathlib.Path("job.gcode"),
            manifest={"kind": "gcode"}, metadata={"printer_settings_id": "Busy"})
        dashboard._received_artifacts["artifact-token"] = artifact
        message = {"artifactToken": "artifact-token", "targets": [identity],
                   "start": True, "_approved": True}
        with mock.patch.object(self.plugin, "dashboard_printers", return_value=[printer]), \
                mock.patch.object(self.plugin._pf_artifact, "verify_staged_artifact", return_value=True), \
                mock.patch.object(self.plugin, "poll_printer", return_value=_fresh_status(
                    self.plugin, "moonraker", "printing", "printing")), \
                mock.patch.object(self.plugin, "send_file_path") as send, \
                mock.patch.object(dashboard, "_refresh_worker"):
            dashboard._do_mass_print(message)
        send.assert_not_called()
        self.assertIn("busy", dashboard._post.call_args.args[0]["status"].lower())

    def test_mass_print_requires_explicit_boolean_start(self):
        dashboard = self.plugin.PrintersDashboard()
        dashboard._post = mock.Mock(return_value=True)
        printer = self.plugin._ensure_printer_identity({
            "name": "Target", "url": "http://printer", "type": "moonraker",
        })
        identity = {"targetUuid": printer["target_uuid"], "ownerScope": printer["owner_scope_uuid"],
            "targetSelector": printer["target_selector"], "bindingRevision": printer["binding_revision"],
            "url": printer["url"], "connectionType": printer["type"]}
        artifact = types.SimpleNamespace(filename="job.gcode", path=pathlib.Path("job.gcode"),
            manifest={"kind": "gcode", "sha256": hashlib.sha256(b"G28\n").hexdigest()},
            metadata={"printer_settings_id": "Target"})
        dashboard._received_artifacts["artifact-token"] = artifact
        message = {"artifactToken": "artifact-token", "targets": [identity], "start": "true"}
        with mock.patch.object(self.plugin, "dashboard_printers", return_value=[printer]), \
                mock.patch.object(self.plugin._pf_artifact, "verify_staged_artifact", return_value=True), \
                mock.patch.object(self.plugin, "poll_printer", return_value=_fresh_status(
                    self.plugin, "moonraker", "standby", "standby")), \
                mock.patch.object(self.plugin, "send_file_path", return_value={"ok": True}) as send, \
                mock.patch.object(dashboard, "_journal_begin", return_value=(mock.Mock(), "test-operation")), \
                mock.patch.object(dashboard, "_journal_finish"), \
                mock.patch.object(dashboard, "_refresh_worker"):
            dashboard._do_mass_print(message)
        self.assertTrue(send.called)
        self.assertFalse(send.call_args.args[3])


class ArtifactBridgeTest(unittest.TestCase):
    def _start_journal_operation(self, plugin, storage, printer, state):
        dashboard = plugin.PrintersDashboard()
        with mock.patch.object(plugin, "PLUGIN_STORAGE_DIR", str(storage)), \
                mock.patch.object(plugin, "_STORAGE_DENIED", False):
            digest = hashlib.sha256(b"fixture").hexdigest()
            dispatch = dashboard._journal_begin(printer, "start", artifact_sha256=digest,
                                                artifact_name="part.3mf")
            self.assertIsNotNone(dispatch)
            journal, operation_id = dispatch
            if state != "dispatching":
                journal.report(operation_id, state, actor="transport")
            journal.close()
        return dashboard, operation_id

    def test_history_lists_only_private_account_holds_and_release_makes_no_network_call(self):
        plugin = _load_plugin()
        printer = plugin._ensure_printer_identity({
            "name": "Printer A", "url": "http://printer-a", "type": "moonraker",
        })
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            dashboard, operation_id = self._start_journal_operation(
                plugin, first, printer, "unknown_outcome")
            dashboard._post = mock.Mock(return_value=True)
            with mock.patch.object(plugin, "PLUGIN_STORAGE_DIR", first), \
                    mock.patch.object(plugin, "_STORAGE_DENIED", False), \
                    mock.patch.object(plugin, "dashboard_printers", return_value=[printer]):
                dashboard._post_print_history_all({"requestId": 31})
                payload = dashboard._post.call_args.args[0]["printHistory"]
                self.assertEqual([hold["operation_id"] for hold in payload["holds"]], [operation_id])
                self.assertEqual(payload["holds"][0]["target_name"], "http://printer-a")
                with mock.patch.object(plugin, "PLUGIN_STORAGE_DIR", second):
                    dashboard._resolve_journal_hold({"operationId": operation_id, "requestId": 31})
                journal = plugin._pf_journal.OperationJournal(
                    pathlib.Path(first) / "operations.sqlite3")
                self.assertEqual(journal.get(operation_id)["state"], "unknown_outcome")
                journal.close()
                with mock.patch.object(plugin, "send_file_path") as send, \
                        mock.patch.object(plugin, "_bambu_send_file") as bambu_send:
                    dashboard._resolve_journal_hold({"operationId": operation_id, "requestId": 31})
                send.assert_not_called()
                bambu_send.assert_not_called()
                journal = plugin._pf_journal.OperationJournal(
                    pathlib.Path(first) / "operations.sqlite3")
                self.assertEqual(journal.get(operation_id)["state"], "manually_resolved")
                journal.close()

    def test_dispatching_hold_cannot_be_manually_released(self):
        plugin = _load_plugin()
        printer = plugin._ensure_printer_identity({
            "name": "Printer A", "url": "http://printer-a", "type": "moonraker",
        })
        with tempfile.TemporaryDirectory() as storage:
            dashboard, operation_id = self._start_journal_operation(
                plugin, storage, printer, "dispatching")
            dashboard._post = mock.Mock(return_value=True)
            with mock.patch.object(plugin, "PLUGIN_STORAGE_DIR", storage), \
                    mock.patch.object(plugin, "_STORAGE_DENIED", False), \
                    mock.patch.object(plugin, "send_file_path") as send:
                dashboard._resolve_journal_hold({"operationId": operation_id, "requestId": 32})
            send.assert_not_called()
            journal = plugin._pf_journal.OperationJournal(
                pathlib.Path(storage) / "operations.sqlite3")
            self.assertEqual(journal.get(operation_id)["state"], "dispatching")
            journal.close()

    def test_server_error_after_dispatch_keeps_outcome_unknown(self):
        plugin = _load_plugin()
        dashboard = plugin.PrintersDashboard()
        store = types.SimpleNamespace(report=mock.Mock(), close=mock.Mock())
        dashboard._journal_finish((store, "operation"), {
            "ok": False, "error_code": "server_error", "evidence_ref": "http:500",
        })
        store.report.assert_called_once_with("operation", "unknown_outcome", actor="transport")
        store.close.assert_called_once()

    def test_explicit_auth_rejection_is_not_unknown(self):
        plugin = _load_plugin()
        dashboard = plugin.PrintersDashboard()
        store = types.SimpleNamespace(report=mock.Mock(), close=mock.Mock())
        dashboard._journal_finish((store, "operation"), {
            "ok": False, "error_code": "authorization_denied", "evidence_ref": "http:403",
        })
        store.report.assert_called_once_with("operation", "rejected", evidence_ref="http:403")
        store.close.assert_called_once()

    def test_single_file_release_embeds_artifact_helpers(self):
        spec = importlib.util.spec_from_file_location("print_farm_builder_for_test", BUILD_PATH)
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        source = PLUGIN_PATH.read_text(encoding="utf-8")
        release_source = builder._source_with_embedded_helpers(
            builder._source_with_embedded_locales(source)
        )
        self.assertNotIn("_pf_load_local_helper", release_source)
        with tempfile.TemporaryDirectory() as temp:
            release_path = pathlib.Path(temp) / "printers_plugin.py"
            release_path.write_text(release_source, encoding="utf-8")
            with mock.patch.dict(globals(), {"PLUGIN_PATH": release_path}):
                packaged = _load_plugin()
            self.assertTrue(callable(packaged._pf_artifact.inspect_staged_path))
            self.assertTrue(callable(packaged._pf_transfer.ArtifactTransferManager))
            self.assertTrue(callable(packaged._pf_upload.post_multipart_file))

    def test_webview_chunks_are_staged_and_sent_by_path(self):
        plugin = _load_plugin()
        dashboard = plugin.PrintersDashboard()
        dashboard._post = mock.Mock(return_value=True)
        printer = plugin._ensure_printer_identity({
            "name": "Target", "url": "http://printer", "type": "moonraker",
        })
        identity = {"targetUuid": printer["target_uuid"], "ownerScope": printer["owner_scope_uuid"],
            "targetSelector": printer["target_selector"], "bindingRevision": printer["binding_revision"],
            "url": printer["url"], "connectionType": printer["type"]}
        content = b"G28\n; printer_settings_id = Target\n"
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(plugin, "PLUGIN_STORAGE_DIR", temp), \
                mock.patch.object(plugin, "_STORAGE_DENIED", False):
            dashboard._do_artifact_begin({"filename": "job.gcode", "size": len(content)})
            token = dashboard._post.call_args.args[0]["artifactBegin"]["token"]
            dashboard._do_artifact_chunk({
                "token": token, "sequence": 0,
                "dataB64": __import__("base64").b64encode(content).decode("ascii"),
                "sha256": hashlib.sha256(content).hexdigest(),
            })
            self.assertEqual(dashboard._post.call_args.args[0]["artifactChunk"]["sequence"], 0)
            dashboard._do_artifact_finish({"token": token})
            self.assertEqual(dashboard._post.call_args.args[0]["artifactReady"]["plates"], [])
            artifact = dashboard._received_artifacts[token]
            self.assertEqual(artifact.path.read_bytes(), content)
            with mock.patch.object(plugin, "dashboard_printers", return_value=[printer]), \
                    mock.patch.object(plugin, "poll_printer", return_value=_fresh_status(
                        plugin, "moonraker", "standby", "standby")), \
                    mock.patch.object(plugin, "send_file_path", return_value={"ok": True}) as send, \
                    mock.patch.object(dashboard, "_refresh_worker"):
                dashboard._do_mass_print({"artifactToken": token, "targets": [identity],
                                          "start": False})
            self.assertEqual(send.call_args.args[2], artifact.path)
            self.assertFalse(send.call_args.args[3])
            self.assertNotIn(token, dashboard._received_artifacts)
            os.chmod(artifact.path, 0o600)

    def test_mass_upload_cleans_staged_copy_and_rejects_start_without_compatibility(self):
        plugin = _load_plugin()
        dashboard = plugin.PrintersDashboard()
        dashboard._post = mock.Mock(return_value=True)
        printer = plugin._ensure_printer_identity({
            "name": "Target", "url": "http://printer.local", "type": "moonraker",
        })
        identity = {"targetUuid": printer["target_uuid"],
                    "ownerScope": printer["owner_scope_uuid"],
                    "targetSelector": printer["target_selector"],
                    "bindingRevision": printer["binding_revision"],
                    "url": printer["url"], "connectionType": printer["type"]}
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(plugin, "PLUGIN_STORAGE_DIR", temp), \
                mock.patch.object(plugin, "_STORAGE_DENIED", False), \
                mock.patch.object(plugin, "dashboard_printers", return_value=[printer]), \
                mock.patch.object(dashboard, "_do_mass_print", return_value=True) as send:
            incoming = pathlib.Path(temp) / "incoming"
            incoming.mkdir()
            staged = incoming / ("pf-artifact-" + "a" * 32 + ".gcode")
            staged.write_bytes(b"G28\n; printer_settings_id = Target\n")
            artifact = plugin._pf_artifact.inspect_staged_path(
                staged, "job.gcode", incoming,
                expected_sha256=hashlib.sha256(staged.read_bytes()).hexdigest(),
            )
            dashboard._received_artifacts["token"] = artifact
            request = {"artifactToken": "token", "targets": [identity],
                       "plate": None, "start": False, "use_ams": True}
            try:
                dashboard._submit_mass_print({**request, "start": True})
                send.assert_not_called()
                dashboard._submit_mass_print(request)
                self.assertTrue(dashboard._action_scheduler.wait_for_idle(3))
                self.assertEqual(send.call_count, 1)
                self.assertFalse(send.call_args.args[0]["_approved"])
                self.assertFalse(staged.exists())
                failed = incoming / ("pf-artifact-" + "b" * 32 + ".gcode")
                failed.write_bytes(b"G28\n")
                dashboard._received_artifacts["token-fail"] = (
                    plugin._pf_artifact.inspect_staged_path(
                        failed, "failed.gcode", incoming,
                        expected_sha256=hashlib.sha256(failed.read_bytes()).hexdigest(),
                    )
                )
                send.return_value = False
                dashboard._submit_mass_print({**request, "artifactToken": "token-fail"})
                self.assertTrue(dashboard._action_scheduler.wait_for_idle(3))
                self.assertFalse(failed.exists())
                abandoned = incoming / ("pf-artifact-" + "c" * 32 + ".gcode")
                abandoned.write_bytes(b"G28\n")
                dashboard._received_artifacts["token-close"] = (
                    plugin._pf_artifact.inspect_staged_path(
                        abandoned, "abandoned.gcode", incoming,
                        expected_sha256=hashlib.sha256(abandoned.read_bytes()).hexdigest(),
                    )
                )
                dashboard.on_close()
                self.assertFalse(abandoned.exists())
            finally:
                dashboard._stop_action_scheduler()

    def test_cancel_disposes_only_the_prepared_copy(self):
        plugin = _load_plugin()
        dashboard = plugin.PrintersDashboard()
        dashboard._post = mock.Mock(return_value=True)
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(plugin, "PLUGIN_STORAGE_DIR", temp), \
                mock.patch.object(plugin, "_STORAGE_DENIED", False):
            original = pathlib.Path(temp) / "original.gcode"
            original.write_bytes(b"G28\n")
            incoming = pathlib.Path(temp) / "incoming"
            artifact = plugin._pf_artifact.stage_artifact(original, incoming)
            dashboard._received_artifacts["token"] = artifact
            dashboard._do_artifact_discard({"token": "token"})
            self.assertTrue(original.exists())
            self.assertFalse(artifact.path.exists())
            self.assertNotIn("token", dashboard._received_artifacts)

    def test_same_staged_token_cannot_enter_two_upload_batches(self):
        plugin = _load_plugin()
        dashboard = plugin.PrintersDashboard()
        dashboard._post = mock.Mock(return_value=True)
        printer = plugin._ensure_printer_identity({
            "name": "Target", "url": "http://printer.local", "type": "moonraker",
        })
        identity = {"targetUuid": printer["target_uuid"],
                    "ownerScope": printer["owner_scope_uuid"],
                    "targetSelector": printer["target_selector"],
                    "bindingRevision": printer["binding_revision"],
                    "url": printer["url"], "connectionType": printer["type"]}
        entered = threading.Event()
        release = threading.Event()

        def blocked_send(_message):
            entered.set()
            self.assertTrue(release.wait(3))
            return True

        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(plugin, "PLUGIN_STORAGE_DIR", temp), \
                mock.patch.object(plugin, "_STORAGE_DENIED", False), \
                mock.patch.object(plugin, "dashboard_printers", return_value=[printer]), \
                mock.patch.object(dashboard, "_do_mass_print", side_effect=blocked_send) as send:
            incoming = pathlib.Path(temp) / "incoming"
            incoming.mkdir()
            staged = incoming / ("pf-artifact-" + "d" * 32 + ".gcode")
            staged.write_bytes(b"G28\n")
            dashboard._received_artifacts["token"] = (
                plugin._pf_artifact.inspect_staged_path(
                    staged, "job.gcode", incoming,
                    expected_sha256=hashlib.sha256(staged.read_bytes()).hexdigest(),
                )
            )
            message = {"artifactToken": "token", "targets": [identity],
                       "start": False}
            try:
                dashboard._submit_mass_print(message)
                self.assertTrue(entered.wait(2))
                dashboard._submit_mass_print(message)
                self.assertEqual(send.call_count, 1)
                self.assertTrue(staged.exists())
            finally:
                release.set()
                if dashboard._action_scheduler is not None:
                    dashboard._action_scheduler.wait_for_idle(3)
                dashboard._stop_action_scheduler()
            self.assertFalse(staged.exists())

    def test_outbox_start_blocks_and_upload_retains_source(self):
        plugin = _load_plugin()
        dashboard = plugin.PrintersDashboard()
        dashboard._post = mock.Mock(return_value=True)
        printer = plugin._ensure_printer_identity({
            "name": "Renamed card", "url": "http://printer.local",
            "type": "moonraker",
        })
        identity = {"targetUuid": printer["target_uuid"],
                    "ownerScope": printer["owner_scope_uuid"],
                    "targetSelector": printer["target_selector"],
                    "bindingRevision": printer["binding_revision"],
                    "url": printer["url"], "connectionType": printer["type"]}
        content = b"G28\n; printer_settings_id = Original profile\n"
        stored = printer["target_uuid"] + "/fixture.gcode"
        record = {"file": stored, "name": "fixture.gcode",
                  "sha256": hashlib.sha256(content).hexdigest(),
                  "target_uuid": printer["target_uuid"],
                  "owner_scope_uuid": printer["owner_scope_uuid"],
                  "target_selector": printer["target_selector"],
                  "binding_revision": printer["binding_revision"]}
        with tempfile.TemporaryDirectory() as temp:
            (pathlib.Path(temp) / printer["target_uuid"]).mkdir()
            source = pathlib.Path(temp) / stored
            source.write_bytes(content)
            with mock.patch.object(plugin, "OUTBOX_DIR", temp), \
                    mock.patch.object(plugin, "dashboard_printers", return_value=[printer]), \
                    mock.patch.object(plugin, "load_outbox_index", return_value=[record]), \
                    mock.patch.object(plugin, "poll_printer", return_value=_fresh_status(
                        plugin, "moonraker", "standby", "standby")), \
                    mock.patch.object(plugin, "send_file_path", return_value={"ok": True}) as send, \
                    mock.patch.object(dashboard, "_journal_begin", return_value=(mock.Mock(), "op")), \
                    mock.patch.object(dashboard, "_journal_finish"), \
                    mock.patch.object(dashboard, "_refresh_worker"), \
                    mock.patch.object(plugin, "outbox_remove") as remove:
                try:
                    dashboard._do_outbox_send({**identity, "file": stored,
                                              "start": True})
                    send.assert_not_called()
                    dashboard._do_outbox_send({**identity, "file": stored,
                                              "start": False})
                finally:
                    dashboard._stop_action_scheduler()
            self.assertEqual(send.call_count, 1)
            self.assertTrue(source.exists())
            remove.assert_not_called()

    def test_runtime_journal_unknown_outcome_blocks_duplicate_network_dispatch(self):
        plugin = _load_plugin()
        dashboard = plugin.PrintersDashboard()
        dashboard._post = mock.Mock(return_value=True)
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(plugin, "PLUGIN_STORAGE_DIR", temp), \
                mock.patch.object(plugin, "_STORAGE_DENIED", False):
            printer = plugin._ensure_printer_identity({
                "name": "Target", "url": "http://printer.local",
                "type": "moonraker", "apikey": "private-api-key",
            })
            digest = hashlib.sha256(b"artifact").hexdigest()
            identity = {"targetUuid": printer["target_uuid"],
                        "ownerScope": printer["owner_scope_uuid"],
                        "targetSelector": printer["target_selector"],
                        "bindingRevision": printer["binding_revision"],
                        "url": printer["url"], "connectionType": printer["type"]}
            artifact = types.SimpleNamespace(
                filename="job.gcode", path=pathlib.Path("job.gcode"),
                manifest={"kind": "gcode", "sha256": digest}, sha256=digest,
                metadata={"printer_settings_id": "Target"},
            )
            message = {"artifactToken": "artifact-token", "targets": [identity],
                       "start": False}
            dashboard._received_artifacts["artifact-token"] = artifact
            with mock.patch.object(plugin, "dashboard_printers", return_value=[printer]), \
                    mock.patch.object(plugin._pf_artifact, "verify_staged_artifact", return_value=True), \
                    mock.patch.object(plugin, "poll_printer", return_value=_fresh_status(
                        plugin, "moonraker", "standby", "standby")), \
                    mock.patch.object(plugin, "send_file_path", return_value={
                        "ok": False, "error_code": "unknown_outcome",
                    }) as send, \
                    mock.patch.object(dashboard, "_refresh_worker"):
                dashboard._do_mass_print(message)
                dashboard._received_artifacts["artifact-token"] = artifact
                dashboard._do_mass_print(message)
            self.assertEqual(send.call_count, 1)

            journal = plugin._pf_journal.OperationJournal(
                pathlib.Path(temp) / "operations.sqlite3")
            operations = journal.list_operations(
                hashlib.sha256(os.path.realpath(temp).encode("utf-8")).hexdigest())
            journal.close()
            self.assertEqual(len(operations), 1)
            self.assertEqual(operations[0]["state"], "unknown_outcome")
            self.assertEqual(operations[0]["artifact_sha256"], digest)
            binding = json.dumps(operations[0]["target_binding"], sort_keys=True)
            self.assertNotIn("private-api-key", binding)
            self.assertEqual(operations[0]["target_binding"]["target_uuid"], printer["target_uuid"])

    def test_journal_failure_fails_closed_before_mass_print_transport(self):
        plugin = _load_plugin()
        dashboard = plugin.PrintersDashboard()
        dashboard._post = mock.Mock(return_value=True)
        printer = plugin._ensure_printer_identity({
            "name": "Target", "url": "http://printer", "type": "moonraker",
        })
        identity = {"targetUuid": printer["target_uuid"], "ownerScope": printer["owner_scope_uuid"],
                    "targetSelector": printer["target_selector"], "bindingRevision": printer["binding_revision"],
                    "url": printer["url"], "connectionType": printer["type"]}
        artifact = types.SimpleNamespace(
            filename="job.gcode", path=pathlib.Path("job.gcode"),
            manifest={"kind": "gcode", "sha256": hashlib.sha256(b"job").hexdigest()},
            sha256=hashlib.sha256(b"job").hexdigest(), metadata={"printer_settings_id": "Target"},
        )
        dashboard._received_artifacts["artifact-token"] = artifact
        with mock.patch.object(plugin, "PLUGIN_STORAGE_DIR", ""), \
                mock.patch.object(plugin, "_STORAGE_DENIED", False), \
                mock.patch.object(plugin, "dashboard_printers", return_value=[printer]), \
                mock.patch.object(plugin._pf_artifact, "verify_staged_artifact", return_value=True), \
                mock.patch.object(plugin, "poll_printer", return_value=_fresh_status(
                    plugin, "moonraker", "standby", "standby")), \
                mock.patch.object(plugin, "send_file_path") as send, \
                mock.patch.object(dashboard, "_refresh_worker"):
            dashboard._do_mass_print({"artifactToken": "artifact-token", "targets": [identity],
                                      "start": False})
        send.assert_not_called()


class PrintHistoryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin = _load_plugin()

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.plugin.PRINT_HISTORY_FILE = str(
            pathlib.Path(self.temp_dir.name) / "print_history.json"
        )
        self.printer = {
            "id": 7,
            "name": "Voron",
            "url": "http://voron.local",
            "type": "moonraker",
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_observed_job_keeps_one_record_across_pause_and_completion(self):
        self.assertTrue(self.plugin.record_print_observation(
            self.printer,
            {
                "state": "printing",
                "filename": "parts/cube.gcode",
                "progress": 25,
                "elapsed_seconds": 30,
            },
            now=1_000,
        ))
        self.assertTrue(self.plugin.record_print_observation(
            self.printer,
            {"state": "paused", "filename": "cube.gcode", "progress": 50},
            now=1_060,
        ))
        self.assertTrue(self.plugin.record_print_observation(
            self.printer,
            {
                "state": "complete",
                "filename": "cube.gcode",
                "progress": 100,
                "elapsed_seconds": 120,
            },
            now=1_120,
        ))

        history = self.plugin.print_history_for_printer(self.printer)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["filename"], "cube.gcode")
        self.assertEqual(history[0]["state"], "completed")
        self.assertEqual(history[0]["progress"], 100)
        self.assertEqual(history[0]["duration_seconds"], 150)
        self.assertEqual(history[0]["source"], "observed")

    def test_partial_job_returning_to_idle_is_ended_not_claimed_complete(self):
        self.plugin.record_print_observation(
            self.printer,
            {"state": "printing", "filename": "fixture.gcode", "progress": 30},
            now=2_000,
        )
        self.plugin.record_print_observation(
            self.printer,
            {"state": "standby", "filename": "fixture.gcode", "progress": 30},
            now=2_100,
        )

        self.assertEqual(
            self.plugin.print_history_for_printer(self.printer)[0]["state"],
            "ended",
        )

    def test_repeated_terminal_snapshot_does_not_duplicate_history(self):
        terminal = {
            "state": "finished",
            "filename": "fixture.gcode",
            "progress": 100,
            "elapsed_seconds": 90,
        }
        self.plugin.record_print_observation(self.printer, terminal, now=3_000)
        first = dict(self.plugin.print_history_for_printer(self.printer)[0])
        self.plugin.record_print_observation(self.printer, terminal, now=3_015)

        history = self.plugin.print_history_for_printer(self.printer)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["finished_at"], first["finished_at"])
        self.assertEqual(history[0]["duration_seconds"], first["duration_seconds"])

    def test_print_farm_start_is_persisted_before_the_first_poll(self):
        record = self.plugin.record_print_started(
            self.printer,
            "fresh.gcode",
            now=4_000,
        )

        self.assertEqual(record["source"], "print_farm")
        self.assertEqual(record["state"], "printing")
        self.assertEqual(
            self.plugin.print_history_for_printer(self.printer)[0]["filename"],
            "fresh.gcode",
        )

    def test_accepted_start_ignores_an_immediate_stale_idle_poll(self):
        self.plugin.record_print_started(self.printer, "fresh.gcode", now=4_000)

        self.assertFalse(self.plugin.record_print_observation(
            self.printer,
            {"state": "idle", "filename": "", "progress": 0},
            now=4_010,
        ))

        record = self.plugin.print_history_for_printer(self.printer)[0]
        self.assertEqual(record["state"], "printing")
        self.assertIsNone(record["finished_at"])

    def test_malformed_numeric_fields_are_sanitized_when_history_is_loaded(self):
        pathlib.Path(self.plugin.PRINT_HISTORY_FILE).write_text(
            json.dumps([{
                "id": "broken",
                "printer_key": "url:http://voron.local",
                "state": "printing",
                "started_at": "not-a-time",
                "updated_at": {},
                "duration_seconds": "forever",
                "progress": "lots",
            }]),
            encoding="utf-8",
        )

        record = self.plugin.load_print_history()[0]

        self.assertEqual(record["started_at"], 0)
        self.assertEqual(record["updated_at"], 0)
        self.assertEqual(record["duration_seconds"], 0)
        self.assertIsNone(record["progress"])

    def test_reused_numeric_id_does_not_inherit_another_printer_history(self):
        self.plugin.record_print_started(self.printer, "voron.gcode", now=4_000)
        replacement = dict(self.printer, url="http://replacement.local")

        self.assertEqual(self.plugin.print_history_for_printer(replacement), [])

    def test_dashboard_pushes_only_the_requested_printer_history(self):
        dashboard = self.plugin.PrintersDashboard()
        dashboard._post = mock.Mock(return_value=True)
        self.printer = self.plugin._ensure_printer_identity(self.printer)
        other = {
            "id": 8,
            "name": "Other",
            "url": "http://other.local",
            "type": "octoprint",
        }
        self.plugin.record_print_started(self.printer, "voron.gcode", now=5_000)
        self.plugin.record_print_started(other, "other.gcode", now=5_001)

        with mock.patch.object(
            self.plugin,
            "dashboard_printers",
            return_value=[self.printer, other],
        ):
            dashboard._post_print_history({
                "targetUuid": self.printer["target_uuid"],
                "ownerScope": self.printer["owner_scope_uuid"],
                "targetSelector": self.printer["target_selector"],
                "bindingRevision": self.printer["binding_revision"],
                "url": self.printer["url"],
                "connectionType": self.printer["type"],
            })

        payload = dashboard._post.call_args.args[0]["printHistory"]
        self.assertEqual(payload["mode"], "printer")
        self.assertEqual(payload["printerId"], 7)
        self.assertEqual([item["filename"] for item in payload["items"]], ["voron.gcode"])
        self.assertNotIn("printer_key", payload["items"][0])
        self.assertNotIn("target_uuid", payload["items"][0])

    def test_global_history_returns_bounded_summary_and_minimized_items(self):
        pathlib.Path(self.plugin.PRINT_HISTORY_FILE).write_text(json.dumps([
            {
                "id": "record-1", "printer_key": "private-key", "printer_name": "Voron",
                "filename": "cube.gcode", "state": "completed", "started_at": 5000,
                "updated_at": 5100, "finished_at": 5100, "duration_seconds": 100,
                "progress": 100, "source": "observed", "apikey": "secret",
                "access_code": "also-secret", "legacy_blob": {"credential": "hidden"},
            },
            {
                "id": "record-2", "printer_key": "another-private-key", "printer_name": "Other",
                "filename": "benchy.gcode", "state": "paused", "started_at": 4000,
                "duration_seconds": 30, "progress": 20,
            },
        ]), encoding="utf-8")
        dashboard = self.plugin.PrintersDashboard()
        dashboard._post = mock.Mock(return_value=True)

        dashboard._post_print_history_all({"requestId": 17})

        payload = dashboard._post.call_args.args[0]["printHistory"]
        self.assertEqual(payload["mode"], "all")
        self.assertEqual(payload["requestId"], 17)
        self.assertTrue(payload["available"])
        self.assertEqual(payload["counts"], {"all": 2, "completed": 1, "active": 1})
        self.assertEqual(payload["items"][0]["printer_name"], "Voron")
        self.assertEqual(payload["items"][0]["filename"], "cube.gcode")
        self.assertNotIn("id", payload["items"][0])
        self.assertNotIn("printer_key", payload["items"][0])
        self.assertNotIn("apikey", payload["items"][0])
        self.assertNotIn("access_code", payload["items"][0])
        self.assertNotIn("legacy_blob", payload["items"][0])

    def test_global_history_marks_corrupt_store_unavailable_not_empty(self):
        pathlib.Path(self.plugin.PRINT_HISTORY_FILE).write_text("{corrupt", encoding="utf-8")
        dashboard = self.plugin.PrintersDashboard()
        dashboard._post = mock.Mock(return_value=True)

        dashboard._post_print_history_all({"requestId": 18})

        payload = dashboard._post.call_args.args[0]["printHistory"]
        self.assertFalse(payload["available"])
        self.assertEqual(payload["items"], [])
        self.assertEqual(payload["counts"], {"all": 0, "completed": 0, "active": 0})

    def test_printer_history_marks_corrupt_store_unavailable(self):
        pathlib.Path(self.plugin.PRINT_HISTORY_FILE).write_text("{corrupt", encoding="utf-8")
        dashboard = self.plugin.PrintersDashboard()
        dashboard._post = mock.Mock(return_value=True)
        printer = self.plugin._ensure_printer_identity(self.printer)
        with mock.patch.object(self.plugin, "dashboard_printers", return_value=[printer]):
            dashboard._post_print_history({
                "targetUuid": printer["target_uuid"],
                "ownerScope": printer["owner_scope_uuid"],
                "targetSelector": printer["target_selector"],
                "bindingRevision": printer["binding_revision"],
                "url": printer["url"],
                "connectionType": printer["type"],
            })
        payload = dashboard._post.call_args.args[0]["printHistory"]
        self.assertFalse(payload["available"])
        self.assertEqual(payload["items"], [])

    def test_global_history_message_is_routed_to_action_worker(self):
        dashboard = self.plugin.PrintersDashboard()
        message = {"type": "print-history-all", "requestId": 19}
        with mock.patch.object(self.plugin.ACTION_WORKER, "submit") as submit:
            dashboard.on_message(message)
        submit.assert_called_once()
        self.assertEqual(submit.call_args.args[0].__wrapped__, dashboard._post_print_history_all)
        self.assertEqual(submit.call_args.args[1:], (message,))

    def test_history_dialog_has_global_search_keyboard_and_late_response_guards(self):
        source = PLUGIN_PATH.read_text(encoding="utf-8")
        for marker in (
            'id="journal-open"', 'id="journal-search"', 'id="history-results"',
            'aria-modal="true"', "print-history-all", "event.key === 'Escape'",
            "payload.requestId !== historyExpected.requestId", "journalNoResults",
            "journalStoreUnavailable", "journalResultsCount",
        ):
            self.assertIn(marker, source)

    def test_history_button_is_per_printer_and_explains_observation_scope(self):
        source = PLUGIN_PATH.read_text(encoding="utf-8")
        self.assertIn('class="history ghost"', source)
        self.assertIn("openPrintHistory(p)", source)
        self.assertIn("text('history-note', 'historyCoverage')", source)


class LiveActionSchedulerIntegrationTest(unittest.TestCase):
    def test_cancel_for_b_runs_while_upload_for_a_is_blocked(self):
        plugin = _load_plugin(lifecycle=True)
        dashboard = plugin.PrintersDashboard()
        a = plugin._ensure_printer_identity({
            "name": "Printer A", "url": "http://printer-a", "type": "moonraker",
        })
        b = plugin._ensure_printer_identity({
            "name": "Printer B", "url": "http://printer-b", "type": "moonraker",
        }, a["owner_scope_uuid"])
        a_identity = {"targetUuid": a["target_uuid"], "ownerScope": a["owner_scope_uuid"],
                      "targetSelector": a["target_selector"], "bindingRevision": a["binding_revision"],
                      "url": a["url"], "connectionType": a["type"]}
        b_identity = {"targetUuid": b["target_uuid"], "ownerScope": b["owner_scope_uuid"],
                      "targetSelector": b["target_selector"], "bindingRevision": b["binding_revision"],
                      "url": b["url"], "connectionType": b["type"]}
        record = {"file": "fixture.gcode", "name": "fixture.gcode",
                  "target_uuid": a["target_uuid"], "owner_scope_uuid": a["owner_scope_uuid"],
                  "target_selector": a["target_selector"], "binding_revision": a["binding_revision"]}
        upload_started = threading.Event()
        release_upload = threading.Event()
        cancel_finished = threading.Event()

        def send_path(printer, *args, **kwargs):
            if printer["target_uuid"] == a["target_uuid"]:
                upload_started.set()
                release_upload.wait(3)
                return {"ok": True}
            raise AssertionError("unexpected upload target")

        def command(printer, action, **kwargs):
            if printer["target_uuid"] == b["target_uuid"] and action == "cancel":
                cancel_finished.set()
                return {"ok": True}
            raise AssertionError("unexpected printer command")

        with tempfile.TemporaryDirectory() as temp:
            plugin.OUTBOX_DIR = temp
            pathlib.Path(temp, "fixture.gcode").write_bytes(b"G28\n")
            status_a = _fresh_status(plugin, "moonraker", "standby", "standby")
            status_b = _fresh_status(plugin, "moonraker", "printing", "printing")
            with mock.patch.object(plugin, "configure_plugin_storage", return_value=True), \
                 mock.patch.object(plugin, "dashboard_printers", return_value=[a, b]), \
                 mock.patch.object(plugin, "load_outbox_index", return_value=[record]), \
                 mock.patch.object(plugin, "poll_printer", side_effect=lambda p: status_a if p["target_uuid"] == a["target_uuid"] else status_b), \
                 mock.patch.object(plugin, "send_file_path", side_effect=send_path), \
                 mock.patch.object(plugin, "printer_command", side_effect=command), \
                 mock.patch.object(dashboard, "_journal_begin", return_value=(object(), "operation")), \
                 mock.patch.object(dashboard, "_journal_finish"), \
                 mock.patch.object(plugin, "outbox_remove") as remove, \
                 mock.patch.object(dashboard, "_post") as post, \
                 mock.patch.object(dashboard, "_refresh_worker"), \
                 mock.patch.object(dashboard, "_poll_and_push"):
                dashboard.on_load()
                try:
                    dashboard.on_message({"type": "outbox-send", "file": record["file"],
                                          **a_identity, "start": False})
                    self.assertTrue(upload_started.wait(2))
                    dashboard.on_message({"type": "printer-cmd", **b_identity,
                                          "action": "cancel"})
                    self.assertTrue(cancel_finished.wait(2),
                                    "printer B cancel was blocked by printer A upload")
                    post.reset_mock()
                    scheduler = dashboard._action_scheduler
                    dashboard.on_unload()
                    release_upload.set()
                    self.assertTrue(scheduler.wait_for_idle(3))
                    remove.assert_not_called()
                    post.assert_not_called()
                finally:
                    release_upload.set()
                    dashboard.on_unload()


class StartCompatibilityTest(unittest.TestCase):
    """Start approval: verdict per target, operator checks, single-use nonces."""

    PRESET = "Voron 2.4 350"

    def setUp(self):
        self.plugin = _load_plugin()
        self.dashboard = self.plugin.PrintersDashboard()
        self.dashboard._post = mock.Mock(return_value=True)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        patches = [
            mock.patch.object(self.plugin, "PLUGIN_STORAGE_DIR", self.temp.name),
            mock.patch.object(self.plugin, "_STORAGE_DENIED", False),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.dashboard._stop_action_scheduler)

    def adopted(self, name="Renamed card", preset=None, url="http://voron.local",
                host_type="moonraker"):
        preset = preset or self.PRESET
        return self.plugin._ensure_printer_identity({
            "name": name, "url": url, "type": host_type, "source": "orca",
            "discovery_ref": self.plugin._orca_discovery_ref(preset, url, host_type),
        })

    def manual(self, name="Manual", url="http://manual.local", host_type="moonraker"):
        return self.plugin._ensure_printer_identity({
            "name": name, "url": url, "type": host_type, "source": "manual",
        })

    @staticmethod
    def identity(printer):
        return {"targetUuid": printer["target_uuid"],
                "ownerScope": printer["owner_scope_uuid"],
                "targetSelector": printer["target_selector"],
                "bindingRevision": printer["binding_revision"],
                "url": printer["url"], "connectionType": printer["type"]}

    def stage_gcode(self, profile=None, token="token", suffix="a"):
        incoming = self.root / "incoming"
        incoming.mkdir(exist_ok=True)
        staged = incoming / ("pf-artifact-" + suffix * 32 + ".gcode")
        body = b"G28\n"
        if profile:
            body += ("; printer_settings_id = %s\n" % profile).encode()
        staged.write_bytes(body)
        artifact = self.plugin._pf_artifact.inspect_staged_path(
            staged, "job.gcode", incoming,
            expected_sha256=hashlib.sha256(body).hexdigest(),
        )
        self.dashboard._received_artifacts[token] = artifact
        return artifact

    def stage_3mf(self, plates=(1, 3), profile=None, token="token3", suffix="b"):
        incoming = self.root / "incoming"
        incoming.mkdir(exist_ok=True)
        staged = incoming / ("pf-artifact-" + suffix * 32 + ".3mf")
        with zipfile.ZipFile(staged, "w") as archive:
            archive.writestr("3D/3dmodel.model", "<model/>")
            for number in plates:
                archive.writestr("Metadata/plate_%d.gcode" % number, "G28 ; plate %d\n" % number)
            if profile:
                archive.writestr("Metadata/project_settings.config",
                                 json.dumps({"printer_settings_id": profile}))
        artifact = self.plugin._pf_artifact.inspect_staged_path(
            staged, "job.gcode.3mf", incoming,
            expected_sha256=hashlib.sha256(staged.read_bytes()).hexdigest(),
        )
        self.dashboard._received_artifacts[token] = artifact
        return artifact

    def statuses(self):
        return [call.args[0]["status"] for call in self.dashboard._post.call_args_list
                if "status" in call.args[0]]

    def prepared(self):
        return next(call.args[0]["massStartApprovals"]
                    for call in reversed(self.dashboard._post.call_args_list)
                    if "massStartApprovals" in call.args[0])

    # --- pure verdicts ------------------------------------------------------
    def test_verdict_is_compatible_for_the_adopted_preset_even_after_a_card_rename(self):
        artifact = self.stage_gcode(profile=self.PRESET)
        printer = self.adopted(name="Totally different card name")
        self.assertEqual(self.plugin.start_compatibility(printer, artifact, None),
                         ("compatible", "startReasonProfileMatch"))

    def test_verdict_is_unknown_without_reliable_target_data(self):
        artifact = self.stage_gcode(profile=self.PRESET)
        # Manual card with the same name: names are never evidence.
        manual = self.manual(name=self.PRESET)
        self.assertEqual(self.plugin.start_compatibility(manual, artifact, None)[0], "unknown")
        # Adopted from a different preset.
        other = self.adopted(preset="Another preset")
        self.assertEqual(self.plugin.start_compatibility(other, artifact, None)[0], "unknown")
        # Adopted preset, but the address changed afterwards.
        moved = dict(self.adopted(), url="http://elsewhere.local")
        self.assertEqual(self.plugin.start_compatibility(moved, artifact, None)[0], "unknown")
        # File without printer profile data.
        bare = self.stage_gcode(profile=None, token="bare", suffix="c")
        self.assertEqual(self.plugin.start_compatibility(self.adopted(), bare, None)[0], "unknown")

    def test_verdict_is_incompatible_for_provable_mismatches(self):
        fake = types.SimpleNamespace
        project = fake(manifest={"kind": "sliced_3mf"}, metadata={}, plate_entries=())
        self.assertEqual(self.plugin.start_compatibility(self.manual(), project, "Metadata/plate_1.gcode"),
                         ("incompatible", "startReasonProject"))
        gcode = self.stage_gcode(profile=self.PRESET)
        bambu = self.manual(host_type="bambu")
        self.assertEqual(self.plugin.start_compatibility(bambu, gcode, None),
                         ("incompatible", "startReasonSlicedOnly"))
        self.assertEqual(self.plugin.start_compatibility(self.manual(), gcode, "Metadata/plate_1.gcode"),
                         ("incompatible", "startReasonPlate"))
        sliced = self.stage_3mf()
        self.assertEqual(self.plugin.start_compatibility(self.manual(), sliced, None),
                         ("incompatible", "startReasonPlate"))
        self.assertEqual(
            self.plugin.start_compatibility(self.manual(), sliced, "Metadata/plate_2.gcode"),
            ("incompatible", "startReasonPlate"))
        self.assertEqual(
            self.plugin.start_compatibility(self.manual(host_type="prusalink"), gcode, None),
            ("incompatible", "startReasonNoStart"))
        self.assertEqual(
            self.plugin.start_compatibility(self.manual(host_type="crealityprint"), gcode, None),
            ("incompatible", "startReasonUnsupported"))
        # Duet is the one adapter that can start: same rules as the other starters.
        self.assertEqual(
            self.plugin.start_compatibility(self.manual(host_type="duet"), gcode, None),
            ("unknown", "startReasonProfileUnknown"))
        self.assertEqual(
            self.plugin.start_compatibility(self.manual(host_type="repetier"), gcode, None),
            ("unknown", "startReasonProfileUnknown"))
        # Sliced 3MF is fine for a G-code backend (the plate is extracted).
        self.assertEqual(
            self.plugin.start_compatibility(self.manual(), sliced, "Metadata/plate_3.gcode")[0],
            "unknown")

    def test_plate_three_is_never_approved_as_plate_one(self):
        sliced = self.stage_3mf()
        printer = self.manual()
        store = self.plugin._pf_approval.OperationApprovalStore()
        plan1 = self.dashboard._start_approval_plan(printer, sliced, "Metadata/plate_1.gcode", False)
        plan3 = self.dashboard._start_approval_plan(printer, sliced, "Metadata/plate_3.gcode", False)
        self.assertNotEqual(plan1, plan3)
        nonce, _ = store.create(plan1, 60, "epoch")
        self.assertFalse(store.consume(nonce, plan3, "epoch"))

    def test_operator_check_is_part_of_the_plan_digest(self):
        gcode = self.stage_gcode(profile=self.PRESET)
        unknown = self.dashboard._start_approval_plan(self.manual(), gcode, None, False)
        compatible = self.dashboard._start_approval_plan(self.adopted(), gcode, None, False)
        self.assertNotEqual(unknown["metadata_digest"], compatible["metadata_digest"])

    # --- prepare / submit -----------------------------------------------------
    def run_prepare(self, printers, **extra):
        message = {"artifactToken": "token", "plate": None, "use_ams": False,
                   "targets": [self.identity(p) for p in printers], **extra}
        with mock.patch.object(self.plugin, "dashboard_printers", return_value=printers):
            self.dashboard._prepare_mass_start(message)
        return self.prepared()

    def run_submit(self, printers, grants, **extra):
        message = {"artifactToken": "token", "plate": None, "use_ams": False, "start": True,
                   "targets": [self.identity(p) for p in printers], "approvals": grants, **extra}
        with mock.patch.object(self.plugin, "dashboard_printers", return_value=printers), \
                mock.patch.object(self.dashboard, "_do_mass_print", return_value=True) as send:
            self.dashboard._submit_mass_print(message)
            scheduler = self.dashboard._action_scheduler
            if scheduler is not None:
                scheduler.wait_for_idle(3)
        return send

    def test_prepare_reports_a_verdict_per_target_and_no_nonce_when_incompatible(self):
        self.stage_gcode(profile=self.PRESET)
        compatible, unknown = self.adopted(), self.manual()
        bambu = self.manual(name="Bambu", url="http://bambu.local", host_type="bambu")
        prepared = self.run_prepare([compatible, unknown, bambu])
        by_uuid = {item["targetUuid"]: item for item in prepared["targets"]}
        self.assertEqual(by_uuid[compatible["target_uuid"]]["verdict"], "compatible")
        self.assertEqual(by_uuid[unknown["target_uuid"]]["verdict"], "unknown")
        blocked = by_uuid[bambu["target_uuid"]]
        self.assertEqual((blocked["verdict"], blocked["reason"], blocked["nonce"]),
                         ("incompatible", "startReasonSlicedOnly", None))
        self.assertTrue(by_uuid[compatible["target_uuid"]]["nonce"])
        self.assertTrue(by_uuid[unknown["target_uuid"]]["nonce"])

    def test_compatible_start_needs_only_the_bed_clear_confirmation(self):
        self.stage_gcode(profile=self.PRESET)
        printer = self.adopted()
        grant = self.run_prepare([printer])["targets"][0]
        base = {"targetUuid": grant["targetUuid"], "nonce": grant["nonce"]}
        send = self.run_submit([printer], [dict(base)])
        send.assert_not_called()
        self.assertIn(self.plugin.ui_text("startConfirmMissing"), self.statuses())
        # The refused attempt consumed nothing, so the nonce still works once.
        send = self.run_submit([printer], [{**base, "bedClear": True}])
        self.assertEqual(send.call_count, 1)
        task = send.call_args.args[0]
        self.assertTrue(task["_approved"])
        self.assertEqual(task["_startCheck"]["verdict"], "compatible")
        self.assertFalse(task["_startCheck"]["operator_checked"])
        self.assertTrue(task["_startCheck"]["bed_clear"])

    def test_unknown_start_requires_the_operator_file_check(self):
        self.stage_gcode(profile="Some other preset")
        printer = self.manual()
        grant = self.run_prepare([printer])["targets"][0]
        base = {"targetUuid": grant["targetUuid"], "nonce": grant["nonce"], "bedClear": True}
        send = self.run_submit([printer], [dict(base)])
        send.assert_not_called()
        self.assertIn(self.plugin.ui_text("startConfirmMissing"), self.statuses())
        send = self.run_submit([printer], [{**base, "operatorChecked": True}])
        self.assertEqual(send.call_count, 1)
        check = send.call_args.args[0]["_startCheck"]
        self.assertEqual(check["verdict"], "unknown")
        self.assertTrue(check["operator_checked"])

    def test_nonce_is_single_use(self):
        self.stage_gcode(profile=self.PRESET)
        printer = self.adopted()
        grant = self.run_prepare([printer])["targets"][0]
        grants = [{"targetUuid": grant["targetUuid"], "nonce": grant["nonce"], "bedClear": True}]
        self.assertEqual(self.run_submit([printer], grants).call_count, 1)
        self.dashboard._received_artifacts.clear()
        self.stage_gcode(profile=self.PRESET)
        replay = self.run_submit([printer], grants)
        replay.assert_not_called()
        self.assertEqual(self.statuses()[-1], self.plugin.ui_text("startApprovalExpired"))

    def test_changing_the_file_after_approval_cancels_the_start(self):
        artifact = self.stage_gcode(profile=self.PRESET)
        printer = self.adopted()
        grant = self.run_prepare([printer])["targets"][0]
        os.chmod(artifact.path, 0o600)
        artifact.path.write_bytes(b"G28\nM104 S300\n")
        send = self.run_submit([printer], [{"targetUuid": grant["targetUuid"],
                                            "nonce": grant["nonce"], "bedClear": True}])
        send.assert_not_called()
        self.assertEqual(self.statuses()[-1], self.plugin.ui_text("startApprovalExpired"))

    def test_incompatible_target_is_never_submitted(self):
        self.stage_gcode(profile=self.PRESET)
        printer = self.manual(host_type="bambu")
        send = self.run_submit([printer], [{"targetUuid": printer["target_uuid"],
                                            "nonce": "forged", "bedClear": True,
                                            "operatorChecked": True}])
        send.assert_not_called()
        self.assertIn(self.plugin.ui_text(
            "startBlocked", printer=printer["name"],
            reason=self.plugin.ui_text("startReasonSlicedOnly")), self.statuses())

    def test_start_without_boolean_true_is_only_an_upload(self):
        self.stage_gcode(profile=self.PRESET)
        printer = self.adopted()
        send = self.run_submit([printer], [], start="true")
        self.assertEqual(send.call_count, 1)
        self.assertFalse(send.call_args.args[0]["_approved"])

    def test_approved_mass_start_reaches_the_driver_and_the_journal(self):
        self.stage_gcode(profile=self.PRESET)
        printer = self.adopted()
        grant = self.run_prepare([printer])["targets"][0]
        message = {"artifactToken": "token", "plate": None, "use_ams": False, "start": True,
                   "targets": [self.identity(printer)],
                   "approvals": [{"targetUuid": grant["targetUuid"], "nonce": grant["nonce"],
                                  "bedClear": True}]}
        with mock.patch.object(self.plugin, "dashboard_printers", return_value=[printer]), \
                mock.patch.object(self.plugin, "poll_printer", return_value=_fresh_status(
                    self.plugin, "moonraker", "standby", "standby")), \
                mock.patch.object(self.plugin, "send_file_path", return_value={"ok": True}) as send, \
                mock.patch.object(self.dashboard, "_refresh_worker"):
            self.dashboard._submit_mass_print(message)
            self.assertTrue(self.dashboard._action_scheduler.wait_for_idle(5))
        self.assertEqual(send.call_count, 1)
        self.assertTrue(send.call_args.args[3], "start must reach the driver")
        journal = self.plugin._pf_journal.OperationJournal(
            os.path.join(os.path.realpath(self.root), "operations.sqlite3"))
        try:
            operations = journal.list_operations(
                self.plugin.PrintersDashboard._journal_account_id())
        finally:
            journal.close()
        self.assertEqual(len(operations), 1)
        self.assertEqual(operations[0]["action"], "start")
        self.assertEqual(operations[0]["target_binding"]["start_check"], {
            "verdict": "compatible", "reason": "startReasonProfileMatch",
            "operator_checked": False, "bed_clear": True})

    def test_use_ams_defaults_to_false_in_the_send_like_in_the_approval(self):
        self.stage_gcode(profile=self.PRESET)
        printer = self.adopted()
        grant = self.run_prepare([printer])["targets"][0]
        message = {"artifactToken": "token", "plate": None, "start": True,
                   "targets": [self.identity(printer)],
                   "approvals": [{"targetUuid": grant["targetUuid"], "nonce": grant["nonce"],
                                  "bedClear": True}]}
        with mock.patch.object(self.plugin, "dashboard_printers", return_value=[printer]),                 mock.patch.object(self.plugin, "poll_printer", return_value=_fresh_status(
                    self.plugin, "moonraker", "standby", "standby")),                 mock.patch.object(self.plugin, "send_file_path", return_value={"ok": True}) as send,                 mock.patch.object(self.dashboard, "_refresh_worker"):
            self.dashboard._submit_mass_print(message)
            self.assertTrue(self.dashboard._action_scheduler.wait_for_idle(5))
        self.assertEqual(send.call_count, 1)
        self.assertIs(send.call_args.args[4], False)

    def test_a_refused_batch_does_not_burn_the_other_nonces(self):
        self.stage_gcode(profile=self.PRESET)
        first, second = self.adopted(), self.manual()
        grants = self.run_prepare([first, second])["targets"]
        ready = [{"targetUuid": g["targetUuid"], "nonce": g["nonce"], "bedClear": True,
                  "operatorChecked": True} for g in grants]
        broken = [dict(ready[0]), {**ready[1], "bedClear": False}]
        self.run_submit([first, second], broken).assert_not_called()
        self.assertEqual(self.run_submit([first, second], ready).call_count, 2)

    def test_approved_outbox_start_needs_bed_clear_and_reaches_the_driver(self):
        printer = self.adopted()
        content = ("G28\n; printer_settings_id = %s\n" % self.PRESET).encode()
        stored = printer["target_uuid"] + "/fixture.gcode"
        record = {"file": stored, "name": "fixture.gcode",
                  "sha256": hashlib.sha256(content).hexdigest(),
                  "target_uuid": printer["target_uuid"],
                  "owner_scope_uuid": printer["owner_scope_uuid"],
                  "target_selector": printer["target_selector"],
                  "binding_revision": printer["binding_revision"]}
        outbox = self.root / "outbox"
        (outbox / printer["target_uuid"]).mkdir(parents=True)
        (outbox / stored).write_bytes(content)
        with mock.patch.object(self.plugin, "OUTBOX_DIR", str(outbox)), \
                mock.patch.object(self.plugin, "dashboard_printers", return_value=[printer]), \
                mock.patch.object(self.plugin, "load_outbox_index", return_value=[record]), \
                mock.patch.object(self.plugin, "poll_printer", return_value=_fresh_status(
                    self.plugin, "moonraker", "standby", "standby")), \
                mock.patch.object(self.plugin, "send_file_path", return_value={"ok": True}) as send, \
                mock.patch.object(self.dashboard, "_journal_begin",
                                  return_value=(mock.Mock(), "op")) as begin, \
                mock.patch.object(self.dashboard, "_journal_finish"), \
                mock.patch.object(self.dashboard, "_refresh_worker"):
            self.dashboard._prepare_outbox_start({**self.identity(printer), "file": stored})
            approval = next(call.args[0]["outboxStartApproval"]
                            for call in self.dashboard._post.call_args_list
                            if "outboxStartApproval" in call.args[0])
            self.assertEqual(approval["verdict"], "compatible")
            request = {**self.identity(printer), "file": stored, "start": True,
                       "use_ams": True, "approvalNonce": approval["nonce"]}
            self.dashboard._do_outbox_send(dict(request))
            send.assert_not_called()
            self.dashboard._do_outbox_send({**request, "bedClear": True})
            self.assertEqual(send.call_count, 1)
            self.assertIs(send.call_args.kwargs["start"], True)
            self.assertEqual(begin.call_args.kwargs["start_check"]["verdict"], "compatible")
            self.dashboard._do_outbox_send({**request, "bedClear": True})
            self.assertEqual(send.call_count, 1, "a used approval must not start again")

    def test_journal_records_the_compatibility_check(self):
        printer = self.manual()
        check = {"verdict": "unknown", "reason": "startReasonProfileUnknown",
                 "operator_checked": True, "bed_clear": True}
        with mock.patch.object(self.plugin, "dashboard_printers", return_value=[printer]):
            dispatch = self.dashboard._journal_begin(
                printer, "start", artifact_sha256="a" * 64, artifact_name="job.gcode",
                start_check=check)
        self.assertIsNotNone(dispatch)
        journal, operation_id = dispatch
        try:
            binding = journal.get(operation_id)["target_binding"]
        finally:
            journal.close()
        self.assertEqual(binding["start_check"], check)


class StorageLeftoverCleanupTest(unittest.TestCase):
    """"Delete leftovers" removes only what the plugin itself left behind."""

    def setUp(self):
        self.plugin = _load_plugin()
        self.dashboard = self.plugin.PrintersDashboard()
        self.dashboard._post = mock.Mock(return_value=True)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = pathlib.Path(self.temp.name)
        self.outside = base / "outside"
        self.outside.mkdir()
        self.root = base / "plugin"
        self.root.mkdir()
        self.printer_uuid = "11111111-1111-4111-8111-111111111111"
        patches = [
            mock.patch.object(self.plugin, "PLUGIN_STORAGE_DIR", str(self.root)),
            mock.patch.object(self.plugin, "OUTBOX_DIR", str(self.root / "outbox")),
            mock.patch.object(self.plugin, "OUTBOX_INDEX", str(self.root / "outbox_index.json")),
            mock.patch.object(self.plugin, "_STORAGE_DENIED", False),
            mock.patch.object(self.dashboard, "_refresh_worker"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.dashboard._stop_action_scheduler)

        def put(relative, data=b"x" * 10, parent=self.root):
            path = parent / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            return path

        self.indexed = put("outbox/%s/1__indexed.gcode" % self.printer_uuid)
        self.orphan_in_printer_folder = put("outbox/%s/orphan.gcode" % self.printer_uuid)
        self.unassigned = put("outbox/unassigned/2__unassigned.gcode")
        self.loose = put("outbox/loose.gcode")
        self.stray = put("outbox/stray/deep.gcode")
        self.incoming = put("incoming/pf-artifact-%s.gcode" % ("a" * 32))
        os.chmod(self.incoming, 0o400)
        self.recovery = put("outbox-legacy-recovery/r.gcode")
        self.index = put("outbox_index.json", json.dumps([
            {"file": "%s/1__indexed.gcode" % self.printer_uuid, "name": "indexed.gcode"},
            {"file": "unassigned/2__unassigned.gcode", "name": "unassigned.gcode"},
        ]).encode())
        self.index_backup = put("outbox_index.json.layout-backup", b"[]")
        self.printers = put("printers.json", b"[]")
        self.history = put("print_history.json", b"[]")
        self.legacy_backup = put("plugin.legacy-backup-1/printers.json", b"[]", parent=base)
        self.leftovers = {self.loose, self.stray, self.incoming, self.recovery,
                          self.index_backup}
        self.kept = {self.indexed, self.orphan_in_printer_folder, self.unassigned,
                     self.index, self.printers, self.history, self.legacy_backup}

    def statuses(self):
        return [call.args[0]["status"] for call in self.dashboard._post.call_args_list
                if "status" in call.args[0]]

    def run_cleanup(self):
        self.dashboard._cleanup_leftovers_and_refresh([])

    def test_lists_exactly_the_plugin_leftovers(self):
        files, skipped = self.plugin.outbox_leftover_files()
        self.assertEqual({pathlib.Path(path) for _kind, path, _size in files}, self.leftovers)
        self.assertEqual(skipped, [])
        self.assertEqual({kind for kind, _path, _size in files},
                         {"loose", "incoming", "recovery", "index-backup"})

    def test_cleanup_deletes_only_leftovers(self):
        expected_size = sum(path.stat().st_size for path in self.leftovers)
        self.run_cleanup()
        for path in self.leftovers:
            self.assertFalse(path.exists(), path)
        for path in self.kept:
            self.assertTrue(path.exists(), path)
        self.assertEqual(json.loads(self.index.read_text())[0]["name"], "indexed.gcode")
        self.assertEqual(self.statuses()[-1], self.plugin.ui_text(
            "outboxStorageCleanDone", count=5, size=self.plugin.format_size(expected_size)))

    def test_refuses_while_an_upload_or_transfer_is_active(self):
        self.dashboard._received_artifacts["token"] = types.SimpleNamespace(path=self.incoming)
        self.run_cleanup()
        self.assertTrue(all(path.exists() for path in self.leftovers))
        self.assertEqual(self.statuses()[-1], self.plugin.ui_text("outboxStorageCleanBlocked"))
        self.assertFalse(self.dashboard._leftover_cleanup_state()["available"])
        self.dashboard._received_artifacts.clear()
        self.dashboard._active_transfer_token = "transfer"
        self.assertEqual(self.dashboard._leftover_blocker(), "busy")
        self.dashboard._active_transfer_token = None
        self.assertEqual(self.dashboard._leftover_blocker(), "")

    def test_refuses_while_an_operation_is_dispatching(self):
        storage = os.path.realpath(self.root)
        journal = self.plugin._pf_journal.OperationJournal(
            os.path.join(storage, "operations.sqlite3"))
        try:
            operation = journal.prepare(
                account_id=hashlib.sha256(storage.encode("utf-8")).hexdigest(),
                target_id="scope:target", target_binding={"target_uuid": "t"},
                action="upload", artifact_sha256="b" * 64, artifact_name="job.gcode")
            journal.begin_dispatch(operation["operation_id"])
        finally:
            journal.close()
        self.run_cleanup()
        self.assertTrue(all(path.exists() for path in self.leftovers))
        self.assertEqual(self.dashboard._leftover_blocker(), "busy")

    def test_refuses_unless_the_index_is_migrated_and_valid(self):
        self.index.write_text("{not json")
        self.run_cleanup()
        self.assertTrue(all(path.exists() for path in self.leftovers))
        self.assertEqual(self.dashboard._leftover_blocker(), "index")
        # A flat (unmigrated) record keeps the old layout "in use".
        self.index.write_text(json.dumps([{"file": "loose.gcode", "name": "loose.gcode"}]))
        self.run_cleanup()
        self.assertTrue(self.loose.exists())
        self.assertEqual(self.dashboard._leftover_blocker(), "index")
        # A record that escapes the outbox is not a valid index either.
        self.index.write_text(json.dumps([{"file": "../outside/secret", "name": "x"}]))
        self.assertEqual(self.dashboard._leftover_blocker(), "index")

    def test_failures_are_reported_apart_and_can_be_retried(self):
        real_remove = os.remove

        def deny(path, *args, **kwargs):
            if pathlib.Path(path) == self.recovery:
                raise PermissionError("locked")
            return real_remove(path, *args, **kwargs)

        with mock.patch.object(self.plugin.os, "remove", side_effect=deny):
            self.run_cleanup()
        self.assertTrue(self.recovery.exists())
        self.assertFalse(self.loose.exists())
        self.assertIn("outbox-legacy-recovery/r.gcode", self.statuses()[-1])
        self.assertTrue(self.dashboard._leftover_cleanup_state()["available"])
        self.run_cleanup()
        self.assertFalse(self.recovery.exists())
        self.assertEqual(self.statuses()[-1], self.plugin.ui_text(
            "outboxStorageCleanDone", count=1, size=self.plugin.format_size(10)))

    def test_never_deletes_outside_storage_or_through_links(self):
        secret = self.outside / "secret.txt"
        secret.write_text("keep me")
        files, _skipped = self.plugin.outbox_leftover_files()
        result = self.plugin.outbox_remove_leftovers(files + [("loose", str(secret), 7)])
        self.assertTrue(secret.exists())
        self.assertIn(os.path.relpath(secret, os.path.realpath(self.root)).replace(os.sep, "/"),
                      result["failed"])
        link = self.root / "outbox" / "link.gcode"
        junction = self.root / "outbox" / "dirlink"
        expected = []
        try:
            os.symlink(secret, link)
            expected.append("outbox/link.gcode")
        except (OSError, NotImplementedError):
            pass
        try:
            os.symlink(self.outside, junction, target_is_directory=True)
        except (OSError, NotImplementedError):
            import subprocess
            made = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(self.outside)],
                                  capture_output=True) if os.name == "nt" else None
            if made is None or made.returncode != 0:
                junction = None
        if junction is not None:
            expected.append("outbox/dirlink")
        if not expected:
            self.skipTest("links are not available")
        files, skipped = self.plugin.outbox_leftover_files()
        listed = {pathlib.Path(path) for _kind, path, _size in files}
        self.assertNotIn(link, listed)
        self.assertEqual(sorted(skipped), sorted(expected))
        self.run_cleanup()
        self.assertTrue(secret.exists())
        self.assertEqual(sorted(os.listdir(self.outside)), ["secret.txt"])
        for name in expected:
            self.assertIn(name, self.statuses()[-1])

class ConnectionRegistryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin = _load_plugin()

    def test_form_options_and_page_lists_follow_the_backend_registry(self):
        page = self.plugin.render_page()
        for value, label in self.plugin._CONNECTION_TYPES:
            if self.plugin.backend_is_active(value):
                self.assertIn('<option value="%s">%s</option>' % (value, label), page)
            else:
                self.assertIn('<option value="%s" disabled>%s \u2014 coming later</option>'
                              % (value, label), page)
        active = json.loads(re.search(r"var ACTIVE_TYPES = (\[.*?\]);", page).group(1))
        self.assertEqual(active, self.plugin._active_host_types())
        self.assertTrue(set(self.plugin._ADAPTER_BACKENDS) <= set(active))
        adapters = json.loads(re.search(r"var ADAPTER_TYPES = (\[.*?\]);", page).group(1))
        self.assertEqual(adapters, sorted(self.plugin._ADAPTER_BACKENDS))
        self.assertNotIn("['duet','prusalink']", page)

    def test_adapter_connections_cannot_start_prints_yet(self):
        for backend in self.plugin._ADAPTER_BACKENDS:
            self.assertFalse(self.plugin.status_can_start(
                {"backend": backend, "online": True}, {"type": backend}))
            self.assertEqual(self.plugin.backend_for_type(backend), backend)
            self.assertTrue(self.plugin.backend_is_active(backend))

    def test_every_loaded_helper_is_embedded_by_the_release_builder(self):
        spec = importlib.util.spec_from_file_location("pf_build_registry", BUILD_PATH)
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        source = PLUGIN_PATH.read_text(encoding="utf-8")
        loaded = re.findall(r'_pf_load_local_helper\("([a-z0-9_]+)"\)', source)
        registered = [name for _alias, name in builder.HELPER_LOAD.findall(source)]
        self.assertEqual(registered, loaded)
        for name in loaded:
            self.assertTrue(PLUGIN_PATH.with_name(name + ".py").is_file(), name)


_FAKE_DOM_JS = r"""
function El(tag){ this.tag = tag; this.children = []; this.style = {}; this.listeners = {};
  this.textContent = ''; this.disabled = false; this.className = ''; this._html = ''; }
El.prototype.appendChild = function(c){ this.children.push(c); return c; };
El.prototype.addEventListener = function(type, fn){ (this.listeners[type] = this.listeners[type] || []).push(fn); };
El.prototype.focus = function(){ document.activeElement = this; };
El.prototype.setAttribute = function(k, v){ (this.attrs = this.attrs || {})[k] = String(v); };
El.prototype.getAttribute = function(k){ return (this.attrs || {})[k] === undefined ? null : this.attrs[k]; };
El.prototype.removeAttribute = function(k){ if (k === 'src') this._src = undefined; else if (this.attrs) delete this.attrs[k]; };
Object.defineProperty(El.prototype, 'src', { get: function(){ return this._src; },
  set: function(v){ this._src = v; if (v) (document.loads = document.loads || []).push(v); } });
El.prototype.fire = function(type, ev){ ev = ev || {}; ev.target = ev.target || this;
  ev.preventDefault = ev.preventDefault || function(){}; ev.stopImmediatePropagation = function(){};
  (this.listeners[type] || []).forEach(function(fn){ fn.call(this, ev); }, this); };
Object.defineProperty(El.prototype, 'innerHTML', { get: function(){ return this._html; },
  set: function(v){ this._html = v; this.children = []; } });
El.prototype.texts = function(){ var out = []; (function walk(n){ if (n.textContent) out.push(n.textContent);
  n.children.forEach(walk); })(this); return out; };
var ids = {};
var document = { activeElement: null,
  getElementById: function(id){ return ids[id] || (ids[id] = new El(id)); },
  createElement: function(tag){ return new El(tag); }, contains: function(){ return true; },
  addEventListener: function(type, fn){ document.l = document.l || {}; (document.l[type] = document.l[type] || []).push(fn); },
  key: function(key, extra){ var ev = Object.assign({key:key, preventDefault:function(){},
      stopImmediatePropagation:function(){}}, extra || {});
    ((document.l || {}).keydown || []).forEach(function(fn){ fn(ev); }); } };
var posted = [];
var orca = { postMessage: function(m){ posted.push(m); } };
function t(key, vars){ return key; }
"""


def _page_slice(start, end):
    page = _load_plugin().PAGE
    return page[page.index(start):page.index(end)]


@unittest.skipUnless(__import__("shutil").which("node"), "node is not available")
@unittest.skipUnless(__import__("shutil").which("node"), "node is not available")
class JournalFilesUiTest(unittest.TestCase):
    """Runs the page's own journal, stored-file and confirmation code against a fake DOM."""

    PRELUDE = r"""
var ACTIVE_TYPES = ['moonraker','octoprint','bambu','duet'];
var ADAPTER_TYPES = ['duet'];
var window = {lastUnassignedOutbox: [], lastOutboxStorage: null};
var current = [], expandedQueues = {}, queueScrollPositions = {};
function esc(s){ return String(s == null ? '' : s).replace(/[&<>"]/g, function(c){
  return {'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;'}[c]; }); }
Object.defineProperty(El.prototype, 'classList', { get: function(){ var el = this; return {
  contains: function(c){ return (' ' + el.className + ' ').indexOf(' ' + c + ' ') >= 0; },
  add: function(c){ if (!this.contains(c)) el.className = (el.className + ' ' + c).trim(); },
  toggle: function(c){ if (this.contains(c)) { el.className = el.className.split(' ').filter(function(x){
    return x !== c; }).join(' '); return false; } this.add(c); return true; } }; } });
t = function(key, vars){ return key + (vars ? JSON.stringify(vars) : ''); };
function card(name, uuid, state, type, queued){
  type = type || 'moonraker';
  return {name:name, url:'http://' + name, type:type, target_uuid:uuid, owner_scope_uuid:'o',
    target_selector:'s', binding_revision:3, queued:queued || [],
    status:{online:true, transport_online:true, authorization:'accepted', device_health:'healthy',
      freshness:'fresh', observed_at:Date.now() / 1000, backend:type, state:state, job_state:state}};
}
function openJournal(items, counts){
  historyMode = 'all'; historyExpected = {mode:'all', requestId:1}; historyItems = items;
  historyCounts = counts || {all:items.length, completed:0, active:0};
  document.getElementById('journal-search').value = '';
  renderHistoryItems();
}
var list = document.getElementById('history-list');
function rows(){ return list.children.filter(function(c){ return c.className.indexOf('history-item') === 0; }); }
function title(row){ return row._html.match(/history-file" title="([^"]*)"/)[1]; }
function buttons(el){ var out = []; (function walk(n){ n.children.forEach(function(c){
  if (c.tag === 'button') out.push(c); walk(c); }); })(el); return out; }
function labels(row){ return buttons(row).map(function(b){ return b.textContent; }); }
function named(el, text){ return buttons(el).filter(function(b){ return b.textContent === text; }); }
function modal(){ return document.getElementById('ask-modal').style.display || ''; }
function files(n, prefix, base){ var out = []; for (var i = 0; i < n; i++)
  out.push({file:prefix + '/' + i + '__f' + i + '.gcode', name:'f' + i + '.gcode', size:1024, ts:(base || 1000) + i});
  return out; }
"""

    def run_js(self, body):
        import subprocess
        report = _page_slice("// <outbox-report>", "// </outbox-report>")
        ask = _page_slice("var askState = null;", "function render(printers){")
        fmt = _page_slice("function formatBytes(value){", "var historyExpected = null;")
        printer_ui = _page_slice("function stateKind(s){", "function statusControlReady(p, action){")
        hist = _page_slice("var historyExpected = null;",
                           "document.getElementById('history-close').addEventListener")
        script = (_FAKE_DOM_JS + self.PRELUDE + fmt + printer_ui + hist + report + ask + body)
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "case.js"
            path.write_text(script, encoding="utf-8")
            done = subprocess.run(["node", str(path)], capture_output=True, text=True,
                                  encoding="utf-8", timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_empty_journal_says_so_and_shows_nothing_about_storage(self):
        out = self.run_js("""
          openJournal([]);
          updateStorageNotices();
          console.log(JSON.stringify({html:list._html, storage:document.getElementById('journal-storage').style.display,
            warn:document.getElementById('storage-warn').style.display,
            link:document.getElementById('unassigned-link').style.display, rows:rows().length}));""")
        self.assertIn("journalEmptyAll", out["html"])
        self.assertEqual((out["storage"], out["warn"], out["link"], out["rows"]), ("none", "none", "none", 0))

    def test_one_list_newest_first_with_a_file_on_every_row_that_has_one(self):
        out = self.run_js("""
          window.lastUnassignedOutbox = [
            {file:'unassigned/1__a.gcode', name:'a.gcode', size:10, ts:5000},
            {file:'unassigned/2__b.gcode', name:'b.gcode', size:20, ts:9000}];
          openJournal([
            {filename:'x.gcode', state:'completed', started_at:7000, source:'print_farm', printer_name:'P',
             duration_seconds:60, file:'unassigned/1__a.gcode'},
            {filename:'y.gcode', state:'failed', started_at:3000, source:'observed', printer_name:'P',
             duration_seconds:0, file:'unassigned/deleted-long-ago'},
            {filename:'z.gcode', state:'completed', started_at:2000, source:'orca', printer_name:'P',
             duration_seconds:0}], {all:3, completed:2, active:0});
          console.log(JSON.stringify({names:rows().map(title), buttons:rows().map(labels),
            all:document.getElementById('journal-all-label').textContent,
            unsent:rows()[0]._html.indexOf('journalFileUnsent') >= 0}));""")
        # a.gcode is the stored copy of x.gcode, so it is not listed a second time.
        self.assertEqual(out["names"], ["b.gcode", "x.gcode", "y.gcode", "z.gcode"])
        self.assertEqual(out["buttons"], [["unassignedSend", "unassignedDelete"],
                                          ["journalResend", "journalFileDelete"], [], []])
        self.assertTrue(out["unsent"])
        self.assertEqual(out["all"], 'journalAllCount{"count":4}')

    def test_search_finds_stored_files_by_name_and_printer(self):
        out = self.run_js("""
          current = [card('Voron', 'u1', 'standby', 'moonraker', [
            {file:'u1/1__benchy.gcode', name:'benchy.gcode', size:1, ts:3}])];
          window.lastUnassignedOutbox = [{file:'unassigned/1__cube.gcode', name:'cube.gcode', size:1, ts:4}];
          openJournal([{filename:'old.gcode', state:'completed', started_at:1, printer_name:'Ulti', duration_seconds:0}]);
          var search = document.getElementById('journal-search'), seen = {};
          ['bench', 'CUBE', 'voron', 'ulti', 'nothing'].forEach(function(q){
            search.value = q; renderHistoryItems();
            seen[q] = rows().map(title).concat(list._html.indexOf('journalNoResults') >= 0 ? ['none'] : []); });
          console.log(JSON.stringify(seen));""")
        self.assertEqual(out, {"bench": ["benchy.gcode"], "CUBE": ["cube.gcode"], "voron": ["benchy.gcode"],
                               "ulti": ["old.gcode"], "nothing": ["none"]})

    def test_unassigned_copy_is_sent_once_through_a_chosen_ready_printer(self):
        out = self.run_js("""
          current = [card('ready', 'u1', 'standby'), card('busy', 'u2', 'printing'),
                     {name:'off', url:'http://off', type:'moonraker', target_uuid:'u3', status:{online:false}}];
          window.lastUnassignedOutbox = [{file:'unassigned/1__a.gcode', name:'a.gcode', size:10, ts:5}];
          openJournal([]);
          var shown = function(){ return list.texts().filter(function(x){ return ['ready','busy','off'].indexOf(x) >= 0; }); };
          var before = shown();
          named(list, 'unassignedSend')[0].fire('click');
          var picker = {names:shown(), upload:named(list, 'unassignedUpload').length,
                        start:named(list, 'unassignedUploadStart').length};
          named(list, 'unassignedUpload')[0].fire('click');
          var afterUpload = posted.slice();
          named(list, 'unassignedSend')[0].fire('click');
          named(list, 'unassignedUploadStart')[0].fire('click');
          var afterStart = posted.slice(1);
          current = [current[1], current[2]];
          named(list, 'unassignedSend')[0].fire('click');
          console.log(JSON.stringify({before:before, picker:picker, afterUpload:afterUpload, afterStart:afterStart,
            none:list.texts().indexOf('unassignedNoPrinter') >= 0, modal:modal()}));""")
        self.assertEqual(out["before"], [])
        self.assertEqual(out["picker"], {"names": ["ready"], "upload": 1, "start": 1})
        identity = {"targetUuid": "u1", "ownerScope": "o", "targetSelector": "s", "bindingRevision": 3,
                    "url": "http://ready", "connectionType": "moonraker", "name": "ready",
                    "type": "outbox-start-prepare", "file": "unassigned/1__a.gcode", "unassigned": True}
        self.assertEqual(out["afterUpload"], [{**identity, "start": False}])
        self.assertEqual(out["afterStart"], [{**identity, "start": True}])
        self.assertTrue(out["none"])
        self.assertNotEqual(out["modal"], "flex", "nothing is confirmed before the printer is chosen")

    def test_delete_needs_confirmation_for_unassigned_and_linked_copies(self):
        out = self.run_js("""
          window.lastUnassignedOutbox = [
            {file:'unassigned/1__a.gcode', name:'a.gcode', size:10, ts:5},
            {file:'unassigned/2__b.gcode', name:'b.gcode', size:20, ts:9}];
          openJournal([{filename:'a.gcode', state:'sent', started_at:6, printer_name:'P', duration_seconds:0,
                        file:'unassigned/1__a.gcode'}]);
          var log = [];
          var del = named(list, 'unassignedDelete')[0];
          del.fire('click');
          log.push([modal(), document.getElementById('ask-body').textContent, posted.length]);
          document.getElementById('ask-cancel').fire('click');
          log.push([modal(), posted.length]);
          del.fire('click'); document.getElementById('ask-ok').fire('click');
          var linked = named(list, 'journalFileDelete')[0];
          linked.fire('click'); log.push([posted.length]);
          document.getElementById('ask-ok').fire('click');
          console.log(JSON.stringify({log:log, posted:posted}));""")
        self.assertEqual(out["log"][0][0], "flex")
        self.assertIn("unassignedDeleteConfirm", out["log"][0][1])
        self.assertEqual((out["log"][0][2], out["log"][1][1], out["log"][2][0]), (0, 0, 1))
        self.assertEqual(out["posted"], [
            {"type": "outbox-unassigned-drop", "file": "unassigned/2__b.gcode", "requestId": 1},
            {"type": "outbox-unassigned-drop", "file": "unassigned/1__a.gcode", "requestId": 2},
        ])

    def test_copy_bound_to_a_printer_uses_that_printers_card_path(self):
        out = self.run_js("""
          var ready = card('ready', 'u1', 'standby', 'moonraker', [{file:'u1/1__q.gcode', name:'q.gcode', size:2048, ts:50}]);
          var busy = card('busy', 'u2', 'printing', 'moonraker', [{file:'u2/1__w.gcode', name:'w.gcode', size:1, ts:40}]);
          current = [ready, busy];
          openJournal([{filename:'q.gcode', state:'sent', started_at:60, printer_name:'ready', duration_seconds:0,
                        file:'u1/1__q.gcode'}]);
          var r = rows();
          var sendQ = named(r[0], 'journalResend')[0], sendW = named(r[1], 'unassignedSend')[0];
          var busyState = {disabled:sendW.disabled, title:sendW.title};
          sendQ.fire('click');
          var asked = [modal(), document.getElementById('ask-body').textContent, posted.length];
          document.getElementById('ask-ok').fire('click');
          named(r[0], 'journalFileDelete')[0].fire('click'); document.getElementById('ask-ok').fire('click');
          named(r[1], 'unassignedDelete')[0].fire('click'); document.getElementById('ask-ok').fire('click');
          console.log(JSON.stringify({busyState:busyState, asked:asked, posted:posted, names:r.map(title)}));""")
        self.assertEqual(out["names"], ["q.gcode", "w.gcode"])
        self.assertEqual(out["busyState"], {"disabled": True, "title": "printerBusy"})
        self.assertEqual((out["asked"][0], out["asked"][2]), ("flex", 0))
        self.assertIn("journalSendConfirm", out["asked"][1])
        identity = {"targetUuid": "u1", "ownerScope": "o", "targetSelector": "s", "bindingRevision": 3,
                    "url": "http://ready", "connectionType": "moonraker", "name": "ready"}
        busy = {**identity, "targetUuid": "u2", "url": "http://busy", "name": "busy"}
        self.assertEqual(out["posted"], [
            {**identity, "type": "outbox-send", "file": "u1/1__q.gcode", "start": False, "requestId": 1},
            {**identity, "type": "outbox-drop", "file": "u1/1__q.gcode", "requestId": 2},
            {**busy, "type": "outbox-drop", "file": "u2/1__w.gcode", "requestId": 3}])

    def test_long_journal_is_capped_and_grows_on_request(self):
        out = self.run_js("""
          window.lastUnassignedOutbox = files(150, 'unassigned');
          openJournal([]);
          var first = rows().length, more = named(list, 'storageMore{"count":50}');
          more[0].fire('click');
          console.log(JSON.stringify({first:first, after:rows().length, moreLeft:named(list, 'storageMore{"count":50}').length,
            top:title(rows()[0])}));""")
        self.assertEqual(out, {"first": 100, "after": 150, "moreLeft": 0, "top": "f149.gcode"})

    def test_top_bar_shows_size_warning_and_file_link_only_when_needed(self):
        out = self.run_js("""
          var warn = document.getElementById('storage-warn'), link = document.getElementById('unassigned-link');
          var log = [];
          function snap(){ updateStorageNotices(); log.push([warn.style.display, warn.textContent, link.style.display, link.textContent]); }
          snap();
          window.lastOutboxStorage = {groups:[{kind:'printer', size:600 * 1024 * 1024, count:1}]};
          window.lastUnassignedOutbox = files(3, 'unassigned');
          snap();
          window.lastOutboxStorage = {groups:[{kind:'printer', size:10, count:1}]};
          window.lastUnassignedOutbox = [];
          snap();
          console.log(JSON.stringify(log));""")
        self.assertEqual(out, [["none", "", "none", ""],
                               ["", "outboxStorageWarning", "", 'unmatchedQueued{"count":3}'],
                               ["none", "", "none", ""]])

    def test_leftover_cleanup_shows_only_with_leftovers_and_needs_confirmation(self):
        out = self.run_js("""
          var box = document.getElementById('journal-storage');
          function show(report, mode){ window.lastOutboxStorage = report; historyMode = mode || 'all'; renderJournalStorage(); }
          var log = {};
          show(null); log.none = box.style.display;
          show({groups:[{kind:'outboxStorageKindLoose', size:20, count:2}], cleanup:{count:0}, ok:true}); log.noLeftovers = box.style.display;
          show({groups:[], cleanup:{count:2, size:20, available:true}, ok:true}, 'printer'); log.printerMode = box.style.display;
          show({groups:[], cleanup:{count:2, size:20, available:true}, ok:true});
          log.shown = box.style.display; log.texts = box.texts(); var button = buttons(box)[0];
          log.disabled = button.disabled;
          button.fire('click');
          log.asked = [modal(), document.getElementById('ask-body').textContent, posted.length];
          document.getElementById('ask-cancel').fire('click'); log.afterCancel = posted.length;
          button.fire('click'); document.getElementById('ask-ok').fire('click'); log.posted = posted.slice();
          show({groups:[], cleanup:{count:3, size:30, available:false}, ok:true});
          log.blocked = [buttons(box)[0].disabled, box.texts().indexOf('outboxStorageCleanBlocked') >= 0];
          show({groups:[{kind:'printer', size:600 * 1024 * 1024, count:1}], cleanup:{count:0}, ok:false, errors:['p']});
          log.warn = box.texts(); log.warnButtons = buttons(box).length;
          console.log(JSON.stringify(log));""")
        self.assertEqual((out["none"], out["noLeftovers"], out["printerMode"], out["shown"]),
                         ("none", "none", "none", "flex"))
        self.assertIn("outboxStorageManualCleanup", out["texts"])
        self.assertFalse(out["disabled"])
        self.assertEqual(out["asked"][0], "flex")
        self.assertEqual(out["asked"][1], 'outboxStorageCleanConfirm{"count":2,"size":"0 KB"}')
        self.assertEqual((out["asked"][2], out["afterCancel"]), (0, 0))
        self.assertEqual(out["posted"], [{"type": "outbox-cleanup", "requestId": 1}])
        self.assertEqual(out["blocked"], [True, True])
        self.assertEqual(out["warn"], ['outboxStorageScanFailed{"paths":"p"}', "outboxStorageWarning"])
        self.assertEqual(out["warnButtons"], 0)

    def test_card_queue_shows_the_three_newest_and_more_reveals_the_rest(self):
        out = self.run_js("""
          var queued = files(5, 'u1', 100);  // f0 is the oldest, f4 the newest
          var p = card('ready', 'u1', 'standby', 'moonraker', queued);
          var q = buildQueue(p, true, '');
          var list1 = q.children[0], toggle = q.children[1];
          var names = list1.children.map(function(r){ return r.children[0].textContent; });
          var log = {names:names, label:toggle.getAttribute('aria-label'), expanded:q.classList.contains('expanded'),
            direct:list1.children[0].children[2].textContent,
            meta:list1.children[0].children[1].textContent, metaClass:list1.children[0].children[1].className};
          toggle.fire('click');
          log.afterOpen = [q.classList.contains('expanded'), toggle.getAttribute('aria-label')];
          toggle.fire('click');
          log.afterClose = [q.classList.contains('expanded'), toggle.getAttribute('aria-label')];
          var few = buildQueue(card('b', 'u2', 'standby', 'moonraker', files(2, 'u2')), true, '');
          log.few = {rows:few.children[0].children.length, toggle:few.children.length};
          var blocked = buildQueue(card('c', 'u3', 'printing', 'moonraker', files(1, 'u3')), false, 'printerBusy');
          var row = blocked.children[0].children[0];
          log.blocked = [row.children[2].disabled, row.children[2].title];
          console.log(JSON.stringify(log));""")
        self.assertEqual(out["names"], ["f4.gcode", "f3.gcode", "f2.gcode", "f1.gcode", "f0.gcode"])
        self.assertEqual(out["label"], 'queueMore{"count":2}')
        self.assertFalse(out["expanded"])
        self.assertEqual(out["afterOpen"], [True, "queueLess"])
        self.assertEqual(out["afterClose"], [False, 'queueMore{"count":2}'])
        self.assertEqual(out["few"], {"rows": 2, "toggle": 1})
        self.assertEqual(out["blocked"], [True, "printerBusy"])
        # Size and added time sit between the name and the buttons.
        self.assertEqual(out["metaClass"], "qmeta")
        self.assertTrue(out["meta"].startswith("1 KB · "), out["meta"])
        page = _load_plugin().PAGE
        # Only three rows are visible until "more" is used, and no queue is built for an empty list.
        self.assertIn(".queue-scroll { display:flex; flex-direction:column; gap:4px; min-width:0;\n"
                      "                  max-height:92px;", page)
        self.assertIn("if (p.queued && p.queued.length) {\n      var q = buildQueue(", page)

    def test_card_has_no_empty_control_slot_or_spacer(self):
        out = self.run_js("""
          function build(s, kind, controls){
            var statusControlReady = function(){ return controls; };
            var p = {type:'bambu'}, supported = true;
            """ + _page_slice("    var ctl = '';", "    var printDisabledReason") + """
            return ctl;
          }
          var bambu = _openHtmlFor('bambu'), other = _openHtmlFor('moonraker');
          function _openHtmlFor(type){ var p = {type:type};
            """ + _page_slice("    var openHtml = ", "    var selectHtml") + """
            return openHtml; }
          console.log(JSON.stringify({finished:build({online:true, state:'finished'}, 'idle', true),
            idle:build({online:true}, 'idle', false), printing:build({online:true}, 'printing', true),
            bambu:bambu, other:other}));""")
        self.assertEqual(out["finished"], "")
        self.assertEqual(out["idle"], "")
        self.assertTrue(out["printing"].startswith('<span class="ctl-slot">'))
        self.assertIn('data-a="pause"', out["printing"])
        self.assertEqual(out["bambu"], "")
        self.assertIn('class="open ghost"', out["other"])

    def test_finished_bambu_can_take_a_new_job_but_failed_cannot(self):
        out = self.run_js("""
          function bambu(state){ return card('b', 'u1', state, 'bambu'); }
          console.log(JSON.stringify({idle:printerSelectable(bambu('idle')), finished:printerSelectable(bambu('finished')),
            failed:printerSelectable(bambu('failed')), printing:printerSelectable(bambu('printing')),
            moon:printerSelectable(card('m', 'u2', 'finished', 'moonraker'))}));""")
        self.assertEqual(out, {"idle": True, "finished": True, "failed": False, "printing": False, "moon": False})

    def test_new_question_replaces_open_one_without_confirming_it(self):
        out = self.run_js("""
          var fired = [];
          askConfirm('one', 'OK1', function(){ fired.push(1); });
          askConfirm('two', 'OK2', function(){ fired.push(2); });
          var body = document.getElementById('ask-body').textContent;
          document.getElementById('ask-ok').fire('click');
          console.log(JSON.stringify({fired:fired, body:body}));
        """)
        self.assertEqual(out["fired"], [2])
        self.assertEqual(out["body"], "two")

    def test_replacing_open_question_keeps_original_focus_target(self):
        out = self.run_js("""
          var opener = document.getElementById('opener'); opener.focus();
          askConfirm('one', 'OK1', function(){});
          askConfirm('two', 'OK2', function(){});
          document.getElementById('ask-cancel').fire('click');
          console.log(JSON.stringify({focus:document.activeElement.tag}));
        """)
        self.assertEqual(out["focus"], "opener")

    def test_modal_traps_tab_focus(self):
        out = self.run_js("""
          askConfirm('q', 'OK', function(){});
          var seq = [document.activeElement.tag];
          document.getElementById('ask-ok').focus();
          document.key('Tab'); seq.push(document.activeElement.tag);
          document.key('Tab', {shiftKey:true}); seq.push(document.activeElement.tag);
          console.log(JSON.stringify(seq));
        """)
        self.assertEqual(out, ["ask-cancel", "ask-cancel", "ask-ok"])

    def test_page_has_no_native_confirm_and_keeps_approval_fields(self):
        page = _load_plugin().PAGE
        self.assertNotIn("window.confirm(", page)
        self.assertNotRegex(page, r"(?<![\w.])confirm\(")
        self.assertIn('id="ask-modal"', page)
        self.assertIn('role="alertdialog"', page)
        for fragment in ("request.approvalNonce = item.nonce", "request.bedClear = request.start",
                         "request.operatorChecked = item.verdict === 'unknown'",
                         "t('startFileCheck')", "t('startBedClear')",
                         "type:'journal-resolve-hold'", "type:'legacy-import-confirm'"):
            self.assertIn(fragment, page)



class BambuFinishedStartTest(unittest.TestCase):
    """A finished Bambu job is a normal state for the next one; failed is not."""

    @classmethod
    def setUpClass(cls):
        cls.plugin = _load_plugin()

    def can_start(self, state):
        status = _fresh_status(self.plugin, "bambu", state, state)
        return self.plugin.status_can_start(status, {"type": "bambu"})

    def test_idle_and_finished_may_start_but_failed_and_busy_may_not(self):
        self.assertEqual(self.plugin._BAMBU_STATES["FINISH"], "finished")
        self.assertEqual(self.plugin._BAMBU_STATES["FAILED"], "failed")
        self.assertTrue(self.can_start("idle"))
        self.assertTrue(self.can_start("finished"))
        for state in ("failed", "printing", "paused", "preparing", "slicing", "unknown"):
            self.assertFalse(self.can_start(state), state)

    def test_finished_state_still_needs_a_fresh_healthy_accepted_status(self):
        status = _fresh_status(self.plugin, "bambu", "finished", "finished")
        for key, value in (("online", False), ("authorization", "rejected"),
                           ("device_health", "error"), ("freshness", "unknown")):
            self.assertFalse(self.plugin.status_can_start({**status, key: value}, {"type": "bambu"}), key)
        # Another backend's "finished" is never a start state.
        self.assertFalse(self.plugin.status_can_start(
            _fresh_status(self.plugin, "moonraker", "finished", "finished"), {"type": "moonraker"}))

    def test_stale_finish_after_a_start_does_not_end_the_new_job(self):
        plugin = self.plugin
        printer = {"id": 1, "name": "Bambu", "url": "http://b.local", "type": "bambu"}
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(plugin, "PRINT_HISTORY_FILE", str(pathlib.Path(temp) / "h.json")):
                plugin.record_print_started(printer, "next.3mf", now=1_000)
                self.assertFalse(plugin.record_print_observation(
                    printer, {"state": "finished", "filename": "old.3mf", "progress": 100}, now=1_005))
                self.assertEqual(plugin.print_history_for_printer(printer)[0]["state"], "printing")


class JournalFileLinkTest(unittest.TestCase):
    """The journal names a stored copy only by the digest Print Farm recorded itself."""

    @classmethod
    def setUpClass(cls):
        cls.plugin = _load_plugin()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patch = mock.patch.object(self.plugin, "PRINT_HISTORY_FILE",
                                  str(pathlib.Path(self.temp.name) / "print_history.json"))
        patch.start()
        self.addCleanup(patch.stop)
        self.printer = {"id": 7, "name": "Voron", "url": "http://voron.local", "type": "moonraker"}
        self.digest = hashlib.sha256(b"G28 job").hexdigest()

    def payload(self, index):
        dashboard = self.plugin.PrintersDashboard()
        dashboard._post = mock.Mock(return_value=True)
        with mock.patch.object(self.plugin, "load_outbox_index", return_value=index):
            dashboard._post_print_history_all({"requestId": 5})
        return dashboard._post.call_args.args[0]["printHistory"]

    def test_start_and_upload_records_carry_the_digest_but_never_expose_it(self):
        started = self.plugin.record_print_started(self.printer, "a.gcode", now=100, sha256=self.digest)
        self.assertEqual((started["state"], started["source"], started["sha256"]), ("printing", "print_farm", self.digest))
        sent = self.plugin.record_file_sent(self.printer, "b.gcode", now=200, sha256=self.digest)
        self.assertEqual((sent["state"], sent["source"], sent["finished_at"]), ("sent", "print_farm", 200))
        bad = self.plugin.record_file_sent(self.printer, "c.gcode", now=300, sha256="NOT-A-DIGEST")
        self.assertNotIn("sha256", bad)
        payload = self.payload([])
        for item in payload["items"]:
            self.assertNotIn("sha256", item)
            self.assertNotIn("file", item)

    def test_record_links_to_the_stored_copy_only_by_matching_digest(self):
        self.plugin.record_file_sent(self.printer, "a.gcode", now=100, sha256=self.digest)
        self.plugin.record_file_sent(self.printer, "same-name-other-file.gcode", now=200,
                                     sha256=hashlib.sha256(b"other").hexdigest())
        self.plugin.record_file_sent(self.printer, "a.gcode", now=300)  # Orca-style: no digest
        index = [
            {"file": "unassigned/1__a.gcode", "name": "a.gcode", "sha256": self.digest, "ts": 10},
            {"file": "unassigned/9__a.gcode", "name": "a.gcode", "sha256": self.digest, "ts": 20},
            {"file": "unassigned/2__x.gcode", "name": "same-name-other-file.gcode", "ts": 5},
        ]
        items = {(i["filename"], i["started_at"]): i for i in self.payload(index)["items"]}
        self.assertEqual(items[("a.gcode", 100)]["file"], "unassigned/9__a.gcode")  # the newest copy
        self.assertNotIn("file", items[("same-name-other-file.gcode", 200)])
        self.assertNotIn("file", items[("a.gcode", 300)], "a name alone is never a link")
        self.assertEqual(self.payload([])["items"][0].get("file"), None)

    def test_recording_off_writes_no_link(self):
        with mock.patch.object(self.plugin, "history_recording_enabled", return_value=False):
            self.assertIsNone(self.plugin.record_file_sent(self.printer, "a.gcode", sha256=self.digest))
            self.assertIsNone(self.plugin.record_print_started(self.printer, "a.gcode", sha256=self.digest))
        self.assertEqual(self.plugin.load_print_history(), [])


_FULL_BAMBU_REPORT = {
    "print": {
        "gcode_state": "RUNNING", "nozzle_temper": 215.0, "nozzle_target_temper": 220,
        "bed_temper": 60, "bed_target_temper": 60, "mc_percent": 42,
        "subtask_name": "benchy", "mc_remaining_time": 75, "layer_num": 120,
        "total_layer_num": 300, "print_error": 83935249, "spd_lvl": 2,
        "ams": {"ams": [{"id": "0", "tray": [
            {"id": "0", "tray_type": "PLA", "tray_color": "FF8800FF", "remain": 80},
            {"id": "1", "tray_type": "PETG", "tray_color": "00000000", "remain": -1},
            {"id": "2", "tray_slot_placeholder": "1"},
            {"id": "3"},
        ]}]},
    },
}


class BambuLanStatsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin = _load_plugin()

    def stats(self, info, state="printing"):
        return self.plugin._bambu_stats(info, state)

    def test_full_report_gives_confirmed_fields(self):
        stats = self.stats(_FULL_BAMBU_REPORT["print"])
        self.assertEqual(stats["remaining_minutes"], 75)
        self.assertEqual((stats["layer"], stats["total_layers"]), (120, 300))
        self.assertEqual(stats["print_error"], "%08X" % 83935249)
        self.assertEqual(stats["ams"], [
            {"type": "PLA", "color": "#FF8800", "remain": 80},
            {"type": "PETG", "color": None, "remain": None},
        ])

    def test_partial_report_leaves_missing_fields_none_not_zero(self):
        stats = self.stats({"nozzle_temper": 30}, "idle")
        self.assertEqual(stats, dict.fromkeys(self.plugin._BAMBU_STAT_KEYS))
        stats = self.stats({"mc_remaining_time": 5}, "printing")
        self.assertEqual(stats["remaining_minutes"], 5)
        self.assertIsNone(stats["layer"])
        self.assertIsNone(stats["ams"])
        self.assertIsNone(stats["print_error"])

    def test_job_numbers_are_ignored_while_idle_and_zero_error_is_none(self):
        info = dict(_FULL_BAMBU_REPORT["print"], print_error=0)
        stats = self.stats(info, "idle")
        self.assertIsNone(stats["remaining_minutes"])
        self.assertIsNone(stats["layer"])
        self.assertIsNone(stats["print_error"])
        self.assertIsNotNone(stats["ams"])
        self.assertEqual(self.stats({"mc_remaining_time": "30"}, "paused")["remaining_minutes"], 30)

    def test_garbage_and_oversized_values_are_dropped(self):
        info = {"mc_remaining_time": True, "layer_num": -1, "total_layer_num": 10 ** 9,
                "print_error": "x", "ams": {"ams": [
                    {"tray": [{"tray_type": "<script>", "tray_color": "zzzzzz"},
                              {"tray_type": "P" * 40}, "str",
                              {"tray_type": "ABS", "tray_color": "12345", "remain": 101}]},
                    "junk", {"tray": "x"}]}}
        stats = self.stats(info)
        self.assertEqual({k: v for k, v in stats.items() if k != "ams"},
                         {"remaining_minutes": None, "layer": None, "total_layers": None,
                          "print_error": None})
        self.assertEqual(stats["ams"], [{"type": "ABS", "color": None, "remain": None}])
        for bad in (None, [], "x", 5):
            self.assertEqual(self.stats(bad), dict.fromkeys(self.plugin._BAMBU_STAT_KEYS))
        self.assertIsNone(self.stats({"layer_num": 5, "total_layer_num": 3})["layer"])
        big = {"ams": {"ams": [{"tray": [{"tray_type": "PLA"}] * 4}] * 30}}
        self.assertEqual(len(self.stats(big)["ams"]), 16)

    def test_no_ams_means_no_ams_field(self):
        self.assertIsNone(self.stats({"gcode_state": "IDLE", "ams": {"ams": []}}, "idle")["ams"])
        self.assertIsNone(self.stats({"ams": {}}, "idle")["ams"])

    def test_public_status_whitelists_and_revalidates(self):
        status = dict(self.stats(_FULL_BAMBU_REPORT["print"]), online=True, secret="x",
                      access_code="c")
        public = self.plugin.public_status(status)
        for key in self.plugin._BAMBU_STAT_KEYS:
            self.assertIn(key, self.plugin._PUBLIC_STATUS_FIELDS)
        self.assertNotIn("secret", public)
        self.assertNotIn("access_code", public)
        self.assertEqual(public["ams"][0], {"type": "PLA", "color": "#FF8800", "remain": 80})
        hostile = self.plugin.public_status({
            "remaining_minutes": "5", "layer": 9, "total_layers": 3, "print_error": "<b>",
            "ams": [{"type": "PLA\"><img>", "color": "red"},
                    {"type": "PLA", "color": "red", "remain": 500}, 7]})
        self.assertEqual(hostile, {"remaining_minutes": 5, "layer": None,
                                   "total_layers": None, "print_error": None,
                                   "ams": [{"type": "PLA", "color": None, "remain": None}]})
        self.assertNotIn("ams", self.plugin.public_status({"online": True}))

    def test_poll_bambu_returns_stats_from_a_synthetic_report(self):
        topic = b"device/SERIAL/report"
        payload = json.dumps(_FULL_BAMBU_REPORT).encode()
        packets = iter([(0x20, b"\x00\x00"), (0x90, b"\x00\x01\x00"),
                        (0x30, struct.pack("!H", len(topic)) + topic + payload)])
        fake = mock.MagicMock()
        fake.getpeercert.return_value = b"synthetic Bambu TLS certificate"
        context = mock.MagicMock()
        context.wrap_socket.return_value = fake
        printer = _trusted_bambu_card(self, self.plugin, {
            "url": "http://192.0.2.1", "access_code": "x", "serial": "SERIAL"})
        with mock.patch.object(self.plugin.socket, "create_connection", return_value=fake), \
             mock.patch.object(self.plugin, "ssl_ctx_for", return_value=context), \
             mock.patch.object(self.plugin, "_mqtt_read_packet",
                               side_effect=lambda *args: next(packets)):
            status = self.plugin.poll_bambu(printer)
        self.assertIs(status["online"], True)
        self.assertEqual(status["state"], "printing")
        self.assertEqual((status["remaining_minutes"], status["layer"], status["total_layers"]),
                         (75, 120, 300))
        self.assertEqual(len(status["ams"]), 2)
        public = self.plugin.public_status(self.plugin._finalize_status(status, "bambu"))
        self.assertEqual(public["print_error"], "%08X" % 83935249)

    def render(self, status, fn="bambuStatsHtml"):
        page = self.plugin.PAGE
        script = ("function t(key, vars){ return key + (vars ? ':' + Object.keys(vars).map(function(k){ return vars[k]; }).join('/') : ''); }\n"
                  + page[page.index("function esc(s){"):page.index("function temp(v)")]
                  + page[page.index("// <bambu-stats>"):page.index("// </bambu-stats>")]
                  + "console.log(JSON.stringify(%s(%s)));" % (fn, json.dumps(status)))
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "case.js"
            path.write_text(script, encoding="utf-8")
            done = subprocess.run(["node", str(path)], capture_output=True, text=True,
                                  encoding="utf-8", timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    @unittest.skipUnless(__import__("shutil").which("node"), "node is not available")
    def test_card_rows_exist_only_for_data_and_escape_text(self):
        self.assertEqual(self.render({}), "")
        self.assertEqual(self.render({"ams": [], "remaining_minutes": None, "layer": 0,
                                      "total_layers": 0, "print_error": "00000000"}), "")
        html = self.render(self.plugin.public_status(self.stats(_FULL_BAMBU_REPORT["print"])))
        self.assertNotIn('bambuLeftHM', html)
        job = self.render(self.plugin.public_status(self.stats(_FULL_BAMBU_REPORT["print"])), "jobStatsHtml")
        self.assertIn('bambuLeftHM:1/15', job)
        self.assertIn('bambuLayer:120/300', job)
        self.assertIn(" · ", job)
        self.assertIn('<i style="background:#FF8800"></i>PLA 80%', html)
        self.assertIn("<i></i>PETG</span>", html)
        self.assertIn('bambuError:%08X' % 83935249, html)
        self.assertIn("bambuLeftM", self.render({"remaining_minutes": 7}, "jobStatsHtml"))
        hostile = self.render({"ams": [{"type": "<b>", "color": "red\"onload=x"},
                                       {"type": "PLA", "color": "#12\"><x", "remain": 5}],
                               "print_error": "<img>"})
        self.assertNotIn("<b>", hostile)
        self.assertNotIn("<img", hostile)
        self.assertNotIn("onload", hostile)
        self.assertIn("<i></i>PLA 5%", hostile)

    def test_card_only_adds_bambu_rows_for_the_bambu_backend(self):
        page = self.plugin.PAGE
        self.assertEqual(page.count("+= bambuStatsHtml(s)"), 1)
        self.assertEqual(page.count("+= jobStatsHtml(s)"), 1)
        self.assertIn("(jobKind === 'printing' || jobKind === 'paused')) fname += jobStatsHtml(s);", page)
        self.assertIn("=== 'bambu') fname += bambuStatsHtml(s);", page)


class ThumbnailFetchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin = _load_plugin()

    def setUp(self):
        self.plugin._thumb_cache.clear()

    def test_moonraker_thumbnail_path_is_relative_to_the_gcode_folder(self):
        meta = {"result": {"thumbnails": [{"size": 5, "relative_path": ".thumbs/a-32x32.png"},
                                          {"size": 9, "relative_path": ".thumbs/a-300x300.png"}]}}
        with mock.patch.object(self.plugin, "_get_json", return_value=meta),              mock.patch.object(self.plugin, "_get_bytes", return_value=b"PNG") as raw:
            uri = self.plugin.moonraker_thumbnail("http://h", None, "sub dir/a.gcode")
        self.assertTrue(uri.startswith("data:image/png;base64,"))
        self.assertEqual(raw.call_args.args[0], "http://h/server/files/gcodes/sub%20dir/.thumbs/a-300x300.png")

    def test_failed_thumbnail_read_is_retried_not_cached_for_the_whole_print(self):
        meta = {"result": {"thumbnails": [{"size": 9, "relative_path": "a.png"}]}}
        with mock.patch.object(self.plugin, "_get_json", side_effect=OSError("down")):
            self.assertEqual(self.plugin.moonraker_thumbnail("http://h", None, "a.gcode"), "")
        with mock.patch.object(self.plugin, "_get_json", side_effect=AssertionError("not yet")):
            self.assertEqual(self.plugin.moonraker_thumbnail("http://h", None, "a.gcode"), "")
        key = next(iter(self.plugin._thumb_cache))
        self.plugin._thumb_cache[key] = ("", 0)
        with mock.patch.object(self.plugin, "_get_json", return_value=meta),              mock.patch.object(self.plugin, "_get_bytes", return_value=b"PNG"):
            self.assertTrue(self.plugin.moonraker_thumbnail("http://h", None, "a.gcode").startswith("data:"))


class SharedJobStatsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin = _load_plugin()

    def moonraker(self, objects):
        reply = {"result": {"status": objects}}
        with mock.patch.object(self.plugin, "_moonraker_extruders", return_value=["extruder"]), \
             mock.patch.object(self.plugin, "_get_json", return_value=reply) as get, \
             mock.patch.object(self.plugin, "moonraker_thumbnail", return_value=""):
            status = self.plugin.poll_moonraker("http://192.0.2.1", None, {"X-Api-Key": "k"})
        self.assertEqual(get.call_count, 1)
        self.assertIn("virtual_sdcard", get.call_args.args[0])
        self.assertEqual(get.call_args.args[2], {"X-Api-Key": "k"})
        return status

    def test_moonraker_remaining_and_layers(self):
        status = self.moonraker({
            "print_stats": {"state": "printing", "filename": "a.gcode", "print_duration": 600.0,
                            "info": {"current_layer": 12, "total_layer": 120}},
            "virtual_sdcard": {"progress": 0.25}})
        self.assertEqual(status["remaining_seconds"], 1800)
        self.assertEqual((status["layer"], status["total_layers"]), (12, 120))
        paused = self.moonraker({"print_stats": {"state": "paused", "print_duration": 100},
                                 "virtual_sdcard": {"progress": 0.5}})
        self.assertEqual(paused["remaining_seconds"], 100)

    def test_moonraker_missing_or_invalid_data_is_none(self):
        status = self.moonraker({
            "print_stats": {"state": "printing", "print_duration": 50,
                            "info": {"current_layer": None, "total_layer": None}},
            "virtual_sdcard": {"progress": 0}})
        self.assertEqual((status["remaining_seconds"], status["layer"], status["total_layers"]),
                         (None, None, None))
        for sdcard in ({"progress": 0.009}, {"progress": True}, {"progress": 1.5}, {}, None):
            status = self.moonraker({"print_stats": {"state": "printing", "print_duration": 50},
                                     "virtual_sdcard": sdcard})
            self.assertIsNone(status["remaining_seconds"], sdcard)
        for info in ({"current_layer": 5, "total_layer": 3}, {"current_layer": True, "total_layer": 3},
                     {"current_layer": 1, "total_layer": 0}, {"current_layer": "1", "total_layer": 9},
                     {"current_layer": 1}, "x"):
            status = self.moonraker({"print_stats": {"state": "printing", "info": info}})
            self.assertEqual((status["layer"], status["total_layers"]), (None, None), info)
        idle = self.moonraker({"print_stats": {"state": "standby", "print_duration": 50,
                                               "info": {"current_layer": 1, "total_layer": 9}},
                               "virtual_sdcard": {"progress": 0.5}})
        self.assertEqual((idle["remaining_seconds"], idle["layer"], idle["total_layers"]),
                         (None, None, None))
        huge = self.moonraker({"print_stats": {"state": "printing", "print_duration": 10 ** 9},
                               "virtual_sdcard": {"progress": 0.02}})
        self.assertIsNone(huge["remaining_seconds"])

    def octoprint(self, job):
        def fake(url, ctx, headers=None, timeout=None):
            if url.endswith("/api/printer"):
                return {"state": {"text": "Printing"}, "temperature": {}}
            return job
        with mock.patch.object(self.plugin, "_get_json", side_effect=fake), \
             mock.patch.object(self.plugin, "octoprint_thumbnail", return_value=""):
            return self.plugin.poll_octoprint("http://192.0.2.1", None, "k")

    def test_octoprint_print_time_left(self):
        job = {"state": "Printing", "job": {"file": {"name": "a.gcode"}},
               "progress": {"completion": 10.2, "printTime": 60, "printTimeLeft": 3600}}
        status = self.octoprint(job)
        self.assertEqual(status["remaining_seconds"], 3600)
        self.assertEqual((status["layer"], status["total_layers"]), (None, None))
        for bad in (None, True, -1, "5", 10 ** 9, float("nan")):
            job["progress"]["printTimeLeft"] = bad
            self.assertIsNone(self.octoprint(job)["remaining_seconds"], bad)
        del job["progress"]["printTimeLeft"]
        self.assertIsNone(self.octoprint(job)["remaining_seconds"])
        job["progress"]["printTimeLeft"] = 90
        job["state"] = "Operational"
        self.assertIsNone(self.octoprint(job)["remaining_seconds"])

    def test_public_status_unifies_seconds_into_minutes_for_all_backends(self):
        public = self.plugin.public_status({"remaining_seconds": 4500, "layer": 3, "total_layers": 9})
        self.assertEqual((public["remaining_seconds"], public["remaining_minutes"]), (4500, 75))
        self.assertEqual((public["layer"], public["total_layers"]), (3, 9))
        self.assertEqual(self.plugin.public_status({"remaining_seconds": 61})["remaining_minutes"], 2)
        for bad in (True, "9", -5, 10 ** 12, None):
            public = self.plugin.public_status({"remaining_seconds": bad})
            self.assertIsNone(public["remaining_seconds"])
            self.assertNotIn("remaining_minutes", public)
        self.assertEqual(self.plugin.public_status({"remaining_seconds": 600, "remaining_minutes": 5})
                         ["remaining_minutes"], 5)
        self.assertIsNone(self.plugin.public_status({"layer": 0, "total_layers": 0})["layer"])

    @unittest.skipUnless(__import__("shutil").which("node"), "node is not available")
    def test_one_render_for_every_backend_and_escaping(self):
        render = BambuLanStatsTest("test_no_ams_means_no_ams_field").render
        public = self.plugin.public_status({"remaining_seconds": 4500, "layer": 3, "total_layers": 9})
        html = render(public, "jobStatsHtml")
        self.assertIn("bambuLeftHM:1/15", html)
        self.assertIn("bambuLayer:3/9", html)
        self.assertEqual(render({"remaining_seconds": 4500}, "jobStatsHtml"), "")
        self.assertEqual(render({}, "jobStatsHtml"), "")
        self.assertEqual(render({"layer": 0, "total_layers": 0}, "jobStatsHtml"), "")
        hostile = render({"remaining_minutes": 5, "layer": "<b>", "total_layers": 9}, "jobStatsHtml")
        self.assertIn("bambuLeftM:5", hostile)
        self.assertNotIn("<b>", hostile)


class CameraLookupTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin = _load_plugin()

    def lookup(self, printer, reply):
        calls = []

        def fake(url, ctx, headers=None, timeout=None):
            calls.append((url, dict(headers or {}), timeout))
            if isinstance(reply, BaseException):
                raise reply
            return reply

        with mock.patch.object(self.plugin, "_get_json", side_effect=fake):
            return self.plugin.camera_lookup(printer), calls

    @staticmethod
    def moon(**extra):
        return dict({"url": "http://voron.local:7125", "type": "moonraker", "apikey": "k1"}, **extra)

    @staticmethod
    def http_error(code):
        return urllib.error.HTTPError("http://x", code, "err", {}, None)

    def test_validated_camera_url_keeps_only_the_printer_host(self):
        ok = lambda value: self.plugin.validated_camera_url(value, "http://voron.local:7125")
        self.assertEqual(ok("/webcam/?action=stream"), "http://voron.local:7125/webcam/?action=stream")
        self.assertEqual(ok("http://VORON.local:8080/stream/"), "http://voron.local:8080/stream/")
        self.assertEqual(ok("https://voron.local/cam"), "https://voron.local/cam")
        self.assertEqual(ok("snap?x=1"), "http://voron.local:7125/snap?x=1")
        for bad in ("http://evil.example/x", "//evil.example/x", "http://user:pw@voron.local/x",
                    "http://voron.local.evil.example/x", "javascript:alert(1)", "ftp://voron.local/x",
                    "data:text/html,x", "http://voron.local/a b", "http://voron.local/\"x", "",
                    None, 5, "http://voron.local/" + "a" * 3000, "http://127.0.0.1/x"):
            self.assertEqual(ok(bad), "", bad)
        self.assertEqual(self.plugin.validated_camera_url("/x", "ftp://voron.local"), "")

    def test_moonraker_uses_first_enabled_camera_on_the_printer_host(self):
        reply = {"result": {"webcams": [
            {"name": "off", "enabled": False, "stream_url": "/off", "snapshot_url": "/off.jpg"},
            {"name": "foreign", "stream_url": "http://evil.example/s", "snapshot_url": "http://evil.example/p"},
            {"name": "main", "service": "mjpegstreamer", "stream_url": "/webcam/?action=stream",
             "snapshot_url": "http://voron.local:8080/snap"},
            {"name": "later", "stream_url": "/later"}]}}
        result, calls = self.lookup(self.moon(), reply)
        self.assertEqual(result, {"ok": True, "code": "",
                                  "stream": "http://voron.local:7125/webcam/?action=stream",
                                  "snapshot": "http://voron.local:8080/snap"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "http://voron.local:7125/server/webcams/list")
        self.assertEqual(calls[0][1].get("X-Api-Key"), "k1")
        self.assertEqual(calls[0][2], self.plugin.CAMERA_TIMEOUT)

    def test_moonraker_webrtc_stream_is_not_used_but_snapshot_is(self):
        reply = {"result": {"webcams": [{"service": "webrtc-camerastreamer", "stream_url": "/webrtc",
                                         "snapshot_url": "/snapshot"}]}}
        result, _ = self.lookup(self.moon(), reply)
        self.assertEqual((result["stream"], result["snapshot"]),
                         ("", "http://voron.local:7125/snapshot"))

    def test_moonraker_missing_unusable_or_failing_cameras_have_distinct_codes(self):
        for reply in ({"result": {"webcams": []}}, {"result": {}}, {}, [], {"result": {"webcams": [
                {"enabled": False, "stream_url": "/a"}, {"stream_url": "http://evil.example/a"}, 7]}}):
            result, _ = self.lookup(self.moon(), reply)
            self.assertEqual((result["ok"], result["code"]), (False, "no_camera"), reply)
        self.assertEqual(self.lookup(self.moon(), self.http_error(404))[0]["code"], "no_camera")
        self.assertEqual(self.lookup(self.moon(), self.http_error(500))[0]["code"], "unavailable")
        self.assertEqual(self.lookup(self.moon(), OSError("down"))[0]["code"], "unavailable")
        self.assertEqual(self.lookup({"url": "", "type": "moonraker"}, {})[0]["code"], "unavailable")

    def test_octoprint_reads_settings_with_the_api_key(self):
        printer = {"url": "http://octo.local", "type": "octoprint", "apikey": "oct-key"}
        reply = {"webcam": {"webcamEnabled": True, "streamUrl": "/webcam/?action=stream",
                            "snapshotUrl": "http://octo.local:8081/?action=snapshot"}}
        result, calls = self.lookup(printer, reply)
        self.assertEqual((result["stream"], result["snapshot"]), (
            "http://octo.local/webcam/?action=stream", "http://octo.local:8081/?action=snapshot"))
        self.assertEqual(calls[0][0], "http://octo.local/api/settings")
        self.assertEqual(calls[0][1].get("X-Api-Key"), "oct-key")

    def test_octoprint_disabled_foreign_and_default_paths(self):
        printer = {"url": "http://octo.local", "type": "octoprint", "apikey": "k"}
        self.assertEqual(self.lookup(printer, {"webcam": {"webcamEnabled": False,
                                                          "streamUrl": "/s"}})[0]["code"], "no_camera")
        foreign = {"webcam": {"streamUrl": "http://evil.example/s", "snapshotUrl": "http://evil.example/p"}}
        self.assertEqual(self.lookup(printer, foreign)[0]["code"], "no_camera")
        for reply in ({"webcam": {}}, {"webcam": {"webcamEnabled": True, "streamUrl": " "}}):
            self.assertEqual(self.lookup(printer, reply)[0]["code"], "no_camera", reply)
        # Only unreadable or unrecognised settings fall back to the standard paths.
        for reply in ({}, [], self.http_error(403)):
            result, _ = self.lookup(printer, reply)
            self.assertEqual((result["stream"], result["snapshot"]), (
                "http://octo.local/webcam/?action=stream", "http://octo.local/webcam/?action=snapshot"), reply)
        self.assertEqual(self.lookup(printer, OSError("down"))[0]["code"], "unavailable")

    def test_other_backends_have_no_camera(self):
        for kind in ("bambu", "duet", "crealityprint", "flashforge", "prusalink"):
            result, calls = self.lookup({"url": "http://p.local", "type": kind}, {})
            self.assertEqual((result["code"], calls), ("no_camera", []), kind)
        self.assertEqual(sorted(self.plugin._CAMERA_TYPES),
                         sorted({"moonraker", "klipper", "mainsail", "fluidd", "octoprint"}))

    def test_public_dto_is_whitelisted(self):
        printer = {"target_uuid": "t", "owner_scope_uuid": "o", "apikey": "secret", "url": "http://x"}
        dto = self.plugin.public_camera_info(
            {"ok": True, "stream": "http://x/s", "snapshot": 5, "apikey": "secret"}, 4, printer)
        self.assertEqual(dto, {"requestId": 4, "targetUuid": "t", "ownerScope": "o", "ok": True,
                               "code": "", "stream": "http://x/s", "snapshot": ""})
        bad = self.plugin.public_camera_info({"ok": False, "code": "weird", "stream": "http://x/s"}, 1, None)
        self.assertEqual((bad["ok"], bad["code"], bad["stream"]), (False, "unavailable", ""))
        self.assertNotIn("secret", json.dumps([dto, bad]))


class CameraDashboardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin = _load_plugin()

    def setUp(self):
        self.printer = self.plugin._ensure_printer_identity(
            {"id": 3, "name": "Voron", "url": "http://voron.local:7125", "type": "moonraker", "apikey": "k"})
        self.dashboard = self.plugin.PrintersDashboard()
        self.dashboard._post = mock.Mock(return_value=True)
        self.dashboard._work_is_active = lambda: True
        self.msg = {key: self.printer.get(src) for key, src in (
            ("targetUuid", "target_uuid"), ("ownerScope", "owner_scope_uuid"),
            ("targetSelector", "target_selector"), ("bindingRevision", "binding_revision"),
            ("url", "url"), ("connectionType", "type"))}

    def info(self, printers=None, **msg):
        with mock.patch.object(self.plugin, "dashboard_printers", return_value=printers or [self.printer]), \
             mock.patch.object(self.plugin, "_get_json", return_value={"result": {"webcams": [
                 {"stream_url": "/webcam/?action=stream"}]}}):
            self.dashboard._post_camera_info(dict(self.msg, **dict({"requestId": 9}, **msg)))
        return self.dashboard._post.call_args.args[0]["cameraInfo"]

    def test_lookup_reply_goes_through_post_and_never_carries_credentials(self):
        dto = self.info()
        self.assertEqual((dto["ok"], dto["requestId"], dto["targetUuid"]), (True, 9, self.printer["target_uuid"]))
        self.assertEqual(dto["stream"], "http://voron.local:7125/webcam/?action=stream")
        self.assertNotIn("apikey", json.dumps(dto))

    def test_unknown_target_and_unsupported_type_reply_without_a_lookup(self):
        with mock.patch.object(self.plugin, "_get_json", side_effect=AssertionError("no lookup")):
            with mock.patch.object(self.plugin, "dashboard_printers", return_value=[]):
                self.dashboard._post_camera_info(dict(self.msg, requestId=5))
            dto = self.dashboard._post.call_args.args[0]["cameraInfo"]
            self.assertEqual((dto["ok"], dto["code"], dto["requestId"]), (False, "unavailable", 5))
            bambu = self.plugin._ensure_printer_identity(
                {"id": 4, "name": "B", "url": "http://b.local", "type": "bambu"})
            msg = dict(self.msg, targetUuid=bambu["target_uuid"], ownerScope=bambu["owner_scope_uuid"],
                       targetSelector=bambu["target_selector"], bindingRevision=bambu["binding_revision"],
                       url=bambu["url"], connectionType="bambu", requestId=6)
            with mock.patch.object(self.plugin, "dashboard_printers", return_value=[bambu]):
                self.dashboard._post_camera_info(msg)
        self.assertEqual(self.dashboard._post.call_args.args[0]["cameraInfo"]["code"], "no_camera")

    def test_junk_request_id_is_bounded(self):
        for junk, expected in (("x", 0), (-4, 0), (True, 0), (10 ** 30, 2 ** 31)):
            self.assertEqual(self.info(requestId=junk)["requestId"], expected)

    def test_message_routing_uses_the_action_worker(self):
        with mock.patch.object(self.plugin.ACTION_WORKER, "submit") as submit:
            self.dashboard.on_message(dict(self.msg, type="camera-info", requestId=1))
        submit.assert_called_once()
        self.assertEqual(submit.call_args.args[0].__wrapped__, self.dashboard._post_camera_info)

    def test_open_in_window_uses_only_the_checked_cached_address(self):
        opened = []
        self.dashboard._open_viewer = lambda url, name: opened.append((url, name)) or True
        with mock.patch.object(self.plugin, "dashboard_printers", return_value=[self.printer]):
            self.dashboard.on_message(dict(self.msg, type="camera-open"))
            self.assertEqual(opened, [])  # nothing looked up yet
            self.info()
            self.dashboard.on_message(dict(self.msg, type="camera-open", url="http://evil.example/x"))
            self.assertEqual(opened, [])  # identity mismatch
            self.dashboard.on_message(dict(self.msg, type="camera-open"))
            self.assertEqual([url for url, _ in opened], ["http://voron.local:7125/webcam/?action=stream"])
            self.dashboard._camera_urls[self.printer["target_uuid"]] = "http://evil.example/x"
            self.dashboard.on_message(dict(self.msg, type="camera-open"))
        self.assertEqual(len(opened), 1)
        page = self.plugin.render_viewer_page(opened[0][0])
        self.assertIn("frame-src http://voron.local:7125", page)


@unittest.skipUnless(__import__("shutil").which("node"), "node is not available")
class CameraOverlayUiTest(unittest.TestCase):
    """Runs the page's own overlay code against a fake DOM and fake timers."""

    PRELUDE = r"""
var timers = [], clock = 0, timerSeq = 1;
function setTimeout(fn, ms){ var id = timerSeq++; timers.push({id:id, at:clock + ms, fn:fn}); return id; }
function clearTimeout(id){ timers = timers.filter(function(x){ return x.id !== id; }); }
function advance(ms){ var end = clock + ms;
  for (;;) { timers.sort(function(a, b){ return a.at - b.at; });
    if (!timers.length || timers[0].at > end) break;
    var next = timers.shift(); clock = next.at; next.fn(); }
  clock = end; }
function targetIdentity(p){ return {targetUuid:p.target_uuid, ownerScope:'o', url:p.url, connectionType:p.type}; }
var P1 = {target_uuid:'u1', name:'Voron', url:'http://v.local', type:'moonraker'};
var P2 = {target_uuid:'u2', name:'Octo', url:'http://o.local', type:'octoprint'};
var img = document.getElementById('cam-img'), modal = document.getElementById('cam-modal');
var note = document.getElementById('cam-note'), win = document.getElementById('cam-window');
var closeBtn = document.getElementById('cam-close');
var opener = new El('button');
function reply(id, extra){ onCameraInfo(Object.assign({requestId:id, targetUuid:'u1', ok:true, code:'',
  stream:'http://v.local/s', snapshot:'http://v.local/p.jpg'}, extra || {})); }
function report(extra){ console.log(JSON.stringify(Object.assign({src:img.src === undefined ? null : img.src,
  display:modal.style.display, note:note.textContent, win:win.style.display, loads:document.loads || [],
  posted:posted, focus:document.activeElement === opener}, extra || {}))); }
"""

    def run_js(self, body):
        report = _page_slice("// <camera-overlay>", "// </camera-overlay>")
        script = _FAKE_DOM_JS + self.PRELUDE + report + body
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "case.js"
            path.write_text(script, encoding="utf-8")
            done = subprocess.run(["node", str(path)], capture_output=True, text=True,
                                  encoding="utf-8", timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_open_requests_lookup_and_close_stops_the_stream(self):
        out = self.run_js("""
openCamera(P1, opener);
var opened = {posted: posted.slice(), display: modal.style.display, focus: document.activeElement === closeBtn,
  title: document.getElementById('cam-title').textContent};
reply(1);
var live = img.src;
closeBtn.fire('click');
advance(60000);
report({opened: opened, live: live});""")
        self.assertEqual(out["opened"]["posted"], [{
            "targetUuid": "u1", "ownerScope": "o", "url": "http://v.local", "connectionType": "moonraker",
            "type": "camera-info", "requestId": 1}])
        self.assertEqual((out["opened"]["display"], out["opened"]["focus"]), ("flex", True))
        self.assertEqual(out["opened"]["title"], "cameraTitle")
        self.assertEqual(out["live"], "http://v.local/s")
        self.assertIsNone(out["src"])
        self.assertEqual(out["display"], "none")
        self.assertEqual(out["loads"], ["http://v.local/s"])  # nothing after close
        self.assertTrue(out["focus"])

    def test_every_way_of_closing_stops_traffic(self):
        for closer in ("closeBtn.fire('click');", "document.key('Escape');",
                       "modal.fire('click', {target: modal});"):
            out = self.run_js("""
openCamera(P1, opener); reply(1);
img.onload();
%s
if (img.onload || img.onerror) throw new Error('handlers kept');
reply(1); advance(60000);
report();""" % closer)
            self.assertIsNone(out["src"], closer)
            self.assertEqual(out["display"], "none", closer)
            self.assertEqual(out["loads"], ["http://v.local/s"], closer)
            self.assertTrue(out["focus"], closer)

    def test_card_clicks_inside_the_dialog_do_not_close_it(self):
        out = self.run_js("""
openCamera(P1, opener); reply(1);
modal.fire('click', {target: img});
report();""")
        self.assertEqual(out["display"], "flex")
        self.assertEqual(out["src"], "http://v.local/s")

    def test_only_one_stream_at_a_time_and_stale_replies_are_ignored(self):
        out = self.run_js("""
openCamera(P1, opener); reply(1);
openCamera(P2, opener);
var between = img.src === undefined ? null : img.src;
reply(1);                                   // late answer of the first request
var afterStale = img.src === undefined ? null : img.src;
onCameraInfo({requestId:2, targetUuid:'u2', ok:true, stream:'http://o.local/s', snapshot:''});
report({between: between, afterStale: afterStale});""")
        self.assertIsNone(out["between"])
        self.assertIsNone(out["afterStale"])
        self.assertEqual(out["src"], "http://o.local/s")
        self.assertEqual(out["loads"], ["http://v.local/s", "http://o.local/s"])

    def test_reply_for_another_printer_is_ignored(self):
        out = self.run_js("openCamera(P1, opener); reply(1, {targetUuid:'u2'}); report();")
        self.assertIsNone(out["src"])
        self.assertEqual(out["note"], "cameraLoading")

    def test_error_codes_show_localized_messages_and_no_image(self):
        for reply, key in (("{ok:false, code:'no_camera'}", "cameraNone"),
                           ("{ok:false, code:'unavailable'}", "cameraUnavailable"),
                           ("{ok:false, code:'x'}", "cameraUnavailable"),
                           ("{stream:'', snapshot:''}", "cameraNone"),
                           ("{stream:'javascript:alert(1)', snapshot:'//evil/x'}", "cameraNone")):
            out = self.run_js("openCamera(P1, opener); reply(1, %s); report();" % reply)
            self.assertEqual(out["note"], key, reply)
            self.assertIsNone(out["src"], reply)
            self.assertEqual(out["win"], "none", reply)
            self.assertEqual(out["loads"], [], reply)

    def test_lookup_timeout_reports_unavailable(self):
        out = self.run_js("openCamera(P1, opener); advance(11999); var early = note.textContent;"
                          "advance(2); report({early: early});")
        self.assertEqual((out["early"], out["note"]), ("cameraLoading", "cameraUnavailable"))

    def test_failed_stream_falls_back_to_refreshing_snapshots(self):
        out = self.run_js("""
openCamera(P1, opener); reply(1);
img.onerror();
var first = img.src;
img.onload(); advance(1999); var early = document.loads.length;
advance(1); var second = img.src;
img.onload(); advance(2000); var third = img.src;
report({first: first, early: early, second: second, third: third});""")
        self.assertTrue(out["first"].startswith("http://v.local/p.jpg?_="))
        self.assertEqual(out["early"], 2)
        self.assertTrue(out["second"].startswith("http://v.local/p.jpg?_="))
        self.assertEqual(len(out["loads"]), 4)
        self.assertEqual(out["note"], "")

    def test_no_picture_offers_the_window_and_stops_the_stream(self):
        for failure in ("img.onerror();", "advance(6000);"):
            out = self.run_js("""
openCamera(P1, opener); reply(1, {snapshot:''});
%s
var shown = {src: img.src === undefined ? null : img.src, note: note.textContent, win: win.style.display};
win.fire('click');
report({shown: shown});""" % failure)
            self.assertEqual(out["shown"], {"src": None, "note": "cameraLoadFailed", "win": ""}, failure)
            self.assertEqual(out["display"], "none", failure)
            self.assertEqual(out["posted"][-1], {
                "targetUuid": "u1", "ownerScope": "o", "url": "http://v.local",
                "connectionType": "moonraker", "type": "camera-open"}, failure)
            self.assertEqual(out["loads"], ["http://v.local/s"], failure)

    def test_stream_that_never_fires_load_but_has_a_picture_is_kept(self):
        out = self.run_js("""
openCamera(P1, opener); reply(1, {snapshot:''});
img.naturalWidth = 640; advance(6000);
report();""")
        self.assertEqual((out["src"], out["note"], out["win"]), ("http://v.local/s", "", "none"))

    def test_page_losing_visibility_stops_the_stream(self):
        out = self.run_js("""
openCamera(P1, opener); reply(1);
document.hidden = true; document.l.visibilitychange.forEach(function(fn){ fn(); });
report();""")
        self.assertIsNone(out["src"])
        self.assertEqual(out["display"], "none")

    def test_tab_stays_inside_the_dialog(self):
        out = self.run_js("""
openCamera(P1, opener); reply(1, {snapshot:''});
document.key('Tab');
report({onClose: document.activeElement === closeBtn});""")
        self.assertTrue(out["onClose"])

    def test_no_camera_hides_the_icon_until_reload(self):
        out = self.run_js("""
var btn1 = new El('button'); btn1.dataset = {uuid:'u1'}; btn1.remove = function(){ this.gone = true; };
var btn2 = new El('button'); btn2.dataset = {uuid:'u2'}; btn2.remove = function(){ this.gone = true; };
var grid = {querySelectorAll: function(){ return [btn1, btn2].filter(function(b){ return !b.gone; }); }};
openCamera(P1, opener); reply(1, {ok:false, code:'no_camera', stream:'', snapshot:''});
var first = {gone1: !!btn1.gone, gone2: !!btn2.gone, mem: JSON.stringify(noCamera)};
openCamera(P2, opener); onCameraInfo({requestId:2, targetUuid:'u2', ok:false, code:'unavailable'});
report({first: first, gone2: !!btn2.gone, mem: JSON.stringify(noCamera)});""")
        self.assertEqual(out["first"], {"gone1": True, "gone2": False, "mem": '{"u1":true}'})
        self.assertEqual(out["note"], "cameraUnavailable")
        self.assertFalse(out["gone2"])  # a failed lookup is not "no camera"
        self.assertEqual(out["mem"], '{"u1":true}')

    def test_page_wires_the_button_only_for_camera_types(self):
        page = self.plugin_page()
        self.assertIn("var CAMERA_TYPES = " + json.dumps(["fluidd", "klipper", "mainsail", "moonraker", "octoprint"]) + ";", page)
        self.assertIn("if (data.cameraInfo) onCameraInfo(data.cameraInfo);", page)
        self.assertIn('role="dialog" aria-modal="true" aria-labelledby="cam-title"', page)
        self.assertNotIn("innerHTML", page[page.index("// <camera-overlay>"):page.index("// </camera-overlay>")])
        card = page[page.index("CAMERA_TYPES.indexOf"):page.index("// Bambu has no LAN")]
        self.assertIn("&& !noCamera[p.target_uuid]", card)
        self.assertIn("class=\"cam ghost\"", card)
        self.assertNotIn("p.url", card)

    @staticmethod
    def plugin_page():
        return _load_plugin().render_page()


class UnassignedDropAllTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin = _load_plugin()

    def test_drop_all_removes_only_unassigned_records_and_routes_through_the_worker(self):
        dash = self.plugin.PrintersDashboard()
        dash._post_status = mock.Mock()
        dash._refresh_worker = mock.Mock()
        index = [{"file": "unassigned/1__a"}, {"file": "uuid/2__b"}, {"file": "unassigned/3__c"}, {"x": 1}]
        with mock.patch.object(self.plugin, "load_outbox_index", return_value=index),              mock.patch.object(self.plugin, "outbox_remove", return_value=True) as remove,              mock.patch.object(self.plugin.ACTION_WORKER, "current_job_is_active", return_value=True):
            dash._drop_unassigned_and_refresh([])
        self.assertEqual([c.args[0] for c in remove.call_args_list], ["unassigned/1__a", "unassigned/3__c"])
        dash._post_status.assert_not_called()
        dash._refresh_worker.assert_called_once()
        with mock.patch.object(self.plugin.ACTION_WORKER, "submit") as submit:
            dash.on_message({"type": "outbox-unassigned-drop-all"})
        self.assertEqual(submit.call_args.args[0].__wrapped__, dash._drop_unassigned_and_refresh)

    def test_queued_files_expose_their_timestamp_for_sorting(self):
        public = self.plugin.public_printer_record({"queued": [
            {"file": "u/1", "name": "a", "size": 3, "ts": 7, "secret": "x"}]})
        self.assertEqual(public["queued"], [{"file": "u/1", "name": "a", "size": 3, "ts": 7}])


@unittest.skipUnless(__import__("shutil").which("node"), "node is not available")
class BusyActionsUiTest(unittest.TestCase):
    """Selecting and Print... are left out while a job runs; other reasons stay visible."""

    def run_js(self, body):
        script = _FAKE_DOM_JS + """
var ADAPTER_TYPES = ['duet', 'flashforge', 'crealityprint', 'prusalink'];
var ACTIVE_TYPES = ['moonraker', 'klipper', 'octoprint', 'bambu', 'duet', 'flashforge'];
var current = [], selected = {}, sendBtn = document.getElementById('send');
""" + _page_slice("function stateKind(s){", "function statusControlReady(p, action){") \
            + _page_slice("function updateSendBtn(){", "document.getElementById('sel-all').addEventListener") + body
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "case.js"
            path.write_text(script, encoding="utf-8")
            done = subprocess.run(["node", str(path)], capture_output=True, text=True,
                                  encoding="utf-8", timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_busy_only_for_online_printing_or_paused_printers(self):
        out = self.run_js("""
function P(type, status){ return {type:type, target_uuid:'u', status:status}; }
console.log(JSON.stringify([
  printerBusy(P('bambu', {online:true, state:'printing'})),
  printerBusy(P('octoprint', {online:true, state:'Paused'})),
  printerBusy(P('duet', {online:true, state:'processing', upload_reason:'state_printing'})),
  printerBusy(P('octoprint', {online:true, state:'operational'})),
  printerBusy(P('octoprint', {online:false, state:'printing'})),
  printerBusy(P('moonraker', {online:true, state:'standby', upload_reason:'hardware_unverified'})),
  printerBusy(P('duet', {online:true, state:'halted', upload_reason:'state_halted'})),
  printerBusy(P('octoprint', {online:true, supported:false, state:'printing'}))]));""")
        self.assertEqual(out, [True, True, True, False, False, False, False, False])

    def test_busy_printer_leaves_the_batch_and_the_counter_follows(self):
        out = self.run_js("""
var idle = {type:'octoprint', target_uuid:'a', owner_scope_uuid:'o', binding_revision:'1', status:{
  backend:'octoprint', supported:true, online:true, transport_online:true, authorization:'accepted',
  device_health:'healthy', freshness:'fresh', observed_at:Date.now() / 1000, state:'operational',
  job_state:'operational'}};
var busy = JSON.parse(JSON.stringify(idle)); busy.target_uuid = 'b';
current = [idle, busy]; selected[targetKey(idle)] = true; selected[targetKey(busy)] = true;
updateSendBtn(); var before = [sendBtn.textContent, sendBtn.disabled];
busy.status.state = busy.status.job_state = 'printing';
var sel = printerSelectable(busy);
if (!sel) selected[targetKey(busy)] = false;  // what render() does for every card
updateSendBtn();
console.log(JSON.stringify({before: before, sel: sel, after: [sendBtn.textContent, sendBtn.disabled]}));""")
        self.assertEqual(out["before"], ["sendSelectedCount", False])
        self.assertFalse(out["sel"])
        self.assertEqual(out["after"], ["sendSelectedCount", False])

    def test_card_markup_omits_controls_only_for_busy(self):
        page = _load_plugin().PAGE
        start = page.index("var hideSend = printerBusy(p);")
        card = page[start:page.index("tile.innerHTML +=", start)]
        self.assertIn("var selectHtml = hideSend ? ''", card)
        self.assertIn("var printHtml = hideSend ? ''", card)
        self.assertIn("select-control", card)
        self.assertIn("printAttrs", card)  # other reasons keep the disabled button and its title


@unittest.skipUnless(__import__("shutil").which("node"), "node is not available")
class CardActionsLayoutTest(unittest.TestCase):
    """The card's action rows: one wrapping row without Pause/Stop, no empty rows or slots."""

    def cards(self, cases):
        script = _FAKE_DOM_JS + r"""
function esc(s){ return String(s == null ? '' : s).replace(/[&<>"]/g, function(c){
  return {'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;'}[c]; }); }
t = function(key){ return key; };
var ADAPTER_TYPES = ['duet', 'flashforge', 'crealityprint', 'prusalink'];
var ACTIVE_TYPES = ['moonraker', 'klipper', 'octoprint', 'bambu', 'duet', 'flashforge'];
var CAMERA_TYPES = ['moonraker', 'klipper', 'octoprint', 'bambu'], noCamera = {};
var current = [], selected = {};
function targetKey(p){ return p.target_uuid; }
function statusControlReady(p, action){ return true; }
""" + _page_slice("function stateKind(s){", "function statusControlReady(p, action){") \
            + _page_slice("function cardActionsHtml(parts){", "function render(printers){") + r"""
function build(p, noCam){
  var s = p.status, supported = true;
  var multiTool = Array.isArray(s.tools) && s.tools.length > 1; noCamera = noCam ? {u: true} : {};
  var tile = {innerHTML: ''};
""" + _page_slice("    var kind = stateKind(s);", "    var selBox") + r"""
  return tile.innerHTML;
}
function rowsOf(html){   // [{cls, items:[button/label classes in order]}]
  var out = [], re = /<div class="(ctl-row|row)">(.*?)<\/div>(?=<div class="(?:ctl-row|row)">|$)/g, m;
  var inner = html.replace(/^<div class="tile-actions">/, '').replace(/<\/div>$/, '');
  while ((m = re.exec(inner))) {
    var items = [], r2 = /<(label|button|span) class="([^"]*)"/g, k;
    while ((k = r2.exec(m[2]))) items.push(k[2].split(' ')[0]);
    out.push({cls: m[1], items: items});
  }
  return {rows: out, empty: /<div class="(ctl-row|row)"><\/div>/.test(html) ||
    /class="ctl-slot"><\/span>/.test(html)};
}
function P(type, state, online){ return {type:type, url:'http://x', target_uuid:'u', status:{
  backend:type, supported:true, online:online !== false, transport_online:online !== false,
  authorization:'accepted', device_health:'healthy', freshness:'fresh', observed_at:Date.now() / 1000,
  state:state, job_state:state}}; }
var out = {};
""" + cases + "\nconsole.log(JSON.stringify(out));"
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "case.js"
            path.write_text(script, encoding="utf-8")
            done = subprocess.run(["node", str(path)], capture_output=True, text=True,
                                  encoding="utf-8", timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_idle_card_is_one_row_in_tab_order(self):
        out = self.cards("""
out.moon = rowsOf(build(P('moonraker', 'standby'), false));
out.bambu = rowsOf(build(P('bambu', 'idle'), false));""")
        self.assertEqual(out["moon"]["rows"], [{"cls": "row", "items":
                         ["select-control", "history", "connection-options", "print", "cam", "open"]}])
        self.assertEqual(out["bambu"]["rows"], [{"cls": "row", "items":
                         ["select-control", "history", "connection-options", "print", "tls-trust", "cam"]}])
        self.assertFalse(out["moon"]["empty"] or out["bambu"]["empty"])

    def test_feedback_page_offers_copy_with_confirmation_and_no_navigation(self):
        plugin = _load_plugin()
        page = plugin.render_feedback_page()
        self.assertIn('id="copy"', page)
        self.assertIn('value="https://filamenthub.ru"', page)
        self.assertIn("default-src 'none'", page)
        self.assertNotIn("<a ", page)
        self.assertNotIn("http-equiv=\"refresh\"", page)
        self.assertIn(plugin.ui_text("feedbackCopied"), page)

    def test_bambu_card_highlights_certificates_only_when_trust_is_missing(self):
        out = self.cards("""
var needed = P('bambu', 'offline', false); needed.status.error_code = 'tls_trust_required';
var fine = P('bambu', 'idle');
var other = P('moonraker', 'offline', false); other.status.error_code = 'tls_trust_required';
out.needed = /class="tls-trust primary"/.test(build(needed, false));
out.fine = /class="tls-trust ghost"/.test(build(fine, false));
out.other = tlsTrustNeeded(other, other.status);
out.flag = tlsTrustNeeded(needed, needed.status);""")
        self.assertEqual(out, {"needed": True, "fine": True, "other": False, "flag": True})

    def test_missing_camera_or_open_keeps_the_single_row_intact(self):
        out = self.cards("""
var u = P('moonraker', 'standby'); u.type = 'prusalink'; u.status.backend = 'prusalink';
out.nocam = rowsOf(build(P('moonraker', 'standby'), true));
out.nocamOpen = rowsOf(build(P('bambu', 'standby'), true));
out.nocamIdleOnlyOpen = rowsOf(build(u, false));""")
        self.assertEqual(out["nocam"]["rows"][0]["items"], ["select-control", "history", "connection-options", "print", "open"])
        self.assertEqual(out["nocamOpen"]["rows"], [{"cls": "row", "items":
                         ["select-control", "history", "connection-options", "print", "tls-trust"]}])
        self.assertEqual(out["nocamIdleOnlyOpen"]["rows"][0]["items"],
                         ["select-control", "history", "connection-options", "print", "open"])

    def test_offline_card_keeps_the_disabled_actions_in_the_same_row(self):
        out = self.cards("""
var html = build(P('moonraker', 'offline', false), false);
out.rows = rowsOf(html); out.disabledPrint = /<button class="print ghost" disabled/.test(html);""")
        self.assertEqual(len(out["rows"]["rows"]), 1)
        self.assertEqual(out["rows"]["rows"][0]["items"],
                         ["select-control", "history", "connection-options", "print", "cam", "open"])
        self.assertTrue(out["disabledPrint"])

    def test_printing_keeps_history_and_stop_together_without_select_or_print(self):
        out = self.cards("""
out.moon = rowsOf(build(P('moonraker', 'printing'), false));
out.paused = rowsOf(build(P('octoprint', 'paused'), false));
out.bambu = rowsOf(build(P('bambu', 'printing'), true));""")
        self.assertEqual(out["moon"]["rows"], [
            {"cls": "ctl-row", "items": ["history", "connection-options", "ctl-slot", "ctl", "ctl"]},
            {"cls": "row", "items": ["cam", "open"]}])
        self.assertEqual(out["paused"]["rows"][0]["items"], ["history", "connection-options", "ctl-slot", "ctl", "ctl"])
        # Nothing left for the second row: it is not drawn at all.
        self.assertEqual(out["bambu"]["rows"], [{"cls": "ctl-row",
                                                  "items": ["history", "connection-options", "tls-trust", "ctl-slot", "ctl", "ctl"]}])
        for key in ("moon", "paused", "bambu"):
            self.assertFalse(out[key]["empty"])

    def test_busy_without_controls_still_has_one_row(self):
        out = self.cards("""
var p = P('octoprint', 'printing'); statusControlReady = function(){ return false; };
out.rows = rowsOf(build(p, true));""")
        self.assertEqual(len(out["rows"]["rows"]), 1)
        self.assertEqual(out["rows"]["rows"][0]["items"], ["history", "connection-options", "open"])

    def test_row_css_pushes_actions_right_and_selection_left(self):
        page = _load_plugin().PAGE
        self.assertIn(".row { display:flex; flex-wrap:wrap; justify-content:flex-end;", page)
        self.assertIn(".row label { display:flex; align-items:center; gap:4px; font-size:11px; margin-right:auto; }", page)


class _MoonrakerEmulator:
    """Loopback Moonraker: records uploads (path + raw body) and answers 201."""

    def __init__(self):
        self.requests = []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                owner.requests.append((self.path, self.rfile.read(length)))
                self.send_response(201)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


class UnassignedSendTest(unittest.TestCase):
    """One-off send of a file linked to no printer, to a printer the user picks."""

    PRESET = "Voron 2.4 350"
    CONTENT = ("G28\n; printer_settings_id = %s\nG1 X1\n" % PRESET).encode()
    adopted = StartCompatibilityTest.adopted
    manual = StartCompatibilityTest.manual
    identity = staticmethod(StartCompatibilityTest.identity)

    def setUp(self):
        self.plugin = _load_plugin()
        self.dashboard = self.plugin.PrintersDashboard()
        self.dashboard._post = mock.Mock(return_value=True)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.emulator = _MoonrakerEmulator()
        self.addCleanup(self.emulator.close)
        for patch in (mock.patch.object(self.plugin, "PLUGIN_STORAGE_DIR", self.temp.name),
                      mock.patch.object(self.plugin, "_STORAGE_DENIED", False),
                      mock.patch.object(self.plugin, "OUTBOX_DIR", str(self.root / "outbox")),
                      mock.patch.object(self.dashboard, "_refresh_worker")):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.dashboard._stop_action_scheduler)
        self.status = _fresh_status(self.plugin, "moonraker", "standby", "standby")
        self.printer = self.manual("Target", self.emulator.url)
        self.other = self.manual("Other", "http://other.local")
        self.stored = "unassigned/1__job.gcode"
        self.records = [{"file": self.stored, "name": "job.gcode",
                         "sha256": hashlib.sha256(self.CONTENT).hexdigest(), "size": len(self.CONTENT)}]
        self.path = self.root / "outbox" / "unassigned" / "1__job.gcode"
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(self.CONTENT)
        self.original_records = json.loads(json.dumps(self.records))

    def ctx(self, status=None, printers=None):
        stack = [
            mock.patch.object(self.plugin, "dashboard_printers",
                              return_value=printers or [self.printer, self.other]),
            mock.patch.object(self.plugin, "load_outbox_index", side_effect=lambda: self.records),
            mock.patch.object(self.plugin, "poll_printer", return_value=status or self.status),
        ]
        for patch in stack:
            patch.start()
            self.addCleanup(patch.stop)

    def prepare(self, printer=None, start=False, stored=None):
        self.dashboard._post.reset_mock()
        self.dashboard._prepare_outbox_start({
            **self.identity(printer or self.printer), "file": stored or self.stored,
            "unassigned": True, "start": start})
        for call in self.dashboard._post.call_args_list:
            if "outboxStartApproval" in call.args[0]:
                return call.args[0]["outboxStartApproval"]
        return None

    def request(self, approval, printer=None, start=False, **extra):
        return {**self.identity(printer or self.printer), "file": self.stored, "unassigned": True,
                "start": start, "approvalNonce": approval["nonce"], "use_ams": start,
                "operatorChecked": True, "bedClear": start, **extra}

    def send(self, request):
        self.dashboard._do_outbox_send(dict(request))

    def uploads(self):
        return [body for path, body in self.emulator.requests if path == "/server/files/upload"]

    def statuses(self):
        return [c.args[0]["status"] for c in self.dashboard._post.call_args_list
                if "status" in c.args[0]]

    def journal(self):
        journal = self.plugin._pf_journal.OperationJournal(
            os.path.join(os.path.realpath(self.root), "operations.sqlite3"))
        try:
            return journal.list_operations(self.plugin.PrintersDashboard._journal_account_id())
        finally:
            journal.close()

    def test_upload_only_reaches_the_emulator_without_starting_or_relinking(self):
        self.ctx()
        approval = self.prepare()
        self.assertEqual((approval["verdict"], approval["unassigned"], approval["start"]),
                         ("unknown", True, False))
        self.assertEqual(approval["target"]["targetUuid"], self.printer["target_uuid"])
        self.send(self.request(approval))
        bodies = self.uploads()
        self.assertEqual(len(bodies), 1)
        self.assertIn(self.CONTENT, bodies[0])
        self.assertNotIn(b'name="print"', bodies[0])
        self.assertEqual(self.records, self.original_records, "the index record must stay unbound")
        operations = self.journal()
        self.assertEqual([o["action"] for o in operations], ["upload"])
        check = operations[0]["target_binding"]["start_check"]
        self.assertEqual((check["verdict"], check["operator_checked"], check["bed_clear"]),
                         ("unknown", True, False))

    def test_send_from_storage_journals_the_exact_file_by_digest(self):
        self.ctx()
        digest = hashlib.sha256(self.CONTENT).hexdigest()
        with mock.patch.object(self.plugin, "PRINT_HISTORY_FILE", str(self.root / "history.json")):
            self.send(self.request(self.prepare()))
            self.send(self.request(self.prepare(start=True), start=True))
            records = self.plugin.load_print_history()
            by_state = {item["state"]: item for item in records}
            self.assertEqual(set(by_state), {"sent", "printing"})
            for item in records:
                self.assertEqual((item["sha256"], item["filename"], item["source"]),
                                 (digest, "job.gcode", "print_farm"))
            self.dashboard._post.reset_mock()
            self.dashboard._post_print_history_all({"requestId": 1})
            payload = self.dashboard._post.call_args.args[0]["printHistory"]
            self.assertEqual({item["file"] for item in payload["items"]}, {self.stored})

    def test_refused_send_writes_nothing_to_the_journal(self):
        self.ctx()
        approval = self.prepare()
        with mock.patch.object(self.plugin, "PRINT_HISTORY_FILE", str(self.root / "history.json")):
            self.send({**self.request(approval), "approvalNonce": "forged"})
            self.assertEqual(self.plugin.load_print_history(), [])

    def test_upload_and_start_sends_print_true(self):
        self.ctx()
        approval = self.prepare(start=True)
        self.send(self.request(approval, start=True))
        bodies = self.uploads()
        self.assertEqual(len(bodies), 1)
        self.assertRegex(bodies[0], rb'name="print"\r\n\r\ntrue')
        operations = self.journal()
        self.assertEqual([o["action"] for o in operations], ["start"])
        self.assertTrue(operations[0]["target_binding"]["start_check"]["bed_clear"])

    def test_marks_are_mandatory(self):
        self.ctx()
        approval = self.prepare(start=True)
        self.send(self.request(approval, start=True, operatorChecked=False))
        self.send(self.request(approval, start=True, bedClear=False))
        self.send(self.request(approval, start=True, operatorChecked=None))
        self.assertEqual(self.uploads(), [])
        self.assertIn(self.plugin.ui_text("startConfirmMissing"), self.statuses())
        approval = self.prepare()
        self.send(self.request(approval, operatorChecked=False))
        self.assertEqual(self.uploads(), [])

    def test_missing_wrong_and_replayed_nonce_are_refused(self):
        self.ctx()
        approval = self.prepare()
        self.send({**self.request(approval), "approvalNonce": None})
        self.send({**self.request(approval), "approvalNonce": "forged"})
        self.assertEqual(self.uploads(), [])
        self.send(self.request(approval))
        self.assertEqual(len(self.uploads()), 1)
        self.send(self.request(approval))
        self.assertEqual(len(self.uploads()), 1, "a used approval must not send again")

    def test_digest_binds_mode_file_and_target(self):
        self.ctx()
        approval = self.prepare(start=False)
        self.send(self.request(approval, start=True))  # an upload approval cannot start a print
        self.assertEqual(self.uploads(), [])
        approval = self.prepare(start=False)
        self.send(self.request(approval, printer=self.other))  # approval of another target
        self.assertEqual(self.uploads(), [])
        digests = {mode: self.prepare(start=mode)["digest"] for mode in (False, True)}
        self.assertNotEqual(digests[False], digests[True])
        self.assertNotEqual(self.prepare(printer=self.other)["digest"], digests[False])

    def test_foreign_or_stale_target_is_not_accepted(self):
        self.ctx()
        approval = self.prepare()
        for key, value in (("bindingRevision", 99), ("ownerScope", "x"), ("targetUuid", "nope"),
                           ("targetSelector", "other"), ("connectionType", "octoprint"),
                           ("url", "http://elsewhere.local")):
            self.send({**self.request(approval), key: value})
        self.assertEqual(self.uploads(), [])
        self.dashboard._prepare_outbox_start({
            **self.identity(self.printer), "bindingRevision": 99, "file": self.stored,
            "unassigned": True, "start": False})
        self.assertEqual(self.statuses()[-1], self.plugin.ui_text("startApprovalExpired"))

    def test_replaced_file_voids_the_plan(self):
        self.ctx()
        approval = self.prepare(start=True)
        os.chmod(self.path, 0o600)
        self.path.write_bytes(self.CONTENT + b"G1 X999\n")
        self.send(self.request(approval, start=True))
        self.assertEqual(self.uploads(), [])
        self.assertIn(self.plugin.ui_text("fileReadFailed"), self.statuses())

    def test_replaced_file_with_updated_hash_in_index_changes_the_digest(self):
        self.ctx()
        approval = self.prepare()
        changed = self.CONTENT + b"G1 X999\n"
        os.chmod(self.path, 0o600)
        self.path.write_bytes(changed)
        self.records[0]["sha256"] = hashlib.sha256(changed).hexdigest()
        self.send(self.request(approval))
        self.assertEqual(self.uploads(), [])
        self.assertIn(self.plugin.ui_text("startApprovalExpired"), self.statuses())

    def test_busy_or_unready_printer_gets_nothing(self):
        for state in ("printing", "paused"):
            status = _fresh_status(self.plugin, "moonraker", state, state)
            self.ctx(status=status)
            for start in (False, True):
                approval = self.prepare(start=start)
                self.send(self.request(approval, start=start))
        self.assertEqual(self.uploads(), [])
        offline = self.plugin._finalize_status({"online": False, "state": "offline"}, "moonraker")
        self.ctx(status=offline)
        approval = self.prepare()
        self.send(self.request(approval))
        self.assertEqual(self.uploads(), [])

    def test_invalid_or_foreign_paths_are_refused(self):
        self.ctx()
        for bad in ("unassigned/../outside.gcode", "../x", "unassigned/a/b", "unassigned\\x",
                    "unassigned/missing.gcode"):
            self.records.append({"file": bad, "name": "x", "sha256": "0" * 64})
            self.assertIsNone(self.prepare(stored=bad), bad)
            self.dashboard._do_outbox_send({**self.identity(self.printer), "file": bad,
                                            "unassigned": True, "start": False,
                                            "approvalNonce": "x", "operatorChecked": True})
        self.assertEqual(self.uploads(), [])

    def test_explicit_mode_is_only_for_unbound_unassigned_records(self):
        self.ctx()
        bound = {**self.records[0], "file": self.printer["target_uuid"] + "/job.gcode",
                 "target_uuid": self.printer["target_uuid"]}
        (self.root / "outbox" / self.printer["target_uuid"]).mkdir()
        (self.root / "outbox" / bound["file"]).write_bytes(self.CONTENT)
        self.records.append(bound)
        self.assertIsNone(self.prepare(printer=self.other, stored=bound["file"]))
        tagged = {**self.records[0], "file": "unassigned/2__t.gcode",
                  "target_uuid": self.other["target_uuid"]}
        self.records.append(tagged)
        self.assertIsNone(self.prepare(stored=tagged["file"]))
        # Without the explicit flag an unassigned record still matches nothing.
        self.dashboard._post.reset_mock()
        self.dashboard._prepare_outbox_start({**self.identity(self.printer), "file": self.stored})
        self.assertNotIn("outboxStartApproval",
                         " ".join(str(c.args[0]) for c in self.dashboard._post.call_args_list))
        self.dashboard._do_outbox_send({**self.identity(self.printer), "file": self.stored,
                                        "start": False})
        self.assertEqual(self.uploads(), [])

    def test_incompatible_file_kind_is_blocked_with_a_reason(self):
        bambu = self.manual("Bambu", "http://bambu.local", host_type="bambu")
        self.ctx(printers=[bambu])
        approval = self.prepare(printer=bambu)
        self.assertEqual((approval["verdict"], approval["reason"], approval["nonce"]),
                         ("incompatible", "startReasonSlicedOnly", None))
        self.send({**self.identity(bambu), "file": self.stored, "unassigned": True, "start": False,
                   "approvalNonce": "x", "operatorChecked": True})
        self.assertEqual(self.uploads(), [])
        self.assertIn(self.plugin.ui_text(
            "startBlocked", printer="Bambu", reason=self.plugin.ui_text("startReasonSlicedOnly")),
            self.statuses())

    def test_profile_match_is_not_enough_the_operator_check_stays_mandatory(self):
        card = self.adopted(url=self.emulator.url)
        self.ctx(printers=[card])
        approval = self.prepare(printer=card)
        self.assertEqual(approval["verdict"], "unknown")
        self.send({**self.request(approval, printer=card), "operatorChecked": False})
        self.assertEqual(self.uploads(), [])


class GcodePrinterParseTest(unittest.TestCase):
    BLOCK = ("; CONFIG_BLOCK_START\n; printer_model = Bambu Lab P1S\n"
             "; printer_settings_id = Bambu Lab P1S 0.4 nozzle\n; CONFIG_BLOCK_END\n")

    def setUp(self):
        self.plugin = _load_plugin()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def parse(self, head="", body=b"", tail=""):
        path = pathlib.Path(self.temp.name) / "f.gcode"
        with open(path, "wb") as fh:
            fh.write(("; HEADER_BLOCK_START\n; x\n; HEADER_BLOCK_END\n" + head).encode())
            fh.write(body)
            fh.write(tail.encode())
        return self.plugin.parse_gcode_printer(str(path))

    def test_block_at_start_at_end_in_both_or_nowhere(self):
        expected = ("Bambu Lab P1S 0.4 nozzle", "Bambu Lab P1S")
        filler = b"G1 X1 Y1\n" * 200000  # ~1.8 MB: the start and end windows do not overlap
        self.assertEqual(self.parse(head=self.BLOCK, body=filler), expected)
        self.assertEqual(self.parse(body=filler, tail=self.BLOCK), expected)
        self.assertEqual(self.parse(head=self.BLOCK, body=filler, tail=self.BLOCK), expected)
        self.assertEqual(self.parse(body=filler), ("", ""))
        self.assertEqual(self.parse(head=self.BLOCK), expected)

    def test_tail_value_wins_when_both_exist(self):
        tail = self.BLOCK.replace("P1S", "X1C")
        self.assertEqual(self.parse(head=self.BLOCK, body=b"G1\n" * 400000, tail=tail),
                         ("Bambu Lab X1C 0.4 nozzle", "Bambu Lab X1C"))

    def test_config_in_the_middle_of_a_big_file_is_not_read(self):
        filler = b"G1 X1\n" * 400000
        self.assertEqual(self.parse(body=filler + self.BLOCK.encode() + filler), ("", ""))

    def test_a_30_mb_file_without_a_block_is_bounded_in_memory_and_time(self):
        import time
        import tracemalloc
        path = pathlib.Path(self.temp.name) / "big.gcode"
        chunk = b"G1 X10.000 Y10.000 E0.01234\n" * 1000
        with open(path, "wb") as fh:
            for _ in range(30 * 1024 * 1024 // len(chunk) + 1):
                fh.write(chunk)
        self.assertGreater(path.stat().st_size, 30 * 1024 * 1024)
        tracemalloc.start()
        started = time.monotonic()
        try:
            result = self.plugin.parse_gcode_printer(str(path))
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(result, ("", ""))
        self.assertLess(peak, 12 * 1024 * 1024)
        self.assertLess(time.monotonic() - started, 5)

    def test_missing_file_and_binary_noise(self):
        self.assertEqual(self.plugin.parse_gcode_printer(str(pathlib.Path(self.temp.name) / "no")),
                         ("", ""))
        path = pathlib.Path(self.temp.name) / "one.gcode"
        path.write_bytes(b"\x00" * (3 * 1024 * 1024))
        self.assertEqual(self.plugin.parse_gcode_printer(str(path)), ("", ""))


class HostLanguageAndOrcaKeyTest(unittest.TestCase):
    def setUp(self):
        self.plugin = _load_plugin()

    def tearDown(self):
        self.plugin._test_storage.cleanup()

    def test_language_comes_from_app_info_with_fallbacks(self):
        host = self.plugin.orca.host
        host.app_info = lambda: {"language": "de_DE"}
        self.assertEqual(self.plugin.host_ui_language(), "de")
        host.app_info = lambda: types.SimpleNamespace(language="ru")
        self.assertEqual(self.plugin.host_ui_language(), "ru")
        host.app_info = mock.Mock(side_effect=RuntimeError("not on UI thread"))
        host.app_language = lambda: "fr_FR"
        self.assertEqual(self.plugin.host_ui_language(), "fr")
        del host.app_info
        del host.app_language
        self.assertEqual(self.plugin.host_ui_language(), "")

    def _adopted(self, **extra):
        record = {"id": 1, "name": "Preset", "url": "http://p.local", "type": "moonraker",
                  "source": "orca", "discovery_ref": "ref-1"}
        record.update(extra)
        return self.plugin._ensure_printer_identity(record)

    def _stored(self):
        with open(self.plugin.PRINTERS_FILE, encoding="utf-8") as fh:
            return json.load(fh)

    def test_preset_key_is_used_but_not_written_to_disk(self):
        plugin = self.plugin
        plugin.remember_orca_keys([{"discovery_ref": "ref-1", "apikey": "PRESET-KEY"}])
        self.assertTrue(plugin.save_manual_printers([self._adopted(apikey="PRESET-KEY")]))
        self.assertNotIn("PRESET-KEY", open(plugin.PRINTERS_FILE, encoding="utf-8").read())
        printer = plugin.dashboard_printers()[0]
        self.assertEqual(printer["apikey"], "PRESET-KEY")
        self.assertNotIn("apikey", plugin.public_printer_record(printer))
        self.assertNotIn("PRESET-KEY", json.dumps(plugin.public_printer_record(printer)))

    def test_old_saved_key_is_replaced_by_preset_key_and_scrubbed(self):
        plugin = self.plugin
        plugin._PRINTERS_STORE_DAMAGED = False
        plugin.save_manual_printers([self._adopted(apikey="OLD-COPY")])
        self.assertEqual(self._stored()[0]["apikey"], "OLD-COPY")
        plugin.migrate_orca_keys()  # preset key unknown yet: nothing is lost
        self.assertEqual(self._stored()[0]["apikey"], "OLD-COPY")
        self.assertEqual(plugin.dashboard_printers()[0]["apikey"], "OLD-COPY")
        plugin.remember_orca_keys([{"discovery_ref": "ref-1", "apikey": "NEW-KEY"}])
        self.assertEqual(plugin.dashboard_printers()[0]["apikey"], "NEW-KEY")
        plugin.migrate_orca_keys()
        self.assertNotIn("apikey", self._stored()[0])

    def test_preset_without_key_keeps_saved_key_and_manual_cards_untouched(self):
        plugin = self.plugin
        manual = {"id": 2, "name": "M", "url": "http://m.local", "type": "moonraker",
                  "source": "manual", "apikey": "MANUAL-KEY"}
        plugin.save_manual_printers([self._adopted(apikey="KEPT"), manual])
        plugin.remember_orca_keys([{"discovery_ref": "ref-1", "apikey": ""},
                                   {"discovery_ref": "ref-2", "apikey": "OTHER"}])
        plugin.migrate_orca_keys()
        stored = {p["id"]: p for p in self._stored()}
        self.assertEqual(stored[1]["apikey"], "KEPT")
        self.assertEqual(stored[2]["apikey"], "MANUAL-KEY")


class _Enum:
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return "LifecycleEvent." + self.name


def _fake_lifecycle(plugin):
    names = ("GCodeExportStarted", "GCodeExportFinished", "SlicingJobComplete",
             "PrintJobStarted", "PrintJobFinished", "SendJobStarted", "SendJobFinished")
    enum = types.SimpleNamespace(**{n: _Enum(n) for n in names})
    plugin.orca.LifecycleEvent = enum
    return enum


class OrcaLifecycleEventsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin = _load_plugin()
        cls.events = _fake_lifecycle(cls.plugin)

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.temp_dir.name)
        p = self.plugin
        p.PRINT_HISTORY_FILE = str(root / "print_history.json")
        p.PRINTERS_FILE = str(root / "printers.json")
        p.OUTBOX_DIR = str(root / "outbox")
        p.OUTBOX_INDEX = str(root / "outbox_index.json")
        p.PLUGIN_STORAGE_DIR = str(root)
        p._STORAGE_DENIED = False
        p._HISTORY_STORE_DAMAGED = False
        p.ORCA_HINT_DELAY_SECONDS = 0.01
        with p._ORCA_EVENT_LOCK:
            p._ORCA_STATE.update(slice_at=None, hook_at=None, no_hook=False)
            for key in ("names", "starts", "recent"):
                p._ORCA_STATE[key].clear()
        (root / "printers.json").write_text(json.dumps([
            {"id": 1, "name": "X1C", "url": "bambu.local", "type": "bambu",
             "serial": "01P00A123"},
            {"id": 2, "name": "Voron", "url": "http://v.local", "type": "moonraker"},
        ]), encoding="utf-8")
        self.root = root
        self.outbox = p.PrintersOutbox()

    def tearDown(self):
        self.temp_dir.cleanup()

    def fire(self, event, **fields):
        fields.setdefault("code", _Enum("Ok"))
        ctx = types.SimpleNamespace(**fields)
        self.outbox.on_lifecycle_event(getattr(self.events, event), ctx)

    def history(self):
        return self.plugin.load_print_history()

    def test_send_finished_is_recorded_as_sent_for_the_matching_printer(self):
        self.fire("SendJobStarted", name="cube", device_id="01p00a123", source="send_job")
        self.fire("SendJobFinished", name="cube", device_id="01p00a123", source="send_job")
        (record,) = self.history()
        self.assertEqual((record["state"], record["source"], record["filename"]),
                         ("sent", "orca", "cube"))
        self.assertEqual(record["printer_name"], "X1C")
        card = [c for c in self.plugin.load_manual_printers() if c["id"] == 1][0]
        self.assertEqual(record["printer_key"], self.plugin.printer_history_key(card))
        self.assertEqual(len(self.plugin.print_history_for_printer(card)), 1)
        self.assertEqual(self.plugin._public_history_record(record)["source"], "orca")

    def test_unknown_or_ambiguous_device_is_journaled_without_printer(self):
        self.fire("SendJobFinished", name="a", device_id="NOPE", source="send_job")
        self.fire("PrintJobFinished", name="b", device_id="", source="print_job")
        records = self.history()
        self.assertEqual(len(records), 2)
        for record in records:
            self.assertEqual((record["printer_name"], record["state"]), ("", "sent"))
            self.assertEqual(record["printer_key"], "printer:unknown")
        stored = json.loads((self.root / "printers.json").read_text(encoding="utf-8"))
        stored.append(dict(stored[0], id=3))
        (self.root / "printers.json").write_text(json.dumps(stored), encoding="utf-8")
        self.fire("SendJobFinished", name="c", device_id="01P00A123", source="send_job")
        self.assertEqual(self.history()[0]["printer_name"], "")

    def test_print_job_outcomes_follow_the_event_code(self):
        self.fire("PrintJobFinished", name="ok", device_id="01P00A123", source="print_job")
        self.fire("PrintJobFinished", name="bad", device_id="01P00A123",
                  source="print_job", code=_Enum("Error"))
        self.fire("PrintJobFinished", name="stop", device_id="01P00A123",
                  source="print_job", code=_Enum("Warn"), msg="cancelled")
        by_name = {r["filename"]: r for r in self.history()}
        self.assertEqual({k: v["state"] for k, v in by_name.items()},
                         {"ok": "printing", "bad": "failed", "stop": "cancelled"})
        self.assertIsNone(by_name["ok"]["finished_at"])
        self.assertIsNotNone(by_name["bad"]["finished_at"])

    def test_started_plus_finished_and_repeated_finished_make_one_record(self):
        for _ in range(2):
            self.fire("SendJobStarted", name="cube", device_id="01P00A123", source="send_job")
            self.fire("SendJobFinished", name="cube", device_id="01P00A123", source="send_job")
        self.assertEqual(len(self.history()), 1)
        self.fire("SendJobFinished", name="cube", device_id="01P00A123",
                  source="send_job", code=_Enum("Error"))
        self.assertEqual(len(self.history()), 2)

    def test_old_host_without_lifecycle_enum_does_nothing(self):
        plugin = _load_plugin()
        self.assertFalse(hasattr(plugin.orca, "LifecycleEvent"))
        plugin.PRINT_HISTORY_FILE = str(self.root / "old_history.json")
        plugin.PrintersOutbox().on_lifecycle_event(
            _Enum("SendJobFinished"), types.SimpleNamespace(
                name="x", source="send_job", device_id="", code=_Enum("Ok")))
        self.assertFalse((self.root / "old_history.json").exists())
        # a member the host does not know is ignored as well
        self.outbox.on_lifecycle_event(_Enum("Brand New"), types.SimpleNamespace())
        self.assertEqual(self.history(), [])

    def test_callback_swallows_every_exception(self):
        with mock.patch.object(self.plugin, "save_print_history", side_effect=OSError("disk")):
            self.fire("SendJobFinished", name="x", device_id="", source="send_job")
        self.outbox.on_lifecycle_event(self.events.SendJobFinished, None)
        self.outbox.on_lifecycle_event(object(), object())
        self.assertFalse(hasattr(self.plugin.PrintersDashboard, "on_lifecycle_event"))

    def _slice(self, hook, name=".1234.0.gcode"):
        gcode = self.root / name
        gcode.write_text("G28\n", encoding="utf-8")
        self.fire("GCodeExportStarted", name="Benchy", msg=str(gcode))
        self.fire("GCodeExportFinished", name="Benchy", msg=str(gcode))
        if hook:
            result = self.outbox.execute(types.SimpleNamespace(
                step="post-process", gcode_path=str(gcode), output_name=gcode.name, host=""))
            self.assertEqual(result[0], "success")
        self.fire("SlicingJobComplete", name="Benchy")
        time.sleep(0.15)
        return gcode

    def test_hint_flag_set_when_slice_has_no_hook_and_cleared_when_hook_runs(self):
        self._slice(hook=False)
        self.assertTrue(self.plugin.outbox_hook_missing())
        self._slice(hook=True)
        self.assertFalse(self.plugin.outbox_hook_missing())

    def test_no_hint_when_hook_ran_for_the_slice_or_slice_failed(self):
        self._slice(hook=True)
        self.assertFalse(self.plugin.outbox_hook_missing())
        self.fire("GCodeExportStarted", name="Other", msg=str(self.root / "z.gcode"))
        self.fire("SlicingJobComplete", name="x", code=_Enum("Error"))
        time.sleep(0.1)
        self.assertFalse(self.plugin.outbox_hook_missing())

    def test_hook_firing_late_clears_the_flag(self):
        gcode = self._slice(hook=False)
        self.assertTrue(self.plugin.outbox_hook_missing())
        self.outbox.execute(types.SimpleNamespace(
            step="post-process", gcode_path=str(gcode), output_name="", host=""))
        self.assertFalse(self.plugin.outbox_hook_missing())

    def test_storage_name_uses_model_name_only_for_the_matching_path(self):
        self._slice(hook=True)
        (record,) = self.plugin.load_outbox_index()
        self.assertEqual(record["name"], "Benchy.gcode")
        other = self.root / ".9999.0.gcode"
        other.write_text("G28\nM1\n", encoding="utf-8")
        self.outbox.execute(types.SimpleNamespace(
            step="post-process", gcode_path=str(other), output_name="", host=""))
        names = [r["name"] for r in self.plugin.load_outbox_index()]
        self.assertEqual(names.count("Benchy.gcode"), 1)
        self.assertEqual(len(names), 2)
        self.assertEqual(
            self.plugin._orca_output_name_hint(str(self.root / "real.gcode"), ""), "")

    def test_old_history_source_and_state_normalize(self):
        record = self.plugin._normalized_history_record(
            {"id": "1", "source": "bogus", "state": "weird"})
        self.assertEqual((record["source"], record["state"]), ("observed", "ended"))
        record = self.plugin._normalized_history_record(
            {"id": "1", "source": "orca", "state": "sent"})
        self.assertEqual((record["source"], record["state"]), ("orca", "sent"))

    def test_locale_keys_exist_in_every_catalog(self):
        keys = ("historyStateSent", "historyFromOrca", "outboxHintText", "outboxHintWhere")
        for path in (PLUGIN_PATH.parent / "printers_locales").glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            for key in keys:
                self.assertTrue(data.get(key), (path.name, key))


@unittest.skipUnless(__import__("shutil").which("node"), "node is not available")
class OutboxHintUiTest(unittest.TestCase):
    def test_hint_line_shows_only_while_flag_is_true(self):
        page = _load_plugin().PAGE
        self.assertIn('id="outbox-hint"', page)
        block = _page_slice("if (typeof data.outboxHint === 'boolean') {",
                            "  if (data.cameraInfo)")
        script = (_FAKE_DOM_JS + "function t(k){ return 'T:' + k; }\n"
                  "function apply(data){\n" + block + "\n}\n"
                  "var box = document.getElementById('outbox-hint');\n"
                  "var text = document.getElementById('outbox-hint-text');\n"
                  "var where = document.getElementById('outbox-hint-where');\n"
                  "var out = [];\n"
                  "function snap(){ out.push([box.style.display, text.textContent, where.textContent]); }\n"
                  "apply({outboxHint:true}); snap(); apply({printers:[]}); snap();\n"
                  "apply({outboxHint:false}); snap(); console.log(JSON.stringify(out));\n")
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "case.js"
            path.write_text(script, encoding="utf-8")
            done = subprocess.run(["node", str(path)], capture_output=True, text=True,
                                  encoding="utf-8", timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        shown, kept, hidden = json.loads(done.stdout)
        self.assertEqual(shown, ["", "T:outboxHintText", "T:outboxHintWhere"])
        self.assertEqual(kept, shown)
        self.assertEqual(hidden, ["none", "", ""])


class ThreadPermissionSafetyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin = _load_plugin()

    def test_denied_worker_start_never_queues_and_retry_runs_once(self):
        pool = self.plugin.ReusableDaemonPool("denial-test", max_workers=1)
        ran = []
        with mock.patch.object(threading.Thread, "start", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                pool.submit(lambda: ran.append("denied"))
        self.assertTrue(pool._jobs.empty())
        self.assertFalse(pool._workers)
        pool.submit(lambda: ran.append("retry")).wait(2)
        self.assertEqual(ran, ["retry"])
        pool.shutdown()

    def test_growth_denial_preserves_previously_accepted_job(self):
        pool = self.plugin.ReusableDaemonPool("growth-test", max_workers=2)
        entered, release = threading.Event(), threading.Event()
        ran = []

        def first():
            entered.set()
            release.wait(2)
            ran.append("first")

        accepted = pool.submit(first)
        self.assertTrue(entered.wait(2))
        with mock.patch.object(threading.Thread, "start", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                pool.submit(lambda: ran.append("denied"))
        release.set()
        accepted.wait(2)
        pool.submit(lambda: ran.append("retry")).wait(2)
        self.assertEqual(ran, ["first", "retry"])
        pool.shutdown()

    def test_cleanup_timer_denial_keeps_clean_handle_and_retry(self):
        plugin = self.plugin
        plugin.stop_outbox_pruning()
        with mock.patch.object(threading.Timer, "start", side_effect=PermissionError("denied")):
            plugin.start_outbox_pruning()
        self.assertIsNone(plugin._PRUNE_STOP["timer"])
        with mock.patch.object(plugin, "_prune_tick"):
            self.assertTrue(plugin._schedule_prune(plugin._PRUNE_STOP, 60))
        plugin.stop_outbox_pruning()

    def test_loader_thread_never_starts_a_thread_before_a_grant(self):
        plugin = self.plugin
        plugin.stop_outbox_pruning()
        plugin._THREAD_GRANT_SEEN = False
        started = []
        real_start = threading.Timer.start

        def record(timer):
            started.append(timer)
            return real_start(timer)

        loader = threading.Thread(target=lambda: (
            plugin.start_outbox_pruning(), plugin.start_outbox_pruning(),
        ))
        with mock.patch.object(threading.Timer, "start", record):
            loader.start()
            loader.join(2)
            self.assertEqual(started, [])
            self.assertIsNone(plugin._PRUNE_STOP["timer"])
            with mock.patch.object(plugin, "_PLUGIN_RUNTIME_ACTIVE", True),                  mock.patch.object(plugin, "_prune_tick"):
                plugin.resume_deferred_pruning()
            self.assertEqual(len(started), 1)
        self.assertTrue(plugin._THREAD_GRANT_SEEN)
        plugin.stop_outbox_pruning()

    def test_ui_poll_denial_resets_busy_state_and_retry_runs(self):
        plugin = _load_plugin()
        dashboard = plugin.PrintersDashboard()
        plugin.ACTION_WORKER.activate()
        ran = threading.Event()
        with mock.patch.object(dashboard, "_post_status") as status, \
             mock.patch.object(threading.Thread, "start", side_effect=PermissionError("denied")):
            dashboard.on_message({"type": "poll"})
        self.assertFalse(dashboard._polling)
        status.assert_called_once()
        with mock.patch.object(dashboard, "_poll_dashboard",
                               side_effect=lambda token: (ran.set(), dashboard._finish_dashboard_poll(token))):
            dashboard.on_message({"type": "poll"})
            self.assertTrue(ran.wait(2))
        self.assertFalse(dashboard._polling)
        plugin.ACTION_WORKER.shutdown()

    def test_stop_ready_releases_discarded_batch_and_old_completion_cannot_release_new_poll(self):
        plugin = _load_plugin()
        dashboard = plugin.PrintersDashboard()
        plugin.POLL_WORKERS = plugin.ReusableDaemonPool("poll-restart-test", max_workers=1)
        plugin.ACTION_WORKER.activate()
        plugin.POLL_WORKERS.activate()
        plugin._PLUGIN_RUNTIME_ACTIVE = True
        old_entered, new_entered = threading.Event(), threading.Event()
        old_release, new_release, completed = threading.Event(), threading.Event(), threading.Event()
        calls = []
        batches = []

        def poll(printer, batch, generation):
            calls.append(printer["name"])
            batches.append(batch)
            try:
                if len(calls) == 1:
                    old_entered.set()
                    old_release.wait(5)
                elif len(calls) == 2:
                    new_entered.set()
                    new_release.wait(5)
            finally:
                batch.finish_one()

        def finish(printers, on_complete, generation):
            on_complete()
            completed.set()

        def restart(**kwargs):
            plugin.ACTION_WORKER.activate()
            plugin.POLL_WORKERS.activate()

        self.addCleanup(plugin.ACTION_WORKER.shutdown)
        self.addCleanup(plugin.POLL_WORKERS.shutdown)
        self.addCleanup(old_release.set)
        self.addCleanup(new_release.set)
        with mock.patch.object(dashboard, "_post"), \
             mock.patch.object(dashboard, "_page_settings", return_value={}), \
             mock.patch.object(dashboard, "_refresh_async", side_effect=restart), \
             mock.patch.object(dashboard, "_poll_into", side_effect=poll), \
             mock.patch.object(dashboard, "_finish_poll_batch", side_effect=finish), \
             mock.patch.object(plugin, "outbox_hint_visible", return_value=False), \
             mock.patch.object(plugin, "dashboard_printers", return_value=[
                 {"name": "first"}, {"name": "queued"}]):
            dashboard.on_message({"type": "poll"})
            self.assertTrue(old_entered.wait(2))
            old_token = dashboard._poll_token
            plugin.stop_plugin_runtime()
            dashboard.on_message({"type": "ready"})
            self.assertFalse(dashboard._polling)
            dashboard.on_message({"type": "poll"})
            old_release.set()
            self.assertTrue(new_entered.wait(2))
            self.assertNotEqual(old_token, dashboard._poll_token)
            batches[0].finish_one()  # a late completion from the old batch
            self.assertTrue(dashboard._polling)
            completed.clear()
            new_release.set()
            self.assertTrue(completed.wait(2))
            self.assertFalse(dashboard._polling)
            self.assertEqual(calls, ["first", "first", "queued"])

    def test_active_poll_denial_keeps_exact_action_status_and_finishes_batch(self):
        plugin = _load_plugin()
        dashboard = plugin.PrintersDashboard()
        plugin.ACTION_WORKER.activate()
        plugin.POLL_WORKERS.activate()
        printer = {"name": "Duet", "url": "http://192.0.2.10", "type": "duet"}
        posted = []
        completed = []
        dashboard._post = posted.append
        dashboard._post_status("upload failed: authentication rejected")
        with mock.patch.object(threading.Thread, "start", side_effect=PermissionError("denied")), \
             mock.patch.object(plugin, "attach_outbox"), \
             mock.patch.object(plugin, "autoname_printers"):
            dashboard._poll_and_push([printer], discover=True,
                                     on_complete=lambda: completed.append(True))
        self.assertEqual(completed, [True])
        self.assertEqual([item["status"] for item in posted if "status" in item],
                         ["upload failed: authentication rejected"])
        self.assertEqual(printer["status"]["state"], "offline")
        self.assertTrue(any("printers" in item for item in posted))
        plugin.ACTION_WORKER.shutdown()
        plugin.POLL_WORKERS.shutdown()

    @unittest.skipUnless(__import__("shutil").which("node"), "node is not available")
    def test_rejected_add_and_adopt_unlock_actual_page_controls(self):
        plugin = _load_plugin()
        dashboard = plugin.PrintersDashboard()
        plugin.ACTION_WORKER.activate()
        posted = []
        discovered = {"name": "Preset", "url": "http://192.0.2.10",
                      "type": "bambu", "discovery_ref": "orca:fixture"}
        dashboard._discovered_printers = {"orca:fixture": discovered}
        with mock.patch.object(dashboard, "_post", side_effect=posted.append), \
             mock.patch.object(dashboard, "_post_status"), \
             mock.patch.object(dashboard, "_capture_discovered_printers", return_value=[]), \
             mock.patch.object(threading.Thread, "start", side_effect=PermissionError("denied")):
            dashboard.on_message({"type": "add", "name": "Manual", "url": "192.0.2.11",
                                  "kind": "bambu", "access_code": "code"})
            dashboard.on_message({"type": "adopt", "discoveryRef": "orca:fixture"})
        failures = [item["adoptResult"] for item in posted if "adoptResult" in item]
        self.assertEqual(len(failures), 2)
        self.assertTrue(all(item["ok"] is False for item in failures))
        handler = _page_slice("  if (data.adoptResult) {", "  if (data.settings)")
        script = (_FAKE_DOM_JS + "\nvar pendingFormAdd=true, pendingAdoptUrl='pending', pendingAdoptRef='ref';\n"
                  "function setManualFormOpen(){} function renderAvailable(){}\n"
                  "function receive(data){\n" + handler + "\n}\n"
                  "document.getElementById('add').disabled=true;"
                  "document.getElementById('f-adopt').disabled=true;"
                  "receive(" + json.dumps({"adoptResult": failures[0]}) + ");\n"
                  "console.log(JSON.stringify([pendingAdoptUrl,pendingAdoptRef,pendingFormAdd,"
                  "document.getElementById('add').disabled,"
                  "document.getElementById('f-adopt').disabled]));")
        done = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                              encoding="utf-8", timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), ["", "", False, False, False])
        plugin.ACTION_WORKER.shutdown()

    def test_rejected_history_camera_and_artifact_answer_page_protocol(self):
        plugin = _load_plugin()
        dashboard = plugin.PrintersDashboard()
        plugin.ACTION_WORKER.activate()
        posted = []
        with mock.patch.object(dashboard, "_post", side_effect=posted.append), \
             mock.patch.object(dashboard, "_post_status"), \
             mock.patch.object(threading.Thread, "start", side_effect=PermissionError("denied")):
            dashboard.on_message({"type": "print-history-all", "requestId": 7})
            dashboard.on_message({"type": "camera-info", "requestId": 8,
                                  "targetUuid": "target"})
            dashboard.on_message({"type": "artifact-begin", "filename": "a.gcode", "size": 1})
        self.assertEqual(posted[0]["printHistory"]["requestId"], 7)
        self.assertFalse(posted[0]["printHistory"]["available"])
        self.assertEqual(posted[1]["cameraInfo"]["targetUuid"], "target")
        self.assertFalse(posted[1]["cameraInfo"]["ok"])
        self.assertTrue(posted[2]["artifactError"])
        plugin.ACTION_WORKER.shutdown()

    @unittest.skipUnless(__import__("shutil").which("node"), "node is not available")
    def test_rejected_command_unlocks_only_its_button_for_retry(self):
        plugin = _load_plugin()
        dashboard = plugin.PrintersDashboard()
        posted = []
        printer = plugin._ensure_printer_identity({"name": "Printer", "type": "moonraker",
                                                   "url": "http://192.0.2.10"})
        with mock.patch.object(plugin, "resolve_printer_target", return_value=printer), \
             mock.patch.object(plugin, "dashboard_printers", return_value=[printer]), \
             mock.patch.object(dashboard, "_ensure_action_scheduler", return_value=(None, 0)), \
             mock.patch.object(dashboard, "_post", side_effect=posted.append), \
             mock.patch.object(dashboard, "_post_status"):
            dashboard._submit_target_action(mock.Mock(), {"requestId": 1}, "cancel")
        rejection = posted[0]
        self.assertEqual(rejection, {"actionRejected": {"requestId": 1}})
        helper = _page_slice("var actionRejectSequence = 0;", "function statusFresh(")
        handler = _page_slice("  if (data.actionRejected", "  if (data.artifactBegin")
        script = (_FAKE_DOM_JS + helper + "\nfunction receive(data){\n" + handler + "\n}\n"
                  "var first=new El('button'), second=new El('button');\n"
                  "sendRejectableAction(first,{type:'printer-cmd',action:'cancel'});\n"
                  "sendRejectableAction(second,{type:'printer-cmd',action:'pause'});\n"
                  "receive(" + json.dumps(rejection) + ");\n"
                  "var afterReject=[first.disabled,second.disabled,posted.length];\n"
                  "sendRejectableAction(first,{type:'printer-cmd',action:'cancel'});\n"
                  "console.log(JSON.stringify([afterReject,first.disabled,second.disabled,"
                  "posted.length,posted.map(function(x){return x.requestId;})]));")
        done = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                              encoding="utf-8", timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), [[False, True, 2], True, True, 3,
                                                   [1, 2, 3]])


class BambuModelCodeMatchTest(unittest.TestCase):
    SERIAL = "01P00A000000001"

    @classmethod
    def setUpClass(cls):
        cls.plugin = _load_plugin()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.plugin.PRINTERS_FILE = str(pathlib.Path(self.temp.name, "printers.json"))
        self.plugin._PRINTERS_STORE_DAMAGED = False

    def tearDown(self):
        self.temp.cleanup()

    def card(self, serial=None, code="N7", name="Card", kind="bambu", bound_serial=True, **extra):
        serial = self.SERIAL if serial is None else serial
        card = {"id": 1, "name": name, "url": "http://192.0.2.10", "type": kind,
                "serial": serial, "binding_revision": 1}
        if code:
            card["model_code"] = code
            card["model_code_serial"] = serial if bound_serial else "OTHER"
            card["model_code_source"] = "mqtt"
            card["model_code_endpoint"] = self.plugin._endpoint_key(card["url"])
            card["model_code_binding_revision"] = 1
        card.update(extra)
        return card

    def match(self, record, cards):
        return self.plugin.outbox_target_match(record, cards)

    def save(self, *cards):
        self.plugin.save_manual_printers([
            self.plugin._ensure_printer_identity(dict(c, id=i + 1)) for i, c in enumerate(cards)])

    def stored(self):
        return self.plugin.load_manual_printers()

    # -- matching ---------------------------------------------------------
    def test_single_p2s_card_matches_any_nozzle(self):
        card = self.card()
        for sid in ("Bambu Lab P2S 0.4 nozzle", "Bambu Lab P2S 0.2 nozzle", ""):
            record = {"printer_model": "Bambu Lab P2S", "printer_settings_id": sid}
            self.assertIs(self.match(record, [card, self.card(serial="X", code="N2S", name="A1")]), card)

    def test_two_cards_with_same_code_are_ambiguous(self):
        a = self.card()
        b = self.card(serial="01P00A000000002", name="Other")
        self.assertIsNone(self.match({"printer_model": "Bambu Lab P2S"}, [a, b]))

    def test_code_bound_to_another_serial_is_ignored(self):
        self.assertIsNone(self.match({"printer_model": "Bambu Lab P2S"},
                                     [self.card(bound_serial=False)]))
        self.assertIsNone(self.match({"printer_model": "Bambu Lab P2S"},
                                     [self.card(model_code_serial="")]))

    def test_unknown_model_o1c_and_non_bambu_do_not_match(self):
        self.assertIsNone(self.match({"printer_model": "Bambu Lab Z9"}, [self.card()]))
        self.assertIsNone(self.match({"printer_model": "Bambu Lab O1C"}, [self.card(code="O1C")]))
        self.assertIsNone(self.match({"printer_model": "Voron 2.4"}, [self.card(code="N7")]))
        self.assertIsNone(self.match({"printer_model": "Bambu Lab P2S"},
                                     [self.card(kind="moonraker")]))
        self.assertIsNone(self.match({"printer_model": ""}, [self.card()]))

    def test_endpoint_or_serial_without_hit_skips_model_step(self):
        card = self.card()
        record = {"printer_model": "Bambu Lab P2S",
                  "match_endpoint": "endpoint:http://203.0.113.9"}
        self.assertIsNone(self.match(record, [card]))
        self.assertIsNone(self.match({"printer_model": "Bambu Lab P2S",
                                      "match_serial": "NOPE"}, [card]))

    def test_exact_name_and_endpoint_beat_model(self):
        by_model = self.card()
        named = self.card(serial="S2", code="", name="Bambu Lab P2S",
                          name_provenance="user", name_match_value="Bambu Lab P2S",
                          name_match_binding_revision=1)
        self.assertIs(self.match({"printer_model": "Bambu Lab P2S"}, [by_model, named]), named)
        other = dict(by_model, serial="S3", model_code_serial="S3", url="http://192.0.2.77")
        record = {"printer_model": "Bambu Lab P2S",
                  "match_endpoint": self.plugin._slice_host_endpoint("192.0.2.77")}
        self.assertIs(self.match(record, [by_model, other]), other)

    def test_model_match_keeps_target_uuid_and_send_rules(self):
        card = self.plugin._ensure_printer_identity(self.card())
        record = {"printer_model": "Bambu Lab P2S", "file": "unassigned/x.gcode"}
        self.assertTrue(self.plugin.outbox_target_allowed(record, card, [card]))
        bound = dict(record, target_uuid="00000000-0000-4000-8000-000000000000",
                     owner_scope_uuid=card["owner_scope_uuid"],
                     target_selector=card["target_selector"], binding_revision=1)
        self.assertIsNone(self.match(bound, [card]))
        self.assertFalse(self.plugin.outbox_target_allowed(bound, card, [card]))

    def test_outbox_add_binds_by_model_and_start_stays_unknown(self):
        card = self.plugin._ensure_printer_identity(self.card())
        root = pathlib.Path(self.temp.name)
        self.plugin.OUTBOX_DIR = str(root / "outbox")
        self.plugin.OUTBOX_INDEX = str(root / "outbox_index.json")
        gcode = root / "a.gcode"
        gcode.write_text("G28\n", encoding="utf-8")
        with mock.patch.object(self.plugin, "dashboard_printers", return_value=[card]):
            record = self.plugin.outbox_add(str(gcode), identity={
                "printer_model": "Bambu Lab P2S",
                "printer_settings_id": "Bambu Lab P2S 0.6 nozzle"})
        self.assertEqual(record["target_uuid"], card["target_uuid"])

    def test_outbox_copy_preserves_source_gcode_bytes(self):
        card = self.plugin._ensure_printer_identity(self.card())
        root = pathlib.Path(self.temp.name)
        self.plugin.OUTBOX_DIR = str(root / "outbox")
        self.plugin.OUTBOX_INDEX = str(root / "outbox_index.json")
        gcode = root / "binary.gcode"
        payload = b"; printer_model = Bambu Lab P2S\r\nG28\n\xff\x00\r\n"
        gcode.write_bytes(payload)
        with mock.patch.object(self.plugin, "dashboard_printers", return_value=[card]):
            record = self.plugin.outbox_add(str(gcode), identity={
                "printer_model": "Bambu Lab P2S"})
        self.assertEqual(gcode.read_bytes(), payload)
        self.assertEqual((root / "outbox" / record["file"]).read_bytes(), payload)

    # -- normalization ----------------------------------------------------
    def test_code_normalization(self):
        norm = self.plugin._normalize_model_code
        self.assertEqual(norm("3DPrinter-X1-Carbon"), "BL-P001")
        self.assertEqual(norm("3DPrinter-X1"), "BL-P002")
        self.assertEqual(norm(" n7 "), "N7")
        self.assertEqual(norm("O1C2"), "O1C2")
        for bad in (None, 5, "", "  ", "N 7", "N7;rm", "x" * 33, "<b>", "N\n7"):
            self.assertEqual(norm(bad), "", repr(bad))

    def test_ssdp_returns_normalized_model_code(self):
        packet = (b"NOTIFY * HTTP/1.1\r\nDevName.bambu.com: Shop\r\nUSN: " + self.SERIAL.encode()
                  + b"\r\nLocation: 192.0.2.10\r\nDevModel.bambu.com: 3DPrinter-X1-Carbon\r\n")
        sock = mock.MagicMock()
        sock.recvfrom.side_effect = [(packet, ("192.0.2.10", 2021)), OSError()]
        with mock.patch.object(self.plugin.socket, "socket", return_value=sock):
            names = self.plugin.ssdp_bambu_names(1.0)
        self.assertEqual(names["192.0.2.10"], {"name": "Shop", "serial": self.SERIAL,
                                               "model_code": "BL-P001"})

    def test_ssdp_rejects_location_redirect_and_oversized_payload(self):
        forged = (b"DevName.bambu.com: Forged\r\nUSN: SERIAL\r\n"
                  b"Location: http://192.0.2.99/device.xml\r\n")
        valid = b"DevName.bambu.com: Hint\r\nUSN: SERIAL\r\n"
        sock = mock.MagicMock()
        sock.recvfrom.side_effect = [
            (forged, ("192.0.2.10", 2021)),
            (b"X" * 2049, ("192.0.2.10", 2021)),
            (valid, ("192.0.2.10", 2021)), OSError(),
        ]
        with mock.patch.object(self.plugin.socket, "socket", return_value=sock):
            found = self.plugin.ssdp_bambu_names(1.0)
        self.assertEqual(list(found), ["192.0.2.10"])
        self.assertEqual(found["192.0.2.10"]["name"], "Hint")

    def test_ssdp_malformed_location_skips_only_that_packet(self):
        broken = b"DevName.bambu.com: Bad\r\nLocation: http://[broken\r\n"
        valid = b"DevName.bambu.com: Good\r\nUSN: SERIAL\r\n"
        sock = mock.MagicMock()
        sock.recvfrom.side_effect = [
            (broken, ("192.0.2.10", 2021)),
            (valid, ("192.0.2.11", 2021)), OSError(),
        ]
        with mock.patch.object(self.plugin.socket, "socket", return_value=sock):
            found = self.plugin.ssdp_bambu_names(1.0)
        self.assertEqual(list(found), ["192.0.2.11"])

    def test_legacy_serial_needs_exact_provenance_before_matching(self):
        card = self.card(code="")
        record = {"match_serial": self.SERIAL}
        self.assertIsNone(self.match(record, [card]))
        card.update(serial_source="mqtt", serial_value=self.SERIAL,
                    serial_endpoint=self.plugin._endpoint_key(card["url"]),
                    serial_binding_revision=1)
        self.assertIs(self.match(record, [card]), card)
        self.assertIsNone(self.match(record, [dict(card, serial_value="OTHER")]))
        self.assertIsNone(self.match(record, [dict(card, serial_binding_revision=2)]))
        self.assertIsNone(self.match(record, [dict(card, url="http://192.0.2.99")]))
        user_card = dict(card, serial_source="user")
        self.assertIs(self.match(record, [user_card]), user_card)

    def test_legacy_model_and_name_are_not_matchable(self):
        card = self.card(name="Bambu Lab P2S")
        self.assertIsNone(self.match({"printer_model": "Bambu Lab P2S"}, [
            {key: value for key, value in card.items()
             if not key.startswith("model_code_") and key != "model_code"}]))
        self.assertIsNone(self.match({"printer_model": "Bambu Lab P2S"}, [
            dict(card, model_code_source=None)]))

    def test_poll_bambu_remembers_printer_type_from_any_message(self):
        topic = b"device/%s/report" % self.SERIAL.encode()

        def frame(body):
            payload = json.dumps(body).encode()
            return (0x30, struct.pack("!H", len(topic)) + topic + payload)

        packets = iter([(0x20, b"\x00\x00"), (0x90, b"\x00\x01\x00"),
                        frame({"print": {"printer_type": "N7"}}),
                        frame({"info": {"x": 1}}),
                        frame({"print": {"gcode_state": "IDLE", "nozzle_temper": 20}})])
        fake = mock.MagicMock()
        fake.getpeercert.return_value = b"synthetic Bambu TLS certificate"
        context = mock.MagicMock()
        context.wrap_socket.return_value = fake
        printer = _trusted_bambu_card(self, self.plugin, {
            "url": "http://192.0.2.10", "access_code": "x", "serial": self.SERIAL})
        with mock.patch.object(self.plugin.socket, "create_connection", return_value=fake), \
             mock.patch.object(self.plugin, "ssl_ctx_for", return_value=context), \
             mock.patch.object(self.plugin, "_mqtt_read_packet",
                               side_effect=lambda *a: next(packets)):
            status = self.plugin.poll_bambu(printer)
        self.assertIs(status["online"], True)
        self.assertEqual(printer["_seen_model_code"], "N7")

    def test_wildcard_mqtt_ignores_other_serial_after_first_report(self):
        def frame(serial, body):
            topic = ("device/%s/report" % serial).encode()
            return (0x30, struct.pack("!H", len(topic)) + topic + json.dumps(body).encode())

        packets = iter([(0x20, b"\x00\x00"), (0x90, b"\x00\x01\x00"),
                        frame(self.SERIAL, {"print": {"printer_type": "N7"}}),
                        frame("OTHER", {"print": {"printer_type": "N2S"}}),
                        frame(self.SERIAL, {"print": {
                            "gcode_state": "IDLE", "nozzle_temper": 20}})])
        fake, context = mock.MagicMock(), mock.MagicMock()
        fake.getpeercert.return_value = b"synthetic Bambu TLS certificate"
        context.wrap_socket.return_value = fake
        printer = _trusted_bambu_card(self, self.plugin, {
            "url": "http://192.0.2.10", "access_code": "x", "serial": ""})
        with mock.patch.object(self.plugin.socket, "create_connection", return_value=fake), \
             mock.patch.object(self.plugin, "ssl_ctx_for", return_value=context), \
             mock.patch.object(self.plugin, "_mqtt_read_packet",
                               side_effect=lambda *a: next(packets)):
            status = self.plugin.poll_bambu(printer)
        self.assertTrue(status["online"])
        self.assertEqual(printer["serial"], self.SERIAL)
        self.assertEqual(printer["_seen_model_code"], "N7")

    def test_fixed_serial_mqtt_ignores_wrong_topic(self):
        def frame(serial, body):
            topic = ("device/%s/report" % serial).encode()
            return (0x30, struct.pack("!H", len(topic)) + topic + json.dumps(body).encode())

        packets = iter([(0x20, b"\x00\x00"), (0x90, b"\x00\x01\x00"),
                        frame("OTHER", {"print": {"printer_type": "N2S"}}),
                        frame(self.SERIAL, {"print": {"printer_type": "N7"}}),
                        frame(self.SERIAL, {"print": {
                            "gcode_state": "IDLE", "nozzle_temper": 20}})])
        fake, context = mock.MagicMock(), mock.MagicMock()
        fake.getpeercert.return_value = b"synthetic Bambu TLS certificate"
        context.wrap_socket.return_value = fake
        printer = _trusted_bambu_card(self, self.plugin, {
            "url": "http://192.0.2.10", "access_code": "x", "serial": self.SERIAL})
        with mock.patch.object(self.plugin.socket, "create_connection", return_value=fake), \
             mock.patch.object(self.plugin, "ssl_ctx_for", return_value=context), \
             mock.patch.object(self.plugin, "_mqtt_read_packet",
                               side_effect=lambda *a: next(packets)):
            status = self.plugin.poll_bambu(printer)
        self.assertTrue(status["online"])
        self.assertEqual(printer["_seen_model_code"], "N7")

    def test_poll_status_does_not_depend_on_printer_type(self):
        printer = {"url": "http://192.0.2.10", "type": "bambu", "serial": self.SERIAL}
        with mock.patch.object(self.plugin, "poll_bambu",
                               return_value={"online": True, "state": "idle"}), \
             mock.patch.object(self.plugin, "persist_model_code") as persist:
            self.plugin.poll_printer(printer)
        persist.assert_not_called()
        printer["_seen_model_code"] = "N7"
        with mock.patch.object(self.plugin, "poll_bambu",
                               return_value={"online": True, "state": "idle"}), \
             mock.patch.object(self.plugin, "persist_model_code") as persist:
            self.plugin.poll_printer(printer)
        persist.assert_called_once_with(printer, "N7")
        self.assertNotIn("_seen_model_code", printer)

    # -- persistence ------------------------------------------------------
    def test_persist_sets_code_for_exact_card_only(self):
        self.save(self.card(code=""), self.card(serial="OTHER", code="", name="B"),
                  self.card(serial=self.SERIAL.lower(), code="", name="M", kind="moonraker"))
        first = self.stored()[0]
        self.assertTrue(self.plugin.persist_model_code(first, "n7"))
        first, second, third = self.stored()
        self.assertEqual((first["model_code"], first["model_code_serial"]), ("N7", self.SERIAL))
        self.assertNotIn("model_code", second)
        self.assertNotIn("model_code", third)
        self.assertEqual(first["model_code_source"], "mqtt")
        self.assertFalse(self.plugin.persist_model_code(first, "N7"))
        self.assertFalse(self.plugin.persist_model_code(first, "bad code"))

    def test_disagreeing_code_clears_instead_of_overwriting(self):
        self.save(self.card(code=""))
        self.plugin.persist_model_code(self.stored()[0], "N7")
        self.plugin.persist_model_code(self.stored()[0], "N2S")
        card = self.stored()[0]
        self.assertNotIn("model_code", card)
        self.assertNotIn("model_code_serial", card)
        self.assertIsNone(self.match({"printer_model": "Bambu Lab P2S"}, [card]))

    def test_ssdp_code_never_enters_stored_matching_identity(self):
        self.save(self.card(code=""))
        with mock.patch.object(self.plugin, "ssdp_bambu_names", return_value={
                "192.0.2.10": {"name": "Forged", "serial": self.SERIAL,
                                 "model_code": "N7"}}):
            self.plugin.autoname_printers(self.stored())
        self.assertNotIn("model_code", self.stored()[0])
        self.assertNotEqual(self.stored()[0]["name"], "Forged")

    def test_changed_serial_invalidates_stored_code_and_new_code_replaces_it(self):
        self.save(self.card(code="N7"))
        card = self.stored()[0]
        card["serial"] = "NEWSERIAL"
        self.assertIsNone(self.match({"printer_model": "Bambu Lab P2S"}, [card]))
        self.plugin.save_manual_printers([card])
        self.plugin.persist_model_code(card, "N2S")
        self.assertEqual(self.stored()[0]["model_code_serial"], "NEWSERIAL")

    # -- dictionary -------------------------------------------------------
    def test_dictionary_matches_orca_bbl_profiles_when_available(self):
        root = pathlib.Path(r"F:\OrcaSlicer-upstream\resources\profiles\BBL\machine")
        if not root.is_dir():
            self.skipTest("OrcaSlicer BBL profiles not available")
        profiles = {}
        for path in root.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if data.get("type") == "machine_model" and data.get("model_id"):
                profiles[data["name"]] = data["model_id"]
        for name, code in self.plugin._BAMBU_MODEL_IDS.items():
            self.assertEqual(profiles.get(name), code, name)
        self.assertEqual(len(self.plugin._BAMBU_MODEL_IDS), 14)


class BambuTlsTrustTest(unittest.TestCase):
    def setUp(self):
        self.plugin = _load_plugin()
        self.card = _trusted_bambu_card(self, self.plugin, {
            "url": "http://192.0.2.77", "serial": "SERIAL", "access_code": "secret"})

    def identity(self):
        card = self.card
        return {"targetUuid": card["target_uuid"],
                "ownerScope": card["owner_scope_uuid"],
                "targetSelector": card["target_selector"],
                "bindingRevision": card["binding_revision"],
                "url": card["url"], "connectionType": "bambu"}

    def probe(self, dashboard, posted, protocol="mqtt"):
        dashboard._post = posted.append
        dashboard._probe_tls_trust(dict(self.identity(), protocol=protocol))
        return posted[-1]["tlsTrustResult"]["token"]

    def test_dashboard_denies_embedded_frames_objects_and_base_replacement(self):
        page = self.plugin.render_page()
        self.assertIn("frame-src 'none'; object-src 'none'; base-uri 'none'", page)
        self.assertNotIn("<iframe", page.casefold())
        self.assertIn("<script>", page)  # existing inline dashboard remains allowed

    def test_work_started_with_previous_page_epoch_is_silent(self):
        dashboard, posted = self.plugin.PrintersDashboard(), []
        dashboard._post = posted.append
        epoch = dashboard._tls_epoch
        dashboard._invalidate_tls_challenges()
        with mock.patch.object(self.plugin, "_probe_bambu_certificate") as probe:
            dashboard._probe_tls_trust(dict(self.identity(), protocol="mqtt"), page_epoch=epoch)
            dashboard._confirm_tls_trust({"token": "old", "consent": True}, page_epoch=epoch)
        self.assertEqual(posted, [])
        probe.assert_not_called()

    def test_tls_confirmation_preserves_only_valid_name_provenance(self):
        for valid in (True, False):
            with self.subTest(valid=valid):
                self.card.update(name="Printer", name_provenance="user",
                                 name_match_value="Printer",
                                 name_match_binding_revision=(
                                     self.card["binding_revision"] if valid else 0))
                self.plugin.save_manual_printers([self.card])
                dashboard, posted = self.plugin.PrintersDashboard(), []
                with mock.patch.object(self.plugin, "_probe_bambu_certificate", return_value="a" * 64), \
                     mock.patch.object(dashboard, "_refresh_worker"):
                    token = self.probe(dashboard, posted)
                    dashboard._confirm_tls_trust({"token": token, "consent": True})
                saved = self.plugin.load_manual_printers()[0]
                self.assertEqual(self.plugin._name_is_matchable(saved), valid)
                self.assertIsNone(self.plugin.resolve_printer_target([saved], self.identity()))

    def test_connected_without_report_never_promotes_configured_serial(self):
        for status in ({"online": True, "state": "connected"},
                       {"online": True, "state": "idle"},
                       {"online": False, "state": "offline"}):
            with self.subTest(status=status):
                self.card["_seen_verified_serial"] = "SERIAL"  # stale previous poll
                with mock.patch.object(self.plugin, "poll_bambu", return_value=status), \
                     mock.patch.object(self.plugin, "persist_verified_bambu_serial") as persist:
                    self.plugin.poll_printer(self.card)
                persist.assert_not_called()

    def test_exact_report_evidence_promotes_serial_even_without_full_status(self):
        def reported(printer):
            printer["_seen_verified_serial"] = "SERIAL"
            return {"online": True, "state": "connected"}
        with mock.patch.object(self.plugin, "poll_bambu", side_effect=reported), \
             mock.patch.object(self.plugin, "persist_verified_bambu_serial") as persist:
            self.plugin.poll_printer(self.card)
        persist.assert_called_once_with(self.card)
        self.assertNotIn("_seen_verified_serial", self.card)

    def test_mqtt_wrong_topic_or_invalid_report_cannot_verify_configured_serial(self):
        for topic, message in (("device/OTHER/report", {"print": {"gcode_state": "IDLE"}}),
                               ("device/SERIAL/report", {"info": {"value": 1}}),
                               ("device/SERIAL/report", ["invalid", "report"])):
            with self.subTest(topic=topic, message=message):
                encoded = topic.encode()
                packets = iter([(0x20, b"\x00\x00"), (0x90, b"\x00\x01\x00"),
                                (0x30, struct.pack("!H", len(encoded)) + encoded +
                                 json.dumps(message).encode())])
                socket, context = mock.Mock(), mock.Mock()
                socket.getpeercert.return_value = b"synthetic Bambu TLS certificate"
                context.wrap_socket.return_value = socket
                with mock.patch.object(self.plugin.socket, "create_connection", return_value=socket), \
                     mock.patch.object(self.plugin, "ssl_ctx_for", return_value=context), \
                     mock.patch.object(self.plugin.time, "monotonic", side_effect=[0, 1, 9]), \
                     mock.patch.object(self.plugin, "_mqtt_read_packet", side_effect=lambda *args: next(packets)), \
                     mock.patch.object(self.plugin, "persist_verified_bambu_serial") as persist:
                    status = self.plugin.poll_printer(self.card)
                self.assertEqual(status["state"], "connected")
                persist.assert_not_called()

    def test_changed_certificate_or_deleted_target_rejects_confirmation(self):
        for changed in ("certificate", "deleted", "edited"):
            with self.subTest(changed=changed):
                self.plugin.save_manual_printers([self.card])
                dashboard, posted = self.plugin.PrintersDashboard(), []
                with mock.patch.object(self.plugin, "_probe_bambu_certificate", return_value="a" * 64):
                    token = self.probe(dashboard, posted)
                if changed == "deleted":
                    self.plugin.save_manual_printers([])
                elif changed == "edited":
                    edited = dict(self.card, binding_revision=self.card["binding_revision"] + 1)
                    self.plugin.save_manual_printers([edited])
                with mock.patch.object(self.plugin, "_probe_bambu_certificate", return_value="b" * 64):
                    dashboard._confirm_tls_trust({"token": token, "consent": True})
                self.assertEqual(posted[-1]["tlsTrustResult"]["error"], "tlsTrustStale")
                for saved in self.plugin.load_manual_printers():
                    self.assertEqual(saved["tls_pins"], self.card["tls_pins"])

    def test_storage_switch_before_or_during_confirm_cannot_save_trust(self):
        other = pathlib.Path(self.plugin.PLUGIN_STORAGE_DIR) / "other"
        for during in (False, True):
            with self.subTest(during=during):
                dashboard, posted = self.plugin.PrintersDashboard(), []
                with mock.patch.object(self.plugin, "_probe_bambu_certificate", return_value="a" * 64):
                    token = self.probe(dashboard, posted)
                original = self.plugin.PLUGIN_STORAGE_DIR
                def observe(*args):
                    self.plugin.PLUGIN_STORAGE_DIR = str(other)
                    return "a" * 64
                try:
                    if not during:
                        self.plugin.PLUGIN_STORAGE_DIR = str(other)
                    with mock.patch.object(self.plugin, "_probe_bambu_certificate", side_effect=observe):
                        dashboard._confirm_tls_trust({"token": token, "consent": True})
                finally:
                    self.plugin.PLUGIN_STORAGE_DIR = original
                self.assertEqual(posted[-1]["tlsTrustResult"]["error"], "tlsTrustStale")
                self.assertEqual(self.plugin.load_manual_printers()[0]["tls_pins"], self.card["tls_pins"])

    def test_inflight_probe_after_lifecycle_change_posts_no_challenge_or_error(self):
        for boundary in ("close", "ready", "unload"):
            with self.subTest(boundary=boundary):
                dashboard, posted = self.plugin.PrintersDashboard(), []
                dashboard._post = posted.append
                def observe(*args):
                    if boundary == "close":
                        dashboard.on_close()
                    elif boundary == "unload":
                        dashboard.on_unload()
                    else:
                        with mock.patch.object(dashboard, "_refresh_async"):
                            dashboard.on_message({"type": "ready"})
                    return "a" * 64
                with mock.patch.object(self.plugin, "_probe_bambu_certificate", side_effect=observe):
                    dashboard._probe_tls_trust(dict(self.identity(), protocol="mqtt"))
                self.assertEqual(posted, [])
                self.assertEqual(dashboard._tls_challenges, {})

    def test_inflight_confirmation_after_close_cannot_save_or_post(self):
        dashboard, posted = self.plugin.PrintersDashboard(), []
        with mock.patch.object(self.plugin, "_probe_bambu_certificate", return_value="a" * 64):
            token = self.probe(dashboard, posted)
        posted.clear()
        def observe(*args):
            dashboard.on_close()
            return "a" * 64
        with mock.patch.object(self.plugin, "_probe_bambu_certificate", side_effect=observe):
            dashboard._confirm_tls_trust({"token": token, "consent": True})
        self.assertEqual(posted, [])
        self.assertEqual(self.plugin.load_manual_printers()[0]["tls_pins"], self.card["tls_pins"])

    def test_mqtt_and_ftps_certificates_are_independent(self):
        self.card["tls_pins"]["ftps"] = hashlib.sha256(b"FTPS certificate").hexdigest()
        self.plugin.save_manual_printers([self.card])
        socket = mock.Mock()
        socket.getpeercert.return_value = b"FTPS certificate"
        self.plugin._verify_bambu_socket(self.card, "ftps", socket)
        with self.assertRaises(self.plugin.BambuTlsError):
            self.plugin._verify_bambu_socket(self.card, "mqtt", socket)

    def test_missing_mqtt_pin_blocks_all_application_bytes_even_with_insecure_flag(self):
        self.card["insecure"] = True
        self.card["tls_pins"].pop("mqtt")
        self.assertTrue(self.plugin.save_manual_printers([self.card]))
        with mock.patch.object(self.plugin.socket, "create_connection") as connect:
            status = self.plugin.poll_bambu(self.card)
            publish = self.plugin._bambu_publish(
                "192.0.2.77", "secret", "device/SERIAL/request", {}, printer=self.card)
        self.assertEqual(status["error_code"], "tls_trust_required")
        self.assertEqual(publish, "tls_trust_required")
        connect.assert_not_called()

    def test_certificate_probe_sends_no_mqtt_or_ftp_application_bytes(self):
        raw, wrapped, context = mock.Mock(), mock.Mock(), mock.Mock()
        wrapped.getpeercert.return_value = b"observed certificate"
        context.wrap_socket.return_value = wrapped
        with mock.patch.object(self.plugin.socket, "create_connection", return_value=raw) as connect, \
             mock.patch.object(self.plugin, "_SSL_INSECURE", context):
            mqtt = self.plugin._probe_bambu_certificate(self.card, "mqtt")
            ftps = self.plugin._probe_bambu_certificate(self.card, "ftps")
        self.assertEqual(mqtt, hashlib.sha256(b"observed certificate").hexdigest())
        self.assertEqual(ftps, mqtt)
        self.assertEqual([call.args[0][1] for call in connect.call_args_list], [8883, 990])
        wrapped.sendall.assert_not_called()
        raw.sendall.assert_not_called()
        wrapped.close.assert_called()

    def test_changed_mqtt_certificate_blocks_connect_and_command_bytes(self):
        raw, wrapped, context = mock.Mock(), mock.Mock(), mock.Mock()
        wrapped.getpeercert.return_value = b"another certificate"
        context.wrap_socket.return_value = wrapped
        with mock.patch.object(self.plugin.socket, "create_connection", return_value=raw), \
             mock.patch.object(self.plugin, "_SSL_INSECURE", context):
            status = self.plugin.poll_bambu(self.card)
            result = self.plugin._bambu_publish(
                "192.0.2.77", "secret", "device/SERIAL/request", {}, printer=self.card)
        self.assertEqual(status["error_code"], "tls_certificate_changed")
        self.assertEqual(result, "tls_certificate_changed")
        wrapped.sendall.assert_not_called()
        raw.sendall.assert_not_called()

    def test_changed_ftps_control_certificate_blocks_credentials_and_stor(self):
        temporary = tempfile.TemporaryDirectory(dir=self.plugin.PLUGIN_STORAGE_DIR)
        self.addCleanup(temporary.cleanup)
        source = pathlib.Path(temporary.name) / "job.gcode"
        source.write_bytes(b"G28\r\n")
        raw, wrapped, context = mock.Mock(), mock.Mock(), mock.Mock()
        wrapped.getpeercert.return_value = b"another certificate"
        context.wrap_socket.return_value = wrapped
        with mock.patch.object(self.plugin.socket, "create_connection", return_value=raw), \
             mock.patch.object(self.plugin, "_SSL_INSECURE", context):
            result = self.plugin._bambu_send_file(
                self.card, "job.gcode", str(source), True, False, None)
        self.assertEqual(result["error_code"], "tls_certificate_changed")
        self.assertEqual(result["journal_state"], "rejected")
        wrapped.sendall.assert_not_called()
        raw.sendall.assert_not_called()

    def test_data_pin_mismatch_closes_channel_before_file_bytes(self):
        import ftplib
        ftp = object.__new__(self.plugin._implicit_ftps_class())
        ftp._bambu_printer = self.card
        ftp._prot_p = True
        ftp.host = "192.0.2.77"
        ftp.sock = types.SimpleNamespace(session="session")
        data, wrapped = mock.Mock(), mock.Mock()
        wrapped.getpeercert.return_value = b"another certificate"
        ftp.context = mock.Mock()
        ftp.context.wrap_socket.return_value = wrapped
        with mock.patch.object(ftplib.FTP, "ntransfercmd", return_value=(data, None)):
            with self.assertRaises(self.plugin.BambuTlsError):
                ftp.ntransfercmd("STOR file.gcode")
        wrapped.sendall.assert_not_called()
        wrapped.close.assert_called_once()

    def test_upload_path_rejects_data_certificate_before_streaming_file(self):
        import ftplib
        source = pathlib.Path(self.plugin.PLUGIN_STORAGE_DIR) / "data-pin-test.gcode"
        source.write_bytes(b"G28\n")
        ftp_class = self.plugin._implicit_ftps_class()
        data, wrapped, context = mock.Mock(), mock.Mock(), mock.Mock()
        wrapped.getpeercert.return_value = b"different data certificate"
        context.wrap_socket.return_value = wrapped
        def connect(ftp, host, port, timeout):
            ftp.host = host
            ftp.sock = mock.Mock(session="control-session")
        def protect(ftp):
            ftp._prot_p = True
        with mock.patch.object(self.plugin, "ssl_ctx_for", return_value=context), \
             mock.patch.object(ftp_class, "connect", connect), \
             mock.patch.object(ftp_class, "login"), \
             mock.patch.object(ftp_class, "prot_p", protect), \
             mock.patch.object(ftp_class, "voidcmd", return_value="200 OK"), \
             mock.patch.object(ftp_class, "quit"), \
             mock.patch.object(ftplib.FTP, "ntransfercmd", return_value=(data, None)), \
             mock.patch.object(self.plugin, "bambu_start_print") as start:
            result = self.plugin._bambu_send_file(
                self.card, source.name, str(source), True, False, None)
        # STOR may already have created an empty remote file; keep its outcome
        # uncertain even though the untrusted data channel receives no bytes.
        self.assertEqual(result["error_code"], "unknown_outcome")
        self.assertEqual(result["error_key"], "tlsCertificateChanged")
        wrapped.sendall.assert_not_called()
        data.sendall.assert_not_called()
        wrapped.close.assert_called_once()
        start.assert_not_called()

    def test_probe_confirm_ignores_browser_pin_and_consumes_challenge(self):
        dashboard = self.plugin.PrintersDashboard()
        posted = []
        dashboard._post = posted.append
        observed = hashlib.sha256(b"new certificate").hexdigest()
        with mock.patch.object(self.plugin, "_probe_bambu_certificate", return_value=observed), \
             mock.patch.object(dashboard, "_refresh_worker"):
            dashboard._probe_tls_trust(dict(self.identity(), protocol="mqtt", requestId=4))
            challenge = posted[-1]["tlsTrustResult"]
            self.assertEqual(challenge["old"], self.card["tls_pins"]["mqtt"])
            self.assertEqual(challenge["observed"], observed)
            dashboard._confirm_tls_trust({"token": challenge["token"], "consent": True,
                                           "pin": "0" * 64, "requestId": 4})
            self.assertTrue(posted[-1]["tlsTrustResult"]["saved"])
            dashboard._confirm_tls_trust({"token": challenge["token"], "consent": True,
                                           "requestId": 4})
        self.assertEqual(posted[-1]["tlsTrustResult"]["error"], "tlsTrustStale")
        saved = self.plugin.load_manual_printers()[0]
        self.assertEqual(saved["tls_pins"]["mqtt"], observed)
        self.assertEqual(saved["tls_pins"]["ftps"], self.card["tls_pins"]["ftps"])
        self.assertEqual(saved["binding_revision"], self.card["binding_revision"] + 1)
        self.assertIsNone(self.plugin.resolve_printer_target([saved], self.identity()))

    def test_pre_dispatch_pin_refusal_is_journal_rejected_not_unknown(self):
        self.card["tls_pins"].pop("ftps")
        self.assertTrue(self.plugin.save_manual_printers([self.card]))
        temporary = tempfile.TemporaryDirectory(dir=self.plugin.PLUGIN_STORAGE_DIR)
        self.addCleanup(temporary.cleanup)
        source = pathlib.Path(temporary.name) / "job.gcode"
        source.write_bytes(b"G28\n")
        with mock.patch.object(self.plugin.socket, "create_connection") as connect:
            result = self.plugin._bambu_send_file(
                self.card, "job.gcode", str(source), True, False, None)
        self.assertEqual(result["journal_state"], "rejected")
        self.assertEqual(result["evidence_ref"], "tls:pre_dispatch")
        connect.assert_not_called()
        journal = mock.Mock()
        dashboard = self.plugin.PrintersDashboard()
        dashboard._journal_finish((journal, "operation"), result)
        journal.report.assert_called_once_with(
            "operation", "rejected", evidence_ref="tls:pre_dispatch")

    def test_repeated_probe_messages_are_admitted_only_once_until_completion(self):
        dashboard = self.plugin.PrintersDashboard()
        posted = []
        submitted = []
        dashboard._post = posted.append
        dashboard._submit_ui_work = lambda work, **kwargs: submitted.append(work)
        message = dict(self.identity(), type="tls-probe", protocol="mqtt", requestId=1)
        for _ in range(30):
            dashboard.on_message(message)
        self.assertEqual(len(submitted), 1)
        self.assertEqual(len([item for item in posted
                              if item.get("tlsTrustResult", {}).get("error") == "tlsTrustBusy"]), 29)
        dashboard.on_message({"type": "tls-close"})
        dashboard.on_message(message)
        self.assertEqual(len(submitted), 1)

    def test_save_failure_preserves_old_pins_and_reports_error(self):
        dashboard = self.plugin.PrintersDashboard()
        posted = []
        dashboard._post = posted.append
        new = hashlib.sha256(b"new certificate").hexdigest()
        with mock.patch.object(self.plugin, "_probe_bambu_certificate", return_value=new):
            dashboard._probe_tls_trust(dict(self.identity(), protocol="mqtt"))
            token = posted[-1]["tlsTrustResult"]["token"]
            with mock.patch.object(self.plugin, "save_manual_printers", return_value=False):
                dashboard._confirm_tls_trust({"token": token, "consent": True})
        self.assertEqual(posted[-1]["tlsTrustResult"]["error"], "tlsTrustSaveFailed")
        self.assertEqual(self.plugin.load_manual_printers()[0]["tls_pins"], self.card["tls_pins"])

    def test_edit_or_expiry_invalidates_challenge_without_changing_pins(self):
        dashboard = self.plugin.PrintersDashboard()
        posted = []
        dashboard._post = posted.append
        with mock.patch.object(self.plugin, "_probe_bambu_certificate",
                               return_value=hashlib.sha256(b"new").hexdigest()):
            dashboard._probe_tls_trust(dict(self.identity(), protocol="mqtt"))
            token = posted[-1]["tlsTrustResult"]["token"]
            dashboard._tls_challenges[token]["expires"] = time.monotonic() - 1
            dashboard._confirm_tls_trust({"token": token, "consent": True})
            self.assertEqual(posted[-1]["tlsTrustResult"]["error"], "tlsTrustStale")
            dashboard._probe_tls_trust(dict(self.identity(), protocol="mqtt"))
            token = posted[-1]["tlsTrustResult"]["token"]
            dashboard._invalidate_tls_challenges()
            dashboard._confirm_tls_trust({"token": token, "consent": True})
        self.assertEqual(posted[-1]["tlsTrustResult"]["error"], "tlsTrustStale")
        self.assertEqual(self.plugin.load_manual_printers()[0]["tls_pins"], self.card["tls_pins"])

    def test_wildcard_serial_learning_keeps_pins_but_stale_poll_cannot_retarget(self):
        self.card["serial"] = ""
        self.assertTrue(self.plugin.save_manual_printers([self.card]))
        old = dict(self.card)
        learned = dict(old, serial="LEARNED")
        self.plugin.persist_discovered_serial(learned)
        saved = self.plugin.load_manual_printers()[0]
        self.assertEqual(saved["tls_pins"], old["tls_pins"])
        self.assertEqual(self.plugin._bambu_pin(saved, "mqtt"), old["tls_pins"]["mqtt"])
        self.plugin.persist_discovered_serial(dict(old, serial="WRONG"))
        self.assertEqual(self.plugin.load_manual_printers()[0]["serial"], "LEARNED")


if __name__ == "__main__":
    unittest.main()
