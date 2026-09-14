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

from datetime import datetime, timedelta

import pytest

from agent_bridge.orchestration import windows_wsl as ww


VALID_MANIFEST = {
    "schema_version": 1,
    "distro_release": "22.04.3",
    "rootfs_sha256": "a" * 64,
    "node_version": "20.11.1",
    "claude_version": "1.2.3",
    "codex_version": "0.9.0",
}


# ---------------------------------------------------------------------------
# Manifest parsing
# ---------------------------------------------------------------------------


def test_parse_manifest_accepts_valid_pinned_manifest():
    manifest = ww.parse_manifest(VALID_MANIFEST)
    assert manifest.distro_release == "22.04.3"
    assert manifest.rootfs_sha256 == "a" * 64


@pytest.mark.parametrize("missing_key", list(VALID_MANIFEST.keys()))
def test_parse_manifest_rejects_missing_key(missing_key):
    data = dict(VALID_MANIFEST)
    del data[missing_key]
    with pytest.raises(ww.ManifestError):
        ww.parse_manifest(data)


def test_parse_manifest_rejects_extra_key():
    data = dict(VALID_MANIFEST)
    data["extra_field"] = "x"
    with pytest.raises(ww.ManifestError):
        ww.parse_manifest(data)


def test_parse_manifest_rejects_wrong_type():
    data = dict(VALID_MANIFEST)
    data["schema_version"] = "1"
    with pytest.raises(ww.ManifestError):
        ww.parse_manifest(data)


def test_parse_manifest_rejects_bool_schema_version():
    data = dict(VALID_MANIFEST)
    data["schema_version"] = True
    with pytest.raises(ww.ManifestError):
        ww.parse_manifest(data)


@pytest.mark.parametrize("token", ["latest", "stable", "default", "LATEST", "current", "head"])
@pytest.mark.parametrize(
    "field", ["distro_release", "node_version", "claude_version", "codex_version"]
)
def test_parse_manifest_rejects_unpinned_values(field, token):
    data = dict(VALID_MANIFEST)
    data[field] = token
    with pytest.raises(ww.ManifestError):
        ww.parse_manifest(data)


def test_parse_manifest_rejects_path_traversal():
    data = dict(VALID_MANIFEST)
    data["node_version"] = "../../etc/passwd"
    with pytest.raises(ww.ManifestError):
        ww.parse_manifest(data)


def test_parse_manifest_rejects_control_characters():
    data = dict(VALID_MANIFEST)
    data["claude_version"] = "1.2.3\x00"
    with pytest.raises(ww.ManifestError):
        ww.parse_manifest(data)


def test_parse_manifest_rejects_bad_rootfs_sha256_case():
    data = dict(VALID_MANIFEST)
    data["rootfs_sha256"] = "A" * 64
    with pytest.raises(ww.ManifestError):
        ww.parse_manifest(data)


def test_parse_manifest_rejects_short_rootfs_sha256():
    data = dict(VALID_MANIFEST)
    data["rootfs_sha256"] = "a" * 63
    with pytest.raises(ww.ManifestError):
        ww.parse_manifest(data)


def test_parse_manifest_rejects_non_mapping():
    with pytest.raises(ww.ManifestError):
        ww.parse_manifest(["not", "a", "mapping"])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Windows / WSL prerequisite parsing
# ---------------------------------------------------------------------------


def test_windows_min_build_constant():
    assert ww.WINDOWS_MIN_BUILD == 22621


def test_wsl_min_version_constant():
    assert ww.WSL_MIN_VERSION == (2, 0, 0)


def test_parse_windows_build_passes_above_minimum():
    output = "Microsoft Windows [Version 10.0.22631.3527]"
    result = ww.parse_windows_build(output)
    assert result.passed is True
    assert result.value == "10.0.22631"


def test_parse_windows_build_fails_below_minimum():
    output = "Microsoft Windows [Version 10.0.19045.1]"
    result = ww.parse_windows_build(output)
    assert result.passed is False


def test_parse_windows_build_fails_closed_on_garbage():
    result = ww.parse_windows_build("not a real ver string")
    assert result.passed is False
    assert result.value is None


def test_parse_windows_build_fails_closed_on_empty():
    result = ww.parse_windows_build("")
    assert result.passed is False


def test_parse_windows_build_fails_closed_on_none():
    result = ww.parse_windows_build(None)  # type: ignore[arg-type]
    assert result.passed is False


def test_parse_wsl_version_passes_above_minimum():
    output = (
        "WSL version: 2.0.9.0\n"
        "Kernel version: 5.15.146.1-2\n"
        "WSLg version: 1.0.60\n"
        "Windows version: 10.0.22631.3527\n"
    )
    result = ww.parse_wsl_version(output)
    assert result.passed is True
    assert result.value == "2.0.9"


def test_parse_wsl_version_fails_below_minimum():
    output = "WSL version: 1.2.5.0\nKernel version: 4.19\n"
    result = ww.parse_wsl_version(output)
    assert result.passed is False


