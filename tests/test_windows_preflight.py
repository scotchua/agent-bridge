"""Tests for the read-only Windows preflight collector.

All subprocess calls are mocked; nothing here spawns a real process or
requires a live Windows host. These tests assert argv shape, shell=False,
env scrubbing, timeout bounds, and fail-closed behavior for every
missing/failing/malformed input, plus a full success path that reports
only ``prerequisites_ready`` (never "delegation enabled/verified").
"""

from __future__ import annotations

import subprocess
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge.orchestration import windows_preflight as wp


VALID_MANIFEST_RAW = {
    "schema_version": 1,
    "distro_release": "22.04.3",
    "rootfs_sha256": "a" * 64,
    "node_version": "20.11.1",
    "claude_version": "1.2.3",
    "codex_version": "0.9.0",
}


def _completed(stdout: bytes, returncode: int = 0):
    return subprocess.CompletedProcess(args=["x"], returncode=returncode, stdout=stdout, stderr=b"")


class RunArgvTests(unittest.TestCase):
    def test_uses_shell_false_capture_timeout_and_scrubbed_env(self):
        with mock.patch("agent_bridge.orchestration.windows_preflight.subprocess.run") as run:
            run.return_value = _completed(b"ok")
            wp._run_argv(["cmd.exe", "/c", "ver"])
            _, kwargs = run.call_args
            self.assertEqual(run.call_args.args[0], ["cmd.exe", "/c", "ver"])
            self.assertIs(kwargs["shell"], False)
            self.assertTrue(kwargs["capture_output"])
            self.assertEqual(kwargs["timeout"], wp.COMMAND_TIMEOUT_SECONDS)
            self.assertEqual(kwargs["env"], wp._MINIMAL_ENV)
            self.assertNotIn("PYTHONPATH", kwargs["env"])

    def test_rejects_invalid_argv_shapes(self):
        self.assertFalse(wp._run_argv([]).ok)
        self.assertFalse(wp._run_argv("cmd.exe").ok)
        self.assertFalse(wp._run_argv([1, 2]).ok)

    def test_missing_executable_is_explicit_failure(self):
        with mock.patch(
            "agent_bridge.orchestration.windows_preflight.subprocess.run",
            side_effect=FileNotFoundError(),
        ):
            outcome = wp._run_argv(["nope.exe"])
            self.assertFalse(outcome.ok)
            self.assertIn("not found", outcome.detail)

    def test_timeout_is_explicit_failure(self):
        with mock.patch(
            "agent_bridge.orchestration.windows_preflight.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="x", timeout=10),
        ):
            outcome = wp._run_argv(["wsl.exe", "--version"])
            self.assertFalse(outcome.ok)
            self.assertIn("timed out", outcome.detail)

    def test_nonzero_exit_is_explicit_failure(self):
        with mock.patch("agent_bridge.orchestration.windows_preflight.subprocess.run") as run:
            run.return_value = _completed(b"", returncode=1)
            outcome = wp._run_argv(["cmd.exe"])
            self.assertFalse(outcome.ok)
            self.assertIn("exited with code 1", outcome.detail)

    def test_decode_failure_is_explicit_failure(self):
        with mock.patch("agent_bridge.orchestration.windows_preflight.subprocess.run") as run:
            run.return_value = _completed(b"\xff")
            outcome = wp._run_argv(["cmd.exe"])
            self.assertFalse(outcome.ok)
            self.assertIn("could not decode", outcome.detail)

    def test_accepts_bounded_utf16_output_used_by_redirected_wsl(self):
        with mock.patch("agent_bridge.orchestration.windows_preflight.subprocess.run") as run:
            run.return_value = _completed("WSL version: 2.1.5.0\r\n".encode("utf-16"))
            outcome = wp._run_argv(["wsl.exe", "--version"])
            self.assertTrue(outcome.ok)
            self.assertIn("WSL version", outcome.stdout)

    def test_oversized_output_fails_closed(self):
        with mock.patch("agent_bridge.orchestration.windows_preflight.subprocess.run") as run:
            run.return_value = _completed(b"x" * (wp.MAX_COMMAND_OUTPUT_BYTES + 1))
            outcome = wp._run_argv(["systeminfo.exe"])
            self.assertFalse(outcome.ok)
            self.assertIn("exceeded bound", outcome.detail)

    def test_oserror_is_explicit_failure(self):
        with mock.patch(
            "agent_bridge.orchestration.windows_preflight.subprocess.run",
            side_effect=OSError("boom"),
        ):
            outcome = wp._run_argv(["cmd.exe"])
            self.assertFalse(outcome.ok)
            self.assertIn("failed to spawn", outcome.detail)


