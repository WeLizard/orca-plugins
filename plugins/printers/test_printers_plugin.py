import importlib.util
import pathlib
import re
import sys
import tempfile
import tomllib
import types
import unittest
from unittest import mock


PLUGIN_PATH = pathlib.Path(__file__).with_name("printers_plugin.py")


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


def _load_plugin():
    fake_orca = types.ModuleType("orca")
    fake_orca.base = object
    fake_orca.plugin = lambda cls: cls
    fake_orca.register_capability = lambda capability: None
    fake_orca.ExecutionResult = _ExecutionResult
    fake_orca.PluginResult = types.SimpleNamespace(RecoverableError="recoverable")
    fake_orca.script = types.SimpleNamespace(ScriptPluginCapabilityBase=object)
    fake_orca.slicing = types.SimpleNamespace(
        SlicingPipelineCapabilityBase=object,
        Step=types.SimpleNamespace(psGCodePostProcess="post-process"),
    )
    sys.modules["orca"] = fake_orca

    spec = importlib.util.spec_from_file_location("printers_plugin_under_test", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PrintersOutboxTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin = _load_plugin()

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.temp_dir.name)
        self.plugin.OUTBOX_DIR = str(root / "outbox")
        self.plugin.OUTBOX_INDEX = str(root / "outbox_index.json")
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

    def test_file_target_is_case_insensitive(self):
        result = self.execute(" file ")

        self.assertEqual(result[0], "success")
        self.assertEqual(len(self.plugin.load_outbox_index()), 1)

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

    def test_hub_version_matches_wheel_project(self):
        source = PLUGIN_PATH.read_text(encoding="utf-8")
        match = re.search(r'^# version = "([0-9]+\.[0-9]+\.[0-9]+)"$', source, re.MULTILINE)
        self.assertIsNotNone(match)
        project = tomllib.loads(PLUGIN_PATH.with_name("pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(match.group(1), project["project"]["version"])

    def test_plugin_does_not_forge_host_owned_process_manifest_refs(self):
        source = PLUGIN_PATH.read_text(encoding="utf-8")
        self.assertNotIn("OUTBOX_REF", source)
        self.assertNotIn("outbox-toggle", source)
        self.assertNotIn("set_outbox_binding", source)

    def test_curl_fallback_keeps_temp_file_inside_plugin_data(self):
        temp_file = types.SimpleNamespace(
            name=str(pathlib.Path(self.temp_dir.name) / "upload.gcode"),
            write=mock.Mock(),
            close=mock.Mock(),
        )
        with mock.patch.object(self.plugin.shutil, "which", return_value="curl"), \
             mock.patch.object(self.plugin.tempfile, "NamedTemporaryFile", return_value=temp_file) as make_temp, \
             mock.patch.object(self.plugin.subprocess, "run", return_value=types.SimpleNamespace(returncode=0, stderr="")), \
             mock.patch.object(self.plugin.os, "remove"):
            self.assertEqual(self.plugin._curl_stor("printer", "code", "job.gcode", b"G28"), "")

        make_temp.assert_called_once_with(delete=False, suffix=".gcode", dir=self.plugin.PLUGIN_DIR)


if __name__ == "__main__":
    unittest.main()
