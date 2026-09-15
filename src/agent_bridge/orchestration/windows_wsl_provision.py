"""Guided, resumable Windows/WSL2 provisioning for delegation setup.

WSL is an implementation detail of Windows delegation, not something a user
should have to know about or install by hand. This module turns "is this
machine ready?" into an ordered ladder of stages, each with a plain-language
title and an exact action, so an installer can carry a non-technical user from
a stock Windows machine to a verified boundary without asking them to paste
commands into a terminal.

The hard parts it exists to handle honestly:

* **The admin boundary is real.** Enabling ``VirtualMachinePlatform`` and the
  Windows Subsystem for Linux, and installing or updating the WSL kernel, are
  machine-wide changes that require elevation. Nothing here elevates silently:
  a stage that needs administrator rights says so, and the caller must obtain
  explicit consent before running it. Everything after the kernel is per-user
  and needs no elevation at all.
* **The reboot boundary is real.** Enabling those features does not take
  effect until the machine restarts. A stage can declare that it reboots, and
  provisioning writes a durable record first so setup resumes at the right
  stage afterwards rather than starting over or, worse, appearing finished.
* **Some things software cannot do.** Firmware virtualization is a BIOS/UEFI
  setting. It is reported as a stage the user must perform, with no pretence
  that the installer can do it.

Fail-closed throughout: :func:`delegation_may_be_enabled` is true only at the
final stage, and that stage is reached only by a real verification run whose
evidence is recorded. Every unknown is a "not ready", never a "probably fine".

This module plans and records. It runs nothing by itself: the caller supplies
a bounded runner, so the planner is pure and fully testable off Windows.
Nothing here has been validated on a live Windows host.
"""

from __future__ import annotations

import json
import ntpath
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import windows_activation as wact
from . import windows_preflight, windows_privacy as wpv, windows_wsl as ww

SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Stages, in the order they must be satisfied
# ---------------------------------------------------------------------------

STAGE_UNSUPPORTED_PLATFORM = "unsupported_platform"
STAGE_WINDOWS_TOO_OLD = "windows_too_old"
STAGE_FIRMWARE_VIRTUALIZATION = "firmware_virtualization"
STAGE_WINDOWS_FEATURES = "windows_features"
STAGE_REBOOT_REQUIRED = "reboot_required"
STAGE_WSL_KERNEL = "wsl_kernel"
STAGE_WSL_UPDATE = "wsl_update"
STAGE_GUEST_IMAGE = "guest_image"
STAGE_BOUNDARY_VERIFICATION = "boundary_verification"
#: A sealed boundary and a working subscription session are different facts,
#: proven by different runs. This rung is where the second one is established,
#: and it is deliberately after the first: a session must not be handed into a
#: guest whose containment has not yet been demonstrated on this machine.
STAGE_PROVIDER_ENROLMENT = "provider_enrolment"
STAGE_READY = "ready"

STAGE_ORDER = (
    STAGE_UNSUPPORTED_PLATFORM,
    STAGE_WINDOWS_TOO_OLD,
    STAGE_FIRMWARE_VIRTUALIZATION,
    STAGE_WINDOWS_FEATURES,
    STAGE_REBOOT_REQUIRED,
    STAGE_WSL_KERNEL,
    STAGE_WSL_UPDATE,
    STAGE_GUEST_IMAGE,
    STAGE_BOUNDARY_VERIFICATION,
    STAGE_PROVIDER_ENROLMENT,
    STAGE_READY,
)

#: Who can carry a stage out.
ACTOR_INSTALLER = "installer"          # automatic, no elevation
ACTOR_INSTALLER_ELEVATED = "installer_elevated"  # automatic, after one consent
ACTOR_USER = "user"                    # only a human can do it

#: The Windows optional features WSL2 needs. Both are machine-wide.
REQUIRED_FEATURES = ("VirtualMachinePlatform", "Microsoft-Windows-Subsystem-Linux")

# ---------------------------------------------------------------------------
# Fixed argv. Built here so the exact command an installer will run is
# reviewable in one place rather than assembled from strings at call time.
# ---------------------------------------------------------------------------


