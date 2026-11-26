"""The verification-command policy shared by both harnesses and the queue."""

import os
import json
import sys
import tempfile
from pathlib import Path
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.execution import claude_task, codex_task, verify_policy
from agent_bridge.execution.verify_policy import VerifyPolicyError, check_verify_argv


class BareNameTests(unittest.TestCase):
    def test_paths_are_not_bare_names_on_either_platform(self):
        for program in ("tools/pytest", "tools\\pytest"):
            with self.subTest(program=program):
                self.assertFalse(verify_policy._bare_name(program))


class CheckVerifyArgvBoundaryTests(unittest.TestCase):
    def test_outer_container_must_be_a_list(self):
        with self.assertRaisesRegex(VerifyPolicyError, verify_policy.MESSAGE_SHAPE):
            check_verify_argv((["pytest"],))

    def test_python_module_boundary(self):
        for module in ("pytest", "unittest"):
            with self.subTest(module=module):
                command = ["python3", "-m", module]
                self.assertEqual(check_verify_argv([command]), [command])
        with self.assertRaisesRegex(VerifyPolicyError, verify_policy.MESSAGE_PYTHON):
            check_verify_argv([["python3", "-m"]])


class VerifyPolicyTests(unittest.TestCase):
    def test_the_repository_s_own_test_runner_is_permitted(self):
        # Job d8e5763d in the live queue asked for exactly this and failed as
        # a bare TaskError: the harness allowed only pytest.
        commands = [["python3", "-m", "unittest", "discover", "-s", "tests",
                     "-p", "test_execution_dispatcher.py"]]
        self.assertEqual(check_verify_argv(commands), commands)
        self.assertEqual(check_verify_argv([["python", "-m", "pytest", "-q"]]),
                         [["python", "-m", "pytest", "-q"]])

    def test_the_returned_commands_are_copies(self):
        commands = [["git", "status"]]
        checked = check_verify_argv(commands)
        checked[0].append("--porcelain")
        self.assertEqual(commands, [["git", "status"]])

    def test_refusals_carry_fixed_messages(self):
        cases = {
            "not a list": verify_policy.MESSAGE_SHAPE,
            "shape": verify_policy.MESSAGE_SHAPE,
            "sh": verify_policy.MESSAGE_PROGRAM,
            "path": verify_policy.MESSAGE_PROGRAM,
            "windows path": verify_policy.MESSAGE_PROGRAM,
            "python -c": verify_policy.MESSAGE_PYTHON,
            "python script": verify_policy.MESSAGE_PYTHON,
            "python -m other": verify_policy.MESSAGE_PYTHON,
            "git push": verify_policy.MESSAGE_GIT,
            "git alone": verify_policy.MESSAGE_GIT,
            "newline": verify_policy.MESSAGE_CONTROL,
        }
        inputs = {
            "not a list": "pytest",
            "shape": [["pytest", ""]],
            "sh": [["sh", "-c", "touch escaped"]],
            "path": [["/usr/bin/test", "-e", "x"]],
            "windows path": [["C:\\tools\\pytest.exe"]],
            "python -c": [["python3", "-c", "print(1)"]],
            "python script": [["python3", "setup.py", "test"]],
            "python -m other": [["python", "-m", "http.server"]],
            "git push": [["git", "push"]],
            "git alone": [["git"]],
            "newline": [["pytest", "-k", "a\nb"]],
        }
        for label, message in cases.items():
            with self.subTest(label), self.assertRaises(VerifyPolicyError) as caught:
                check_verify_argv(inputs[label])
            self.assertEqual(str(caught.exception), message)

    def test_emptiness_is_left_to_the_caller(self):
        self.assertEqual(check_verify_argv([]), [])

    def test_both_harnesses_use_this_policy_and_nothing_wider(self):
        for harness in (claude_task, codex_task):
            with self.subTest(harness.__name__):
                self.assertIs(harness.ALLOWED_VERIFY_PROGRAMS,
                              verify_policy.ALLOWED_VERIFY_PROGRAMS)
                with self.assertRaises(harness.TaskError) as caught:
                    harness._verify_argv([["python3", "-c", "print(1)"]])
                self.assertEqual(str(caught.exception), verify_policy.MESSAGE_PYTHON)
                unittest_run = [["python3", "-m", "unittest", "-q"]]
                self.assertEqual(harness._verify_argv(unittest_run), unittest_run)


class VerificationDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.path = self.repo / verify_policy.DEFAULT_FILE
        self.checks = [["python3", "-m", "unittest", "discover", "-s", "tests"]]

    def write(self, commands):
        self.path.write_text(json.dumps({"verify_argv": commands}), encoding="utf-8")

    def test_omitted_and_empty_commands_resolve_the_repo_default(self):
        self.write(self.checks)
        for value in (None, []):
            self.assertEqual(verify_policy.resolve_verification(self.repo, value), (self.checks, "repo", 300))

    def test_no_default_is_visible(self):
        self.assertEqual(verify_policy.resolve_verification(self.repo, None), ([], "none", 300))

    def test_explicit_commands_win_even_over_an_invalid_default_file(self):
        self.path.write_text("invalid", encoding="utf-8")
        self.assertEqual(verify_policy.resolve_verification(self.repo, self.checks), (self.checks, "explicit", 300))

    def test_repo_timeout_is_bounded_and_explicit_timeout_wins(self):
        for timeout in (0, 3601, True, "600"):
            self.path.write_text(json.dumps({"verify_argv": self.checks, "verify_timeout_seconds": timeout}))
            with self.assertRaisesRegex(VerifyPolicyError, "verification_default_invalid"):
                verify_policy.resolve_verification(self.repo, None)
        self.path.write_text(json.dumps({"verify_argv": self.checks, "verify_timeout_seconds": 1800}))
        self.assertEqual(verify_policy.resolve_verification(self.repo, None), (self.checks, "repo", 1800))
        self.assertEqual(verify_policy.resolve_verification(self.repo, None, timeout=60), (self.checks, "repo", 60))
        self.assertEqual(verify_policy.resolve_verification(self.repo, self.checks), (self.checks, "explicit", 300))

    def test_defaults_cannot_relax_the_command_policy_or_silently_disable_tests(self):
        for value in ([], [["sh", "-c", "exit 0"]], [["python3", "tests/test_suite.py"]],
                      [["python3", "-c", "print(1)"]], [["git", "push"]], [["pytest", "bad\narg"]]):
            with self.subTest(value=value):
                self.write(value)
                with self.assertRaisesRegex(VerifyPolicyError, "verification_default_invalid"):
                    verify_policy.resolve_verification(self.repo, None)
        for value in ("bad json", "[]", '{"verify_argv": [], "env": {}}', " " * (64 * 1024 + 1)):
            self.path.write_text(value, encoding="utf-8")
            with self.assertRaisesRegex(VerifyPolicyError, "verification_default_invalid"):
                verify_policy.resolve_verification(self.repo, None)

    def test_unreadable_default_is_refused(self):
        with mock.patch.object(Path, "open", side_effect=PermissionError):
            with self.assertRaisesRegex(VerifyPolicyError, "verification_default_unreadable"):
                verify_policy.resolve_verification(self.repo, None)

    def test_repository_defaults_are_policy_valid_and_do_not_duplicate_discovery(self):
        repo = Path(__file__).resolve().parents[1]
        checks, source, timeout = verify_policy.resolve_verification(repo, None)
        self.assertEqual(source, "repo")
        self.assertEqual(timeout, 1800)
        self.assertEqual(checks, [["python3", "-m", "unittest", "bridge_verify"],
                                  ["python3", "-m", "unittest", "discover", "-s", "tests"]])
        import bridge_verify
        completed = mock.Mock(returncode=0)
        with mock.patch.object(bridge_verify.subprocess, "run", return_value=completed) as run:
            bridge_verify.BrokerSuite("test_broker_suite").test_broker_suite()
        self.assertEqual(run.call_args.args[0], [sys.executable, str(repo / "tests" / "test_suite.py")])
        completed.returncode = 1
        with mock.patch.object(bridge_verify.subprocess, "run", return_value=completed), self.assertRaises(AssertionError):
            bridge_verify.BrokerSuite("test_broker_suite").test_broker_suite()

    def test_verification_environment_uses_portable_repo_paths_and_removes_pytest_injection(self):
        env = codex_task._verification_env({"PATH": "synthetic", "PYTEST_ADDOPTS": "--override", "PYTHONPATH": "outside"})
        self.assertEqual(env["PYTHONPATH"], os.pathsep.join(("src", "tests")))
        self.assertNotIn("PYTEST_ADDOPTS", env)


if __name__ == "__main__":
    unittest.main()
