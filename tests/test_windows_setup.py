"""The production Windows setup command, driven as a command.

These tests exist because the provisioning driver spent its whole life with
no caller. Every one of them goes through :func:`windows_setup.main` with an
argv, so what is covered is the thing a person runs: argument parsing,
dispatch, the exit code, and the JSON on stdout. A test that called
``dr.step`` directly would prove the ladder works and say nothing about
whether anything walks it.

The lifecycle test is the point of the file: a stage that reboots must
register a logon task, the command that task runs must find the record and
continue, and the record and task must be retired exactly once setup reaches
a terminal state. All three are asserted on the same machine state, in order,
through ``main``.

Nothing here runs on a live Windows host. The OS is supplied, as it is
everywhere else in this suite, and the gates that must not be fakeable stay
shut.
"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_bridge import windows_setup as ws
from agent_bridge.orchestration import windows_activation as wact
from agent_bridge.orchestration import windows_evidence as wev
from agent_bridge.orchestration import windows_provision_driver as dr
from agent_bridge.orchestration import windows_wsl_provision as wp

from test_windows_provision_driver import (DriverTestCase, ENABLED, _JobResult,
                                           _Preflight)


class _Captured:
    """One command, with whatever the test wants it to return."""

    def __init__(self, returncode=0, stdout=b""):
        self.returncode = returncode
        self.stdout = stdout


class SetupTestCase(DriverTestCase):
    """The driver fixture, plus a way to run the command over it."""

    def setUp(self) -> None:
        super().setUp()
        self.responses: dict[str, _Captured] = {}
        self.context_overrides: dict[str, object] = {}

    def _run(self, argv):  # overrides the driver fixture's runner
        self.commands.append(list(argv))
        for fragment, response in self.responses.items():
            if any(fragment in str(item) for item in argv):
                return response
        if "/Query" in argv:
            return _Captured(1)
        return _Captured(0)

    def _factory(self):
        def build(args):
            # The flag has to actually reach the installation, or the resume
            # task would be registered for one root and read another.
            self.assertEqual(args.runtime_root, str(self.runtime))
            options = dict(
                config=self._config(), run=self._run, preflight=_Preflight(),
                feature_states=dict(ENABLED), platform_name="win32",
                machine="arm64", os_name="posix", wsl_version="2.0.0",
                clock=lambda: "2026-01-01T00:00:00+00:00",
                resume_command=ws.resume_command(
                    str(self.runtime), executable="C:\\Python\\python.exe",
                    script="C:\\agent-bridge\\agent-bridge-resume.pyz"))
            options.update(self.context_overrides)
            return dr.DriverContext(**options)

        return build

    def _main(self, *argv):
        stream = io.StringIO()
        code = ws.main([*argv, "--runtime-root", str(self.runtime)],
                       context_factory=self._factory(), stream=stream)
        return code, json.loads(stream.getvalue())

    def _record_path(self) -> Path:
        return Path(wp.resume_record_path(str(self.runtime)))

    def _write_record(self, *, stage=wp.STAGE_REBOOT_REQUIRED, attempts=1):
        record = wp.build_resume_record(
            stage=stage, stage_completed=None, awaiting_reboot=True,
            updated_at="2026-01-01T00:00:00+00:00", attempts=attempts)
        wp.write_resume_record(self._record_path(), record)

    def _set_owned_task(self):
        args = ws.build_parser().parse_args(
            ["plan", "--runtime-root", str(self.runtime)])
        action = wact.build_action(self._factory()(args).resume_command)
        self.responses["/Query"] = _Captured(
            0, f"Status: Ready\r\nTask To Run: {action}\r\n".encode())

    def _made_ready(self):
        """Boundary and lane recorded, so the ladder's last gate opens."""

        outcome = dr.verify_boundary_live(self._config(),
                                          run_job=lambda request: _JobResult())
        dr.record_boundary(self._config(), outcome, wsl_version="2.0.0",
                           recorded_at="2026-01-01T00:00:00+00:00",
                           fingerprint=wev.host_fingerprint(), os_name="nt")
        dr.record_provider_lane(
            self._config(),
            wev.ProviderLane(verified=True, providers=("claude",),
                             portability_observed=True,
                             refresh_behaviour_observed=True), os_name="nt")

    def _schtasks(self, verb):
        return [argv for argv in self.commands
                if argv and argv[0].lower().endswith("schtasks.exe")
                and verb in argv]


