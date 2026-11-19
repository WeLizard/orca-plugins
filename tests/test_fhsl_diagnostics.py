import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import shutil
import uuid
import unittest
from unittest.mock import patch
import zipfile
import zlib

spec = importlib.util.spec_from_file_location("inspect_fhsl", Path(__file__).resolve().parents[1] / "tools" / "inspect_fhsl.py")
diag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diag)


def png(depth=8, color=0):
    chunk = b"IHDR" + struct.pack(">IIBBBBB", 2048, 2048, depth, color, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + chunk + struct.pack(">I", zlib.crc32(chunk))


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.data = Path(__file__).parent / ("fixture_" + uuid.uuid4().hex[:8])
        self.data.mkdir()
        self.addCleanup(shutil.rmtree, self.data)
        self.module = next(iter(diag.EXPECTED))
        self.copy = self.data / "orca_plugins" / "_subscribed" / "private-account" / "cloud-id"
        self.extracted = self.copy / "__whl_extracted__" / self.module
        self.textures = self.extracted / self.module / "textures"
        self.textures.mkdir(parents=True)

    def report(self):
        with patch.object(diag, "native_probe", return_value={"status": "mocked"}):
            return diag.inspect(self.data)

    def test_l8_classification_and_redaction(self):
        (self.textures / "height.png").write_bytes(png())
        pack = self.report()["packs"][0]
        self.assertEqual(pack["classification"], "all_PNG_L8")
        self.assertFalse(pack["count_matches"])
        self.assertNotIn(str(self.data), json.dumps(pack))
        self.assertNotIn("private-account", json.dumps(pack))
        self.assertEqual(pack["images"][0]["integrity"], "IHDR_only_no_pixel_decode_or_remaining_chunk_CRC")

    def test_corrupt_ihdr_not_validated_as_l8(self):
        (self.textures / "bad.png").write_bytes(png()[:-1] + b"X")
        self.assertFalse(self.report()["packs"][0]["images"][0]["header_valid"])

    def test_16bit_and_jpeg_are_mixed(self):
        (self.textures / "height.png").write_bytes(png(16))
        (self.textures / "old.jpg").write_bytes(b"\xff\xd8\xff\xd9")
        self.assertEqual(self.report()["packs"][0]["classification"], "mixed_or_non_L8_or_unreadable")

    def test_wheel_version_sidecar_and_record_missing(self):
        data = png()
        (self.textures / "present.png").write_bytes(data)
        (self.copy / ".install_state.json").write_text(json.dumps({
            "installed_version": "1.0.1", "enabled": True, "token": "secret"}))
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
        record = f"{self.module}/textures/present.png,sha256={digest},{len(data)}\n{self.module}/textures/missing.png,,\n"
        wheel = self.copy / "sample.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("sample.dist-info/METADATA", "Name: sample\nVersion: 1.0.1rc1\n")
            archive.writestr("sample.dist-info/RECORD", record)
        pack = self.report()["packs"][0]
        self.assertEqual(pack[".install_state.json"]["installed_version"], "1.0.1")
        self.assertEqual(pack["wheels"][0]["metadata"]["version"], "1.0.1rc1")
        self.assertTrue(pack["wheels"][0]["sidecar_version_differs_from_wheel"])
        self.assertEqual(pack["wheels"][0]["sha256"], hashlib.sha256(wheel.read_bytes()).hexdigest())
        self.assertEqual(pack["wheels"][0]["extracted_RECORD"]["missing_textures"], ["missing.png"])
        self.assertNotIn("secret", json.dumps(pack))

    def test_symlink_file_ignored(self):
        outside = self.data / "outside.png"
        outside.write_bytes(png())
        link = self.textures / "linked.png"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("symlink privilege unavailable")
        self.assertEqual(self.report()["packs"][0]["image_count"], 0)

    def test_errors_never_echo_paths(self):
        exc = FileNotFoundError(2, "secret path", "C:/Users/Private/person.txt")
        self.assertNotIn("Private", json.dumps(diag.error_code(exc)))

    def test_missing_root_safe(self):
        report = diag.inspect(self.data / "missing")
        self.assertEqual(report["packs"], [])
        self.assertNotIn(str(self.data), json.dumps(report))

    def test_short_module_preserves_long_distribution_identity(self):
        short = self.copy / "__whl_extracted__" / "fhslh" / "fhslh" / "t"
        short.mkdir(parents=True)
        (short / "height.png").write_bytes(png())
        unrelated = self.copy / "__whl_extracted__" / "unrelated" / "unrelated" / "textures"
        unrelated.mkdir(parents=True)
        (unrelated / "height.png").write_bytes(png())
        with zipfile.ZipFile(self.copy / "candidate.whl", "w") as archive:
            archive.writestr("candidate.dist-info/METADATA", "Name: filamenthub-surface-hard-surface\nVersion: 1.0.2\n")
        packs = self.report()["packs"]
        short_pack = next(p for p in packs if p["import_module"] == "fhslh")
        self.assertEqual(short_pack["package"], self.module)
        self.assertEqual(short_pack["expected_count"], 53)
        self.assertTrue(short_pack["wheels"][0]["distribution_identity_matches"])
        self.assertEqual(short_pack["wheels"][0]["metadata"]["name"], "filamenthub-surface-hard-surface")
        self.assertNotIn("unrelated", {p["import_module"] for p in packs})


if __name__ == "__main__":
    unittest.main()
