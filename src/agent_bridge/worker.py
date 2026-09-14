"""Worker: runs one consultation to a terminal state.

Spawned detached, in its own session, by the MCP process.  It owns the retry
decision, contract validation, quarantine, provenance and the ledger record.
It writes status atomically at every transition, so the MCP process can die and
restart without losing the job.

Retry rules enforced here:
  deterministic  -> no retry, ever
  transient      -> at most one retry, same prompt
  malformed or
  schema-invalid -> at most one corrective retry, whose prompt is generated
                    only from a closed error code plus schema metadata

The attempt log records the prompt hash of each attempt, so "was an identical
prompt retried" is a checkable fact rather than a claim.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from typing import Any

from . import envelope, preflight, provenance, registry, runner, schema_validate, store
from .backends import claude_backend, codex_backend
from .backends.base import PeerOutcome, argv_shape
from .config import Config, load as load_config
from .errors import CORRECTIVE, DETERMINISTIC, TRANSIENT, ErrorCategory, BrokerError

MAX_ATTEMPTS = 2


def _quarantine(cfg: Config, job_dir: str, attempt: int, outcome: PeerOutcome) -> str:
    """Persist unusable peer output for later human inspection, owner-only.

    Quarantined bytes are never read back into a prompt and never returned to
    the caller.  This directory exists so a failure can be diagnosed by a
    person, not so the broker can salvage it.
    """
    qdir = store.secure_mkdir(os.path.join(job_dir, "quarantine", f"attempt-{attempt}"))
    if outcome.raw_stdout:
        store.atomic_write_bytes(os.path.join(qdir, "stdout.bin"), outcome.raw_stdout)
    if outcome.raw_stderr:
        store.atomic_write_bytes(os.path.join(qdir, "stderr.bin"), outcome.raw_stderr)
    if outcome.raw_last_message:
        store.atomic_write_bytes(
            os.path.join(qdir, "last_message.bin"), outcome.raw_last_message
        )
    return qdir


def _run_peer(
    cfg: Config,
    peer: str,
    *,
    prompt: str,
    schema: dict[str, Any],
    schema_file: str,
    peer_session_id: str | None,
    resume: bool,
    workspace: str,
    attempt_dir: str,
) -> PeerOutcome:
    if peer == "claude":
        session_id = peer_session_id or claude_backend.new_session_id()
        return claude_backend.run_consultation(
            cfg, prompt=prompt, schema=schema, session_id=session_id,
            resume=resume, workspace=workspace, attempt_dir=attempt_dir,
        )
    return codex_backend.run_consultation(
        cfg, prompt=prompt, schema_file=schema_file,
        thread_id=peer_session_id if resume else None,
        workspace=workspace, attempt_dir=attempt_dir,
    )


def execute(job_dir: str) -> int:
    store.set_umask()
    request = store.read_json_atomic(os.path.join(job_dir, "request.json"))
    cfg = load_config(request.get("config_path") or None)
    job_id = request["job_id"]
    conversation_id = request["conversation_id"]
    peer = request["peer"]
    caller = request["caller"]
    resume = bool(request["resume"])

    # The `running` write is load-bearing, so its success must be checked
    # rather than assumed. Terminal documents are immutable, so if this job was
    # already reconciled to failed while it sat queued, this write is a silent
    # no-op and NO pid is published. The ownership handshake below would then
    # still pass, a rival would see a terminal job and steal the claim, and two
    # workers would reach the same peer session: exactly the gap the handshake
    # exists to close, reopened by the immutability rule it depends on.
    published = registry.write_status(
        cfg, job_id, "running", worker_pid=os.getpid(), started_at=store.utc_now(),
        conversation_id=conversation_id, peer=peer, caller=caller, attempts=0,
    )
    if published.get("status") != "running" or published.get("worker_pid") != os.getpid():
        store.atomic_write_json(os.path.join(job_dir, "abandoned.json"), {
            "reason": ErrorCategory.JOB_ALREADY_TERMINAL.value,
            "observed_status": published.get("status"),
            "observed_error_category": published.get("error_category"),
            "worker_pid": os.getpid(),
            "abandoned_at": store.utc_now(),
            "peer_contacted": False,
        })
        store.append_ledger(cfg.ledger_path, {
            "record_type": "worker_abandoned",
            "job_id": job_id, "conversation_id": conversation_id,
            "caller": caller, "peer": peer,
            "error_category": ErrorCategory.JOB_ALREADY_TERMINAL.value,
            "peer_contacted": False,
            "abandoned_at": store.utc_now(),
            "implementation": provenance.collect(cfg.repo_root),
        })
        # Give up the claim if we still hold it. No peer call is made.
        try:
            registry.release_conversation_slot(cfg, conversation_id, job_id)
        except BrokerError:
            pass
        return 1

    started_at = store.utc_now()
    ownership_verified = False
    impl = provenance.collect(cfg.repo_root)
    attempts_log: list[dict[str, Any]] = []
    retries_exhausted = False
    schema_sha: str | None = None
    peer_info: dict[str, Any] = {}
    final: PeerOutcome | None = None
    category = ErrorCategory.INTERNAL_ERROR
    validated: dict[str, Any] | None = None
    violations: list[str] = []

    try:
        schema, schema_sha = cfg.load_schema()
        peer_info = preflight.check_peer(cfg, peer)
        workspace = store.secure_mkdir(cfg.workspace(peer, conversation_id))
        preflight.assert_workspace_clean(
            workspace, record_to=cfg.state("last-contamination.json"))
        schema_file = os.path.join(job_dir, "contract.schema.json")
        store.atomic_write_bytes(
            schema_file, json.dumps(schema, indent=2, sort_keys=True).encode("utf-8")
        )

        # Ownership handshake. The pid is already published above, so a rival
        # now sees a live process and cannot take the slot; and if a rival took
        # it while this worker was starting, this raises here, before any peer
        # call is made. Nothing below this line may run without the slot.
        registry.assert_conversation_ownership(cfg, conversation_id, job_id)
        ownership_verified = True

        conversation = registry.load_conversation(cfg, conversation_id)
        peer_session_id = conversation.get("peer_session_id")
        if resume and not peer_session_id:
            raise BrokerError(ErrorCategory.PEER_SESSION_ID_MISSING)

        prompt_text = (
            envelope.build_continuation(request["prompt"], cfg.contract_version)
            if resume
            else envelope.build_initial(request["prompt"], schema, cfg.contract_version)
        )

        for attempt in range(1, MAX_ATTEMPTS + 1):
            attempt_dir = store.secure_mkdir(os.path.join(job_dir, "attempts", str(attempt)))
            outcome = _run_peer(
                cfg, peer, prompt=prompt_text, schema=schema, schema_file=schema_file,
                peer_session_id=peer_session_id, resume=resume,
                workspace=workspace, attempt_dir=attempt_dir,
            )
            final = outcome
            category = outcome.category
            violations = []
            store.atomic_write_json(os.path.join(attempt_dir, "argv.json"), outcome.argv)
            if outcome.raw_stderr:
                store.atomic_write_bytes(
                    os.path.join(attempt_dir, "stderr.bin"), outcome.raw_stderr
                )

            if category is ErrorCategory.OK and isinstance(outcome.payload, dict):
                # Version first: a peer answering a different contract version
                # will not be fixed by a corrective retry, so it must not spend
                # one. Only then is the payload validated against the schema.
                if outcome.payload.get("contract_version") != cfg.contract_version:
                    category = ErrorCategory.PEER_CONTRACT_VERSION_MISMATCH
                else:
                    violations = schema_validate.validate(outcome.payload, schema)
                    if violations:
                        category = ErrorCategory.PEER_OUTPUT_SCHEMA_INVALID
                    else:
                        validated = outcome.payload

            attempts_log.append({
                "attempt": attempt,
                "prompt_sha256": store.sha256_text(prompt_text),
                "prompt_chars": len(prompt_text),
                "category": category.value,
                "returncode": outcome.returncode,
                "duration_seconds": round(outcome.duration_seconds, 3),
                "timed_out": outcome.timed_out,
                "group_kill": outcome.group_kill,
                "extraction_path": outcome.extraction_path,
                "schema_violation_count": len(violations),
                "schema_violations": violations[:20],
                "peer_session_id": outcome.peer_session_id,
                "notes": outcome.notes,
                "argv_shape": argv_shape(outcome.argv),
            })

            if validated is not None:
                category = ErrorCategory.OK
                break
            if attempt >= MAX_ATTEMPTS:
                # Keep the substantive category. "Retry budget exhausted" tells
                # the caller nothing actionable and, worse, its hint does not
                # mention the quarantine that actually happened.
                if category in CORRECTIVE or category in TRANSIENT:
                    retries_exhausted = True
                break
            if category in DETERMINISTIC:
                break  # never retried, by rule
            if category in CORRECTIVE:
                _quarantine(cfg, job_dir, attempt, outcome)
                # The corrective prompt is deliberately built only from an error
                # code plus schema metadata, never from quarantined text. That
                # works ONLY because the retry resumes the same peer session,
                # which still holds the original question. With no session to
                # resume, attempt two would open a fresh one whose entire prompt
                # is "your previous reply did not satisfy the contract", asking
                # about a conversation the peer has never seen. A schema-valid
                # but contentless answer could then be committed as the turn.
                if not outcome.peer_session_id:
                    break
                prompt_text = envelope.build_corrective(
                    category.value, violations, schema, cfg.contract_version
                )
                peer_session_id = outcome.peer_session_id
                resume = True
                continue
            if category in TRANSIENT:
                continue  # one plain retry, same prompt
            break

        # Quarantine whenever a run produced output that was not usable, not
        # only when it parsed into an object. The error hint tells the caller
        # the output was quarantined, so it has to actually be there.
        if validated is None and final is not None and (
            final.raw_stdout or final.raw_stderr or final.raw_last_message
        ):
            _quarantine(cfg, job_dir, len(attempts_log), final)

    except BrokerError as exc:
        category = exc.category
    except Exception:  # noqa: BLE001 - worker must always land somewhere
        category = ErrorCategory.INTERNAL_ERROR
        store.atomic_write_bytes(
            os.path.join(job_dir, "worker_traceback.txt"),
            traceback.format_exc().encode("utf-8"),
        )

    # ---- terminal state -------------------------------------------------
    if category is ErrorCategory.OK:
        status = "complete"
    elif category is ErrorCategory.PEER_TIMEOUT:
        status = "timed_out"
    else:
        status = "failed"

    finished_at = store.utc_now()
    peer_session_id_final = final.peer_session_id if final else None
    record: dict[str, Any] = {
        "job_id": job_id,
        "conversation_id": conversation_id,
        "caller": caller,
        "peer": peer,
        "source_classification": request["source_classification"],
        "label": request.get("label"),
        "resume": bool(request["resume"]),
        "status": status,
        "error_category": category.value,
        "retries_exhausted": retries_exhausted,
        "started_at": started_at,
        "finished_at": finished_at,
        "contract_version": cfg.contract_version,
        "contract_schema_sha256": schema_sha,
        "config_snapshot_sha256": (
            store.sha256_file(os.path.join(job_dir, "config.snapshot.json"))
            if os.path.isfile(os.path.join(job_dir, "config.snapshot.json")) else None
        ),
        "prompt_sha256": store.sha256_text(request["prompt"]),
        "prompt_chars": len(request["prompt"]),
        "response_sha256": (
            store.sha256_text(json.dumps(validated, sort_keys=True)) if validated else None
        ),
        "peer_session_id": peer_session_id_final,
        "peer_executable": peer_info.get("executable"),
        "peer_executable_realpath": peer_info.get("realpath"),
        "peer_auth_failure_reason": (final.notes.get("auth_failure_reason") if final else None),
        "peer_observed_version": peer_info.get("observed_version"),
        "peer_requested_model": cfg.peer(peer).get("model"),
        # Recorded whether set or not. A null here means "the CLI's own
        # default was used", which is a fact about the consultation and not an
        # absence of one.
        "peer_requested_reasoning_effort": cfg.peer_reasoning_effort(peer),
        "peer_observed_model": final.observed_model if final else None,
        "peer_observed_models": (final.observed_models if final else []) or None,
        # Whether the peer REPORTED these, as opposed to whether we captured
        # them. A null model with reported=false is the CLI's silence; a null
        # with reported=true would be a bug here.
        "peer_model_reported": (
            (final.notes or {}).get("model_usage_present") if final else None),
        "peer_cost_reported": (
            (final.notes or {}).get("total_cost_present") if final else None),
        "peer_requested_alias_in_observed": (
            (final.notes or {}).get("requested_alias_in_observed_model")
            if final else None),
        "peer_cost_usd": final.cost_usd if final else None,
        "attempts": attempts_log,
        "attempt_count": len(attempts_log),
        "implementation": impl,
    }
    store.atomic_write_json(os.path.join(job_dir, "provenance.json"), record)
    if validated is not None:
        store.atomic_write_json(os.path.join(job_dir, "result.json"), {
            "peer": peer,
            "peer_identity": f"{peer} ({record['peer_observed_version']})",
            "conversation_id": conversation_id,
            "response": validated,
        })
    # One locked pass: increment the turn counter, persist the peer session id,
    # and clear this job's claim on the conversation. The increment happens
    # inside the lock that guards its own read, so a concurrent update cannot
    # lose it and let the turn limit be exceeded.
    commit_succeeded = False
    try:
        changes: dict[str, Any] = {}
        if peer_session_id_final and ownership_verified:
            changes["peer_session_id"] = peer_session_id_final
            changes["increment_turns"] = True
        registry.release_conversation_slot(cfg, conversation_id, job_id, **changes)
        commit_succeeded = True
    except BrokerError as exc:
        # Only "not the owner" is expected here: a rival holds the slot and is
        # responsible for it. Anything else (a missing or damaged conversation
        # record) is a real problem and must not be hidden.
        if exc.category is not ErrorCategory.CONVERSATION_NOT_OWNED:
            store.atomic_write_bytes(
                os.path.join(job_dir, "release_error.txt"),
                exc.category.value.encode("utf-8"))
    # Retirement is gated on the release actually SUCCEEDING, not merely on
    # having been attempted. Source order alone is not the invariant: if the
    # release was refused, for instance because a rival owns the conversation,
    # then from the conversation's point of view this attempt never committed,
    # and marking its evidence committed would be a lie in the one direction
    # that matters.
    if not commit_succeeded:
        record["markers_retained"] = "conversation release did not succeed"
    store.append_ledger(cfg.ledger_path, record)
    if commit_succeeded:
        retired = registry.retire_attempt_markers(cfg, job_id)
        if retired:
            runner.pause_after_marker_transition(cfg.peer_extra_env(peer), "committed")
    # Existing MCP processes cache their error enum. Keep a category they
    # understand on disk; newer readers promote only this closed diagnostic.
    # Retry policy and provenance retain the specific deterministic category.
    compatible_category = (ErrorCategory.PEER_OUTPUT_SCHEMA_INVALID
                            if category is ErrorCategory.PEER_STRUCTURED_OUTPUT_EXHAUSTED
                            else category)
    registry.write_status(
        cfg, job_id, status, error_category=compatible_category.value,
        diagnostic_category=(category.value if compatible_category is not category else None),
        retries_exhausted=retries_exhausted,
        finished_at=finished_at, attempts=len(attempts_log),
        conversation_id=conversation_id, peer=peer, caller=caller,
        worker_pid=None,
    )
    return 0 if status == "complete" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-bridge-worker")
    parser.add_argument("--job-dir", required=True)
    args = parser.parse_args(argv)
    return execute(args.job_dir)


if __name__ == "__main__":
    sys.exit(main())