def test_parse_wsl_version_fails_closed_on_garbage():
    result = ww.parse_wsl_version("garbage output with no version")
    assert result.passed is False
    assert result.value is None


def test_parse_wsl_version_fails_closed_on_empty():
    assert ww.parse_wsl_version("").passed is False


# ---------------------------------------------------------------------------
# Safe ephemeral distro names
# ---------------------------------------------------------------------------


def test_validate_distro_name_accepts_valid_name():
    assert ww.validate_distro_name("agent-bridge-abc123") == "agent-bridge-abc123"


def test_build_distro_name_from_hex_token():
    assert ww.build_distro_name("deadbeef") == "agent-bridge-deadbeef"


@pytest.mark.parametrize(
    "name",
    [
        "agent-bridge-ABC123",  # uppercase not allowed
        "agent-bridge-",  # empty token
        "agent-bridge-xyz",  # non-hex chars
        "not-agent-bridge-abc123",  # wrong prefix
        "agent-bridge-" + "a" * 40,  # too long
        "",
        "agent-bridge-../etc",
    ],
)
def test_validate_distro_name_rejects_unsafe_names(name):
    with pytest.raises(ww.DistroNameError):
        ww.validate_distro_name(name)


def test_build_distro_name_rejects_uppercase_token():
    with pytest.raises(ww.DistroNameError):
        ww.build_distro_name("ABCDEF")


# ---------------------------------------------------------------------------
# Guest boundary spec
# ---------------------------------------------------------------------------


def test_wsl_conf_disables_automount_and_interop():
    assert "enabled = false" in ww.WSL_CONF_CONTENTS
    assert "[automount]" in ww.WSL_CONF_CONTENTS
    assert "[interop]" in ww.WSL_CONF_CONTENTS


def test_build_guest_environment_allows_allowlisted_keys():
    env = ww.build_guest_environment({"HOME": "/root", "PATH": "/usr/bin"})
    assert env == {"HOME": "/root", "PATH": "/usr/bin"}


def test_build_guest_environment_rejects_non_allowlisted_key():
    with pytest.raises(ww.JobSpecError):
        ww.build_guest_environment({"HOST_SECRET": "x"})


def test_build_guest_environment_never_inherits_host(monkeypatch):
    monkeypatch.setenv("HOME", "/should/not/leak")
    env = ww.build_guest_environment({})
    assert env == {}


def test_build_guest_environment_rejects_control_chars():
    with pytest.raises(ww.JobSpecError):
        ww.build_guest_environment({"HOME": "/root\x00"})


# ---------------------------------------------------------------------------
# Job spec validation
# ---------------------------------------------------------------------------


def _valid_job_spec(**overrides):
    spec = {
        "command": ["echo", "hello"],
        "workdir": "/workspace/job",
        "env": {"HOME": "/root"},
    }
    spec.update(overrides)
    return spec


def test_validate_job_spec_accepts_valid_spec():
    job = ww.validate_job_spec(_valid_job_spec())
    assert job.command == ("echo", "hello")
    assert job.workdir == "/workspace/job"


@pytest.mark.parametrize(
    "workdir",
    [
        "C:\\Users\\bob\\project",
        "c:/Users/bob/project",
        "\\\\server\\share\\project",
        "/mnt/c/Users/bob",
        "/mnt/d/checkout",
    ],
)
def test_validate_job_spec_rejects_windows_and_mount_paths(workdir):
    with pytest.raises(ww.JobSpecError):
        ww.validate_job_spec(_valid_job_spec(workdir=workdir))


def test_validate_job_spec_rejects_host_checkout_like_path():
    with pytest.raises(ww.JobSpecError):
        ww.validate_job_spec(_valid_job_spec(workdir="/Users/bob/repo"))


def test_validate_job_spec_rejects_path_traversal_in_workdir():
    with pytest.raises(ww.JobSpecError):
        ww.validate_job_spec(_valid_job_spec(workdir="/workspace/../etc"))


def test_validate_job_spec_rejects_symlinks_key():
    spec = _valid_job_spec()
    spec["symlinks"] = [{"from": "/tmp/a", "to": "/tmp/b"}]
    with pytest.raises(ww.JobSpecError):
        ww.validate_job_spec(spec)


def test_validate_job_spec_rejects_mounts_key():
    spec = _valid_job_spec()
    spec["mounts"] = ["/host/path:/guest/path"]
    with pytest.raises(ww.JobSpecError):
        ww.validate_job_spec(spec)


@pytest.mark.parametrize(
    "key", ["AWS_SECRET_ACCESS_KEY", "api_key", "password", "DB_CREDENTIAL", "auth_token"]
)
def test_validate_job_spec_rejects_secret_looking_env_keys(key):
    spec = _valid_job_spec(env={key: "x"})
    with pytest.raises(ww.JobSpecError):
        ww.validate_job_spec(spec)


def test_validate_job_spec_rejects_unknown_field():
    spec = _valid_job_spec()
    spec["extra"] = "nope"
    with pytest.raises(ww.JobSpecError):
        ww.validate_job_spec(spec)


