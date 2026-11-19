from __future__ import annotations

import hashlib
import shutil
import sys
import tomllib
import unittest
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from fhsl_package import PACKS, audit_wheel, build_one, valid_version, validate_png_payload, validate_source

CAPABILITY_LABELS = {
    "fhsl-hard-surface": "FilamentHub Surface Library — Hard Surface",
    "fhsl-nature-organics": "FilamentHub Surface Library — Nature & Organics",
    "fhsl-textiles-materials": "FilamentHub Surface Library — Textiles & Materials",
}


class FHSLPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        work_root = ROOT / ".worktmp"
        work_root.mkdir(exist_ok=True)
        cls.fixture_root = work_root / f"test-fhsl-{uuid.uuid4().hex}"
        cls.fixture_root.mkdir()
        cls.releases = {}
        for key in PACKS:
            release_root = cls.fixture_root / key
            cls.releases[key] = (release_root, build_one(key, release_root))

    @classmethod
    def tearDownClass(cls):
        if cls.fixture_root.exists() and cls.fixture_root.parent == ROOT / ".worktmp":
            shutil.rmtree(cls.fixture_root)

    def test_independent_versions_and_checked_texture_inventory(self):
        self.assertTrue(valid_version("1.0.2"))
        self.assertFalse(valid_version("1.0.11"))
        for key, (distribution, import_name, count, _display) in PACKS.items():
            with self.subTest(plugin=key):
                files = validate_source(ROOT / "plugins" / key, distribution, import_name, count)
                self.assertEqual(len(files), count + 1)

    def test_plugins_register_with_strict_orca_host_and_distinct_names(self):
        import types
        names = []
        registered = []
        class CapabilityBase: pass
        class PluginBase: pass
        def plugin(cls): return cls
        def register(capability): registered.append(capability)
        host = types.ModuleType("orca")
        host.base = PluginBase
        host.plugin = plugin
        host.register_capability = register
        host.resource_texture = types.SimpleNamespace(ResourceTexturePluginCapabilityBase=CapabilityBase)
        original = sys.modules.get("orca")
        sys.modules["orca"] = host
        try:
            for key, (_distribution, import_name, _count, _display) in PACKS.items():
                source = ROOT / "plugins" / key / import_name / "__init__.py"
                namespace = {"__name__": f"_test_{import_name}", "__file__": str(source)}
                exec(compile(source.read_text(encoding="utf-8"), str(source), "exec"), namespace)
                namespace["SurfaceTexturePlugin"]().register_capabilities()
                capability = registered[-1]()
                names.append(capability.get_name())
                self.assertEqual(capability.get_texture_dir(), str(source.parent / "t"))
                self.assertEqual(capability.get_name(), CAPABILITY_LABELS[key])
            self.assertEqual(len(set(names)), 3)
            self.assertEqual(len(registered), 3)
        finally:
            if original is None:
                sys.modules.pop("orca", None)
            else:
                sys.modules["orca"] = original

    def test_corrupted_png_payload_is_rejected(self):
        key = "fhsl-hard-surface"
        import_name = PACKS[key][1]
        image = next((ROOT / "plugins" / key / import_name / "t").glob("*.png"))
        data = bytearray(image.read_bytes())
        offset = 8
        while offset < len(data):
            size = int.from_bytes(data[offset:offset + 4], "big")
            kind = data[offset + 4:offset + 8]
            if kind == b"IDAT" and size:
                data[offset + 8 + size // 2] ^= 0xFF
                break
            offset += size + 12
        else:
            self.fail("PNG contains no non-empty IDAT chunk")
        with self.assertRaises(ValueError):
            validate_png_payload(bytes(data), image.name)

    def test_built_wheels_are_deterministic_and_pass_full_audit(self):
        for key, (distribution, import_name, count, _display) in PACKS.items():
            with self.subTest(plugin=key):
                release_root, wheel = self.releases[key]
                first_hash = hashlib.sha256(wheel.read_bytes()).hexdigest()
                second = build_one(key, release_root)
                self.assertEqual(hashlib.sha256(second.read_bytes()).hexdigest(), first_hash)
                project = ROOT / "plugins" / key / "pyproject.toml"
                version = tomllib.loads(project.read_text(encoding="utf-8"))["project"]["version"]
                audit_wheel(second, distribution, import_name, version, count)
                self.assertLess(second.stat().st_size, 100_000_000)
                sums = (release_root / "SHA256SUMS").read_text(encoding="ascii")
                self.assertIn(first_hash, sums)
                self.assertTrue((release_root / "RELEASE_NOTES.md").is_file())
                with zipfile.ZipFile(second) as package:
                    self.assertFalse(any(name.endswith(".pyc") for name in package.namelist()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
