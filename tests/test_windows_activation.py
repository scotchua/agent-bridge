"""Portable tests for Windows per-user background activation.

The module builds argv and parses output, so all of it is testable off
Windows. What is not tested here is that Task Scheduler accepts these
arguments on a real machine, or that the worker actually starts at logon.
"""
from __future__ import annotations

from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge.orchestration import windows_activation as wa
from agent_bridge.orchestration import windows_wsl_provision as wp

SYSTEM32 = "C:\\Windows\\System32"
WORKER = "C:\\Users\\sam\\AppData\\Local\\AgentBridge\\worker.exe"


def _result(returncode=0):
    return types.SimpleNamespace(returncode=returncode)


def _query_output(action=WORKER, state="Ready"):
    """What schtasks /Query /FO LIST /V prints for a registered task."""
    return (f"TaskName: \\{wa.TASK_NAME}\r\n"
            f"Task To Run: {action}\r\n"
            f"Status: {state}\r\n").encode("utf-8")


class RecordingRunner:
    """Answers the ownership query, then records the mutating call.

    ``query_stdout`` defaults to empty, which parses as "no such task", the
    state of a machine that has never been activated.
    """

    def __init__(self, returncode=0, query_stdout=b"", query_returncode=0):
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.query_stdout = query_stdout
        self.query_returncode = query_returncode

    def __call__(self, argv):
        self.calls.append(list(argv))
        if "/Query" in argv:
            return types.SimpleNamespace(returncode=self.query_returncode,
                                         stdout=self.query_stdout)
        return _result(self.returncode)

    @property
    def mutating(self):
        return [call for call in self.calls if "/Query" not in call]


# ---------------------------------------------------------------------------
# Argument quoting
# ---------------------------------------------------------------------------


class QuotingTests(unittest.TestCase):
    def test_an_argument_without_spaces_is_left_alone(self):
        self.assertEqual(wa.quote_action_argument("--worker"), "--worker")

    def test_an_argument_with_spaces_is_quoted(self):
        self.assertEqual(
            wa.quote_action_argument("C:\\Program Files\\x\\y.exe"),
            '"C:\\Program Files\\x\\y.exe"')

    def test_an_embedded_quote_is_refused_rather_than_escaped(self):
        with self.assertRaises(wa.ActivationError):
            wa.quote_action_argument('bad"value')

    def test_a_percent_sign_is_refused_because_the_task_engine_expands_it(self):
        with self.assertRaises(wa.ActivationError):
            wa.quote_action_argument("%APPDATA%\\worker.exe")

    def test_control_characters_are_refused(self):
        for bad in ("a\nb", "a\rb", "a\tb", "a\x00b"):
            with self.subTest(bad=bad):
                with self.assertRaises(wa.ActivationError):
                    wa.quote_action_argument(bad)

    def test_an_empty_argument_is_refused(self):
        with self.assertRaises(wa.ActivationError):
            wa.quote_action_argument("")

    def test_a_non_string_argument_is_refused(self):
        for bad in (None, 3, ["x"]):
            with self.subTest(bad=bad):
                with self.assertRaises(wa.ActivationError):
                    wa.quote_action_argument(bad)


class ActionTests(unittest.TestCase):
    def test_the_action_joins_the_quoted_arguments(self):
        action = wa.build_action([WORKER, "--worker", "--once"])
        self.assertEqual(action, f"{WORKER} --worker --once")

    def test_an_empty_command_is_refused(self):
        with self.assertRaises(wa.ActivationError):
            wa.build_action([])

    def test_a_relative_executable_is_refused(self):
        with self.assertRaises(wa.ActivationError):
            wa.build_action(["worker.exe", "--worker"])

    def test_a_bare_name_cannot_be_resolved_through_path(self):
        with self.assertRaises(wa.ActivationError):
            wa.build_action(["schtasks.exe"])

    def test_an_over_long_command_is_refused_rather_than_truncated(self):
        long_tail = "a" * wa.MAX_TASK_ACTION_CHARS
        with self.assertRaises(wa.ActivationError):
            wa.build_action([WORKER, long_tail])

    def test_a_command_at_the_limit_is_accepted(self):
        pad = wa.MAX_TASK_ACTION_CHARS - len(WORKER) - 1
        action = wa.build_action([WORKER, "a" * pad])
        self.assertEqual(len(action), wa.MAX_TASK_ACTION_CHARS)

    def test_the_limit_matches_the_one_the_resume_task_uses(self):
        # Both go through schtasks /TR, so the two modules must agree; a
        # divergence would mean one of them silently truncates.
        self.assertEqual(wa.MAX_TASK_ACTION_CHARS, 261)
        with self.assertRaises(ValueError):
            wp.register_resume_argv([WORKER, "a" * 261])


