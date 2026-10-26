from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

import artifact_transfer as transfers


WORK_TEMP = Path(r"F:\orca-plugins\.worktmp")


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


class ArtifactTransferTests(unittest.TestCase):
    def setUp(self):
        WORK_TEMP.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="artifact-transfer-", dir=WORK_TEMP))
        self.clock = Clock()
        self.manager = transfers.ArtifactTransferManager(
            self.root / "approved", max_artifact_bytes=200_000,
            storage_quota_bytes=300_000, idle_timeout_seconds=10, clock=self.clock,
        )
        self.tokens = []

    def tearDown(self):
        for owner, token in reversed(self.tokens):
            try:
                self.manager.cancel(owner, token)
            except transfers.ArtifactTransferError:
                pass
        # These tests own only their unique temporary directory.
        for path in sorted(self.root.rglob("*"), reverse=True):
            if path.is_file() or path.is_symlink():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        self.root.rmdir()

    def begin(self, payload, filename="job.gcode", *, digest=True):
        token = self.manager.begin(
            "window-1", filename, len(payload),
            hashlib.sha256(payload).hexdigest() if digest else None,
        )
        self.tokens.append(("window-1", token))
        return token

    def begin_raw(self, owner, filename, total_bytes, sha256=None):
        token = self.manager.begin(owner, filename, total_bytes, sha256)
        self.tokens.append((owner, token))
        return token

    def test_receives_ordered_chunks_and_uses_generated_path(self):
        payload = b"G28\n" * 20_000
        token = self.begin(payload, "job.gcode")
        self.assertTrue(self.manager.storage_dir.name == "approved")
        parts = [payload[i:i + transfers.MAX_CHUNK_BYTES]
                 for i in range(0, len(payload), transfers.MAX_CHUNK_BYTES)]
        for sequence, part in enumerate(parts):
            offset = sum(len(previous) for previous in parts[:sequence])
            self.assertEqual(
                self.manager.append("window-1", token, sequence, part,
                                    hashlib.sha256(part).hexdigest()),
                offset + len(part),
            )
        result = self.manager.finish("window-1", token)
        self.assertEqual(result.filename, "job.gcode")
        self.assertEqual(result.size_bytes, len(payload))
        self.assertEqual(result.sha256, hashlib.sha256(payload).hexdigest())
        self.assertEqual(result.path.read_bytes(), payload)
        self.assertTrue(result.path.parent.samefile(self.manager.storage_dir))
        self.assertFalse(self.manager.active_owners())

    def test_server_digest_is_authoritative_when_webview_hash_is_unavailable(self):
        payload = b"small content"
        token = self.begin(payload, digest=False)
        self.manager.append("window-1", token, 0, payload)
        result = self.manager.finish("window-1", token)
        self.assertEqual(result.sha256, hashlib.sha256(payload).hexdigest())

    def test_optional_chunk_digest_is_verified_when_present(self):
        payload = b"content"
        token = self.begin(payload)
        with self.assertRaisesRegex(transfers.ArtifactTransferError, "chunk SHA-256"):
            self.manager.append("window-1", token, 0, payload, "0" * 64)
        self.manager.append("window-1", token, 0, payload,
                            hashlib.sha256(payload).hexdigest())
        self.assertEqual(self.manager.finish("window-1", token).size_bytes, len(payload))

    def test_owner_token_and_sequence_are_bound_to_transfer(self):
        token = self.begin(b"abc")
        with self.assertRaisesRegex(transfers.ArtifactTransferError, "unavailable"):
            self.manager.append("another-window", token, 0, b"abc")
        with self.assertRaisesRegex(transfers.ArtifactTransferError, "sequence"):
            self.manager.append("window-1", token, 1, b"abc")
        self.manager.append("window-1", token, 0, b"abc")
        self.manager.finish("window-1", token)

    def test_one_active_session_per_owner_and_other_owners_are_independent(self):
        self.begin(b"one")
        with self.assertRaisesRegex(transfers.ArtifactTransferError, "already has"):
            self.begin_raw("window-1", "two.gcode", 3)
        token = self.begin_raw("window-2", "two.gcode", 3)
        self.manager.cancel("window-2", token)

    def test_chunk_bounds_declared_length_and_incomplete_finish(self):
        token = self.begin(b"abcd")
        with self.assertRaisesRegex(transfers.ArtifactTransferError, "1..32 KiB"):
            self.manager.append("window-1", token, 0, b"x" * (transfers.MAX_CHUNK_BYTES + 1))
        with self.assertRaisesRegex(transfers.ArtifactTransferError, "incomplete"):
            self.manager.finish("window-1", token)
        with self.assertRaisesRegex(transfers.ArtifactTransferError, "declared size"):
            self.manager.append("window-1", token, 0, b"abcde")
        self.assertEqual(self.manager.active_owners(), ("window-1",))

    def test_final_digest_mismatch_discards_partial_file(self):
        token = self.begin_raw("window-1", "job.gcode", 3, "0" * 64)
        self.manager.append("window-1", token, 0, b"abc")
        with self.assertRaisesRegex(transfers.ArtifactTransferError, "SHA-256 mismatch"):
            self.manager.finish("window-1", token)
        self.assertEqual(self.manager.active_owners(), ())
        self.assertEqual(list(self.manager.storage_dir.iterdir()), [])

    def test_cancel_and_timeout_remove_incomplete_data(self):
        token = self.begin(b"first")
        self.manager.append("window-1", token, 0, b"fi")
        partial = list(self.manager.storage_dir.iterdir())[0]
        self.manager.cancel("window-1", token)
        self.assertFalse(partial.exists())

        token = self.begin(b"second")
        self.manager.append("window-1", token, 0, b"se")
        partial = list(self.manager.storage_dir.iterdir())[0]
        self.clock.value = 10
        self.assertEqual(self.manager.expire(), 1)
        self.assertFalse(partial.exists())

    def test_begin_validates_filename_size_digest_and_storage_quota(self):
        for filename in ("../job.gcode", "C:\\..\\job.gcode", "job.txt"):
            with self.subTest(filename=filename), self.assertRaises(
                    transfers.ArtifactTransferError):
                self.manager.begin("window-x", filename, 3)
        with self.assertRaisesRegex(transfers.ArtifactTransferError, "size"):
            self.manager.begin("window-x", "big.gcode", 200_001)
        with self.assertRaisesRegex(transfers.ArtifactTransferError, "SHA-256"):
            self.manager.begin("window-x", "job.gcode", 3, "bad")
        self.begin_raw("window-1", "reserved.gcode", 200_000)
        with self.assertRaisesRegex(transfers.ArtifactTransferError, "quota"):
            self.manager.begin("window-2", "other.gcode", 100_001)


if __name__ == "__main__":
    unittest.main()
