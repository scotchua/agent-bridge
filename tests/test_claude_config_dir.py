"""The Claude execution lane's credential store.

One directory, and only one. These tests cover the four ways that has gone
wrong: the shared ``~/.claude`` store used directly, a link or alias that
resolves to it, some other directory somebody configured, and the right
directory left readable by other accounts.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge.execution import claude_config as cc
from agent_bridge.orchestration import delegation
from agent_bridge.orchestration.execution_queue import (
    ExecutionAdmissionError,
    Harnesses,
    SubprocessHarnessExecutor,
)
import platform_support


class ConfigDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.canonical = cc.canonical_config_dir(self.home)
        self.canonical.mkdir(mode=0o700, parents=True)
        if os.name == "nt":
            # A store the lane created and protected, not one mkdir happened
            # to leave usable. Python 3.13's mkdir(0o700) writes an explicit
            # owner-only ACL on Windows; 3.11's leaves only inherited entries,
            # which the read-back refuses by design. Tighten it the way the
            # lane does so the fixture means the same thing on both.
            cc.enforce_private(self.canonical)
        self.shared = cc.shared_store_dir(self.home)
        self.shared.mkdir(mode=0o700)


class CanonicalDirectoryTests(ConfigDirTestCase):
    def test_the_lane_s_own_store_is_accepted(self):
        self.assertEqual(cc.checked_config_dir(self.canonical, home=self.home),
                         self.canonical)

    def test_the_shared_desktop_store_is_refused_by_name(self):
        with self.assertRaises(cc.ConfigDirError) as caught:
            cc.assert_canonical(self.shared, home=self.home)
        self.assertIn("~/.claude", str(caught.exception))

    def test_a_symlink_that_resolves_to_the_shared_store_is_refused(self):
        """String equality would pass this, which is the whole problem."""
        platform_support.require_symlinks(self)
        alias = self.home / "alias-home"
        alias.symlink_to(self.shared, target_is_directory=True)
        with self.assertRaises(cc.ConfigDirError) as caught:
            cc.assert_canonical(alias, home=self.home)
        self.assertIn("~/.claude", str(caught.exception))

    def test_the_canonical_name_pointing_at_the_shared_store_is_refused(self):
        platform_support.require_symlinks(self)
        self.canonical.rmdir()
        self.canonical.symlink_to(self.shared, target_is_directory=True)
        with self.assertRaises(cc.ConfigDirError) as caught:
            cc.assert_canonical(self.canonical, home=self.home)
        self.assertIn("~/.claude", str(caught.exception))

    def test_a_link_to_the_canonical_directory_is_still_refused(self):
        """What a link points at can change between the check and the login."""
        platform_support.require_symlinks(self)
        alias = self.home / "also-fine"
        alias.symlink_to(self.canonical, target_is_directory=True)
        with self.assertRaises(cc.ConfigDirError) as caught:
            cc.assert_canonical(alias, home=self.home)
        self.assertIn("link or alias", str(caught.exception))

    def test_an_arbitrary_configured_directory_is_refused(self):
        other = self.home / "somewhere-else"
        other.mkdir(mode=0o700)
        with self.assertRaises(cc.ConfigDirError) as caught:
            cc.assert_canonical(other, home=self.home)
        self.assertIn("claude-home", str(caught.exception))

    def test_a_missing_directory_is_refused(self):
        self.canonical.rmdir()
        with self.assertRaises(cc.ConfigDirError) as caught:
            cc.assert_canonical(self.canonical, home=self.home)
        self.assertIn("does not exist", str(caught.exception))

    def test_an_unset_directory_is_refused_rather_than_defaulted(self):
        with self.assertRaises(cc.ConfigDirError) as caught:
            cc.assert_canonical(None, home=self.home)
        self.assertIn("not configured", str(caught.exception))

    def test_a_relative_path_is_refused(self):
        with self.assertRaises(cc.ConfigDirError):
            cc.assert_canonical("claude-home", home=self.home)

    def test_a_file_where_the_directory_belongs_is_refused(self):
        self.canonical.rmdir()
        self.canonical.write_text("not a directory", encoding="utf-8")
        with self.assertRaises(cc.ConfigDirError) as caught:
            cc.assert_canonical(self.canonical, home=self.home)
        self.assertIn("not a directory", str(caught.exception))

    def test_every_refusal_is_fixed_text_that_names_no_path(self):
        self.canonical.rmdir()
        for call in (lambda: cc.assert_canonical(None, home=self.home),
                     lambda: cc.assert_canonical(self.shared, home=self.home),
                     lambda: cc.assert_canonical(self.canonical, home=self.home)):
            with self.assertRaises(cc.ConfigDirError) as caught:
                call()
            self.assertNotIn(str(self.home), str(caught.exception))


class PermissionTests(ConfigDirTestCase):
    def test_a_permissive_directory_is_tightened_and_then_verified(self):
        platform_support.make_permissive(self.canonical)
        cc.checked_config_dir(self.canonical, home=self.home)
        platform_support.assert_owner_only(self, self.canonical, 0o700)

    def test_a_permissive_credential_file_is_tightened(self):
        secret = self.canonical / ".credentials.json"
        secret.write_text("{}", encoding="utf-8")
        platform_support.make_permissive(secret)
        cc.checked_config_dir(self.canonical, home=self.home)
        platform_support.assert_owner_only(self, secret, 0o600,
                                           inside_protected_store=True)

    def test_a_permissive_nested_file_is_tightened_on_windows(self):
        """Codex review of d9e93ab, F2: Windows grants every account "bypass
        traverse checking", so a nested file with its own permissive entry is
        readable by name whatever its parents allow. Enforcement covers the
        tree there. (POSIX stops at the top level on purpose: a 0700
        directory cannot be traversed, so nothing below it is reachable.)"""
        if os.name != "nt":
            self.skipTest("POSIX confidentiality rests on the 0700 directory")
        nested = self.canonical / "projects" / "deep"
        nested.mkdir(parents=True)
        secret = nested / "session.jsonl"
        secret.write_text("{}", encoding="utf-8")
        platform_support.make_permissive(secret)
        platform_support.assert_not_owner_only(self, secret)
        cc.checked_config_dir(self.canonical, home=self.home)
        platform_support.assert_owner_only(self, secret, 0o600,
                                           inside_protected_store=True)
        # The directory carried only entries inherited from the protected
        # root, which is owner-only already; enforcement leaves such an
        # object alone rather than rewriting what is already right.
        platform_support.assert_owner_only(self, nested, 0o700,
                                           inside_protected_store=True)
        self.assertTrue(cc.is_ready(self.canonical, home=self.home))

    def test_readiness_sees_a_permissive_nested_file_on_windows(self):
        if os.name != "nt":
            self.skipTest("POSIX confidentiality rests on the 0700 directory")
        nested = self.canonical / "todos"
        nested.mkdir()
        secret = nested / "todo.json"
        secret.write_text("{}", encoding="utf-8")
        self.assertTrue(cc.is_ready(self.canonical, home=self.home))
        platform_support.make_permissive(secret)
        self.assertFalse(cc.is_ready(self.canonical, home=self.home))
        # And describing did not repair it.
        platform_support.assert_not_owner_only(self, secret)

    def test_files_the_lane_creates_after_enforcement_are_still_ready(self):
        """Claude itself writes into the store between enforcements. On
        Windows those objects carry only entries inherited from the
        protected store; readiness must accept them or the lane would refuse
        every job after its first."""
        if os.name != "nt":
            self.skipTest("inherited ACL entries are a Windows concept")
        later = self.canonical / "shell-snapshots"
        later.mkdir()
        (later / "snap.sh").write_text("#", encoding="utf-8")
        self.assertTrue(cc.is_ready(self.canonical, home=self.home))

    def test_an_owned_unreadable_directory_is_repaired_on_posix(self):
        """Codex review of d9e93ab, F3: listing before chmod failed exactly
        the case enforcement exists to repair."""
        if os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0):
            self.skipTest("mode bits, as a non-root account")
        os.chmod(self.canonical, 0o000)
        self.addCleanup(os.chmod, self.canonical, 0o700)
        cc.enforce_private(self.canonical)
        platform_support.assert_owner_only(self, self.canonical, 0o700)

    def test_a_link_inside_the_store_is_refused(self):
        platform_support.require_symlinks(self)
        (self.canonical / "leak").symlink_to(self.shared)
        with self.assertRaises(cc.ConfigDirError) as caught:
            cc.enforce_private(self.canonical)
        self.assertIn("link or alias", str(caught.exception))

    def test_readiness_reports_a_permissive_directory_as_not_ready(self):
        self.assertTrue(cc.is_ready(self.canonical, home=self.home))
        platform_support.make_permissive(self.canonical)
        self.assertFalse(cc.is_ready(self.canonical, home=self.home))

    def test_readiness_never_changes_anything(self):
        """Describing a machine must not modify it."""
        platform_support.make_permissive(self.canonical)
        cc.is_ready(self.canonical, home=self.home)
        platform_support.assert_not_owner_only(self, self.canonical, 0o755)

    def test_readiness_is_false_for_the_shared_store(self):
        self.assertFalse(cc.is_ready(self.shared, home=self.home))

    def test_readiness_is_false_for_a_missing_or_empty_value(self):
        for value in (None, "", 17, self.home / "absent"):
            self.assertFalse(cc.is_ready(value, home=self.home), repr(value))


class ReadinessGateTests(ConfigDirTestCase):
    """The installer's report must not call the lane ready when it is not."""

    def _availability(self, **overrides):
        cfg = {"python_executable": os.path.realpath(sys.executable),
               "codex_task_executable": str(ROOT / "src" / "agent_bridge" /
                                            "execution" / "codex_task.py"),
               "claude_task_executable": str(ROOT / "src" / "agent_bridge" /
                                             "execution" / "claude_task.py"),
               "claude_config_dir": str(cc.canonical_config_dir()),
               "worker_executable": os.path.realpath(sys.executable)}
        cfg.update(overrides)
        return delegation.harness_availability(cfg)

    def test_the_store_is_reported_separately_from_the_shipped_files(self):
        report = self._availability(claude_config_dir=None)
        self.assertTrue(report["execution_complete"])
        self.assertFalse(report["claude_config_dir_ready"])
        self.assertFalse(report["claude_lane_ready"])

    def test_a_readiness_mismatch_cannot_hide_behind_execution_complete(self):
        """Shipped files present, store absent: the lane is not ready."""
        report = self._availability(claude_config_dir="/nonexistent/claude-home")
        self.assertTrue(report["execution_complete"])
        self.assertFalse(report["claude_lane_ready"])

    def test_the_shared_store_is_never_reported_ready(self):
        report = self._availability(claude_config_dir=str(Path.home() / ".claude"))
        self.assertFalse(report["claude_config_dir_ready"])

    def test_guided_setup_writes_the_canonical_store(self):
        config = delegation.build_config(str(self.home), str(ROOT),
                                         local_worker_executable=None)
        self.assertEqual(config["claude_config_dir"], str(self.canonical))


