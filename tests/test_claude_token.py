"""Offline contracts for the macOS Claude setup-token adapter."""
from __future__ import annotations

import os
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import sys
from types import SimpleNamespace
from contextlib import redirect_stdout

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge import admin, broker, claude_token, health, runner
from agent_bridge.backends import claude_backend
from agent_bridge.execution import claude_task, hostenv
from agent_bridge.orchestration.harness_probe import HarnessReadinessProbe
from agent_bridge.orchestration.execution_queue import Harnesses, SubprocessHarnessExecutor
from agent_bridge.localq.spool import SubprocessBackend
from harness import Sandbox


class ClaudeTokenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.disable_value = os.environ.pop(claude_token.DISABLE_ENV, None)
        self.addCleanup(self._restore_disable_env)
        self.platform = mock.patch.object(claude_token.sys, "platform", "darwin")
        self.platform.start()
        self.home_patch = mock.patch.object(claude_token, "_bridge_home", return_value=self.home / ".agent-bridge")
        self.home_patch.start()
        # The global pytest guard disables the reader.  These adapter tests
        # deliberately exercise it only while subprocess.run is stubbed.
        self.reader_patch = mock.patch.object(claude_token, "_reader", claude_token._read_keychain)
        self.reader_patch.start()

    def tearDown(self) -> None:
        self.reader_patch.stop(); self.home_patch.stop(); self.platform.stop(); self.temp.cleanup()

    def _restore_disable_env(self) -> None:
        if self.disable_value is not None:
            os.environ[claude_token.DISABLE_ENV] = self.disable_value

    def security(self, value: bytes = b"", code: int = 0):
        return mock.patch.object(claude_token.subprocess, "run",
                                 return_value=subprocess.CompletedProcess([], code, value, b"not exposed"))

    def test_load_is_bounded_scrubbed_and_never_exposes_stderr(self):
        with self.security(b"fake-token-123\n") as run:
            self.assertEqual(claude_token.load(), "fake-token-123")
        _, kwargs = run.call_args
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(kwargs["timeout"], 3)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", kwargs["env"])

    def test_missing_item_quietly_uses_login_but_locked_is_distinguished(self):
        with self.security(code=44):
            self.assertIsNone(claude_token.load())
            self.assertIsNone(claude_token.diagnostic())
        with self.security(code=36):
            self.assertIsNone(claude_token.load())
            self.assertEqual(claude_token.diagnostic(), "keychain_locked")

    def test_malformed_values_are_rejected(self):
        for value in (b"", b"two words", b" token", b"token ", b"line\nbreak", b"\xff"):
            with self.subTest(value=value), self.security(value):
                self.assertIsNone(claude_token.load())
                self.assertEqual(claude_token.diagnostic(), "token_malformed")

    def test_mark_date_never_writes_the_value(self):
        with self.security(b"fake-token-123"):
            self.assertEqual(claude_token.mark_set("2026-11-19"), "2026-11-19")
        path = self.home / ".agent-bridge" / "claude-token.json"
        self.assertEqual(path.read_text(), '{"set_on": "2026-11-19"}')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(claude_token.days_left(claude_token.dt.date(2027, 11, 18)), 1)

    def test_parent_token_is_scrubbed_and_keychain_token_wins(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "parent-token"}, clear=False):
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", runner.scrubbed_env())
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", runner.scrubbed_env(
                {"CLAUDE_CODE_OAUTH_TOKEN": "configured-token"}))
            with self.security(b"keychain-token"):
                self.assertEqual(claude_token.child_auth_env(),
                                 {"CLAUDE_CODE_OAUTH_TOKEN": "keychain-token"})

    def test_common_capture_boundary_redacts_token_echoes(self):
        token = "fake-token-do-not-store"
        result = runner.run(["/bin/sh", "-c", "printf '%s' \"$CLAUDE_CODE_OAUTH_TOKEN\" >&2"],
                            cwd=self.temp.name, env={"PATH": "/usr/bin:/bin", "CLAUDE_CODE_OAUTH_TOKEN": token},
                            stdin_data="", timeout=3, grace=0, stdout_cap=1024, stderr_cap=1024)
        self.assertNotIn(token.encode(), result.stdout + result.stderr)
        self.assertIn(b"[redacted]", result.stderr)
        self.assertEqual(runner.redact_child_output(b"patch " + token.encode(),
                                                    {"CLAUDE_CODE_OAUTH_TOKEN": token}),
                         b"patch [redacted]")