def _system32(name: str) -> str:
    """A trusted absolute path, never a bare name resolved through PATH.

    Provisioning runs elevated. A bare ``dism`` would let anything earlier on
    PATH be run as administrator.
    """

    return ntpath.join("C:\\Windows\\System32", name)


def enable_feature_argv(feature: str) -> list[str]:
    """DISM argv that enables one optional feature without restarting.

    ``/NoRestart`` is deliberate: the reboot is a stage of its own, so that
    the record is written before the machine goes down rather than after.
    """

    if feature not in REQUIRED_FEATURES:
        raise ValueError("feature is not one of the required Windows features")
    return [
        _system32("dism.exe"), "/Online", "/Enable-Feature",
        f"/FeatureName:{feature}", "/All", "/NoRestart",
    ]


def feature_state_argv(feature: str) -> list[str]:
    if feature not in REQUIRED_FEATURES:
        raise ValueError("feature is not one of the required Windows features")
    return [
        _system32("WindowsPowerShell\\v1.0\\powershell.exe"),
        "-NoProfile", "-NonInteractive", "-NoLogo", "-Command",
        f"Get-WindowsOptionalFeature -Online -FeatureName {feature} "
        "| Select-Object -ExpandProperty State",
    ]


def install_wsl_argv() -> list[str]:
    """Install the WSL kernel and nothing else.

    ``--no-distribution`` matters: the default would fetch and register a
    Microsoft Store distro, which is a second, unpinned Linux image this
    runtime would neither own nor be able to vouch for.
    """

    return [_system32("wsl.exe"), "--install", "--no-distribution"]


def update_wsl_argv() -> list[str]:
    return [_system32("wsl.exe"), "--update"]


def wsl_status_argv() -> list[str]:
    return [_system32("wsl.exe"), "--version"]


def restart_argv(delay_seconds: int = 60) -> list[str]:
    """A delayed, cancellable restart, so the user is never surprised."""

    if not isinstance(delay_seconds, int) or isinstance(delay_seconds, bool):
        raise ValueError("delay_seconds must be an int")
    if not 0 < delay_seconds <= 3600:
        raise ValueError("delay_seconds must be between 1 and 3600")
    return [_system32("shutdown.exe"), "/r", "/t", str(delay_seconds),
            "/c", "Agent Bridge setup is restarting Windows to finish enabling WSL."]


def cancel_restart_argv() -> list[str]:
    return [_system32("shutdown.exe"), "/a"]


def elevate_argv(inner: Sequence[str]) -> list[str]:
    """Wrap an argv so Windows shows one UAC prompt for it.

    The caller must have consent before running this. Elevation is not a
    detail to slip past a user: it is the point at which setup stops being
    reversible by that user alone.
    """

    argv = [str(item) for item in inner]
    if not argv:
        raise ValueError("inner argv must not be empty")
    for item in argv:
        if "\x00" in item or "\n" in item or "\r" in item:
            raise ValueError("inner argv must not contain control characters")
    quoted = ",".join("'" + item.replace("'", "''") + "'" for item in argv[1:])
    command = (f"Start-Process -FilePath '{argv[0].replace(chr(39), chr(39) * 2)}' "
               f"-Verb RunAs -Wait")
    if quoted:
        command += f" -ArgumentList {quoted}"
    return [_system32("WindowsPowerShell\\v1.0\\powershell.exe"),
            "-NoProfile", "-NonInteractive", "-NoLogo", "-Command", command]


# ---------------------------------------------------------------------------
# Observed state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProvisionState:
    """What is actually known about this machine. Every field fails closed.

    ``None`` means "not determined", which is treated exactly like False.
    Nothing here is inferred from something else being true.
    """

    is_windows: bool = False
    windows_build_ok: bool | None = None
    firmware_virtualization: bool | None = None
    features_enabled: Mapping[str, bool | None] = field(default_factory=dict)
    reboot_pending: bool = False
    wsl_present: bool | None = None
    wsl_version_ok: bool | None = None
    guest_image_installed: bool | None = None
    boundary_verified: bool | None = None
    #: Set only from a recorded provider-lane observation that was read back
    #: off disk after it was written. Never from the run that produced it.
    provider_lane_verified: bool | None = None

    def features_all_enabled(self) -> bool:
        return all(self.features_enabled.get(name) is True
                   for name in REQUIRED_FEATURES)


