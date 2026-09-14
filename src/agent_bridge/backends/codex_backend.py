"""Codex consultation backend: `codex exec` and `codex exec resume`.

Verified against codex-cli 0.147.0.  Three facts drove this design and none of
them match the original build brief, so they are stated here rather than left
implicit:

1. `codex exec resume` accepts neither `-s/--sandbox` nor `-C/--cd`.  The
   sandbox is therefore set via `-c sandbox_mode="read-only"`, which resume
   does accept, and the working root is set by the subprocess cwd.
2. The session identifier is `thread_id`, carried on the `thread.started`
   JSONL event.  There is no `session_id` field.  Resume takes that thread id.
3. The final message is read from `-o/--output-last-message`, a documented file
   channel, rather than by guessing which event carries the terminal text.

Isolation: `--ignore-user-config` means $CODEX_HOME/config.toml is not loaded,
so the bridge's own MCP server cannot be registered for a peer run.  That is
what blocks Claude -> Codex -> Claude recursion on this side.

The read-only sandbox restricts writes, not reads.  This is not a filesystem
read boundary and is not claimed as one.  The controls that matter here are
the refusal of client-derived content, an empty isolated workspace, no user
config or rules, and an instruction not to inspect the filesystem.
"""

from __future__ import annotations

import json
import os
from typing import Any

from .. import preflight, runner, store
from ..config import Config
from ..errors import ErrorCategory
from .base import PeerOutcome, auth_failure_reason, parse_single_object

PEER = "codex"


def _executable(cfg: Config) -> str:
    """The peer binary: the pinned path if configured, otherwise discovered."""
    from .. import preflight
    return cfg.peer(PEER).get("executable") or preflight.discover_executable(PEER) or PEER

#: Event carrying the session identifier, and the field within it.
THREAD_STARTED_EVENT = "thread.started"
THREAD_ID_FIELD = "thread_id"


def build_argv(
    cfg: Config,
    *,
    schema_file: str,
    last_message_file: str,
    thread_id: str | None,
    workspace: str,
) -> list[str]:
    spec = cfg.peer(PEER)
    sandbox = str(spec.get("sandbox_mode", "read-only"))
    argv: list[str] = [_executable(cfg), "exec"]
    if thread_id:
        # `codex exec resume [OPTIONS] [SESSION_ID] [PROMPT]`; "-" reads the
        # prompt from stdin.  --last is never used: continuation is by exact id.
        argv += ["resume", thread_id, "-"]
    argv += [
        "--json",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "--skip-git-repo-check",
        "--output-schema", schema_file,
        "--output-last-message", last_message_file,
        "-c", f'sandbox_mode="{sandbox}"',
    ]
    effort = cfg.peer_reasoning_effort(PEER)
    if effort:
        # Accepted on both `exec` and `exec resume`, verified against 0.147.0
        # under --strict-config.
        argv += ["-c", f'model_reasoning_effort="{effort}"']
    argv += [
    ]
    if not thread_id:
        # Only `exec` accepts these two; `exec resume` rejects them.
        argv += ["-s", sandbox, "-C", workspace]
        if spec.get("model"):
            argv += ["-m", str(spec["model"])]
    return argv


def parse_events(
    stdout: bytes,
) -> tuple[str | None, list[str], list[dict[str, Any]], list[str]]:
    """Return (thread_id, error_event_messages, events, all distinct thread ids).

    On a first turn the emitted thread id is the only authority, since the
    caller cannot choose one. More than one distinct id in a single run means
    that authority is ambiguous, so the caller is given the full set and fails
    closed rather than taking whichever arrived first.

    Unknown event types are ignored rather than fatal, so a Codex minor version
    that adds events does not break the bridge.  Error events are collected for
    classification only; their text is never returned to the caller.
    """
    thread_id: str | None = None
    errors: list[str] = []
    events: list[dict[str, Any]] = []
    all_ids: list[str] = []
    for line in stdout.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        events.append(event)
        etype = event.get("type")
        if etype == THREAD_STARTED_EVENT:
            candidate = event.get(THREAD_ID_FIELD)
            if isinstance(candidate, str) and candidate:
                if candidate not in all_ids:
                    all_ids.append(candidate)
                if not thread_id:
                    thread_id = candidate
        elif etype in ("error", "turn.failed"):
            failure = event.get("error")
            message = (failure.get("message") if etype == "turn.failed" and isinstance(failure, dict)
                       else event.get("message"))
            if isinstance(message, str):
                errors.append(message)
    return thread_id, errors, events, all_ids