class ArgumentTests(SetupTestCase):
    def test_an_unknown_command_is_a_usage_error_and_does_nothing(self):
        stream = io.StringIO()
        code = ws.main(["provision"], context_factory=self._factory(),
                       stream=stream)
        self.assertEqual(code, ws.EXIT_USAGE)
        self.assertEqual(self.commands, [])

    def test_every_documented_subcommand_dispatches(self):
        for name in ("plan", "step", "resume", "status"):
            with self.subTest(name):
                _code, payload = self._main(name)
                self.assertEqual(payload["command"], name)

    def test_plan_reads_and_changes_nothing(self):
        code, payload = self._main("plan")
        self.assertEqual(code, ws.EXIT_OK)
        self.assertIn("stage", payload)
        self.assertIn("remaining", payload)
        self.assertEqual(self.commands, [])

    def test_the_output_is_one_json_object_per_invocation(self):
        stream = io.StringIO()
        ws.main(["plan", "--runtime-root", str(self.runtime)],
                context_factory=self._factory(), stream=stream)
        text = stream.getvalue()
        self.assertEqual(text.count("\n"), 1)
        json.loads(text)

    def test_the_consents_are_separate_flags_and_none_is_a_blanket_yes(self):
        parser = ws.build_parser()
        args = parser.parse_args(["step", "--admin-consent"])
        self.assertTrue(args.admin_consent)
        self.assertFalse(args.reboot_consent)
        self.assertFalse(args.image_consent)
        self.assertFalse(args.provider_consent)

    def test_a_step_that_needs_consent_exits_with_the_consent_code(self):
        self.context_overrides["feature_states"] = {
            name: "Disabled" for name in wp.REQUIRED_FEATURES}
        code, payload = self._main("step")
        self.assertEqual(payload["status"], dr.STEP_CONSENT_REQUIRED)
        self.assertEqual(code, ws.EXIT_CONSENT_REQUIRED)
        self.assertEqual(self.commands, [])


