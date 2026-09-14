"""Portable tests for guided Windows/WSL2 provisioning.

The planner is pure, so the whole ladder, including the admin and reboot
boundaries, is testable off Windows. What is not tested here is that DISM,
wsl.exe, schtasks and shutdown behave as documented on a real machine.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge.orchestration import windows_wsl as ww
from agent_bridge.orchestration import windows_wsl_provision as wp

SYSTEM32 = "C:\\Windows\\System32"


def _check(passed, detail="", value=None):
    return ww.PrerequisiteCheckResult(passed, detail, value)


def _preflight(**overrides):
    fields = dict(
        windows_build=_check(True, "build 26100", "26100"),
        wsl_version=_check(True, "wsl 2.3.26", "2.3.26"),
        virtual_machine_platform=_check(True, "Enabled", "Enabled"),
        firmware_virtualization=_check(True, "enabled", "Yes"),
        manifest=_check(True, "pinned", "a" * 64),
    )
    fields.update(overrides)
    return types.SimpleNamespace(**fields)


def _state(**overrides):
    fields = dict(
        is_windows=True,
        windows_build_ok=True,
        firmware_virtualization=True,
        features_enabled={name: True for name in wp.REQUIRED_FEATURES},
        reboot_pending=False,
        wsl_present=True,
        wsl_version_ok=True,
        guest_image_installed=True,
        boundary_verified=True,
        provider_lane_verified=True,
    )
    fields.update(overrides)
    return wp.ProvisionState(**fields)


class LadderOrderTests(unittest.TestCase):
    """Order matters: a later check is meaningless until the earlier ones hold."""

    def test_a_fully_provisioned_machine_is_ready(self):
        self.assertEqual(wp.current_stage(_state()), wp.STAGE_READY)
        self.assertTrue(wp.delegation_may_be_enabled(_state()))

    def test_each_unsatisfied_rung_is_reported_in_order(self):
        cases = [
            (dict(is_windows=False), wp.STAGE_UNSUPPORTED_PLATFORM),
            (dict(windows_build_ok=False), wp.STAGE_WINDOWS_TOO_OLD),
            (dict(firmware_virtualization=False), wp.STAGE_FIRMWARE_VIRTUALIZATION),
            (dict(features_enabled={}), wp.STAGE_WINDOWS_FEATURES),
            (dict(reboot_pending=True), wp.STAGE_REBOOT_REQUIRED),
            (dict(wsl_present=False), wp.STAGE_WSL_KERNEL),
            (dict(wsl_version_ok=False), wp.STAGE_WSL_UPDATE),
            (dict(guest_image_installed=False), wp.STAGE_GUEST_IMAGE),
            (dict(boundary_verified=False), wp.STAGE_BOUNDARY_VERIFICATION),
        ]
        for overrides, expected in cases:
            self.assertEqual(wp.current_stage(_state(**overrides)), expected, overrides)

    def test_an_earlier_failure_outranks_a_later_one(self):
        state = _state(firmware_virtualization=False, guest_image_installed=False,
                       boundary_verified=False)
        self.assertEqual(wp.current_stage(state), wp.STAGE_FIRMWARE_VIRTUALIZATION)

    def test_an_undetermined_check_is_treated_exactly_like_a_failure(self):
        for field in ("windows_build_ok", "firmware_virtualization", "wsl_present",
                      "wsl_version_ok", "guest_image_installed", "boundary_verified"):
            self.assertFalse(wp.delegation_may_be_enabled(_state(**{field: None})), field)

    def test_a_partially_enabled_feature_set_is_not_enabled(self):
        partial = {wp.REQUIRED_FEATURES[0]: True, wp.REQUIRED_FEATURES[1]: None}
        self.assertEqual(wp.current_stage(_state(features_enabled=partial)),
                         wp.STAGE_WINDOWS_FEATURES)

    def test_delegation_is_gated_on_the_final_rung_alone(self):
        for stage in wp.STAGE_ORDER[:-1]:
            state = wp._state_at(_state(), stage)
            self.assertFalse(wp.delegation_may_be_enabled(state), stage)


class NextStageTests(unittest.TestCase):
    def test_every_stage_has_a_plain_language_title_and_detail(self):
        for stage in wp.STAGE_ORDER:
            step = wp.next_stage(wp._state_at(_state(), stage))
            self.assertTrue(step.title and len(step.title) < 60, stage)
            self.assertTrue(len(step.detail) > 40, stage)
            self.assertIn(step.actor, (wp.ACTOR_USER, wp.ACTOR_INSTALLER,
                                       wp.ACTOR_INSTALLER_ELEVATED))

    def test_the_stages_a_program_cannot_perform_are_marked_for_the_user(self):
        for stage in (wp.STAGE_UNSUPPORTED_PLATFORM, wp.STAGE_WINDOWS_TOO_OLD,
                      wp.STAGE_FIRMWARE_VIRTUALIZATION):
            step = wp.next_stage(wp._state_at(_state(), stage))
            self.assertEqual(step.actor, wp.ACTOR_USER, stage)
            self.assertEqual(step.argv, (), stage)

    def test_the_firmware_stage_says_plainly_that_no_program_can_do_it(self):
        step = wp.next_stage(_state(firmware_virtualization=False))
        self.assertIn("No program can change that setting", step.detail)
        self.assertIn("BIOS/UEFI", step.detail)

    def test_only_machine_wide_stages_require_administrator(self):
        elevated = {wp.STAGE_WINDOWS_FEATURES, wp.STAGE_WSL_KERNEL, wp.STAGE_WSL_UPDATE}
        for stage in elevated:
            self.assertTrue(wp.next_stage(wp._state_at(_state(), stage)).requires_admin,
                            stage)
        for stage in (wp.STAGE_GUEST_IMAGE, wp.STAGE_BOUNDARY_VERIFICATION, wp.STAGE_READY):
            self.assertFalse(wp.next_stage(wp._state_at(_state(), stage)).requires_admin,
                             stage)

    def test_the_image_stage_says_it_needs_no_administrator_and_is_undoable(self):
        detail = wp.next_stage(_state(guest_image_installed=False)).detail
        self.assertIn("no administrator permission", detail)
        self.assertIn("undone", detail)

    def test_only_the_features_stage_enables_the_missing_features(self):
        partial = {wp.REQUIRED_FEATURES[0]: True, wp.REQUIRED_FEATURES[1]: False}
        step = wp.next_stage(_state(features_enabled=partial))
        self.assertEqual(len(step.argv), 1)
        self.assertIn(f"/FeatureName:{wp.REQUIRED_FEATURES[1]}", step.argv[0])

    def test_the_boundary_stage_promises_delegation_stays_off_unless_it_passes(self):
        step = wp.next_stage(_state(boundary_verified=False))
        self.assertIn("stays switched off unless every check passes", step.detail)


class PlanTests(unittest.TestCase):
    def test_the_plan_warns_up_front_about_administrator_and_restart(self):
        plan = wp.plan(_state(features_enabled={}))
        self.assertTrue(plan["requires_admin_ahead"])
        self.assertTrue(plan["requires_reboot_ahead"])
        self.assertFalse(plan["ready"])

    def test_a_plan_near_the_end_no_longer_warns_about_administrator(self):
        plan = wp.plan(_state(boundary_verified=False))
        self.assertFalse(plan["requires_admin_ahead"])
        self.assertFalse(plan["requires_reboot_ahead"])

    def test_the_plan_is_json_serialisable(self):
        import json
        json.dumps(wp.plan(_state(guest_image_installed=False)))

    def test_remaining_stages_shrink_as_setup_progresses(self):
        early = wp.remaining_stages(_state(firmware_virtualization=False))
        late = wp.remaining_stages(_state(boundary_verified=False))
        self.assertGreater(len(early), len(late))
        self.assertNotIn(wp.STAGE_READY, early)

    def test_a_ready_plan_is_the_only_one_that_enables_delegation(self):
        self.assertTrue(wp.plan(_state())["delegation_may_be_enabled"])
        for stage in wp.STAGE_ORDER[:-1]:
            self.assertFalse(
                wp.plan(wp._state_at(_state(), stage))["delegation_may_be_enabled"], stage)


class ArgvTests(unittest.TestCase):
    """The exact commands an installer will run, reviewed in one place."""

    def test_every_command_names_an_absolute_system32_program(self):
        commands = [wp.enable_feature_argv(wp.REQUIRED_FEATURES[0]),
                    wp.feature_state_argv(wp.REQUIRED_FEATURES[0]),
                    wp.install_wsl_argv(), wp.update_wsl_argv(), wp.wsl_status_argv(),
                    wp.restart_argv(), wp.cancel_restart_argv(),
                    wp.register_resume_argv(["C:\\tool.exe"]),
                    wp.unregister_resume_argv()]
        for argv in commands:
            self.assertTrue(argv[0].startswith(SYSTEM32 + "\\"), argv[0])
            self.assertTrue(argv[0].endswith(".exe"), argv[0])

    def test_wsl_is_installed_without_a_microsoft_store_distribution(self):
        """A default install would register a second, unpinned Linux image
        this runtime neither owns nor can vouch for."""
        self.assertIn("--no-distribution", wp.install_wsl_argv())

    def test_enabling_a_feature_does_not_restart_on_its_own(self):
        argv = wp.enable_feature_argv("VirtualMachinePlatform")
        self.assertIn("/NoRestart", argv)

    def test_only_the_two_required_features_can_be_enabled(self):
        for feature in ("Microsoft-Hyper-V", "", "VirtualMachinePlatform; rm -rf /"):
            with self.assertRaises(ValueError):
                wp.enable_feature_argv(feature)
            with self.assertRaises(ValueError):
                wp.feature_state_argv(feature)

    def test_the_restart_is_delayed_and_cancellable(self):
        argv = wp.restart_argv(120)
        self.assertEqual(argv[1:4], ["/r", "/t", "120"])
        self.assertIn("/a", wp.cancel_restart_argv())

    def test_an_unreasonable_restart_delay_is_refused(self):
        for delay in (0, -5, 4000, True, 1.5):
            with self.assertRaises(ValueError):
                wp.restart_argv(delay)

    def test_elevation_wraps_the_inner_command_without_losing_arguments(self):
        argv = wp.elevate_argv(["C:\\Windows\\System32\\wsl.exe", "--install",
                                "--no-distribution"])
        command = argv[-1]
        self.assertIn("-Verb RunAs", command)
        self.assertIn("'--install'", command)
        self.assertIn("'--no-distribution'", command)

    def test_elevation_escapes_a_quote_rather_than_breaking_out_of_it(self):
        argv = wp.elevate_argv(["C:\\t.exe", "it's"])
        self.assertIn("'it''s'", argv[-1])

    def test_elevation_refuses_control_characters(self):
        for bad in ("a\nb", "a\x00b", "a\rb"):
            with self.assertRaises(ValueError):
                wp.elevate_argv(["C:\\t.exe", bad])

    def test_elevation_refuses_an_empty_command(self):
        with self.assertRaises(ValueError):
            wp.elevate_argv([])


class ResumeRegistrationTests(unittest.TestCase):
    """Per-user, no elevation, and removable."""

    def test_the_resume_task_is_a_per_user_logon_task(self):
        argv = wp.register_resume_argv(["C:\\Program Files\\bridge.exe", "--resume"])
        self.assertIn("/SC", argv)
        self.assertEqual(argv[argv.index("/SC") + 1], "ONLOGON")
        self.assertEqual(argv[argv.index("/TN") + 1], wp.RESUME_TASK_NAME)
        self.assertNotIn("/RU", argv)  # no other user, so no elevation
        self.assertNotIn("/RL", argv)  # no HIGHEST run level

    def test_a_path_with_spaces_is_quoted_in_the_task_action(self):
        argv = wp.register_resume_argv(["C:\\Program Files\\bridge.exe", "--resume"])
        self.assertIn('"C:\\Program Files\\bridge.exe" --resume',
                      argv[argv.index("/TR") + 1])

    def test_unregistering_removes_exactly_that_task(self):
        argv = wp.unregister_resume_argv()
        self.assertIn("/Delete", argv)
        self.assertEqual(argv[argv.index("/TN") + 1], wp.RESUME_TASK_NAME)

    def test_a_command_too_long_for_a_task_is_refused_not_truncated(self):
        """schtasks silently truncates a long action, and a truncated command
        is a different command."""
        with self.assertRaises(ValueError):
            wp.register_resume_argv(["C:\\" + "a" * 300])

    def test_a_command_containing_quotes_or_newlines_is_refused(self):
        for bad in ('C:\\a"b.exe', "C:\\a\nb.exe"):
            with self.assertRaises(ValueError):
                wp.register_resume_argv([bad])

    def test_an_empty_resume_command_is_refused(self):
        with self.assertRaises(ValueError):
            wp.register_resume_argv([])


class ResumeRecordTests(unittest.TestCase):
    def test_a_record_round_trips(self):
        record = wp.build_resume_record(
            stage=wp.STAGE_REBOOT_REQUIRED, stage_completed=wp.STAGE_WINDOWS_FEATURES,
            awaiting_reboot=True, updated_at="2026-03-01T12:00:00+00:00", attempts=1)
        self.assertEqual(wp.parse_resume_record(record), record)

    def test_the_record_lives_beside_the_runtime_root(self):
        self.assertEqual(wp.resume_record_path("C:\\Users\\t\\.agent-bridge\\wsl"),
                         "C:\\Users\\t\\.agent-bridge\\wsl\\provision-resume.json")

    def test_an_unknown_stage_is_refused_on_write_and_on_read(self):
        with self.assertRaises(ValueError):
            wp.build_resume_record(stage="made-up", stage_completed=None,
                                   awaiting_reboot=False, updated_at="now")
        record = wp.build_resume_record(
            stage=wp.STAGE_WSL_KERNEL, stage_completed=None,
            awaiting_reboot=False, updated_at="now")
        record["stage"] = "made-up"
        with self.assertRaises(ValueError):
            wp.parse_resume_record(record)

    def test_a_malformed_record_is_refused_rather_than_partially_believed(self):
        good = wp.build_resume_record(stage=wp.STAGE_WSL_KERNEL, stage_completed=None,
                                      awaiting_reboot=False, updated_at="now")
        for mutate in (lambda r: r.pop("attempts"),
                       lambda r: r.update(schema_version=2),
                       lambda r: r.update(awaiting_reboot="yes"),
                       lambda r: r.update(updated_at=""),
                       lambda r: r.update(attempts=-1),
                       lambda r: r.update(extra=1)):
            record = dict(good)
            mutate(record)
            with self.assertRaises(ValueError):
                wp.parse_resume_record(record)

    def test_the_record_cannot_claim_the_boundary_was_verified(self):
        """Verification is re-observed every time, never remembered: a record
        that could assert it would be a way to switch delegation on without a
        sandbox ever being started."""
        self.assertNotIn("boundary_verified", wp.RESUME_KEYS)
        self.assertNotIn("delegation_enabled", wp.RESUME_KEYS)

    def test_a_stage_that_keeps_failing_stops_instead_of_looping(self):
        record = wp.build_resume_record(
            stage=wp.STAGE_WINDOWS_FEATURES, stage_completed=None,
            awaiting_reboot=True, updated_at="now", attempts=wp.MAX_STAGE_ATTEMPTS)
        self.assertTrue(wp.should_stop_retrying(record, wp.STAGE_WINDOWS_FEATURES))
        self.assertFalse(wp.should_stop_retrying(record, wp.STAGE_WSL_KERNEL))

    def test_a_stage_below_the_attempt_limit_still_retries(self):
        record = wp.build_resume_record(
            stage=wp.STAGE_WINDOWS_FEATURES, stage_completed=None,
            awaiting_reboot=True, updated_at="now", attempts=wp.MAX_STAGE_ATTEMPTS - 1)
        self.assertFalse(wp.should_stop_retrying(record, wp.STAGE_WINDOWS_FEATURES))


class ObservationTests(unittest.TestCase):
    def test_a_non_windows_host_observes_as_unsupported_without_detail(self):
        state, evidence = wp.observe(platform_name="darwin")
        self.assertFalse(state.is_windows)
        self.assertEqual(wp.current_stage(state), wp.STAGE_UNSUPPORTED_PLATFORM)
        self.assertEqual(len(evidence.checks), 1)

    def test_a_complete_observation_reaches_ready(self):
        state, _ = wp.observe(
            platform_name="win32", preflight=_preflight(),
            feature_states={name: "Enabled" for name in wp.REQUIRED_FEATURES},
            guest_image_installed=True, boundary_verified=True,
            provider_lane_verified=True)
        self.assertEqual(wp.current_stage(state), wp.STAGE_READY)

    def test_a_verified_boundary_alone_does_not_reach_ready(self):
        """A sealed sandbox says nothing about whether a session works in it."""
        state, evidence = wp.observe(
            platform_name="win32", preflight=_preflight(),
            feature_states={name: "Enabled" for name in wp.REQUIRED_FEATURES},
            guest_image_installed=True, boundary_verified=True)
        self.assertEqual(wp.current_stage(state), wp.STAGE_PROVIDER_ENROLMENT)
        self.assertFalse(wp.delegation_may_be_enabled(state))
        self.assertIn("provider_lane", {name for name, _, _ in evidence.checks})

    def test_an_uncollected_feature_state_is_not_read_as_enabled(self):
        state, evidence = wp.observe(
            platform_name="win32", preflight=_preflight(), feature_states={},
            guest_image_installed=True, boundary_verified=True)
        self.assertEqual(wp.current_stage(state), wp.STAGE_WINDOWS_FEATURES)
        names = {name for name, _, _ in evidence.checks}
        self.assertIn(f"feature:{wp.REQUIRED_FEATURES[0]}", names)

    def test_a_disabled_feature_is_distinguished_from_an_uncollected_one(self):
        disabled, _ = wp.observe(
            platform_name="win32", preflight=_preflight(),
            feature_states={wp.REQUIRED_FEATURES[0]: "Disabled",
                            wp.REQUIRED_FEATURES[1]: "Enabled"},
            guest_image_installed=True, boundary_verified=True)
        self.assertIs(disabled.features_enabled[wp.REQUIRED_FEATURES[0]], False)
        self.assertIs(disabled.features_enabled[wp.REQUIRED_FEATURES[1]], True)

    def test_a_missing_wsl_goes_to_the_install_stage_not_the_update_stage(self):
        state, _ = wp.observe(
            platform_name="win32",
            preflight=_preflight(wsl_version=_check(False, "executable not found", None)),
            feature_states={name: "Enabled" for name in wp.REQUIRED_FEATURES},
            guest_image_installed=True, boundary_verified=True)
        self.assertEqual(wp.current_stage(state), wp.STAGE_WSL_KERNEL)

    def test_an_outdated_wsl_goes_to_the_update_stage(self):
        state, _ = wp.observe(
            platform_name="win32",
            preflight=_preflight(wsl_version=_check(False, "wsl 1.9 too old", "1.9.0")),
            feature_states={name: "Enabled" for name in wp.REQUIRED_FEATURES},
            guest_image_installed=True, boundary_verified=True)
        self.assertEqual(wp.current_stage(state), wp.STAGE_WSL_UPDATE)

    def test_a_pending_reboot_holds_setup_at_the_restart_stage(self):
        state, _ = wp.observe(
            platform_name="win32", preflight=_preflight(),
            feature_states={name: "Enabled" for name in wp.REQUIRED_FEATURES},
            reboot_pending=True, guest_image_installed=True, boundary_verified=True)
        self.assertEqual(wp.current_stage(state), wp.STAGE_REBOOT_REQUIRED)

    def test_the_evidence_records_every_check_it_made(self):
        _, evidence = wp.observe(
            platform_name="win32", preflight=_preflight(),
            feature_states={name: "Enabled" for name in wp.REQUIRED_FEATURES},
            guest_image_installed=True, boundary_verified=True)
        names = {name for name, _, _ in evidence.checks}
        self.assertLessEqual(
            {"platform", "windows_build", "firmware_virtualization", "wsl_version",
             "reboot_pending", "guest_image", "boundary_verified",
             "provider_lane"}, names)
        import json
        json.dumps(evidence.as_dict())


class ConsentTests(unittest.TestCase):
    """Two separate surprises, so two separate consents."""

    def _run(self, calls):
        def run(argv):
            calls.append(list(argv))
            return types.SimpleNamespace(returncode=0)
        return run

    def test_an_admin_stage_refuses_to_run_without_admin_consent(self):
        step = wp.next_stage(_state(features_enabled={}))
        calls = []
        with self.assertRaises(wp.ConsentRequired):
            wp.advance(step, run=self._run(calls), admin_consent=False)
        self.assertEqual(calls, [])

    def test_a_restarting_stage_refuses_to_run_without_restart_consent(self):
        step = wp.next_stage(_state(reboot_pending=True))
        calls = []
        with self.assertRaises(wp.ConsentRequired):
            wp.advance(step, run=self._run(calls), admin_consent=True,
                       reboot_consent=False)
        self.assertEqual(calls, [])

    def test_admin_consent_alone_does_not_authorise_a_restart(self):
        step = wp.next_stage(_state(reboot_pending=True))
        with self.assertRaises(wp.ConsentRequired) as caught:
            wp.advance(step, run=self._run([]), admin_consent=True)
        self.assertEqual(str(caught.exception), "restart_consent_required")

    def test_with_consent_the_elevated_stage_runs_each_command_once(self):
        step = wp.next_stage(_state(features_enabled={}))
        calls = []
        outcome = wp.advance(step, run=self._run(calls), admin_consent=True)
        self.assertEqual(outcome["status"], "ok")
        self.assertEqual(len(calls), len(wp.REQUIRED_FEATURES))
        for argv in calls:
            self.assertIn("-Verb RunAs", argv[-1])

    def test_a_failing_command_stops_the_stage_rather_than_continuing(self):
        step = wp.next_stage(_state(features_enabled={}))
        calls = []

        def run(argv):
            calls.append(list(argv))
            return types.SimpleNamespace(returncode=1)

        outcome = wp.advance(step, run=run, admin_consent=True)
        self.assertEqual(outcome["status"], "failed")
        self.assertEqual(outcome["commands_run"], 1)
        self.assertEqual(len(calls), 1)

    def test_a_command_with_no_exit_code_is_a_failure_not_a_pass(self):
        step = wp.next_stage(_state(wsl_present=False))
        outcome = wp.advance(step, run=lambda argv: types.SimpleNamespace(),
                             admin_consent=True)
        self.assertEqual(outcome["status"], "failed")
        self.assertEqual(outcome["reason"], "no_exit_code")

    def test_a_user_only_stage_runs_nothing_at_all(self):
        step = wp.next_stage(_state(firmware_virtualization=False))
        calls = []
        outcome = wp.advance(step, run=self._run(calls), admin_consent=True,
                             reboot_consent=True)
        self.assertEqual(outcome["status"], "blocked_on_user")
        self.assertEqual(calls, [])

    def test_the_ready_stage_runs_nothing(self):
        outcome = wp.advance(wp.next_stage(_state()), run=self._run([]))
        self.assertEqual(outcome["status"], "nothing_to_run")


class DocumentedLimitationTests(unittest.TestCase):
    def test_the_admin_and_firmware_and_reboot_boundaries_are_documented(self):
        self.assertIn("require administrator rights", wp.ADMIN_BOUNDARY_LIMITATION)
        self.assertIn("no program can change", wp.FIRMWARE_LIMITATION)
        self.assertIn("durable resume record", wp.REBOOT_LIMITATION)

    def test_the_module_does_not_claim_live_windows_validation(self):
        self.assertIn("has been validated on a live Windows host", wp.__doc__)



class ResumeHardeningTests(unittest.TestCase):
    """The resume task runs elevated work at logon, so it gets the strict contract."""

    WORKER = "C:\\Users\\sam\\AppData\\Local\\AgentBridge\\setup.exe"

    def test_a_unc_resume_command_is_refused(self):
        with self.assertRaises(ValueError):
            wp.register_resume_argv(["\\\\server\\share\\setup.exe"])

    def test_a_device_path_resume_command_is_refused(self):
        with self.assertRaises(ValueError):
            wp.register_resume_argv(["\\\\?\\C:\\setup.exe"])

    def test_a_traversal_resume_command_is_refused(self):
        with self.assertRaises(ValueError):
            wp.register_resume_argv(["C:\\a\\..\\setup.exe"])

    def test_a_percent_in_the_resume_command_is_refused(self):
        with self.assertRaises(ValueError):
            wp.register_resume_argv([self.WORKER, "--config", "%APPDATA%\\c.json"])

    def test_a_quote_in_the_resume_command_is_refused(self):
        with self.assertRaises(ValueError):
            wp.register_resume_argv([self.WORKER, 'a"b'])

    def test_a_resume_command_that_would_be_truncated_is_refused(self):
        with self.assertRaises(ValueError):
            wp.register_resume_argv([self.WORKER, "a" * 300])

    def test_a_local_absolute_resume_command_registers(self):
        argv = wp.register_resume_argv([self.WORKER, "--resume"])
        self.assertIn("/SC", argv)
        self.assertEqual(argv[argv.index("/TN") + 1], wp.RESUME_TASK_NAME)

    def test_the_resume_task_is_never_overwritten_when_it_is_not_ours(self):
        from agent_bridge.orchestration import windows_activation as wact
        status = wact.ActivationStatus(True, "Ready", "",
                                       "C:\\Windows\\System32\\calc.exe")
        self.assertEqual(wp.resume_ownership_blocker(status, [self.WORKER]),
                         "task_not_owned_by_agent_bridge")

    def test_our_own_resume_task_may_be_replaced(self):
        from agent_bridge.orchestration import windows_activation as wact
        status = wact.ActivationStatus(True, "Ready", "",
                                       f'"{self.WORKER}" --resume')
        self.assertEqual(wp.resume_ownership_blocker(status, [self.WORKER]), "")

    def test_an_absent_resume_task_blocks_nothing(self):
        from agent_bridge.orchestration import windows_activation as wact
        self.assertEqual(
            wp.resume_ownership_blocker(wact.ActivationStatus(False),
                                        [self.WORKER]), "")


class ResumeRecordWriteTests(unittest.TestCase):
    """A record any local account can edit decides what runs elevated next."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / wp.RESUME_RECORD_NAME)
        self.record = wp.build_resume_record(
            stage=wp.STAGE_WSL_KERNEL, stage_completed=wp.STAGE_WINDOWS_FEATURES,
            awaiting_reboot=False, updated_at="2026-09-14T00:00:00Z")

    def tearDown(self):
        self.temp.cleanup()

    @unittest.skipIf(os.name == "nt", "POSIX mode bits")
    def test_the_record_is_written_owner_only(self):
        wp.write_resume_record(self.path, self.record)
        self.assertEqual(Path(self.path).stat().st_mode & 0o777, 0o600)

    def test_a_record_that_cannot_be_locked_down_is_not_written(self):
        def refuse(_fd):
            raise PermissionError("no acl")

        with self.assertRaises(wp.ResumeError):
            wp.write_resume_record(self.path, self.record, secure=refuse)
        # Not an empty file: nothing at all. An empty record parses as no
        # record and sends setup back to the first stage.
        self.assertFalse(Path(self.path).exists())

    def test_protection_is_the_default_rather_than_opt_in(self):
        """The common call must be the safe one."""
        calls = []
        real = wp.wpv.platform_secure_writer

        def counting(platform=None):
            calls.append(platform)
            return real(platform)

        with mock.patch.object(wp.wpv, "platform_secure_writer", counting):
            wp.write_resume_record(self.path, self.record)
        self.assertEqual(len(calls), 1)

    def test_a_failed_write_never_truncates_the_record_already_there(self):
        wp.write_resume_record(self.path, self.record)
        before = Path(self.path).read_bytes()

        def refuse(_fd):
            raise PermissionError("no acl")

        later = wp.build_resume_record(
            stage=wp.STAGE_WSL_UPDATE, stage_completed=wp.STAGE_WSL_KERNEL,
            awaiting_reboot=False, updated_at="2026-09-14T01:00:00Z")
        with self.assertRaises(wp.ResumeError):
            wp.write_resume_record(self.path, later, secure=refuse)
        self.assertEqual(Path(self.path).read_bytes(), before)

    def test_what_was_written_parses_back(self):
        wp.write_resume_record(self.path, self.record)
        parsed = wp.parse_resume_record(
            json.loads(Path(self.path).read_text(encoding="utf-8")))
        self.assertEqual(parsed["stage"], wp.STAGE_WSL_KERNEL)


