"""Regression tests for behavior missed by the provisioning tests."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge.orchestration import windows_wsl_provision as wp


class ClearResumeRecordTests(unittest.TestCase):
    def test_unlink_failure_reports_resume_not_removable(self):
        with mock.patch.object(Path, "unlink", side_effect=PermissionError("locked")):
            with self.assertRaises(wp.ResumeError) as caught:
                wp.clear_resume_record("C:\\Users\\sample\\provision-resume.json")
        self.assertEqual(caught.exception.reason, "resume_not_removable")


class StageAsDictTests(unittest.TestCase):
    def test_stage_serializes_its_commands_and_flags(self):
        stage = wp.Stage("sample", "Sample", "Sample detail", wp.ACTOR_INSTALLER,
                         False, False, (("C:\\Windows\\System32\\sample.exe", "--check"),))
        self.assertEqual(stage.as_dict(), {
            "stage": "sample", "title": "Sample", "detail": "Sample detail",
            "actor": wp.ACTOR_INSTALLER, "requires_admin": False,
            "reboots": False,
            "commands": [["C:\\Windows\\System32\\sample.exe", "--check"]],
            "reversible": True,
        })


class PlanBlockedOnUserTests(unittest.TestCase):
    def test_unsupported_platform_is_blocked_on_user(self):
        self.assertTrue(wp.plan(wp.ProvisionState())["blocked_on_user"])


class WriteResumeRecordTests(unittest.TestCase):
    def test_resume_record_has_two_space_indentation(self):
        record = wp.build_resume_record(
            stage=wp.STAGE_WSL_KERNEL, stage_completed=None,
            awaiting_reboot=False, updated_at="2026-01-02T03:04:05Z")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / wp.RESUME_RECORD_NAME
            wp.write_resume_record(str(path), record)
            lines = path.read_text(encoding="utf-8").splitlines()
        self.assertTrue(lines[1].startswith('  "'))


class RegisterResumeArgvTests(unittest.TestCase):
    def test_empty_command_has_a_direct_validation_error(self):
        with self.assertRaisesRegex(ValueError, "^resume command must not be empty$"):
            wp.register_resume_argv([])


class ObserveRecordTests(unittest.TestCase):
    def test_uncollected_preflight_checks_are_recorded(self):
        _, evidence = wp.observe(platform_name="win32")
        for name in ("windows_build", "firmware_virtualization", "wsl_version"):
            self.assertIn((name, None, "not collected"), evidence.checks)


if __name__ == "__main__":
    unittest.main()
