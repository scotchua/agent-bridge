"""Direct offline checks for broker failure receipt construction."""
from __future__ import annotations

import unittest

from agent_bridge import receipts, store
from agent_bridge.config import effective_config_sha256
from agent_bridge.errors import ErrorCategory


class ReceiptBuildTests(unittest.TestCase):
    class ConfigStub:
        raw = {"company": "Oriole Paper", "account": "Sable Ledger"}

        def request_timeout(self, peer):
            return {"claude": 17.5, "codex": 29.0}[peer]

    def setUp(self):
        self.cfg = self.ConfigStub()
        self.prompt = "  Review Oriole Paper's 2026-03-04 entry?\n"
        self.attempts = [
            {"attempt": 1, "prompt_sha256": "a" * 64, "prompt_chars": 42,
             "category": "peer_timeout", "duration_seconds": 1.25,
             "private_detail": "discard"},
            {"attempt": 2, "prompt_sha256": "b" * 64, "prompt_chars": 38,
             "category": "peer_timeout", "duration_seconds": 2.5,
             "private_detail": "discard"},
        ]
        self.stages = [{"stage": "execution", "elapsed_seconds": 3.75}]

    def build(self, category=ErrorCategory.PEER_TIMEOUT, elapsed=3.756):
        return receipts.build(self.cfg, "claude", self.prompt, category,
                              self.attempts, self.stages, elapsed)

    def test_build_timeout_identity_and_actions(self):
        result = self.build()
        self.assertEqual(result["receipt_version"], "1")
        self.assertEqual(result["outcome"], "timeout")
        self.assertEqual(result["error_category"], "peer_timeout")
        self.assertEqual(result["next_action"], "shorten")
        self.assertEqual(result["allowed_next_actions"],
                         ["shorten", "decompose", "route_to_code_task"])
        self.assertFalse(result["authentication_required"])
        self.assertIs(result["approval_granted"], False)

    def test_build_auth_failure_requires_authentication(self):
        result = self.build(ErrorCategory.PEER_AUTH_FAILURE)
        self.assertEqual(result["outcome"], "auth_failure")
        self.assertEqual(result["next_action"], "route_to_code_task")
        self.assertIs(result["authentication_required"], True)

    def test_build_corrective_and_exhausted_categories(self):
        for category in (ErrorCategory.PEER_OUTPUT_MALFORMED,
                         ErrorCategory.PEER_OUTPUT_SCHEMA_INVALID,
                         ErrorCategory.PEER_STRUCTURED_OUTPUT_EXHAUSTED):
            with self.subTest(category=category):
                result = self.build(category)
                self.assertEqual(result["outcome"], "structured_output_exhausted")
                self.assertEqual(result["next_action"], "decompose")
                self.assertEqual(result["error_category"], category.value)

    def test_build_hashes_budget_and_verbatim_question(self):
        result = self.build()
        self.assertEqual(result["prompt_sha256"], store.sha256_text(self.prompt))
        self.assertEqual(result["config_snapshot_sha256"],
                         effective_config_sha256(self.cfg.raw))
        self.assertEqual(result["budget_seconds"], 17.5)
        self.assertEqual(result["unresolved_questions"], [self.prompt])

    def test_build_attempts_and_stages(self):
        result = self.build()
        self.assertEqual(result["attempt_count"], 2)
        self.assertEqual(result["attempts"], [
            {key: attempt[key] for key in ("attempt", "prompt_sha256", "prompt_chars",
                                             "category", "duration_seconds")}
            for attempt in self.attempts])
        self.assertEqual(result["elapsed_stages"], self.stages)
        self.assertIsNot(result["elapsed_stages"], self.attempts)

    def test_build_elapsed_is_clamped_and_rounded(self):
        self.assertEqual(self.build(elapsed=-0.5)["elapsed_seconds"], 0.0)
        self.assertEqual(self.build(elapsed=3.756)["elapsed_seconds"], 3.756)
        self.assertEqual(self.build(elapsed=3.7564)["elapsed_seconds"], 3.756)

    def test_build_rejects_unsupported_category(self):
        with self.assertRaisesRegex(ValueError, "unsupported receipt category ok"):
            self.build(ErrorCategory.OK)


if __name__ == "__main__":
    unittest.main()
