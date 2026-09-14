"""Offline tests for durable cross-provider implementation dispatch."""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.capacity_router import CapacityObservation, StageRouter
from agent_bridge.localq.spool import FakeBackend, LocalQueue, ResourceSnapshot
from agent_bridge.orchestration.execution_queue import (
    ExecutionAdmissionError, ExecutionQueue, _atomic_json, _harness_summary,
    outcome_is_success)
from agent_bridge.orchestration.server import Server


class FakeExecutor:
    def __init__(self, returncode=0, harness_ok=True, harness_status="complete"):
        self.returncode = returncode
        self.harness_ok = harness_ok
        self.harness_status = harness_status
        self.requests = []

    def __call__(self, request, job_dir):
        self.requests.append(request)
        return {"returncode": self.returncode, "stdout_sha256": "a" * 64,
                "stderr_sha256": "b" * 64, "stdout_bytes": 10, "stderr_bytes": 0,
                "harness_ok": self.harness_ok,
                "harness_status": self.harness_status,
                "harness_verdict": "read"}


class Sampler:
    def sample(self):
        return ResourceSnapshot(100.0, "normal", "normal", True, 120.0)


class LocalService:
    def __init__(self, root):
        self.queue = LocalQueue(root, sampler=Sampler(), backend=FakeBackend(), clock=lambda: 100.0)

    def once(self):
        return None


class ExecutionDispatcherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        self.brief = self.root / "brief.md"
        self.brief.write_text("Synthetic implementation brief", encoding="utf-8")
        self.fake = FakeExecutor()
        self.queue = ExecutionQueue(self.root / "queue", self.fake, clock=lambda: 100.0)

    def tearDown(self):
        self.temp.cleanup()

    def submit(self, **changes):
        values = {"caller": "codex", "provider": "claude", "repo": str(self.repo),
                  "brief": str(self.brief), "base": "HEAD", "classification": "synthetic",
                  "model": "sonnet", "effort": "medium", "verify_argv": [["pytest", "-q"]],
                  "timeout_seconds": 90, "paid_fallback": False, "item_id": "item-1",
                  "stage": "implement", "owner_id": "owner-1", "stage_revision": 1}
        values.update(changes)
        return self.queue.submit(**values)

    def test_opposite_provider_only_and_no_paid_fallback(self):
        with self.assertRaisesRegex(ExecutionAdmissionError, "provider_not_eligible"):
            self.submit(provider="codex")
        with self.assertRaisesRegex(ExecutionAdmissionError, "paid_fallback_forbidden"):
            self.submit(paid_fallback=True)
        claude_to_codex = self.submit(caller="claude", provider="codex", model="gpt-5.6-terra",
                                      verify_argv=[])
        self.assertEqual(claude_to_codex["provider"], "codex")

    def test_atomic_receipt_closes_descriptor_and_preserves_acl_failure(self):
        target = self.root / "queue" / "acl-test.json"
        target.parent.mkdir(exist_ok=True)
        real_close = os.close
        with mock.patch(
            "agent_bridge.orchestration.execution_queue.host_platform.enforce_owner_only_file",
            side_effect=PermissionError("owner-only ACL unavailable"),
        ), mock.patch(
            "agent_bridge.orchestration.execution_queue.os.close",
            wraps=real_close,
        ) as close:
            with self.assertRaisesRegex(PermissionError, "owner-only ACL unavailable"):
                _atomic_json(target, {"state": "test"})
        self.assertTrue(close.called)
        self.assertFalse(target.exists())
        self.assertEqual(list(target.parent.glob(".pending-*")), [])

    def test_classification_and_paths_fail_closed(self):
        with self.assertRaisesRegex(ExecutionAdmissionError, "classification_not_eligible"):
            self.submit(classification="client_derived")
        with self.assertRaisesRegex(ExecutionAdmissionError, "repo_and_brief_must_be_absolute"):
            self.submit(brief="brief.md")
        with self.assertRaisesRegex(ExecutionAdmissionError, "claude_verification_required"):
            self.submit(verify_argv=[])

    def test_job_is_durable_and_fake_executor_completes_with_nonlanding_receipt(self):
        queued = self.submit(idempotency_key="same-task")
        duplicate = self.submit(idempotency_key="same-task")
        self.assertEqual(queued["job_id"], duplicate["job_id"])
        with self.assertRaisesRegex(ExecutionAdmissionError, "idempotency_conflict"):
            self.submit(idempotency_key="same-task", base="other")
        restarted = ExecutionQueue(self.root / "queue", self.fake, clock=lambda: 101.0)
        self.assertEqual(restarted.status(queued["job_id"])["state"], "queued")
        self.assertEqual(restarted.run_once("worker-1")["state"], "complete")
        result = restarted.result(queued["job_id"])
        self.assertFalse(result["permission_to_apply"])
        self.assertFalse(result["permission_to_commit"])
        self.assertFalse(result["permission_to_push"])
        self.assertFalse(result["permission_to_merge"])
        self.assertNotIn("stdout", result["harness"])

    def test_changed_brief_fails_without_calling_provider(self):
        job = self.submit()
        self.brief.write_text("changed", encoding="utf-8")
        self.queue.run_once("worker-1")
        self.assertEqual(self.queue.result(job["job_id"])["state"], "failed")
        self.assertEqual(self.fake.requests, [])

    def test_zero_exit_with_failed_verification_is_failed(self):
        failed = FakeExecutor(0, False, "verification_failed")
        queue = ExecutionQueue(self.root / "failed-queue", failed, clock=lambda: 100.0)
        self.queue = queue
        job = self.submit()
        self.assertEqual(queue.run_once("worker-1")["state"], "failed")

    def test_missing_or_unreadable_harness_receipt_fails_closed(self):
        self.assertFalse(outcome_is_success({"returncode": 0, **_harness_summary(b"")}))
        self.assertFalse(outcome_is_success({"returncode": 0, **_harness_summary(b"not json\n")}))

    def test_restart_blocks_interrupted_job_instead_of_resending(self):
        job = self.submit()
        directory = self.root / "queue" / job["job_id"]
        receipt = json.loads((directory / "receipt.json").read_text())
        receipt["state"] = "running"
        (directory / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
        restarted = ExecutionQueue(self.root / "queue", self.fake, clock=lambda: 102.0)
        self.assertEqual(restarted.status(job["job_id"])["state"], "blocked")
        self.assertIsNone(restarted.run_once("worker-2"))
        self.assertEqual(self.fake.requests, [])

    def test_mcp_tools_bind_caller_and_only_queue_execution(self):
        router = StageRouter(self.root / "capacity.sqlite3", clock=lambda: 100.0)
        router.observe_capacity(CapacityObservation(
            route="claude", observed_at=100.0, fresh_until=200.0, available=True, source="test"))
        registered = router.register("item-1", "implement", allowed_routes=["claude"])
        owned = router.assign("item-1", "implement", owner_id="owner-1", lease_seconds=60,
                              expected_revision=registered["revision"])
        service = LocalService(self.root / "local")
        server = Server("codex", service, router, execution=self.queue, interval=0.01)
        names = {tool["name"] for tool in server.handle({"jsonrpc": "2.0", "id": 1,
                 "method": "tools/list"})["result"]["tools"]}
        self.assertTrue({"execution_dispatch", "execution_status", "execution_result"} <= names)
        response = server.tools["execution_dispatch"]["handler"]({
            "provider": "claude", "repo": str(self.repo), "brief": str(self.brief),
            "base": "HEAD", "classification": "synthetic", "model": "sonnet",
            "effort": "medium", "verify_argv": [["pytest", "-q"]], "item_id": "item-1",
            "stage": "implement", "owner_id": "owner-1", "stage_revision": owned["revision"]})
        self.assertTrue(response["ok"])
        self.assertEqual(response["state"], "queued")
        spoof = server.tools["execution_dispatch"]["handler"]({
            "caller": "claude", "provider": "codex", "repo": str(self.repo),
            "brief": str(self.brief), "base": "HEAD", "classification": "synthetic",
            "model": "gpt-5.6-terra", "effort": "medium", "item_id": "item-1",
            "stage": "implement", "owner_id": "owner-1", "stage_revision": owned["revision"]})
        self.assertFalse(spoof["ok"])

        server.start()
        import time
        time.sleep(0.03)
        server.stop()
        self.assertEqual(self.queue.status(response["job_id"])["state"], "queued")
        self.assertEqual(self.fake.requests, [])

    def test_status_only_queue_does_not_reconcile_or_execute(self):
        job = self.submit()
        directory = self.root / "queue" / job["job_id"]
        receipt = json.loads((directory / "receipt.json").read_text())
        receipt["state"] = "running"
        (directory / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
        status_only = ExecutionQueue(self.root / "queue", None,
                                     clock=lambda: 102.0,
                                     recover_interrupted=False)
        self.assertEqual(status_only.status(job["job_id"])["state"], "running")
        with self.assertRaisesRegex(ExecutionAdmissionError, "worker_not_configured"):
            status_only.run_once("mcp-process")

    def test_restart_blocks_claimed_but_not_yet_running_job(self):
        job = self.submit()
        directory = self.root / "queue" / job["job_id"]
        (directory / "claim.lock").write_text("", encoding="utf-8")
        restarted = ExecutionQueue(self.root / "queue", self.fake, clock=lambda: 103.0)
        result = restarted.result(job["job_id"])
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["error"], "interrupted_requires_reconciliation")
        self.assertEqual(self.fake.requests, [])


if __name__ == "__main__":
    unittest.main()
