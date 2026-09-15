"""``agent-bridge-windows-setup``: the command a person actually runs.

:mod:`~.orchestration.windows_provision_driver` knows how to walk a stock
Windows machine to an activated worker. Until this module existed, nothing
called it. That is not a small gap: a driver with no entrypoint is a library
that describes an installation rather than one that performs it, and the
reboot machinery in particular could never have run, because the resume task
it registers has to point at *a command*, and there was no command to point
at.

So this file is deliberately thin. It owns three things and delegates the
rest:

* **Where the installation lives.** The runtime root, the pinned rootfs and
  its manifest, resolved from flags or from the per-user default under
  ``%LOCALAPPDATA%``.
* **What the OS says right now.** Preflight, the two optional-feature states,
  and whether a restart is pending. All read-only, all bounded, none of them
  inferred from a previous run of this process.
* **The argv the resume task will run.** The same interpreter and a protected,
  self-contained copy of this package under the runtime root, with ``resume``.
  It has to be an absolute local path
  (``windows_activation.validate_executable``), and must survive the source
  checkout being moved or updated after the restart is scheduled.

Subcommands
-----------

``plan``
    Read-only. What is done, what is next, what is still ahead.
``step``
    Do the single next thing, with consent flags for the things that need
    them. One step per invocation, because each one can require a decision.
``resume``
    What the logon task runs after a restart. Parses the record the reboot
    left, continues while stages need no new decision, and retires the record
    and the task once setup has reached a terminal state.
``status``
    Read-only. The resume record and the logon task, as they are.

Exit codes are the step vocabulary, so a wrapper can branch without parsing:
0 ok, 1 failed, 2 blocked, 3 a consent is required, 4 the arguments were
wrong. Every subcommand prints one JSON object on stdout.

Nothing here has been run on a live Windows host. On any other platform the
driver's own gates refuse, which is the intended behaviour and not a mock.
"""

from __future__ import annotations

import argparse
import io
import json
import ntpath
import os
from pathlib import Path
import platform as platform_module
import subprocess
import sys
import zipfile
from typing import Any, Callable, Sequence

from . import store

from .orchestration import windows_activation as wact
from .orchestration import windows_delegation as wd
from .orchestration import windows_preflight as wpf
from .orchestration import windows_provision_driver as dr
from .orchestration import windows_validation as wv
from .orchestration import windows_wsl_provision as wp

#: The module path the resume task re-enters. Written once here so the task
#: argv and the ``python -m`` invocation can never drift apart.
MODULE_PATH = "agent_bridge.windows_setup"

#: Exit codes, in the step vocabulary. ``EXIT_USAGE`` is the only one that is
#: not a driver outcome.
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_BLOCKED = 2
EXIT_CONSENT_REQUIRED = 3
EXIT_USAGE = 4

_EXIT_FOR_STATUS = {
    dr.STEP_OK: EXIT_OK,
    dr.STEP_REBOOT_SCHEDULED: EXIT_OK,
    dr.STEP_FAILED: EXIT_FAILED,
    dr.STEP_BLOCKED: EXIT_BLOCKED,
    dr.STEP_CONSENT_REQUIRED: EXIT_CONSENT_REQUIRED,
}

#: How many stages one ``resume`` invocation may take before stopping. A
#: resume that looped until something changed would be indistinguishable from
#: a resume that hung, and each stage here is minutes of elevated work.
MAX_RESUME_STEPS = 8

#: Nothing this file runs is interactive or slow by design. A command that
#: exceeds this is wedged, and a wedged command at logon is worse than a
#: failed one.
COMMAND_TIMEOUT_SECONDS = 600.0

#: The registry locations Windows itself uses to record a pending restart.
#: Queried, never written.
_REBOOT_PENDING_KEYS = (
    r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing"
    r"\RebootPending",
    r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update"
    r"\RebootRequired",
)


class SetupError(Exception):
    """A usage or environment problem, reported rather than raised onward."""


# ---------------------------------------------------------------------------
# Where the installation lives
# ---------------------------------------------------------------------------


