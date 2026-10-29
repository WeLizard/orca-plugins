"""Bounded, per-target serial action scheduler for print-farm operations.

The scheduler keeps each target's actions in FIFO order while allowing actions
for other targets to run concurrently. Control actions are selected ahead of
ordinary work on different idle targets, so a long upload cannot hold up a
cancel or pause for another printer.
"""

import threading
import time
from collections import deque


class ActionScheduler:
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    FULL = "queue_full"
    STOPPING = "shutting_down"

    CONTROL_KINDS = frozenset(("cancel", "pause", "resume", "stop"))
    CONTROL_BURST_LIMIT = 4

    def __init__(self, max_pending=64, max_workers=4, name="print-farm-action"):
        if not isinstance(max_pending, int) or max_pending < 1:
            raise ValueError("max_pending must be a positive integer")
        if not isinstance(max_workers, int) or max_workers < 1:
            raise ValueError("max_workers must be a positive integer")

        self.max_pending = max_pending
        self.max_workers = max_workers
        self._condition = threading.Condition()
        self._queues = {}
        self._busy_targets = set()
        self._records = {}
        self._seen_ids = set()
        self._pending = 0
        self._control_streak = 0
        self._stopping = False
        self._cancel_pending = False
        self._workers = []
        try:
            for index in range(max_workers):
                worker = threading.Thread(
                    target=self._run,
                    name="{}-{}".format(name, index + 1),
                    daemon=True,
                )
                worker.start()
                self._workers.append(worker)
        except (PermissionError, RuntimeError):
            self.shutdown(wait=True, cancel_pending=True)
            raise

    def submit(self, target_id, action_id, callback, kind="upload"):
        """Queue one action and return a result with an explicit status.

        action_id must be stable and unique for the logical operation. An ID is
        remembered for this scheduler's lifetime even after terminal completion
        so a stale caller cannot cause that operation to be dispatched twice.
        """
        if not target_id or not action_id or not callable(callback):
            raise ValueError("target_id, action_id, and callable callback are required")
        if not isinstance(kind, str) or not kind:
            raise ValueError("kind must be a non-empty string")

        with self._condition:
            if self._stopping:
                return {"status": self.STOPPING, "action_id": action_id}
            if action_id in self._seen_ids:
                return {"status": self.DUPLICATE, "action_id": action_id}
            if self._pending >= self.max_pending:
                return {"status": self.FULL, "action_id": action_id}

            record = {
                "action_id": action_id,
                "target_id": target_id,
                "kind": kind,
                "status": "queued",
                "sequence": len(self._records),
                "submitted_at": time.monotonic(),
                "started_at": None,
                "finished_at": None,
                "error": None,
                "result": None,
                "callback": callback,
            }
            self._seen_ids.add(action_id)
            self._records[action_id] = record
            self._queues.setdefault(target_id, deque()).append(action_id)
            self._pending += 1
            self._condition.notify_all()
            return {"status": self.ACCEPTED, "action_id": action_id}

    def get(self, action_id):
        """Return a snapshot without exposing the callback."""
        with self._condition:
            record = self._records.get(action_id)
            return self._snapshot(record) if record else None

    def snapshot(self):
        """Return scheduler lifecycle and action states."""
        with self._condition:
            return {
                "stopping": self._stopping,
                "pending": self._pending,
                "running": len(self._busy_targets),
                "actions": [self._snapshot(item) for item in self._records.values()],
            }

    def wait_for_idle(self, timeout=None):
        """Wait until no queued or executing callback remains."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while self._pending or self._busy_targets:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def shutdown(self, wait=True, cancel_pending=False, timeout=None):
        """Stop accepting work and drain or cancel queued work.

        Running callbacks are not interrupted. With a timeout, False means a
        callback is still executing; the workers finish in the background and
        exit after it returns. Calling shutdown again is safe.
        """
        with self._condition:
            self._stopping = True
            self._cancel_pending = self._cancel_pending or bool(cancel_pending)
            if self._cancel_pending:
                now = time.monotonic()
                for queue in self._queues.values():
                    while queue:
                        action_id = queue.popleft()
                        record = self._records[action_id]
                        record["status"] = "cancelled_before_dispatch"
                        record["finished_at"] = now
                        record["callback"] = None
                        self._pending -= 1
                self._queues.clear()
            self._condition.notify_all()

        if not wait:
            return not self._has_live_workers()
        deadline = None if timeout is None else time.monotonic() + timeout
        for worker in self._workers:
            remaining = None if deadline is None else max(0, deadline - time.monotonic())
            worker.join(remaining)
        return not self._has_live_workers()

    def _has_live_workers(self):
        return any(worker.is_alive() for worker in self._workers)

    def _run(self):
        while True:
            with self._condition:
                action_id = self._next_action_locked()
                while action_id is None:
                    if self._stopping and self._pending == 0 and not self._busy_targets:
                        self._condition.notify_all()
                        return
                    self._condition.wait()
                    action_id = self._next_action_locked()
                record = self._records[action_id]
                target_id = record["target_id"]
                self._busy_targets.add(target_id)
                self._pending -= 1
                record["status"] = "running"
                record["started_at"] = time.monotonic()
                callback = record["callback"]

            result = None
            error = None
            try:
                result = callback()
            except Exception as exc:
                error = "{}: {}".format(type(exc).__name__, exc)

            with self._condition:
                record["result"] = result
                record["error"] = error
                record["status"] = "failed" if error is not None else "completed"
                record["finished_at"] = time.monotonic()
                record["callback"] = None
                self._busy_targets.remove(target_id)
                self._condition.notify_all()

    def _next_action_locked(self):
        candidates = []
        for target_id, queue in self._queues.items():
            if target_id in self._busy_targets or not queue:
                continue
            action_id = queue[0]
            record = self._records[action_id]
            priority = 0 if record["kind"] in self.CONTROL_KINDS else 1
            candidates.append((priority, record["sequence"], target_id, queue))
        if not candidates:
            return None
        ordinary_waiting = any(item[0] for item in candidates)
        if ordinary_waiting and self._control_streak >= self.CONTROL_BURST_LIMIT:
            candidates = [item for item in candidates if item[0] == 1]
        candidates.sort(key=lambda item: (item[0], item[1]))
        _, _, target_id, queue = candidates[0]
        action_id = queue.popleft()
        if self._records[action_id]["kind"] in self.CONTROL_KINDS:
            self._control_streak += 1
        else:
            self._control_streak = 0
        if not queue:
            del self._queues[target_id]
        return action_id

    @staticmethod
    def _snapshot(record):
        return {key: value for key, value in record.items() if key != "callback"}
