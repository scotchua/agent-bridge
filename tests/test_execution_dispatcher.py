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
    UNEXPECTED_FAILURE_DETAIL, ExecutionAdmissionError, ExecutionQueue, _atomic_json,
    _harness_summary, outcome_is_success)
from agent_bridge.orchestration.server import Server


class FakeExecutor:
    """Shaped like SubprocessHarnessExecutor's outcome, semantics included.

    The exit code and the harness's own verdict are separate inputs here
    precisely because they can disagree on a real run: a harness whose
    verification commands failed still exits 0 today.
    """

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

    def test_a_command_the_harness_would_refuse_is_refused_at_admission(self):
        # Live jobs d8e5763d, 7a79ae7f and 19869b0e were admitted with commands
        # the harness refuses, ran, and failed as a bare "TaskError".
        with self.assertRaisesRegex(
                ExecutionAdmissionError,
                "verify_argv_rejected: Python verification is limited to "
                "python -m pytest or python -m unittest"):
            self.submit(verify_argv=[["python3", "-c", "print(1)"]])
        with self.assertRaisesRegex(ExecutionAdmissionError,
                                    "verify_argv_rejected: verification executable"):
            self.submit(verify_argv=[["/usr/bin/test", "-e", "x"]])
        with self.assertRaisesRegex(ExecutionAdmissionError, "verify_argv_invalid"):
            self.submit(verify_argv=[["pytest", ""]])
        self.assertEqual(self.queue.state_report(), {})
        unittest_run = [["python3", "-m", "unittest", "discover", "-s", "tests"]]
        job = self.submit(verify_argv=unittest_run)
        request = json.loads((self.root / "queue" / job["job_id"] / "request.json").read_text())
        self.assertEqual(request["verify_argv"], unittest_run)

    def test_a_failed_run_records_the_reason_not_only_the_class(self):
        job = self.submit()
        self.brief.write_text("changed", encoding="utf-8")
        self.queue.run_once("worker-1")
        result = self.queue.result(job["job_id"])
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error"], "ExecutionAdmissionError")
        self.assertEqual(result["error_detail"], "brief_changed_after_admission")

    def test_an_unexpected_exception_records_a_fixed_marker_not_its_text(self):
        """Only admission and OS errors carry text vetted for a receipt.
        Anything else is recorded by class and a fixed marker, so a stray
        message never reaches the durable record."""
        job = self.submit()

        def explode(request, selected):
            raise RuntimeError("token=do-not-record")

        queue = ExecutionQueue(self.root / "queue", explode, clock=lambda: 100.0)
        queue.run_once("worker-1")
        result = queue.result(job["job_id"])
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error"], "RuntimeError")
        self.assertEqual(result["error_detail"], UNEXPECTED_FAILURE_DETAIL)
        self.assertNotIn("do-not-record", json.dumps(result))

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
            route="claude", observed_at=100.0, fresh_until=200.0, available=True,
            source="test"), trusted=True)
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



class HarnessVerdictTests(unittest.TestCase):
    """A zero exit code is not a verified task."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _made(self, **extra):
        outcome = {"returncode": 0, "harness_ok": True,
                   "harness_status": "complete"}
        outcome.update(extra)
        return outcome

    def test_a_clean_run_succeeds(self):
        self.assertTrue(outcome_is_success(self._made()))

    def test_a_verification_failure_that_exited_zero_is_not_a_success(self):
        self.assertFalse(outcome_is_success(self._made(
            harness_ok=False, harness_status="verification_failed")))

    def test_a_harness_that_said_ok_but_did_not_complete_is_not_a_success(self):
        self.assertFalse(outcome_is_success(self._made(
            harness_status="verification_failed")))

    def test_a_nonzero_exit_is_not_a_success_whatever_the_receipt_said(self):
        self.assertFalse(outcome_is_success(self._made(returncode=1)))

    def test_an_outcome_with_no_verdict_at_all_is_not_a_success(self):
        self.assertFalse(outcome_is_success({"returncode": 0}))

    def test_an_unparseable_final_line_is_not_a_success(self):
        summary = _harness_summary(b"not json\n")
        self.assertEqual(summary["harness_verdict"], "receipt_unparseable")
        self.assertFalse(outcome_is_success({"returncode": 0, **summary}))

    def test_no_output_at_all_is_not_a_success(self):
        summary = _harness_summary(b"")
        self.assertEqual(summary["harness_verdict"], "receipt_absent")
        self.assertFalse(outcome_is_success({"returncode": 0, **summary}))

    def test_the_verdict_is_read_from_the_last_line_not_the_first(self):
        stdout = (b'{"ok": true, "status": "complete"}\n'
                  b'{"ok": false, "status": "verification_failed"}\n')
        summary = _harness_summary(stdout)
        self.assertEqual(summary["harness_status"], "verification_failed")
        self.assertFalse(summary["harness_ok"])

    def test_a_json_array_is_refused_rather_than_indexed(self):
        self.assertEqual(_harness_summary(b'["complete"]')["harness_verdict"],
                         "receipt_not_an_object")

    def test_a_harness_refusal_line_is_a_failure_with_its_reason(self):
        # Exactly what the live harness printed for job d8e5763d, plus the
        # detail the harness now adds.
        summary = _harness_summary(
            b'{"error": "TaskError", "error_detail": "Python verification is limited '
            b'to python -m pytest or python -m unittest", "ok": false}\n')
        self.assertEqual(summary["harness_status"], "failed")
        self.assertEqual(summary["harness_verdict"], "read_failure")
        self.assertEqual(summary["error"], "TaskError")
        self.assertTrue(summary["error_detail"].startswith("Python verification"))
        self.assertFalse(outcome_is_success({"returncode": 1, **summary}))
        self.assertFalse(outcome_is_success({"returncode": 0, **summary}))

    def test_a_failure_line_without_an_error_is_still_unknown(self):
        self.assertEqual(_harness_summary(b'{"ok": false}')["harness_verdict"],
                         "receipt_status_unknown")
        self.assertEqual(_harness_summary(b'{"ok": true}')["harness_verdict"],
                         "receipt_status_unknown")

    def test_diagnostics_are_bounded_and_printable(self):
        line = json.dumps({"ok": False, "error": "TaskError",
                           "error_detail": "x\x1b[31m" + "y" * 2000}).encode()
        summary = _harness_summary(line)
        self.assertEqual(len(summary["error_detail"]), 512)
        self.assertNotIn("\x1b", summary["error_detail"])
        self.assertEqual(_harness_summary(b'{"ok": false, "error": 7}')["harness_verdict"],
                         "receipt_status_unknown")

    def test_a_status_line_keeps_its_diagnostics(self):
        summary = _harness_summary(
            b'{"ok": false, "status": "failed", "error": "OSError", '
            b'"error_detail": "codex login status: not logged in"}')
        self.assertEqual(summary["harness_verdict"], "read")
        self.assertEqual(summary["error_detail"], "codex login status: not logged in")



if __name__ == "__main__":
    unittest.main()
