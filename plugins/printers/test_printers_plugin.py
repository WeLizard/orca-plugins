import importlib.util
import pathlib
import sys
import tempfile
import types
import unittest


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
        psGCodePostProcess="post-process",
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
        self.assertEqual(len(self.plugin.load_outbox_index()), 1)

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
        self.assertEqual(len(self.plugin.load_outbox_index()), 1)

    def test_bulk_toggle_state_comes_from_process_presets(self):
        self.plugin.DATA_DIR = self.temp_dir.name
        process_dir = pathlib.Path(self.temp_dir.name) / "user" / "account" / "process"
        process_dir.mkdir(parents=True)
        (process_dir / "bound.json").write_text(
            '{"slicing_pipeline_plugin": ["Printers Outbox"]}', encoding="utf-8")
        (process_dir / "free.json").write_text('{}', encoding="utf-8")

        self.assertEqual(self.plugin.outbox_binding_status(), (1, 2))
        self.assertEqual(self.plugin.set_outbox_binding(True), 2)
        self.assertEqual(self.plugin.outbox_binding_status(), (2, 2))
        self.assertEqual(self.plugin.set_outbox_binding(False), 2)
        self.assertEqual(self.plugin.outbox_binding_status(), (0, 2))


if __name__ == "__main__":
    unittest.main()
