"""Check that the installable wheel can import without source helpers nearby."""

import importlib
import importlib.util
import pathlib
import sys
import tempfile
import unittest

from test_printers_plugin import _load_plugin


ROOT = pathlib.Path(__file__).resolve().parent


class WheelImportTest(unittest.TestCase):
    def test_wheel_imports_embedded_helpers_in_isolation(self):
        _load_plugin()  # Register the same minimal Orca host contract used by runtime tests.
        spec = importlib.util.spec_from_file_location("print_farm_wheel_builder", ROOT / "build_package.py")
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)

        with tempfile.TemporaryDirectory() as temp:
            output = pathlib.Path(temp) / "candidate"
            builder.build(output)
            wheel = output / "wheels" / ("printers-%s-py3-none-any.whl" % builder.current_version())
            original = sys.modules.pop("printers_plugin", None)
            sys.path.insert(0, str(wheel))
            try:
                module = importlib.import_module("printers_plugin")
                self.assertIn(str(wheel), module.__file__)
                self.assertTrue(callable(module._pf_artifact.inspect_staged_path))
                self.assertTrue(callable(module._pf_transfer.ArtifactTransferManager))
                self.assertTrue(callable(module._pf_journal.OperationJournal))
                self.assertTrue(callable(module._pf_scheduler.ActionScheduler))
                self.assertEqual(module._pf_settings.normalize("dashboard", None)["poll_seconds"], 15)
                # The four device adapters are embedded and really run from the wheel
                # (an invalid address is rejected before any network access).
                for name, helper, code in (
                        ("duet_adapter", module._pf_duet, "invalid_request"),
                        ("prusalink_adapter", module._pf_prusalink, "invalid_address"),
                        ("creality_adapter", module._pf_creality, "invalid_request"),
                        ("flashforge_adapter", module._pf_flashforge, "invalid_request")):
                    self.assertEqual(helper.__name__, module.__name__ + "_" + name)
                    self.assertFalse(hasattr(helper, "__file__"), "must be embedded, not loaded from disk")
                    result = helper.poll("ftp://invalid.example", None, "")
                    self.assertFalse(result["online"])
                    self.assertEqual(result["error_code"], code)
                self.assertEqual(module._pf_creality.ws_base("http://127.0.0.1:80/upload", 9999),
                                 "ws://127.0.0.1:9999")
                self.assertIs(module._ADAPTER_BACKENDS["duet"], module._pf_duet)
                self.assertIs(module._ADAPTER_BACKENDS["prusalink"], module._pf_prusalink)
                self.assertIs(module._ADAPTER_BACKENDS["crealityprint"], module._pf_creality)
            finally:
                sys.path.remove(str(wheel))
                sys.modules.pop("printers_plugin", None)
                if original is not None:
                    sys.modules["printers_plugin"] = original


def _load_builder():
    spec = importlib.util.spec_from_file_location("print_farm_release_builder", ROOT / "build_package.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    return builder


class ReleaseSourceGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.builder = _load_builder()
        source = cls.builder.SOURCE.read_text(encoding="utf-8")
        cls.release = cls.builder._source_with_embedded_helpers(
            cls.builder._source_with_embedded_locales(source))

    def test_shipped_plugin_has_no_forbidden_constructs(self):
        self.assertEqual(self.builder.release_source_violations(self.release), [])

    def test_helpers_are_real_code_not_executed_strings(self):
        import ast

        tree = ast.parse(self.release)
        called = {node.func.id for node in ast.walk(tree)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        self.assertFalse(called & {"exec", "eval", "compile", "__import__"})
        defined = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
        for alias, _name in self.builder.HELPER_LOAD.findall(
                self.builder.SOURCE.read_text(encoding="utf-8")):
            self.assertIn("_pf_define_" + alias, defined)

    def test_guard_names_each_forbidden_construct(self):
        samples = {
            "import subprocess": "import subprocess",
            "from ctypes import windll": "from ctypes import",
            "import webbrowser": "import webbrowser",
            "import importlib.util": "import importlib.util",
            "import http.server": "import http.server",
            "exec('1')": "call to exec()",
            "x = compile('1', 'a', 'exec')": "call to compile()",
            "__import__('os')": "call to __import__()",
            "import os\nos.environ['A']": "os.environ",
            "import os\nos.getenv('A')": "os.getenv",
            "import os\nos.startfile('a')": "os.startfile",
            "import os\ngetattr(os, 'startfile')": "indirect getattr() on os",
        }
        for source, expected in samples.items():
            found = self.builder.release_source_violations(source)
            self.assertTrue(any(expected in item for item in found), (source, found))

    def test_guard_allows_regex_compile_and_plain_os_use(self):
        source = "import re\nimport os\nre.compile('a')\nos.path.join('a', 'b')\n"
        self.assertEqual(self.builder.release_source_violations(source), [])

    def test_helper_that_would_break_when_wrapped_is_refused(self):
        for body in ("X = 1\ndef f():\n    global X\n    X = 2\n",
                     "from os import *\n",
                     "def f():\n    return __name__\n",
                     "def f():\n    return globals()\n"):
            with self.assertRaises(ValueError):
                self.builder._helper_definition("sample", "sample", body)

    def test_future_annotations_helper_keeps_lazy_annotations(self):
        definition, doc = self.builder._helper_definition(
            "sample", "sample",
            '"""Doc."""\nfrom __future__ import annotations\n'
            "def f(x: Later) -> Later:\n    return x\n"
            "class Later:\n    value: Later = None\n")
        self.assertEqual(doc, "Doc.")
        self.assertNotIn("__future__", definition)
        scope = {}
        # Test-only evaluation of the generated function; the plugin itself never does this.
        exec(compile(definition, "sample", "exec"), scope)
        values = scope["_pf_define_sample"]()
        self.assertEqual(values["f"].__annotations__, {"x": "Later", "return": "Later"})
        self.assertEqual(values["f"](3), 3)


if __name__ == "__main__":
    unittest.main()