class DispatchTests(ConfigDirTestCase):
    """The queue refuses to dispatch Claude against the wrong store."""

    def _harnesses(self, directory):
        return Harnesses(
            codex=ROOT / "src" / "agent_bridge" / "execution" / "codex_task.py",
            claude=ROOT / "src" / "agent_bridge" / "execution" / "claude_task.py",
            python=Path(sys.executable), claude_config_dir=directory)

    def test_construction_refuses_a_store_that_is_not_the_lane_s(self):
        with self.assertRaises(ExecutionAdmissionError) as caught:
            SubprocessHarnessExecutor(self._harnesses(self.shared))
        self.assertIn("claude_config_dir_unavailable", str(caught.exception))

    def test_construction_refuses_a_permissive_store(self):
        real = cc.canonical_config_dir()
        if not real.is_dir():
            self.skipTest("no canonical store on this machine")
        SubprocessHarnessExecutor(self._harnesses(real))

    def test_a_claude_dispatch_without_a_store_refuses_before_spawning(self):
        executor = SubprocessHarnessExecutor(self._harnesses(None))
        request = {"provider": "claude", "brief": "/tmp/brief", "repo": "/tmp/repo",
                   "base": "HEAD", "timeout_seconds": 60, "classification": "synthetic",
                   "model": "sonnet", "effort": "low", "verify_argv": []}
        with self.assertRaises(ExecutionAdmissionError) as caught:
            executor(request, Path(self.temp.name))
        self.assertIn("claude_config_dir_unavailable", str(caught.exception))


