"""Offline checks of the text handed to a consultation peer."""
from __future__ import annotations

import json
import unittest

from agent_bridge import envelope


SCHEMA = {"zeta": {"type": "number"}, "alpha": {"type": "string"}}
QUESTION = "For invented Northstar Kites LLC, explain account 42 on 2031-04-05."


class InitialPromptTests(unittest.TestCase):
    def test_initial_keeps_preamble_version_and_question(self) -> None:
        prompt = envelope.build_initial(QUESTION, SCHEMA, "v7")

        self.assertTrue(prompt.startswith(envelope.PREAMBLE + "\n\n"))
        self.assertIn("Response contract (JSON Schema, version v7):\n", prompt)
        self.assertIn(QUESTION, prompt)

    def test_initial_serializes_schema_with_stable_indentation_and_order(self) -> None:
        prompt = envelope.build_initial(QUESTION, SCHEMA, "v7")
        rendered = json.dumps(SCHEMA, indent=2, sort_keys=True)

        self.assertIn("\n" + rendered + "\n\n", prompt)

    def test_initial_delimits_the_question(self) -> None:
        prompt = envelope.build_initial(QUESTION, SCHEMA, "v7")

        self.assertIn("--- BEGIN CONSULTATION QUESTION ---\n" + QUESTION, prompt)
        self.assertTrue(prompt.endswith(QUESTION + "\n--- END CONSULTATION QUESTION ---"))


class ContinuationPromptTests(unittest.TestCase):
    def test_continuation_keeps_rules_version_and_question(self) -> None:
        prompt = envelope.build_continuation(QUESTION, "v7")

        self.assertTrue(prompt.startswith(
            "Continuing the same consultation. Same rules, same response contract "
            "(version v7): reply with a single conforming JSON object, "
            "no prose outside the JSON.\n\n"))
        self.assertIn(QUESTION, prompt)

    def test_continuation_delimits_the_follow_up_question(self) -> None:
        prompt = envelope.build_continuation(QUESTION, "v7")

        self.assertIn("--- BEGIN FOLLOW-UP QUESTION ---\n" + QUESTION, prompt)
        self.assertTrue(prompt.endswith(QUESTION + "\n--- END FOLLOW-UP QUESTION ---"))


class CorrectivePromptTests(unittest.TestCase):
    @staticmethod
    def violation_lines(prompt: str) -> list[str]:
        section = prompt.split(
            "Structural violations detected by the broker (paths and expected types only):\n", 1
        )[1].split("\n\nReply again", 1)[0]
        return section.splitlines()

    def test_corrective_preserves_code_version_and_sorted_schema(self) -> None:
        prompt = envelope.build_corrective(
            "peer_output_schema_invalid", ["$.account: expected string"], SCHEMA, "v7")

        self.assertIn("Failure code: peer_output_schema_invalid\n", prompt)
        self.assertIn("Contract (JSON Schema, version v7):\n", prompt)
        self.assertTrue(prompt.endswith(json.dumps(SCHEMA, indent=2, sort_keys=True)))

    def test_corrective_keeps_bulleted_violations(self) -> None:
        prompt = envelope.build_corrective(
            "peer_output_schema_invalid", ["$.account: expected string"], SCHEMA, "v7")

        self.assertEqual(self.violation_lines(prompt), ["- $.account: expected string"])

    def test_corrective_limits_violation_count_to_twelve(self) -> None:
        violations = [f"$.account{index}: expected string" for index in range(13)]
        prompt = envelope.build_corrective("peer_output_schema_invalid", violations, SCHEMA, "v7")

        self.assertEqual(self.violation_lines(prompt), ["- " + item for item in violations[:12]])

    def test_corrective_limits_each_violation_to_240_characters(self) -> None:
        violation = "x" * 241
        prompt = envelope.build_corrective(
            "peer_output_schema_invalid", [violation], SCHEMA, "v7")

        self.assertEqual(self.violation_lines(prompt), ["- " + violation[:240]])

    def test_corrective_rejects_non_string_violations_with_validator_error(self) -> None:
        with self.assertRaisesRegex(TypeError, "violations must be strings from the broker validator"):
            envelope.build_corrective("peer_output_schema_invalid", [42], SCHEMA, "v7")

    def test_corrective_explains_absent_violations(self) -> None:
        prompt = envelope.build_corrective("peer_output_schema_invalid", [], SCHEMA, "v7")

        self.assertEqual(self.violation_lines(prompt), [
            "- response did not parse as a single JSON object"])


if __name__ == "__main__":
    unittest.main()
