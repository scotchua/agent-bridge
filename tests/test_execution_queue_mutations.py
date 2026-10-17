"""Offline boundary checks for the durable execution queue."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge.orchestration import execution_queue as eq


class QueueMutationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "invented-repo"
        self.repo.mkdir()
        env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
               "GIT_CONFIG_SYSTEM": os.devnull, "GIT_AUTHOR_NAME": "Queue Test",
               "GIT_AUTHOR_EMAIL": "queue@example.invalid", "GIT_COMMITTER_NAME": "Queue Test",
               "GIT_COMMITTER_EMAIL": "queue@example.invalid"}
        subprocess.run(["git", "init", "--quiet"], cwd=self.repo, check=True, env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=self.repo, check=True, env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)
        subprocess.run(["git", "commit", "--quiet", "-m", "base"], cwd=self.repo, check=True,
                       env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)
        self.brief = self.root / "brief.md"
        self.brief.write_text("Implement a synthetic ledger report", encoding="utf-8")
        self.queue = eq.ExecutionQueue(self.root / "queue", lambda request, path: {
            "returncode": 0, "harness_ok": True, "harness_status": "complete",
            "harness_verdict": "read"}, model_reserved=eq.reserve_nothing)

    def submit(self, **changes):
        values = {"caller": "claude", "provider": "codex", "repo": str(self.repo),
                  "brief": str(self.brief), "base": "HEAD", "classification": "synthetic",
                  "model": "invented-model", "effort": "medium", "item_id": "item-1",
                  "stage": "implement", "owner_id": "owner-1", "stage_revision": 1}
        values.update(changes)
        return self.queue.submit(**values)

    def test_read_json_missing_file_has_named_refusal(self):
        with self.assertRaisesRegex(eq.ExecutionAdmissionError, "execution_receipt_unreadable"):
            eq._read_json(self.root / "absent.json")

    def test_executor_init_requires_absolute_harness_paths(self):
        relative = Path(os.path.relpath(ROOT / "src/agent_bridge/execution/codex_task.py"))
        self.assertTrue(relative.is_file())
        harnesses = eq.Harnesses(relative,
                                 ROOT / "src/agent_bridge/execution/claude_task.py",
                                 Path(sys.executable))
        with self.assertRaisesRegex(eq.ExecutionAdmissionError, "codex_harness_unavailable"):
            eq.SubprocessHarnessExecutor(harnesses)

    @unittest.skipUnless(os.name == "posix", "POSIX executor")
    def test_executor_call_allows_a_normal_claude_job_past_review_gate(self):
        harnesses = eq.Harnesses(
            ROOT / "src/agent_bridge/execution/codex_task.py",
            ROOT / "src/agent_bridge/execution/claude_task.py",
            Path(sys.executable))
        executor = eq.SubprocessHarnessExecutor(harnesses)
        class ReachedVerification(Exception):
            pass
        with mock.patch.object(eq.claude_config, "is_ready", return_value=True), \
                mock.patch.object(eq.verify_policy, "check_runnable",
                                  side_effect=ReachedVerification):
            with self.assertRaises(ReachedVerification):
                executor({"provider": "claude", "kind": "execution", "verify_argv": []},
                         self.root)

    def test_validate_outcome_names_a_non_mapping_result(self):
        with self.assertRaisesRegex(eq.ExecutionAdmissionError, "execution_outcome_not_a_mapping"):
            eq.validate_outcome([])

    def test_outcome_is_success_returns_boolean_false(self):
        self.assertIs(eq.outcome_is_success({"returncode": 1}), False)

    def test_require_private_queue_defaults_root_to_the_path(self):
        path = self.root / "queue"
        with mock.patch.object(eq.wpv, "require_private_directory") as check:
            eq.require_private_queue(path)
        self.assertEqual(check.call_args.kwargs["root"], path)

    def test_clean_text_refuses_its_exact_upper_boundary(self):
        with self.assertRaisesRegex(eq.ExecutionAdmissionError, "field_invalid"):
            self.queue._clean_text("x" * 257, "field")

    def test_clean_text_refuses_empty_text(self):
        with self.assertRaisesRegex(eq.ExecutionAdmissionError, "field_invalid"):
            self.queue._clean_text("", "field")

    def test_refuse_reserved_model_refuses_empty_model(self):
        with self.assertRaisesRegex(eq.ExecutionAdmissionError, "model_invalid"):
            self.queue._refuse_reserved_model("")

    def test_submit_uses_the_declared_default_timeout(self):
        job = self.submit()
        request = json.loads((self.queue.root / job["job_id"] / "request.json").read_text())
        self.assertEqual(request["timeout_seconds"], 900)

    def test_directory_rejects_invalid_hex_id_by_name(self):
        with self.assertRaisesRegex(eq.ExecutionAdmissionError, "job_id_invalid"):
            self.queue._directory("g" * 32)

    def test_status_formats_the_dependency_reason(self):
        job = self.submit()
        path = self.queue.root / job["job_id"] / "receipt.json"
        receipt = json.loads(path.read_text())
        receipt["waiting_on_dependency"] = "capacity-pause"
        path.write_text(json.dumps(receipt), encoding="utf-8")
        self.assertEqual(self.queue.status(job["job_id"])["waiting_on_dependency"],
                         "waiting_on_dependency:capacity-pause")

    def test_hold_dependencies_annotates_only_matching_queued_jobs(self):
        first = self.submit()
        second = self.submit(item_id="item-2")
        completed = self.queue.run_once("worker-1")
        pending = second if completed["job_id"] == first["job_id"] else first
        self.assertEqual(self.queue.result(completed["job_id"])["state"], "complete")
        self.queue.hold_dependencies({"codex": "capacity-pause"})
        self.assertNotIn("waiting_on_dependency", self.queue.result(completed["job_id"]))
        self.assertEqual(self.queue.status(pending["job_id"])["waiting_on_dependency"],
                         "waiting_on_dependency:capacity-pause")
        self.queue.hold_dependencies({})
        self.assertNotIn("waiting_on_dependency", self.queue.status(pending["job_id"]))

    def test_result_refuses_queued_job_by_name(self):
        job = self.submit()
        with self.assertRaisesRegex(eq.ExecutionAdmissionError, "execution_job_not_terminal"):
            self.queue.result(job["job_id"])

    def test_state_report_counts_two_queued_jobs(self):
        self.submit()
        self.submit(item_id="item-2")
        self.assertEqual(self.queue.state_report(), {"queued": 2})


if __name__ == "__main__":
    unittest.main()