@dataclass(frozen=True)
class Stage:
    """One rung of the ladder, described for a human and for a machine."""

    stage: str
    title: str
    detail: str
    actor: str
    requires_admin: bool
    reboots: bool
    argv: tuple[tuple[str, ...], ...] = ()
    reversible: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "title": self.title,
            "detail": self.detail,
            "actor": self.actor,
            "requires_admin": self.requires_admin,
            "reboots": self.reboots,
            "commands": [list(argv) for argv in self.argv],
            "reversible": self.reversible,
        }


def current_stage(state: ProvisionState) -> str:
    """The first unsatisfied rung. Order is the whole point: a later check is
    meaningless until the earlier ones hold."""

    if not state.is_windows:
        return STAGE_UNSUPPORTED_PLATFORM
    if state.windows_build_ok is not True:
        return STAGE_WINDOWS_TOO_OLD
    if state.firmware_virtualization is not True:
        return STAGE_FIRMWARE_VIRTUALIZATION
    if not state.features_all_enabled():
        return STAGE_WINDOWS_FEATURES
    if state.reboot_pending:
        return STAGE_REBOOT_REQUIRED
    if state.wsl_present is not True:
        return STAGE_WSL_KERNEL
    if state.wsl_version_ok is not True:
        return STAGE_WSL_UPDATE
    if state.guest_image_installed is not True:
        return STAGE_GUEST_IMAGE
    if state.boundary_verified is not True:
        return STAGE_BOUNDARY_VERIFICATION
    if state.provider_lane_verified is not True:
        return STAGE_PROVIDER_ENROLMENT
    return STAGE_READY


def delegation_may_be_enabled(state: ProvisionState) -> bool:
    """The single gate. Delegation is off until every rung is proven."""

    return current_stage(state) == STAGE_READY