class WindowsBuildTests(unittest.TestCase):
    def test_success(self):
        with mock.patch.object(wp, "_run_argv") as run_argv:
            run_argv.return_value = wp.CommandOutcome(
                True, "ok", "Microsoft Windows [Version 10.0.22631.3527]\n"
            )
            result = wp.collect_windows_build()
            self.assertTrue(result.passed)
            run_argv.assert_called_once_with(wp._CMD_VER_ARGV)

    def test_command_failure_propagates(self):
        with mock.patch.object(wp, "_run_argv") as run_argv:
            run_argv.return_value = wp.CommandOutcome(False, "executable not found: 'cmd.exe'", None)
            result = wp.collect_windows_build()
            self.assertFalse(result.passed)
            self.assertIn("not found", result.detail)

    def test_unparseable_output_fails(self):
        with mock.patch.object(wp, "_run_argv") as run_argv:
            run_argv.return_value = wp.CommandOutcome(True, "ok", "garbage")
            result = wp.collect_windows_build()
            self.assertFalse(result.passed)


class WslVersionTests(unittest.TestCase):
    def test_success(self):
        with mock.patch.object(wp, "_run_argv") as run_argv:
            run_argv.return_value = wp.CommandOutcome(True, "ok", "WSL version: 2.1.5.0\n")
            result = wp.collect_wsl_version()
            self.assertTrue(result.passed)
            run_argv.assert_called_once_with(wp._WSL_VERSION_ARGV)

    def test_failure_propagates(self):
        with mock.patch.object(wp, "_run_argv") as run_argv:
            run_argv.return_value = wp.CommandOutcome(False, "timed out", None)
            result = wp.collect_wsl_version()
            self.assertFalse(result.passed)


class FeatureStateTests(unittest.TestCase):
    def test_vmp_enabled(self):
        with mock.patch.object(wp, "_run_argv") as run_argv:
            run_argv.return_value = wp.CommandOutcome(True, "ok", "Enabled\n")
            result = wp.collect_virtual_machine_platform()
            self.assertTrue(result.passed)

    def test_vmp_disabled(self):
        with mock.patch.object(wp, "_run_argv") as run_argv:
            run_argv.return_value = wp.CommandOutcome(True, "ok", "Disabled\n")
            result = wp.collect_virtual_machine_platform()
            self.assertFalse(result.passed)

    def test_vmp_unknown_state_fails(self):
        with mock.patch.object(wp, "_run_argv") as run_argv:
            run_argv.return_value = wp.CommandOutcome(True, "ok", "Something\n")
            result = wp.collect_virtual_machine_platform()
            self.assertFalse(result.passed)
            self.assertIn("unknown", result.detail)

    def test_vmp_empty_output_fails(self):
        with mock.patch.object(wp, "_run_argv") as run_argv:
            run_argv.return_value = wp.CommandOutcome(True, "ok", "")
            result = wp.collect_virtual_machine_platform()
            self.assertFalse(result.passed)

    def test_vmp_command_failure_propagates(self):
        with mock.patch.object(wp, "_run_argv") as run_argv:
            run_argv.return_value = wp.CommandOutcome(False, "not found", None)
            result = wp.collect_virtual_machine_platform()
            self.assertFalse(result.passed)


