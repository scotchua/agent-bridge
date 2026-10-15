"""Focused offline regressions for the shared backend helpers."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from agent_bridge.backends import base


class AuthFailureReasonTests(unittest.TestCase):
    def test_ignores_non_text_diagnostics(self):
        self.assertEqual(
            base.auth_failure_reason(401, None, b"No credentials for Cedar Ledger"),
            "not_logged_in",
        )


class LooksLikeAuthFailureTests(unittest.TestCase):
    def test_returns_true_for_auth_diagnostic(self):
        self.assertIs(base.looks_like_auth_failure("Please run codex login"), True)


class StripFencesTests(unittest.TestCase):
    def test_two_line_fence_and_unclosed_single_line(self):
        self.assertEqual(base.strip_fences('```json\n{"account":"Cedar Ledger"}'),
                         '{"account":"Cedar Ledger"}')
        self.assertEqual(base.strip_fences("```json"), "```json")


class ArgvShapeTests(unittest.TestCase):
    def test_preserves_short_values_and_summarizes_long_values(self):
        short = "A" * 60
        long = "B" * 61
        self.assertEqual(base.argv_shape(["--schema", short, long]),
                         ["--schema", short, "<61 chars>"])


if __name__ == "__main__":
    unittest.main()
