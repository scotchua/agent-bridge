"""MCP tool definitions for durable routing and local work intake."""

from __future__ import annotations

from typing import Any, Callable

from ..capacity_router import CapacityObservation, RoutingError, StageRouter
from ..localq.intake import AutomaticIntake
from ..localq.spool import AdmissionError, JobNotFound, LocalQueue
from . import autodecide, gate
from .execution_queue import ExecutionAdmissionError, ExecutionQueue


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False,
            "properties": properties, "required": required}


def build_tools(caller: str, router: StageRouter, queue: LocalQueue,
                intake: AutomaticIntake,
                execution: ExecutionQueue | None = None,
                state_root: str | None = None) -> dict[str, dict[str, Any]]:
    """Build one caller-bound tool set.

    Caller provenance is injected here and is intentionally absent from the
    schemas. A Claude-mode client cannot submit a receipt claiming Codex made
    the request, or vice versa.
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
    observe = _schema({
        "route": {"type": "string", "enum": ["claude", "codex", "local"]},
        "observed_at": {"type": "number"}, "fresh_until": {"type": "number"},
        "available": {"type": "boolean"}, "source": {"type": "string"},
    }, ["route", "observed_at", "fresh_until", "available", "source"])
    empty = _schema({}, [])

    def route_local(args: dict[str, Any]) -> dict[str, Any]:
        return call(intake.route, {"params": None, "risk_flags": [], **args, "caller": caller})

    def observe_capacity(args: dict[str, Any]) -> dict[str, Any]:
        observation = CapacityObservation(**args)
        return call(router.observe_capacity, {"observation": observation})

    tools = {
        "work_route_local": {
            "description": "Route substantial declared non-client mechanical work to the local worker or refuse it. Never falls back to cloud.",
            "inputSchema": route, "handler": route_local,
        },
        "work_status": {
            "description": "Read local worker job status without reading its result.",
            "inputSchema": job, "handler": lambda args: call(queue.status, args),
        },
        "work_result": {
            "description": "Read a terminal local draft and disposition. The draft remains untrusted until reviewed.",
            "inputSchema": job, "handler": lambda args: call(queue.result, args),
        },
        "work_feedback": {
            "description": "Record one immutable usefulness outcome for completed production work.",
            "inputSchema": feedback, "handler": lambda args: call(queue.feedback, args),
        },
        "capacity_observe": {
            "description": "Record a time-bounded capacity observation from a supported source. It grants no new authority or data route.",
            "inputSchema": observe, "handler": observe_capacity,
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
                    autodecide.clear_intent(state_root, args["repo"], clock=router.clock)
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
