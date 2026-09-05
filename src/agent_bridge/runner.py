"""Peer process execution.

Fixed argument arrays only.  There is no code path in this module that accepts
a command string, and shell=False is passed explicitly on every call so a
future edit cannot flip it by omission.

Every peer starts in its own process group.  On timeout the whole group is
signalled, given a grace period, then killed.

The limit of that guarantee, stated precisely: it covers descendants that
remain in the peer's process group.  A descendant that calls setsid() and
creates its own session leaves the group and will survive group termination. Nothing
here is an OS-level containment mechanism, and no such claim is made.  Closing
that gap would need a supervisor outside this process, which version 1 does
not have.  Group kill is measured to work on a forked child in the normal
case; it is not a boundary against a process that deliberately escapes.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

from .platform import platform


#: How long to keep draining after the peer process itself has exited, when a
#: descendant still holds a pipe open.
POST_EXIT_DRAIN_SECONDS = 0.5

# Test-only rendezvous used to kill a real worker immediately after a marker
# transition. It is carried in the fake peer's declared environment, never
# inherited from the broker process.
MARKER_FAULT_PHASE_ENV = "AGENT_BRIDGE_TEST_PAUSE_AFTER_MARKER_PHASE"
MARKER_FAULT_PAUSE_SECONDS = 30.0


@dataclass
class RunResult:
    argv: list[str]
    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool
    duration_seconds: float
    pgid: int | None
    spawn_failed: bool = False
    #: True when the run was refused because its in-flight marker could not be
    #: written. No peer process was created.
    marker_write_failed: bool = False
    group_kill: dict[str, Any] = field(default_factory=dict)
    cap_exceeded: bool = False
    descendant_held_pipes: bool = False

    def sanitized_argv(self) -> list[str]:
        """argv shape for provenance. Paths kept, no secrets are ever in argv."""
        return list(self.argv)


def run(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    stdin_data: str,
    timeout: float,
    grace: float,
    stdout_cap: int,
    stderr_cap: int,
    pgid_file: str | None = None,
) -> RunResult:
    """Run a peer CLI with a fixed argv. Never a shell string.

    pgid_file, when given, receives the peer's process group id immediately
    after spawn. If this worker is killed mid-call the peer survives, because
    it lives in its own session by design; the recorded pgid is what lets a
    later reconcile reap it instead of leaving an unbounded orphan.
    """
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        raise TypeError("argv must be a list of strings")
    started = time.monotonic()

    # Write the in-flight marker BEFORE the peer exists. The identity fields are
    # unknown at this point and are filled in immediately after spawn, but the
    # marker's presence is what blocks a conversation from being reassigned.
    # Writing it only after Popen left a window in which a worker could die with
    # a live peer and no durable evidence of it, which is an invisible orphan.
    # A surviving pre_spawn marker is therefore treated as indeterminate even
    # though it names no process: not knowing whether a peer was created is
    # itself a reason to hold.
    if pgid_file:
        if not _write_marker(pgid_file, {"phase": "pre_spawn", "argv0": argv[0],
                                         "marker_written_at": time.time()}):
            # Fail before creating anything. Without the marker a crash would
            # leave a peer nothing could see, quarantine or reap.
            return RunResult(
                argv=argv, returncode=None, stdout=b"", stderr=b"",
                timed_out=False, duration_seconds=time.monotonic() - started,
                pgid=None, spawn_failed=True, marker_write_failed=True,
            )
        pause_after_marker_transition(env, "pre_spawn")

    try:
        if env.get(MARKER_FAULT_PHASE_ENV) == "spawn_failed":
            raise OSError("injected peer spawn failure")
        proc = platform.spawn_isolated(argv, cwd=cwd, env=env)
    except (OSError, ValueError):
        # A confirmed spawn failure proves no child was created, so the
        # pre_spawn marker is retired: holding a conversation for a peer that
        # never existed is a pointless outage. If retirement itself fails the
        # evidence stays and the conversation holds, which is the safe
        # direction.
        if pgid_file:
            try:
                os.unlink(pgid_file)
            except OSError:
                pass
            pause_after_marker_transition(env, "spawn_failed")
        return RunResult(
            argv=argv, returncode=None, stdout=b"", stderr=b"", timed_out=False,
            duration_seconds=time.monotonic() - started, pgid=None, spawn_failed=True,
        )

    pgid = platform.isolated_process_group(proc.pid)
    if pgid_file:
        # Enrich the pre-spawn marker with spawn identity. A bare PGID is not
        # identity: it can be recycled, and signalling a recycled group would be
        # worse than leaving an orphan. The leader pid plus its ps start time is
        # what lets a later reaper prove the group is still ours.
        leader_start = platform.process_identity(proc.pid) if pgid is not None else ""
        _write_marker(pgid_file, {
            "phase": "spawned",
            "pgid": pgid,
            "leader_pid": proc.pid,
            "leader_start": leader_start,
            "argv0": argv[0],
            "spawned_at": time.time(),
        })
        pause_after_marker_transition(env, "spawned")

    group_kill: dict[str, Any] = {}
    # Named fields, not positional unpacking: see StreamReadResult for why.
    read = platform.read_streams_with_caps(
        proc, stdin_data, timeout, stdout_cap, stderr_cap,
        POST_EXIT_DRAIN_SECONDS)
    stdout, stderr = read.stdout, read.stderr
    timed_out = read.timed_out
    cap_exceeded = read.cap_exceeded
    descendant_held_pipes = read.descendant_held_pipes
    if timed_out or cap_exceeded:
        if pgid is not None:
            group_kill = platform.terminate_process_tree(pgid, grace)
        else:
            try:
                proc.kill()
            except OSError:
                pass

    # Reap and confirm the group is gone even on the normal path, so a peer
    # that forked a lingering child is recorded rather than silently leaked.
    returncode = proc.poll()
    if returncode is None:
        try:
            returncode = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if pgid is not None:
                group_kill = platform.terminate_process_tree(pgid, grace)
            returncode = proc.poll()
    if pgid is not None and not group_kill:
        if platform.process_tree_alive(pgid):
            group_kill = platform.terminate_process_tree(pgid, grace)
            group_kill["killed_lingering_group"] = True

    # The local process is done, but the ATTEMPT is not: the worker has yet to
    # durably commit the peer's session id, the turn increment and the claim
    # release. A worker dying in that interval would leave a conversation whose
    # remote turn may well have completed, and deleting the marker here would
    # let admission resume it as though nothing had happened.
    #
    # So the marker is transitioned, not removed. Its lifetime spans the whole
    # attempt: written before the peer exists, and retired only once the worker
    # has committed. That is what makes the set of lifecycle windows closed
    # rather than a sequence of ever-narrower ones to patch individually.
    if pgid_file:
        _write_marker(pgid_file, {
            "phase": "peer_exited_uncommitted",
            "argv0": argv[0],
            "pgid": pgid,
            "returncode": returncode,
            "peer_exited_at": time.time(),
        })
        pause_after_marker_transition(env, "peer_exited_uncommitted")

    return RunResult(
        argv=argv,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        duration_seconds=time.monotonic() - started,
        pgid=pgid,
        group_kill=group_kill,
        cap_exceeded=cap_exceeded,
        descendant_held_pipes=descendant_held_pipes,
    )


def _write_marker(path: str, payload: dict[str, Any]) -> bool:
    """Write an in-flight marker durably. Returns whether it landed.

    The caller must check this for the PRE-SPAWN write. Treating a safety
    mechanism as best-effort is a contradiction: if the marker cannot be
    written there is no durable evidence, and spawning anyway produces exactly
    the invisible orphan the marker exists to prevent.
    """
    try:
        directory = os.path.dirname(os.path.abspath(path))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, mode=0o700, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            platform.enforce_owner_only_file(handle.fileno())
            handle.write(json.dumps(payload, sort_keys=True))
            handle.flush()
            os.fsync(handle.fileno())
        return True
    except OSError:
        return False


def pause_after_marker_transition(env: dict[str, str], phase: str) -> None:
    """Pause a fake-backed run so the suite can kill its worker at a boundary."""
    if env.get(MARKER_FAULT_PHASE_ENV) == phase:
        time.sleep(MARKER_FAULT_PAUSE_SECONDS)


def scrubbed_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Minimal environment for a peer. No inherited API keys, no TMPDIR."""
    keep = ["PATH", "HOME", "LANG", "LC_ALL", "TERM", "SHELL", "USER", "LOGNAME"]
    if os.name == "nt":
        # Windows essentials. A Python child without SYSTEMROOT frequently fails
        # to initialise at all, and without PATHEXT it cannot resolve a command
        # by name. Scrubbing is about credentials, not about breaking the OS.
        keep += ["SYSTEMROOT", "SystemRoot", "COMSPEC", "PATHEXT", "WINDIR",
                 "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
                 "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE"]
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    env["AGENT_BRIDGE_PEER"] = "1"
    # Explicitly drop credentials that would otherwise leak into a peer run.
    for banned in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AWS_ACCESS_KEY_ID",
                   "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "TMPDIR"):
        env.pop(banned, None)
    if extra:
        env.update(extra)
    return env