# ---------------------------------------------------------------------------
# Registration argv
# ---------------------------------------------------------------------------


class RegisterArgvTests(unittest.TestCase):
    def setUp(self):
        self.argv = wa.register_argv([WORKER, "--worker"])

    def test_it_runs_schtasks_from_an_absolute_system32_path(self):
        self.assertEqual(self.argv[0], f"{SYSTEM32}\\schtasks.exe")

    def test_it_creates_the_named_task(self):
        self.assertEqual(self.argv[1], "/Create")
        self.assertIn("/TN", self.argv)
        self.assertEqual(self.argv[self.argv.index("/TN") + 1],
                         wa.TASK_NAME)

    def test_it_triggers_on_logon(self):
        self.assertEqual(self.argv[self.argv.index("/SC") + 1], "ONLOGON")

    def test_it_never_asks_for_another_user(self):
        self.assertNotIn("/RU", self.argv)
        self.assertNotIn("/RP", self.argv)
        self.assertNotIn("/S", self.argv)
        self.assertNotIn("/U", self.argv)

    def test_it_never_asks_for_elevation(self):
        self.assertNotIn("/RL", self.argv)
        self.assertNotIn("HIGHEST", self.argv)

    def test_it_carries_the_validated_action(self):
        self.assertEqual(self.argv[self.argv.index("/TR") + 1],
                         f"{WORKER} --worker")

    def test_it_delays_the_start_after_logon(self):
        self.assertEqual(self.argv[self.argv.index("/DELAY") + 1], "0001:00")

    def test_an_hour_delay_is_converted_to_minutes(self):
        argv = wa.register_argv([WORKER], delay="PT2H")
        self.assertEqual(argv[argv.index("/DELAY") + 1], "0120:00")

    def test_a_malformed_delay_is_refused(self):
        for bad in ("", "1m", "PT", "PT1S", "PT1000M", "later"):
            with self.subTest(bad=bad):
                with self.assertRaises(wa.ActivationError):
                    wa.register_argv([WORKER], delay=bad)

    def test_a_bad_command_is_refused_before_any_argv_is_produced(self):
        with self.assertRaises(wa.ActivationError):
            wa.register_argv(['C:\\x\\"y.exe'])

    def test_no_argument_contains_a_shell_metacharacter_we_did_not_write(self):
        for item in self.argv:
            self.assertNotIn("&", item)
            self.assertNotIn("|", item)
            self.assertNotIn(";", item)


class TaskNameTests(unittest.TestCase):
    def test_the_task_name_is_fixed_and_descriptive(self):
        self.assertEqual(wa.TASK_NAME, "AgentBridgeExecutionWorker")

    def test_it_does_not_collide_with_the_setup_resume_task(self):
        self.assertNotEqual(wa.TASK_NAME, wp.RESUME_TASK_NAME)

    def test_an_injected_task_name_is_refused(self):
        for bad in ("", "a b", "a\\b", "a/b", "a\"b", "x" * 121, None, 7):
            with self.subTest(bad=bad):
                with self.assertRaises(wa.ActivationError):
                    wa.unregister_argv(bad)

    def test_a_custom_valid_task_name_is_accepted(self):
        argv = wa.unregister_argv("AgentBridge.Test-1")
        self.assertIn("AgentBridge.Test-1", argv)


class OtherArgvTests(unittest.TestCase):
    def test_unregister_deletes_exactly_the_named_task(self):
        self.assertEqual(
            wa.unregister_argv(),
            [f"{SYSTEM32}\\schtasks.exe", "/Delete", "/TN", wa.TASK_NAME, "/F"])

    def test_status_queries_verbose_list_output(self):
        argv = wa.status_argv()
        self.assertEqual(argv[:4],
                         [f"{SYSTEM32}\\schtasks.exe", "/Query", "/TN", wa.TASK_NAME])
        self.assertIn("/V", argv)

    def test_run_now_starts_the_task_once(self):
        self.assertEqual(
            wa.run_now_argv(),
            [f"{SYSTEM32}\\schtasks.exe", "/Run", "/TN", wa.TASK_NAME])

    def test_every_builder_uses_the_same_absolute_binary(self):
        binaries = {
            wa.register_argv([WORKER])[0],
            wa.unregister_argv()[0],
            wa.status_argv()[0],
            wa.run_now_argv()[0],
        }
        self.assertEqual(binaries, {f"{SYSTEM32}\\schtasks.exe"})


