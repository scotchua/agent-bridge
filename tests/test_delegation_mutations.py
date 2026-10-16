"""Offline regression tests for delegation behavior missed by the first mutation sweep."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from agent_bridge.orchestration import delegation  # noqa: E402
from test_automatic_delegation import evidence  # noqa: E402


class PortablePythonMutationTests(unittest.TestCase):
    def test_windowsapps_name_on_posix_is_not_rejected(self):
        with mock.patch.object(delegation.os, "name", "posix"), \
             mock.patch.object(delegation.os.path, "realpath", return_value="/invented/WindowsApps/python"):
            self.assertEqual(delegation._portable_python(), "/invented/WindowsApps/python")

    def test_windowsapps_alias_on_windows_is_refused(self):
        with mock.patch.object(delegation.os, "name", "nt"), \
             mock.patch.object(delegation.os.path, "realpath", return_value="C:/invented/WindowsApps/python.exe"):
            with self.assertRaises(delegation.DelegationConfigError):
                delegation._portable_python()


class BuildConfigMutationTests(unittest.TestCase):
    def test_default_poll_interval_is_five_seconds(self):
        with tempfile.TemporaryDirectory() as home:
            cfg = delegation.build_config(home, str(ROOT), local_worker_executable=None)
        self.assertEqual(cfg["interval_seconds"], 5.0)


class RenderConfigBytesMutationTests(unittest.TestCase):
    def test_config_render_uses_two_space_indent(self):
        cfg = {"invented_account": "cedar"}
        expected = (json.dumps(cfg, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
        self.assertEqual(delegation.render_config_bytes(cfg), expected)


class ConfigSha256MutationTests(unittest.TestCase):
    def test_hash_matches_rendered_bytes(self):
        cfg = {"invented_company": "Larch Works"}
        expected = hashlib.sha256(delegation.render_config_bytes(cfg)).hexdigest()
        self.assertEqual(delegation.config_sha256(cfg), expected)


class HarnessAvailabilityMutationTests(unittest.TestCase):
    def test_missing_python_disables_both_execution_verdicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex = Path(tmp) / "codex_task.py"
            claude = Path(tmp) / "claude_task.py"
            codex.write_text("")
            claude.write_text("")
            cfg = {"python_executable": str(Path(tmp) / "absent-python"),
                   "codex_task_executable": str(codex),
                   "claude_task_executable": str(claude),
                   "claude_config_dir": str(Path(tmp) / "claude"),
                   "worker_executable": str(Path(tmp) / delegation.NO_WORKER_SENTINEL)}
            with mock.patch.object(delegation.claude_config, "is_ready", return_value=True):
                status = delegation.harness_availability(cfg)
        self.assertFalse(status["python"])
        self.assertFalse(status["execution_complete"])
        self.assertFalse(status["claude_lane_ready"])


class PlatformBoundaryMutationTests(unittest.TestCase):
    def test_windows_prefix_is_reported_as_windows(self):
        self.assertEqual(delegation.platform_boundary_report("win32")["platform"], "windows")


class ValidateEvidenceMutationTests(unittest.TestCase):
    def test_empty_required_directions_is_refused(self):
        with tempfile.TemporaryDirectory() as home:
            cfg = delegation.build_config(home, str(ROOT), local_worker_executable=None)
            with self.assertRaises(delegation.DelegationVerificationError):
                delegation.validate_evidence(evidence(cfg), cfg, required_directions=(),
                                             local_worker_required=False)

    def test_complete_synthetic_local_model_is_accepted_when_required(self):
        with tempfile.TemporaryDirectory() as home:
            cfg = delegation.build_config(home, str(ROOT), local_worker_executable=None)
            result = delegation.validate_evidence(
                evidence(cfg, local_model={"status": "complete", "source_classification": "synthetic"}),
                cfg, required_directions=("codex->claude",), local_worker_required=True)
        self.assertEqual(result["local_model"], "enabled")
        self.assertEqual(result["overall"], "enabled")


class RequiredDirectionsMutationTests(unittest.TestCase):
    def test_caller_order_maps_to_opposite_provider(self):
        self.assertEqual(delegation.required_directions_for(("claude", "codex")),
                         ("claude->codex", "codex->claude"))


class LaunchAgentPathMutationTests(unittest.TestCase):
    def test_path_is_under_user_launchagents(self):
        with tempfile.TemporaryDirectory() as home:
            expected = os.path.join(home, "Library", "LaunchAgents",
                                    delegation.LAUNCH_AGENT_LABEL + ".plist")
            self.assertEqual(delegation.launch_agent_path(home), expected)


class RenderLaunchAgentMutationTests(unittest.TestCase):
    def test_empty_account_is_refused(self):
        with self.assertRaises(delegation.DelegationConfigError):
            delegation.render_launch_agent(
                worker_binary="/invented/worker", config_path="/invented/config.json",
                python_executable="/invented/python", account="",
                claude_bin_dir="/invented/bin", stdout_log="/invented/out.log",
                stderr_log="/invented/err.log")


class LaunchctlMutationTests(unittest.TestCase):
    def test_default_timeout_is_twenty_seconds(self):
        with mock.patch.object(delegation.subprocess, "run") as run:
            delegation._launchctl(["print", "gui/501/invented"])
        run.assert_called_once_with(["launchctl", "print", "gui/501/invented"],
                                    capture_output=True, timeout=20.0,
                                    check=False, shell=False)


class LaunchAgentStatusMutationTests(unittest.TestCase):
    def test_non_darwin_status_is_unsupported(self):
        with mock.patch.object(delegation.sys, "platform", "linux"):
            self.assertEqual(delegation.launch_agent_status("/invented/home"),
                             {"status": "unsupported_platform"})

    def test_probe_returncode_controls_active_status(self):
        with mock.patch.object(delegation.sys, "platform", "darwin"), \
             mock.patch.object(delegation.os, "getuid", return_value=501, create=True), \
             mock.patch.object(delegation, "_launchctl") as launchctl:
            launchctl.return_value.returncode = 0
            self.assertEqual(delegation.launch_agent_status("/invented/home")["status"], "active")
            launchctl.return_value.returncode = 1
            self.assertEqual(delegation.launch_agent_status("/invented/home")["status"], "inactive")


class ActivateLaunchAgentMutationTests(unittest.TestCase):
    def test_successful_bootstrap_reports_activated(self):
        with tempfile.TemporaryDirectory() as tmp:
            plist = Path(tmp) / "invented.plist"
            plist.write_text("<plist/>")
            with mock.patch.object(delegation.sys, "platform", "darwin"), \
                 mock.patch.object(delegation.os, "getuid", return_value=501, create=True), \
                 mock.patch.object(delegation, "_launchctl") as launchctl:
                launchctl.side_effect = [mock.Mock(returncode=1), mock.Mock(returncode=0)]
                result = delegation.activate_launch_agent(str(plist), apply=True)
        self.assertEqual(result["status"], "activated")
        self.assertEqual(launchctl.call_count, 2)


class DeactivateLaunchAgentMutationTests(unittest.TestCase):
    def test_inactive_agent_returns_without_bootout(self):
        with mock.patch.object(delegation.sys, "platform", "darwin"), \
             mock.patch.object(delegation.os, "getuid", return_value=501, create=True), \
             mock.patch.object(delegation, "_launchctl", return_value=mock.Mock(returncode=1)) as launchctl:
            result = delegation.deactivate_launch_agent("/invented/agent.plist", apply=True)
        self.assertEqual(result["status"], "already_inactive")
        launchctl.assert_called_once()

    def test_active_agent_is_staged_without_apply(self):
        with mock.patch.object(delegation.sys, "platform", "darwin"), \
             mock.patch.object(delegation.os, "getuid", return_value=501, create=True), \
             mock.patch.object(delegation, "_launchctl", return_value=mock.Mock(returncode=0)) as launchctl:
            result = delegation.deactivate_launch_agent("/invented/agent.plist", apply=False)
        self.assertEqual(result["status"], "staged")
        launchctl.assert_called_once()

    def test_failed_bootout_is_reported(self):
        with mock.patch.object(delegation.sys, "platform", "darwin"), \
             mock.patch.object(delegation.os, "getuid", return_value=501, create=True), \
             mock.patch.object(delegation, "_launchctl") as launchctl:
            launchctl.side_effect = [mock.Mock(returncode=0),
                                     mock.Mock(returncode=9, stderr=b"invented failure")]
            result = delegation.deactivate_launch_agent("/invented/agent.plist", apply=True)
        self.assertEqual(result["status"], "deactivation_failed")
        self.assertEqual(result["returncode"], 9)


if __name__ == "__main__":
    unittest.main()