def default_runtime_root(environ: Any = None) -> str:
    """The per-user installation directory.

    Per-user and not machine-wide on purpose: everything below it is a
    credential-adjacent artefact of one person's sessions, and the ACLs the
    privacy layer applies are per-user ACLs.
    """

    env = os.environ if environ is None else environ
    base = env.get("LOCALAPPDATA") or env.get("XDG_STATE_HOME")
    if not base:
        home = env.get("USERPROFILE") or env.get("HOME") or ""
        base = str(Path(home) / "AppData" / "Local") if home else ""
    if not base:
        raise SetupError("no per-user application directory in the environment")
    return str(Path(base) / "agent-bridge" / "windows")


def build_config(*, runtime_root: str, rootfs_path: str | None,
                 manifest_path: str | None,
                 sidecar_path: str | None) -> wd.DelegationConfig:
    root = Path(runtime_root)
    return wd.DelegationConfig(
        runtime_root=str(root),
        rootfs_path=rootfs_path or str(root / "image" / "rootfs.tar"),
        manifest_path=manifest_path or str(root / "image" / "manifest.json"),
        sidecar_path=sidecar_path or None)


# ---------------------------------------------------------------------------
# What the OS says right now
# ---------------------------------------------------------------------------


def run_command(argv: Sequence[str]) -> Any:
    """Bounded, non-shell, argv-only. The one process spawn in this file.

    ``stdout`` is captured because ownership proof reads it; ``stderr`` is
    not, because the only thing done with a failure here is to report the
    exit status, and command error text is exactly the kind of content that
    ends up quoted into a durable record.
    """

    try:
        return subprocess.run(list(argv), stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=COMMAND_TIMEOUT_SECONDS,
                              check=False)
    except (OSError, subprocess.SubprocessError):
        return subprocess.CompletedProcess(list(argv), 1, b"", b"")


def collect_feature_states(*, run: Callable[[Sequence[str]], Any]
                           ) -> dict[str, str | None]:
    """The two optional features, as Windows reports them.

    ``None`` for a feature whose state could not be read. The ladder treats
    that as "not collected" rather than "disabled", so an unreadable state
    blocks instead of triggering an elevated enable of something that may
    already be on.
    """

    states: dict[str, str | None] = {}
    for feature in wp.REQUIRED_FEATURES:
        result = run(wp.feature_state_argv(feature))
        if getattr(result, "returncode", 1) != 0:
            states[feature] = None
            continue
        raw = getattr(result, "stdout", b"") or b""
        try:
            text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        except (UnicodeDecodeError, ValueError):
            states[feature] = None
            continue
        value = text.strip().splitlines()[0].strip() if text.strip() else ""
        states[feature] = value or None
    return states


def reboot_pending(*, run: Callable[[Sequence[str]], Any]) -> bool:
    """Whether Windows is holding a restart, by its own registry flags.

    Fail-closed means *pending* here, not clear. Reporting "no restart
    needed" when the check could not run would let setup enable the virtual
    machine platform and then try to start a sandbox that cannot work until
    the machine comes back. The cost of the other direction is one avoidable
    restart prompt, which is bounded by the stage attempt budget.
    """

    powershell = ntpath.join("C:\\Windows\\System32", "WindowsPowerShell",
                            "v1.0", "powershell.exe")
    for key in _REBOOT_PENDING_KEYS:
        script = ("try { if (Test-Path -LiteralPath 'Registry::" + key +
                  "' -ErrorAction Stop) { exit 0 }; exit 2 } catch { exit 1 }")
        result = run([powershell, "-NoLogo", "-NoProfile", "-NonInteractive",
                      "-Command", script])
        code = getattr(result, "returncode", None)
        if code == 0:
            return True
        if code != 2:
            return True
    return False


# ---------------------------------------------------------------------------
# The argv the resume task will run
# ---------------------------------------------------------------------------


def launcher_script() -> str | None:
    """The repository's portable entry script, for direct interactive use.

    Scheduled resume does not use this path; it uses the self-contained copy
    installed by :func:`install_resume_launcher`.  Keeping this helper allows
    the ordinary checkout command and its tests to share one discovery rule.
    """

    script = Path(__file__).resolve().parents[2] / "setup_bridge.py"
    return str(script) if script.is_file() else None


def stable_resume_launcher(runtime_root: str) -> str:
    return str(Path(runtime_root) / "bootstrap" / "agent-bridge-resume.pyz")


