"""Windows WSL2 core contract scaffolding (phase one).

This module is **phase-one contract scaffolding, not live Windows
delegation**. It defines, in pure standard-library Python, the strict
data contracts that any future Windows/WSL2 delegation path must honor:
an immutable pinned base-image manifest schema, Windows/WSL prerequisite
result structures, safe ephemeral distro naming, a guest security
boundary specification, strict job-spec validation, pure argv builders
for `wsl.exe`, and pure stale-instance selection logic.

Nothing in this module mutates state, spawns processes, makes network
calls, or touches the filesystem. It is intentionally importable and
testable on macOS, Linux, and Windows without WSL installed.

Real enablement of Windows delegation requires, at minimum: a pinned
and independently verified WSL2 base image (with a recorded rootfs
SHA-256), and live synthetic evidence collected on actual Windows
hosts. Until that evidence exists, nothing in this module should be
treated as proof that WSL delegation works.
"""

from __future__ import annotations

import ntpath
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class WindowsWslContractError(ValueError):
    """Raised when a value violates the Windows/WSL2 contract."""


class ManifestError(WindowsWslContractError):
    """Raised when a base-image manifest is malformed or unpinned."""


class JobSpecError(WindowsWslContractError):
    """Raised when a guest job specification is unsafe or malformed."""


class DistroNameError(WindowsWslContractError):
    """Raised when a proposed ephemeral distro name is unsafe."""


# ---------------------------------------------------------------------------
# Pinned base-image manifest
# ---------------------------------------------------------------------------

_MANIFEST_KEYS = (
    "schema_version",
    "distro_release",
    "rootfs_sha256",
    "node_version",
    "claude_version",
    "codex_version",
)

_UNPINNED_TOKENS = frozenset(
    {
        "latest",
        "stable",
        "default",
        "current",
        "head",
        "master",
        "main",
        "",
    }
)

_ROOTFS_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
_PINNED_VERSION_RE = re.compile(r"^[0-9][0-9A-Za-z_.+-]*$")


