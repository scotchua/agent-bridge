"""Boundary and observable-result checks for the offline local queue."""

from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.localq.spool import (FakeBackend, LocalQueue, QueueCaps,
                                       ResourceSnapshot, SubprocessBackend,
                                       _child_failure_suffix, _fixed_child_detail)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Sampler:
    def __init__(self, clock):
        self.clock = clock
        self.snapshot = self.fresh()

    def fresh(self, **changes):
        values = dict(observed_at=self.clock(), memory_pressure="normal",
                      thermal_state="normal", ac_power=True, idle_seconds=120.0)
        values.update(changes)
        return ResourceSnapshot(**values)

    def sample(self):
        return self.snapshot


class ChildDiagnosticMutationTests(unittest.TestCase):
    def test_fixed_child_detail_rejects_document_index_sixteen(self):
        self.assertFalse(_fixed_child_detail("GemmaRefusal", "extract_document_16_invalid"))

    def test_child_failure_suffix_keeps_recognized_detail_at_text_limit(self):
        detail = ("extract_document_10_failed:" * 3
                  + "extract_document_1_failed:" * 2
                  + "classify_failed:worker state must be an existing absolute directory")
        self.assertEqual(len(detail), 200)
        output = json.dumps({"ok": False, "error": "GemmaRefusal", "error_detail": detail})
        self.assertEqual(_child_failure_suffix(output), ": GemmaRefusal: " + detail)


class SubprocessBackendMutationTests(unittest.TestCase):
    def test_constructor_rejects_empty_command(self):
        with self.assertRaisesRegex(ValueError, "non-empty argv"):
            SubprocessBackend([])

    def test_cancel_reports_that_a_live_child_was_signalled(self):
        backend = SubprocessBackend([sys.executable])
        process = mock.Mock()
        process.poll.return_value = None
        backend._processes["invented-job"] = process
        with mock.patch.object(backend, "_terminate_group") as terminate:
            self.assertTrue(backend.cancel("invented-job"))
        terminate.assert_called_once_with(process, signal.SIGTERM)


class LocalQueueMutationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clock = Clock()
        self.sampler = Sampler(self.clock)
        self.backend = FakeBackend(lambda payload: {"draft": payload["input"]})
        self.queue = self.make_queue()

    def make_queue(self, *, caps=None):
        return LocalQueue(self.temp.name, sampler=self.sampler, backend=self.backend,
                          caps=caps or QueueCaps(bulk_ttl_seconds=5, lease_seconds=10),
                          clock=self.clock)

    def submit(self, **changes):
        values = dict(task_type="summarize", input="Invented Copper Kite LLC note",
                      params={}, priority="interactive", classification="synthetic",
                      caller="codex", purpose="test")
        values.update(changes)
        return self.queue.submit(**values)

    def test_failure_text_hides_unrecognized_backend_exception(self):
        class InventedBackendFault(Exception):
            pass

        def fail(_payload):
            raise InventedBackendFault("invented account text")

        self.queue.backend = FakeBackend(fail)
        self.submit()
        result = self.queue.run_once("runner")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "backend_failure")

    def test_private_admit_accepts_sample_exactly_one_second_ahead(self):
        self.sampler.snapshot = self.sampler.fresh(observed_at=self.clock() + 1)
        self.assertIs(self.queue._admit("interactive"), self.sampler.snapshot)

    def test_private_admit_accepts_measured_idle_at_exact_threshold(self):
        self.sampler.snapshot = self.sampler.fresh(cpu_load_ratio=2.0,
                                                   cpu_idle_ratio=self.queue.caps.min_cpu_idle_ratio)
        self.assertIs(self.queue._admit("interactive"), self.sampler.snapshot)

    def test_admit_returns_the_validated_resource_snapshot(self):
        self.assertIs(self.queue.admit("interactive"), self.sampler.snapshot)

    def test_submit_accepts_payload_exactly_at_configured_byte_cap(self):
        values = dict(task_type="summarize", input="Invented Copper Kite LLC note",
                      params={}, priority="interactive", classification="synthetic",
                      caller="codex", purpose="test")
        cap = len(self.queue._normalized(values).encode("utf-8"))
        self.queue.caps = QueueCaps(max_input_bytes=cap)
        self.assertEqual(self.queue.submit(**values)["status"], "queued")

    def test_hold_dependency_blocks_then_releases_queued_work(self):
        job = self.submit()
        self.queue.hold_dependency("invented prerequisite")
        self.assertEqual(self.queue.status(job["job_id"])["error"],
                         "waiting_on_dependency:invented prerequisite")
        self.assertIsNone(self.queue.run_once("runner"))
        self.queue.hold_dependency(None)
        self.assertIsNone(self.queue.status(job["job_id"])["error"])
        self.assertEqual(self.queue.run_once("runner")["status"], "complete")

    def test_recover_locked_counts_only_lease_that_has_passed(self):
        expired = self.submit(input="Invented expired lease")
        current = self.submit(input="Invented current lease")
        with self.queue._db() as db:
            db.execute("UPDATE jobs SET status='running',lease_until=? WHERE job_id=?",
                       (self.clock() - 1, expired["job_id"]))
            db.execute("UPDATE jobs SET status='running',lease_until=? WHERE job_id=?",
                       (self.clock(), current["job_id"]))
            db.execute("BEGIN IMMEDIATE")
            self.assertTrue(self.queue._acquire(db, "runner", self.clock()))
            self.assertEqual(self.queue._recover_locked(db, self.clock()), 1)
            db.execute("COMMIT")
        self.assertEqual(self.queue.status(expired["job_id"])["status"], "unknown")
        self.assertEqual(self.queue.status(current["job_id"])["status"], "running")

    def test_acquire_preserves_other_owner_at_exact_lease_deadline(self):
        with self.queue._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self.assertTrue(self.queue._acquire(db, "first", self.clock()))
            self.assertFalse(self.queue._acquire(db, "second", self.clock() + self.queue.caps.lease_seconds))
            db.execute("COMMIT")

    def test_run_once_executes_bulk_job_at_exact_expiry_deadline(self):
        job = self.submit(priority="bulk")
        self.clock.now += self.queue.caps.bulk_ttl_seconds
        self.sampler.snapshot = self.sampler.fresh()
        result = self.queue.run_once("runner")
        self.assertEqual(result["job_id"], job["job_id"])
        self.assertEqual(result["status"], "complete")

    def test_state_report_declares_schema_version_one(self):
        self.assertEqual(self.queue.state_report()["schema"], 1)


if __name__ == "__main__":
    unittest.main()
