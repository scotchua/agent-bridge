"""Broker-owned failure receipts, separate from the peer response contract."""

from __future__ import annotations

from typing import Any

from . import store
from .config import Config, effective_config_sha256
from .errors import CORRECTIVE, ErrorCategory

CATEGORIES = CORRECTIVE | {ErrorCategory.PEER_TIMEOUT, ErrorCategory.PEER_AUTH_FAILURE,
                          ErrorCategory.PEER_STRUCTURED_OUTPUT_EXHAUSTED}


def build(cfg: Config, peer: str, prompt: str, category: ErrorCategory,
          attempts: list[dict[str, Any]], stages: list[dict[str, Any]],
          elapsed: float) -> dict[str, Any]:
    if category is ErrorCategory.PEER_TIMEOUT:
        outcome, action = "timeout", "shorten"
    elif category is ErrorCategory.PEER_AUTH_FAILURE:
        outcome, action = "auth_failure", "route_to_code_task"
    elif category in CORRECTIVE or category is ErrorCategory.PEER_STRUCTURED_OUTPUT_EXHAUSTED:
        outcome, action = "structured_output_exhausted", "decompose"
    else:
        raise ValueError(f"unsupported receipt category {category.value}")
    return {
        "receipt_version": "1",
        "outcome": outcome,
        "error_category": category.value,
        "prompt_sha256": store.sha256_text(prompt),
        "config_snapshot_sha256": effective_config_sha256(cfg.raw),
        "attempt_count": len(attempts),
        "attempts": [{key: attempt[key] for key in (
            "attempt", "prompt_sha256", "prompt_chars", "category", "duration_seconds"
        )} for attempt in attempts],
        "elapsed_stages": stages,
        "elapsed_seconds": round(max(0.0, elapsed), 3),
        "budget_seconds": cfg.request_timeout(peer),
        "next_action": action,
        "allowed_next_actions": ["shorten", "decompose", "route_to_code_task"],
        "authentication_required": category is ErrorCategory.PEER_AUTH_FAILURE,
        "approval_granted": False,
        # Preserve the entire caller question; extracting sentences could lose
        # context, whitespace or questions embedded in code. Never salvage peer text.
        "unresolved_questions": [prompt],
    }