class HarnessWiringTests(ConfigDirTestCase):
    """The harness itself, without running a model."""

    def test_the_environment_carries_the_selector_and_no_token(self):
        from agent_bridge.execution import claude_task

        env = claude_task._env(self.canonical)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.canonical))
        for key, value in env.items():
            self.assertNotIn("token", key.lower())
            self.assertNotIn("TOKEN", value)

    def test_no_selector_is_set_when_none_is_supplied(self):
        from agent_bridge.execution import claude_task

        self.assertNotIn("CLAUDE_CONFIG_DIR", claude_task._env())

    def test_the_harness_refuses_the_shared_store_with_safe_detail(self):
        from agent_bridge.execution import claude_task

        with self.assertRaises(claude_task.TaskError) as caught:
            claude_task._checked_config_dir(Path.home() / ".claude")
        self.assertIn("Claude configuration directory", str(caught.exception))

    def test_verification_subprocesses_never_see_the_selector(self):
        """Project code under verification has no use for a credential store."""
        source = (ROOT / "src" / "agent_bridge" / "execution" /
                  "claude_task.py").read_text(encoding="utf-8")
        marker = 'sandbox_env.pop("CLAUDE_CONFIG_DIR",None)'
        self.assertIn(marker, source)
        self.assertLess(source.index("sandbox_env={"), source.index(marker))

    def test_the_codex_lane_keeps_its_store_out_of_verification_too(self):
        source = (ROOT / "src" / "agent_bridge" / "execution" /
                  "codex_task.py").read_text(encoding="utf-8")
        self.assertIn('sandbox_env.pop("CODEX_HOME",None)', source)

    def test_the_cli_has_no_default_pointing_at_the_shared_store(self):
        source = (ROOT / "src" / "agent_bridge" / "execution" /
                  "claude_task.py").read_text(encoding="utf-8")
        self.assertNotIn('default=Path.home()/".claude"', source)
        self.assertIn("--claude-config-dir", source)


if __name__ == "__main__":
    unittest.main()
