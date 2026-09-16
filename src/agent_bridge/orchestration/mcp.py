"""MCP tool definitions for durable routing and local work intake.

There is deliberately no tool here for recording capacity.  There used to be
``capacity_observe``, and an adversarial review was right about it: the
assistant chose the route, the availability, the source string and the
freshness window, so "a fresh observation from an authorized source" meant
whatever the model typed.  Capacity now has exactly two writers, neither of
them model-facing: the gate hook, which records that the client calling it is
running, and the operator's ``routing-policy.json``, which is a file only the
operator edits.  See :mod:`agent_bridge.orchestration.autodecide`.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any, Callable

from ..capacity_router import RoutingError, StageRouter
from ..localq.intake import AutomaticIntake
from ..localq.spool import AdmissionError, JobNotFound, LocalQueue
from . import autodecide, autoroute, gate, localfirst
from .execution_queue import ExecutionAdmissionError, ExecutionQueue


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False,
            "properties": properties, "required": required}


def build_tools(caller: str, router: StageRouter, queue: LocalQueue,
                intake: AutomaticIntake,
                execution: ExecutionQueue | None = None,
                state_root: str | None = None,
                protected: tuple[str, ...] = ()) -> dict[str, dict[str, Any]]:
    """Build one caller-bound tool set.

    Caller provenance is injected here and is intentionally absent from the
    schemas. A Claude-mode client cannot submit a receipt claiming Codex made
    the request, or vice versa.

    ``protected``, when given, is the same tuple ``gate.protected_paths``
    computes for the delegation-first hook: the gate's own state, the stage
    router's database, the local queue's database and heartbeat, and the
    hook installation files. ``work_digest_file`` refuses a target under any
    of them by the same reasoning the hook already refuses a write there.
    """
    if caller not in {"claude", "codex"}:
        raise ValueError("caller_invalid")

    def call(method: Callable[..., dict[str, Any] | None], args: dict[str, Any]) -> dict[str, Any]:
        try:
            value = method(**args)
            return {"ok": True, **(value or {})}
        except (AdmissionError, ExecutionAdmissionError, JobNotFound,
                RoutingError, TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc) or type(exc).__name__}

    route = _schema({
        "task_type": {"type": "string"},
        "input": {"type": "string", "maxLength": 24000},
        "params": {"type": "object"},
        "priority": {"type": "string", "enum": ["interactive", "bulk"]},
        "classification": {"type": "string"},
        "purpose": {"type": "string", "enum": ["work", "test"]},
        "risk_flags": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
        "idempotency_key": {"type": "string", "maxLength": 256},
    }, ["task_type", "input", "priority", "classification", "purpose"])
    job = _schema({"job_id": {"type": "string"}}, ["job_id"])
    feedback = _schema({
        "job_id": {"type": "string"},
        "outcome": {"type": "string", "enum": ["used", "reworked", "discarded"]},
    }, ["job_id", "outcome"])
    identity = {
        "item_id": {"type": "string"}, "stage": {"type": "string"},
    }
    register = _schema({
        **identity,
        "allowed_routes": {"type": "array", "items": {"type": "string", "enum": ["claude", "codex", "local"]}, "uniqueItems": True},
        "preferred_routes": {"type": "array", "items": {"type": "string", "enum": ["claude", "codex", "local"]}, "uniqueItems": True},
        "is_review": {"type": "boolean"},
        "author_owner": {"type": "string"},
        "author_route": {"type": "string", "enum": ["claude", "codex", "local"]},
    }, ["item_id", "stage", "allowed_routes"])
    claim = _schema({
        **identity, "owner_id": {"type": "string"},
        "lease_seconds": {"type": "number", "exclusiveMinimum": 0},
        "expected_revision": {"type": "integer", "minimum": 0},
    }, ["item_id", "stage", "owner_id", "lease_seconds", "expected_revision"])
    complete = _schema({
        **identity, "owner_id": {"type": "string"},
        "expected_revision": {"type": "integer", "minimum": 0},
    }, ["item_id", "stage", "owner_id", "expected_revision"])
    status = _schema(identity, ["item_id", "stage"])
    empty = _schema({}, [])

    def route_local(args: dict[str, Any]) -> dict[str, Any]:
        # This call retires no dispatch intent, and cannot: it carries no
        # repository, item or stage, so there is nothing to match an intent
        # against, and retiring one on the strength of "some local work
        # happened" is exactly the unbound cleanup ``clear_intent`` now
        # refuses. It is also not currently reachable: the gate infers
        # ``implementation`` from every tool call, because a local worker does
        # not edit files, so no local intent is ever written. Anyone adding
        # one has to add the stage identity to this schema first.
        return call(intake.route, {"params": None, "risk_flags": [], **args, "caller": caller})

    digest = _schema({
        "path": {"type": "string", "description": "Absolute path to the file to digest."},
        "task_type": {"type": "string", "enum": list(localfirst.DIGEST_TASK_TYPES)},
        "offset": {"type": "integer", "minimum": 0,
                  "description": "Byte offset to start the window at. Defaults to the tail. "
                                 "The window length is fixed and not the caller's to choose; "
                                 "an offset that would shorten it is refused."},
        "fields": {"type": "array", "items": {"type": "string"}, "maxItems": localfirst.MAX_EXTRACT_FIELDS,
                  "description": "extract only: field names to return, null where absent."},
        "priority": {"type": "string", "enum": ["interactive", "bulk"]},
        "idempotency_key": {"type": "string", "maxLength": 256},
    }, ["path", "task_type"])

    def _under_any(path: str, roots: "tuple[str, ...]") -> bool:
        real = os.path.realpath(path)
        for root in roots:
            if not root:
                continue
            root_real = os.path.realpath(root)
            if real == root_real or real.startswith(root_real.rstrip(os.sep) + os.sep):
                return True
        return False

    def digest_file(args: dict[str, Any]) -> dict[str, Any]:
        """Satisfy a digest intent, or run a digest on the assistant's own
        initiative for a file the operator's policy already makes eligible.
        Refuses by name at every step (design section 2.2); no assistant-
        supplied classification or instruction ever reaches the local model.
        """
        if state_root is None:
            return {"ok": False, "error": "local_first_unavailable"}
        path = args.get("path")
        task_type = args.get("task_type")
        offset_arg = args.get("offset")
        fields = tuple(args.get("fields") or ())
        priority = args.get("priority", "interactive")
        idempotency_key = args.get("idempotency_key")
        if (not isinstance(path, str) or not path or not os.path.isabs(path)
                or task_type not in localfirst.DIGEST_TASK_TYPES):
            return {"ok": False, "error": "digest_path_refused:input_invalid"}
        if os.path.islink(path):
            return {"ok": False, "error": "digest_path_refused:symlink"}
        try:
            if not os.path.isfile(path):
                return {"ok": False, "error": "digest_path_refused:not_a_regular_file"}
        except OSError as exc:
            return {"ok": False, "error": f"digest_path_refused:{type(exc).__name__}"}
        real_path = os.path.realpath(path)
        local_queue_root = str(getattr(queue, "root", "") or "")
        if _under_any(real_path, (state_root, local_queue_root, *protected)):
            return {"ok": False, "error": "digest_path_refused:protected_or_state_path"}
        repo_root = gate.repo_key(real_path)
        if repo_root is None:
            return {"ok": False, "error": "digest_path_refused:not_in_a_repository"}
        try:
            policy = autoroute.load_policy(state_root)
        except autoroute.PolicyError as exc:
            return {"ok": False, "error": f"digest_path_refused:policy_unreadable:{type(exc).__name__}"}
        if not policy.local_first.enabled:
            return {"ok": False, "error": "digest_path_refused:local_first_disabled"}
        repo_policy = policy.for_repo(repo_root)
        if not repo_policy.mechanical_ok or repo_policy.classification not in autoroute.LOCAL_CLASSIFICATIONS:
            return {"ok": False, "error": "digest_path_refused:repo_not_mechanical_ok"}
        globs = localfirst.effective_globs(repo_policy, policy.local_first)
        if not localfirst.matches_any(real_path, repo_root, globs):
            return {"ok": False, "error": "digest_path_refused:no_glob_match"}
        try:
            stat_result = os.stat(real_path)
        except OSError as exc:
            return {"ok": False, "error": f"digest_path_refused:{type(exc).__name__}"}
        size, mtime_ns = stat_result.st_size, stat_result.st_mtime_ns
        try:
            computed_offset, window_bytes = localfirst.digest_window(size, offset=offset_arg)
        except localfirst.WindowError as exc:
            return {"ok": False, "error": f"digest_window_refused:{exc}"}
        try:
            instruction = localfirst.render_instruction(
                task_type, max_chars=policy.local_first.digest_max_output_chars, fields=fields)
        except localfirst.TemplateError as exc:
            return {"ok": False, "error": f"digest_task_refused:{exc}"}
        try:
            text, decode_replacements = localfirst.read_window(real_path, computed_offset, window_bytes)
        except localfirst.WindowReadError as exc:
            return {"ok": False, "error": f"digest_read_refused:{exc}"}
        window_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        idem = idempotency_key or f"digest:{caller}:{task_type}:{window_sha256}"
        try:
            intake_result = intake.route(
                task_type=task_type, input=text, params={"instruction": instruction},
                priority=priority, classification=repo_policy.classification, caller=caller,
                purpose="work", idempotency_key=idem)
        except (AdmissionError, TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc) or type(exc).__name__}
        if intake_result.get("decision") != "local":
            # A normal outcome, not an error: the intake refused (too small,
            # a prohibited flag), and there is nothing further to record.
            return {"ok": True, **intake_result}
        job_id = intake_result.get("job_id")
        receipt = localfirst.write_digest_receipt(
            state_root, path=real_path, repo=repo_root, size=size, mtime_ns=mtime_ns,
            offset=computed_offset, window_bytes=window_bytes, window_sha256=window_sha256,
            decode_replacements=decode_replacements, task_type=task_type,
            classification=repo_policy.classification, caller=caller,
            job_id=job_id or "", intake_receipt_id=intake_result.get("receipt_id", ""))
        if job_id:
            localfirst.record_digest_job(
                state_root, job_id, max_output_chars=policy.local_first.digest_max_output_chars)
        try:
            localfirst.retire_digest_intent(
                state_root, real_path, binding={"path": real_path, "size": size, "mtime_ns": mtime_ns})
        except OSError:
            # The digest itself already succeeded; a tidy-up failure is not
            # a reason to report this call as failed.
            pass
        return {"ok": True, "job_id": job_id, "receipt_id": intake_result.get("receipt_id"),
                "deduplicated": intake_result.get("deduplicated", False),
                "digest_receipt_created_at": receipt["created_at"],
                "window": {"offset": computed_offset, "bytes": window_bytes, "sha256": window_sha256},
                "classification": repo_policy.classification, "next_call": "work_result"}

    def work_result(args: dict[str, Any]) -> dict[str, Any]:
        result = call(queue.result, args)
        if state_root and result.get("ok") and result.get("ready"):
            job_id = args.get("job_id", "")
            max_chars = localfirst.digest_job_max_output_chars(state_root, job_id)
            if max_chars is not None:
                payload = result.get("result")
                output = payload.get("output") if isinstance(payload, dict) else None
                text = output.get("text") if isinstance(output, dict) else None
                if isinstance(text, str):
                    char_count = len(text)
                    truncated = char_count > max_chars
                    if truncated:
                        output["text"] = text[:max_chars]
                    output["output_char_count"] = char_count
                    output["output_truncated"] = truncated
        return result

    tools = {
        "work_route_local": {
            "description": "Route substantial declared non-client mechanical work to the local worker or refuse it. Never falls back to cloud.",
            "inputSchema": route, "handler": route_local,
        },
        "work_digest_file": {
            "description": ("Digest a mechanical artifact (a log or captured test output) the "
                            "operator's policy has marked eligible, before reading it in full. "
                            "The classification and the instruction are the operator's policy and "
                            "a fixed template, never the caller's; satisfies a delegation-first "
                            "gate local_digest_required denial or runs ahead of one."),
            "inputSchema": digest, "handler": digest_file,
        },
        "work_status": {
            "description": "Read local worker job status without reading its result.",
            "inputSchema": job, "handler": lambda args: call(queue.status, args),
        },
        "work_result": {
            "description": "Read a terminal local draft and disposition. The draft remains untrusted until reviewed. A digest job's output is truncated to the policy's digest_max_output_chars, with output_truncated set.",
            "inputSchema": job, "handler": work_result,
        },
        "work_feedback": {
            "description": "Record one immutable usefulness outcome for completed production work.",
            "inputSchema": feedback, "handler": lambda args: call(queue.feedback, args),
        },
        "stage_register": {
            "description": "Register one durable stage and its permitted routes before claiming it.",
            "inputSchema": register,
            "handler": lambda args: call(router.register, {
                "preferred_routes": None, "is_review": False,
                "author_owner": None, "author_route": None, **args}),
        },
        "stage_claim": {
            "description": "Claim one registered stage on the first fresh eligible route. Paid fallback is never available.",
            "inputSchema": claim,
            "handler": lambda args: call(router.assign, {**args, "paid_fallback": False}),
        },
        "stage_renew": {
            "description": "Renew a stage lease only for its current owner and revision.",
            "inputSchema": claim,
            "handler": lambda args: call(router.renew, args),
        },
        "stage_complete": {
            "description": "Mark an owned stage complete using the current owner and revision.",
            "inputSchema": complete,
            "handler": lambda args: call(router.complete, args),
        },
        "stage_status": {
            "description": "Read one stage's ownership, route, state and revision.",
            "inputSchema": status, "handler": lambda args: call(router.get, args),
        },
        "orchestration_status": {
            "description": "Read aggregate stage, capacity and local queue status. Contains no task content.",
            "inputSchema": empty,
            "handler": lambda args: {"ok": True, "caller": caller,
                                      "stages": router.report(),
                                      "local_queue": queue.state_report(),
                                      "execution_queue": execution.state_report() if execution else {"disabled": 1}},
        },
    }
    decide = _schema({
        **identity, "owner_id": {"type": "string"},
        "stage_revision": {"type": "integer", "minimum": 0},
        "repo": {"type": "string"},
        "reason": {"type": "string", "maxLength": gate.MAX_REASON},
        "ttl_seconds": {"type": "integer", "minimum": gate.MIN_TTL_SECONDS,
                        "maximum": gate.MAX_TTL_SECONDS},
    }, ["item_id", "stage", "owner_id", "stage_revision", "repo", "reason"])

    def decide_routing(args: dict[str, Any]) -> dict[str, Any]:
        if state_root is None:
            return {"ok": False, "error": "routing_receipts_unavailable"}
        try:
            current = router.get(args.get("item_id"), args.get("stage"))
            if (current["state"] != "owned" or current["owner_id"] != args.get("owner_id")
                    or current["revision"] != args.get("stage_revision")):
                raise RoutingError("execution_stage_binding_invalid")
            receipt = gate.record_decision(
                state_root, caller=caller, stage_record=current, repo=args.get("repo"),
                reason=args.get("reason"), ttl_seconds=args.get("ttl_seconds", 4 * 3600),
                clock=router.clock)
            return {"ok": True, "receipt": receipt}
        except (RoutingError, TypeError, ValueError, OSError) as exc:
            return {"ok": False, "error": str(exc) or type(exc).__name__}

    tools["routing_decide"] = {
        "description": ("Record the routing decision for an owned stage as a durable receipt for "
                        "one repository. The delegation-first gate lets a client edit that "
                        "repository only while a fresh receipt names its route. Requires the "
                        "same stage binding as execution_dispatch."),
        "inputSchema": decide, "handler": decide_routing,
    }
    if execution is not None:
        dispatch = _schema({
            "provider": {"type": "string", "enum": ["claude", "codex"]},
            "repo": {"type": "string"}, "brief": {"type": "string"},
            "base": {"type": "string"}, "classification": {"type": "string"},
            "model": {"type": "string"}, "effort": {"type": "string"},
            "verify_argv": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 7200},
            "paid_fallback": {"type": "boolean"},
            "idempotency_key": {"type": "string", "maxLength": 256},
            "item_id": {"type": "string"}, "stage": {"type": "string"},
            "owner_id": {"type": "string"},
            "stage_revision": {"type": "integer", "minimum": 0},
        }, ["provider", "repo", "brief", "base", "classification", "model", "effort",
            "item_id", "stage", "owner_id", "stage_revision"])

        def dispatch_execution(args: dict[str, Any]) -> dict[str, Any]:
            try:
                current = router.get(args.get("item_id"), args.get("stage"))
                if (current["state"] != "owned" or current["owner_id"] != args.get("owner_id")
                        or current["owner_route"] != args.get("provider")
                        or current["revision"] != args.get("stage_revision")):
                    raise RoutingError("execution_stage_binding_invalid")
                result = call(execution.submit, {
                    "verify_argv": None, "timeout_seconds": 900,
                    "paid_fallback": False, "idempotency_key": None,
                    **args, "caller": caller})
            except (RoutingError, TypeError, ValueError) as exc:
                return {"ok": False, "error": str(exc) or type(exc).__name__}
            if result.get("ok") and state_root is not None:
                # The gate wrote a dispatch intent when it routed this
                # repository away from its caller. The job now exists, so the
                # intent is met: retiring it here is what keeps the audit's
                # "routed but never dispatched" column meaningful instead of
                # every satisfied intent sitting in it forever.
                try:
                    # Bound to this exact job. Keyed on the repository alone,
                    # any owned stage in it could retire the intent for the
                    # stage that was actually refused, and the audit's
                    # "routed but never dispatched" column would record the
                    # unhonoured routing as met.
                    autodecide.clear_intent(
                        state_root, args["repo"], clock=router.clock,
                        binding={"route": current["owner_route"],
                                 "item_id": current["item_id"],
                                 "stage": current["stage"],
                                 "owner_id": current["owner_id"],
                                 "stage_revision": current["revision"]})
                except (OSError, ValueError, KeyError):
                    # A job was accepted. Failing the dispatch because its
                    # bookkeeping could not be tidied would be the worse
                    # outcome; the audit reports the stale intent instead.
                    pass
            return result

        tools.update({
            "execution_dispatch": {
                "description": "Queue implementation on the opposite provider's bounded subscription harness. No paid fallback, apply, commit, push or merge.",
                "inputSchema": dispatch,
                "handler": dispatch_execution,
            },
            "execution_status": {
                "description": "Read durable execution state without task or model output.",
                "inputSchema": job,
                "handler": lambda args: call(execution.status, args),
            },
            "execution_result": {
                "description": "Read a terminal harness receipt. Returned patches remain unapplied and require review.",
                "inputSchema": job,
                "handler": lambda args: call(execution.result, args),
            },
        })
    return tools
