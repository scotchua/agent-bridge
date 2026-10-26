"""Offline tests for the execution step ledger and safe resume after interruption."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.orchestration.execution_queue import (
    MAX_RESUMES, ExecutionQueue, _append_step, _read_steps, reserve_nothing,
)
from git_fixture import make_git_repo
from sqlite_teardown import cleanup_temporary_directory
from test_execution_dispatcher import FakeExecutor


class StepLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        make_git_repo(self.repo)
        self.brief = self.root / "brief.md"
        self.brief.write_text("Synthetic implementation brief", encoding="utf-8")
        self.fake = FakeExecutor()
        self.queue = self.open_queue()

    def tearDown(self):
        cleanup_temporary_directory(self.temp)

    def open_queue(self, recover=True):
        return ExecutionQueue(self.root / "queue", self.fake, clock=lambda: 100.0,
                              recover_interrupted=recover, model_reserved=reserve_nothing)

    def submit(self):
        return self.queue.submit(
            caller="codex", provider="claude", repo=str(self.repo), brief=str(self.brief),
            base="HEAD", classification="synthetic", model="sonnet", effort="medium",
            verify_argv=[["pytest", "-q"]], timeout_seconds=90, paid_fallback=False,
            item_id="item-1", stage="implement", owner_id="owner-1", stage_revision=1)

    def directory(self, job):
        return self.root / "queue" / job["job_id"]

    def receipt(self, job):
        return json.loads((self.directory(job) / "receipt.json").read_text())

    def interrupt(self, job, *steps):
        """Leave the job as a dead worker would: running, claimed, with steps."""
        directory = self.directory(job)
        receipt = self.receipt(job)
        attempt = receipt.get("resume_count", 0)
        (directory / "claim.lock").write_bytes(b"")
        receipt.update(state="running", worker_id="dead-worker", started_at=99.0)
        (directory / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
        for step, phase in steps:
            _append_step(directory, step, phase, 99.0, attempt)

    def test_completed_job_records_every_step_in_order(self):
        job = self.submit()
        self.queue.run_once("worker-1")
        records = _read_steps(self.directory(job))
        # The fake executor does not mark its own spawn, so the queue does.
        self.assertEqual([(r["step"], r["phase"]) for r in records], [
            ("claimed", "commit"), ("stage_rechecked", "commit"),
            ("provider_spawned", "begin"), ("provider_exited", "commit"),
            ("result_written", "commit")])
        self.assertEqual(len({r["step_id"] for r in records}), len(records))

    def test_append_is_idempotent_by_step_id(self):
        job = self.submit()
        directory = self.directory(job)
        for _ in range(3):
            _append_step(directory, "claimed", "commit", 1.0, 0)
        _append_step(directory, "claimed", "begin", 1.0, 0)
        _append_step(directory, "claimed", "commit", 1.0, 1)
        records = _read_steps(directory)
        self.assertEqual([(r["phase"]) for r in records], ["commit", "begin", "commit"])
        self.assertEqual(len({r["step_id"] for r in records}), 2)

    def test_interrupted_before_spawn_requeues_and_then_succeeds(self):
        job = self.submit()
        self.interrupt(job, ("claimed", "commit"), ("stage_rechecked", "commit"))
        self.queue = self.open_queue()
        receipt = self.receipt(job)
        self.assertEqual(receipt["state"], "queued")
        self.assertEqual(receipt["resume_count"], 1)
        self.assertEqual(receipt["resume_reason"], "resumed_after_interruption")
        self.assertFalse((self.directory(job) / "claim.lock").exists())
        result = self.queue.run_once("worker-2")
        self.assertEqual(result["state"], "complete")
        self.assertEqual(len(self.fake.requests), 1)

    def test_interrupted_after_spawn_stays_blocked_with_last_step(self):
        job = self.submit()
        self.interrupt(job, ("claimed", "commit"), ("stage_rechecked", "commit"),
                       ("provider_spawned", "begin"))
        self.queue = self.open_queue()
        receipt = self.receipt(job)
        self.assertEqual(receipt["state"], "blocked")
        self.assertEqual(receipt["error"], "interrupted_requires_reconciliation")
        self.assertEqual(receipt["last_step"], "provider_spawned")
        self.assertIs(receipt["provider_send_may_have_happened"], True)
        self.assertNotIn("resume_count", receipt)
        self.assertIsNone(self.queue.run_once("worker-2"))
        self.assertEqual(self.fake.requests, [])

    def test_requeue_is_capped(self):
        job = self.submit()
        for expected in range(1, MAX_RESUMES + 1):
            self.interrupt(job, ("claimed", "commit"))
            self.queue = self.open_queue()
            self.assertEqual(self.receipt(job)["state"], "queued")
            self.assertEqual(self.receipt(job)["resume_count"], expected)
        self.interrupt(job, ("claimed", "commit"))
        self.queue = self.open_queue()
        receipt = self.receipt(job)
        self.assertEqual(receipt["state"], "blocked")
        self.assertEqual(receipt["error"], "interrupted_requires_reconciliation")
        self.assertEqual(receipt["resume_count"], MAX_RESUMES)
        self.assertNotIn("provider_send_may_have_happened", receipt)
        self.assertEqual(self.fake.requests, [])

    def test_job_without_a_ledger_is_blocked_exactly_as_before(self):
        job = self.submit()
        self.interrupt(job)
        self.assertFalse((self.directory(job) / "steps.jsonl").exists())
        self.queue = self.open_queue()
        receipt = self.receipt(job)
        self.assertEqual(receipt["state"], "blocked")
        self.assertEqual(receipt["error"], "interrupted_requires_reconciliation")
        for key in ("last_step", "resume_count", "provider_send_may_have_happened"):
            self.assertNotIn(key, receipt)
        self.assertTrue((self.directory(job) / "claim.lock").exists())

    def test_claimed_but_not_running_with_ledger_requeues(self):
        job = self.submit()
        directory = self.directory(job)
        (directory / "claim.lock").write_bytes(b"")
        _append_step(directory, "claimed", "commit", 99.0, 0)
        self.queue = self.open_queue()
        self.assertEqual(self.receipt(job)["state"], "queued")
        self.assertEqual(self.queue.run_once("worker-2")["state"], "complete")

    def test_torn_final_line_is_ignored(self):
        job = self.submit()
        directory = self.directory(job)
        _append_step(directory, "claimed", "commit", 1.0, 0)
        with open(directory / "steps.jsonl", "ab") as handle:
            handle.write(b'{"step_id":"x","step":"provid')
        self.assertEqual([r["step"] for r in _read_steps(directory)], ["claimed"])


if __name__ == "__main__":
    unittest.main()