def _reject_unsafe_string(name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise ManifestError(f"{name} must be a string, got {type(value).__name__}")
    if _CONTROL_CHAR_RE.search(value):
        raise ManifestError(f"{name} contains control characters")
    if ".." in value or "/" in value or "\\" in value:
        raise ManifestError(f"{name} contains path traversal or separators")
    stripped = value.strip()
    if stripped != value:
        raise ManifestError(f"{name} has leading/trailing whitespace")
    if value.lower() in _UNPINNED_TOKENS:
        raise ManifestError(f"{name} is unpinned ({value!r})")
    return value


def _reject_unpinned_version(name: str, value: Any) -> str:
    value = _reject_unsafe_string(name, value)
    if not _PINNED_VERSION_RE.match(value):
        raise ManifestError(f"{name} is not a pinned version string: {value!r}")
    return value


@dataclass(frozen=True)
class PinnedBaseImageManifest:
    """An immutable, fully-pinned WSL2 base image manifest."""

    schema_version: int
    distro_release: str
    rootfs_sha256: str
    node_version: str
    claude_version: str
    codex_version: str


def parse_manifest(data: Mapping[str, Any]) -> PinnedBaseImageManifest:
    """Strictly parse a pinned base-image manifest.

    Rejects: unknown/missing keys, wrong types, traversal/control
    characters, and any unpinned value such as ``latest``/``stable``/
    ``default``.
    """

    if not isinstance(data, Mapping):
        raise ManifestError("manifest must be a mapping")

    raw_keys = list(data.keys())
    if any(not isinstance(key, str) for key in raw_keys):
        raise ManifestError("manifest keys must be strings")

    keys = set(raw_keys)
    expected = set(_MANIFEST_KEYS)
    if keys != expected:
        missing = expected - keys
        extra = keys - expected
        raise ManifestError(
            f"manifest keys must be exactly {sorted(expected)}; "
            f"missing={sorted(missing)} extra={sorted(extra)}"
        )

    schema_version = data["schema_version"]
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise ManifestError("schema_version must be an int")
    if schema_version != 1:
        raise ManifestError("schema_version must be exactly 1")

    distro_release = _reject_unpinned_version("distro_release", data["distro_release"])

    rootfs_sha256 = data["rootfs_sha256"]
    if not isinstance(rootfs_sha256, str) or not _ROOTFS_SHA256_RE.match(rootfs_sha256):
        raise ManifestError(
            "rootfs_sha256 must be 64 lowercase hex characters"
        )

    node_version = _reject_unpinned_version("node_version", data["node_version"])
    claude_version = _reject_unpinned_version("claude_version", data["claude_version"])
    codex_version = _reject_unpinned_version("codex_version", data["codex_version"])

    return PinnedBaseImageManifest(
        schema_version=schema_version,
        distro_release=distro_release,
        rootfs_sha256=rootfs_sha256,
        node_version=node_version,
        claude_version=claude_version,
        codex_version=codex_version,
    )


# ---------------------------------------------------------------------------
# Windows / WSL prerequisite checks (parsing only, fail closed)
# ---------------------------------------------------------------------------

WINDOWS_MIN_BUILD = 22621
WSL_MIN_VERSION = (2, 0, 0)

_WINDOWS_VER_RE = re.compile(r"\[Version (\d+)\.(\d+)\.(\d+)(?:\.(\d+))?\]")
_WSL_VERSION_LINE_RE = re.compile(
    r"^WSL version:\s*(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?\s*$", re.MULTILINE
)


@dataclass(frozen=True)
class PrerequisiteCheckResult:
    """Structured, fail-closed result of a prerequisite check."""

    passed: bool
    detail: str
    value: str | None = None


def parse_windows_build(cmd_ver_output: str) -> PrerequisiteCheckResult:
    """Parse representative ``cmd /c ver`` output.

    Example input: ``Microsoft Windows [Version 10.0.22631.3527]``.
    Never infers success from missing or unparseable output.
    """

    if not isinstance(cmd_ver_output, str) or not cmd_ver_output.strip():
        return PrerequisiteCheckResult(False, "empty or missing ver output", None)

    match = _WINDOWS_VER_RE.search(cmd_ver_output)
    if not match:
        return PrerequisiteCheckResult(False, "could not parse Windows version", None)

    build = int(match.group(3))
    value = f"{match.group(1)}.{match.group(2)}.{match.group(3)}"
    if build < WINDOWS_MIN_BUILD:
        return PrerequisiteCheckResult(
            False, f"Windows build {build} < required {WINDOWS_MIN_BUILD}", value
        )
    return PrerequisiteCheckResult(True, "Windows build meets minimum", value)


def parse_wsl_version(wsl_version_output: str) -> PrerequisiteCheckResult:
    """Parse representative ``wsl.exe --version`` output.

    Never infers success from missing or unparseable output.
    """

    if not isinstance(wsl_version_output, str) or not wsl_version_output.strip():
        return PrerequisiteCheckResult(False, "empty or missing wsl --version output", None)

    match = _WSL_VERSION_LINE_RE.search(wsl_version_output)
    if not match:
        return PrerequisiteCheckResult(False, "could not parse WSL version", None)

    version = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    value = ".".join(str(part) for part in version)
    if version < WSL_MIN_VERSION:
        return PrerequisiteCheckResult(
            False,
            f"WSL version {value} < required {'.'.join(str(p) for p in WSL_MIN_VERSION)}",
            value,
        )
    return PrerequisiteCheckResult(True, "WSL version meets minimum", value)


def all_prerequisites_met(
    windows_build: PrerequisiteCheckResult,
    wsl_version: PrerequisiteCheckResult,
    *,
    virtual_machine_platform_enabled: bool,
    firmware_virtualization_enabled: bool,
) -> bool:
    """Pure aggregate of all Windows/WSL2 prerequisites.

    Fails closed: passes only when both parsed results explicitly
    passed and both boolean feature flags are explicitly ``True``.
    Unknown, missing, or non-bool values never count as satisfied.
    """

    if not isinstance(windows_build, PrerequisiteCheckResult):
        return False
    if not isinstance(wsl_version, PrerequisiteCheckResult):
        return False
    if windows_build.passed is not True:
        return False
    if wsl_version.passed is not True:
        return False
    if virtual_machine_platform_enabled is not True:
        return False
    if firmware_virtualization_enabled is not True:
        return False
    return True


# ---------------------------------------------------------------------------
# Safe ephemeral distro names
# ---------------------------------------------------------------------------

DISTRO_NAME_PREFIX = "agent-bridge-"
_DISTRO_TOKEN_RE = re.compile(r"^[0-9a-f]{6,32}$")
DISTRO_NAME_MAX_LENGTH = len(DISTRO_NAME_PREFIX) + 32


def validate_distro_name(name: str) -> str:
    """Validate a fully-formed ephemeral distro name.

    Must be exactly ``agent-bridge-`` followed by 6-32 lowercase hex
    characters, and stay within the bounded maximum length.
    """

    if not isinstance(name, str):
        raise DistroNameError("distro name must be a string")
    if len(name) > DISTRO_NAME_MAX_LENGTH:
        raise DistroNameError("distro name exceeds maximum length")
    if not name.startswith(DISTRO_NAME_PREFIX):
        raise DistroNameError(f"distro name must start with {DISTRO_NAME_PREFIX!r}")
    token = name[len(DISTRO_NAME_PREFIX):]
    if not _DISTRO_TOKEN_RE.match(token):
        raise DistroNameError(
            "distro name suffix must be 6-32 lowercase hex characters"
        )
    return name


def build_distro_name(token: str) -> str:
    """Build and validate a distro name from a lowercase-hex token."""

    if not isinstance(token, str) or not _DISTRO_TOKEN_RE.match(token):
        raise DistroNameError("token must be 6-32 lowercase hex characters")
    return validate_distro_name(DISTRO_NAME_PREFIX + token)


# ---------------------------------------------------------------------------
# Guest security boundary
# ---------------------------------------------------------------------------

WSL_CONF_CONTENTS = (
    "[automount]\n"
    "enabled = false\n"
    "\n"
    "[interop]\n"
    "enabled = false\n"
    "appendWindowsPath = false\n"
)

ALLOWED_GUEST_ENV_KEYS = frozenset(
    {
        "HOME",
        "PATH",
        "LANG",
        "LC_ALL",
        "TERM",
        "AGENT_BRIDGE_JOB_ID",
    }
)


def build_guest_environment(overrides: Mapping[str, str]) -> dict[str, str]:
    """Build a guest environment from an explicit allowlist only.

    Never reads or inherits host process environment. Any key outside
    :data:`ALLOWED_GUEST_ENV_KEYS` is rejected.
    """

    if not isinstance(overrides, Mapping):
        raise JobSpecError("guest environment overrides must be a mapping")

    result: dict[str, str] = {}
    for key, value in overrides.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise JobSpecError("guest environment keys/values must be strings")
        if key not in ALLOWED_GUEST_ENV_KEYS:
            raise JobSpecError(f"guest environment key not allowlisted: {key!r}")
        if _CONTROL_CHAR_RE.search(value):
            raise JobSpecError(f"guest environment value for {key!r} has control characters")
        if _looks_like_windows_path(value) or _contains_host_marker(value):
            raise JobSpecError(f"guest environment value for {key!r} references a host path: {value!r}")
        result[key] = value
    return result


# ---------------------------------------------------------------------------
# Job spec validation
# ---------------------------------------------------------------------------

_JOB_SPEC_KEYS = frozenset({"command", "workdir", "env"})

ALLOWED_GUEST_PATH_PREFIXES = ("/root/", "/home/", "/tmp/", "/workspace/")

GUEST_RUNNER_PATH = "/usr/local/bin/agent-bridge-guest-runner"

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_UNC_PATH_RE = re.compile(r"^\\\\")
_UNC_PATH_SLASH_RE = re.compile(r"^//[^/]")
_DRIVE_AFTER_PREFIX_RE = re.compile(r"(^|[=\s:])[A-Za-z]:[\\/]")

_SECRET_KEY_RE = re.compile(
    r"(secret|token|password|passwd|credential|apikey|api_key|private_key)",
    re.IGNORECASE,
)

_HOST_FALLBACK_KEYS = frozenset(
    {"allow_host_fallback", "host_fallback", "fallback_to_host", "use_host"}
)

_MOUNT_OR_SYMLINK_KEYS = frozenset({"symlinks", "mounts", "mount", "symlink"})


@dataclass(frozen=True)
class GuestJobSpec:
    """A validated, guest-only job specification."""

    command: tuple[str, ...]
    workdir: str
    env: Mapping[str, str] = field(default_factory=dict)


def _looks_like_windows_path(value: str) -> bool:
    if _WINDOWS_DRIVE_RE.match(value):
        return True
    if _UNC_PATH_RE.match(value):
        return True
    if _UNC_PATH_SLASH_RE.match(value):
        return True
    if "\\" in value:
        return True
    return False


def _contains_host_marker(value: str) -> bool:
    """Detect host-mount markers or Windows paths anywhere within a string,
    including when embedded after ``KEY=`` or option-style prefixes."""

    if "\\" in value:
        return True
    if "/mnt/" in value:
        return True
    if value == "/mnt":
        return True
    if _UNC_PATH_RE.search(value) or _UNC_PATH_SLASH_RE.match(value):
        return True
    if _DRIVE_AFTER_PREFIX_RE.search(value):
        return True
    if ".." in re.split(r"[\\/]", value):
        return True
    return False


def _reject_host_path(name: str, value: str) -> None:
    if not isinstance(value, str) or not value:
        raise JobSpecError(f"{name} must be a non-empty string")
    if _CONTROL_CHAR_RE.search(value):
        raise JobSpecError(f"{name} contains control characters")
    if _looks_like_windows_path(value):
        raise JobSpecError(f"{name} looks like a Windows path: {value!r}")
    if _contains_host_marker(value):
        raise JobSpecError(f"{name} references a host path or mount: {value!r}")
    if ".." in value.split("/"):
        raise JobSpecError(f"{name} contains path traversal: {value!r}")


def validate_job_spec(spec: Mapping[str, Any]) -> GuestJobSpec:
    """Strictly validate a guest job specification.

    Rejects Windows paths, UNC paths, ``/mnt/*`` paths, host checkout
    paths, symlink/mount declarations, secret-looking environment
    keys, unknown fields, and any host-fallback option.
    """

    if not isinstance(spec, Mapping):
        raise JobSpecError("job spec must be a mapping")

    keys = set(spec.keys())
    if any(not isinstance(key, str) for key in keys):
        raise JobSpecError("job spec keys must be strings")
    if keys & _HOST_FALLBACK_KEYS:
        raise JobSpecError("host-fallback options are not permitted")
    if keys & _MOUNT_OR_SYMLINK_KEYS:
        raise JobSpecError("symlink/mount declarations are not permitted")
    if keys != _JOB_SPEC_KEYS:
        unknown = keys - _JOB_SPEC_KEYS
        missing = _JOB_SPEC_KEYS - keys
        raise JobSpecError(
            f"job spec keys must be exactly {sorted(_JOB_SPEC_KEYS)}; "
            f"unknown={sorted(unknown)} missing={sorted(missing)}"
        )

    command = spec["command"]
    if not isinstance(command, Sequence) or isinstance(command, (str, bytes)):
        raise JobSpecError("command must be a list of strings")
    if not command:
        raise JobSpecError("command must not be empty")
    if command[0] != GUEST_RUNNER_PATH:
        raise JobSpecError(
            f"command must invoke the pinned guest runner {GUEST_RUNNER_PATH!r}, "
            f"got {command[0]!r}"
        )
    validated_command = []
    for arg in command:
        if not isinstance(arg, str):
            raise JobSpecError("command arguments must be strings")
        if _CONTROL_CHAR_RE.search(arg):
            raise JobSpecError("command argument contains control characters")
        if _looks_like_windows_path(arg) or _contains_host_marker(arg):
            raise JobSpecError(f"command argument references a host path: {arg!r}")
        validated_command.append(arg)

    workdir = spec["workdir"]
    _reject_host_path("workdir", workdir)
    if not any(workdir.startswith(prefix) for prefix in ALLOWED_GUEST_PATH_PREFIXES):
        raise JobSpecError(
            f"workdir must be within an allowlisted guest path prefix: {workdir!r}"
        )

    env_input = spec["env"]
    if not isinstance(env_input, Mapping):
        raise JobSpecError("env must be a mapping")
    for key in env_input:
        if not isinstance(key, str):
            raise JobSpecError("env keys must be strings")
        if _SECRET_KEY_RE.search(key):
            raise JobSpecError(f"env key looks like a secret/credential: {key!r}")
    env = build_guest_environment(env_input)

    return GuestJobSpec(
        command=tuple(validated_command),
        workdir=workdir,
        env=env,
    )


# ---------------------------------------------------------------------------
# Pure argv builders (never shell strings)
# ---------------------------------------------------------------------------

_SHELL_UNSAFE_RE = re.compile(r"[\x00-\x1f\x7f]")


def _reject_shell_unsafe(name: str, value: str) -> str:
    if not isinstance(value, str) or not value:
        raise WindowsWslContractError(f"{name} must be a non-empty string")
    if _SHELL_UNSAFE_RE.search(value):
        raise WindowsWslContractError(f"{name} contains control characters")
    return value


_UNC_PREFIXES = ("\\\\", "//")
_DEVICE_PREFIXES = ("\\\\.\\", "\\\\?\\", "//./", "//?/")


def _validate_windows_host_path(name: str, value: str) -> str:
    """Validate an absolute native Windows path (not UNC/device/relative)."""

    _reject_shell_unsafe(name, value)
    if ".." in re.split(r"[\\/]", value):
        raise WindowsWslContractError(f"{name} contains path traversal: {value!r}")
    for prefix in _DEVICE_PREFIXES:
        if value.startswith(prefix):
            raise WindowsWslContractError(f"{name} is a device path: {value!r}")
    for prefix in _UNC_PREFIXES:
        if value.startswith(prefix):
            raise WindowsWslContractError(f"{name} is a UNC/network path: {value!r}")
    drive, tail = ntpath.splitdrive(value)
    if not drive or not re.match(r"^[A-Za-z]:$", drive):
        raise WindowsWslContractError(f"{name} must be an absolute drive path: {value!r}")
    if not tail.startswith("\\") and not tail.startswith("/"):
        raise WindowsWslContractError(f"{name} must be an absolute path: {value!r}")
    if not ntpath.isabs(value):
        raise WindowsWslContractError(f"{name} must be an absolute path: {value!r}")
    return value


def build_import_argv(
    distro_name: str, install_dir: str, rootfs_path: str, *, wsl_version: int = 2
) -> list[str]:
    """Build argv for ``wsl.exe --import``. Host install/rootfs paths are
    legitimate here since this targets the Windows host, not the guest."""

    validate_distro_name(distro_name)
    _validate_windows_host_path("install_dir", install_dir)
    _validate_windows_host_path("rootfs_path", rootfs_path)
    if wsl_version != 2:
        raise WindowsWslContractError("wsl_version must be 2 (WSL1 is not permitted)")
    return [
        "wsl.exe",
        "--import",
        distro_name,
        install_dir,
        rootfs_path,
        "--version",
        str(wsl_version),
    ]


def build_terminate_argv(distro_name: str) -> list[str]:
    validate_distro_name(distro_name)
    return ["wsl.exe", "--terminate", distro_name]


def build_unregister_argv(distro_name: str) -> list[str]:
    validate_distro_name(distro_name)
    return ["wsl.exe", "--unregister", distro_name]


def build_guest_exec_argv(distro_name: str, job: GuestJobSpec) -> list[str]:
    """Build argv for executing a validated job inside the guest.

    No host path may appear in the resulting argv: ``job`` is already
    strictly validated by :func:`validate_job_spec`.
    """

    validate_distro_name(distro_name)
    if not isinstance(job, GuestJobSpec):
        raise WindowsWslContractError("job must be a validated GuestJobSpec")

    argv = ["wsl.exe", "-d", distro_name, "--cd", job.workdir, "--", "env", "-i"]
    for key in sorted(job.env):
        argv.append(f"{key}={job.env[key]}")
    argv.extend(job.command)

    for arg in argv:
        if _looks_like_windows_path(arg) or _contains_host_marker(arg):
            raise WindowsWslContractError(
                f"refusing to build guest exec argv containing a host path: {arg!r}"
            )
    return argv


# ---------------------------------------------------------------------------
# Pure stale-instance selection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstanceRecord:
    """A supplied (not enumerated) ephemeral WSL instance record."""

    name: str
    created_at: datetime


def select_stale_instances(
    instances: Sequence[InstanceRecord], now: datetime, max_age: timedelta
) -> list[str]:
    """Pure selection of stale ephemeral instance names.

    Only considers records already supplied by the caller; performs no
    enumeration or deletion. Only ever selects names matching the
    ``agent-bridge-`` safety prefix, regardless of age.
    """

    if not isinstance(max_age, timedelta):
        raise WindowsWslContractError("max_age must be a timedelta")
    if max_age <= timedelta(0):
        raise WindowsWslContractError("max_age must be positive")
    if not isinstance(now, datetime):
        raise WindowsWslContractError("now must be a datetime")

    stale: list[str] = []
    for record in instances:
        if not isinstance(record, InstanceRecord):
            raise WindowsWslContractError("instances must be InstanceRecord values")
        if not isinstance(record.created_at, datetime):
            raise WindowsWslContractError("created_at must be a datetime")
        if (record.created_at.tzinfo is None) != (now.tzinfo is None):
            raise WindowsWslContractError(
                "now and created_at must be both naive or both timezone-aware"
            )
        try:
            validate_distro_name(record.name)
        except DistroNameError:
            continue
        try:
            age_exceeded = now - record.created_at >= max_age
        except TypeError as exc:
            raise WindowsWslContractError(
                f"incompatible datetime values: {exc}"
            ) from exc
        if age_exceeded:
            stale.append(record.name)
    return stale