def next_stage(state: ProvisionState) -> Stage:
    """The one thing to do next, in language a non-technical user can act on."""

    stage = current_stage(state)

    if stage == STAGE_UNSUPPORTED_PLATFORM:
        return Stage(
            stage=stage,
            title="This feature needs Windows",
            detail=("Delegation to a sandboxed Linux environment is a Windows "
                    "feature. Nothing will be installed or changed on this "
                    "computer."),
            actor=ACTOR_USER, requires_admin=False, reboots=False)

    if stage == STAGE_WINDOWS_TOO_OLD:
        return Stage(
            stage=stage,
            title="Update Windows first",
            detail=(f"Windows 11 build {ww.WINDOWS_MIN_BUILD} or newer is "
                    "required. Install pending Windows updates and run setup "
                    "again."),
            actor=ACTOR_USER, requires_admin=True, reboots=True)

    if stage == STAGE_FIRMWARE_VIRTUALIZATION:
        return Stage(
            stage=stage,
            title="Make virtualization available to Windows",
            detail=("Windows cannot use the processor features WSL2 needs. "
                    "On a physical PC, enable virtualization in BIOS/UEFI "
                    "(often SVM, VT-x, or Intel Virtualization Technology). "
                    "Inside a virtual machine, the host must support and "
                    "expose nested virtualization; some hosts, including "
                    "Parallels on Apple silicon, cannot currently do that. "
                    "This installer cannot change either limitation."),
            actor=ACTOR_USER, requires_admin=False, reboots=True)

    if stage == STAGE_WINDOWS_FEATURES:
        missing = [name for name in REQUIRED_FEATURES
                   if state.features_enabled.get(name) is not True]
        return Stage(
            stage=stage,
            title="Turn on the Windows features WSL needs",
            detail=("Setup will ask for administrator permission once, turn on "
                    f"{len(missing)} Windows feature(s), and then restart. "
                    "These are standard Windows components; they can be turned "
                    "off again later from Windows Features."),
            actor=ACTOR_INSTALLER_ELEVATED, requires_admin=True, reboots=False,
            argv=tuple(tuple(enable_feature_argv(name)) for name in missing))

    if stage == STAGE_REBOOT_REQUIRED:
        return Stage(
            stage=stage,
            title="Restart to finish turning the features on",
            detail=("Windows needs to restart before the new features work. "
                    "Setup will continue automatically after you sign back in."),
            actor=ACTOR_INSTALLER, requires_admin=True, reboots=True,
            argv=(tuple(restart_argv()),))

    if stage == STAGE_WSL_KERNEL:
        return Stage(
            stage=stage,
            title="Install the Linux kernel component",
            detail=("Setup will ask for administrator permission once and "
                    "install the Windows Subsystem for Linux kernel. No Linux "
                    "distribution is installed: Agent Bridge brings its own "
                    "pinned image."),
            actor=ACTOR_INSTALLER_ELEVATED, requires_admin=True, reboots=False,
            argv=(tuple(install_wsl_argv()),))

    if stage == STAGE_WSL_UPDATE:
        return Stage(
            stage=stage,
            title="Update the Linux kernel component",
            detail=("The installed Windows Subsystem for Linux is older than "
                    f"{'.'.join(str(part) for part in ww.WSL_MIN_VERSION)}. "
                    "Setup will ask for administrator permission once and "
                    "update it."),
            actor=ACTOR_INSTALLER_ELEVATED, requires_admin=True, reboots=False,
            argv=(tuple(update_wsl_argv()),))

    if stage == STAGE_GUEST_IMAGE:
        return Stage(
            stage=stage,
            title="Install the Agent Bridge sandbox image",
            detail=("Setup will place the pinned sandbox image in your own "
                    "user folder and check it against its published checksum. "
                    "This needs no administrator permission and can be undone "
                    "by deleting that folder."),
            actor=ACTOR_INSTALLER, requires_admin=False, reboots=False)

    if stage == STAGE_BOUNDARY_VERIFICATION:
        return Stage(
            stage=stage,
            title="Check the sandbox really is sealed",
            detail=("Setup will start a throwaway sandbox and prove it cannot "
                    "see your Windows drives, cannot run Windows programs, and "
                    "contains exactly the pinned software, then destroy it. "
                    "Delegation stays switched off unless every check passes."),
            actor=ACTOR_INSTALLER, requires_admin=False, reboots=False)

    if stage == STAGE_PROVIDER_ENROLMENT:
        return Stage(
            stage=stage,
            title="Prove your provider sign-in works inside the sandbox",
            detail=("Setup will run one very small request through your own "
                    "provider subscription from inside the sandbox, and check "
                    "the answer came back. It also checks that an expired "
                    "sign-in is refused rather than quietly ignored. Nothing "
                    "you type is sent, and the answer is not recorded. Until "
                    "both checks pass, jobs that need a provider stay off."),
            actor=ACTOR_INSTALLER, requires_admin=False, reboots=False)

    return Stage(
        stage=STAGE_READY,
        title="Ready",
        detail=("Every prerequisite is installed and the sandbox boundary has "
                "been proven on this machine."),
        actor=ACTOR_INSTALLER, requires_admin=False, reboots=False)


def remaining_stages(state: ProvisionState) -> list[str]:
    """Everything still ahead, so an installer can show real progress."""

    stage = current_stage(state)
    index = STAGE_ORDER.index(stage)
    return [name for name in STAGE_ORDER[index:] if name != STAGE_READY]


def plan(state: ProvisionState) -> dict[str, Any]:
    """A complete, serialisable description of where setup stands."""

    stage = current_stage(state)
    step = next_stage(state)
    remaining = remaining_stages(state)
    return {
        "schema_version": SCHEMA_VERSION,
        "stage": stage,
        "ready": stage == STAGE_READY,
        "delegation_may_be_enabled": delegation_may_be_enabled(state),
        "next": step.as_dict(),
        "remaining_stages": remaining,
        "requires_admin_ahead": any(
            next_stage(_state_at(state, name)).requires_admin for name in remaining),
        "requires_reboot_ahead": any(
            next_stage(_state_at(state, name)).reboots for name in remaining),
        "blocked_on_user": step.actor == ACTOR_USER,
    }


