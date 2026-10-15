"""Focused offline checks for Windows setup mutation survivors."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge import windows_setup as ws
from agent_bridge.orchestration import windows_provision_driver as dr
from agent_bridge.orchestration import windows_validation as wv


class RunCommandTests(unittest.TestCase):
    def test_command_uses_the_declared_timeout(self):
        with mock.patch.object(ws.subprocess, "run") as run:
            ws.run_command(["C:\\Tools\\probe.exe"])
        self.assertEqual(run.call_args.kwargs["timeout"],
                         ws.COMMAND_TIMEOUT_SECONDS)


class WslVersionTests(unittest.TestCase):
    def test_collected_version_is_returned(self):
        preflight = SimpleNamespace(wsl_version=SimpleNamespace(value="2.3.4"))
        self.assertEqual(ws._wsl_version(preflight), "2.3.4")


class CommandStepTests(unittest.TestCase):
    def setUp(self):
        self.context = SimpleNamespace(
            config=SimpleNamespace(runtime_root="C:\\Users\\Example\\AppData\\Local\\bridge"),
            resume_command=["C:\\Python\\python.exe", "C:\\bridge\\resume.pyz"])

    def test_launcher_install_requires_both_reboot_consents(self):
        args = argparse.Namespace(admin_consent=True, reboot_consent=False,
                                  image_consent=False, provider_consent=False)
        with mock.patch.object(ws, "install_resume_launcher") as install, \
                mock.patch.object(ws.dr, "step", return_value={
                    "status": dr.STEP_BLOCKED, "step": "feature", "reason": "waiting"}):
            ws.command_step(self.context, args)
        install.assert_not_called()

    def test_launcher_is_installed_when_both_consents_are_given(self):
        args = argparse.Namespace(admin_consent=True, reboot_consent=True,
                                  image_consent=False, provider_consent=False)
        with mock.patch.object(ws, "install_resume_launcher") as install, \
                mock.patch.object(ws.dr, "step", return_value={
                    "status": dr.STEP_BLOCKED, "step": "feature", "reason": "waiting"}):
            ws.command_step(self.context, args)
        install.assert_called_once_with(self.context.config.runtime_root)


class CommandStatusTests(unittest.TestCase):
    def test_successful_task_query_reports_registration(self):
        context = SimpleNamespace(run=lambda argv: SimpleNamespace(returncode=0),
                                  resume_command=None)
        with mock.patch.object(ws.dr, "resume", return_value=None):
            payload, code = ws.command_status(context, argparse.Namespace())
        self.assertEqual(code, ws.EXIT_OK)
        self.assertTrue(payload["resume_task_registered"])


class CommandValidateTests(unittest.TestCase):
    def test_ready_verdict_exits_successfully(self):
        report = {"verdict": wv.VERDICT_READY, "envelope": {}}
        with mock.patch.object(ws, "_validation_runners", return_value={}), \
                mock.patch.object(ws.wv, "run_checks", return_value=[]), \
                mock.patch.object(ws.wv, "build_report", return_value=report):
            payload, code = ws.command_validate(SimpleNamespace(),
                                                argparse.Namespace(report=None))
        self.assertEqual(payload["verdict"], wv.VERDICT_READY)
        self.assertEqual(code, ws.EXIT_OK)


class ValidationRunnersTests(unittest.TestCase):
    def test_valid_manifest_supplies_rootfs_to_image_check(self):
        config = SimpleNamespace(manifest_path="C:\\bridge\\manifest.json",
                                 rootfs_path="C:\\bridge\\rootfs.tar",
                                 runtime_root="C:\\bridge")
        context = SimpleNamespace(config=config, run=lambda argv: None,
                                  platform_name="win32", feature_states={},
                                  reboot_pending=False)
        with mock.patch.object(ws.wd, "load_manifest", return_value={"name": "Example"}), \
                mock.patch.object(ws.wv, "default_runners", return_value={}) as runners:
            ws._validation_runners(context)
        self.assertEqual(runners.call_args.kwargs["rootfs_path"], config.rootfs_path)


class NowTests(unittest.TestCase):
    def test_timestamp_omits_fractional_seconds(self):
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2031, 2, 3, 4, 5, 6, 654321, tzinfo=timezone.utc)

        with mock.patch("datetime.datetime", FixedDateTime):
            self.assertEqual(ws._now(), "2031-02-03T04:05:06+00:00")


class ParserTests(unittest.TestCase):
    def test_report_path_defaults_to_none(self):
        args = ws.build_parser().parse_args(["validate"])
        self.assertIsNone(args.report)


class CommandResumeTests(unittest.TestCase):
    def test_resume_stops_after_eight_successful_nonterminal_steps(self):
        state = SimpleNamespace(reason="", exhausted=False,
                                as_dict=lambda: {"stage": "example"})
        step = {"status": dr.STEP_OK, "step": "not_ready"}
        finish = {"status": dr.STEP_BLOCKED, "reason": "still_provisioning"}
        with mock.patch.object(ws.dr, "resume", return_value=state), \
                mock.patch.object(ws.dr, "step", return_value=step) as advance, \
                mock.patch.object(ws.dr, "finish_resume", return_value=finish):
            payload, _code = ws.command_resume(SimpleNamespace(), argparse.Namespace())
        self.assertEqual(len(payload["steps"]), ws.MAX_RESUME_STEPS)
        self.assertEqual(advance.call_count, ws.MAX_RESUME_STEPS)


if __name__ == "__main__":
    unittest.main()
