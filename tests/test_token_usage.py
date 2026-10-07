"""Offline fixtures for provider-reported cloud token metadata."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge import token_usage


class ClaudeUsageTests(unittest.TestCase):
    def test_full_usage_for_one_and_two_models(self):
        one = token_usage.claude_model_usage({"claude-a": {
            "inputTokens": 10, "outputTokens": 20,
            "cacheCreationInputTokens": 3, "cacheReadInputTokens": 4,
            "costUSD": 0.12, "thinkingTokens": 7,
        }})
        usage = one["usage"]["models"][0]["usage"]
        self.assertTrue(one["usage_present"])
        self.assertEqual(usage["total_reported"], 37)
        self.assertEqual(usage["thinking_subset_of_output"], 7)
        self.assertEqual(len(token_usage.claude_model_usage({"claude-b": {}, "claude-a": {}})["usage"]["models"]), 2)

    def test_absent_usage_is_not_zero(self):
        usage = token_usage.claude_model_usage(None)
        self.assertEqual(usage, {"usage_present": False, "usage": None})

    def test_partial_usage_has_nulls_and_no_total(self):
        usage = token_usage.claude_model_usage({"claude-a": {"inputTokens": 10}})["usage"]["models"][0]["usage"]
        self.assertEqual(usage["input"], 10)
        self.assertIsNone(usage["output"])
        self.assertIsNone(usage["cache_creation"])
        self.assertIsNone(usage["cache_read"])
        self.assertIsNone(usage["cost_usd"])
        self.assertIsNone(usage["thinking_subset_of_output"])
        self.assertIsNone(usage["total_reported"])


class CodexUsageTests(unittest.TestCase):
    def test_reasoning_is_a_subset_not_a_total_component(self):
        recorded = token_usage.codex_turn_usage([{"type": "turn.completed", "usage": {
            "input_tokens": 10, "cached_input_tokens": 2,
            "cache_write_input_tokens": 3, "output_tokens": 20,
            "reasoning_output_tokens": 7,
        }}], cli_version="codex-cli fixture", resumed=False)
        turn = recorded["turns"][0]
        self.assertEqual(recorded["aggregation"], "per_event_as_reported")
        self.assertEqual(turn["cli_version"], "codex-cli fixture")
        self.assertFalse(turn["resumed"])
        self.assertEqual(turn["usage"]["reasoning_output_tokens_subset_of_output"], 7)
        self.assertIsNone(turn["usage"]["total_reported"])

    def test_older_cli_and_missing_usage_stay_distinct(self):
        old = token_usage.codex_turn_usage([{"type": "turn.completed", "usage": {
            "input_tokens": 10, "output_tokens": 20,
        }}], cli_version="older", resumed=True)
        self.assertTrue(old["turns"][0]["resumed"])
        self.assertIsNone(old["turns"][0]["usage"]["reasoning_output_tokens_subset_of_output"])
        absent = token_usage.codex_turn_usage([{"type": "turn.completed"}], cli_version="older", resumed=False)
        self.assertFalse(absent["usage_present"])
        self.assertFalse(absent["turns"][0]["usage_present"])
        self.assertIsNone(absent["turns"][0]["usage"])

    def test_multiple_turns_are_not_summed(self):
        recorded = token_usage.codex_turn_usage([
            {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 2}},
            {"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 4}},
        ], cli_version="fixture", resumed=True)
        self.assertEqual(len(recorded["turns"]), 2)
        self.assertEqual(recorded["turns"][0]["usage"]["input_tokens"], 1)
        self.assertEqual(recorded["turns"][1]["usage"]["input_tokens"], 3)
        self.assertTrue(all(turn["resumed"] for turn in recorded["turns"]))

    def test_cached_input_does_not_produce_a_codex_total(self):
        recorded = token_usage.codex_turn_usage([{"type": "turn.completed", "usage": {
            "input_tokens": 14_741, "cached_input_tokens": 11_008,
            "output_tokens": 5,
        }}], cli_version="codex-cli 0.160.1", resumed=False)
        usage = recorded["turns"][0]["usage"]
        self.assertIsNone(usage["cached_input_included_in_input"])
        self.assertIsNone(usage["total_reported"])


if __name__ == "__main__":
    unittest.main()
