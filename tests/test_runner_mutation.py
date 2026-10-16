"""Focused offline contracts for peer execution and its small helpers."""
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from agent_bridge import runner


class RunResultTests(unittest.TestCase):
    def test_sanitized_argv_is_a_separate_copy(self):
        result = runner.RunResult(["synthetic-peer", "--mode"], 0, b"", b"",
                                  False, 0.0, None)
        sanitized = result.sanitized_argv()
        self.assertEqual(sanitized, ["synthetic-peer", "--mode"])
        self.assertIsNot(sanitized, result.argv)


class RunTests(unittest.TestCase):
    def call(self, **changes):
        options = dict(cwd=tempfile.gettempdir(), env={}, stdin_data="",
                       timeout=4, grace=1, stdout_cap=32, stderr_cap=32)
        options.update(changes)
        return runner.run(["synthetic-peer"], **options)

    def test_bad_arguments_and_nonfinite_budgets_are_rejected(self):
        for argv in ("synthetic-peer", ["synthetic-peer", 4]):
            with self.subTest(argv=argv), \
                 mock.patch.object(runner.platform, "spawn_isolated") as spawn:
                with self.assertRaises(TypeError):
                    runner.run(argv, cwd=tempfile.gettempdir(), env={}, stdin_data="",
                               timeout=4, grace=1, stdout_cap=32, stderr_cap=32)
                spawn.assert_not_called()
        for changes in ({"timeout": float("inf")}, {"grace": float("nan")},
                        {"deadline": float("inf")}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.call(**changes)

    def test_exhausted_budget_does_not_spawn(self):
        with tempfile.TemporaryDirectory() as root:
            marker = os.path.join(root, "existing-marker")
            with open(marker, "w", encoding="utf-8") as handle:
                handle.write("synthetic marker")
            with mock.patch.object(runner.platform, "spawn_isolated") as spawn:
                result = self.call(timeout=0, pgid_file=marker)
            self.assertTrue(result.timed_out)
            self.assertIsNone(result.pgid)
            self.assertTrue(os.path.exists(marker))
            spawn.assert_not_called()

    def test_read_deadline_boundary_does_not_spawn(self):
        with mock.patch.object(runner.time, "monotonic", side_effect=[100.0, 102.9, 102.9]), \
             mock.patch.object(runner.platform, "spawn_isolated") as spawn:
            result = self.call(timeout=4, grace=1)
        self.assertTrue(result.timed_out)
        spawn.assert_not_called()

    def test_marker_failure_refuses_spawn(self):
        with mock.patch.object(runner, "_write_marker", return_value=False), \
             mock.patch.object(runner.platform, "spawn_isolated") as spawn:
            result = self.call(pgid_file="synthetic-marker")
        self.assertTrue(result.spawn_failed)
        self.assertTrue(result.marker_write_failed)
        spawn.assert_not_called()

    def test_deadline_and_grace_bound_stream_budget(self):
        proc = mock.Mock(pid=12345)
        proc.poll.return_value = 0
        read = SimpleNamespace(stdout=b"ok", stderr=b"notice", timed_out=False,
                               cap_exceeded=False, descendant_held_pipes=False)
        with mock.patch.object(runner.time, "monotonic", return_value=100.0), \
             mock.patch.object(runner.platform, "spawn_isolated", return_value=proc), \
             mock.patch.object(runner.platform, "isolated_process_group", return_value=None), \
             mock.patch.object(runner.platform, "read_streams_with_caps", return_value=read) as streams:
            result = self.call(timeout=4, grace=3, deadline=103)
        self.assertAlmostEqual(streams.call_args.args[2], 1.4)
        self.assertEqual(result.stdout, b"ok")
        self.assertEqual(result.stderr, b"notice")
        self.assertFalse(result.timed_out)
        self.assertEqual(result.returncode, 0)

    def test_cap_breach_terminates_the_group(self):
        proc = mock.Mock(pid=12345)
        proc.poll.return_value = 0
        read = SimpleNamespace(stdout=b"x", stderr=b"", timed_out=False,
                               cap_exceeded=True, descendant_held_pipes=False)
        with mock.patch.object(runner.platform, "spawn_isolated", return_value=proc), \
             mock.patch.object(runner.platform, "isolated_process_group", return_value=54321), \
             mock.patch.object(runner.platform, "read_streams_with_caps", return_value=read), \
             mock.patch.object(runner.platform, "terminate_process_tree", return_value={"sigkill": True}) as terminate:
            result = self.call()
        terminate.assert_called_once()
        self.assertEqual(terminate.call_args.args[0], 54321)
        self.assertTrue(result.cap_exceeded)
        self.assertEqual(result.group_kill, {"sigkill": True})


class MarkerTests(unittest.TestCase):
    def test_write_marker_creates_durable_json(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "nested", "marker.json")
            with mock.patch.object(runner.os, "fsync", wraps=os.fsync) as fsync:
                self.assertTrue(runner._write_marker(path, {"phase": "synthetic"}))
            self.assertGreaterEqual(fsync.call_count, 1)
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), '{"phase": "synthetic"}')

    def test_write_marker_reports_open_error(self):
        with mock.patch("builtins.open", side_effect=OSError("synthetic failure")):
            self.assertFalse(runner._write_marker("synthetic-marker", {"phase": "synthetic"}))


class PauseTests(unittest.TestCase):
    def test_pause_only_at_selected_phase(self):
        with mock.patch.object(runner.time, "sleep") as sleep:
            runner.pause_after_marker_transition({}, "spawned")
            runner.pause_after_marker_transition(
                {runner.MARKER_FAULT_PHASE_ENV: "pre_spawn"}, "spawned")
            sleep.assert_not_called()
            runner.pause_after_marker_transition(
                {runner.MARKER_FAULT_PHASE_ENV: "spawned"}, "spawned")
            sleep.assert_called_once_with(runner.MARKER_FAULT_PAUSE_SECONDS)


class EnvironmentTests(unittest.TestCase):
    def test_scrubbed_env_has_defaults_and_explicit_overrides(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "invented-secret"}, clear=True):
            env = runner.scrubbed_env({"PATH": "/synthetic/bin", "ACME_ACCOUNT": "demo"})
        self.assertEqual(env["PATH"], "/synthetic/bin")
        self.assertEqual(env["AGENT_BRIDGE_PEER"], "1")
        self.assertEqual(env["ACME_ACCOUNT"], "demo")
        self.assertNotIn("OPENAI_API_KEY", env)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(runner.scrubbed_env()["PATH"],
                             "/usr/bin:/bin:/usr/sbin:/sbin")

    def test_credential_context_contains_only_present_selectors(self):
        context = runner.credential_context({"HOME": "/synthetic/home",
                                             "CODEX_HOME": "/synthetic/codex",
                                             "OPENAI_API_KEY": "invented-secret"})
        self.assertEqual(context, {"HOME": "/synthetic/home",
                                   "CODEX_HOME": "/synthetic/codex"})


if __name__ == "__main__":
    unittest.main()
