"""Per-user background activation of the execution worker on Windows.

The POSIX side runs the worker from a launchd agent in the user's own
``LaunchAgents`` directory. This is the Windows equivalent: a logon-triggered
task in the current user's own Task Scheduler namespace.

Three properties are deliberate and tested:

* **No administrator.** The task is created without ``/RU``, ``/RL HIGHEST``
  or ``/S``, so it lives in the calling user's namespace and runs as them.
  Nothing about running a background worker needs machine-wide rights, and
  asking for them would mean a worker that survives the user signing out and
  can touch other users' state.
* **Reversible by the person who created it.** :func:`unregister_argv`
  removes exactly the task this module creates, and the user can see and
  delete it themselves in Task Scheduler. Activation is not a one-way door.
* **Nothing variable is interpolated into a command line.** ``schtasks``
  takes the action as a single string, which is a quoting problem waiting to
  happen, so every argument is validated and quoted, a length that would be
  silently truncated is refused, and a path containing a quote is rejected
  rather than escaped.

Registering a task is an outward-facing change to the user's machine, so the
caller must pass explicit consent. Nothing here runs a command; it builds
argv and the caller executes them with its own bounded runner.

Nothing here has been validated on a live Windows host.
"""

from __future__ import annotations

import ntpath
import re
from dataclasses import dataclass
from typing import Any, Sequence

from . import windows_wsl as ww

#: The single task this module owns. Fixed so that unregistering cannot
#: remove something else, and so a user reading Task Scheduler sees a name
#: that says what it is.
TASK_NAME = "AgentBridgeExecutionWorker"

#: schtasks truncates a /TR longer than this without saying so, and a
#: truncated command is a different command.
MAX_TASK_ACTION_CHARS = 261

#: Start the worker a short time after logon rather than instantly: at logon
#: the user's profile, network and any credential helpers are still settling,
#: and a worker that starts into a half-ready session fails for reasons that
#: have nothing to do with the work.
DEFAULT_START_DELAY = "PT1M"

_DELAY_RE = re.compile(r"^PT(?:\d{1,2}M|\d{1,2}H)$")


class ActivationError(ValueError):
    """The requested activation is malformed or unsafe to register."""


class ConsentRequired(RuntimeError):
    """Activation changes the user's machine and was not authorised."""


def _system32(name: str) -> str:
    """A trusted absolute path, never a bare name resolved through PATH."""

    return ntpath.join("C:\\Windows\\System32", name)


def quote_action_argument(value: Any) -> str:
    """Quote one argument for the single-string ``/TR`` action.

    A value containing a double quote is refused rather than escaped.
    ``schtasks`` does not document an escape for it, cmd and the task engine
    disagree about how one would be parsed, and guessing here would produce a
    task that runs a command nobody wrote.
    """

    if not isinstance(value, str) or not value:
        raise ActivationError("every command argument must be a non-empty string")
    if any(character in value for character in ('"', "\x00", "\n", "\r", "\t")):
        raise ActivationError("command arguments must not contain quotes or control characters")
    if "%" in value:
        # The task engine expands environment variables in the action string,
        # so a literal % would become something else at logon.
        raise ActivationError("command arguments must not contain '%'")
    return f'"{value}"' if " " in value else value


def validate_executable(value: Any) -> str:
    """An absolute local Windows path, by the same contract the runtime uses.

    ``ntpath.isabs`` returns True for ``\\\\server\\share\\x.exe`` and for
    ``\\\\?\\C:\\x.exe``. A logon task pointing at a network share runs
    whatever that share serves at sign-in, and a device path sidesteps the
    normalisation every other check here relies on. Neither is a local
    executable, so neither is accepted.
    """

    if not isinstance(value, str) or not value:
        raise ActivationError("the command must be an absolute path")
    try:
        ww._validate_windows_host_path("command", value)
    except ww.WindowsWslContractError as exc:
        # Re-raised as this module's error, without the offending value: the
        # message goes into installer output and durable records.
        raise ActivationError("the command must be a local absolute path") from exc
    return value


