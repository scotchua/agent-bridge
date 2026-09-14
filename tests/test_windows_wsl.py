"""Tests for the Windows WSL2 core contract scaffolding.

These tests exercise phase-one contract logic only: strict manifest
parsing, prerequisite string parsing, safe distro naming, guest
boundary specs, job-spec validation, pure argv builders, and pure
stale-instance selection. Nothing here mocks a live security pass or
requires WSL to be installed; the module performs no mutations,
process spawns, or network calls, so these tests run identically on
macOS, Linux, and Windows.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge.orchestration import windows_wsl as ww


VALID_MANIFEST = {
    "schema_version": 1,
    "distro_release": "22.04.3",
    "rootfs_sha256": "a" * 64,
    "node_version": "20.11.1",
    "claude_version": "1.2.3",
    "codex_version": "0.9.0",
}


def _valid_job_spec(**overrides):
    spec = {
        "command": [ww.GUEST_RUNNER_PATH, "hello"],
        "workdir": "/workspace/job",
        "env": {"HOME": "/root"},
    }
    spec.update(overrides)
    return spec


class ManifestTests(unittest.TestCase):
    def test_accepts_valid_pinned_manifest(self):
        manifest = ww.parse_manifest(VALID_MANIFEST)
        self.assertEqual(manifest.distro_release, "22.04.3")
        self.assertEqual(manifest.rootfs_sha256, "a" * 64)

    def test_rejects_missing_key(self):
        for missing_key in VALID_MANIFEST:
            with self.subTest(missing_key=missing_key):
                data = dict(VALID_MANIFEST)
                del data[missing_key]
                with self.assertRaises(ww.ManifestError):
                    ww.parse_manifest(data)

    def test_rejects_extra_key(self):
        data = dict(VALID_MANIFEST)
        data["extra_field"] = "x"
        with self.assertRaises(ww.ManifestError):
            ww.parse_manifest(data)

    def test_rejects_wrong_type(self):
        data = dict(VALID_MANIFEST)
        data["schema_version"] = "1"
        with self.assertRaises(ww.ManifestError):
            ww.parse_manifest(data)

    def test_rejects_bool_schema_version(self):
        data = dict(VALID_MANIFEST)
        data["schema_version"] = True
        with self.assertRaises(ww.ManifestError):
            ww.parse_manifest(data)

    def test_rejects_non_one_schema_version(self):
        for bad in (0, 2, -1, 99):
            with self.subTest(schema_version=bad):
                data = dict(VALID_MANIFEST)
                data["schema_version"] = bad
                with self.assertRaises(ww.ManifestError):
                    ww.parse_manifest(data)

    def test_rejects_non_string_keys(self):
        data = dict(VALID_MANIFEST)
        del data["schema_version"]
        data[1] = "1"
        with self.assertRaises(ww.ManifestError):
            ww.parse_manifest(data)

    def test_rejects_non_string_keys_mixed_types_no_sort_crash(self):
        # Ensure mixed int/None/str keys never leak a TypeError from
        # attempting to sort incomparable types; must raise cleanly.
        data = {1: "x", None: "y", (1, 2): "z", "schema_version": 1}
        with self.assertRaises(ww.ManifestError):
            ww.parse_manifest(data)

    def test_rejects_unpinned_values(self):
        tokens = ["latest", "stable", "default", "LATEST", "current", "head"]
        fields = ["distro_release", "node_version", "claude_version", "codex_version"]
        for field in fields:
            for token in tokens:
                with self.subTest(field=field, token=token):
                    data = dict(VALID_MANIFEST)
                    data[field] = token
                    with self.assertRaises(ww.ManifestError):
                        ww.parse_manifest(data)

    def test_rejects_path_traversal(self):
        data = dict(VALID_MANIFEST)
        data["node_version"] = "../../etc/passwd"
        with self.assertRaises(ww.ManifestError):
            ww.parse_manifest(data)

    def test_rejects_control_characters(self):
        data = dict(VALID_MANIFEST)
        data["claude_version"] = "1.2.3\x00"
        with self.assertRaises(ww.ManifestError):
            ww.parse_manifest(data)

    def test_rejects_bad_rootfs_sha256_case(self):
        data = dict(VALID_MANIFEST)
        data["rootfs_sha256"] = "A" * 64
        with self.assertRaises(ww.ManifestError):
            ww.parse_manifest(data)

    def test_rejects_short_rootfs_sha256(self):
        data = dict(VALID_MANIFEST)
        data["rootfs_sha256"] = "a" * 63
        with self.assertRaises(ww.ManifestError):
            ww.parse_manifest(data)

    def test_rejects_non_mapping(self):
        with self.assertRaises(ww.ManifestError):
            ww.parse_manifest(["not", "a", "mapping"])  # type: ignore[arg-type]


class PrerequisiteParsingTests(unittest.TestCase):
    def test_windows_min_build_constant(self):
        self.assertEqual(ww.WINDOWS_MIN_BUILD, 22621)

    def test_wsl_min_version_constant(self):
        self.assertEqual(ww.WSL_MIN_VERSION, (2, 0, 0))

    def test_parse_windows_build_passes_above_minimum(self):
        output = "Microsoft Windows [Version 10.0.22631.3527]"
        result = ww.parse_windows_build(output)
        self.assertTrue(result.passed)
        self.assertEqual(result.value, "10.0.22631")

    def test_parse_windows_build_fails_below_minimum(self):
        output = "Microsoft Windows [Version 10.0.19045.1]"
        result = ww.parse_windows_build(output)
        self.assertFalse(result.passed)

    def test_parse_windows_build_fails_closed_on_garbage(self):
        result = ww.parse_windows_build("not a real ver string")
        self.assertFalse(result.passed)
        self.assertIsNone(result.value)

    def test_parse_windows_build_fails_closed_on_empty(self):
        result = ww.parse_windows_build("")
        self.assertFalse(result.passed)

    def test_parse_windows_build_fails_closed_on_none(self):
        result = ww.parse_windows_build(None)  # type: ignore[arg-type]
        self.assertFalse(result.passed)

    def test_parse_wsl_version_passes_above_minimum(self):
        output = (
            "WSL version: 2.0.9.0\n"
            "Kernel version: 5.15.146.1-2\n"
            "WSLg version: 1.0.60\n"
            "Windows version: 10.0.22631.3527\n"
        )
        result = ww.parse_wsl_version(output)
        self.assertTrue(result.passed)
        self.assertEqual(result.value, "2.0.9")

    def test_parse_wsl_version_fails_below_minimum(self):
        output = "WSL version: 1.2.5.0\nKernel version: 4.19\n"
        result = ww.parse_wsl_version(output)
        self.assertFalse(result.passed)

    def test_parse_wsl_version_fails_closed_on_garbage(self):
        result = ww.parse_wsl_version("garbage output with no version")
        self.assertFalse(result.passed)
        self.assertIsNone(result.value)

    def test_parse_wsl_version_fails_closed_on_empty(self):
        self.assertFalse(ww.parse_wsl_version("").passed)


class AggregatePrerequisiteTests(unittest.TestCase):
    def _passing_results(self):
        windows_build = ww.parse_windows_build("Microsoft Windows [Version 10.0.22631.3527]")
        wsl_version = ww.parse_wsl_version("WSL version: 2.0.9.0\n")
        return windows_build, wsl_version

    def test_passes_when_everything_explicitly_true(self):
        windows_build, wsl_version = self._passing_results()
        self.assertTrue(
            ww.all_prerequisites_met(
                windows_build,
                wsl_version,
                virtual_machine_platform_enabled=True,
                firmware_virtualization_enabled=True,
            )
        )

    def test_fails_when_windows_build_failed(self):
        _, wsl_version = self._passing_results()
        failed_build = ww.parse_windows_build("garbage")
        self.assertFalse(
            ww.all_prerequisites_met(
                failed_build,
                wsl_version,
                virtual_machine_platform_enabled=True,
                firmware_virtualization_enabled=True,
            )
        )

    def test_fails_when_wsl_version_failed(self):
        windows_build, _ = self._passing_results()
        failed_wsl = ww.parse_wsl_version("garbage")
        self.assertFalse(
            ww.all_prerequisites_met(
                windows_build,
                failed_wsl,
                virtual_machine_platform_enabled=True,
                firmware_virtualization_enabled=True,
            )
        )

    def test_fails_closed_on_falsy_flags(self):
        windows_build, wsl_version = self._passing_results()
        self.assertFalse(
            ww.all_prerequisites_met(
                windows_build,
                wsl_version,
                virtual_machine_platform_enabled=False,
                firmware_virtualization_enabled=True,
            )
        )
        self.assertFalse(
            ww.all_prerequisites_met(
                windows_build,
                wsl_version,
                virtual_machine_platform_enabled=True,
                firmware_virtualization_enabled=False,
            )
        )

    def test_fails_closed_on_non_bool_truthy_values(self):
        windows_build, wsl_version = self._passing_results()
        self.assertFalse(
            ww.all_prerequisites_met(
                windows_build,
                wsl_version,
                virtual_machine_platform_enabled=1,  # type: ignore[arg-type]
                firmware_virtualization_enabled=True,
            )
        )
        self.assertFalse(
            ww.all_prerequisites_met(
                windows_build,
                wsl_version,
                virtual_machine_platform_enabled=True,
                firmware_virtualization_enabled="true",  # type: ignore[arg-type]
            )
        )

    def test_fails_closed_on_wrong_result_types(self):
        self.assertFalse(
            ww.all_prerequisites_met(
                "not-a-result",  # type: ignore[arg-type]
                ww.parse_wsl_version("WSL version: 2.0.9.0\n"),
                virtual_machine_platform_enabled=True,
                firmware_virtualization_enabled=True,
            )
        )


class DistroNameTests(unittest.TestCase):
    def test_validate_accepts_valid_name(self):
        self.assertEqual(
            ww.validate_distro_name("agent-bridge-abc123"), "agent-bridge-abc123"
        )

    def test_build_from_hex_token(self):
        self.assertEqual(ww.build_distro_name("deadbeef"), "agent-bridge-deadbeef")

    def test_rejects_unsafe_names(self):
        names = [
            "agent-bridge-ABC123",  # uppercase not allowed
            "agent-bridge-",  # empty token
            "agent-bridge-xyz",  # non-hex chars
            "not-agent-bridge-abc123",  # wrong prefix
            "agent-bridge-" + "a" * 40,  # too long
            "",
            "agent-bridge-../etc",
        ]
        for name in names:
            with self.subTest(name=name):
                with self.assertRaises(ww.DistroNameError):
                    ww.validate_distro_name(name)

    def test_build_rejects_uppercase_token(self):
        with self.assertRaises(ww.DistroNameError):
            ww.build_distro_name("ABCDEF")


class GuestBoundaryTests(unittest.TestCase):
    def test_wsl_conf_disables_automount_and_interop(self):
        self.assertIn("enabled = false", ww.WSL_CONF_CONTENTS)
        self.assertIn("[automount]", ww.WSL_CONF_CONTENTS)
        self.assertIn("[interop]", ww.WSL_CONF_CONTENTS)

    def test_build_guest_environment_allows_allowlisted_keys(self):
        env = ww.build_guest_environment({"HOME": "/root", "PATH": "/usr/bin"})
        self.assertEqual(env, {"HOME": "/root", "PATH": "/usr/bin"})

    def test_build_guest_environment_rejects_non_allowlisted_key(self):
        with self.assertRaises(ww.JobSpecError):
            ww.build_guest_environment({"HOST_SECRET": "x"})

    def test_build_guest_environment_never_inherits_host(self):
        # No monkeypatching of the real environment: the function must
        # never read os.environ at all, so an empty override always
        # yields an empty guest environment regardless of host state.
        env = ww.build_guest_environment({})
        self.assertEqual(env, {})

    def test_build_guest_environment_rejects_control_chars(self):
        with self.assertRaises(ww.JobSpecError):
            ww.build_guest_environment({"HOME": "/root\x00"})

    def test_build_guest_environment_rejects_windows_path_value(self):
        with self.assertRaises(ww.JobSpecError):
            ww.build_guest_environment({"HOME": "C:\\Users\\bob"})

    def test_build_guest_environment_rejects_embedded_mnt(self):
        with self.assertRaises(ww.JobSpecError):
            ww.build_guest_environment({"PATH": "/usr/bin:/mnt/c/tools"})

    def test_build_guest_environment_rejects_drive_after_key_prefix(self):
        with self.assertRaises(ww.JobSpecError):
            ww.build_guest_environment({"HOME": "C:/Users/bob"})


class JobSpecTests(unittest.TestCase):
    def test_accepts_valid_spec(self):
        job = ww.validate_job_spec(_valid_job_spec())
        self.assertEqual(job.command, (ww.GUEST_RUNNER_PATH, "hello"))
        self.assertEqual(job.workdir, "/workspace/job")

    def test_rejects_windows_and_mount_paths_in_workdir(self):
        workdirs = [
            "C:\\Users\\bob\\project",
            "c:/Users/bob/project",
            "\\\\server\\share\\project",
            "/mnt/c/Users/bob",
            "/mnt/d/checkout",
            "/mnt",
            "/workspace/foo/../../mnt/c/x",
        ]
        for workdir in workdirs:
            with self.subTest(workdir=workdir):
                with self.assertRaises(ww.JobSpecError):
                    ww.validate_job_spec(_valid_job_spec(workdir=workdir))

    def test_rejects_host_checkout_like_path(self):
        with self.assertRaises(ww.JobSpecError):
            ww.validate_job_spec(_valid_job_spec(workdir="/Users/bob/repo"))

    def test_rejects_path_traversal_in_workdir(self):
        with self.assertRaises(ww.JobSpecError):
            ww.validate_job_spec(_valid_job_spec(workdir="/workspace/../etc"))

    def test_rejects_symlinks_key(self):
        spec = _valid_job_spec()
        spec["symlinks"] = [{"from": "/tmp/a", "to": "/tmp/b"}]
        with self.assertRaises(ww.JobSpecError):
            ww.validate_job_spec(spec)

    def test_rejects_mounts_key(self):
        spec = _valid_job_spec()
        spec["mounts"] = ["/host/path:/guest/path"]
        with self.assertRaises(ww.JobSpecError):
            ww.validate_job_spec(spec)

    def test_rejects_secret_looking_env_keys(self):
        keys = ["AWS_SECRET_ACCESS_KEY", "api_key", "password", "DB_CREDENTIAL", "auth_token"]
        for key in keys:
            with self.subTest(key=key):
                spec = _valid_job_spec(env={key: "x"})
                with self.assertRaises(ww.JobSpecError):
                    ww.validate_job_spec(spec)

    def test_rejects_unknown_field(self):
        spec = _valid_job_spec()
        spec["extra"] = "nope"
        with self.assertRaises(ww.JobSpecError):
            ww.validate_job_spec(spec)

    def test_rejects_non_string_keys_cleanly(self):
        spec = _valid_job_spec()
        spec[1] = "nope"
        with self.assertRaisesRegex(ww.JobSpecError, "keys must be strings"):
            ww.validate_job_spec(spec)

    def test_rejects_non_string_env_keys_cleanly(self):
        with self.assertRaisesRegex(ww.JobSpecError, "env keys must be strings"):
            ww.validate_job_spec(_valid_job_spec(env={1: "nope"}))

    def test_rejects_any_host_fallback_option(self):
        keys = ["allow_host_fallback", "host_fallback", "fallback_to_host", "use_host"]
        for key in keys:
            with self.subTest(key=key):
                spec = _valid_job_spec()
                spec[key] = False  # even explicitly false must be rejected: presence alone
                with self.assertRaises(ww.JobSpecError):
                    ww.validate_job_spec(spec)

    def test_rejects_empty_command(self):
        with self.assertRaises(ww.JobSpecError):
            ww.validate_job_spec(_valid_job_spec(command=[]))

    def test_rejects_command_with_host_path(self):
        with self.assertRaises(ww.JobSpecError):
            ww.validate_job_spec(
                _valid_job_spec(command=[ww.GUEST_RUNNER_PATH, "/mnt/c/secret.txt"])
            )

    def test_rejects_arbitrary_executable(self):
        with self.assertRaises(ww.JobSpecError):
            ww.validate_job_spec(_valid_job_spec(command=["echo", "hello"]))

    def test_rejects_non_pinned_wrapper_path(self):
        with self.assertRaises(ww.JobSpecError):
            ww.validate_job_spec(
                _valid_job_spec(command=["/usr/local/bin/other-runner", "x"])
            )

    def test_rejects_non_mapping(self):
        with self.assertRaises(ww.JobSpecError):
            ww.validate_job_spec(["not", "a", "mapping"])  # type: ignore[arg-type]

    def test_rejects_backslash_in_command_arg(self):
        with self.assertRaises(ww.JobSpecError):
            ww.validate_job_spec(
                _valid_job_spec(command=[ww.GUEST_RUNNER_PATH, "C:\\evil"])
            )

    def test_rejects_traversal_in_command_arg(self):
        with self.assertRaises(ww.JobSpecError):
            ww.validate_job_spec(
                _valid_job_spec(command=[ww.GUEST_RUNNER_PATH, "/workspace/../etc/passwd"])
            )

    def test_preserves_legitimate_guest_absolute_paths(self):
        job = ww.validate_job_spec(
            _valid_job_spec(command=[ww.GUEST_RUNNER_PATH, "/workspace/job/input.json"])
        )
        self.assertIn("/workspace/job/input.json", job.command)


class ArgvBuilderTests(unittest.TestCase):
    def test_build_import_argv(self):
        argv = ww.build_import_argv(
            "agent-bridge-abc123", "C:\\wsl\\agent-bridge-abc123", "C:\\images\\rootfs.tar"
        )
        self.assertEqual(
            argv,
            [
                "wsl.exe",
                "--import",
                "agent-bridge-abc123",
                "C:\\wsl\\agent-bridge-abc123",
                "C:\\images\\rootfs.tar",
                "--version",
                "2",
            ],
        )
        self.assertIsInstance(argv, list)
        self.assertTrue(all(isinstance(part, str) for part in argv))

    def test_build_import_argv_rejects_unsafe_distro_name(self):
        with self.assertRaises(ww.DistroNameError):
            ww.build_import_argv("not-safe", "C:\\x\\y", "C:\\y\\z")

    def test_build_import_argv_rejects_wsl1(self):
        with self.assertRaises(ww.WindowsWslContractError):
            ww.build_import_argv(
                "agent-bridge-abc123", "C:\\x\\y", "C:\\y\\z", wsl_version=1
            )

    def test_build_import_argv_rejects_bad_wsl_version(self):
        with self.assertRaises(ww.WindowsWslContractError):
            ww.build_import_argv(
                "agent-bridge-abc123", "C:\\x\\y", "C:\\y\\z", wsl_version=3
            )

    def test_build_import_argv_rejects_relative_install_dir(self):
        with self.assertRaises(ww.WindowsWslContractError):
            ww.build_import_argv(
                "agent-bridge-abc123", "wsl\\agent-bridge-abc123", "C:\\images\\rootfs.tar"
            )

    def test_build_import_argv_rejects_relative_rootfs_path(self):
        with self.assertRaises(ww.WindowsWslContractError):
            ww.build_import_argv(
                "agent-bridge-abc123", "C:\\wsl\\agent-bridge-abc123", "images\\rootfs.tar"
            )

    def test_build_import_argv_rejects_unc_paths(self):
        with self.assertRaises(ww.WindowsWslContractError):
            ww.build_import_argv(
                "agent-bridge-abc123", "\\\\server\\share\\wsl", "C:\\images\\rootfs.tar"
            )
        with self.assertRaises(ww.WindowsWslContractError):
            ww.build_import_argv(
                "agent-bridge-abc123", "C:\\wsl\\agent-bridge-abc123", "//server/share/rootfs.tar"
            )

    def test_build_import_argv_rejects_device_paths(self):
        with self.assertRaises(ww.WindowsWslContractError):
            ww.build_import_argv(
                "agent-bridge-abc123", "\\\\.\\C:\\wsl", "C:\\images\\rootfs.tar"
            )
        with self.assertRaises(ww.WindowsWslContractError):
            ww.build_import_argv(
                "agent-bridge-abc123", "C:\\wsl\\agent-bridge-abc123", "\\\\?\\C:\\images\\rootfs.tar"
            )

    def test_build_import_argv_rejects_traversal(self):
        with self.assertRaises(ww.WindowsWslContractError):
            ww.build_import_argv(
                "agent-bridge-abc123", "C:\\wsl\\..\\..\\Windows", "C:\\images\\rootfs.tar"
            )

    def test_build_import_argv_rejects_control_characters(self):
        with self.assertRaises(ww.WindowsWslContractError):
            ww.build_import_argv(
                "agent-bridge-abc123", "C:\\wsl\\agent-bridge-abc123\x00", "C:\\images\\rootfs.tar"
            )

    def test_build_import_argv_accepts_forward_slash_drive_path(self):
        argv = ww.build_import_argv(
            "agent-bridge-abc123", "C:/wsl/agent-bridge-abc123", "C:/images/rootfs.tar"
        )
        self.assertEqual(argv[3], "C:/wsl/agent-bridge-abc123")

    def test_build_terminate_argv(self):
        self.assertEqual(
            ww.build_terminate_argv("agent-bridge-abc123"),
            ["wsl.exe", "--terminate", "agent-bridge-abc123"],
        )

    def test_build_unregister_argv(self):
        self.assertEqual(
            ww.build_unregister_argv("agent-bridge-abc123"),
            ["wsl.exe", "--unregister", "agent-bridge-abc123"],
        )

    def test_build_terminate_argv_rejects_unsafe_name(self):
        with self.assertRaises(ww.DistroNameError):
            ww.build_terminate_argv("evil; rm -rf /")

    def test_build_guest_exec_argv_no_host_path(self):
        job = ww.validate_job_spec(_valid_job_spec())
        argv = ww.build_guest_exec_argv("agent-bridge-abc123", job)
        self.assertEqual(argv[0], "wsl.exe")
        self.assertIn("--", argv)
        for part in argv:
            self.assertNotIn("\\", part)
            self.assertFalse(part.startswith("/mnt/"))
            self.assertFalse(part.upper().startswith("C:"))

    def test_build_guest_exec_argv_only_pinned_wrapper(self):
        job = ww.validate_job_spec(_valid_job_spec())
        argv = ww.build_guest_exec_argv("agent-bridge-abc123", job)
        self.assertIn(ww.GUEST_RUNNER_PATH, argv)

    def test_build_guest_exec_argv_rejects_non_validated_job(self):
        with self.assertRaises(ww.WindowsWslContractError):
            ww.build_guest_exec_argv("agent-bridge-abc123", _valid_job_spec())  # type: ignore[arg-type]

    def test_build_guest_exec_argv_is_argv_list_not_shell_string(self):
        job = ww.validate_job_spec(
            _valid_job_spec(command=[ww.GUEST_RUNNER_PATH, "a b c"])
        )
        argv = ww.build_guest_exec_argv("agent-bridge-abc123", job)
        self.assertIsInstance(argv, list)
        self.assertIn("a b c", argv)  # preserved as a single argument, not shell-joined


class StaleInstanceSelectionTests(unittest.TestCase):
    def test_selects_old_ones_only(self):
        now = datetime(2026, 9, 14, 12, 0, 0)
        instances = [
            ww.InstanceRecord("agent-bridge-abc123", now - timedelta(hours=5)),
            ww.InstanceRecord("agent-bridge-def456", now - timedelta(minutes=5)),
        ]
        stale = ww.select_stale_instances(instances, now, timedelta(hours=1))
        self.assertEqual(stale, ["agent-bridge-abc123"])

    def test_ignores_non_prefixed_names(self):
        now = datetime(2026, 9, 14, 12, 0, 0)
        instances = [
            ww.InstanceRecord("Ubuntu-22.04", now - timedelta(days=10)),
            ww.InstanceRecord("Docker-desktop", now - timedelta(days=10)),
        ]
        stale = ww.select_stale_instances(instances, now, timedelta(hours=1))
        self.assertEqual(stale, [])

    def test_rejects_non_positive_max_age(self):
        now = datetime(2026, 9, 14, 12, 0, 0)
        with self.assertRaises(ww.WindowsWslContractError):
            ww.select_stale_instances([], now, timedelta(0))

    def test_rejects_wrong_type_max_age(self):
        now = datetime(2026, 9, 14, 12, 0, 0)
        with self.assertRaises(ww.WindowsWslContractError):
            ww.select_stale_instances([], now, "1 hour")  # type: ignore[arg-type]

    def test_rejects_wrong_type_now(self):
        with self.assertRaises(ww.WindowsWslContractError):
            ww.select_stale_instances([], "not-a-datetime", timedelta(hours=1))  # type: ignore[arg-type]

    def test_rejects_wrong_type_created_at(self):
        now = datetime(2026, 9, 14, 12, 0, 0)
        instances = [ww.InstanceRecord("agent-bridge-abc123", "not-a-datetime")]  # type: ignore[arg-type]
        with self.assertRaises(ww.WindowsWslContractError):
            ww.select_stale_instances(instances, now, timedelta(hours=1))

    def test_rejects_naive_aware_mismatch_raises_contract_error_not_typeerror(self):
        now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)
        instances = [
            ww.InstanceRecord("agent-bridge-abc123", datetime(2026, 9, 14, 6, 0, 0))
        ]
        with self.assertRaises(ww.WindowsWslContractError):
            ww.select_stale_instances(instances, now, timedelta(hours=1))

    def test_accepts_aware_datetimes_consistently(self):
        now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)
        instances = [
            ww.InstanceRecord(
                "agent-bridge-abc123", now - timedelta(hours=5)
            )
        ]
        stale = ww.select_stale_instances(instances, now, timedelta(hours=1))
        self.assertEqual(stale, ["agent-bridge-abc123"])

    def test_is_pure_no_enumeration_no_deletion(self):
        # No filesystem/process access is possible: the function only takes
        # supplied records and returns a list of names. This test asserts the
        # function signature contract by construction (empty input -> empty
        # output, no side effects observable).
        now = datetime(2026, 9, 14, 12, 0, 0)
        self.assertEqual(ww.select_stale_instances([], now, timedelta(hours=1)), [])


if __name__ == "__main__":
    unittest.main()
