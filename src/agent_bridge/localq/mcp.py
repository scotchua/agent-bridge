"""Transport adapter for local queue tools, kept separate from peer tools."""

from __future__ import annotations

from typing import Any, Callable

from .spool import AdmissionError, JobNotFound, LocalQueue
from .intake import AutomaticIntake


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "properties": properties, "required": required}


def build_tools(queue: LocalQueue) -> dict[str, dict[str, Any]]:
    """Return the additive MCP schemas and handlers for one queue instance."""
    submit = _schema({
        "task_type": {"type": "string", "enum": ["summarize", "extract", "checklist", "log_triage", "test_draft"]},
        "input": {"type": "string", "maxLength": 24000}, "params": {"type": "object"},
        "priority": {"type": "string", "enum": ["interactive", "bulk"]},
        "classification": {"type": "string", "enum": ["synthetic", "public", "internal_nonclient"]},
        "caller": {"type": "string", "enum": ["codex", "claude"]},
        "purpose": {"type": "string", "enum": ["work", "test"]},
        "idempotency_key": {"type": "string"},
    }, ["task_type", "input", "priority", "classification", "caller", "purpose"])
    job = _schema({"job_id": {"type": "string"}}, ["job_id"])
    feedback = _schema({"job_id": {"type": "string"},
                        "outcome": {"type": "string", "enum": ["used", "reworked", "discarded"]}},
                       ["job_id", "outcome"])

    def call(method: Callable[..., dict[str, Any]], args: dict[str, Any]) -> dict[str, Any]:
        try:
            return {"ok": True, **method(**args)}
        except (AdmissionError, JobNotFound, TypeError) as exc:
            return {"ok": False, "error": str(exc) or type(exc).__name__}

    return {
        "local_submit": {"description": "Submit admitted mechanical work to the local durable queue. No alternate route is used.", "inputSchema": submit,
                         "handler": lambda args: call(queue.submit, args)},
        "local_status": {"description": "Read local job status and metadata.", "inputSchema": job,
                         "handler": lambda args: call(queue.status, args)},
        "local_result": {"description": "Read a terminal local job result and disposition.", "inputSchema": job,
                         "handler": lambda args: call(queue.result, args)},
        "local_cancel": {"description": "Cancel queued local work or request cancellation of running local work.", "inputSchema": job,
                         "handler": lambda args: call(queue.cancel, args)},
        "local_feedback": {"description": "Record one immutable usefulness outcome for completed work. Test jobs are not eligible.", "inputSchema": feedback,
                           "handler": lambda args: call(queue.feedback, args)},
    }


def build_intake_tools(intake: AutomaticIntake) -> dict[str, dict[str, Any]]:
    """Return automatic-routing tools without changing the explicit queue API."""
    route = _schema({
        "task_type": {"type": "string"},
        "input": {"type": "string", "maxLength": 24000},
        "params": {"type": "object"},
        "priority": {"type": "string", "enum": ["interactive", "bulk"]},
        "classification": {"type": "string"},
        "caller": {"type": "string", "enum": ["codex", "claude"]},
        "purpose": {"type": "string", "enum": ["work", "test"]},
        "risk_flags": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
        "idempotency_key": {"type": "string"},
        # AutomaticIntake.route now requires a checkpoint for every
        # purpose="work" call (purpose="test" -- internal direct
        # test/calibration code -- may still omit one). This standalone,
        # non-caller-bound transport is a lower-level surface than
        # orchestration's work_route_local, which mints and consumes its
        # own checkpoint automatically; a caller here supplies one it
        # already minted (a future local_checkpoint tool, or a test).
        "checkpoint_id": {"type": "string"},
    }, ["task_type", "input", "priority", "classification", "caller", "purpose"])
    receipt = _schema({"receipt_id": {"type": "string"}}, ["receipt_id"])

    def call(method: Callable[..., dict[str, Any]], args: dict[str, Any]) -> dict[str, Any]:
        try:
            return {"ok": True, **method(**args)}
        except (AdmissionError, JobNotFound, TypeError) as exc:
            return {"ok": False, "error": str(exc) or type(exc).__name__}

    return {
        "local_route": {
            "description": "Automatically route substantial, declared non-client mechanical work locally or refuse it. Never falls back to cloud.",
            "inputSchema": route,
            "handler": lambda args: call(intake.route, {"params": None, "risk_flags": [], **args}),
        },
        "local_route_receipt": {
            "description": "Read an immutable local-routing decision receipt.",
            "inputSchema": receipt,
            "handler": lambda args: call(intake.receipt, args),
        },
    }