class FirmwareVirtualizationTests(unittest.TestCase):
    def test_nested_probe_uses_the_absolute_system_powershell(self):
        self.assertEqual(
            wp._NESTED_VIRT_ARGV[0],
            r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")

    def test_active_hypervisor_on_physical_host_is_enabled(self):
        output = (
            "Hyper-V Requirements:          A hypervisor has been detected. "
            "Features required for Hyper-V will not be displayed.\n"
        )
        with mock.patch.object(wp, "_run_argv") as run_argv:
            run_argv.side_effect = (
                wp.CommandOutcome(True, "ok", output),
                wp.CommandOutcome(
                    True, "ok", "Manufacturer=Framework\nModel=Laptop 13\n"
                    "VirtualizationFirmwareEnabled=False\n"
                    "VMMonitorModeExtensions=False\n"
                    "SecondLevelAddressTranslationExtensions=False\n",
                ),
            )
            result = wp.collect_firmware_virtualization()
            self.assertTrue(result.passed)
            self.assertEqual(result.value, "Yes")

    def test_active_outer_hypervisor_without_nested_extensions_is_rejected(self):
        systeminfo = (
            "Hyper-V Requirements: A hypervisor has been detected. "
            "Features required for Hyper-V will not be displayed.\n"
        )
        processor = (
            "Manufacturer=Parallels International GmbH.\n"
            "Model=Parallels ARM Virtual Machine\n"
            "VirtualizationFirmwareEnabled=False\n"
            "VMMonitorModeExtensions=False\n"
            "SecondLevelAddressTranslationExtensions=False\n"
        )
        with mock.patch.object(wp, "_run_argv") as run_argv:
            run_argv.side_effect = (
                wp.CommandOutcome(True, "ok", systeminfo),
                wp.CommandOutcome(True, "ok", processor),
            )
            result = wp.collect_firmware_virtualization()
        self.assertFalse(result.passed)
        self.assertIn("does not expose", result.detail)

    def test_active_outer_hypervisor_with_nested_extensions_is_enabled(self):
        systeminfo = "A hypervisor has been detected.\n"
        processor = (
            "Manufacturer=Microsoft Corporation\nModel=Virtual Machine\n"
            "VirtualizationFirmwareEnabled=True\nVMMonitorModeExtensions=True\n"
            "SecondLevelAddressTranslationExtensions=True\n"
        )
        with mock.patch.object(wp, "_run_argv") as run_argv:
            run_argv.side_effect = (
                wp.CommandOutcome(True, "ok", systeminfo),
                wp.CommandOutcome(True, "ok", processor),
            )
            result = wp.collect_firmware_virtualization()
        self.assertTrue(result.passed)

    def test_active_virtual_machine_with_unknown_capability_fails_closed(self):
        with mock.patch.object(wp, "_run_argv") as run_argv:
            run_argv.side_effect = (
                wp.CommandOutcome(True, "ok", "A hypervisor has been detected.\n"),
                wp.CommandOutcome(True, "ok", "Manufacturer=VMware, Inc.\nModel=VMware7,1\n"),
            )
            result = wp.collect_firmware_virtualization()
        self.assertFalse(result.passed)
        self.assertIn("could not determine", result.detail)

    def test_active_hypervisor_nested_probe_failure_fails_closed(self):
        with mock.patch.object(wp, "_run_argv") as run_argv:
            run_argv.side_effect = (
                wp.CommandOutcome(True, "ok", "A hypervisor has been detected.\n"),
                wp.CommandOutcome(False, "powershell failed", None),
            )
            result = wp.collect_firmware_virtualization()
        self.assertFalse(result.passed)
        self.assertIn("powershell failed", result.detail)

    def test_enabled(self):
        output = "Some line\nVirtualization Enabled In Firmware:        Yes\nOther\n"
        with mock.patch.object(wp, "_run_argv") as run_argv:
            run_argv.return_value = wp.CommandOutcome(True, "ok", output)
            result = wp.collect_firmware_virtualization()
            self.assertTrue(result.passed)

    def test_disabled(self):
        output = "Virtualization Enabled In Firmware:        No\n"
        with mock.patch.object(wp, "_run_argv") as run_argv:
            run_argv.return_value = wp.CommandOutcome(True, "ok", output)
            result = wp.collect_firmware_virtualization()
            self.assertFalse(result.passed)

    def test_missing_marker_fails(self):
        with mock.patch.object(wp, "_run_argv") as run_argv:
            run_argv.return_value = wp.CommandOutcome(True, "ok", "OS Name: Windows\n")
            result = wp.collect_firmware_virtualization()
            self.assertFalse(result.passed)

    def test_malformed_value_fails(self):
        output = "Virtualization Enabled In Firmware:        Maybe\n"
        with mock.patch.object(wp, "_run_argv") as run_argv:
            run_argv.return_value = wp.CommandOutcome(True, "ok", output)
            result = wp.collect_firmware_virtualization()
            self.assertFalse(result.passed)

    def test_command_failure_propagates(self):
        with mock.patch.object(wp, "_run_argv") as run_argv:
            run_argv.return_value = wp.CommandOutcome(False, "timed out", None)
            result = wp.collect_firmware_virtualization()
            self.assertFalse(result.passed)


class ManifestCheckTests(unittest.TestCase):
    def test_missing_path_fails(self):
        result = wp.collect_manifest_check(None, lambda p: VALID_MANIFEST_RAW)
        self.assertFalse(result.passed)
        self.assertIn("no manifest path", result.detail)

    def test_missing_loader_fails(self):
        result = wp.collect_manifest_check("C:\\pinned\\manifest.json", None)
        self.assertFalse(result.passed)
        self.assertIn("no manifest loader", result.detail)

    def test_file_not_found_fails(self):
        def loader(path):
            raise FileNotFoundError()

        result = wp.collect_manifest_check("C:\\missing.json", loader)
        self.assertFalse(result.passed)
        self.assertIn("not found", result.detail)

    def test_os_error_fails(self):
        def loader(path):
            raise OSError("disk error")

        result = wp.collect_manifest_check("C:\\manifest.json", loader)
        self.assertFalse(result.passed)
        self.assertIn("could not read manifest", result.detail)

    def test_non_mapping_loader_result_fails(self):
        result = wp.collect_manifest_check("C:\\manifest.json", lambda p: "not a dict")
        self.assertFalse(result.passed)

    def test_malformed_manifest_fails(self):
        bad = dict(VALID_MANIFEST_RAW)
        bad["rootfs_sha256"] = "not-hex"
        result = wp.collect_manifest_check("C:\\manifest.json", lambda p: bad)
        self.assertFalse(result.passed)
        self.assertIn("malformed manifest", result.detail)

    def test_valid_manifest_passes(self):
        result = wp.collect_manifest_check("C:\\manifest.json", lambda p: VALID_MANIFEST_RAW)
        self.assertTrue(result.passed)
        self.assertEqual(result.value, "a" * 64)

    def test_hash_match_passes(self):
        result = wp.collect_manifest_check(
            "C:\\manifest.json", lambda p: VALID_MANIFEST_RAW, "a" * 64
        )
        self.assertTrue(result.passed)

    def test_hash_mismatch_fails(self):
        result = wp.collect_manifest_check(
            "C:\\manifest.json", lambda p: VALID_MANIFEST_RAW, "b" * 64
        )
        self.assertFalse(result.passed)
        self.assertIn("mismatch", result.detail)

    def test_invalid_expected_hash_fails(self):
        result = wp.collect_manifest_check(
            "C:\\manifest.json", lambda p: VALID_MANIFEST_RAW, ""
        )
        self.assertFalse(result.passed)


class RunPreflightTests(unittest.TestCase):
    def test_non_windows_platform_returns_unsupported_without_spawning(self):
        with mock.patch("agent_bridge.orchestration.windows_preflight.subprocess.run") as run:
            report = wp.run_preflight(platform="linux")
            self.assertEqual(report.status, wp.STATUS_UNSUPPORTED_PLATFORM)
            run.assert_not_called()

    def test_darwin_also_unsupported(self):
        report = wp.run_preflight(platform="darwin")
        self.assertEqual(report.status, wp.STATUS_UNSUPPORTED_PLATFORM)

    def _mock_success_run(self):
        def fake_run(argv, **kwargs):
            if argv[0] == "cmd.exe":
                return _completed(b"Microsoft Windows [Version 10.0.22631.3527]\n")
            if argv[0] == "wsl.exe":
                return _completed(b"WSL version: 2.1.5.0\n")
            if argv[0] == "powershell.exe":
                return _completed(b"Enabled\n")
            if argv[0] == "systeminfo.exe":
                return _completed(b"Virtualization Enabled In Firmware:        Yes\n")
            raise AssertionError(f"unexpected argv {argv}")

        return fake_run

    def test_all_checks_pass_reports_only_prerequisites_ready(self):
        with mock.patch(
            "agent_bridge.orchestration.windows_preflight.subprocess.run",
            side_effect=self._mock_success_run(),
        ):
            report = wp.run_preflight(
                platform="win32",
                manifest_path="C:\\manifest.json",
                manifest_loader=lambda p: VALID_MANIFEST_RAW,
            )
            self.assertEqual(report.status, wp.STATUS_PREREQUISITES_READY)
            self.assertTrue(report.windows_build.passed)
            self.assertTrue(report.wsl_version.passed)
            self.assertTrue(report.virtual_machine_platform.passed)
            self.assertTrue(report.firmware_virtualization.passed)
            self.assertTrue(report.manifest.passed)
            self.assertNotIn("delegation enabled", report.detail.lower())
            self.assertNotIn("delegation verified", report.detail.lower())

    def test_missing_manifest_forces_not_ready_even_if_all_else_passes(self):
        with mock.patch(
            "agent_bridge.orchestration.windows_preflight.subprocess.run",
            side_effect=self._mock_success_run(),
        ):
            report = wp.run_preflight(platform="win32", manifest_path=None)
            self.assertEqual(report.status, wp.STATUS_PREREQUISITES_NOT_READY)
            self.assertFalse(report.manifest.passed)

    def test_single_failing_check_forces_not_ready(self):
        def fake_run(argv, **kwargs):
            if argv[0] == "cmd.exe":
                return _completed(b"garbage")
            if argv[0] == "wsl.exe":
                return _completed(b"WSL version: 2.1.5.0\n")
            if argv[0] == "powershell.exe":
                return _completed(b"Enabled\n")
            if argv[0] == "systeminfo.exe":
                return _completed(b"Virtualization Enabled In Firmware:        Yes\n")
            raise AssertionError(f"unexpected argv {argv}")

        with mock.patch(
            "agent_bridge.orchestration.windows_preflight.subprocess.run",
            side_effect=fake_run,
        ):
            report = wp.run_preflight(
                platform="win32",
                manifest_path="C:\\manifest.json",
                manifest_loader=lambda p: VALID_MANIFEST_RAW,
            )
            self.assertEqual(report.status, wp.STATUS_PREREQUISITES_NOT_READY)
            self.assertFalse(report.windows_build.passed)

    def test_rootfs_hash_mismatch_forces_not_ready(self):
        with mock.patch(
            "agent_bridge.orchestration.windows_preflight.subprocess.run",
            side_effect=self._mock_success_run(),
        ):
            report = wp.run_preflight(
                platform="win32",
                manifest_path="C:\\manifest.json",
                manifest_loader=lambda p: VALID_MANIFEST_RAW,
                expected_rootfs_sha256="b" * 64,
            )
            self.assertEqual(report.status, wp.STATUS_PREREQUISITES_NOT_READY)
            self.assertFalse(report.manifest.passed)

    def test_all_command_argv_use_shell_false_and_bounded_timeout(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            return self._mock_success_run()(argv, **kwargs)

        with mock.patch(
            "agent_bridge.orchestration.windows_preflight.subprocess.run",
            side_effect=fake_run,
        ):
            wp.run_preflight(
                platform="win32",
                manifest_path="C:\\manifest.json",
                manifest_loader=lambda p: VALID_MANIFEST_RAW,
            )

        self.assertEqual(len(calls), 4)
        for argv, kwargs in calls:
            self.assertIs(kwargs["shell"], False)
            self.assertEqual(kwargs["timeout"], wp.COMMAND_TIMEOUT_SECONDS)
            self.assertEqual(kwargs["env"], wp._MINIMAL_ENV)


class IsWindowsTests(unittest.TestCase):
    def test_win32_is_windows(self):
        self.assertTrue(wp.is_windows("win32"))

    def test_linux_is_not_windows(self):
        self.assertFalse(wp.is_windows("linux"))

    def test_darwin_is_not_windows(self):
        self.assertFalse(wp.is_windows("darwin"))

    def test_defaults_to_sys_platform(self):
        with mock.patch.object(wp.sys, "platform", "win32"):
            self.assertTrue(wp.is_windows())


if __name__ == "__main__":
    unittest.main()