def build_action(command: Sequence[str]) -> str:
    """The exact string schtasks will store, validated for length."""

    argv = list(command)
    if not argv:
        raise ActivationError("command must not be empty")
    validate_executable(argv[0])
    action = " ".join(quote_action_argument(item) for item in argv)
    if len(action) > MAX_TASK_ACTION_CHARS:
        raise ActivationError("command is too long for a scheduled task action")
    return action


def register_argv(command: Sequence[str], *, delay: str = DEFAULT_START_DELAY,
                  task_name: str = TASK_NAME) -> list[str]:
    """Create the per-user logon task. No elevation, no other user."""

    _validate_task_name(task_name)
    if not _DELAY_RE.match(delay or ""):
        raise ActivationError("delay must be an ISO-8601 PTnM or PTnH value")
    return [
        _system32("schtasks.exe"), "/Create",
        "/TN", task_name,
        "/SC", "ONLOGON",
        "/DELAY", _delay_to_schtasks(delay),
        "/TR", build_action(command),
        "/F",
    ]


def _delay_to_schtasks(delay: str) -> str:
    """schtasks wants mmmm:ss, not ISO-8601."""

    amount = int(delay[2:-1])
    minutes = amount * 60 if delay.endswith("H") else amount
    return f"{minutes:04d}:00"


def unregister_argv(task_name: str = TASK_NAME) -> list[str]:
    _validate_task_name(task_name)
    return [_system32("schtasks.exe"), "/Delete", "/TN", task_name, "/F"]


def status_argv(task_name: str = TASK_NAME) -> list[str]:
    _validate_task_name(task_name)
    return [_system32("schtasks.exe"), "/Query", "/TN", task_name,
            "/FO", "LIST", "/V"]


def run_now_argv(task_name: str = TASK_NAME) -> list[str]:
    """Start the task once without waiting for a logon.

    Offered so activation can be proven to work while the user is still in
    front of the installer, rather than discovered to be broken at the next
    sign-in.
    """

    _validate_task_name(task_name)
    return [_system32("schtasks.exe"), "/Run", "/TN", task_name]


def _validate_task_name(task_name: str) -> None:
    if not isinstance(task_name, str) or not task_name:
        raise ActivationError("task name must be a non-empty string")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", task_name):
        raise ActivationError("task name must be 1-120 characters of [A-Za-z0-9._-]")


# ---------------------------------------------------------------------------
# Status parsing
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ActivationStatus:
    """What ``schtasks /Query`` reported, fail closed.

    ``registered`` is false whenever the answer could not be read. A worker
    that might be registered is reported as not registered, because the
    consequence of the two mistakes is not symmetric: believing it is running
    when it is not means queued work sits forever.
    """

    registered: bool
    state: str = "unknown"
    detail: str = ""
    action: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"registered": self.registered, "state": self.state,
                "detail": self.detail, "action": self.action}


def parse_status(returncode: int | None, stdout: bytes) -> ActivationStatus:
    if returncode != 0:
        # schtasks exits nonzero when the task does not exist, which is the
        # common case before activation and is not an error.
        return ActivationStatus(False, "absent", "query_returned_nonzero")
    try:
        text = stdout.decode("utf-8-sig") if b"\x00" not in stdout \
            else stdout.decode("utf-16-le")
    except (UnicodeDecodeError, ValueError):
        return ActivationStatus(False, "unknown", "output_undecodable")
    state = ""
    action = ""
    for line in text.splitlines():
        if ":" not in line:
            continue
        label, _, value = line.partition(":")
        key = label.strip().lower()
        if key in ("status", "scheduled task state") and not state:
            state = value.strip()
        elif key == "task to run" and not action:
            # partition on the first colon only, so a drive letter in the
            # action survives: "Task To Run: C:\x\worker.exe" must not become
            # "C".
            action = line.partition(":")[2].strip()
    if not state:
        return ActivationStatus(False, "unknown", "state_not_reported")
    return ActivationStatus(True, state, "", action)


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------