class TestKeychainTestGuard(unittest.TestCase):
    def test_pytest_guard_prevents_a_real_security_spawn(self):
        """Removing conftest's guard makes this test attempt the reader."""
        self.assertIsNot(claude_token._reader, claude_token._read_keychain)
        with mock.patch.object(claude_token.subprocess, "run",
                               side_effect=AssertionError("real security must not run")):
            self.assertIsNone(claude_token.load())


class ClaudeTokenChildIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.job_dir = Path(self.sandbox.root) / "token-isolation-job"
        self.job_dir.mkdir()

    def _worker_env(self) -> dict[str, str]:
        with mock.patch.object(broker.subprocess, "Popen",
                               return_value=SimpleNamespace(pid=4321)) as popen:
            self.assertEqual(broker._spawn_worker(self.sandbox.cfg, str(self.job_dir)), 4321)
        return popen.call_args.kwargs["env"]

    def test_worker_passes_the_disable_switch_only_when_its_parent_has_it(self):
        with mock.patch.dict(os.environ, {claude_token.DISABLE_ENV: "1"}, clear=False):
            enabled = self._worker_env()
        with mock.patch.dict(os.environ, {}, clear=True):
            absent = self._worker_env()
        self.assertEqual(enabled[claude_token.DISABLE_ENV], "1")
        self.assertNotIn(claude_token.DISABLE_ENV, absent)

    @unittest.skipUnless(os.name == "posix", "the subprocess harness executor is POSIX only")
    def test_execution_harness_passes_the_disable_switch_only_when_present(self):
        harness = Path(self.sandbox.root) / "harness.py"
        harness.write_text("import json\nprint(json.dumps({'ok': True, 'status': 'complete'}))\n",
                           encoding="utf-8")
        executor = SubprocessHarnessExecutor(Harnesses(
            codex=harness, claude=harness, python=Path(sys.executable)))
        request = {"provider": "codex", "repo": self.sandbox.root,
                   "brief": str(harness), "base": "HEAD", "classification": "synthetic",
                   "model": "fake", "effort": "low", "verify_argv": [],
                   "timeout_seconds": 1, "verify_timeout_seconds": 1,
                   "cleanup_timeout_seconds": 1, "total_timeout_seconds": 304}

        def observed(parent_env):
            captured = []
            real_popen = subprocess.Popen

            def record(argv, **kwargs):
                captured.append(kwargs["env"])
                return real_popen(argv, **kwargs)

            with mock.patch.dict(os.environ, parent_env, clear=True), \
                 mock.patch("agent_bridge.orchestration.execution_queue.subprocess.Popen",
                            side_effect=record):
                job_dir = Path(tempfile.mkdtemp(dir=self.sandbox.root))
                outcome = executor(request, job_dir)
            self.assertEqual(outcome["returncode"], 0)
            return captured[0]

        enabled = observed({claude_token.DISABLE_ENV: "1", "PATH": os.defpath})
        absent = observed({"PATH": os.defpath})
        self.assertEqual(enabled[claude_token.DISABLE_ENV], "1")
        self.assertNotIn(claude_token.DISABLE_ENV, absent)

    def test_local_bridge_adapter_passes_the_disable_switch_only_when_present(self):
        command = [sys.executable, "-c", "import json; print(json.dumps({'ok': True}))"]

        def observed(parent_env):
            captured = []
            real_popen = subprocess.Popen

            def record(argv, **kwargs):
                captured.append(kwargs["env"])
                return real_popen(argv, **kwargs)

            with mock.patch.dict(os.environ, parent_env, clear=True), \
                 mock.patch("agent_bridge.localq.spool.subprocess.Popen", side_effect=record):
                self.assertEqual(SubprocessBackend(command, bridge_child=True).run(
                    {"job_id": "token-child"}, 5), {"ok": True})
            return captured[0]

        enabled = observed({claude_token.DISABLE_ENV: "1"})
        absent = observed({})
        self.assertEqual(enabled[claude_token.DISABLE_ENV], "1")
        self.assertNotIn(claude_token.DISABLE_ENV, absent)

    @unittest.skipUnless(os.name == "posix", "the subprocess harness executor is POSIX only")
    def test_real_claude_task_child_does_not_run_security_when_disabled(self):
        """Exercise a direct claude_task.py child under the harness's fixed env.

        The copied package leaves the task script itself unchanged, while its
        Keychain command is a harmless logger.  That makes an attempted read
        observable without ever addressing the operator's Keychain.
        """
        root = Path(self.sandbox.root)
        source_root = root / "copied-src"
        shutil.copytree(ROOT / "src" / "agent_bridge", source_root / "agent_bridge")
        security_log = root / "security-called"
        security = root / "fake-security"
        security.write_text(
            f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(security_log)!r}).write_text('called')\n",
            encoding="utf-8")
        security.chmod(0o700)
        token_module = source_root / "agent_bridge" / "claude_token.py"
        token_module.write_text(token_module.read_text(encoding="utf-8")
                                .replace('SECURITY = "/usr/bin/security"',
                                         f"SECURITY = {str(security)!r}")
                                .replace('if sys.platform != "darwin":', 'if False:'),
                                encoding="utf-8")
        task = source_root / "agent_bridge" / "execution" / "claude_task.py"
        home = root / "home"
        store = home / ".agent-bridge" / "claude-home"
        store.mkdir(parents=True, mode=0o700)
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "--quiet"], cwd=repo, check=True, shell=False)
        brief = root / "brief.txt"
        brief.write_text("synthetic", encoding="utf-8")
        fake_bin = root / "bin"
        fake_bin.mkdir()
        fake_claude = fake_bin / "claude"
        fake_claude.write_text(
            f"#!{sys.executable}\nimport json\nprint(json.dumps({{'loggedIn': True, 'authMethod': 'claude.ai', 'subscriptionType': 'max'}}))\n",
            encoding="utf-8")
        fake_claude.chmod(0o700)
        request = {"provider": "claude", "repo": str(repo), "brief": str(brief),
                   "base": "HEAD", "classification": "synthetic", "model": "fake",
                   "effort": "low", "verify_argv": [], "timeout_seconds": 1,
                   "verify_timeout_seconds": 1, "cleanup_timeout_seconds": 1,
                   "total_timeout_seconds": 304}
        with mock.patch.dict(os.environ, {
                "HOME": str(home), "PATH": str(fake_bin) + os.pathsep + os.defpath,
                claude_token.DISABLE_ENV: "1"}, clear=False):
            executor = SubprocessHarnessExecutor(Harnesses(
                codex=task, claude=task, python=Path(sys.executable), claude_config_dir=store))
            executor(request, root)
        self.assertFalse(security_log.exists())

    def test_fresh_child_with_the_worker_environment_never_runs_security(self):
        with mock.patch.dict(os.environ, {claude_token.DISABLE_ENV: "1"}, clear=False):
            env = self._worker_env()
        code = """
from agent_bridge import claude_token
claude_token.sys.platform = 'darwin'
def forbidden(*args, **kwargs):
    raise AssertionError('security must not run')
claude_token.subprocess.run = forbidden
assert claude_token.load() is None
"""
        result = subprocess.run([sys.executable, "-c", code], env=env,
                                capture_output=True, text=True, shell=False, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_without_the_switch_load_still_calls_the_reader(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(claude_token, "_reader", return_value="reader-token") as reader:
            self.assertEqual(claude_token.load(), "reader-token")
        reader.assert_called_once_with()


class ClaudeTokenLaunchTests(unittest.TestCase):
    """Every launch site gets auth only from the replaceable Keychain seam."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.disable_value = os.environ.pop(claude_token.DISABLE_ENV, None)
        self.addCleanup(self._restore_disable_env)
        self.config = self.home / "claude-home"
        self.config.mkdir()

    def _restore_disable_env(self) -> None:
        if self.disable_value is not None:
            os.environ[claude_token.DISABLE_ENV] = self.disable_value

    def _reader(self, value):
        return mock.patch.object(claude_token, "_reader", return_value=value)

    def test_launch_sites_get_only_the_reader_token_and_no_parent_token(self):
        sandbox = Sandbox()
        self.addCleanup(sandbox.cleanup)
        captured = []
        fake_result = runner.RunResult([], 0, b'{"result": {}}', b"", False, 0, None)
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "parent"}, clear=False), \
             self._reader("reader-token"), \
             mock.patch.object(runner, "run", return_value=fake_result) as run, \
             mock.patch.object(claude_task.Path, "home", return_value=self.home):
            claude_backend.run_consultation(sandbox.cfg, prompt="x", schema={},
                                            session_id="s", resume=False,
                                            workspace=self.temp.name)
            captured.append(run.call_args.kwargs["env"])
            captured.append(claude_task._env(self.config))
            captured.append(health.peer_env(sandbox.cfg, "claude"))
            health.auth_status("claude", "/fake", {})
            captured.append(run.call_args.kwargs["env"])
            binary = self.home / "claude"; binary.write_text("x"); binary.chmod(0o700)
            probe = HarnessReadinessProbe({"claude": binary}, home=self.home,
                env={"PATH": "/usr/bin:/bin"}, claude_config_dir=self.config,
                run=lambda argv, **kw: (captured.append(kw["env"]) or
                    SimpleNamespace(returncode=0,
                                    stdout=(b"version" if "--version" in argv else
                                            b'{"loggedIn":true,"authMethod":"claude.ai","subscriptionType":"max"}'),
                                    stderr=b"", spawn_failed=False, timed_out=False,
                                    cap_exceeded=False, descendant_held_pipes=False)))
            with mock.patch("agent_bridge.orchestration.harness_probe.claude_config.is_ready", return_value=True):
                self.assertTrue(probe("claude"))
        for env in captured:
            self.assertEqual(env.get("CLAUDE_CODE_OAUTH_TOKEN"), "reader-token")
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", claude_task._env())

    def test_no_item_leaves_each_launch_environment_unchanged(self):
        sandbox = Sandbox()
        self.addCleanup(sandbox.cleanup)
        seen = []
        fake_result = runner.RunResult([], 0, b'{"loggedIn": false}', b"", False, 0, None)
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "parent"}, clear=False), \
             self._reader(None), mock.patch.object(claude_task.Path, "home", return_value=self.home), \
             mock.patch.object(runner, "run", return_value=fake_result) as run:
            execution = claude_task._env(self.config)
            self.assertEqual(execution["CLAUDE_CONFIG_DIR"], str(self.config))
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", execution)
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", health.peer_env(sandbox.cfg, "claude"))
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", claude_task._env())
            claude_backend.run_consultation(sandbox.cfg, prompt="x", schema={},
                                            session_id="s", resume=False,
                                            workspace=self.temp.name)
            seen.append(run.call_args.kwargs["env"])
            health.auth_status("claude", "/fake", {})
            seen.append(run.call_args.kwargs["env"])
        for env in seen:
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)

    def test_verification_subprocesses_strip_the_token(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); tree = root / "tree"; scratch = root / "scratch"; tree.mkdir()
            result = SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
            env = {"PATH": "/usr/bin:/bin", "CLAUDE_CODE_OAUTH_TOKEN": "reader-token"}
            backend = hostenv.Confinement("test", True, True, True, frozenset({"synthetic"}))
            with mock.patch.object(claude_task.hostenv, "confined_argv", return_value=["fake"]), \
                 mock.patch.object(claude_task, "_run", return_value=result) as run:
                claude_task._netns_confined(["echo"], tree, scratch, env, 1, backend)
                self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", run.call_args.kwargs["env"])
            scratch = root / "sandbox-scratch"
            with mock.patch.object(claude_task, "_run", return_value=result) as run, \
                 mock.patch.object(claude_task, "_sha", return_value="x"):
                claude_task._sandbox_exec_confined(["echo"], tree, scratch, env, 1, backend)
                self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", run.call_args.kwargs["env"])

    def test_echoed_token_never_reaches_bridge_home_or_job_files(self):
        """Exercise the common capture boundary with a real child process."""
        token = "fake-token-must-not-persist"
        bridge_home, job, workspace = self.home / ".agent-bridge", self.home / "job", self.home / "work"
        bridge_home.mkdir(parents=True); job.mkdir(); workspace.mkdir()
        fake = self.home / "fake-claude.py"
        fake.write_text(
            f"#!{sys.executable}\nimport os, pathlib, sys\n"
            "value = os.environ['CLAUDE_CODE_OAUTH_TOKEN']\n"
            "print(value); print(value, file=sys.stderr)\n"
            "pathlib.Path(os.environ['PATCHED_FILE']).write_text(value)\n")
        fake.chmod(0o700)

        class Config:
            def peer(self, _):
                return {"executable": str(fake), "timeout_seconds": 3,
                        "grace_seconds": 0, "max_budget_usd": None}
            def peer_extra_env(self, _):
                return {"PATCHED_FILE": str(workspace / "patched.txt")}
            def peer_reasoning_effort(self, _): return None
            def limit(self, _): return 4096

        with self._reader(token):
            outcome = claude_backend.run_consultation(
                Config(), prompt="synthetic", schema={}, session_id="s", resume=False,
                workspace=str(workspace), attempt_dir=str(job))
        self.assertNotIn(token.encode(), outcome.raw_stdout + outcome.raw_stderr)
        for root in (bridge_home, job):
            for path in root.rglob("*"):
                if path.is_file():
                    self.assertNotIn(token, path.read_text(errors="replace"), path)


class ClaudeTokenStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / ".agent-bridge"
        self.disable_value = os.environ.pop(claude_token.DISABLE_ENV, None)
        self.addCleanup(self._restore_disable_env)
        self.bridge_home = mock.patch.object(claude_token, "_bridge_home", return_value=self.home)
        self.bridge_home.start()
        self.platform = mock.patch.object(claude_token.sys, "platform", "darwin")
        self.platform.start()
        self.addCleanup(self.bridge_home.stop); self.addCleanup(self.platform.stop)
        self.addCleanup(self.temp.cleanup)

    def _restore_disable_env(self) -> None:
        if self.disable_value is not None:
            os.environ[claude_token.DISABLE_ENV] = self.disable_value

    def test_attention_and_admin_status_are_secret_free(self):
        today = claude_token.dt.date.today()
        with mock.patch.object(claude_token, "_reader", return_value=None):
            self.assertIn("refreshable login", claude_token.attention_once_today())
            self.assertIsNone(claude_token.attention_once_today())
        with mock.patch.object(claude_token, "_reader", return_value="fake-secret"):
            claude_token.mark_set((today - claude_token.dt.timedelta(days=335)).isoformat())
            self.assertIn("expires in 30 days", claude_token.attention_line())
            claude_token.mark_set((today - claude_token.dt.timedelta(days=365)).isoformat())
            self.assertIn("has expired", claude_token.attention_line())
            claude_token.clear_date()
            self.assertIsNone(claude_token.attention_line())
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(admin.cmd_claude_token(None, SimpleNamespace(claude_token_command="status")), 0)
            self.assertNotIn("fake-secret", output.getvalue())

    def test_mark_set_requires_an_item_and_clear_date_only_removes_the_date(self):
        with mock.patch.object(claude_token, "_reader", return_value=None):
            with self.assertRaisesRegex(ValueError, "not available"):
                claude_token.mark_set()
        with mock.patch.object(claude_token, "_reader", return_value="fake"):
            with self.assertRaisesRegex(ValueError, "minted-on"):
                claude_token.mark_set("not-a-date")
            claude_token.mark_set("2026-01-02")
        marker = self.home / "unrelated"
        marker.write_text("keep")
        claude_token.clear_date()
        self.assertFalse((self.home / "claude-token.json").exists())
        self.assertEqual(marker.read_text(), "keep")

    def test_health_source_recovery_and_keychain_diagnostics(self):
        sandbox = Sandbox()
        self.addCleanup(sandbox.cleanup)
        valid = {"executable": "/fake", "realpath": "/fake", "observed_version": "ok"}

        def inspect(reader, auth):
            with mock.patch.object(claude_token, "_reader", side_effect=reader), \
                 mock.patch.object(health.preflight, "discover_executable", return_value=None), \
                 mock.patch.object(health.preflight, "check_peer", return_value=valid), \
                 mock.patch.object(health, "auth_status", return_value=auth):
                return health.inspect_peer(sandbox.cfg, "claude")

        locked = lambda: (setattr(claude_token, "_last_issue", "keychain_locked") or None)
        report = inspect(locked, {"state": "not_signed_in", "network_verified": False,
                                  "credential_source": "login"})
        self.assertEqual(report["credential_source"], "login")
        self.assertIn("locked", report["keychain_diagnostic"])
        denied = lambda: (setattr(claude_token, "_last_issue", "keychain_access_denied") or None)
        self.assertIn("denied", inspect(denied, {"state": "unknown", "network_verified": False,
                                                   "credential_source": "login"})["keychain_diagnostic"])
        absent = inspect(lambda: None, {"state": "unknown", "network_verified": False,
                                        "credential_source": "login"})
        self.assertNotIn("keychain_diagnostic", absent)
        rejected = inspect(lambda: "fake", {"state": "not_signed_in", "network_verified": False,
                                              "credential_source": "setup_token"})
        self.assertEqual(rejected["credential_source"], "setup_token")
        self.assertIn("rejected or expired", rejected["recovery"])


if __name__ == "__main__":
    unittest.main()
