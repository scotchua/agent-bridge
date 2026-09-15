"""The opt-in verification must never touch the user's real queues.

``ExecutionQueue.run_once`` claims the oldest queued job in the root it was
given, not the job the caller just submitted. Draining the configured queue to
verify delegation therefore executed whatever the user had waiting: a real
provider call, a real receipt consumed, as a side effect of a setup check.
These tests hold the isolation in place.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge.orchestration import delegation_verify as dv
from agent_bridge.orchestration.execution_queue import ExecutionQueue


def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


class RecordingExecutor:
    """Stands in for the provider harness and records every job it is given."""

    def __init__(self):
        self.repos: list[str] = []
        self.briefs: list[str] = []

    def __call__(self, request, job_dir):
        self.repos.append(request["repo"])
        self.briefs.append(request["brief"])
        return {"returncode": 0, "stdout_sha256": "a" * 64,
                "stderr_sha256": "b" * 64, "stdout_bytes": 0, "stderr_bytes": 0,
                "harness_ok": True, "harness_status": "complete",
                "harness_verdict": "read"}


@unittest.skipUnless(_git_available(), "git is required to build the fixture repo")
class VerificationQueueIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.production = self.root / "production-queue"
        self.executor = RecordingExecutor()
        self.user_repo = self.root / "user-repo"
        self.user_repo.mkdir()
        subprocess.run(["git", "init", "--quiet", str(self.user_repo)], check=True)
        self.user_brief = self.user_repo / "brief.txt"
        self.user_brief.write_text("the user's own queued work\n", encoding="utf-8")
        self.production.mkdir(mode=0o700)

    def tearDown(self):
        self.temp.cleanup()

    def _queue_unrelated_job(self) -> str:
        queue = ExecutionQueue(self.production, self.executor,
                               recover_interrupted=False)
        submitted = queue.submit(
            caller="codex", provider="claude", repo=str(self.user_repo),
            brief=str(self.user_brief), base="HEAD", classification="synthetic",
            model="default", effort="low", item_id="the-users-work",
            stage="real-stage", owner_id="the-user", stage_revision=0,
            verify_argv=[["git", "status"]], timeout_seconds=300,
            idempotency_key="the-users-own-job")
        return submitted["job_id"]

    def test_a_queued_job_is_not_executed_by_verification(self):
        job_id = self._queue_unrelated_job()
        dv._run_direction(self.executor, self.production,
                          caller="codex", provider="claude")
        queue = ExecutionQueue(self.production, self.executor,
                               recover_interrupted=False)
        self.assertEqual(queue.status(job_id)["state"], "queued")
        self.assertNotIn(str(self.user_repo), self.executor.repos)
        self.assertNotIn(str(self.user_brief), self.executor.briefs)

    def test_verification_executes_exactly_its_own_synthetic_job(self):
        self._queue_unrelated_job()
        dv._run_direction(self.executor, self.production,
                          caller="codex", provider="claude")
        self.assertEqual(len(self.executor.repos), 1)
        self.assertTrue(Path(self.executor.briefs[0]).name
                        .startswith(".agent-bridge-verify-brief"))

    def test_verification_writes_nothing_into_the_configured_queue_root(self):
        before = sorted(p.name for p in self.production.iterdir())
        dv._run_direction(self.executor, self.production,
                          caller="codex", provider="claude")
        self.assertEqual(sorted(p.name for p in self.production.iterdir()),
                         before)

    def test_the_configured_root_is_not_even_created_when_it_is_absent(self):
        absent = self.root / "never-created"
        dv._run_direction(self.executor, absent, caller="codex",
                          provider="claude")
        self.assertFalse(absent.exists())

    def test_each_direction_gets_a_distinct_temporary_root(self):
        first = dv._verification_queue_root()
        second = dv._verification_queue_root()
        try:
            self.assertNotEqual(first, second)
            if os.name != "nt":
                self.assertEqual(first.stat().st_mode & 0o777, 0o700)
        finally:
            for path in (first, second):
                path.rmdir()

    def test_the_temporary_queue_root_does_not_survive_the_direction(self):
        roots: list[Path] = []
        original = dv._verification_queue_root

        def recording():
            created = original()
            roots.append(created)
            return created

        dv._verification_queue_root = recording
        try:
            dv._run_direction(self.executor, self.production,
                              caller="codex", provider="claude")
        finally:
            dv._verification_queue_root = original
        self.assertEqual(len(roots), 1)
        self.assertFalse(roots[0].exists())

    def test_the_synthetic_repository_is_removed_afterwards(self):
        row = dv._run_direction(self.executor, self.production,
                                caller="codex", provider="claude")
        self.assertTrue(row["worktree_removed"])
        self.assertFalse(Path(self.executor.repos[0]).exists())

    def test_the_row_never_reports_permission_to_land(self):
        row = dv._run_direction(self.executor, self.production,
                                caller="codex", provider="claude")
        for key in ("permission_to_apply", "permission_to_commit",
                    "permission_to_push", "permission_to_merge"):
            self.assertFalse(row[key], key)


class ExecutorSelectionTests(unittest.TestCase):
    """Verification must exercise the executor production actually dispatches.

    A POSIX subprocess harness would pass here on a Windows machine and then be
    replaced at dispatch time by an executor with entirely different gates,
    which is verifying something nobody runs.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _cfg(self, **overrides):
        values = {"windows_wsl_runtime_root": str(self.root / "runtime"),
                  "windows_wsl_rootfs_path": str(self.root / "rootfs.tar"),
                  "windows_wsl_manifest_path": str(self.root / "manifest.json"),
                  "windows_wsl_sidecar_path": None,
                  "execution_queue_root": None,
                  "codex_task_executable": None,
                  "claude_task_executable": None,
                  "python_executable": None,
                  "claude_config_dir": None}
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_windows_selects_the_wsl_executor(self):
        from agent_bridge.orchestration import windows_delegation

        executor, reason = dv.select_executor(self._cfg(), platform_name="win32")
        self.assertEqual(reason, "")
        self.assertIsInstance(executor, windows_delegation.WindowsWslExecutor)

    def test_the_selected_windows_executor_refuses_without_evidence(self):
        executor, _reason = dv.select_executor(self._cfg(), platform_name="win32")
        self.assertFalse(executor.provider_lane.enabled_for("claude"))
        self.assertIsNone(executor.provision_state)

    def test_windows_without_runtime_configuration_is_named(self):
        executor, reason = dv.select_executor(
            self._cfg(windows_wsl_runtime_root=None), platform_name="win32")
        self.assertIsNone(executor)
        self.assertEqual(reason, "windows_wsl_configuration_missing")

    def test_posix_still_selects_the_subprocess_harness(self):
        executor, reason = dv.select_executor(self._cfg(), platform_name="darwin")
        self.assertIsNone(executor)
        self.assertEqual(reason, "execution_configuration_missing")



if __name__ == "__main__":
    unittest.main()
