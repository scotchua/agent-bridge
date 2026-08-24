"""Peer process execution.

Fixed argument arrays only.  There is no code path in this module that accepts
a command string, and shell=False is passed explicitly on every call so a
future edit cannot flip it by omission.

Every peer starts in its own process group.  On timeout the whole group is
signalled, given a grace period, then killed.

The limit of that guarantee, stated precisely: it covers descendants that
remain in the peer's process group.  A descendant that calls setsid() and
creates its own session leaves the group and will survive killpg.  Nothing
here is an OS-level containment mechanism, and no such claim is made.  Closing
that gap would need a supervisor outside this process, which version 1 does
not have.  Group kill is measured to work on a forked child in the normal
case; it is not a boundary against a process that deliberately escapes.
"""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any


#: How long to keep draining after the peer process itself has exited, when a
#: descendant still holds a pipe open.
POST_EXIT_DRAIN_SECONDS = 0.5


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


def _kill_group(pgid: int, grace: float) -> dict[str, Any]:
    """SIGTERM the group, wait out the grace period, then SIGKILL. Report both."""
    report: dict[str, Any] = {"pgid": pgid, "sigterm": False, "sigkill": False}
    try:
        os.killpg(pgid, signal.SIGTERM)
        report["sigterm"] = True
    except (ProcessLookupError, PermissionError, OSError):
        pass
    deadline = time.monotonic() + max(0.0, grace)
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            report["group_gone_after_sigterm"] = True
            return report
        except OSError:
            break
        time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
        report["sigkill"] = True
    except (ProcessLookupError, PermissionError, OSError):
        pass
    time.sleep(0.05)
    try:
        os.killpg(pgid, 0)
        report["group_survived_sigkill"] = True
    except ProcessLookupError:
        report["group_gone_after_sigkill"] = True
    except OSError:
        pass
    return report


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

    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, shell explicitly off
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,   # own process group, killable as a unit
            shell=False,
            close_fds=True,
        )
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
        return RunResult(
            argv=argv, returncode=None, stdout=b"", stderr=b"", timed_out=False,
            duration_seconds=time.monotonic() - started, pgid=None, spawn_failed=True,
        )

    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = None
    if pgid_file:
        # Enrich the pre-spawn marker with spawn identity. A bare PGID is not
        # identity: it can be recycled, and signalling a recycled group would be
        # worse than leaving an orphan. The leader pid plus its ps start time is
        # what lets a later reaper prove the group is still ours.
        leader_start = ""
        if pgid is not None:
            try:
                probe = subprocess.run(  # noqa: S603 - fixed argv, shell off
                    ["ps", "-o", "lstart=", "-p", str(proc.pid)],
                    capture_output=True, timeout=10, check=False, shell=False)
                leader_start = probe.stdout.decode("utf-8", "replace").strip()
            except (OSError, subprocess.SubprocessError):
                leader_start = ""
        _write_marker(pgid_file, {
            "phase": "spawned",
            "pgid": pgid,
            "leader_pid": proc.pid,
            "leader_start": leader_start,
            "argv0": argv[0],
            "spawned_at": time.time(),
        })

    timed_out = False
    cap_exceeded = False
    descendant_held_pipes = False
    group_kill: dict[str, Any] = {}

    # Non-blocking selector loop, no reader threads.
    #
    # The previous version used three daemon threads and joined them with a
    # timeout. When a descendant inherited stdout or stderr and outlived the
    # peer, an expired join abandoned a thread still blocked on a pipe, so a
    # long-lived MCP process could accumulate threads and descriptors across
    # jobs. Here the parent owns every descriptor and closes all of them in a
    # finally, whatever the peer or its descendants do.
    buffers: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
    caps = {"stdout": max(0, stdout_cap), "stderr": max(0, stderr_cap)}
    pending = stdin_data.encode("utf-8")
    selector = selectors.DefaultSelector()
    registered: set[Any] = set()

    def register(stream: Any, events: int, name: str) -> None:
        if stream is None:
            return
        try:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, events, name)
            registered.add(stream)
        except (OSError, ValueError, KeyError):
            pass

    def unregister(stream: Any) -> None:
        try:
            selector.unregister(stream)
        except (KeyError, ValueError, OSError):
            pass
        registered.discard(stream)
        try:
            stream.close()
        except (OSError, ValueError):
            pass

    try:
        register(proc.stdout, selectors.EVENT_READ, "stdout")
        register(proc.stderr, selectors.EVENT_READ, "stderr")
        if pending:
            register(proc.stdin, selectors.EVENT_WRITE, "stdin")
        elif proc.stdin is not None:
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass

        deadline = time.monotonic() + timeout
        exited_at: float | None = None
        while True:
            if time.monotonic() >= deadline:
                timed_out = True
                break
            reading = any(
                key.data in ("stdout", "stderr") for key in selector.get_map().values()
            ) if selector.get_map() else False
            if proc.poll() is not None:
                if exited_at is None:
                    exited_at = time.monotonic()
                if not reading:
                    break
                # The peer has exited but a pipe is still open, which means a
                # descendant inherited it. Drain briefly for output written just
                # before exit, then stop. Waiting for EOF would let an unrelated
                # descendant stretch a finished job to the full timeout and get
                # it reported as a timeout, which is simply wrong.
                if time.monotonic() - exited_at > POST_EXIT_DRAIN_SECONDS:
                    descendant_held_pipes = True
                    break
            for key, _events in selector.select(timeout=0.05):
                name = key.data
                stream = key.fileobj
                if name == "stdin":
                    try:
                        # Prefer the raw stream: its write returns a count, or
                        # None when it would block, so a partial write is
                        # always accounted for.
                        raw = getattr(stream, "raw", None)
                        written = raw.write(pending) if raw is not None \
                            else stream.write(pending)
                    except BlockingIOError as exc:
                        # A buffered writer reports the partial count here.
                        # Dropping it would resend bytes the peer already read
                        # and duplicate part of the prompt.
                        written = getattr(exc, "characters_written", 0) or 0
                    except InterruptedError:
                        continue
                    except (BrokenPipeError, OSError, ValueError):
                        pending = b""
                        unregister(stream)
                        continue
                    pending = pending[(written or 0):]
                    if not pending:
                        unregister(stream)
                    continue
                try:
                    chunk = stream.read(65536)
                except (BlockingIOError, InterruptedError):
                    continue
                except (OSError, ValueError):
                    unregister(stream)
                    continue
                if not chunk:          # EOF on this pipe
                    unregister(stream)
                    continue
                buffer = buffers[name]
                room = caps[name] - len(buffer)
                if room > 0:
                    buffer.extend(chunk[:room])
                if len(buffer) >= caps[name]:
                    cap_exceeded = True
                    break
            if cap_exceeded:
                break
            if not selector.get_map() and proc.poll() is not None:
                break

        if timed_out or cap_exceeded:
            if pgid is not None:
                group_kill = _kill_group(pgid, grace)
            else:
                try:
                    proc.kill()
                except OSError:
                    pass
    finally:
        # Every descriptor this process opened is closed here, including ones a
        # descendant still holds open on its own end.
        for stream in list(registered):
            unregister(stream)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
        selector.close()

    stdout, stderr = bytes(buffers["stdout"]), bytes(buffers["stderr"])

    # Reap and confirm the group is gone even on the normal path, so a peer
    # that forked a lingering child is recorded rather than silently leaked.
    returncode = proc.poll()
    if returncode is None:
        try:
            returncode = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if pgid is not None:
                group_kill = _kill_group(pgid, grace)
            returncode = proc.poll()
    if pgid is not None and not group_kill:
        try:
            os.killpg(pgid, 0)
            group_kill = _kill_group(pgid, grace)
            group_kill["killed_lingering_group"] = True
        except (ProcessLookupError, OSError):
            pass

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
            os.fchmod(handle.fileno(), 0o600)
            handle.write(json.dumps(payload, sort_keys=True))
            handle.flush()
            os.fsync(handle.fileno())
        return True
    except OSError:
        return False


def scrubbed_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Minimal environment for a peer. No inherited API keys, no TMPDIR."""
    keep = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "SHELL", "USER", "LOGNAME")
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
