"""Focused POSIX checks for worktrees backed by a queue-pinned source."""
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

from agent_bridge.execution import codex_task, claude_task, job_source
from agent_bridge.orchestration.execution_queue import Harnesses, SubprocessHarnessExecutor


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_CONFIG_SYSTEM": os.devnull, "GIT_AUTHOR_NAME": "Pinned Test",
           "GIT_AUTHOR_EMAIL": "pinned@example.invalid", "GIT_COMMITTER_NAME": "Pinned Test",
           "GIT_COMMITTER_EMAIL": "pinned@example.invalid"}
    return subprocess.run(["git", *args], cwd=repo, env=env, shell=False, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True).stdout.strip()


@unittest.skipUnless(os.name == "posix", "pinned job sources are POSIX-only")
class PinnedHarnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.operator = self.root / "operator"
        self.operator.mkdir()
        git(self.operator, "init", "--quiet")
        (self.operator / "base.txt").write_text("base\n", encoding="utf-8")
        git(self.operator, "add", "base.txt")
        git(self.operator, "commit", "--quiet", "-m", "base")
        self.job = self.root / "job"
        self.job.mkdir(mode=0o700)
        self.manifest = job_source.prepare_job_source(self.operator, "HEAD", self.job)
        (self.job / "job-source-manifest.json").write_text(
            json.dumps(self.manifest), encoding="utf-8")
        self.commit = self.manifest["commit"]

    def test_pinned_manifest_refuses_missing_and_mismatched_base(self) -> None:
        for lane in (codex_task, claude_task):
            with self.assertRaisesRegex(lane.TaskError, "job_source_base_mismatch"):
                lane._pinned_source(self.job, "0" * 40)
        (self.job / "job-source-manifest.json").unlink()
        for lane in (codex_task, claude_task):
            with self.assertRaisesRegex(lane.TaskError, "job_source_manifest_missing"):
                lane._pinned_source(self.job, self.commit)

    def test_pinned_worktree_uses_only_the_bare_job_source(self) -> None:
        source = self.job / job_source.JOB_SOURCE_DIRNAME
        tree = self.root / "tree"
        env = codex_task._env()
        codex_task._add_pinned_worktree(source, tree, self.commit, env)
        checks: list[dict] = []
        codex_task._worktree_checkpoint(tree, source, self.commit, "generation", checks, env)
        self.assertEqual(checks, [{"checkpoint": "generation", "reasons": []}])
        marker = tree / ".git"
        marker.write_text("gitdir: /nowhere\n", encoding="utf-8")
        with self.assertRaisesRegex(codex_task.TaskError, "worktree_pointer_changed"):
            codex_task._worktree_checkpoint(tree, source, self.commit, "tampered", checks, env)
        # Remove through the source's Git directory, not the operator checkout.
        marker.write_text("gitdir: " + str(next((source / "worktrees").iterdir())) + "\n",
                          encoding="utf-8")
        self.assertTrue(codex_task._remove(self.operator, tree, env, git_dir=source))

    def test_manifest_tamper_has_a_closed_integrity_reason(self) -> None:
        source = self.job / job_source.JOB_SOURCE_DIRNAME
        with (source / "config").open("a", encoding="utf-8") as handle:
            handle.write("\n[bridge-test]\nvalue = tampered\n")
        checks: list[dict] = []
        with self.assertRaisesRegex(codex_task.TaskError, "config_changed"):
            codex_task._source_checkpoint(self.job, self.manifest, "before_generation_worktree", checks)
        self.assertEqual(checks[0]["reasons"], ["config_changed"])

    def test_manifest_file_tamper_is_checked_again_at_each_checkpoint(self) -> None:
        altered = {**self.manifest, "base_spelling": "changed"}
        (self.job / "job-source-manifest.json").write_text(json.dumps(altered), encoding="utf-8")
        checks: list[dict] = []
        with self.assertRaisesRegex(claude_task.TaskError, "manifest_mismatch"):
            claude_task._source_checkpoint(self.job, self.manifest, "before_generation_worktree", checks)
        self.assertEqual(checks, [{"checkpoint": "before_generation_worktree",
                                   "reasons": ["manifest_mismatch"]}])

    def test_executor_uses_the_job_directory_only_for_pinned_requests(self) -> None:
        harness = self.root / "harness.py"
        harness.write_text("# test harness\n", encoding="utf-8")
        executor = SubprocessHarnessExecutor(Harnesses(
            codex=harness, claude=harness, python=Path(sys.executable)))

        class Process:
            returncode = 0

            def communicate(self, *, timeout: int):
                return b'{"status":"complete"}\n', b""

        request = {"provider": "codex", "brief": str(self.root / "brief"),
                   "repo": str(self.operator), "base_commit": self.commit,
                   "classification": "synthetic", "model": "test", "effort": "low",
                   "timeout_seconds": 1, "verify_argv": []}
        with mock.patch("agent_bridge.orchestration.execution_queue.subprocess.Popen",
                        return_value=Process()) as popen:
            executor(request, self.job)
        argv = popen.call_args.args[0]
        self.assertIn("--job-source", argv)
        self.assertEqual(argv[argv.index("--job-source") + 1], str(self.job))
        self.assertEqual(popen.call_args.kwargs["cwd"], self.job)

        request.pop("base_commit")
        request["base"] = "HEAD"
        with mock.patch("agent_bridge.orchestration.execution_queue.subprocess.Popen",
                        return_value=Process()) as legacy_popen:
            executor(request, self.job)
        self.assertNotIn("--job-source", legacy_popen.call_args.args[0])
        self.assertEqual(legacy_popen.call_args.kwargs["cwd"], str(self.operator))
