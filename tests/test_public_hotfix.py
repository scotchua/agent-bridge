import hashlib
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.execution import claude_task, codex_task
from agent_bridge.orchestration import delegation_verify
from agent_bridge import onboard


class SourceIntegrityRegressionTests(unittest.TestCase):
    def _exercise(self, module):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            target = repo / "value.txt"
            target.write_text("one\n")
            subprocess.run(["git", "add", "value.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
            target.write_text("already dirty\n")
            env = module._env()
            status = module._git(repo, "status", "--porcelain=v2", "--untracked-files=all", env=env)
            before = module._source_state(repo, env)
            target.write_text("changed again\n")
            self.assertEqual(status, module._git(repo, "status", "--porcelain=v2", "--untracked-files=all", env=env))
            self.assertNotEqual(before, module._source_state(repo, env))

    def test_claude_detects_changes_to_already_dirty_files(self): self._exercise(claude_task)
    def test_codex_detects_changes_to_already_dirty_files(self): self._exercise(codex_task)

    def test_both_harness_clis_exit_nonzero_for_failed_verification(self):
        cases = (
            (claude_task, ["brief", "--repo", "/repo", "--classification", "synthetic", "--verify-json", "[\"git\",\"status\"]"]),
            (codex_task, ["brief", "--repo", "/repo", "--classification", "synthetic"]),
        )
        for module, argv in cases:
            with self.subTest(module=module.__name__):
                output = io.StringIO()
                with mock.patch.object(module, "run_task", return_value={"status": "verification_failed"}), \
                     contextlib.redirect_stdout(output):
                    code = module.main(argv)
                self.assertNotEqual(code, 0)
                self.assertFalse(json.loads(output.getvalue())["ok"])


class VerificationQueueIsolationTests(unittest.TestCase):
    def test_provider_verification_ignores_configured_queue(self):
        with tempfile.TemporaryDirectory() as temporary:
            configured = Path(temporary) / "production"
            observed = {}
            class FakeQueue:
                def __init__(self, root, executor, recover_interrupted=False,
                             model_reserved=None): observed["root"] = Path(root)
                def submit(self, **kwargs): return {"job_id": "synthetic"}
                def status(self, job_id): return {"state": "complete"}
                def result(self, job_id): return {"state": "complete", "classification": "synthetic", "harness": {"returncode": 0}}
            with mock.patch.object(delegation_verify, "ExecutionQueue", FakeQueue):
                delegation_verify._run_direction(object(), configured, caller="codex", provider="claude")
            self.assertNotEqual(observed["root"], configured)
            self.assertFalse(observed["root"].exists())

    def test_local_verification_ignores_configured_queue(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worker = root / "worker"
            worker.write_text("worker")
            observed = {}
            class Queue:
                def submit(self, **kwargs): return {"job_id": "synthetic"}
                def status(self, job_id): return {"status": "complete"}
                def result(self, job_id): return {"status": "complete"}
            class Service:
                def __init__(self, queue_root, *args): observed["root"] = Path(queue_root); self.queue = Queue()
                def once(self): pass
            cfg = type("Config", (), {"worker_executable": worker,
                                      "worker_state": root / "state",
                                      "local_queue_root": root / "production"})()
            with mock.patch("agent_bridge.localq.service.Service", Service):
                self.assertEqual(delegation_verify._local_model_check(cfg)["status"], "complete")
            self.assertNotEqual(observed["root"], cfg.local_queue_root)
            self.assertFalse(observed["root"].exists())

    def test_cleanup_failure_is_not_reported_as_removed(self):
        class FakeQueue:
            def __init__(self, *args, **kwargs): pass
            def submit(self, **kwargs): return {"job_id": "synthetic"}
            def status(self, job_id): return {"state": "complete"}
            def result(self, job_id): return {"state": "complete", "classification": "synthetic", "harness": {"returncode": 0}}
        real_rmtree = delegation_verify.shutil.rmtree
        def fail_repo(path, *args, **kwargs):
            if Path(path).name.startswith("agent-bridge-delegation-verify-"):
                raise OSError("busy")
            return real_rmtree(path, *args, **kwargs)
        with mock.patch.object(delegation_verify, "ExecutionQueue", FakeQueue), \
             mock.patch.object(delegation_verify.shutil, "rmtree", side_effect=fail_repo):
            result = delegation_verify._run_direction(object(), Path("/unused"), caller="codex", provider="claude")
        self.assertFalse(result["worktree_removed"])


class WindowsOnboardingGateTests(unittest.TestCase):
    def test_non_macos_plan_refuses_automatic_execution(self):
        answers = {"version": 1, "directions": "both",
                   "targets": {"codex": True, "claude_code": True, "claude_desktop": False},
                   "privacy": {"mode": "strict", "peers": {}},
                   "local_ollama": {"enabled": False},
                   "automatic_delegation": {"enabled": True}}
        with mock.patch.object(onboard.sys, "platform", "win32"):
            with self.assertRaisesRegex(ValueError, "only on macOS"):
                onboard.plan(answers, "/repo")


if __name__ == "__main__": unittest.main()