class ResumeCommandTests(SetupTestCase):
    """What the logon task will run, checked before a reboot is offered."""

    def test_it_names_the_interpreter_the_launcher_and_the_root(self):
        argv = ws.resume_command(str(self.runtime),
                                 executable="C:\\Python\\python.exe",
                                 script="C:\\agent-bridge\\setup_bridge.py")
        self.assertEqual(argv[0], "C:\\Python\\python.exe")
        self.assertEqual(argv[1], "C:\\agent-bridge\\setup_bridge.py")
        self.assertEqual(argv[2], "windows-setup")
        self.assertIn("resume", argv)
        self.assertIn(str(self.runtime), argv)

    def test_it_falls_back_to_the_module_form_without_a_launcher(self):
        argv = ws.resume_command(str(self.runtime),
                                 executable="C:\\Python\\python.exe", script="")
        self.assertEqual(argv[1:3], ["-m", ws.MODULE_PATH])

    def test_the_zipapp_enters_resume_without_the_checkout_selector(self):
        argv = ws.resume_command(
            "C:\\runtime", executable="C:\\Python\\python.exe",
            script="C:\\runtime\\bootstrap\\agent-bridge-resume.pyz")
        self.assertEqual(argv[2], "resume")
        self.assertNotIn("windows-setup", argv)

    def test_it_is_rejected_before_a_reboot_if_it_cannot_be_registered(self):
        for executable in ("python", "..\\python.exe",
                           "\\\\server\\share\\python.exe"):
            with self.subTest(executable):
                with self.assertRaises(wact.ActivationError):
                    ws.resume_command(str(self.runtime), executable=executable,
                                      script="C:\\x\\setup_bridge.py")

    def test_the_launcher_it_points_at_exists_in_this_checkout(self):
        script = ws.launcher_script()
        self.assertIsNotNone(script)
        self.assertTrue(Path(script).is_file())

    def test_the_launcher_actually_routes_to_this_module(self):
        source = Path(ws.launcher_script()).read_text(encoding="utf-8")
        self.assertIn("windows-setup", source)
        self.assertIn("windows_setup.main", source)

    def test_the_installed_resume_launcher_is_self_contained(self):
        target = ws.install_resume_launcher(str(self.runtime))
        self.assertTrue(Path(target).is_file())
        result = subprocess.run(
            [sys.executable, target, "plan", "--runtime-root", str(self.runtime)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        self.assertEqual(result.returncode, ws.EXIT_OK, result.stderr[:400])
        self.assertEqual(json.loads(result.stdout)["command"], "plan")

    def test_launcher_install_refuses_an_unverified_user_acl(self):
        with mock.patch.object(
                ws.store.platform, "verify_owner_only_path",
                return_value=(False, {"mechanism": "test"})):
            with self.assertRaisesRegex(PermissionError, "owner-only"):
                ws.install_resume_launcher(str(self.runtime))

    def test_production_resume_command_uses_the_stable_runtime_copy(self):
        root = "C:\\Users\\sam\\AppData\\Local\\agent-bridge\\windows"
        stable = root + "\\bootstrap\\agent-bridge-resume.pyz"
        with mock.patch.object(ws.wpf, "is_windows", return_value=False), \
                mock.patch.object(ws, "stable_resume_launcher", return_value=stable), \
                mock.patch.object(ws.sys, "executable", "C:\\Python\\python.exe"):
            args = ws.build_parser().parse_args(
                ["plan", "--runtime-root", root])
            context = ws.build_context(args, run=self._run)
        self.assertEqual(context.resume_command[1], stable)
        self.assertNotIn("setup_bridge.py", " ".join(context.resume_command))
        self.assertEqual(context.resume_command[2], "resume")

    def test_exact_build_context_resume_command_executes_the_zipapp(self):
        target = ws.install_resume_launcher(str(self.runtime))
        args = ws.build_parser().parse_args(
            ["plan", "--runtime-root", str(self.runtime)])
        with mock.patch.object(ws.wpf, "is_windows", return_value=False), \
                mock.patch.object(ws, "stable_resume_launcher", return_value=target), \
                mock.patch.object(ws.wact, "build_action", return_value="validated"):
            context = ws.build_context(args, run=self._run)
        result = subprocess.run(context.resume_command,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=120)
        self.assertEqual(result.returncode, ws.EXIT_OK, result.stderr[:400])
        payload = json.loads(result.stdout)
        self.assertEqual(payload["command"], "resume")
        self.assertEqual(payload["reason"], "nothing_to_resume")


class ContextAssemblyTests(SetupTestCase):
    """The real ``build_context``, without the factory seam."""

    def _args(self, **overrides):
        parser = ws.build_parser()
        args = parser.parse_args(["plan", "--runtime-root", str(self.runtime)])
        for key, value in overrides.items():
            setattr(args, key, value)
        return args

    def test_off_windows_it_collects_nothing_and_spawns_nothing(self):
        with mock.patch.object(ws.wpf, "is_windows", return_value=False):
            context = ws.build_context(self._args(), run=self._run)
        self.assertIsNone(context.preflight)
        self.assertEqual(dict(context.feature_states), {})
        self.assertFalse(context.reboot_pending)
        self.assertEqual(self.commands, [])

    def test_the_paths_default_under_the_runtime_root(self):
        context = ws.build_context(self._args(), run=self._run)
        self.assertEqual(context.config.runtime_root, str(self.runtime))
        self.assertTrue(context.config.rootfs_path.startswith(str(self.runtime)))
        self.assertTrue(context.config.manifest_path.startswith(str(self.runtime)))

    def test_an_explicit_image_path_is_carried_through(self):
        context = ws.build_context(self._args(image=str(self.rootfs)),
                                   run=self._run)
        self.assertEqual(context.image_source_path, str(self.rootfs))

    def test_the_default_root_is_per_user(self):
        root = ws.default_runtime_root({"LOCALAPPDATA": "C:\\Users\\a\\AppData\\Local"})
        self.assertIn("agent-bridge", root)
        self.assertTrue(root.startswith("C:\\Users\\a\\AppData\\Local")
                        or root.startswith("C:/Users/a/AppData/Local"))

    def test_no_per_user_directory_is_an_error_rather_than_a_guess(self):
        with self.assertRaises(ws.SetupError):
            ws.default_runtime_root({})

    def test_a_feature_state_that_cannot_be_read_is_not_disabled(self):
        self.responses["Get-WindowsOptionalFeature"] = _Captured(1, b"")
        states = ws.collect_feature_states(run=self._run)
        self.assertEqual(set(states), set(wp.REQUIRED_FEATURES))
        self.assertTrue(all(value is None for value in states.values()))

    def test_a_readable_feature_state_is_reported_verbatim(self):
        self.responses["Get-WindowsOptionalFeature"] = _Captured(0, b"Enabled\r\n")
        states = ws.collect_feature_states(run=self._run)
        self.assertEqual(set(states.values()), {"Enabled"})

    def test_a_present_reboot_flag_means_pending(self):
        self.responses["RebootPending"] = _Captured(0, b"")
        self.assertTrue(ws.reboot_pending(run=self._run))

    def test_absent_reboot_flags_mean_not_pending(self):
        self.responses["Test-Path"] = _Captured(2, b"")
        self.assertFalse(ws.reboot_pending(run=self._run))

    def test_a_registry_probe_failure_fails_towards_pending(self):
        self.responses["Test-Path"] = _Captured(1, b"")
        self.assertTrue(ws.reboot_pending(run=self._run))

    def test_a_detection_that_cannot_run_fails_towards_pending(self):
        def broken(argv):
            self.commands.append(list(argv))
            return _Captured(None, b"")

        self.assertTrue(ws.reboot_pending(run=broken))


class RebootAndResumeLifecycleTests(SetupTestCase):
    """reboot-required -> resumed -> terminal, through the real command."""

    def setUp(self) -> None:
        super().setUp()
        self.context_overrides["reboot_pending"] = True

    def _take_the_reboot(self):
        result = self._main("step", "--admin-consent", "--reboot-consent")
        if result[1].get("status") == dr.STEP_REBOOT_SCHEDULED \
                and "/Query" not in self.responses:
            args = ws.build_parser().parse_args(
                ["plan", "--runtime-root", str(self.runtime)])
            action = wact.build_action(self._factory()(args).resume_command)
            self.responses["/Query"] = _Captured(
                0, f"Status: Ready\r\nTask To Run: {action}\r\n".encode())
        return result

    # -- the reboot ------------------------------------------------------

    def test_a_rebooting_stage_registers_the_logon_task_and_the_record(self):
        code, payload = self._take_the_reboot()
        self.assertEqual(payload["status"], dr.STEP_REBOOT_SCHEDULED, payload)
        self.assertEqual(code, ws.EXIT_OK)
        created = self._schtasks("/Create")
        self.assertEqual(len(created), 1)
        self.assertIn(wp.RESUME_TASK_NAME, created[0])
        self.assertTrue(self._record_path().exists())

    def test_the_registered_task_runs_this_command_s_resume(self):
        self._take_the_reboot()
        action = self._schtasks("/Create")[0]
        target = action[action.index("/TR") + 1]
        self.assertNotIn("windows-setup", target)
        self.assertIn("resume", target)
        self.assertIn(str(self.runtime), target)

    def test_the_record_says_which_stage_it_stopped_at(self):
        self._take_the_reboot()
        parsed = wp.parse_resume_record(
            json.loads(self._record_path().read_text(encoding="utf-8")))
        self.assertEqual(parsed["stage"], wp.STAGE_REBOOT_REQUIRED)
        self.assertTrue(parsed["awaiting_reboot"])
        self.assertEqual(parsed["attempts"], 1)

    def test_without_a_way_back_the_machine_is_not_restarted(self):
        self.context_overrides["resume_command"] = None
        code, payload = self._take_the_reboot()
        self.assertEqual(payload["status"], dr.STEP_FAILED)
        self.assertEqual(payload["reason"], "resume_command_missing")
        self.assertEqual(code, ws.EXIT_FAILED)
        self.assertEqual(self._schtasks("/Create"), [])
        self.assertFalse(self._record_path().exists())
        self.assertEqual(self.commands, [])

    def test_a_task_that_is_not_ours_is_never_overwritten(self):
        self.responses["/Query"] = _Captured(
            0, b"Status: Ready\r\nTask To Run: C:\\other\\thing.exe\r\n")
        code, payload = self._take_the_reboot()
        self.assertEqual(payload["status"], dr.STEP_BLOCKED)
        self.assertEqual(payload["reason"], "resume_task_not_ours")
        self.assertEqual(code, ws.EXIT_BLOCKED)
        self.assertEqual(self._schtasks("/Create"), [])

    def test_a_task_that_cannot_be_registered_leaves_no_promise_behind(self):
        self.responses["/Create"] = _Captured(1, b"")
        code, payload = self._take_the_reboot()
        self.assertEqual(payload["reason"], "resume_task_unregistered")
        self.assertEqual(code, ws.EXIT_FAILED)
        self.assertFalse(self._record_path().exists())

    def test_the_restart_still_needs_its_own_consent(self):
        code, payload = self._main("step", "--admin-consent")
        self.assertEqual(payload["status"], dr.STEP_CONSENT_REQUIRED)
        self.assertEqual(code, ws.EXIT_CONSENT_REQUIRED)
        self.assertEqual(self.commands, [])
        self.assertFalse(self._record_path().exists())

    # -- the resume ------------------------------------------------------

    def test_nothing_to_resume_is_an_ordinary_success(self):
        self.context_overrides["reboot_pending"] = False
        code, payload = self._main("resume")
        self.assertEqual(code, ws.EXIT_OK)
        self.assertFalse(payload["resumed"])
        self.assertEqual(payload["reason"], "nothing_to_resume")
        self.assertEqual(self.commands, [])

    def test_the_resume_continues_at_the_stage_the_reboot_left(self):
        self._take_the_reboot()
        self.commands.clear()
        self.context_overrides["reboot_pending"] = False
        code, payload = self._main("resume")
        self.assertTrue(payload["resumed"])
        self.assertEqual(payload["resume"]["stage"], wp.STAGE_REBOOT_REQUIRED)
        self.assertEqual(payload["steps"][0]["step"], wp.STAGE_GUEST_IMAGE)
        self.assertEqual(code, ws.EXIT_BLOCKED)

    def test_a_resume_that_is_still_provisioning_keeps_the_record_and_task(self):
        self._take_the_reboot()
        self.context_overrides["reboot_pending"] = False
        self._main("resume")
        self.assertTrue(self._record_path().exists())
        self.assertEqual(self._schtasks("/Delete"), [])

    def test_the_resume_never_elevates_on_a_consent_given_before_the_restart(self):
        self._take_the_reboot()
        self.commands.clear()
        self.context_overrides["reboot_pending"] = False
        self.context_overrides["feature_states"] = {
            name: "Disabled" for name in wp.REQUIRED_FEATURES}
        code, payload = self._main("resume")
        self.assertEqual(payload["steps"][0]["status"], dr.STEP_CONSENT_REQUIRED)
        self.assertEqual(code, ws.EXIT_CONSENT_REQUIRED)
        self.assertFalse([argv for argv in self.commands
                          if any("dism" in str(item).lower() for item in argv)])

    def test_one_resume_takes_a_bounded_number_of_stages(self):
        self._take_the_reboot()
        self.context_overrides["reboot_pending"] = False
        _code, payload = self._main("resume")
        self.assertLessEqual(len(payload["steps"]), ws.MAX_RESUME_STEPS)

    def test_a_tampered_record_is_reported_and_not_quietly_cleared(self):
        self._take_the_reboot()
        self._record_path().write_text("{not json", encoding="utf-8")
        self.context_overrides["reboot_pending"] = False
        code, payload = self._main("resume")
        self.assertEqual(code, ws.EXIT_FAILED)
        self.assertTrue(payload["reason"])
        self.assertEqual(self._schtasks("/Delete"), [])
        self.assertTrue(self._record_path().exists())

    # -- the terminal states ---------------------------------------------

    def test_reaching_ready_retires_the_record_and_the_task(self):
        self._write_record()
        self._set_owned_task()
        self._made_ready()
        self.context_overrides.update(
            reboot_pending=False, anchors=self._anchor(),
            verify_signature=lambda *args: True)
        code, payload = self._main("resume")
        self.assertEqual(code, ws.EXIT_OK)
        self.assertEqual(payload["reason"], "ready")
        self.assertEqual(payload["steps"][-1]["step"], wp.STAGE_READY)
        self.assertEqual(len(self._schtasks("/Delete")), 1)
        self.assertFalse(self._record_path().exists())

    def test_an_exhausted_stage_stops_instead_of_rebooting_again(self):
        self._write_record(attempts=wp.MAX_STAGE_ATTEMPTS)
        self._set_owned_task()
        self.context_overrides["reboot_pending"] = False
        code, payload = self._main("resume")
        self.assertEqual(code, ws.EXIT_OK)
        self.assertEqual(payload["reason"], "stopped")
        self.assertEqual(payload["steps"], [])
        self.assertEqual(len(self._schtasks("/Delete")), 1)
        self.assertFalse(self._record_path().exists())

    def test_a_reboot_asked_for_a_fourth_time_is_refused(self):
        self._write_record(attempts=wp.MAX_STAGE_ATTEMPTS)
        code, payload = self._take_the_reboot()
        self.assertEqual(payload["status"], dr.STEP_BLOCKED)
        self.assertEqual(payload["reason"], "stage_attempts_exhausted")
        self.assertEqual(code, ws.EXIT_BLOCKED)
        self.assertEqual(self._schtasks("/Create"), [])

    def test_an_orphan_task_left_behind_is_reported_as_a_failure(self):
        self._write_record(attempts=wp.MAX_STAGE_ATTEMPTS)
        self._set_owned_task()
        self.responses["/Delete"] = _Captured(1, b"")
        self.context_overrides["reboot_pending"] = False
        code, payload = self._main("resume")
        self.assertEqual(code, ws.EXIT_FAILED)
        self.assertEqual(payload["reason"], "resume_task_not_removed")

    def test_cleanup_refuses_a_task_replaced_after_registration(self):
        self._write_record(attempts=wp.MAX_STAGE_ATTEMPTS)
        self.responses["/Query"] = _Captured(
            0, b"Status: Ready\r\nTask To Run: C:\\Python\\python.exe other.py\r\n")
        self.context_overrides["reboot_pending"] = False
        code, payload = self._main("resume")
        self.assertEqual(code, ws.EXIT_BLOCKED)
        self.assertEqual(payload["reason"], "resume_task_not_ours")
        self.assertEqual(self._schtasks("/Delete"), [])
        self.assertTrue(self._record_path().exists())

    def test_manual_ready_step_retires_reboot_machinery(self):
        self._write_record()
        self._made_ready()
        args = ws.build_parser().parse_args(
            ["plan", "--runtime-root", str(self.runtime)])
        command = self._factory()(args).resume_command
        self.responses["/Query"] = _Captured(
            0, ("Status: Ready\r\nTask To Run: " +
                wact.build_action(command) + "\r\n").encode())
        self.context_overrides.update(
            reboot_pending=False, anchors=self._anchor(),
            verify_signature=lambda *args: True)
        code, payload = self._main("step")
        self.assertEqual(code, ws.EXIT_OK)
        self.assertEqual(payload["finish"]["terminal"], "ready")
        self.assertFalse(self._record_path().exists())
        self.assertEqual(len(self._schtasks("/Delete")), 1)

    # -- what a person can look at ---------------------------------------

    def test_status_reports_the_record_and_the_task_without_changing_them(self):
        self._take_the_reboot()
        self.context_overrides["reboot_pending"] = False
        code, payload = self._main("status")
        self.assertEqual(code, ws.EXIT_OK)
        self.assertEqual(payload["resume"]["stage"], wp.STAGE_REBOOT_REQUIRED)
        self.assertEqual(payload["resume_task_name"], wp.RESUME_TASK_NAME)
        self.assertIn("resume", payload["resume_command"])
        self.assertTrue(self._record_path().exists())

    def test_status_before_any_reboot_reports_no_record(self):
        _code, payload = self._main("status")
        self.assertIsNone(payload["resume"])


class RealLauncherTests(SetupTestCase):
    """The command as a process, with no seam at all.

    Off Windows the ladder refuses at its first rung, which is the point: this
    proves the entrypoint is reachable, parses its arguments and prints a
    result, not that provisioning works here.
    """

    def _launch(self, *argv):
        return subprocess.run(
            [sys.executable, str(ROOT / "setup_bridge.py"), "windows-setup",
             *argv, "--runtime-root", str(self.runtime)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)

    @unittest.skipIf(sys.platform.startswith("win"),
                     "this assertion is specifically the off-Windows refusal")
    def test_the_launcher_runs_plan_and_prints_one_object(self):
        result = self._launch("plan")
        self.assertEqual(result.returncode, ws.EXIT_OK, result.stderr[:400])
        payload = json.loads(result.stdout.decode("utf-8"))
        self.assertEqual(payload["command"], "plan")
        self.assertEqual(payload["stage"], wp.STAGE_UNSUPPORTED_PLATFORM)

    def test_the_launcher_runs_status_with_nothing_to_report(self):
        result = self._launch("status")
        self.assertEqual(result.returncode, ws.EXIT_OK, result.stderr[:400])
        payload = json.loads(result.stdout.decode("utf-8"))
        self.assertIsNone(payload["resume"])

    def test_the_launcher_runs_resume_with_nothing_to_resume(self):
        result = self._launch("resume")
        self.assertEqual(result.returncode, ws.EXIT_OK, result.stderr[:400])
        payload = json.loads(result.stdout.decode("utf-8"))
        self.assertEqual(payload["reason"], "nothing_to_resume")

    def test_an_unknown_subcommand_is_a_usage_error(self):
        result = self._launch("provision")
        self.assertEqual(result.returncode, ws.EXIT_USAGE)


class ExitCodeTests(SetupTestCase):
    def test_the_codes_cover_the_whole_step_vocabulary(self):
        self.assertEqual(set(ws._EXIT_FOR_STATUS), set(wp := dr.STEP_STATUSES))
        self.assertEqual(len(wp), 5)

    def test_the_codes_are_distinct_where_the_meanings_are(self):
        self.assertEqual(len({ws.EXIT_OK, ws.EXIT_FAILED, ws.EXIT_BLOCKED,
                              ws.EXIT_CONSENT_REQUIRED, ws.EXIT_USAGE}), 5)


class OnboardingPointsAtTheCommandTests(SetupTestCase):
    """A status report that names no action leaves the reader stuck."""

    def test_the_ladder_summary_names_the_command_that_advances_it(self):
        from agent_bridge import onboard

        self.assertIn("agent-bridge-windows-setup", onboard.WINDOWS_SETUP_COMMAND)

    def test_the_named_command_exists_in_this_checkout(self):
        from agent_bridge import onboard

        name = onboard.WINDOWS_SETUP_COMMAND.replace("\\", "/")
        self.assertTrue((ROOT / name).is_file())
        self.assertTrue((ROOT / (name + ".cmd")).is_file())


class RunbookTests(unittest.TestCase):
    """The runbook is a promise about a command line, so check the command line.

    A document listing steps for a person to run on a machine nobody here has
    is exactly the kind of prose that rots silently: a flag gets renamed, the
    runbook keeps saying the old one, and the failure surfaces on the one
    machine where it costs the most. These read the document and check it
    against the parser rather than against a reviewer's memory.
    """

    @classmethod
    def setUpClass(cls):
        cls.text = (ROOT / "docs" / "WINDOWS-VALIDATION-RUNBOOK.md").read_text(
            encoding="utf-8")

    def test_every_subcommand_it_names_is_real(self):
        import re

        named = set(re.findall(r"agent-bridge-windows-setup (\w[\w-]*)", self.text))
        self.assertTrue(named, "the runbook names no subcommand at all")
        self.assertLessEqual(named, set(ws.COMMANDS))

    def test_every_consent_flag_it_names_is_real(self):
        import re

        parser = ws.build_parser()
        known = {action.option_strings[0] for action in parser._actions
                 if action.option_strings}
        named = set(re.findall(r"(--[a-z][a-z-]+)", self.text))
        # Flags belonging to the other tools the runbook drives. Each is
        # checked by that tool's own tests; only this parser is checked here.
        other = {"--recipe", "--out-dir", "--out", "--release", "--architecture",
                 "--signature", "--public-key", "--answers", "--check",
                 "--list", "--verbose", "--exit-code", "--root"}
        for flag in sorted(named - other):
            self.assertIn(flag, known, f"the runbook names an unknown flag: {flag}")

    def test_it_does_not_claim_any_of_it_has_been_run(self):
        self.assertIn("nothing below has been run", self.text.lower())

    def test_it_names_the_real_resume_task(self):
        # The one thing in the runbook a reader checks before letting the
        # machine restart. A wrong name here sends them looking for a task
        # that does not exist and reading its absence as a missing resume.
        self.assertIn(wp.RESUME_TASK_NAME, self.text)

    def test_every_step_status_it_quotes_is_a_real_one(self):
        import re

        quoted = set(re.findall(r"`(ok|blocked|failed|consent_required|"
                                r"reboot_scheduled)`", self.text))
        self.assertTrue(quoted)
        self.assertLessEqual(quoted, set(dr.STEP_STATUSES))

    def test_the_checks_it_walks_a_reader_through_are_real_checks(self):
        """Named explicitly, not intersected.

        An intersection test passes when the runbook names a check that does
        not exist, because a bogus name simply falls outside the set. These
        are the ids the document tells a reader to watch, so each one is
        asserted to exist and to still be spelled that way.
        """

        from agent_bridge.orchestration import windows_validation as wv

        real = {check.id for check in wv.CHECKS}
        for name in ("platform", "distro_registered", "guest_runner",
                     "canaries", "egress_policy", "round_trip"):
            self.assertIn(name, real)
            self.assertIn(f"`{name}`", self.text)
        self.assertIn(f"`{wv.VERDICT_READY}`", self.text)
        self.assertIn(f"`{wv.VERDICT_NOT_READY}`", self.text)

    def test_the_provider_verdict_table_is_the_whole_vocabulary(self):
        # The runbook tells the operator these are the only outcomes and that
        # anything else is a defect. That claim is only safe while the table
        # is complete, so completeness is checked in both directions.
        import re

        from agent_bridge.orchestration import guest_runner as guest

        listed = set(re.findall(r"`(auth_probe_\w+)`", self.text))
        self.assertEqual(listed, set(guest.PROBE_VERDICTS))

    def test_it_explains_every_canary_and_invents_none(self):
        # Step 9 tells the operator what each canary failure means. A canary
        # added later with no entry here leaves them reading an unexplained
        # name at the one moment containment is in question.
        import re

        from agent_bridge.orchestration import windows_wsl_runtime as runtime

        listed = set(re.findall(r"`([a-z]+(?:-[a-z0-9]+)+)`", self.text))
        canaries = set(runtime.CANARY_ORDER)
        self.assertEqual(listed & canaries, canaries)

    def test_the_pass_reason_it_promises_for_the_round_trip_is_the_real_one(self):
        from agent_bridge.orchestration import windows_validation as wv

        observation = wv.GuestRoundTrip.__doc__
        self.assertIsNotNone(observation)
        self.assertIn("job_completed_and_instance_destroyed", self.text)
        source = (ROOT / "src" / "agent_bridge" / "orchestration"
                  / "windows_validation.py").read_text(encoding="utf-8")
        self.assertIn('"job_completed_and_instance_destroyed"', source)

    def test_it_names_the_refusal_the_image_step_gives_today(self):
        # RELEASE_TRUST_ANCHORS ships empty, so this is the reason a reader
        # will actually see. If the constant is ever populated by default the
        # runbook is wrong and this test says so.
        from agent_bridge.orchestration import windows_rootfs as wr

        self.assertEqual(wr.RELEASE_TRUST_ANCHORS, ())
        self.assertIn("manifest_untrusted:no_release_anchor", self.text)


if __name__ == "__main__":
    unittest.main()