def run_consultation(
    cfg: Config,
    *,
    prompt: str,
    schema_file: str,
    thread_id: str | None,
    workspace: str,
    attempt_dir: str,
) -> PeerOutcome:
    spec = cfg.peer(PEER)
    codex_home = os.path.expanduser(str(spec.get("codex_home")))
    store.secure_mkdir(codex_home)
    # Fails the job closed rather than silently running with a loaded config.
    preflight.assert_peer_home_has_no_config(codex_home)
    home_inventory = preflight.peer_home_inventory(codex_home)
    last_message_file = os.path.join(attempt_dir, "last_message.txt")
    argv = build_argv(
        cfg,
        schema_file=schema_file,
        last_message_file=last_message_file,
        thread_id=thread_id,
        workspace=workspace,
    )
    env = runner.scrubbed_env(
        {**cfg.peer_extra_env(PEER), "CODEX_HOME": codex_home}
    )
    result = runner.run(
        argv,
        cwd=workspace,
        env=env,
        stdin_data=prompt,
        timeout=float(spec.get("timeout_seconds", 420)),
        grace=float(spec.get("grace_seconds", 5)),
        stdout_cap=cfg.limit("peer_stdout_max_bytes"),
        stderr_cap=cfg.limit("peer_stderr_capture_max_bytes"),
        pgid_file=os.path.join(attempt_dir, "peer.pgid") if attempt_dir else None,
    )
    outcome = PeerOutcome(
        category=ErrorCategory.OK,
        argv=argv,
        returncode=result.returncode,
        duration_seconds=result.duration_seconds,
        timed_out=result.timed_out,
        group_kill=result.group_kill,
        raw_stdout=result.stdout,
        raw_stderr=result.stderr,
        notes={"credential_context": runner.credential_context(env)},
    )

    if result.spawn_failed:
        outcome.category = ErrorCategory.PEER_SPAWN_FAILURE
        return outcome

    parsed_thread_id, error_messages, events, observed_ids = parse_events(result.stdout)
    # Never adopt a thread id other than the one asked for. Recording a
    # mismatch and then persisting the new id anyway would migrate the
    # conversation to a session the caller never opened.
    migrated = bool(thread_id and parsed_thread_id and parsed_thread_id != thread_id)
    # Ambiguous authority: more than one distinct thread id in one run.
    ambiguous = len(observed_ids) > 1
    outcome.peer_session_id = thread_id if thread_id else parsed_thread_id
    outcome.notes = {
        **outcome.notes,
        "descendant_held_pipes": result.descendant_held_pipes,
        "requested_reasoning_effort": cfg.peer_reasoning_effort(PEER),
        "peer_home": home_inventory,
        "event_count": len(events),
        "event_types": sorted({str(e.get("type")) for e in events})[:20],
        "error_event_count": len(error_messages),
        "resumed": bool(thread_id),
        "requested_thread_id": thread_id,
        "observed_thread_id": parsed_thread_id,
        "observed_thread_ids": observed_ids,
        "thread_id_ambiguous": ambiguous,
        "thread_id_honoured": (
            None if not thread_id or not parsed_thread_id else parsed_thread_id == thread_id
        ),
        "thread_migrated": migrated,
    }

    # Classification order matters and is deliberately identical in both
    # backends. It runs from "the bridge stopped this run" to "the peer stopped
    # itself" to "the peer finished but said the wrong thing".
    #
    # cap_exceeded MUST precede the returncode check. Breaching a cap makes the
    # bridge kill the process group, so the peer exits nonzero as a direct
    # consequence. Testing returncode first would label a flooding peer
    # PEER_NONZERO_EXIT, which is transient, and the bridge would retry it and
    # get flooded a second time.
    if result.timed_out:
        outcome.category = ErrorCategory.PEER_TIMEOUT
        return outcome
    if result.cap_exceeded or len(result.stdout) >= cfg.limit("peer_stdout_max_bytes"):
        outcome.category = ErrorCategory.PEER_OUTPUT_TOO_LARGE
        return outcome
    # No EOF means the capture may be a prefix. A prefix can still parse as one
    # valid JSON object while the complete stream would not have, which would
    # turn truncation into a silently accepted answer. Fail closed instead: the
    # length of the drain window is secondary to whether the stream ended.
    if result.descendant_held_pipes:
        outcome.category = ErrorCategory.PEER_OUTPUT_INCOMPLETE
        return outcome
    # Recoverable error events may precede a successful turn. Successful
    # output must not become an auth failure because stderr mentions login.
    failed = result.returncode not in (0, None) or any(
        e.get("type") == "turn.failed" for e in events)
    if failed:
        reason = auth_failure_reason(result.stderr, *error_messages)
        if reason:
            outcome.notes.update(auth_failure_reason=reason, auth_failure_source="failed_process_diagnostics")
        outcome.category = ErrorCategory.PEER_AUTH_FAILURE if reason else ErrorCategory.PEER_NONZERO_EXIT
        return outcome
    # Migration is checked last of the failure modes: it is a statement about a
    # run that otherwise completed, so a timeout or a kill takes precedence.
    if migrated or ambiguous:
        outcome.category = ErrorCategory.PEER_SESSION_MIGRATED
        return outcome

    # Documented channel first: the last-message file.
    text = ""
    if os.path.isfile(last_message_file):
        try:
            size = os.path.getsize(last_message_file)
            if size > cfg.limit("peer_last_message_max_bytes"):
                outcome.category = ErrorCategory.PEER_OUTPUT_TOO_LARGE
                return outcome
            with open(last_message_file, "rb") as handle:
                raw = handle.read()
            outcome.raw_last_message = raw
            text = raw.decode("utf-8", "replace")
        except OSError:
            text = ""
    if text.strip():
        payload = parse_single_object(text)
        if payload is not None:
            outcome.extraction_path = "output_last_message:json"
            outcome.payload = payload
            if not outcome.peer_session_id:
                outcome.category = ErrorCategory.PEER_SESSION_ID_MISSING
            return outcome

    outcome.category = ErrorCategory.PEER_OUTPUT_MALFORMED
    return outcome
