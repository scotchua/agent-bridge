"""Read-only, standard-library Windows preflight collector.

This module collects evidence about whether a native Windows host
*might* be eligible for future WSL2 delegation. It never enables
features, elevates privileges, downloads anything, imports or
unregisters WSL distros, or mutates any state. It runs a small set of
bounded, argv-based, non-shell subprocess calls with a scrubbed
environment and a hard timeout, and it parses their output using the
pure contracts in :mod:`windows_wsl`.

Overall status is ``prerequisites_ready`` only when every individual
check explicitly passed. Any missing executable, nonzero exit,
timeout, decode/parse failure, disabled/unknown feature, missing or
malformed manifest, or rootfs hash mismatch is reported as an explicit
failed check and forces overall status to
``prerequisites_not_ready``. On non-Windows platforms, this module
returns ``unsupported_platform`` without spawning any process.

Nothing here should ever be interpreted as "delegation enabled" or
"delegation verified" -- it is preflight evidence only.
"""

from __future__ import annotations

import subprocess
import sys
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .windows_wsl import (
    PrerequisiteCheckResult,
    all_prerequisites_met,
    parse_manifest,
    parse_windows_build,
    parse_wsl_version,
    ManifestError,
)

COMMAND_TIMEOUT_SECONDS = 10
MAX_COMMAND_OUTPUT_BYTES = 64 * 1024

STATUS_UNSUPPORTED_PLATFORM = "unsupported_platform"
STATUS_PREREQUISITES_READY = "prerequisites_ready"
STATUS_PREREQUISITES_NOT_READY = "prerequisites_not_ready"

_MINIMAL_ENV = {
    "PATH": r"C:\Windows\System32;C:\Windows",
    "SYSTEMROOT": r"C:\Windows",
    "SYSTEMDRIVE": "C:",
}

_CMD_VER_ARGV = ["cmd.exe", "/d", "/s", "/c", "ver"]
_WSL_VERSION_ARGV = ["wsl.exe", "--version"]
_VMP_FEATURE_ARGV = [
    "powershell.exe",
    "-NoProfile",
    "-NonInteractive",
    "-NoLogo",
    "-Command",
    "Get-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform "
    "| Select-Object -ExpandProperty State",
]
_FIRMWARE_VIRT_ARGV = [
    "systeminfo.exe",
]


@dataclass(frozen=True)
class CommandOutcome:
    """The bounded, non-shell result of running one preflight command."""

    ok: bool
    detail: str
    stdout: str | None = None


@dataclass(frozen=True)
class PreflightReport:
    """Structured, fail-closed Windows preflight report."""

    status: str
    windows_build: PrerequisiteCheckResult | None = None
    wsl_version: PrerequisiteCheckResult | None = None
    virtual_machine_platform: PrerequisiteCheckResult | None = None
    firmware_virtualization: PrerequisiteCheckResult | None = None
    manifest: PrerequisiteCheckResult | None = None
    detail: str = ""


def _run_argv(argv: Sequence[str]) -> CommandOutcome:
    """Run a bounded argv command with shell=False and a scrubbed env.

    Never raises. Any executable-not-found, nonzero exit, timeout, or
    decode failure is reported as an explicit failed
    :class:`CommandOutcome`; nothing is inferred as success.
    """

    if not isinstance(argv, Sequence) or isinstance(argv, (str, bytes)) or not argv:
        return CommandOutcome(False, "invalid command argv", None)
    for arg in argv:
        if not isinstance(arg, str):
            return CommandOutcome(False, "invalid command argv element", None)

    try:
        completed = subprocess.run(
            list(argv),
            shell=False,
            capture_output=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            env=dict(_MINIMAL_ENV),
        )
    except FileNotFoundError:
        return CommandOutcome(False, f"executable not found: {argv[0]!r}", None)
    except subprocess.TimeoutExpired:
        return CommandOutcome(
            False, f"command timed out after {COMMAND_TIMEOUT_SECONDS}s: {argv[0]!r}", None
        )
    except OSError as exc:
        return CommandOutcome(False, f"failed to spawn {argv[0]!r}: {exc}", None)

    if len(completed.stdout or b"") + len(completed.stderr or b"") > MAX_COMMAND_OUTPUT_BYTES:
        return CommandOutcome(False, f"output exceeded bound for {argv[0]!r}", None)

    if completed.returncode != 0:
        return CommandOutcome(
            False,
            f"{argv[0]!r} exited with code {completed.returncode}",
            None,
        )

    raw = completed.stdout
    if not isinstance(raw, bytes):
        return CommandOutcome(False, f"could not decode output of {argv[0]!r}", None)
    try:
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            stdout = raw.decode("utf-16", errors="strict")
        elif b"\x00" in raw:
            stdout = raw.decode("utf-16-le", errors="strict")
        else:
            stdout = raw.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        return CommandOutcome(False, f"could not decode output of {argv[0]!r}", None)

    return CommandOutcome(True, "command succeeded", stdout)