def install_resume_launcher(runtime_root: str) -> str:
    """Install a self-contained, owner-only launcher that survives checkout moves."""

    source = Path(__file__).resolve().parent
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("__main__.py", (
            "from agent_bridge.windows_setup import main\n"
            "raise SystemExit(main())\n"))
        for path in sorted(source.rglob("*.py")):
            if path.is_symlink() or not path.is_file():
                continue
            archive.writestr(str(Path("agent_bridge") / path.relative_to(source)),
                             path.read_bytes())
    target = stable_resume_launcher(runtime_root)
    store.atomic_write_bytes(target, output.getvalue())
    # This function can run after UAC elevation.  On Windows, a directory
    # created by an elevated administrator token may otherwise be owned by the
    # Administrators group and unreadable by the same person's ordinary logon
    # token.  The scheduled task deliberately runs without elevation, so prove
    # the final directory and file ACL in the identity that will own the task
    # before promising a resumable reboot.
    verified, _evidence = store.platform.verify_owner_only_path(
        str(Path(target).parent), target)
    if not verified:
        raise PermissionError("resume launcher is not readable owner-only state")
    return target


def resume_command(runtime_root: str, *, executable: str | None = None,
                   script: str | None = None, extra: Sequence[str] = ()) -> list[str]:
    """The exact command the logon task runs, validated before it is promised.

    Validated here and not only inside ``schedule_resume`` because the useful
    moment to discover that this interpreter cannot be named in a scheduled
    task is before a reboot is offered, not after one is taken.
    """

    launcher = script if script is not None else launcher_script()
    if launcher and str(launcher).lower().endswith(".pyz"):
        # The installed zipapp enters windows_setup.main directly.  The
        # checkout's setup_bridge.py is a multiplexer and needs the extra
        # ``windows-setup`` selector; passing that selector to the zipapp
        # makes it an invalid subcommand and prevents every reboot resume.
        head = [executable or sys.executable, launcher]
    elif launcher:
        head = [executable or sys.executable, launcher, "windows-setup"]
    else:
        head = [executable or sys.executable, "-m", MODULE_PATH]
    argv = [*head, "resume", "--runtime-root", runtime_root, *extra]
    wact.build_action(argv)
    return argv


def build_context(args: argparse.Namespace, *,
                  run: Callable[[Sequence[str]], Any] | None = None
                  ) -> dr.DriverContext:
    """One observation of the machine, assembled into what the driver takes."""

    runner = run if run is not None else run_command
    runtime_root = args.runtime_root or default_runtime_root()
    config = build_config(runtime_root=runtime_root, rootfs_path=args.rootfs,
                          manifest_path=args.manifest, sidecar_path=args.sidecar)
    on_windows = wpf.is_windows()
    preflight = wpf.run_preflight(manifest_path=config.manifest_path) \
        if on_windows else None
    features = collect_feature_states(run=runner) if on_windows else {}
    pending = reboot_pending(run=runner) if on_windows else False
    try:
        command = resume_command(runtime_root, script=stable_resume_launcher(runtime_root))
    except wact.ActivationError:
        # Reported as an absent command rather than raised: the driver refuses
        # a rebooting stage without one, which is the outcome either way, and
        # every other stage remains available.
        command = None
    return dr.DriverContext(
        config=config, run=runner, preflight=preflight, feature_states=features,
        reboot_pending=pending, platform_name=sys.platform,
        machine=platform_module.machine(), os_name=os.name,
        wsl_version=_wsl_version(preflight), image_source_path=args.image,
        resume_command=command)


def _wsl_version(preflight: Any) -> str:
    check = getattr(preflight, "wsl_version", None)
    return str(getattr(check, "value", "") or "")


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def command_plan(context: dr.DriverContext, args: argparse.Namespace
                 ) -> tuple[dict[str, Any], int]:
    return dict(dr.plan(context), command="plan"), EXIT_OK


