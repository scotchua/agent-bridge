"""Claude consultation backend: `claude -p`.

Isolation is layered, because no single flag is load-bearing:
  --safe-mode              disables CLAUDE.md, skills, plugins, hooks, MCP
                           servers, commands, agents. Auth still works.
  --strict-mcp-config      only MCP servers from --mcp-config are considered,
  --mcp-config '{}'        and that set is empty. The peer therefore cannot
                           load this bridge and call back into it.
  --tools ""               no built-in tools.
  --setting-sources ''     no user/project/local settings.
  --max-budget-usd         hard per-call spend ceiling.

Verified against 2.1.229 at the pinned path.  Two envelope facts matter:
`subtype` reports "success" even on a failed run, so `is_error` is the only
trustworthy success signal; and `--session-id` is honoured exactly, which is
what makes continuation by explicit ID possible.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

from .. import runner
from ..config import Config
from ..errors import ErrorCategory
from .base import PeerOutcome, looks_like_auth_failure, parse_single_object

PEER = "claude"


def _executable(cfg: Config) -> str:
    """The peer binary: the pinned path if configured, otherwise discovered."""
    from .. import preflight
    return cfg.peer(PEER).get("executable") or preflight.discover_executable(PEER) or PEER


def new_session_id() -> str:
    return str(uuid.uuid4())


def build_argv(cfg: Config, schema: dict[str, Any], session_id: str, resume: bool) -> list[str]:
    spec = cfg.peer(PEER)
    argv: list[str] = [
        _executable(cfg),
        "-p",
        "--output-format", "json",
        "--json-schema", json.dumps(schema, sort_keys=True),
    ]
    if resume:
        argv += ["--resume", session_id]
    else:
        argv += ["--session-id", session_id]
    if spec.get("model"):
        argv += ["--model", str(spec["model"])]
    effort = cfg.peer_reasoning_effort(PEER)
    if effort:
        argv += ["--effort", effort]
    if spec.get("max_budget_usd") is not None:
        argv += ["--max-budget-usd", str(spec["max_budget_usd"])]
    argv += [
        "--safe-mode",
        "--strict-mcp-config",
        "--mcp-config", '{"mcpServers":{}}',
        "--setting-sources", "",
        "--tools", "",
    ]
    return argv


def _extract_payload(envelope: dict[str, Any]) -> tuple[Any | None, str | None]:
    """Find the structured object in the result envelope.

    Ordered, documented-first, and the winning channel is recorded so a peer
    that only complies via a fallback is visible in provenance rather than
    silently accepted.
    """
    for key in ("structured_output", "structuredOutput", "structured_result", "output"):
        value = envelope.get(key)
        if isinstance(value, dict):
            return value, f"envelope.{key}"
    result = envelope.get("result")
    if isinstance(result, dict):
        return result, "envelope.result:object"
    if isinstance(result, str) and result.strip():
        parsed = parse_single_object(result)
        if parsed is not None:
            return parsed, "envelope.result:json_string"
    return None, None


def run_consultation(
    cfg: Config,
    *,
    prompt: str,
    schema: dict[str, Any],
    session_id: str,
    resume: bool,
    workspace: str,
    attempt_dir: str | None = None,
) -> PeerOutcome:
    spec = cfg.peer(PEER)
    argv = build_argv(cfg, schema, session_id, resume)
    env = runner.scrubbed_env(cfg.peer_extra_env(PEER))
    result = runner.run(
        argv,
        cwd=workspace,
        env=env,
        stdin_data=prompt,
        timeout=float(spec.get("timeout_seconds", 300)),
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
    )

    if result.spawn_failed:
        outcome.category = ErrorCategory.PEER_SPAWN_FAILURE
        return outcome
    if result.timed_out:
        outcome.category = ErrorCategory.PEER_TIMEOUT
        return outcome
    # cap_exceeded must stay ahead of any exit-status reasoning: a cap breach
    # is why the bridge killed the peer, so the nonzero exit is a consequence,
    # not the cause. See the matching note in codex_backend.
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

    try:
        envelope = json.loads(result.stdout.decode("utf-8", "replace"))
    except ValueError:
        if looks_like_auth_failure(result.stdout, result.stderr):
            outcome.category = ErrorCategory.PEER_AUTH_FAILURE
        elif result.returncode not in (0, None):
            outcome.category = ErrorCategory.PEER_NONZERO_EXIT
        else:
            outcome.category = ErrorCategory.PEER_OUTPUT_MALFORMED
        return outcome
    if not isinstance(envelope, dict):
        outcome.category = ErrorCategory.PEER_OUTPUT_MALFORMED
        return outcome

    returned_session_id = envelope.get("session_id") or None
    # Symmetry with the Codex backend: never adopt a session other than the one
    # requested. An earlier version rejected a migrated Codex thread but
    # accepted whatever session_id Claude returned, so a response to
    # `--resume A` carrying session B was persisted as the new conversation
    # session. Same defect, one backend late.
    # Fail closed on ANY mismatch, fresh call included. The bridge always
    # supplies --session-id and the pinned CLI is measured to honour it
    # exactly, so a different id back means drift or corruption, not a normal
    # outcome. Adopting it on a first turn was fail-open for no benefit.
    session_migrated = bool(returned_session_id and returned_session_id != session_id)
    outcome.peer_session_id = session_id
    outcome.cost_usd = envelope.get("total_cost_usd")
    usage_models = envelope.get("modelUsage")
    if isinstance(usage_models, dict) and usage_models:
        # Record every model the run reported. Naming one by taking the
        # alphabetically first key would be an arbitrary choice presented as a
        # fact; leave the single-value field empty when it is genuinely
        # ambiguous.
        outcome.observed_models = sorted(usage_models)
        outcome.observed_model = (
            outcome.observed_models[0] if len(outcome.observed_models) == 1 else None
        )
    requested_model = spec.get("model")
    outcome.notes = {
        "descendant_held_pipes": result.descendant_held_pipes,
        # Absence of these has been observed intermittently on 2.1.241, and not
        # together. Recording presence separately turns a null in the ledger
        # into a distinguishable "the CLI did not report it" rather than an
        # ambiguous null that could equally mean the bridge failed to read it.
        "model_usage_present": isinstance(usage_models, dict) and bool(usage_models),
        "total_cost_present": envelope.get("total_cost_usd") is not None,
        "requested_model": requested_model,
        "requested_reasoning_effort": cfg.peer_reasoning_effort(PEER),
        # The config passes an alias such as "sonnet" and the CLI reports a full
        # name such as "claude-sonnet-5". The alias mapping belongs to the CLI,
        # not to this bridge, so this is a heuristic consistency signal and is
        # named as one. False on a present model is worth investigating: it
        # means you asked for one family and a different one answered.
        "requested_alias_in_observed_model": (
            None if not (requested_model and outcome.observed_models)
            else any(str(requested_model).lower() in m.lower()
                     for m in outcome.observed_models)
        ),
        "envelope_subtype": envelope.get("subtype"),
        "envelope_is_error": envelope.get("is_error"),
        "terminal_reason": envelope.get("terminal_reason"),
        "stop_reason": envelope.get("stop_reason"),
        "num_turns": envelope.get("num_turns"),
        "requested_session_id": session_id,
        "returned_session_id": returned_session_id,
        "session_id_honoured": returned_session_id == session_id,
        "session_migrated": session_migrated,
    }

    # `is_error` is authoritative. `subtype` says "success" even on failure.
    if envelope.get("is_error") is True:
        blob = envelope.get("result") if isinstance(envelope.get("result"), str) else ""
        if looks_like_auth_failure(blob, result.stderr):
            outcome.category = ErrorCategory.PEER_AUTH_FAILURE
        else:
            outcome.category = ErrorCategory.PEER_NONZERO_EXIT
        return outcome

    # A success-shaped envelope is not evidence of a successful process.  The
    # Claude CLI has emitted parseable result envelopes before returning a
    # nonzero status, so accepting the payload here would make a failed run
    # caller-visible as a completed consultation.
    if result.returncode not in (0, None):
        outcome.category = (
            ErrorCategory.PEER_AUTH_FAILURE
            if looks_like_auth_failure(result.stderr)
            else ErrorCategory.PEER_NONZERO_EXIT
        )
        return outcome

    if session_migrated:
        outcome.category = ErrorCategory.PEER_SESSION_MIGRATED
        return outcome
    if not returned_session_id:
        # The peer never echoed a session id, so there is no confirmation that
        # a resumable session exists. Do not assume ours took effect.
        outcome.category = ErrorCategory.PEER_SESSION_ID_MISSING
        return outcome

    payload, path = _extract_payload(envelope)
    if payload is None:
        outcome.category = ErrorCategory.PEER_OUTPUT_MALFORMED
        return outcome
    outcome.payload = payload
    outcome.extraction_path = path
    if not outcome.peer_session_id:
        outcome.category = ErrorCategory.PEER_SESSION_ID_MISSING
    return outcome