def collect_windows_build() -> PrerequisiteCheckResult:
    """Collect and parse the native Windows build via ``cmd.exe``."""

    outcome = _run_argv(_CMD_VER_ARGV)
    if not outcome.ok:
        return PrerequisiteCheckResult(False, outcome.detail, None)
    return parse_windows_build(outcome.stdout or "")


def collect_wsl_version() -> PrerequisiteCheckResult:
    """Collect and parse the Store WSL version via ``wsl.exe --version``."""

    outcome = _run_argv(_WSL_VERSION_ARGV)
    if not outcome.ok:
        return PrerequisiteCheckResult(False, outcome.detail, None)
    return parse_wsl_version(outcome.stdout or "")


_KNOWN_FEATURE_STATES = frozenset({"Enabled", "Disabled"})


def _parse_feature_state(output: str, feature_name: str) -> PrerequisiteCheckResult:
    if not isinstance(output, str) or not output.strip():
        return PrerequisiteCheckResult(False, f"empty or missing {feature_name} state", None)
    state = output.strip().splitlines()[-1].strip()
    if state not in _KNOWN_FEATURE_STATES:
        return PrerequisiteCheckResult(
            False, f"unknown {feature_name} state: {state!r}", None
        )
    if state != "Enabled":
        return PrerequisiteCheckResult(False, f"{feature_name} is {state}", state)
    return PrerequisiteCheckResult(True, f"{feature_name} is Enabled", state)


def collect_virtual_machine_platform() -> PrerequisiteCheckResult:
    """Collect the VirtualMachinePlatform optional feature state.

    Uses a read-only, non-elevated query. Never enables the feature.
    """

    outcome = _run_argv(_VMP_FEATURE_ARGV)
    if not outcome.ok:
        return PrerequisiteCheckResult(False, outcome.detail, None)
    return _parse_feature_state(outcome.stdout or "", "VirtualMachinePlatform")


_FIRMWARE_VIRT_LABEL = "Virtualization Enabled In Firmware"


def collect_firmware_virtualization() -> PrerequisiteCheckResult:
    """Collect firmware virtualization state via ``systeminfo.exe``.

    Read-only, non-elevated. Never toggles firmware settings.
    """

    outcome = _run_argv(_FIRMWARE_VIRT_ARGV)
    if not outcome.ok:
        return PrerequisiteCheckResult(False, outcome.detail, None)
    stdout = outcome.stdout or ""
    matching_lines = [
        line.strip() for line in stdout.splitlines() if _FIRMWARE_VIRT_LABEL in line
    ]
    if not matching_lines:
        return PrerequisiteCheckResult(
            False, "could not find firmware virtualization state", None
        )
    line = matching_lines[-1]
    if ":" not in line:
        return PrerequisiteCheckResult(
            False, "could not parse firmware virtualization line", None
        )
    value = line.split(":", 1)[1].strip()
    if value not in ("Yes", "No"):
        return PrerequisiteCheckResult(
            False, f"unknown firmware virtualization value: {value!r}", None
        )
    if value != "Yes":
        return PrerequisiteCheckResult(False, "firmware virtualization is disabled", value)
    return PrerequisiteCheckResult(True, "firmware virtualization is enabled", value)


