"""The identifier exception id reaches every send path (workbench U3, F6).

Synthetic data only. Each layer either carries a valid id through to
send_binding or refuses a malformed one at intake.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import operator_isolation  # noqa: E402,F401

from harness import Sandbox  # noqa: E402
from agent_bridge import broker, mcp_server, send_binding  # noqa: E402
from agent_bridge.errors import BrokerError, ErrorCategory  # noqa: E402
from agent_bridge.execution import claude_task, codex_task  # noqa: E402

BAD_IDS = ("", "has space", "a/b", "../x", "x" * 65, "dot.json")


class ValidIdTests(unittest.TestCase):
    def test_valid_and_invalid_ids(self):
        for value in ("abc", "A-1_b", "x" * 64):
            self.assertTrue(send_binding.valid_exception_id(value), value)
        for value in (*BAD_IDS, None, 7):
            self.assertFalse(send_binding.valid_exception_id(value), value)


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)

    def _validate(self, **extra):
        args = {"prompt": "synthetic question", "source_classification": "internal", **extra}
        broker._validate_common(self.sandbox.cfg, args, broker.START_FIELDS, "claude")

    def test_valid_id_is_accepted(self):
        self._validate(exception_id="exc-1")

    def test_malformed_id_is_refused(self):
        for value in (*BAD_IDS, None, 3):
            with self.subTest(value=value), self.assertRaises(BrokerError) as caught:
                self._validate(exception_id=value)
            self.assertEqual(caught.exception.category, ErrorCategory.INPUT_SCHEMA_INVALID)

    def test_id_reaches_the_worker_request(self):
        started = self.sandbox.consult("claude", exception_id="exc-1")
        self.sandbox.wait(started["job_id"])
        path = os.path.join(self.sandbox.cfg.job_dir(started["job_id"]), "request.json")
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["exception_id"], "exc-1")

    def test_request_without_an_id_keeps_its_old_shape(self):
        # The worker revalidates handoff requests against START_FIELDS, so a
        # stored null id would refuse every handoff (found by the full suite).
        started = self.sandbox.consult("claude")
        self.sandbox.wait(started["job_id"])
        path = os.path.join(self.sandbox.cfg.job_dir(started["job_id"]), "request.json")
        with open(path, encoding="utf-8") as handle:
            self.assertNotIn("exception_id", json.load(handle))


class SchemaTests(unittest.TestCase):
    def test_start_schema_offers_the_id_with_the_same_rule(self):
        schema = mcp_server._start_schema("codex")
        self.assertFalse(schema["additionalProperties"])
        pattern = schema["properties"]["exception_id"]["pattern"]
        for value in ("abc", "x" * 64):
            self.assertTrue(re.search(pattern, value), value)
        for value in BAD_IDS:
            self.assertIsNone(re.search(pattern, value), value)


class HarnessTests(unittest.TestCase):
    def _codex_argv(self, *extra):
        return ["brief.md", "--repo", "/repo", "--classification", "synthetic", *extra]

    def _claude_argv(self, *extra):
        return ["brief.md", "--repo", "/repo", "--classification", "synthetic",
                "--verify-json", '["true"]', *extra]

    def test_harnesses_forward_the_id_to_run_task(self):
        for module, argv in ((codex_task, self._codex_argv), (claude_task, self._claude_argv)):
            with self.subTest(module=module.__name__), \
                    mock.patch.object(module, "run_task", return_value={"status": "complete"}) as run, \
                    mock.patch("sys.stdout"):
                self.assertEqual(module.main(argv("--exception-id", "exc-1")), 0)
                self.assertEqual(run.call_args.kwargs["exception_id"], "exc-1")

    def test_harnesses_default_to_no_id(self):
        for module, argv in ((codex_task, self._codex_argv), (claude_task, self._claude_argv)):
            with self.subTest(module=module.__name__), \
                    mock.patch.object(module, "run_task", return_value={"status": "complete"}) as run, \
                    mock.patch("sys.stdout"):
                module.main(argv())
                self.assertIsNone(run.call_args.kwargs["exception_id"])

    def test_harnesses_refuse_a_malformed_id(self):
        for module, argv in ((codex_task, self._codex_argv), (claude_task, self._claude_argv)):
            with self.subTest(module=module.__name__), \
                    mock.patch.object(module, "run_task") as run, \
                    mock.patch("sys.stderr"), self.assertRaises(SystemExit) as caught:
                module.main(argv("--exception-id", "../x"))
            self.assertEqual(caught.exception.code, 2)
            run.assert_not_called()

    def test_release_send_passes_the_id_to_guard(self):
        for module in (codex_task, claude_task):
            with self.subTest(module=module.__name__), \
                    mock.patch.object(module.send_binding, "guard") as guard:
                module._release_send(b"body", "codex", "client_derived", Path("/job"),
                                     ("execution",), "exc-1")
                self.assertEqual(guard.call_args.kwargs["exception_id"], "exc-1")


if __name__ == "__main__":
    unittest.main()
