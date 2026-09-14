import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.execution.claude_task import TaskError, _command, _env, _remove, _run, _sandboxed, run_task


class ClaudeTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.repo, check=True)
        (self.repo / "value.txt").write_text("before\n")
        subprocess.run(["git", "add", "value.txt"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.repo, check=True)
        self.brief = self.root / "brief.md"
        self.brief.write_text("Replace the fixture value.\n")
        self.fake = self.root / "claude"
        self.fake.write_text("#!/bin/sh\ncase \"$*\" in *\"auth status\"*) printf '{\"loggedIn\":true,\"authMethod\":\"claude.ai\",\"subscriptionType\":\"team\"}\\n'; exit;; esac\nprintf 'after\\n' > value.txt\nprintf '{\"result\":\"done\",\"is_error\":false}\\n'\n")
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IXUSR)

    def tearDown(self):
        self.temp.cleanup()

    def test_current_cli_command_keeps_the_bounded_tool_set(self):
        argv = _command(self.fake, "sonnet", "high")
        self.assertIn("--safe-mode", argv)
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob,Edit,Write")
        self.assertNotIn("Bash", argv)
        self.assertNotIn("--restricted", argv)
        self.assertNotIn("--permission-prompts", argv)

    def test_returns_patch_and_does_not_touch_source(self):
        result = run_task(brief=self.brief, repo=self.repo,
                          task_root=self.root / "tasks", claude_bin=self.fake,
                          classification="synthetic", model="fake", effort="low",
                          verify_argv=[["git", "diff", "--check"]])
        error_log = Path(result["job_dir"]) / "verify-1.stderr"
        self.assertEqual(result["status"], "complete", {"receipt": result, "stderr": error_log.read_text() if error_log.exists() else None})
        self.assertEqual((self.repo / "value.txt").read_text(), "before\n")
        patch = Path(result["patch"]).read_text()
        self.assertIn("+after", patch)
        receipt = json.loads((Path(result["job_dir"]) / "receipt.json").read_text())
        self.assertFalse(receipt["permission_to_land"])
        self.assertEqual(receipt["verification"][0]["returncode"], 0)
        self.assertTrue(receipt["fresh_worktree_patch_match"])
        self.assertFalse((Path(result["job_dir"]) / "generation-worktree").exists())
        self.assertFalse((Path(result["job_dir"]) / "verification-worktree").exists())

    def test_refuses_client_material_before_dispatch(self):
        with self.assertRaises(TaskError):
            run_task(brief=self.brief, repo=self.repo,
                     task_root=self.root / "tasks", claude_bin=self.fake,
                     classification="client_derived", model="fake", effort="low",
                     verify_argv=[["git", "diff", "--check"]])
        self.assertFalse((self.root / "tasks").exists())

    def test_refuses_shell_and_missing_verification(self):
        for commands in ([], [["sh", "-c", "touch escaped"]], [["python3", "-c", "print(1)"]]):
            with self.subTest(commands=commands), self.assertRaises(TaskError):
                run_task(brief=self.brief, repo=self.repo, task_root=self.root / "tasks",
                         claude_bin=self.fake, classification="synthetic", model="fake",
                         effort="low", verify_argv=commands)

    def test_refuses_api_auth(self):
        self.fake.write_text("#!/bin/sh\ncase \"$*\" in *\"auth status\"*) printf '{\"loggedIn\":true,\"authMethod\":\"api_key\",\"subscriptionType\":\"team\"}\\n'; exit;; esac\n")
        with self.assertRaises(TaskError):
            run_task(brief=self.brief, repo=self.repo, task_root=self.root / "tasks",
                     claude_bin=self.fake, classification="synthetic", model="fake",
                     effort="low", verify_argv=[["git", "diff", "--check"]])
        receipt_path = next((self.root / "tasks").glob("*/receipt.json"))
        receipt = json.loads(receipt_path.read_text())
        self.assertEqual(receipt["status"], "failed")
        self.assertTrue(receipt["cleanup"]["generation_removed"])
        self.assertTrue(receipt["cleanup"]["verification_removed"])
        self.assertTrue(receipt["source_integrity_match"])

    def test_refuses_invalid_json_and_empty_patch(self):
        for output in ("not-json", '{\"result\":\"done\",\"is_error\":false}'):
            with self.subTest(output=output):
                self.fake.write_text("#!/bin/sh\ncase \"$*\" in *\"auth status\"*) printf '{\"loggedIn\":true,\"authMethod\":\"claude.ai\",\"subscriptionType\":\"team\"}\\n'; exit;; esac\nprintf '%s\\n' '" + output + "'\n")
                with self.assertRaises(TaskError):
                    run_task(brief=self.brief, repo=self.repo, task_root=self.root / ("tasks-" + str(len(output))),
                             claude_bin=self.fake, classification="synthetic", model="fake",
                             effort="low", verify_argv=[["git", "diff", "--check"]])

    def test_environment_does_not_inherit_api_credentials_or_git_config(self):
        old = {k: os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "GIT_CONFIG_GLOBAL")}
        try:
            os.environ["ANTHROPIC_API_KEY"] = "secret"
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "secret"
            os.environ["GIT_CONFIG_GLOBAL"] = "/tmp/hostile"
            env = _env()
            self.assertNotIn("ANTHROPIC_API_KEY", env)
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
            self.assertEqual(env["GIT_CONFIG_GLOBAL"], "/dev/null")
        finally:
            for key, value in old.items():
                if value is None: os.environ.pop(key, None)
                else: os.environ[key] = value

    def test_timeout_kills_descendant_process_group(self):
        marker = self.root / "escaped"
        program = ("import subprocess,time,sys; "
                   "subprocess.Popen([sys.executable,'-c',"
                   f"\"import time,pathlib; time.sleep(.6); pathlib.Path({str(marker)!r}).write_text('bad')\"]); "
                   "time.sleep(10)")
        with self.assertRaises(TaskError):
            _run([sys.executable, "-c", program], cwd=self.root, env=_env(), timeout=0.1)
        time.sleep(0.8)
        self.assertFalse(marker.exists())

    def test_macos_sandbox_denies_network_and_outside_write(self):
        tree = self.root / "sandbox-tree"
        tree.mkdir()
        scratch = self.root / "sandbox-scratch"
        outside = self.root / "outside"
        code = ("import pathlib,socket; "
                f"pathlib.Path({str(outside)!r}).write_text('escape'); "
                "socket.socket().connect(('127.0.0.1',9))")
        result = _sandboxed([sys.executable, "-c", code], tree, scratch, _env(), 5)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(outside.exists())

    def test_macos_sandbox_denies_outside_read(self):
        tree = self.root / "read-tree"; tree.mkdir()
        protected = self.root / "client-secret"; protected.write_text("canary-secret")
        result = _sandboxed([sys.executable, "-c", f"print(open({str(protected)!r}).read())"],
                            tree, self.root / "read-scratch", _env(), 5)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(b"canary-secret", result.stdout + result.stderr)

    def test_cleanup_timeout_is_bounded_failure(self):
        tree = self.root / "cleanup-tree"; tree.mkdir()
        timed = SimpleNamespace(spawn_failed=False, timed_out=True, cap_exceeded=False,
                                descendant_held_pipes=False, returncode=None, stdout=b"", stderr=b"")
        with mock.patch("agent_bridge.execution.claude_task.runner.run", return_value=timed):
            self.assertFalse(_remove(self.repo, tree, _env()))

    def test_detects_source_checkout_mutation(self):
        self.fake.write_text("#!/bin/sh\ncase \"$*\" in *\"--version\"*) echo fake; exit;; *\"auth status\"*) printf '{\"loggedIn\":true,\"authMethod\":\"claude.ai\",\"subscriptionType\":\"team\"}\\n'; exit;; esac\nprintf 'corrupt\\n' > " + str(self.repo / "value.txt") + "\nprintf 'after\\n' > value.txt\nprintf '{\"result\":\"done\",\"is_error\":false}\\n'\n")
        with self.assertRaises(TaskError):
            run_task(brief=self.brief, repo=self.repo, task_root=self.root / "tasks",
                     claude_bin=self.fake, classification="synthetic", model="fake", effort="low",
                     verify_argv=[["git", "diff", "--check"]])

    def test_refuses_ignored_untracked_model_output(self):
        (self.repo / ".gitignore").write_text("ignored.tmp\n")
        subprocess.run(["git", "add", ".gitignore"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "ignore fixture"], cwd=self.repo, check=True)
        self.fake.write_text("#!/bin/sh\ncase \"$*\" in *\"--version\"*) echo fake; exit;; *\"auth status\"*) printf '{\"loggedIn\":true,\"authMethod\":\"claude.ai\",\"subscriptionType\":\"team\"}\\n'; exit;; esac\nprintf hidden > ignored.tmp\nprintf 'after\\n' > value.txt\nprintf '{\"result\":\"done\",\"is_error\":false}\\n'\n")
        with self.assertRaises(TaskError):
            run_task(brief=self.brief, repo=self.repo, task_root=self.root / "tasks",
                     claude_bin=self.fake, classification="synthetic", model="fake", effort="low",
                     verify_argv=[["git", "diff", "--check"]])


if __name__ == "__main__":
    unittest.main()
