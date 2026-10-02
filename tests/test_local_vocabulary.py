"""The local lane names its vocabulary and refuses what it will refuse, early.

Offline. A fake backend; nothing reaches a model.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.capacity_router import StageRouter
from agent_bridge.localq import gemma_child
from agent_bridge.localq.intake import KNOWN_FLAGS, AutomaticIntake, IntakePolicy
from agent_bridge.localq.spool import (ALLOWED_CLASSIFICATIONS, MECHANICAL_TASKS, AdmissionError,
                                       FakeBackend, LocalQueue, ResourceSnapshot)
from agent_bridge.orchestration.mcp import build_tools


class Sampler:
    def sample(self):
        return ResourceSnapshot(1000.0, "normal", "normal", True, 120.0)


class Fixture(unittest.TestCase):
    def queue(self, backend_id="private_worker", allowed=None):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return LocalQueue(temp.name, sampler=Sampler(), backend=FakeBackend(),
                          clock=lambda: 2000.0, backend_id=backend_id, allowed_task_types=allowed)

    def intake(self, queue):
        return AutomaticIntake(queue, policy=IntakePolicy(min_input_chars=10, min_nonblank_lines=1),
                               clock=lambda: 2000.0)

    def checkpoint(self, intake, **changes):
        values = dict(task_id="unit", task_type="summarize", classification="synthetic",
                      caller="claude", input_bytes=5000, nonblank_lines=50)
        values.update(changes)
        return intake.checkpoint(**values)


class GemmaParametersAreCheckedAtSubmission(Fixture):
    """Live: two of the first nine jobs failed custom_instruction_unsupported
    only after waiting their turn in the queue."""

    def submit(self, queue, params):
        return queue.submit(task_type="summarize", input="text\n" * 50, params=params,
                            priority="interactive", classification="synthetic",
                            caller="claude", purpose="test")

    def test_a_custom_instruction_is_refused_before_anything_is_queued(self):
        queue = self.queue("gemma_certified", frozenset({"summarize"}))
        with self.assertRaisesRegex(AdmissionError, "custom_instruction_unsupported"):
            self.submit(queue, {"instruction": "Summarize in three bullet points."})
        with self.assertRaisesRegex(AdmissionError, "params_invalid_for_gemma_certified"):
            self.submit(queue, {"temperature": 0})
        self.assertEqual(queue.state_report().get("queued", 0), 0)

    def test_the_certified_instruction_and_no_parameters_are_accepted(self):
        queue = self.queue("gemma_certified", frozenset({"summarize"}))
        self.submit(queue, {})
        self.submit(queue, {"instruction": gemma_child.CERTIFIED_SUMMARIZE_INSTRUCTION})

    def test_other_backends_keep_their_own_parameter_contract(self):
        self.submit(self.queue(), {"instruction": "Summarize in three bullet points."})

    def test_the_delegate_and_the_queue_apply_one_rule(self):
        for params, reason in (({}, None),
                               ({"instruction": gemma_child.CERTIFIED_SUMMARIZE_INSTRUCTION}, None),
                               ({"instruction": "other"}, "custom_instruction_unsupported"),
                               ({"x": 1}, "params_invalid_for_gemma_certified")):
            with self.subTest(params=params):
                self.assertEqual(gemma_child.params_refusal(params), reason)


class RefusalsNameTheAcceptedValues(Fixture):
    def test_an_unknown_risk_flag_lists_the_known_flags(self):
        result = self.checkpoint(self.intake(self.queue()), risk_flags=["client"])
        self.assertEqual(result["reason"], "risk_flags_invalid")
        self.assertEqual(result["accepted_risk_flags"], sorted(KNOWN_FLAGS))

    def test_an_unknown_classification_lists_the_accepted_ones(self):
        result = self.checkpoint(self.intake(self.queue()), classification="client")
        self.assertEqual(result["reason"], "classification_refused")
        self.assertEqual(result["accepted_classifications"], sorted(ALLOWED_CLASSIFICATIONS))

    def test_a_kind_the_backend_does_not_carry_lists_the_ones_it_does(self):
        intake = self.intake(self.queue("gemma_certified", frozenset({"summarize"})))
        result = self.checkpoint(intake, task_type="extract")
        self.assertEqual(result["accepted_task_types"], ["summarize"])

    def test_an_eligible_unit_carries_no_vocabulary(self):
        result = self.checkpoint(self.intake(self.queue()))
        self.assertEqual(result["status"], "eligible")
        self.assertFalse({k for k in result if k.startswith(("accepted_", "mechanical_"))})


class TheToolSchemasStateTheVocabulary(Fixture):
    def tools(self):
        queue = self.queue()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        router = StageRouter(os.path.join(temp.name, "capacity.sqlite3"))
        return build_tools("claude", router, queue, self.intake(queue))

    def test_routing_work_takes_only_known_values(self):
        props = self.tools()["work_route_local"]["inputSchema"]["properties"]
        self.assertEqual(props["task_type"]["enum"], sorted(MECHANICAL_TASKS))
        self.assertEqual(props["classification"]["enum"], sorted(ALLOWED_CLASSIFICATIONS))
        self.assertEqual(props["risk_flags"]["items"]["enum"], sorted(KNOWN_FLAGS))

    def test_a_checkpoint_names_the_values_but_still_records_any(self):
        props = self.tools()["work_checkpoint"]["inputSchema"]["properties"]
        self.assertNotIn("enum", props["classification"])
        self.assertNotIn("enum", props["risk_flags"]["items"])
        for value in sorted(ALLOWED_CLASSIFICATIONS):
            self.assertIn(value, props["classification"]["description"])
        for flag in sorted(KNOWN_FLAGS):
            self.assertIn(flag, props["risk_flags"]["description"])


if __name__ == "__main__":
    unittest.main()