def _state_at(state: ProvisionState, stage: str) -> ProvisionState:
    """A hypothetical state whose current stage is ``stage``.

    Used only to ask "does anything ahead need admin or a reboot", so an
    installer can warn up front instead of surprising the user three screens
    in. It never becomes observed state.
    """

    index = STAGE_ORDER.index(stage)

    def reached(name: str) -> bool:
        return STAGE_ORDER.index(name) < index

    return ProvisionState(
        is_windows=True,
        windows_build_ok=True if reached(STAGE_WINDOWS_TOO_OLD) else None,
        firmware_virtualization=True if reached(STAGE_FIRMWARE_VIRTUALIZATION) else None,
        features_enabled={name: True for name in REQUIRED_FEATURES}
        if reached(STAGE_WINDOWS_FEATURES) else {},
        reboot_pending=not reached(STAGE_REBOOT_REQUIRED),
        wsl_present=True if reached(STAGE_WSL_KERNEL) else None,
        wsl_version_ok=True if reached(STAGE_WSL_UPDATE) else None,
        guest_image_installed=True if reached(STAGE_GUEST_IMAGE) else None,
        boundary_verified=True if reached(STAGE_BOUNDARY_VERIFICATION) else None,
        provider_lane_verified=(
            True if reached(STAGE_PROVIDER_ENROLMENT) else None),
    )


# ---------------------------------------------------------------------------
# Durable resume record
# ---------------------------------------------------------------------------
#
# Written before anything that restarts the machine. Without it, setup after a
# reboot cannot tell "the features were enabled and we are mid-install" from
# "nothing has happened yet", and the difference decides whether the user is
# asked to elevate a second time for work already done.

RESUME_RECORD_NAME = "provision-resume.json"

RESUME_KEYS = frozenset({"schema_version", "stage", "stage_completed",
                         "awaiting_reboot", "updated_at", "attempts"})


def resume_record_path(runtime_root: str) -> str:
    return ntpath.join(runtime_root, RESUME_RECORD_NAME)


