import threading
import unittest
from unittest import mock

import action_scheduler


class ActionSchedulerTests(unittest.TestCase):
    def test_first_worker_permission_denial_leaves_no_scheduler(self):
        with mock.patch.object(threading.Thread, "start", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                action_scheduler.ActionScheduler(max_workers=2)

    def test_later_worker_denial_stops_started_workers(self):
        original_start = threading.Thread.start
        calls = 0
        started = []

        def start(worker):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise PermissionError("denied")
            original_start(worker)
            started.append(worker)

        with mock.patch.object(threading.Thread, "start", start):
            with self.assertRaises(PermissionError):
                action_scheduler.ActionScheduler(max_workers=3)
        self.assertEqual(len(started), 1)
        self.assertFalse(started[0].is_alive())

    def setUp(self):
        self.schedulers = []

    def tearDown(self):
        for scheduler in self.schedulers:
            scheduler.shutdown(wait=True, cancel_pending=True, timeout=2)

    def make_scheduler(self, **kwargs):
        scheduler = action_scheduler.ActionScheduler(**kwargs)
        self.schedulers.append(scheduler)
        return scheduler

    def test_target_is_serial_and_keeps_enqueue_order(self):
        scheduler = self.make_scheduler(max_workers=4)
        gate = threading.Event()
        started = threading.Event()
        order = []

        def first():
            started.set()
            gate.wait(2)
            order.append("first")

        scheduler.submit("printer-a", "a1", first)
        self.assertTrue(started.wait(1))
        scheduler.submit("printer-a", "a2", lambda: order.append("second"))
        scheduler.submit("printer-a", "a3", lambda: order.append("third"))
        gate.set()
        self.assertTrue(scheduler.wait_for_idle(2))
        self.assertEqual(order, ["first", "second", "third"])

    def test_control_on_other_target_runs_while_upload_is_blocked(self):
        scheduler = self.make_scheduler(max_workers=2)
        upload_entered = threading.Event()
        release_upload = threading.Event()
        cancelled = threading.Event()

        def upload():
            upload_entered.set()
            release_upload.wait(2)

        scheduler.submit("printer-a", "upload-a", upload, kind="upload")
        self.assertTrue(upload_entered.wait(1))
        scheduler.submit("printer-b", "cancel-b", cancelled.set, kind="cancel")
        self.assertTrue(cancelled.wait(1), "control action was blocked by another printer's upload")
        release_upload.set()
        self.assertTrue(scheduler.wait_for_idle(2))

    def test_bounded_queue_reports_overflow_without_running_rejected_action(self):
        scheduler = self.make_scheduler(max_workers=1, max_pending=1)
        entered = threading.Event()
        release = threading.Event()
        ran = []

        def blocker():
            entered.set()
            release.wait(2)

        scheduler.submit("printer-a", "active", blocker)
        self.assertTrue(entered.wait(1))
        self.assertEqual(scheduler.submit("printer-b", "queued", lambda: ran.append("queued"))["status"],
                         scheduler.ACCEPTED)
        result = scheduler.submit("printer-c", "overflow", lambda: ran.append("overflow"))
        self.assertEqual(result["status"], scheduler.FULL)
        self.assertNotIn("overflow", [item["action_id"] for item in scheduler.snapshot()["actions"]])
        release.set()
        self.assertTrue(scheduler.wait_for_idle(2))
        self.assertEqual(ran, ["queued"])

    def test_action_id_is_single_dispatch_even_after_completion(self):
        scheduler = self.make_scheduler(max_workers=1)
        calls = []
        self.assertEqual(scheduler.submit("printer-a", "once", lambda: calls.append(1))["status"],
                         scheduler.ACCEPTED)
        self.assertTrue(scheduler.wait_for_idle(2))
        duplicate = scheduler.submit("printer-a", "once", lambda: calls.append(2))
        self.assertEqual(duplicate["status"], scheduler.DUPLICATE)
        self.assertEqual(calls, [1])

    def test_control_priority_does_not_starve_ordinary_work(self):
        scheduler = self.make_scheduler(max_workers=1)
        gate = threading.Event()
        blocker_started = threading.Event()
        order = []

        def blocker():
            blocker_started.set()
            gate.wait(2)

        scheduler.submit("busy", "blocker", blocker)
        self.assertTrue(blocker_started.wait(1))
        scheduler.submit("ordinary", "ordinary", lambda: order.append("ordinary"), kind="upload")
        for index in range(6):
            scheduler.submit("control-{}".format(index), "control-{}".format(index),
                             lambda index=index: order.append("control-{}".format(index)), kind="cancel")
        gate.set()
        self.assertTrue(scheduler.wait_for_idle(2))
        self.assertEqual(order[0:4], ["control-0", "control-1", "control-2", "control-3"])
        self.assertEqual(order[4], "ordinary")
        self.assertEqual(len(order), 7)

    def test_shutdown_cancels_queued_work_and_rejects_new_work(self):
        scheduler = self.make_scheduler(max_workers=1)
        entered = threading.Event()
        release = threading.Event()
        ran = []

        def active():
            entered.set()
            release.wait(2)

        scheduler.submit("printer-a", "active", active)
        self.assertTrue(entered.wait(1))
        scheduler.submit("printer-b", "pending", lambda: ran.append("pending"))
        self.assertFalse(scheduler.shutdown(wait=True, cancel_pending=True, timeout=0.02))
        self.assertEqual(scheduler.get("pending")["status"], "cancelled_before_dispatch")
        self.assertEqual(scheduler.submit("printer-c", "late", lambda: ran.append("late"))["status"],
                         scheduler.STOPPING)
        release.set()
        self.assertTrue(scheduler.shutdown(wait=True, timeout=2))
        self.assertEqual(ran, [])


if __name__ == "__main__":
    unittest.main()