# ---------------------------------------------------------------------------
# Status parsing
# ---------------------------------------------------------------------------


class StatusTests(unittest.TestCase):
    def test_a_nonzero_query_reports_absent_not_an_error(self):
        status = wa.parse_status(1, b"ERROR: cannot find the file specified.")
        self.assertFalse(status.registered)
        self.assertEqual(status.state, "absent")

    def test_a_missing_exit_code_is_not_treated_as_registered(self):
        self.assertFalse(wa.parse_status(None, b"Status: Ready").registered)

    def test_a_ready_task_is_reported_registered(self):
        status = wa.parse_status(0, b"TaskName: \\AgentBridge\r\nStatus: Ready\r\n")
        self.assertTrue(status.registered)
        self.assertEqual(status.state, "Ready")

    def test_the_localised_state_label_is_also_read(self):
        status = wa.parse_status(
            0, b"Scheduled Task State: Enabled\r\n")
        self.assertTrue(status.registered)
        self.assertEqual(status.state, "Enabled")

    def test_utf16_output_is_decoded(self):
        payload = "Status: Running\r\n".encode("utf-16-le")
        status = wa.parse_status(0, payload)
        self.assertTrue(status.registered)
        self.assertEqual(status.state, "Running")

    def test_a_utf8_bom_is_stripped(self):
        status = wa.parse_status(0, b"\xef\xbb\xbfStatus: Ready\r\n")
        self.assertEqual(status.state, "Ready")

    def test_undecodable_output_fails_closed(self):
        status = wa.parse_status(0, b"\xff\xfe\x00")
        self.assertFalse(status.registered)

    def test_output_without_a_state_fails_closed(self):
        status = wa.parse_status(0, b"TaskName: \\AgentBridge\r\n")
        self.assertFalse(status.registered)
        self.assertEqual(status.detail, "state_not_reported")

    def test_empty_output_fails_closed(self):
        self.assertFalse(wa.parse_status(0, b"").registered)

    def test_the_status_serialises_to_plain_data(self):
        self.assertEqual(
            wa.parse_status(0, b"Status: Ready\r\n").as_dict(),
            {"registered": True, "state": "Ready", "detail": "", "action": ""})


# ---------------------------------------------------------------------------
# Consent and reversibility
# ---------------------------------------------------------------------------


class ConsentTests(unittest.TestCase):
    def test_activation_without_consent_runs_nothing(self):
        runner = RecordingRunner()
        with self.assertRaises(wa.ConsentRequired):
            wa.activate([WORKER], run=runner, consent=False)
        self.assertEqual(runner.calls, [])

    def test_activation_with_consent_registers_the_task(self):
        runner = RecordingRunner()
        outcome = wa.activate([WORKER, "--worker"], run=runner, consent=True)
        self.assertEqual(outcome["status"], "ok")
        self.assertTrue(outcome["reversible"])
        self.assertEqual(len(runner.mutating), 1)
        self.assertIn("/Create", runner.mutating[0])

    def test_a_failed_registration_is_reported_not_swallowed(self):
        runner = RecordingRunner(returncode=1)
        outcome = wa.activate([WORKER], run=runner, consent=True)
        self.assertEqual(outcome["status"], "failed")
        self.assertEqual(outcome["reason"], "register_failed")

    def test_a_runner_with_no_exit_code_is_a_failure(self):
        outcome = wa.activate([WORKER], run=lambda argv: object(), consent=True)
        self.assertEqual(outcome["status"], "failed")
        self.assertEqual(outcome["reason"], "no_exit_code")

    def test_a_bad_command_is_refused_after_consent_without_running_anything(self):
        runner = RecordingRunner()
        with self.assertRaises(wa.ActivationError):
            wa.activate(["relative.exe"], run=runner, consent=True)
        self.assertEqual(runner.calls, [])

    def test_deactivation_needs_no_consent_flag(self):
        runner = RecordingRunner(query_stdout=_query_output())
        outcome = wa.deactivate([WORKER], run=runner)
        self.assertEqual(outcome["status"], "ok")
        self.assertIn("/Delete", runner.mutating[0])

    def test_deactivating_what_is_not_there_deletes_nothing(self):
        runner = RecordingRunner()
        self.assertEqual(wa.deactivate([WORKER], run=runner)["status"], "absent")
        self.assertEqual(runner.mutating, [])

    def test_a_failed_deactivation_is_reported(self):
        outcome = wa.deactivate([WORKER], run=RecordingRunner(
            returncode=1, query_stdout=_query_output()))
        self.assertEqual(outcome["status"], "failed")
        self.assertEqual(outcome["reason"], "unregister_failed")

    def test_activate_then_deactivate_targets_the_same_task(self):
        runner = RecordingRunner()
        wa.activate([WORKER], run=runner, consent=True, task_name="AgentBridge.T")
        runner.query_stdout = _query_output()
        wa.deactivate([WORKER], run=runner, task_name="AgentBridge.T")
        created = runner.mutating[0][runner.mutating[0].index("/TN") + 1]
        deleted = runner.mutating[1][runner.mutating[1].index("/TN") + 1]
        self.assertEqual(created, deleted)