class ResumeError(RuntimeError):
    """The resume record is missing, malformed, or not this owner's."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def write_resume_record(path: str, record: Mapping[str, Any], *,
                        secure: Callable[[int], None] | None = None,
                        platform: Any = None,
                        expect_identity: Any = None) -> None:
    """Write the record owner-only and atomically, or do not write it.

    The record decides which elevated stage runs next at the following logon.
    A record any local account can edit is a way to make setup elevate for a
    stage the machine does not need, and worse, a way to make it elevate
    repeatedly. So protection is mandatory and the previous record is never
    truncated:

    * ``secure`` defaults to the platform's owner-only writer rather than to
      ``None``. It used to default to ``None``, which meant the common call
      wrote an unprotected file and the protection was opt-in; that is exactly
      backwards for a file that steers an elevated action.
    * The old record is not opened with ``O_TRUNC`` before the new content is
      known to be writable. A crash between the truncate and the write used to
      leave an empty file, which parses as nothing and restarts setup from the
      beginning, asking for elevation again for work already done.
    * ``expect_identity`` carries the identity a caller read, so an increment
      computed from a stale record cannot overwrite a newer one.
    """

    target = Path(path)
    writer = secure if secure is not None else wpv.platform_secure_writer(platform)
    payload = json.dumps(dict(record), indent=2, sort_keys=True) + "\n"
    try:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        wpv.require_private_directory(target.parent, root=target.parent,
                                      platform=platform)
        wpv.atomic_private_write(target, payload.encode("utf-8"),
                                 secure=writer, root=target.parent,
                                 expect_identity=expect_identity)
    except wpv.PrivacyError as exc:
        raise ResumeError(f"resume_{exc.reason}") from None
    except OSError:
        raise ResumeError("resume_unwritable") from None


def read_resume_record(path: str, *, platform: Any = None
                       ) -> tuple[dict[str, Any], Any] | None:
    """The record and the identity it was read from, or None if there is none.

    Read through the same owner-only, descriptor-bound path as every other
    control file here. A record that exists but cannot be proven private is an
    error rather than an absence: "there is no record" and "there is a record
    somebody else could have written" must not produce the same behaviour.
    """

    target = Path(path)
    try:
        payload, identity = wpv.read_private_file(target, root=target.parent,
                                                  platform=platform)
    except wpv.PrivacyError as exc:
        if exc.reason == "file_absent":
            return None
        raise ResumeError(f"resume_{exc.reason}") from None
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ResumeError("resume_malformed") from None
    try:
        return parse_resume_record(raw), identity
    except ValueError:
        raise ResumeError("resume_malformed") from None


def next_attempt(record: Mapping[str, Any], stage: str) -> int:
    """The attempt number this run of ``stage`` is.

    Counting resets when the stage changes. Without the reset a machine that
    needed three tries early on would arrive at a later stage with its budget
    already spent, and stop for help it does not need.
    """

    if record.get("stage") != stage:
        return 1
    try:
        return int(record["attempts"]) + 1
    except (KeyError, TypeError, ValueError):
        return 1


def clear_resume_record(path: str) -> None:
    """Remove the record. Missing is success: the state asked for is "gone"."""

    try:
        Path(path).unlink()
    except FileNotFoundError:
        return
    except OSError:
        raise ResumeError("resume_not_removable") from None


def build_resume_record(*, stage: str, stage_completed: str | None,
                        awaiting_reboot: bool, updated_at: str,
                        attempts: int = 0) -> dict[str, Any]:
    if stage not in STAGE_ORDER:
        raise ValueError("stage must be a known provisioning stage")
    if stage_completed is not None and stage_completed not in STAGE_ORDER:
        raise ValueError("stage_completed must be a known provisioning stage")
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
        raise ValueError("attempts must be a non-negative int")
    return {
        "schema_version": SCHEMA_VERSION,
        "stage": stage,
        "stage_completed": stage_completed,
        "awaiting_reboot": bool(awaiting_reboot),
        "updated_at": updated_at,
        "attempts": attempts,
    }


def parse_resume_record(raw: Any) -> dict[str, Any]:
    """Strict parse. A malformed record restarts planning from observation.

    It deliberately cannot say "already verified": the record tracks where
    setup got to, and the boundary proof is re-observed, never remembered.
    """

    if not isinstance(raw, Mapping) or set(raw) != RESUME_KEYS:
        raise ValueError("resume record shape is invalid")
    if raw["schema_version"] != SCHEMA_VERSION:
        raise ValueError("resume record schema is unsupported")
    if raw["stage"] not in STAGE_ORDER:
        raise ValueError("resume record stage is unknown")
    completed = raw["stage_completed"]
    if completed is not None and completed not in STAGE_ORDER:
        raise ValueError("resume record stage_completed is unknown")
    if not isinstance(raw["awaiting_reboot"], bool):
        raise ValueError("resume record awaiting_reboot is malformed")
    if not isinstance(raw["updated_at"], str) or not raw["updated_at"]:
        raise ValueError("resume record updated_at is malformed")
    attempts = raw["attempts"]
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
        raise ValueError("resume record attempts is malformed")
    return dict(raw)


#: How many times one stage may be retried before setup stops and asks for
#: help. Without this, a stage that fails the same way every boot turns into
#: a reboot loop the user cannot escape.
MAX_STAGE_ATTEMPTS = 3


def should_stop_retrying(record: Mapping[str, Any], stage: str) -> bool:
    return record.get("stage") == stage and int(record.get("attempts", 0)) >= MAX_STAGE_ATTEMPTS


# ---------------------------------------------------------------------------
# Resume-after-reboot registration (per user, no elevation, reversible)
# ---------------------------------------------------------------------------

RESUME_TASK_NAME = "AgentBridgeSetupResume"


def register_resume_argv(command: Sequence[str]) -> list[str]:
    """Register a per-user logon task that continues setup after the reboot.

    Deliberately ``schtasks`` in the current user's own task namespace rather
    than a Run key or a machine-wide task: it needs no elevation, it is
    visible to the user in Task Scheduler, and :func:`unregister_resume_argv`
    removes it completely.
    """

    argv = [str(item) for item in command]
    if not argv:
        raise ValueError("resume command must not be empty")
    # One contract for both scheduled tasks. The resume task runs elevated
    # work at the next logon, so it deserves the stricter of the two checks,
    # not a second, laxer copy: absolute local executable (no UNC, no device
    # path, no traversal), no quotes, no control characters, no '%' for the
    # task engine to expand, and no length that would be truncated into a
    # different command.
    try:
        joined = wact.build_action(argv)
    except wact.ActivationError as exc:
        raise ValueError(f"resume command is unsafe to register: {exc}") from exc
    return [_system32("schtasks.exe"), "/Create", "/TN", RESUME_TASK_NAME,
            "/SC", "ONLOGON", "/TR", joined, "/F"]


def unregister_resume_argv() -> list[str]:
    return [_system32("schtasks.exe"), "/Delete", "/TN", RESUME_TASK_NAME, "/F"]


def resume_status_argv() -> list[str]:
    return wact.status_argv(RESUME_TASK_NAME)


def resume_ownership_blocker(status: wact.ActivationStatus,
                             command: Sequence[str]) -> str:
    """"" if the resume task may be created or deleted, else why not.

    Same rule as the worker task: ``/F`` replaces and deletes whatever holds
    the name, so a collision must be proven to be ours first.
    """

    if not status.registered:
        return "" if status.state == "absent" else "task_status_unavailable"
    if not status.action:
        return "task_action_unreadable"
    try:
        expected = wact.build_action([str(item) for item in command])
    except wact.ActivationError:
        return "task_action_invalid"
    # A resume action normally starts with the user's Python interpreter.
    # Matching only that executable would treat every unrelated Python task
    # with our fixed task name as ours.  Resume ownership is therefore the
    # complete action, including launcher, subcommand and runtime root.
    if status.action.strip() != expected:
        return "task_action_mismatch"
    return ""


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProvisionEvidence:
    """What each observation actually saw, for an evidence record.

    Kept separate from :class:`ProvisionState` so the booleans that drive the
    gate stay small and auditable while the operator-facing detail can be as
    verbose as it needs to be.
    """

    checks: tuple[tuple[str, bool | None, str], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"checks": [{"name": name, "passed": passed, "detail": detail}
                           for name, passed, detail in self.checks]}


def observe(
    *,
    platform_name: str | None = None,
    preflight: Any = None,
    feature_states: Mapping[str, str | None] | None = None,
    reboot_pending: bool = False,
    guest_image_installed: bool | None = None,
    boundary_verified: bool | None = None,
    provider_lane_verified: bool | None = None,
) -> tuple[ProvisionState, ProvisionEvidence]:
    """Turn preflight evidence into a state plus its supporting detail.

    ``preflight`` is a :class:`~.windows_preflight.PreflightReport`, which the
    caller collects. This function adds no new subprocess calls; it exists so
    that the mapping from evidence to the delegation gate is one small,
    testable place rather than scattered conditionals in the installer.
    """

    if not windows_preflight.is_windows(platform_name):
        return (ProvisionState(is_windows=False),
                ProvisionEvidence((("platform", False, "not native Windows"),)))

    checks: list[tuple[str, bool | None, str]] = [
        ("platform", True, "native Windows")]

    def record(name: str, result: Any) -> bool | None:
        if result is None:
            checks.append((name, None, "not collected"))
            return None
        passed = result.passed is True
        checks.append((name, passed, result.detail))
        return passed

    build_ok = record("windows_build", getattr(preflight, "windows_build", None))
    firmware = record("firmware_virtualization",
                      getattr(preflight, "firmware_virtualization", None))
    wsl = getattr(preflight, "wsl_version", None)
    wsl_ok = record("wsl_version", wsl)

    features: dict[str, bool | None] = {}
    for name in REQUIRED_FEATURES:
        raw = (feature_states or {}).get(name)
        enabled = True if raw == "Enabled" else (None if raw is None else False)
        features[name] = enabled
        checks.append((f"feature:{name}", enabled, raw or "not collected"))

    # "WSL is present" and "WSL is new enough" are different questions. A
    # missing wsl.exe and an outdated one need different stages, and a single
    # boolean would send a user with no WSL to the update stage.
    wsl_present = None if wsl is None else (wsl.value is not None or wsl.passed is True)

    checks.append(("reboot_pending", not reboot_pending,
                   "restart pending" if reboot_pending else "no restart pending"))
    checks.append(("guest_image", guest_image_installed,
                   "installed" if guest_image_installed else "not installed"))
    checks.append(("boundary_verified", boundary_verified,
                   "verified on this machine" if boundary_verified
                   else "not verified on this machine"))
    checks.append(("provider_lane", provider_lane_verified,
                   "a provider session was observed working in the sandbox"
                   if provider_lane_verified
                   else "no provider session has been observed working here"))

    return (
        ProvisionState(
            is_windows=True,
            windows_build_ok=build_ok,
            firmware_virtualization=firmware,
            features_enabled=features,
            reboot_pending=bool(reboot_pending),
            wsl_present=wsl_present,
            wsl_version_ok=wsl_ok,
            guest_image_installed=guest_image_installed,
            boundary_verified=boundary_verified,
            provider_lane_verified=provider_lane_verified,
        ),
        ProvisionEvidence(tuple(checks)),
    )


# ---------------------------------------------------------------------------
# Consent
# ---------------------------------------------------------------------------


class ConsentRequired(RuntimeError):
    """A stage needs a decision the installer may not make on the user's behalf."""


