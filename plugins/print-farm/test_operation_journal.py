import hashlib
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

import operation_journal as journal


HASH_A = hashlib.sha256(b"artifact A").hexdigest()
HASH_B = hashlib.sha256(b"artifact B").hexdigest()


class OperationJournalTests(unittest.TestCase):
    def setUp(self):
        Path(r"F:\orca-plugins\.worktmp").mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="operation-journal-", dir=r"F:\orca-plugins\.worktmp"))
        self.path = self.root / "account" / "operations.sqlite3"
        self.store = journal.OperationJournal(self.path, max_records=5)

    def tearDown(self):
        self.store.close()

    def intent(self, *, action="start", target="printer-1", digest=HASH_A,
               plate="Metadata/plate_2.gcode", artifact_name=None, op_id=None, binding=None):
        return self.store.prepare(
            account_id="account-1", target_id=target,
            target_binding=binding or {"backend": "moonraker", "target_uuid": target, "revision": 7},
            action=action, artifact_sha256=digest if action in ("start", "upload") else None,
            plate_entry=plate if action in ("start", "upload") else None, operation_id=op_id,
            artifact_name=artifact_name,
        )

    def test_persists_exact_intent_and_append_only_lifecycle(self):
        op = self.intent()
        self.assertEqual(op["state"], "planned")
        self.assertEqual(op["artifact_sha256"], HASH_A)
        self.assertEqual(op["plate_entry"], "Metadata/plate_2.gcode")
        self.assertEqual(op["target_binding"]["revision"], 7)
        self.assertEqual(self.store.begin_dispatch(op["operation_id"])["state"], "dispatching")
        self.store.report(op["operation_id"], "submitted")
        self.store.report(op["operation_id"], "acknowledged", evidence_ref="moonraker:job-42")
        self.store.close()
        self.store = journal.OperationJournal(self.path, max_records=5)
        reopened = self.store.get(op["operation_id"])
        self.assertEqual(reopened["state"], "acknowledged")
        self.assertEqual([event["state"] for event in self.store.events(op["operation_id"])],
                         ["planned", "dispatching", "submitted", "acknowledged"])

    def test_reused_id_cannot_change_digest_plate_target_or_action(self):
        op_id = str(uuid.uuid4())
        self.intent(op_id=op_id)
        with self.assertRaises(journal.ConflictError):
            self.intent(op_id=op_id, digest=HASH_B)
        with self.assertRaises(journal.ConflictError):
            self.intent(op_id=op_id, plate="Metadata/plate_3.gcode")
        with self.assertRaises(journal.ConflictError):
            self.intent(op_id=op_id, target="printer-2")
        with self.assertRaises(journal.ConflictError):
            self.intent(op_id=op_id, action="upload", plate=None)

    def test_artifact_name_is_sanitized_immutable_and_readable_for_reconciliation(self):
        op_id = str(uuid.uuid4())
        op = self.intent(op_id=op_id, artifact_name="Assembly_ABS.gcode")
        self.store.begin_dispatch(op_id)
        self.store.report(op_id, "submitted")
        self.assertEqual(self.store.get(op_id)["artifact_name"], "Assembly_ABS.gcode")
        self.assertEqual(self.store.list_operations("account-1")[0]["artifact_name"], "Assembly_ABS.gcode")
        with self.assertRaises(journal.ConflictError):
            self.intent(op_id=op_id, artifact_name="different.gcode")
        with self.assertRaises(ValueError):
            self.intent(artifact_name="folder\\Assembly.gcode")
        with self.assertRaises(ValueError):
            self.intent(artifact_name="".join(("bad", "\x00", "name.gcode")))

    def test_dispatch_claim_is_single_use_and_unknown_never_replays(self):
        op = self.intent()
        op_id = op["operation_id"]
        self.assertTrue(self.store.begin_dispatch(op_id))
        self.assertFalse(self.store.begin_dispatch(op_id))
        self.store.report(op_id, "unknown_outcome", actor="recovery")
        self.assertFalse(self.store.begin_dispatch(op_id))
        with self.assertRaises(journal.TransitionError):
            self.store.report(op_id, "dispatching")

    def test_recovery_requires_exclusive_owner_and_marks_inflight_unknown(self):
        op = self.intent()
        self.store.begin_dispatch(op["operation_id"])
        with self.assertRaises(PermissionError):
            self.store.recover_interrupted("account-1")
        self.assertEqual(self.store.recover_interrupted("account-1", exclusive_session_owner=True), 1)
        self.assertEqual(self.store.get(op["operation_id"])["state"], "unknown_outcome")

    def test_recovery_cancels_planned_operation_that_was_never_dispatched(self):
        planned = self.intent(target="printer-planned")
        flying = self.intent(target="printer-flying")
        self.store.begin_dispatch(flying["operation_id"])
        self.assertEqual(self.store.recover_interrupted("account-1", exclusive_session_owner=True), 2)
        self.assertEqual(self.store.get(planned["operation_id"])["state"], "cancelled_before_dispatch")
        self.assertEqual(self.store.get(flying["operation_id"])["state"], "unknown_outcome")
        # The target is free again for a new prepare.
        self.assertEqual(self.intent(target="printer-planned")["state"], "planned")

    def test_reconciliation_records_observation_and_does_not_dispatch(self):
        op = self.intent()
        op_id = op["operation_id"]
        self.store.begin_dispatch(op_id)
        self.store.report(op_id, "unknown_outcome", actor="recovery")
        result = self.store.reconcile(op_id, "verified", evidence_ref="device:file-sha256")
        self.assertEqual(result["state"], "verified")
        self.assertEqual([e["state"] for e in self.store.events(op_id)][-1], "verified")
        self.assertFalse(self.store.begin_dispatch(op_id))

    def test_manual_resolution_releases_target_without_rewriting_unknown_history(self):
        op = self.intent()
        self.store.begin_dispatch(op["operation_id"])
        self.store.report(op["operation_id"], "unknown_outcome", actor="recovery")
        self.store.resolve_unknown_manually(op["operation_id"], reason="printer checked, job absent", actor="operator")
        events = self.store.events(op["operation_id"])
        self.assertEqual([event["state"] for event in events][-2:], ["unknown_outcome", "manually_resolved"])
        self.assertEqual(events[-1]["note"], "printer checked, job absent")
        followup = self.intent()
        self.assertEqual(followup["state"], "planned")
        self.assertNotEqual(followup["operation_id"], op["operation_id"])

    def test_submitted_request_can_be_released_without_claiming_print_success(self):
        op = self.intent()
        self.store.begin_dispatch(op["operation_id"])
        self.store.report(op["operation_id"], "submitted")
        with self.assertRaises(journal.ConflictError):
            self.intent()
        resolved = self.store.resolve_unknown_manually(
            op["operation_id"], reason="checked target and cleared bed", actor="operator"
        )
        self.assertEqual(resolved["state"], "manually_resolved")
        self.assertEqual([event["state"] for event in self.store.events(op["operation_id"])][-2:],
                         ["submitted", "manually_resolved"])
        self.assertEqual(self.intent()["state"], "planned")

    def test_actively_dispatching_request_cannot_be_released_manually(self):
        op = self.intent()
        self.store.begin_dispatch(op["operation_id"])
        with self.assertRaises(journal.TransitionError):
            self.store.resolve_unknown_manually(
                op["operation_id"], reason="not checked", actor="operator"
            )

    def test_another_unresolved_start_for_target_is_blocked_but_other_target_is_independent(self):
        self.intent()
        with self.assertRaises(journal.ConflictError):
            self.intent()
        other = self.intent(target="printer-2")
        self.assertEqual(other["target_id"], "printer-2")

    def test_unresolved_list_keeps_old_holds_visible_after_newer_history(self):
        held = self.intent(target="held")
        self.store.begin_dispatch(held["operation_id"])
        self.store.report(held["operation_id"], "submitted")
        for target in ("done-1", "done-2"):
            op = self.intent(target=target)
            self.store.begin_dispatch(op["operation_id"])
            self.store.report(op["operation_id"], "unknown_outcome")
            self.store.resolve_unknown_manually(op["operation_id"], reason="checked", actor="operator")
        self.assertEqual([row["operation_id"] for row in self.store.list_unresolved("account-1")],
                         [held["operation_id"]])

    def test_unknown_upload_blocks_duplicate_artifact_but_not_other_plate_or_controls(self):
        first = self.intent(action="upload", target="upload-printer")
        self.store.begin_dispatch(first["operation_id"])
        self.store.report(first["operation_id"], "unknown_outcome", actor="recovery")
        with self.assertRaises(journal.ConflictError):
            self.intent(action="upload", target="upload-printer")

        no_plate = self.intent(action="upload", target="gcode-printer", plate=None)
        self.store.begin_dispatch(no_plate["operation_id"])
        self.store.report(no_plate["operation_id"], "unknown_outcome", actor="recovery")
        with self.assertRaises(journal.ConflictError):
            self.intent(action="upload", target="gcode-printer", plate=None)

        other_plate = self.intent(action="upload", target="upload-printer", plate="Metadata/plate_3.gcode")
        other_digest = self.intent(action="upload", target="upload-printer", digest=HASH_B)
        control = self.intent(action="pause", target="upload-printer", plate=None)
        self.assertEqual(other_plate["plate_entry"], "Metadata/plate_3.gcode")
        self.assertEqual(other_digest["artifact_sha256"], HASH_B)
        self.assertEqual(control["action"], "pause")

    def test_v2_journal_migrates_artifact_name(self):
        self.store.close()
        with sqlite3.connect(str(self.path)) as db:
            db.execute("ALTER TABLE operations DROP COLUMN artifact_name")
            db.execute("PRAGMA user_version=2")
        self.store = journal.OperationJournal(self.path, max_records=5)
        with sqlite3.connect(str(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 3)
            indexes = {row[1] for row in db.execute("PRAGMA index_list(operations)")}
            columns = {row[1] for row in db.execute("PRAGMA table_info(operations)")}
        self.assertIn("one_unresolved_upload_per_artifact", indexes)
        self.assertIn("artifact_name", columns)

    def test_control_intents_need_no_artifact_and_can_be_recorded(self):
        op = self.intent(action="pause", plate=None)
        self.assertIsNone(op["artifact_sha256"])
        self.assertIsNone(op["plate_entry"])
        self.assertEqual(op["action"], "pause")

    def test_gcode_start_may_have_no_plate_entry(self):
        op = self.intent(plate=None)
        self.assertEqual(op["action"], "start")
        self.assertIsNone(op["plate_entry"])

    def test_missing_hash_bad_plate_uuid_and_credentials_fail_closed(self):
        with self.assertRaises(ValueError):
            self.store.prepare(account_id="a", target_id="p", target_binding={"uuid": "p"}, action="start")
        with self.assertRaises(ValueError):
            self.intent(plate="../private.gcode")
        with self.assertRaises(ValueError):
            self.intent(op_id="not-a-uuid")
        with self.assertRaises(ValueError):
            self.intent(binding={"target_uuid": "printer-1", "access_code": "do-not-store"})
        with self.assertRaises(ValueError):
            self.intent(binding={"target_uuid": "printer-1", "api_token": "do-not-store"})

    def test_evidence_must_be_opaque_and_required_for_success_claims(self):
        op = self.intent(action="upload", plate=None)
        self.store.begin_dispatch(op["operation_id"])
        with self.assertRaises(ValueError):
            self.store.report(op["operation_id"], "verified")
        with self.assertRaises(ValueError):
            self.store.report(op["operation_id"], "verified", evidence_ref="https://host/?token=secret")
        self.store.report(op["operation_id"], "verified", evidence_ref="remote:file-7")

    def test_record_bound_prunes_only_terminal_history_and_fails_when_full_of_unresolved(self):
        for _ in range(5):
            op = self.intent(action="pause", plate=None)
            self.store.report(op["operation_id"], "cancelled_before_dispatch")
        self.assertEqual(len(self.store.list_operations("account-1", limit=5)), 5)
        op = self.intent(action="pause", plate=None)
        self.assertEqual(len(self.store.list_operations("account-1", limit=5)), 5)
        self.assertIn(op["operation_id"], {item["operation_id"] for item in self.store.list_operations("account-1")})
        for index in range(4):
            self.intent(action="pause", target="active-%d" % index, plate=None)
        with self.assertRaises(journal.JournalError):
            self.intent(action="pause", target="active-overflow", plate=None)

    def test_unrecognized_database_is_preserved_and_rejected(self):
        self.store.close()
        with sqlite3.connect(str(self.path)) as db:
            db.execute("CREATE TABLE unrelated(value TEXT)")
            db.execute("INSERT INTO unrelated VALUES('keep')")
        with self.assertRaises(journal.JournalCorruptError):
            journal.OperationJournal(self.path)
        with sqlite3.connect(str(self.path)) as db:
            self.assertEqual(db.execute("SELECT value FROM unrelated").fetchone()[0], "keep")


if __name__ == "__main__":
    unittest.main()
