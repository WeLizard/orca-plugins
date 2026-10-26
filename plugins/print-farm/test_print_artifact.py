from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest import mock

import print_artifact as artifacts


WORK_TEMP = Path(r"F:\orca-plugins\.worktmp")
REAL_UNSLICED_3MF = Path(r"C:\Users\Lizard\Downloads\Assembly.3mf")


def make_3mf(path: Path, *, plates=(1, 3), include_params=True,
             include_slice_info=True, project_settings=None, extra_entries=(),
             compression=zipfile.ZIP_STORED):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(path, "w", compression=compression) as archive:
            archive.writestr("3D/3dmodel.model", b"<model/>\n")
            if include_params:
                archive.writestr("Metadata/project_settings.config",
                                 project_settings or
                                 b'{"nozzle_diameter":[0.4],"layer_height":0.2}')
            if include_slice_info:
                archive.writestr("Metadata/slice_info.config",
                                 b'{"OrcaSlicer-Version":"2.5.0-alpha"}')
            for plate in plates:
                archive.writestr(f"Metadata/plate_{plate}.gcode", f"G28\n; plate={plate}\n".encode())
            for name, payload, attrs in extra_entries:
                info = zipfile.ZipInfo(name)
                info.compress_type = archive.compression
                if attrs is not None:
                    info.external_attr = attrs
                archive.writestr(info, payload)


