"""The verification-command policy shared by both harnesses and the queue."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.execution import claude_task, codex_task, verify_policy
from agent_bridge.execution.verify_policy import VerifyPolicyError, check_verify_argv


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


if __name__ == "__main__":
    unittest.main()