def collect_manifest_check(
    manifest_path: str | None,
    manifest_loader: Any = None,
    expected_rootfs_sha256: str | None = None,
) -> PrerequisiteCheckResult:
    """Check a caller-supplied pinned manifest path and rootfs hash.

    This never fetches, downloads, or infers a manifest location: the
    caller must supply ``manifest_path`` and a ``manifest_loader``
    callable that reads and parses the raw manifest mapping from that
    path (dependency injection keeps this module read-only and
    testable without real filesystem access).
    """

    if not manifest_path or not isinstance(manifest_path, str):
        return PrerequisiteCheckResult(False, "no manifest path supplied", None)
    if manifest_loader is None:
        return PrerequisiteCheckResult(False, "no manifest loader supplied", None)

    try:
        raw = manifest_loader(manifest_path)
    except FileNotFoundError:
        return PrerequisiteCheckResult(False, f"manifest not found: {manifest_path!r}", None)
    except OSError as exc:
        return PrerequisiteCheckResult(False, f"could not read manifest: {exc}", None)

    if not isinstance(raw, Mapping):
        return PrerequisiteCheckResult(False, "manifest loader did not return a mapping", None)

    try:
        manifest = parse_manifest(raw)
    except ManifestError as exc:
        return PrerequisiteCheckResult(False, f"malformed manifest: {exc}", None)

    if expected_rootfs_sha256 is not None:
        if (not isinstance(expected_rootfs_sha256, str)
                or re.fullmatch(r"[0-9a-fA-F]{64}", expected_rootfs_sha256) is None):
            return PrerequisiteCheckResult(False, "invalid expected rootfs hash supplied", None)
        if manifest.rootfs_sha256 != expected_rootfs_sha256.lower():
            return PrerequisiteCheckResult(
                False,
                "rootfs hash mismatch: manifest does not match expected hash",
                manifest.rootfs_sha256,
            )

    return PrerequisiteCheckResult(
        True, "manifest is present and pinned", manifest.rootfs_sha256
    )


def is_windows(platform: str | None = None) -> bool:
    target = platform if platform is not None else sys.platform
    return target.startswith("win")


def run_preflight(
    *,
    platform: str | None = None,
    manifest_path: str | None = None,
    manifest_loader: Any = None,
    expected_rootfs_sha256: str | None = None,
) -> PreflightReport:
    """Run the read-only Windows preflight collector.

    On non-Windows platforms, returns ``unsupported_platform`` without
    spawning any process. On Windows, runs the bounded, argv-based,
    non-shell checks and aggregates them fail-closed. Never reports
    "delegation enabled" or "delegation verified": the returned status
    is only ever ``unsupported_platform``, ``prerequisites_ready``, or
    ``prerequisites_not_ready``.
    """

    if not is_windows(platform):
        return PreflightReport(
            status=STATUS_UNSUPPORTED_PLATFORM,
            detail="preflight collection only runs on native Windows",
        )

    windows_build = collect_windows_build()
    wsl_version = collect_wsl_version()
    vmp = collect_virtual_machine_platform()
    firmware = collect_firmware_virtualization()
    manifest = collect_manifest_check(
        manifest_path, manifest_loader, expected_rootfs_sha256
    )

    ready = (
        all_prerequisites_met(
            windows_build,
            wsl_version,
            virtual_machine_platform_enabled=vmp.passed is True,
            firmware_virtualization_enabled=firmware.passed is True,
        )
        and manifest.passed is True
    )

    status = STATUS_PREREQUISITES_READY if ready else STATUS_PREREQUISITES_NOT_READY
    detail = (
        "all preflight checks explicitly passed (evidence only; "
        "delegation is neither enabled nor verified by this check)"
        if ready
        else "one or more preflight checks failed or were inconclusive"
    )

    return PreflightReport(
        status=status,
        windows_build=windows_build,
        wsl_version=wsl_version,
        virtual_machine_platform=vmp,
        firmware_virtualization=firmware,
        manifest=manifest,
        detail=detail,
    )