def require_consent(step: Stage, *, admin_consent: bool, reboot_consent: bool) -> None:
    """Refuse to act without the specific consent the stage needs.

    Two separate consents, because they are two separate surprises: one grants
    machine-wide changes, the other ends the user's session. Bundling them
    would let a click that meant "yes, install it" also mean "yes, restart my
    computer now".
    """

    if step.requires_admin and not admin_consent:
        raise ConsentRequired("administrator_consent_required")
    if step.reboots and not reboot_consent:
        raise ConsentRequired("restart_consent_required")


def advance(
    step: Stage,
    *,
    run: Callable[[Sequence[str]], Any],
    admin_consent: bool = False,
    reboot_consent: bool = False,
    elevate: bool = True,
) -> dict[str, Any]:
    """Carry out one stage's commands, in order, stopping at the first failure.

    ``run`` is the caller's bounded runner. Nothing is retried here: a stage
    that failed is reported so the installer can decide, because retrying an
    elevated machine-wide change without telling anyone is precisely the
    behaviour this module exists to avoid.
    """

    require_consent(step, admin_consent=admin_consent, reboot_consent=reboot_consent)
    if step.actor == ACTOR_USER:
        return {"stage": step.stage, "status": "blocked_on_user", "commands_run": 0}
    if not step.argv:
        return {"stage": step.stage, "status": "nothing_to_run", "commands_run": 0}

    run_count = 0
    for argv in step.argv:
        command = list(argv)
        if step.actor == ACTOR_INSTALLER_ELEVATED and elevate:
            command = elevate_argv(command)
        result = run(command)
        run_count += 1
        code = getattr(result, "returncode", None)
        if code != 0:
            return {"stage": step.stage, "status": "failed",
                    "commands_run": run_count,
                    "reason": "command_failed" if code is not None else "no_exit_code"}
    return {"stage": step.stage, "status": "ok", "commands_run": run_count}


# ---------------------------------------------------------------------------
# Limitations, stated rather than papered over
# ---------------------------------------------------------------------------

ADMIN_BOUNDARY_LIMITATION = (
    "enabling VirtualMachinePlatform and the Windows Subsystem for Linux, and "
    "installing or updating the WSL kernel, are machine-wide changes that "
    "require administrator rights; the installer can request one elevation "
    "prompt per stage with explicit consent but cannot avoid the requirement"
)

FIRMWARE_LIMITATION = (
    "firmware virtualization is a BIOS/UEFI setting that no program can change "
    "from within Windows; when it is off the user must change it themselves"
)

REBOOT_LIMITATION = (
    "the Windows features do not take effect until a restart, so setup writes "
    "a durable resume record and registers a per-user logon task before "
    "restarting; if the user cancels the restart, setup resumes at the same "
    "stage rather than reporting progress it did not make"
)