@pytest.mark.parametrize(
    "key", ["allow_host_fallback", "host_fallback", "fallback_to_host", "use_host"]
)
def test_validate_job_spec_rejects_any_host_fallback_option(key):
    spec = _valid_job_spec()
    spec[key] = False  # even explicitly false must be rejected: presence alone
    with pytest.raises(ww.JobSpecError):
        ww.validate_job_spec(spec)


def test_validate_job_spec_rejects_empty_command():
    with pytest.raises(ww.JobSpecError):
        ww.validate_job_spec(_valid_job_spec(command=[]))


def test_validate_job_spec_rejects_command_with_host_path():
    with pytest.raises(ww.JobSpecError):
        ww.validate_job_spec(_valid_job_spec(command=["cat", "/mnt/c/secret.txt"]))


def test_validate_job_spec_rejects_non_mapping():
    with pytest.raises(ww.JobSpecError):
        ww.validate_job_spec(["not", "a", "mapping"])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pure argv builders
# ---------------------------------------------------------------------------


def test_build_import_argv():
    argv = ww.build_import_argv(
        "agent-bridge-abc123", "C:\\wsl\\agent-bridge-abc123", "C:\\images\\rootfs.tar"
    )
    assert argv == [
        "wsl.exe",
        "--import",
        "agent-bridge-abc123",
        "C:\\wsl\\agent-bridge-abc123",
        "C:\\images\\rootfs.tar",
        "--version",
        "2",
    ]
    assert isinstance(argv, list)
    assert all(isinstance(part, str) for part in argv)


def test_build_import_argv_rejects_unsafe_distro_name():
    with pytest.raises(ww.DistroNameError):
        ww.build_import_argv("not-safe", "C:\\x", "C:\\y")


def test_build_import_argv_rejects_bad_wsl_version():
    with pytest.raises(ww.WindowsWslContractError):
        ww.build_import_argv("agent-bridge-abc123", "C:\\x", "C:\\y", wsl_version=3)


def test_build_terminate_argv():
    assert ww.build_terminate_argv("agent-bridge-abc123") == [
        "wsl.exe",
        "--terminate",
        "agent-bridge-abc123",
    ]


def test_build_unregister_argv():
    assert ww.build_unregister_argv("agent-bridge-abc123") == [
        "wsl.exe",
        "--unregister",
        "agent-bridge-abc123",
    ]


def test_build_terminate_argv_rejects_unsafe_name():
    with pytest.raises(ww.DistroNameError):
        ww.build_terminate_argv("evil; rm -rf /")


def test_build_guest_exec_argv_no_host_path():
    job = ww.validate_job_spec(_valid_job_spec())
    argv = ww.build_guest_exec_argv("agent-bridge-abc123", job)
    assert argv[0] == "wsl.exe"
    assert "--" in argv
    for part in argv:
        assert "\\" not in part
        assert not part.startswith("/mnt/")
        assert not part.upper().startswith("C:")


def test_build_guest_exec_argv_rejects_non_validated_job():
    with pytest.raises(ww.WindowsWslContractError):
        ww.build_guest_exec_argv("agent-bridge-abc123", _valid_job_spec())  # type: ignore[arg-type]


def test_build_guest_exec_argv_is_argv_list_not_shell_string():
    job = ww.validate_job_spec(_valid_job_spec(command=["echo", "a b c"]))
    argv = ww.build_guest_exec_argv("agent-bridge-abc123", job)
    assert isinstance(argv, list)
    assert "a b c" in argv  # preserved as a single argument, not shell-joined


# ---------------------------------------------------------------------------
# Pure stale-instance selection
# ---------------------------------------------------------------------------


def test_select_stale_instances_selects_old_ones_only():
    now = datetime(2026, 9, 14, 12, 0, 0)
    instances = [
        ww.InstanceRecord("agent-bridge-abc123", now - timedelta(hours=5)),
        ww.InstanceRecord("agent-bridge-def456", now - timedelta(minutes=5)),
    ]
    stale = ww.select_stale_instances(instances, now, timedelta(hours=1))
    assert stale == ["agent-bridge-abc123"]


def test_select_stale_instances_ignores_non_prefixed_names():
    now = datetime(2026, 9, 14, 12, 0, 0)
    instances = [
        ww.InstanceRecord("Ubuntu-22.04", now - timedelta(days=10)),
        ww.InstanceRecord("Docker-desktop", now - timedelta(days=10)),
    ]
    stale = ww.select_stale_instances(instances, now, timedelta(hours=1))
    assert stale == []


def test_select_stale_instances_rejects_non_positive_max_age():
    now = datetime(2026, 9, 14, 12, 0, 0)
    with pytest.raises(ww.WindowsWslContractError):
        ww.select_stale_instances([], now, timedelta(0))


def test_select_stale_instances_is_pure_no_enumeration_no_deletion():
    # No filesystem/process access is possible: the function only takes
    # supplied records and returns a list of names. This test asserts the
    # function signature contract by construction (empty input -> empty
    # output, no side effects observable).
    now = datetime(2026, 9, 14, 12, 0, 0)
    assert ww.select_stale_instances([], now, timedelta(hours=1)) == []
