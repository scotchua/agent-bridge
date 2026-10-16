"""Boundary cases found by offline mutation of the Windows/WSL contract."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge.orchestration import windows_wsl as ww


class ManifestMutationTests(unittest.TestCase):
    def test_unsafe_string_rejects_single_path_separator(self):
        with self.assertRaises(ww.ManifestError):
            ww._reject_unsafe_string("node_version", "1/2")

    def test_unsafe_string_rejects_non_string_with_contract_error(self):
        with self.assertRaises(ww.ManifestError):
            ww._reject_unsafe_string("node_version", 7)

    def test_unpinned_version_rejects_non_numeric_prefix(self):
        with self.assertRaises(ww.ManifestError):
            ww._reject_unpinned_version("node_version", "v7.2")

    def test_unpinned_version_rejects_embedded_traversal(self):
        with self.assertRaises(ww.ManifestError):
            ww._reject_unpinned_version("node_version", "1..2")


class PrerequisiteMutationTests(unittest.TestCase):
    def test_windows_build_at_minimum_passes(self):
        result = ww.parse_windows_build("Microsoft Windows [Version 10.0.22621.1]")
        self.assertTrue(result.passed)
        self.assertEqual(result.value, "10.0.22621")

    def test_wsl_version_at_minimum_passes(self):
        result = ww.parse_wsl_version("WSL version: 2.0.0\n")
        self.assertTrue(result.passed)
        self.assertEqual(result.value, "2.0.0")

    def test_wsl_version_non_string_fails_closed(self):
        result = ww.parse_wsl_version(7)
        self.assertFalse(result.passed)
        self.assertIsNone(result.value)


class DistroNameMutationTests(unittest.TestCase):
    def test_validate_accepts_maximum_length_suffix(self):
        name = "agent-bridge-" + "a" * 32
        self.assertEqual(ww.validate_distro_name(name), name)

    def test_validate_non_string_raises_contract_error(self):
        with self.assertRaises(ww.DistroNameError):
            ww.validate_distro_name(7)

    def test_build_non_string_raises_contract_error(self):
        with self.assertRaises(ww.DistroNameError):
            ww.build_distro_name(7)


class GuestEnvironmentMutationTests(unittest.TestCase):
    def test_rejects_non_string_allowlisted_value(self):
        with self.assertRaises(ww.JobSpecError):
            ww.build_guest_environment({"HOME": 7})

    def test_rejects_non_mapping_with_contract_error(self):
        with self.assertRaises(ww.JobSpecError):
            ww.build_guest_environment(7)


class HostMarkerMutationTests(unittest.TestCase):
    def test_windows_path_detector_returns_false_for_guest_path(self):
        self.assertIs(ww._looks_like_windows_path("/workspace/cedar"), False)

    def test_host_marker_detects_slash_unc_path(self):
        self.assertIs(ww._contains_host_marker("//fictional-host/share"), True)

    def test_host_marker_returns_false_for_guest_path(self):
        self.assertIs(ww._contains_host_marker("/workspace/cedar"), False)

    def test_reject_host_path_reports_empty_path(self):
        with self.assertRaisesRegex(ww.JobSpecError, "non-empty string"):
            ww._reject_host_path("workdir", "")


class JobSpecMutationTests(unittest.TestCase):
    def test_rejects_non_sequence_command_with_contract_error(self):
        spec = {"command": 7, "workdir": "/workspace/cedar", "env": {}}
        with self.assertRaises(ww.JobSpecError):
            ww.validate_job_spec(spec)


class WindowsHostPathMutationTests(unittest.TestCase):
    def test_shell_unsafe_rejects_non_string_with_contract_error(self):
        with self.assertRaises(ww.WindowsWslContractError):
            ww._reject_shell_unsafe("install_dir", 7)

    def test_shell_unsafe_returns_valid_value(self):
        self.assertEqual(ww._reject_shell_unsafe("install_dir", "C:\\cedar"),
                         "C:\\cedar")

    def test_rejects_trailing_dot_component(self):
        with self.assertRaises(ww.WindowsWslContractError):
            ww._reject_reserved_and_ambiguous_components(
                "rootfs_path", "C:\\images\\cedar.\\rootfs.tar")

    def test_rejects_non_letter_drive(self):
        with self.assertRaises(ww.WindowsWslContractError):
            ww._validate_windows_host_path("rootfs_path", "1:\\images\\rootfs.tar")

    def test_windows_host_path_returns_valid_value(self):
        path = "C:\\images\\rootfs.tar"
        self.assertEqual(ww._validate_windows_host_path("rootfs_path", path), path)

    def test_rejects_traversal_in_absolute_path(self):
        with self.assertRaisesRegex(ww.WindowsWslContractError, "path traversal"):
            ww._validate_windows_host_path("rootfs_path", "C:\\images\\..\\rootfs.tar")


class ArgvMutationTests(unittest.TestCase):
    def test_unregister_rejects_unsafe_name(self):
        with self.assertRaises(ww.DistroNameError):
            ww.build_unregister_argv("fictional-guest")

    def test_guest_exec_rejects_unsafe_name(self):
        job = ww.validate_job_spec({
            "command": [ww.GUEST_RUNNER_PATH],
            "workdir": "/workspace/cedar",
            "env": {},
        })
        with self.assertRaises(ww.DistroNameError):
            ww.build_guest_exec_argv("fictional-guest", job)

    def test_guest_exec_rejects_host_marker_in_supplied_job(self):
        job = ww.GuestJobSpec(
            command=(ww.GUEST_RUNNER_PATH, "/mnt/c/fictional.txt"),
            workdir="/workspace/cedar",
        )
        with self.assertRaises(ww.WindowsWslContractError):
            ww.build_guest_exec_argv("agent-bridge-abcdef", job)


if __name__ == "__main__":
    unittest.main()