class OwnershipTests(unittest.TestCase):
    """``/F`` overwrites and deletes. Neither may happen on someone else's task."""

    def test_a_task_running_another_program_is_never_overwritten(self):
        runner = RecordingRunner(
            query_stdout=_query_output(action="C:\\Windows\\System32\\calc.exe"))
        outcome = wa.activate([WORKER], run=runner, consent=True)
        self.assertEqual(outcome["status"], "refused")
        self.assertEqual(outcome["reason"], "task_not_owned_by_agent_bridge")
        self.assertEqual(runner.mutating, [])

    def test_a_task_running_another_program_is_never_deleted(self):
        runner = RecordingRunner(
            query_stdout=_query_output(action="C:\\Windows\\System32\\calc.exe"))
        outcome = wa.deactivate([WORKER], run=runner)
        self.assertEqual(outcome["reason"], "task_not_owned_by_agent_bridge")
        self.assertEqual(runner.mutating, [])

    def test_a_registered_task_that_will_not_say_what_it_runs_is_left_alone(self):
        runner = RecordingRunner(query_stdout=b"Status: Ready\r\n")
        outcome = wa.activate([WORKER], run=runner, consent=True)
        self.assertEqual(outcome["reason"], "task_action_unreadable")
        self.assertEqual(runner.mutating, [])

    def test_the_same_worker_with_different_flags_is_still_ours(self):
        runner = RecordingRunner(
            query_stdout=_query_output(action=f'"{WORKER}" --worker --once'))
        outcome = wa.activate([WORKER, "--worker"], run=runner, consent=True)
        self.assertEqual(outcome["status"], "ok")
        self.assertIn("/Create", runner.mutating[0])

    def test_ownership_is_decided_case_insensitively_like_windows_paths(self):
        runner = RecordingRunner(query_stdout=_query_output(action=WORKER.upper()))
        self.assertEqual(wa.activate([WORKER], run=runner,
                                     consent=True)["status"], "ok")

    def test_a_quoted_action_with_spaces_parses_back_to_the_program(self):
        self.assertEqual(wa.action_executable('"C:\\a b\\w.exe" --x'),
                         "C:\\a b\\w.exe")

    def test_an_unterminated_quote_parses_to_nothing_rather_than_a_guess(self):
        self.assertEqual(wa.action_executable('"C:\\a b\\w.exe --x'), "")

    def test_a_drive_letter_in_the_action_survives_status_parsing(self):
        status = wa.parse_status(0, _query_output())
        self.assertEqual(status.action, WORKER)

    def test_an_absent_task_blocks_nothing(self):
        self.assertEqual(wa.proves_ownership(wa.ActivationStatus(False),
                                             [WORKER]), "")


class HonestyTests(unittest.TestCase):
    def test_the_module_states_that_nothing_was_validated_on_windows(self):
        self.assertIn("live Windows host", wa.NO_LIVE_VALIDATION)

    def test_the_module_needs_no_windows_only_import(self):
        source = (ROOT / "src/agent_bridge/orchestration/windows_activation.py").read_text(
            encoding="utf-8")
        for forbidden in ("import winreg", "import ctypes", "import fcntl"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