def action_executable(action: str) -> str:
    """The program an existing task would run, unquoted.

    schtasks stores the action as one string. The executable is either the
    first quoted run or everything up to the first space.
    """

    stripped = (action or "").strip()
    if not stripped:
        return ""
    if stripped.startswith('"'):
        closing = stripped.find('"', 1)
        return stripped[1:closing] if closing > 0 else ""
    head, _, _ = stripped.partition(" ")
    return head


def proves_ownership(status: ActivationStatus, command: Sequence[str]) -> str:
    """"" if this task may be overwritten or deleted, else the reason it may not.

    A task name is not ownership. ``schtasks /Create /F`` silently replaces
    whatever is registered under the name, and ``/Delete /F`` removes it, so a
    name collision with something the user (or anything else) created would
    mean Agent Bridge destroying a stranger's task and taking over its
    trigger. The proof is that the registered task runs the same executable
    this installation manages: arguments may differ, because upgrading the
    worker's flags is ordinary, but the program may not.
    """

    if not status.registered:
        return ""
    if not status.action:
        # Registered, but the query did not say what it runs. Refuse: this is
        # precisely the case where overwriting is a guess.
        return "task_action_unreadable"
    existing = action_executable(status.action)
    if not existing:
        return "task_action_unparseable"
    ours = str(command[0]) if command else ""
    if ntpath.normcase(ntpath.normpath(existing)) != ntpath.normcase(
            ntpath.normpath(ours)):
        return "task_not_owned_by_agent_bridge"
    return ""


def query_status(*, run: Any, task_name: str = TASK_NAME) -> ActivationStatus:
    result = run(status_argv(task_name))
    return parse_status(getattr(result, "returncode", None),
                        getattr(result, "stdout", b"") or b"")


# ---------------------------------------------------------------------------
# Consent
# ---------------------------------------------------------------------------


def activate(command: Sequence[str], *, run: Any, consent: bool,
             delay: str = DEFAULT_START_DELAY,
             task_name: str = TASK_NAME) -> dict[str, Any]:
    """Register the worker, once the user has said yes.

    Refuses without consent. Registering a background task that starts every
    time the user signs in is a change to their machine, not an implementation
    detail of finishing setup.
    """

    if not consent:
        raise ConsentRequired("activation_consent_required")
    argv = register_argv(command, delay=delay, task_name=task_name)
    # Built before querying, so a malformed command is refused without
    # touching the scheduler at all.
    blocked = proves_ownership(query_status(run=run, task_name=task_name),
                               command)
    if blocked:
        return {"status": "refused", "task_name": task_name, "reason": blocked}
    result = run(argv)
    code = getattr(result, "returncode", None)
    if code != 0:
        return {"status": "failed", "task_name": task_name,
                "reason": "register_failed" if code is not None else "no_exit_code"}
    return {"status": "ok", "task_name": task_name, "reversible": True}


def deactivate(command: Sequence[str], *, run: Any,
               task_name: str = TASK_NAME) -> dict[str, Any]:
    """Remove the task. Deliberately needs no consent flag.

    Turning a background worker off is always allowed: the asymmetry is the
    point, since the risky direction is the one that adds something.

    ``command`` is still required, and is the same command activation was
    given. It is not used to build anything; it is the thing the registered
    task is checked against before ``/Delete /F`` runs, so deactivation can
    never delete a task this installation did not create.
    """

    status = query_status(run=run, task_name=task_name)
    if not status.registered:
        return {"status": "absent", "task_name": task_name}
    blocked = proves_ownership(status, command)
    if blocked:
        return {"status": "refused", "task_name": task_name, "reason": blocked}
    result = run(unregister_argv(task_name))
    code = getattr(result, "returncode", None)
    if code != 0:
        return {"status": "failed", "task_name": task_name,
                "reason": "unregister_failed" if code is not None else "no_exit_code"}
    return {"status": "ok", "task_name": task_name}


NO_LIVE_VALIDATION = (
    "no task produced by this module has been registered on a live Windows "
    "host from this worktree; the argv are reviewed and tested as data only"
)
