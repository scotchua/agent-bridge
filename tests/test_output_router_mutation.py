"""Boundary cases exposed by the output router mutation sweep."""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.orchestration import output_router  # noqa: E402


class OutputRouterMutationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = self.temp.name

    def route(self, *, output=b"x" * 4096, decision="local", status="complete",
              digest="digest", backend_id="fake", wait_seconds=45, clock=lambda: 0):
        queue = mock.Mock()
        queue.backend_id = backend_id
        queue.caps.max_input_bytes = 100_000
        queue.result.return_value = {"ready": True, "status": status,
                                     "result": {"output": {"text": digest}}}
        intake = mock.Mock(queue=queue)
        intake.checkpoint.return_value = {"checkpoint_id": "checkpoint-1"}
        intake.route.return_value = {"decision": decision, "job_id": "job-1",
                                     "reason": "not eligible"}
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            code = output_router.route_output(output, state_root=self.state,
                                              repo="invented-repo", classification="synthetic",
                                              intake=intake, wait_seconds=wait_seconds, clock=clock)
        return code, captured.getvalue(), intake

    def test_git_routable_rejects_diff_stat_without_path_separator(self):
        self.assertFalse(output_router._git_routable(["git", "diff", "--stat"]))

    def test_is_routable_command_rejects_non_string_and_unbounded_grep(self):
        self.assertFalse(output_router.is_routable_command(None))
        self.assertFalse(output_router.is_routable_command("grep marker invented.txt"))

    def test_prune_output_files_removes_old_capture(self):
        directory = output_router.output_directory(self.state)
        directory.mkdir(parents=True)
        old = directory / "old.output"
        old.write_bytes(b"invented output")
        now = 1_700_000_000
        os.utime(old, (now - output_router.RETENTION_SECONDS - 1,) * 2)
        output_router.prune_output_files(self.state, now=now)
        self.assertFalse(old.exists())

    def test_prune_output_files_keeps_capture_at_cutoff(self):
        directory = output_router.output_directory(self.state)
        directory.mkdir(parents=True)
        boundary = directory / "boundary.output"
        boundary.write_bytes(b"invented output")
        now = 1_700_000_000
        cutoff = now - output_router.RETENTION_SECONDS
        os.utime(boundary, (cutoff, cutoff))
        output_router.prune_output_files(self.state, now=now)
        self.assertTrue(boundary.exists())

    def test_capture_output_prunes_before_writing_and_returns_path(self):
        directory = output_router.output_directory(self.state)
        directory.mkdir(parents=True)
        old = directory / "old.output"
        old.write_bytes(b"old")
        os.utime(old, (1, 1))
        path = output_router.capture_output(self.state, b"new", now=1_700_000_000)
        self.assertFalse(old.exists())
        self.assertEqual(Path(path).read_bytes(), b"new")

    def test_raw_flushes_text_before_buffer_bytes(self):
        events = []

        class Buffer:
            def write(self, data):
                events.append(data.decode())

            def flush(self):
                pass

        class Stream:
            buffer = Buffer()

            def __init__(self):
                self.pending = ""

            def write(self, text):
                self.pending += text

            def flush(self):
                events.append(self.pending)
                self.pending = ""

        stream = Stream()
        with mock.patch.object(output_router.sys, "stdout", stream):
            stream.write("header")
            output_router._raw(b"body")
        self.assertEqual("".join(events), "headerbody")

    def test_fit_for_queue_uses_sixty_percent_budget(self):
        _, clipped = output_router._fit_for_queue(b"x" * 6500, 10_000)
        self.assertTrue(clipped)

    def test_fit_for_queue_keeps_exact_budget_without_clipping(self):
        output = b"x" * 6000
        self.assertEqual(output_router._fit_for_queue(output, 10_000), (output, False))

    def test_fit_for_queue_keeps_equal_head_and_tail_halves(self):
        output = b"a" * 4000 + b"b" * 4000
        fitted, clipped = output_router._fit_for_queue(output, 10_000)
        self.assertTrue(clipped)
        self.assertTrue(fitted.startswith(b"a" * 3000 + b"\n...[middle omitted"))
        self.assertTrue(fitted.endswith(b"b" * 3000))

    def test_failure_tail_keeps_last_forty_matching_lines(self):
        output = "".join(f"FAILED invented {number}\n" for number in range(41)).encode()
        tail = output_router._failure_tail(output).splitlines()
        self.assertEqual(len(tail), 40)
        self.assertEqual(tail[0], "FAILED invented 1")

    def test_digest_text_rejects_non_string_text_and_output(self):
        self.assertIsNone(output_router._digest_text({"result": {"output": {"text": 7}}}))
        self.assertIsNone(output_router._digest_text({"result": {"output": 7}}))

    def test_digest_text_accepts_plain_string_output(self):
        self.assertEqual(output_router._digest_text({"result": {"output": "invented digest"}}),
                         "invented digest")

    def test_route_output_routes_exact_threshold_and_returns_zero(self):
        code, text, intake = self.route()
        self.assertEqual(code, 0)
        self.assertIn("[local digest of 4096 bytes", text)
        intake.queue.admit.assert_called_once_with("interactive")

    def test_route_output_small_output_returns_zero(self):
        code, text, intake = self.route(output=b"small")
        self.assertEqual(code, 0)
        self.assertEqual(text, "small")
        intake.queue.admit.assert_not_called()

    def test_route_output_certified_backend_omits_instruction(self):
        _, text, intake = self.route(backend_id="gemma_certified")
        self.assertIn("[local digest", text)
        self.assertEqual(intake.route.call_args.kwargs["params"], {})

    def test_route_output_refusal_waives_even_with_job_id(self):
        _, text, intake = self.route(decision="remote")
        self.assertIn("[local output routing waived: intake_refused:not eligible]", text)
        self.assertTrue(text.endswith("x" * 4096))
        intake.queue.result.assert_not_called()

    def test_route_output_zero_wait_never_polls(self):
        _, text, intake = self.route(wait_seconds=0)
        self.assertIn("[local output routing waived: timeout]", text)
        intake.queue.result.assert_not_called()

    def test_route_output_failed_result_with_digest_waives(self):
        _, text, _ = self.route(status="failed")
        self.assertIn("[local output routing waived: local_failed]", text)
        self.assertTrue(text.endswith("x" * 4096))

    def test_route_output_digest_with_newline_is_not_doubled(self):
        _, text, _ = self.route(digest="invented digest\n")
        self.assertIn("invented digest\n", text)
        self.assertNotIn("invented digest\n\n", text)

    def test_run_command_returns_127_when_child_cannot_start(self):
        with mock.patch.object(output_router.subprocess, "run", side_effect=FileNotFoundError):
            with contextlib.redirect_stdout(io.StringIO()):
                code = output_router.run_command(["invented-missing-command"],
                                                 config_path="invented-config")
        self.assertEqual(code, 127)

    def test_main_strips_separator_and_returns_child_code(self):
        with mock.patch.object(output_router, "run_command", return_value=7) as run:
            code = output_router.main(["run", "--config", "invented-config", "--", "invented-command"])
        self.assertEqual(code, 7)
        run.assert_called_once_with(["invented-command"], config_path="invented-config")

    def test_main_rejects_empty_command(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                output_router.main(["run", "--config", "invented-config"])


if __name__ == "__main__":
    unittest.main()