def command_step(context: dr.DriverContext, args: argparse.Namespace
                 ) -> tuple[dict[str, Any], int]:
    # Install the durable launcher only once the user has consented to the
    # rebooting step.  Read-only planning and refused consent remain read-only.
    if (args.admin_consent and args.reboot_consent and context.resume_command
            and len(context.resume_command) > 1
            and str(context.resume_command[1]).endswith(".pyz")):
        try:
            install_resume_launcher(context.config.runtime_root)
        except OSError:
            result = {"step": "resume_schedule", "status": dr.STEP_FAILED,
                      "reason": "resume_launcher_install_failed"}
            return dict(result, command="step"), EXIT_FAILED
    result = dr.step(context, admin_consent=args.admin_consent,
                     reboot_consent=args.reboot_consent,
                     image_consent=args.image_consent,
                     provider_consent=args.provider_consent)
    if result["status"] == dr.STEP_OK and result["step"] == wp.STAGE_READY:
        finished = dr.finish_resume(context)
        result = dict(result, finish=finished)
        if finished["status"] not in (dr.STEP_OK,):
            return dict(result, command="step"), _EXIT_FOR_STATUS.get(
                finished["status"], EXIT_FAILED)
    return dict(result, command="step"), _EXIT_FOR_STATUS.get(result["status"],
                                                              EXIT_FAILED)


def command_status(context: dr.DriverContext, args: argparse.Namespace
                   ) -> tuple[dict[str, Any], int]:
    state = dr.resume(context)
    status = context.run(wp.resume_status_argv())
    registered = getattr(status, "returncode", 1) == 0
    return ({"command": "status", "resume": (state.as_dict() if state else None),
             "resume_task_registered": registered,
             "resume_task_name": wp.RESUME_TASK_NAME,
             "resume_command": list(context.resume_command or ())}, EXIT_OK)


def command_resume(context: dr.DriverContext, args: argparse.Namespace
                   ) -> tuple[dict[str, Any], int]:
    """Continue where the restart left off, then retire the machinery.

    Three distinct outcomes, none of which may be collapsed into the others:

    * Nothing to resume. The ordinary case on a machine that never rebooted,
      and on the second logon after setup finished. Exit 0, change nothing.
    * Still provisioning. Stages advanced until one needed a person. The
      record and the task stay, so the next logon continues.
    * Terminal. Either delegation may be enabled, or the stage has exhausted
      its attempts and another reboot will not change that. Only here are the
      record and the logon task removed, and only through ``finish_resume``,
      which re-checks the condition itself rather than trusting this loop.

    Consents are never carried across a reboot. A stage that needs one stops
    here and waits for a person to run ``step``, because an unattended logon
    task that could elevate on a consent given before the restart is a
    standing grant nobody re-affirmed.
    """

    state = dr.resume(context)
    if state is None:
        return {"command": "resume", "status": dr.STEP_OK,
                "resumed": False, "reason": "nothing_to_resume"}, EXIT_OK
    if state.reason:
        # A record that would not parse or was not protected. Reported, and
        # deliberately not cleared: the difference between "setup never ran"
        # and "setup's state was tampered with" is worth keeping.
        return ({"command": "resume", "status": dr.STEP_FAILED, "resumed": True,
                 "reason": state.reason, "resume": state.as_dict()}, EXIT_FAILED)

    steps: list[dict[str, Any]] = []
    last: dict[str, Any] | None = None
    if not state.exhausted:
        for _ in range(MAX_RESUME_STEPS):
            last = dr.step(context)
            steps.append(last)
            if last["status"] != dr.STEP_OK:
                break
            if last["step"] == wp.STAGE_READY:
                break

    finished = dr.finish_resume(context)
    payload = {"command": "resume", "resumed": True,
               "resume": state.as_dict(), "steps": steps,
               "finish": finished}
    if finished["status"] == dr.STEP_OK:
        payload["status"] = dr.STEP_OK
        payload["reason"] = finished.get("terminal", "")
        return payload, EXIT_OK
    if finished["reason"] == "still_provisioning":
        # Not a failure. The record and the task are intact and the next
        # logon continues; the exit code says a person is needed.
        payload["status"] = (last or {}).get("status", dr.STEP_BLOCKED)
        payload["reason"] = (last or {}).get("reason", "still_provisioning")
        return payload, _EXIT_FOR_STATUS.get(payload["status"], EXIT_BLOCKED)
    payload["status"] = finished["status"]
    payload["reason"] = finished["reason"]
    return payload, _EXIT_FOR_STATUS.get(finished["status"], EXIT_FAILED)