class ResumeRecordReadTests(unittest.TestCase):
    """Reading back what the reboot left behind."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / wp.RESUME_RECORD_NAME)

    def _write(self, **overrides):
        fields = {"stage": wp.STAGE_WSL_KERNEL,
                  "stage_completed": wp.STAGE_WINDOWS_FEATURES,
                  "awaiting_reboot": True,
                  "updated_at": "2026-09-14T00:00:00Z", "attempts": 1}
        fields.update(overrides)
        wp.write_resume_record(self.path, wp.build_resume_record(**fields))

    def test_no_record_is_not_an_error(self):
        self.assertIsNone(wp.read_resume_record(self.path))

    def test_a_record_round_trips_with_its_identity(self):
        self._write()
        record, identity = wp.read_resume_record(self.path)
        self.assertEqual(record["stage"], wp.STAGE_WSL_KERNEL)
        self.assertTrue(record["awaiting_reboot"])
        self.assertIsNotNone(identity)

    def test_a_malformed_record_is_an_error_not_an_absence(self):
        Path(self.path).write_text("{", encoding="utf-8")
        os.chmod(self.path, 0o600)
        with self.assertRaises(wp.ResumeError) as caught:
            wp.read_resume_record(self.path)
        self.assertEqual(caught.exception.reason, "resume_malformed")

    @unittest.skipIf(os.name == "nt", "POSIX mode bits")
    def test_a_record_another_account_could_write_is_refused(self):
        self._write()
        os.chmod(self.path, 0o666)
        with self.assertRaises(wp.ResumeError) as caught:
            wp.read_resume_record(self.path)
        self.assertEqual(caught.exception.reason, "resume_file_not_owner_only")

    def test_attempts_increment_within_a_stage_and_reset_across_them(self):
        self._write(attempts=1)
        record, _ = wp.read_resume_record(self.path)
        self.assertEqual(wp.next_attempt(record, wp.STAGE_WSL_KERNEL), 2)
        self.assertEqual(wp.next_attempt(record, wp.STAGE_WSL_UPDATE), 1)

    def test_a_stage_that_keeps_failing_stops_instead_of_looping(self):
        self._write(attempts=wp.MAX_STAGE_ATTEMPTS)
        record, _ = wp.read_resume_record(self.path)
        self.assertTrue(wp.should_stop_retrying(record, wp.STAGE_WSL_KERNEL))
        self.assertFalse(wp.should_stop_retrying(record, wp.STAGE_WSL_UPDATE))

    def test_a_stale_increment_cannot_overwrite_a_newer_record(self):
        """Two resumes racing must not both write from the same read."""
        self._write(attempts=1)
        _record, identity = wp.read_resume_record(self.path)
        self._write(attempts=2)  # somebody else got there first
        with self.assertRaises(wp.ResumeError) as caught:
            wp.write_resume_record(
                self.path,
                wp.build_resume_record(
                    stage=wp.STAGE_WSL_KERNEL, stage_completed=None,
                    awaiting_reboot=False, updated_at="2026-09-14T02:00:00Z",
                    attempts=2),
                expect_identity=identity)
        self.assertEqual(caught.exception.reason,
                         "resume_replace_identity_changed")
        record, _ = wp.read_resume_record(self.path)
        self.assertEqual(record["attempts"], 2)

    def test_clearing_is_idempotent(self):
        self._write()
        wp.clear_resume_record(self.path)
        self.assertFalse(Path(self.path).exists())
        wp.clear_resume_record(self.path)



if __name__ == "__main__":
    unittest.main()