class ArtifactStagingTests(unittest.TestCase):
    def setUp(self):
        WORK_TEMP.mkdir(parents=True, exist_ok=True)
        self.test_root = Path(tempfile.mkdtemp(prefix="print-artifact-test-", dir=WORK_TEMP))
        self.staging = self.test_root / "staged"

    def make_source(self, name: str, content: bytes) -> Path:
        source_dir = self.test_root / "source"
        source_dir.mkdir(exist_ok=True)
        path = source_dir / name
        path.write_bytes(content)
        return path

    def test_gcode_stages_with_bounded_digest_and_metadata(self):
        payload = (b"G28\n" * 100) + (
            b"; printer_settings_id = Orca Voron 2.4\n"
            b"; printer_model = Voron 2.4\n"
            b"; layer_height = 0.2\n"
        )
        source = self.make_source("job.gcode", payload)
        result = artifacts.stage_artifact(source, self.staging, chunk_bytes=31)
        self.assertEqual(result.manifest["kind"], "gcode")
        self.assertEqual(result.manifest["size_bytes"], len(payload))
        self.assertEqual(result.manifest["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(result.manifest["provenance"], "manual_import")
        self.assertEqual(result.metadata["printer_model"], "Voron 2.4")
        self.assertTrue(result.manifest_complete)
        self.assertTrue(artifacts.verify_staged_artifact(result, chunk_bytes=17))
        self.assertNotEqual(result.path.name, source.name)
        self.assertFalse(result.path.stat().st_mode & stat.S_IWRITE)
        with self.assertRaises(TypeError):
            result.manifest["kind"] = "unknown"

    def test_gco_is_supported_and_unknown_parameters_remain_incomplete(self):
        result = artifacts.stage_artifact(
            self.make_source("job.gco", b"G28\n"), self.staging,
        )
        self.assertEqual(result.manifest["kind"], "gcode")
        self.assertFalse(result.manifest_complete)
        self.assertNotIn("parameters_sha256", result.manifest)

    def test_rejects_unsupported_types_and_oversize_before_copy(self):
        source = self.make_source("job.bgcode", b"binary")
        with self.assertRaises(artifacts.ArtifactError):
            artifacts.stage_artifact(source, self.staging)
        source = self.make_source("large.gcode", b"12345")
        with self.assertRaisesRegex(artifacts.ArtifactError, "size"):
            artifacts.stage_artifact(source, self.staging, max_bytes=4)
        self.assertFalse(self.staging.exists())

    def test_sliced_3mf_records_exact_plate_entries_and_metadata(self):
        source = self.test_root / "model.gcode.3mf"
        make_3mf(source, plates=(1, 3))
        result = artifacts.stage_artifact(source, self.staging, chunk_bytes=23)
        self.assertEqual(result.manifest["kind"], "sliced_3mf")
        self.assertEqual(result.plate_entries,
                         ("Metadata/plate_1.gcode", "Metadata/plate_3.gcode"))
        self.assertEqual(result.metadata["slicer_build"], "2.5.0-alpha")
        self.assertFalse(result.manifest_complete)
        self.assertEqual(len(result.manifest["parameters_sha256"]), 64)
        self.assertTrue(artifacts.verify_staged_artifact(result))

    def test_project_metadata_is_allowlisted_and_sanitized(self):
        source = self.test_root / "project-metadata.gcode.3mf"
        config = json.dumps({
            "printer_settings_id": "  Orca Profile A  ",
            "printer_model": "Model A\nInjected",
            "nozzle_diameter": [0.4, "0.6"],
            "secret": "must not be copied",
            "oversized": "x" * 257,
        }).encode()
        make_3mf(source, project_settings=config)
        result = artifacts.stage_artifact(source, self.staging)
        self.assertEqual(result.metadata["printer_settings_id"], "Orca Profile A")
        self.assertEqual(result.metadata["nozzle_diameter"], ["0.4", "0.6"])
        self.assertNotIn("printer_model", result.metadata)
        self.assertNotIn("secret", result.metadata)
        self.assertNotIn("oversized", result.metadata)

    @unittest.skipUnless(
        (Path(r"C:\Users\Lizard\Downloads") / "Assembly_ABS_12h16m.gcode.3mf").is_file(),
        "optional local sliced fixture not present",
    )
    def test_real_sliced_sample_exposes_printer_metadata_read_only(self):
        sample = Path(r"C:\Users\Lizard\Downloads\Assembly_ABS_12h16m.gcode.3mf")
        plates, metadata, _ = artifacts._inspect_3mf(sample, artifacts.DEFAULT_CHUNK_BYTES)
        self.assertIn("Metadata/plate_1.gcode", plates)
        self.assertEqual(metadata["printer_settings_id"], "Bambu Lab P2S 0.4 nozzle")
        self.assertEqual(metadata["printer_model"], "Bambu Lab P2S")
        self.assertEqual(metadata["nozzle_diameter"], ["0.4"])
        self.assertRegex(metadata["parameters_sha256"], r"^[0-9a-f]{64}$")

    def test_plate_selection_requires_exact_discovered_member(self):
        source = self.test_root / "model.gcode.3mf"
        make_3mf(source, plates=(3,))
        result = artifacts.stage_artifact(source, self.staging)
        selected = artifacts.select_plate_entry(result, "Metadata/plate_3.gcode")
        self.assertEqual(selected["selected_plate_entry"], "Metadata/plate_3.gcode")
        self.assertTrue(artifacts.manifest_is_complete(selected))
        with self.assertRaises(artifacts.ArtifactError):
            artifacts.select_plate_entry(result, "Metadata/plate_1.gcode")
        with self.assertRaises(TypeError):
            selected["selected_plate_entry"] = "Metadata/plate_1.gcode"

    def test_inspect_receiver_owned_staged_file_without_second_copy(self):
        self.staging.mkdir()
        path = self.staging / "receiver-owned-opaque.part"
        payload = b"G28\n; printer_model = Voron\n"
        path.write_bytes(payload)
        before = set(self.staging.iterdir())
        inspected = artifacts.inspect_staged_path(
            path, "accepted.gcode", self.staging,
            expected_sha256=hashlib.sha256(payload).hexdigest(),
        )
        self.assertEqual(inspected.path, path.resolve())
        self.assertEqual(set(self.staging.iterdir()), before)
        self.assertTrue(artifacts.verify_staged_artifact(inspected))
        with self.assertRaisesRegex(artifacts.ArtifactError, "digest"):
            artifacts.inspect_staged_path(path, "accepted.gcode", self.staging,
                                          expected_sha256="0" * 64)

    def test_receiver_staged_path_must_be_inside_python_storage_root(self):
        path = self.make_source("outside.gcode", b"G28\n")
        with self.assertRaisesRegex(artifacts.ArtifactError, "outside private storage"):
            artifacts.inspect_staged_path(
                path, path.name, self.staging,
                expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            )

    def test_selected_plate_is_streamed_to_verified_gcode_copy(self):
        source = self.test_root / "model.gcode.3mf"
        make_3mf(source, plates=(2, 3))
        artifact = artifacts.stage_artifact(source, self.staging, chunk_bytes=19)
        selected = artifacts.stage_selected_plate(
            artifact, "Metadata/plate_3.gcode", self.staging, chunk_bytes=13,
        )
        self.assertEqual(selected.plate_entry, "Metadata/plate_3.gcode")
        self.assertEqual(selected.source_artifact_id, artifact.manifest["artifact_id"])
        self.assertTrue(artifacts.verify_staged_plate(selected, chunk_bytes=7))
        self.assertEqual(selected.path.read_bytes(), b"G28\n; plate=3\n")
        self.assertFalse(selected.path.stat().st_mode & stat.S_IWRITE)

    def test_discard_removes_only_staged_artifact_and_its_selected_plate(self):
        source = self.make_source("original.gcode.3mf", b"not a zip")
        make_3mf(source)
        artifact = artifacts.stage_artifact(source, self.staging)
        plate = artifacts.stage_selected_plate(
            artifact, "Metadata/plate_1.gcode", self.staging,
        )

        self.assertTrue(artifacts.discard_staged_artifact(
            artifact, self.staging, selected_plate=plate,
        ))
        self.assertFalse(artifact.path.exists())
        self.assertFalse(plate.path.exists())
        self.assertTrue(source.exists())
        self.assertFalse(artifacts.discard_staged_artifact(
            artifact, self.staging, selected_plate=plate,
        ))

    def test_discard_accepts_receiver_generated_incoming_artifact_name(self):
        path = self.staging / ("pf-artifact-" + "a" * 32 + ".gcode")
        path.parent.mkdir(parents=True)
        payload = b"G28\n"
        path.write_bytes(payload)
        artifact = artifacts.inspect_staged_path(
            path, "received.gcode", self.staging,
            expected_sha256=hashlib.sha256(payload).hexdigest(),
        )

        self.assertTrue(artifacts.discard_staged_artifact(artifact, self.staging))
        self.assertFalse(path.exists())

    def test_discard_rejects_unrecognized_or_outside_paths_without_deleting(self):
        arbitrary = self.staging / "user-copy.gcode"
        arbitrary.parent.mkdir(parents=True)
        arbitrary.write_bytes(b"G28\n")
        artifact = artifacts.inspect_staged_path(
            arbitrary, arbitrary.name, self.staging,
            expected_sha256=hashlib.sha256(arbitrary.read_bytes()).hexdigest(),
        )
        with self.assertRaisesRegex(artifacts.ArtifactError, "plugin-created"):
            artifacts.discard_staged_artifact(artifact, self.staging)
        self.assertTrue(arbitrary.exists())

        outside_source = self.make_source("outside.gcode", b"G28\n")
        outside = artifacts.stage_artifact(
            outside_source, self.test_root / "outside-stage",
        )
        with self.assertRaisesRegex(artifacts.ArtifactError, "outside private storage"):
            artifacts.discard_staged_artifact(outside, self.staging)
        self.assertTrue(outside.path.exists())

    def test_discard_rejects_symbolic_links_and_reparse_roots(self):
        source = self.make_source("job.gcode", b"G28\n")
        artifact = artifacts.stage_artifact(source, self.staging)
        staged_path = artifact.path
        with mock.patch.object(artifacts, "_is_reparse_point", return_value=True):
            with self.assertRaisesRegex(artifacts.ArtifactError, "staging root"):
                artifacts.discard_staged_artifact(artifact, self.staging)
        self.assertTrue(staged_path.exists())

        os.chmod(staged_path, stat.S_IWRITE | stat.S_IREAD)
        staged_path.unlink()
        target = self.make_source("link-target.gcode", b"G28\n")
        try:
            staged_path.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symbolic links are unavailable")
        with self.assertRaisesRegex(artifacts.ArtifactError, "regular file"):
            artifacts.discard_staged_artifact(artifact, self.staging)
        self.assertTrue(target.exists())

    def test_discard_requires_selected_plate_to_match_artifact(self):
        source = self.test_root / "model.gcode.3mf"
        make_3mf(source)
        first = artifacts.stage_artifact(source, self.staging)
        second = artifacts.stage_artifact(source, self.staging)
        plate = artifacts.stage_selected_plate(
            first, "Metadata/plate_1.gcode", self.staging,
        )
        with self.assertRaisesRegex(artifacts.ArtifactError, "does not belong"):
            artifacts.discard_staged_artifact(second, self.staging, selected_plate=plate)
        self.assertTrue(first.path.exists())
        self.assertTrue(second.path.exists())
        self.assertTrue(plate.path.exists())

    def test_discard_staged_plate_removes_only_extracted_plate(self):
        source = self.test_root / "model.gcode.3mf"
        make_3mf(source)
        artifact = artifacts.stage_artifact(source, self.staging)
        plate = artifacts.stage_selected_plate(
            artifact, "Metadata/plate_1.gcode", self.staging,
        )

        self.assertTrue(artifacts.discard_staged_plate(plate, self.staging))
        self.assertFalse(plate.path.exists())
        self.assertTrue(artifact.path.exists())
        self.assertTrue(source.exists())
        self.assertFalse(artifacts.discard_staged_plate(plate, self.staging))

    def test_discard_staged_plate_rejects_a_plate_from_another_directory(self):
        source = self.test_root / "model.gcode.3mf"
        make_3mf(source)
        self.staging.mkdir(parents=True)
        artifact = artifacts.stage_artifact(source, self.test_root / "other-stage")
        plate = artifacts.stage_selected_plate(
            artifact, "Metadata/plate_1.gcode", self.test_root / "other-stage",
        )

        with self.assertRaisesRegex(artifacts.ArtifactError, "outside private storage"):
            artifacts.discard_staged_plate(plate, self.staging)
        self.assertTrue(plate.path.exists())
        self.assertTrue(artifact.path.exists())

    def test_unsliced_project_3mf_is_rejected(self):
        source = self.test_root / "Assembly.3mf"
        make_3mf(source, plates=())
        with self.assertRaisesRegex(artifacts.ArtifactError, "unsliced"):
            artifacts.stage_artifact(source, self.staging)

    def test_unsafe_zip_paths_duplicates_symlinks_and_bombs_are_rejected(self):
        cases = [
            ((('../escape', b'x', None),), "unsafe archive path"),
            ((('Metadata/PLATE_1.GCODE', b'x', None),), "duplicate archive member"),
            ((('Metadata/link', b'target', (stat.S_IFLNK | 0o777) << 16),),
             "symbolic link"),
        ]
        for index, (entries, expected) in enumerate(cases):
            with self.subTest(expected=expected):
                source = self.test_root / f"unsafe-{index}.3mf"
                make_3mf(source, extra_entries=entries)
                with self.assertRaisesRegex(artifacts.ArtifactError, expected):
                    artifacts.stage_artifact(source, self.staging)
        bomb = self.test_root / "bomb.3mf"
        make_3mf(bomb, extra_entries=(("Metadata/payload", b"A" * 500_000, None),),
                 compression=zipfile.ZIP_DEFLATED)
        with self.assertRaisesRegex(artifacts.ArtifactError, "compression ratio"):
            artifacts.stage_artifact(bomb, self.staging)

    def test_zip_unpacked_size_and_member_bounds_are_enforced(self):
        source = self.test_root / "large-member.3mf"
        make_3mf(source, extra_entries=(("Metadata/payload", b"x" * 64, None),))
        with mock.patch.object(artifacts, "MAX_ZIP_MEMBER_BYTES", 32):
            with self.assertRaisesRegex(artifacts.ArtifactError, "member exceeds"):
                artifacts.stage_artifact(source, self.staging)

    def test_duplicate_json_parameter_keys_are_rejected(self):
        source = self.test_root / "bad-config.3mf"
        make_3mf(source, include_params=False,
                 extra_entries=(("Metadata/project_settings.config",
                                 b'{"layer_height":0.2,"layer_height":0.3}', None),))
        with self.assertRaisesRegex(artifacts.ArtifactError, "parameter metadata"):
            artifacts.stage_artifact(source, self.staging)

    def test_deeply_nested_parameter_json_is_rejected_with_bounded_error(self):
        source = self.test_root / "deep-config.gcode.3mf"
        raw = b'{"value":' + (b"[" * 33) + b"0" + (b"]" * 33) + b"}"
        make_3mf(source, project_settings=raw)
        with self.assertRaisesRegex(artifacts.ArtifactError, "nesting"):
            artifacts.stage_artifact(source, self.staging)

    def test_mutating_staged_copy_fails_digest_verification(self):
        result = artifacts.stage_artifact(self.make_source("job.gcode", b"G28\n"), self.staging)
        os.chmod(result.path, stat.S_IWRITE | stat.S_IREAD)
        result.path.write_bytes(b"G29\n")
        self.assertFalse(artifacts.verify_staged_artifact(result))

    def test_artifact_id_collision_never_overwrites_existing_stage(self):
        source = self.make_source("job.gcode", b"G28\n")
        artifact_id = "0d90e47b-85a2-4c95-a3fb-91489d8de853"
        first = artifacts.stage_artifact(source, self.staging, artifact_id=artifact_id)
        with self.assertRaisesRegex(artifacts.ArtifactError, "already staged"):
            artifacts.stage_artifact(source, self.staging, artifact_id=artifact_id)
        self.assertTrue(artifacts.verify_staged_artifact(first))

    def test_corrupt_member_crc_is_rejected_without_extraction(self):
        source = self.test_root / "bad-crc.3mf"
        make_3mf(source)
        with zipfile.ZipFile(source, "r") as archive:
            info = archive.getinfo("Metadata/plate_1.gcode")
            with source.open("r+b") as stream:
                stream.seek(info.header_offset + 26)
                header = stream.read(4)
                filename_length = int.from_bytes(header[:2], "little")
                extra_length = int.from_bytes(header[2:], "little")
                data_offset = info.header_offset + 30 + filename_length + extra_length
                stream.seek(data_offset)
                original = stream.read(1)
                stream.seek(data_offset)
                stream.write(bytes([original[0] ^ 1]))
        with self.assertRaises(artifacts.ArtifactError):
            artifacts.stage_artifact(source, self.staging)

    @unittest.skipUnless(REAL_UNSLICED_3MF.is_file(), "optional local fixture not present")
    def test_supplied_real_assembly_is_read_only_unsliced_fixture(self):
        # Inspect the source directly: no extraction or copy to its C: location.
        with self.assertRaisesRegex(artifacts.ArtifactError, "unsliced"):
            artifacts._inspect_3mf(REAL_UNSLICED_3MF, artifacts.DEFAULT_CHUNK_BYTES)


if __name__ == "__main__":
    unittest.main()