def command_validate(context: dr.DriverContext, args: argparse.Namespace
                     ) -> tuple[dict[str, Any], int]:
    """Run the deterministic validation suite and print one report.

    Reads the machine and writes nothing to it. The report is evidence a
    person reads and attaches; it is deliberately not the durable record the
    planner trusts, which :mod:`windows_evidence` owns and which refuses to be
    written anywhere but on Windows.

    ``--report`` also writes the same bytes to a file, because the thing that
    makes a validation run useful is being able to diff it against the last
    one.
    """

    rows = wv.run_checks(_validation_runners(context))
    report = wv.build_report(rows, recorded_at=_now())
    if getattr(args, "report", None):
        try:
            Path(args.report).write_bytes(wv.render(report).encode("utf-8"))
        except OSError:
            # The report is on stdout either way. Failing the run because a
            # path was not writable would throw away the observation.
            report = dict(report, envelope=dict(report["envelope"],
                                                written=False))
    payload = dict(report, command="validate")
    code = EXIT_OK if report["verdict"] == wv.VERDICT_READY else EXIT_FAILED
    return payload, code


def _validation_runners(context: dr.DriverContext) -> dict[str, Any]:
    """Wire the context into the suite, supplying only what is really known.

    A manifest that will not load supplies no manifest, so the image check is
    reported ``check_not_implemented`` rather than passing on a default. The
    guest round trip is wired only when the context carries a way to run one:
    validation reads a machine, and spinning up an instance the caller did not
    provide the means for would be validation doing provisioning.
    """

    config = context.config
    try:
        manifest = wd.load_manifest(config.manifest_path)
    except wd.DelegationRefused:
        manifest = None
    return wv.default_runners(
        run=context.run,
        platform_name=context.platform_name,
        feature_states=(lambda: dict(context.feature_states)
                        if context.feature_states else None),
        reboot_pending=lambda: bool(context.reboot_pending),
        runtime_root=config.runtime_root,
        rootfs_path=config.rootfs_path if manifest is not None else None,
        manifest=manifest,
        distro_name=None,
        run_job=None)


def _now() -> str:
    """The one place a wall clock is read, and it never reaches the verdict."""

    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


COMMANDS = {"plan": command_plan, "step": command_step, "resume": command_resume,
            "status": command_status, "validate": command_validate}


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-bridge-windows-setup",
        description="Provision Windows delegation, one consented step at a time.")
    parser.add_argument("command", choices=sorted(COMMANDS),
                        help="plan, step, resume, status or validate")
    parser.add_argument("--runtime-root", default=None,
                        help="installation directory (default: per-user LOCALAPPDATA)")
    parser.add_argument("--rootfs", default=None, help="pinned guest rootfs tarball")
    parser.add_argument("--manifest", default=None, help="manifest for that rootfs")
    parser.add_argument("--sidecar", default=None, help="optional manifest sidecar")
    parser.add_argument("--image", default=None,
                        help="source tarball to install at the image stage")
    # Consents are separate flags and never a single --yes. Each one covers a
    # different surprise: machine-wide features, ending the session,
    # installing an image, and spending a provider session.
    parser.add_argument("--admin-consent", action="store_true",
                        help="consent to elevated, machine-wide changes")
    parser.add_argument("--reboot-consent", action="store_true",
                        help="consent to restarting this machine")
    parser.add_argument("--image-consent", action="store_true",
                        help="consent to installing the verified guest image")
    parser.add_argument("--provider-consent", action="store_true",
                        help="consent to spending a provider session on a probe")
    parser.add_argument("--report", default=None,
                        help="also write the validate report to this path")
    return parser


def main(argv: Sequence[str] | None = None, *,
         context_factory: Callable[[argparse.Namespace], dr.DriverContext] | None = None,
         stream: Any = None) -> int:
    """Parse, build one context, run one subcommand, print one object.

    ``context_factory`` is the single seam, and it exists so tests drive this
    function rather than a copy of it. Everything a test would want to fake is
    behind it; the argument parsing, the dispatch, the JSON and the exit codes
    are the production ones either way.
    """

    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit:
        return EXIT_USAGE
    out = stream if stream is not None else sys.stdout
    try:
        context = (context_factory(args) if context_factory is not None
                   else build_context(args))
    except (SetupError, wd.DelegationRefused) as exc:
        json.dump({"command": args.command, "status": dr.STEP_FAILED,
                   "reason": getattr(exc, "reason", None) or "setup_unconfigured"},
                  out)
        out.write("\n")
        return EXIT_FAILED
    payload, code = COMMANDS[args.command](context, args)
    json.dump(payload, out, default=str, sort_keys=True)
    out.write("\n")
    return code


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
