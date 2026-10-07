"""Provider-reported token usage, retained as metadata only."""

from __future__ import annotations

from typing import Any


def _value(mapping: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return None


def claude_model_usage(model_usage: Any) -> dict[str, Any]:
    """Normalize Claude's ``modelUsage`` map without inventing missing values."""
    if not isinstance(model_usage, dict) or not model_usage:
        return {"usage_present": False, "usage": None}
    models: list[dict[str, Any]] = []
    for model in sorted(model_usage):
        reported = model_usage[model]
        reported = reported if isinstance(reported, dict) else {}
        usage = {
            "input": _value(reported, "inputTokens"),
            "output": _value(reported, "outputTokens"),
            "cache_creation": _value(reported, "cacheCreationInputTokens"),
            "cache_read": _value(reported, "cacheReadInputTokens"),
            "cost_usd": _value(reported, "costUSD"),
            # These field names are speculative and were absent from observed
            # Claude Code 2.1.x envelopes. A labeled subset is never a total
            # component.
            "thinking_subset_of_output": _value(
                reported, "thinkingTokens", "reasoningTokens",
                "thinking_tokens", "reasoning_tokens"),
        }
        # Claude total = input + cache_creation + cache_read + output.
        # Thinking is already inside output and must never be added again.
        total_parts = (usage["input"], usage["cache_creation"],
                       usage["cache_read"], usage["output"])
        usage["total_reported"] = (
            sum(total_parts) if all(isinstance(value, (int, float)) for value in total_parts)
            else None
        )
        models.append({"model": model, "usage": usage})
    return {"usage_present": True, "usage": {"models": models}}


def codex_turn_usage(events: list[dict[str, Any]], *, cli_version: str | None,
                     resumed: bool) -> dict[str, Any]:
    """Record each completed Codex turn exactly as its event reported it."""
    turns: list[dict[str, Any]] = []
    for event_index, event in enumerate(events):
        if event.get("type") != "turn.completed":
            continue
        reported = event.get("usage")
        if not isinstance(reported, dict):
            turns.append({"event_index": event_index, "cli_version": cli_version,
                          "resumed": resumed,
                          "usage_present": False, "usage": None})
            continue
        usage = {
            "input_tokens": _value(reported, "input_tokens"),
            "cached_input_tokens": _value(reported, "cached_input_tokens"),
            "cache_write_input_tokens": _value(reported, "cache_write_input_tokens"),
            "output_tokens": _value(reported, "output_tokens"),
            # A labeled subset of output, never an additional total component.
            "reasoning_output_tokens_subset_of_output": _value(reported, "reasoning_output_tokens"),
            # The event does not state whether cached input is included in input.
            "cached_input_included_in_input": None,
            # Measured with codex-cli 0.160.1 on 2026-10-06: first turn
            # input=14741, cached=11008, output=5; resumed turn
            # input=35854, cached=11008, output=52. The event contract does
            # not say whether resumed values are cumulative, so never sum
            # turns or calculate a total.
            "total_reported": None,
        }
        turns.append({"event_index": event_index, "cli_version": cli_version,
                      "resumed": resumed,
                      "usage_present": True, "usage": usage})
    return {"aggregation": "per_event_as_reported",
            "usage_present": any(turn["usage_present"] for turn in turns),
            "turns": turns}
