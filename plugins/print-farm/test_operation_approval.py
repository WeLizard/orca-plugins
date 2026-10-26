import hashlib
import threading
import unittest

import operation_approval as approvals


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def plan(target="scope-a:printer-1:selector-a", revision=4, artifact="model-a",
         plate="Metadata/plate_1.gcode", use_ams=True, metadata="settings-a"):
    return {
        "target_identity": target,
        "binding_revision": revision,
        "artifact_sha256": digest(artifact),
        "plate_entry": plate,
        "action": "start",
        "use_ams": use_ams,
        "metadata_digest": digest(metadata),
    }


class OperationApprovalTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.store = approvals.OperationApprovalStore(clock=lambda: self.now)

    def test_approval_binds_exact_plan_and_is_consumed_once(self):
        exact = plan()
        nonce, expected_digest = self.store.create(exact, ttl=20, epoch="session-a")
        self.assertEqual(expected_digest, approvals._validate_plan(exact))
        self.assertTrue(self.store.consume(nonce, dict(exact), "session-a"))
        self.assertFalse(self.store.consume(nonce, exact, "session-a"))

    def test_each_plan_field_is_bound_including_target_revision_and_metadata(self):
        cases = (
            {"target_identity": "scope-a:printer-2:selector-a"},
            {"binding_revision": 5},
            {"artifact_sha256": digest("different artifact")},
            {"plate_entry": "Metadata/plate_2.gcode"},
            {"action": "upload"},
            {"use_ams": False},
            {"metadata_digest": digest("different settings/material/tool")},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                exact = plan()
                nonce, _ = self.store.create(exact, 20, "session-a")
                tampered = dict(exact)
                tampered.update(changes)
                self.assertFalse(self.store.consume(nonce, tampered, "session-a"))
                self.assertFalse(self.store.consume(nonce, exact, "session-a"))

    def test_mismatched_epoch_or_expiry_rejects_and_consumes_nonce(self):
        exact = plan()
        nonce, _ = self.store.create(exact, 1, "session-a")
        self.assertFalse(self.store.consume(nonce, exact, "session-b"))
        second, _ = self.store.create(exact, 1, "session-a")
        self.now += 1
        self.assertFalse(self.store.consume(second, exact, "session-a"))
        self.assertEqual(len(self.store), 0)

    def test_batch_plans_are_separate_and_cannot_be_swapped_between_targets(self):
        first = plan(target="scope:printer-1:selector-1", metadata="target-one")
        second = plan(target="scope:printer-2:selector-2", metadata="target-two")
        first_nonce, _ = self.store.create(first, 30, "batch-epoch")
        second_nonce, _ = self.store.create(second, 30, "batch-epoch")
        wrong_target_nonce, _ = self.store.create(first, 30, "batch-epoch")
        self.assertFalse(self.store.consume(wrong_target_nonce, second, "batch-epoch"))
        self.assertTrue(self.store.consume(first_nonce, first, "batch-epoch"))
        self.assertTrue(self.store.consume(second_nonce, second, "batch-epoch"))

    def test_invalid_or_overlong_plans_and_ttls_fail_closed(self):
        exact = plan()
        for changes in (
            {"name": "friendly but not an identity"},
            {"binding_revision": True},
            {"artifact_sha256": "not-a-digest"},
            {"action": "pause"},
            {"use_ams": 1},
            {"unexpected": "field"},
        ):
            invalid = dict(exact)
            invalid.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.store.create(invalid, 10, "session")
        for ttl in (0, -1, float("inf"), approvals.MAX_TTL_SECONDS + 1, True):
            with self.subTest(ttl=ttl), self.assertRaises(ValueError):
                self.store.create(exact, ttl, "session")

    def test_capacity_does_not_silently_evict_live_approvals(self):
        store = approvals.OperationApprovalStore(max_entries=1, clock=lambda: self.now)
        plan_a = plan()
        nonce, _ = store.create(plan_a, 10, "epoch")
        with self.assertRaises(RuntimeError):
            store.create(plan(target="other"), 10, "epoch")
        self.assertTrue(store.consume(nonce, plan_a, "epoch"))
        replacement, _ = store.create(plan(target="other"), 10, "epoch")
        self.assertTrue(store.consume(replacement, plan(target="other"), "epoch"))

    def test_concurrent_consumers_can_only_win_once(self):
        exact = plan()
        nonce, _ = self.store.create(exact, 10, "epoch")
        barrier = threading.Barrier(3)
        results = []

        def consume():
            barrier.wait()
            results.append(self.store.consume(nonce, exact, "epoch"))

        threads = [threading.Thread(target=consume) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=2)
        self.assertEqual(sorted(results), [False, True])
        self.assertTrue(all(not thread.is_alive() for thread in threads))


if __name__ == "__main__":
    unittest.main()
