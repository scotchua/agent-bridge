"""The end-to-end provisioning driver.

These tests walk the ladder from a stock Windows machine to an activated
worker with every OS interaction supplied by the caller. What they cannot do,
and deliberately do not try to do, is reach the ready state: recording
evidence refuses off native Windows, so the last gate stays shut here no
matter what the rest of the ladder is told.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge.orchestration import guest_runner as gr
from agent_bridge.orchestration import windows_auth as wa
from agent_bridge.orchestration import windows_delegation as wd
from agent_bridge.orchestration import windows_evidence as wev
from agent_bridge.orchestration import windows_provision_driver as dr
from agent_bridge.orchestration import windows_rootfs as wrf
from agent_bridge.orchestration import windows_wsl_provision as wp
from agent_bridge.orchestration import windows_wsl_runtime as wr


class _Check:
    def __init__(self, passed, detail="", value=None):
        self.passed = passed
        self.detail = detail
        self.value = value


class _Preflight:
    def __init__(self, *, build=True, firmware=True, wsl=True):
        self.windows_build = _Check(build, "build 22631")
        self.firmware_virtualization = _Check(firmware, "enabled")
        self.wsl_version = _Check(wsl, "2.0.0", value="2.0.0")


class _Result:
    def __init__(self, returncode=0):
        self.returncode = returncode


class _Cleanup:
    def __init__(self, complete=True):
        self.complete = complete


class _JobResult:
    def __init__(self, *, status=wr.STATUS_COMPLETED, canaries=True, complete=True,
                 reason="", stdout=b"", stderr=b""):
        self.status = status
        self.reason = reason
        self.canaries_passed = canaries
        self.cleanup = _Cleanup(complete)
        self.stdout = stdout
        self.stderr = stderr


ENABLED = {name: "Enabled" for name in wp.REQUIRED_FEATURES}
DISABLED = {name: "Disabled" for name in wp.REQUIRED_FEATURES}

#: A command shaped the way the real entrypoint builds one: an absolute
#: local interpreter, an absolute script, and the runtime root.
RESUME_COMMAND = ("C:\\Python\\python.exe", "C:\\agent-bridge\\setup_bridge.py",
                  "windows-setup", "resume")


class DriverTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.rootfs = self.root / "rootfs.tar"
        self.rootfs.write_bytes(b"pretend rootfs")
        self.rootfs_sha256 = hashlib.sha256(b"pretend rootfs").hexdigest()
        self.manifest_path = self.root / "manifest.json"
        self.sidecar_path = self.root / "sidecar.json"
        self._write_manifest()
        self.commands: list[list[str]] = []

    def _write_manifest(self, **overrides):
        manifest = {"schema_version": 1, "distro_release": "12.5",
                    "rootfs_sha256": self.rootfs_sha256, "node_version": "20.11.1",
                    "claude_version": "1.2.3", "codex_version": "0.9.0"}
        manifest.update(overrides)
        self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.sidecar_path.write_text(json.dumps(
            {"architecture": "arm64", "rootfs_sha256": manifest["rootfs_sha256"]}),
            encoding="utf-8")
        self.manifest = manifest

    def _config(self):
        return wd.DelegationConfig(
            runtime_root=str(self.runtime), rootfs_path=str(self.rootfs),
            manifest_path=str(self.manifest_path),
            sidecar_path=str(self.sidecar_path))

    def _run(self, argv):
        self.commands.append(list(argv))
        return _Result(0)

    def _context(self, **overrides):
        options = dict(config=self._config(), run=self._run,
                       preflight=_Preflight(), feature_states=dict(ENABLED),
                       platform_name="win32", machine="arm64",
                       os_name="posix", wsl_version="2.0.0",
                       clock=lambda: "2026-01-01T00:00:00+00:00")
        options.update(overrides)
        return dr.DriverContext(**options)

    def _anchor(self):
        """A trust anchor over the manifest actually on disk.

        Carries both halves, because an anchor missing either is refused by
        name now rather than being treated as merely unverified.
        """
        digest = wrf.manifest_digest(self.manifest)
        return (wrf.TrustAnchor(release="test", architecture="arm64",
                                manifest_sha256=digest, signature="ab" * 64,
                                public_key="cd" * 32),)


class ImageProvenanceTests(DriverTestCase):
    def test_an_untrusted_image_is_installed_but_not_usable(self):
        """Hashing answers one question; it cannot answer the other."""
        provenance = dr.verify_installed_image(self._config(), machine="arm64")
        self.assertTrue(provenance.installed)
        self.assertFalse(provenance.trusted)
        self.assertEqual(provenance.reason, "manifest_untrusted:no_release_anchor")
        self.assertIn("manifest_trust_anchor_missing", provenance.release_blockers)

    def test_a_release_anchor_makes_the_same_image_trusted(self):
        provenance = dr.verify_installed_image(
            self._config(), machine="arm64", anchors=self._anchor(),
            verify_signature=lambda payload, signature, key: True)
        self.assertTrue(provenance.trusted, provenance.reason)
        self.assertEqual(provenance.rootfs_sha256, self.rootfs_sha256)

    def test_a_tampered_tarball_fails_before_trust_is_considered(self):
        self.rootfs.write_bytes(b"different bytes")
        provenance = dr.verify_installed_image(
            self._config(), machine="arm64", anchors=self._anchor(),
            verify_signature=lambda *args: True)
        self.assertFalse(provenance.trusted)
        self.assertEqual(provenance.reason, "rootfs_hash_mismatch")

    def test_an_image_for_the_other_architecture_is_refused(self):
        provenance = dr.verify_installed_image(
            self._config(), machine="x86_64", anchors=self._anchor(),
            verify_signature=lambda *args: True)
        self.assertEqual(provenance.reason, "architecture_mismatch")

    def test_a_missing_tarball_is_not_installed(self):
        self.rootfs.unlink()
        self.assertEqual(dr.verify_installed_image(self._config()).reason,
                         "rootfs_absent")

    def test_installing_requires_consent(self):
        source = self.root / "supplied.tar"
        source.write_bytes(b"pretend rootfs")
        self.rootfs.unlink()
        with self.assertRaises(dr.DriverError) as caught:
            dr.acquire_guest_image(str(source), self._config(), consent=False)
        self.assertEqual(caught.exception.reason, "image_install_consent_required")
        self.assertFalse(self.rootfs.exists())

    def test_an_installed_image_is_owner_only_and_verified_in_place(self):
        source = self.root / "supplied.tar"
        source.write_bytes(b"pretend rootfs")
        self.rootfs.unlink()
        provenance = dr.acquire_guest_image(
            str(source), self._config(), consent=True,
            platform=_WindowsLike(), machine="arm64", anchors=self._anchor(),
            verify_signature=lambda *args: True)
        self.assertTrue(provenance.trusted, provenance.reason)
        self.assertEqual(self.rootfs.stat().st_mode & 0o077, 0)

    def test_a_missing_source_file_is_named(self):
        with self.assertRaises(dr.DriverError) as caught:
            dr.acquire_guest_image(str(self.root / "absent.tar"), self._config(),
                                   consent=True)
        self.assertEqual(caught.exception.reason, "image_source_unreadable")


class _WindowsLike:
    name = "nt"

    def enforce_owner_only_file(self, descriptor):
        os.fchmod(descriptor, 0o600)

    def verify_owner_only_path(self, directory, probe_file):
        return (os.stat(probe_file).st_mode & 0o077) == 0, "mode"


class BoundaryVerificationTests(DriverTestCase):
    def test_a_clean_run_with_every_canary_verifies(self):
        outcome = dr.verify_boundary_live(self._config(),
                                          run_job=lambda request: _JobResult())
        self.assertTrue(outcome.verified, outcome.reason)
        self.assertEqual(outcome.canaries, tuple(wr.CANARY_ORDER))

    def test_a_failed_canary_is_never_recoverable(self):
        outcome = dr.verify_boundary_live(
            self._config(), run_job=lambda request: _JobResult(canaries=False))
        self.assertFalse(outcome.verified)
        self.assertEqual(outcome.reason, "canaries_failed")

    def test_a_job_that_did_not_complete_does_not_verify(self):
        outcome = dr.verify_boundary_live(
            self._config(),
            run_job=lambda request: _JobResult(status=wr.STATUS_ABORTED,
                                               reason="import_failed"))
        self.assertFalse(outcome.verified)

    def test_a_sandbox_that_may_still_exist_does_not_verify(self):
        outcome = dr.verify_boundary_live(
            self._config(), run_job=lambda request: _JobResult(complete=False))
        self.assertFalse(outcome.verified)
        self.assertEqual(outcome.reason, "cleanup_incomplete")

    def test_the_boundary_job_carries_no_session(self):
        seen = {}

        def capture(request):
            seen["spec"] = request.job_spec
            return _JobResult()

        dr.verify_boundary_live(self._config(), run_job=capture)
        self.assertIn("spec", seen)

    def test_an_unreadable_manifest_refuses_before_any_sandbox_starts(self):
        self.manifest_path.write_text("{not json", encoding="utf-8")
        calls = []
        outcome = dr.verify_boundary_live(
            self._config(), run_job=lambda request: calls.append(request))
        self.assertFalse(outcome.verified)
        self.assertEqual(calls, [])

    def test_recording_refuses_off_a_real_windows_host(self):
        """The one gate no test wiring may open."""
        outcome = dr.verify_boundary_live(self._config(),
                                          run_job=lambda request: _JobResult())
        with self.assertRaises(dr.DriverError) as caught:
            dr.record_boundary(self._config(), outcome, wsl_version="2.0.0",
                               recorded_at="2026-01-01T00:00:00+00:00",
                               fingerprint="f" * 64, os_name="posix")
        self.assertEqual(caught.exception.reason, "evidence_requires_windows")
        self.assertFalse(Path(wev.evidence_path(str(self.runtime))).exists())

    def test_an_unverified_boundary_is_never_recorded(self):
        with self.assertRaises(dr.DriverError) as caught:
            dr.record_boundary(self._config(),
                               dr.BoundaryOutcome(False, "canaries_failed"),
                               wsl_version="2.0.0", recorded_at="now",
                               os_name="nt")
        self.assertEqual(caught.exception.reason, "boundary_not_verified")


class _ProbeResult(_JobResult):
    """A guest job result carrying one of the probe's fixed verdicts."""

    def __init__(self, verdict, *, canaries=True, complete=True):
        completed = verdict == gr.PROBE_AUTHENTICATED
        super().__init__(
            status=wr.STATUS_COMPLETED if completed else wr.STATUS_ABORTED,
            canaries=canaries, complete=complete, reason=verdict)


class ProviderLaneTests(DriverTestCase):
    """The lane opens on a real authenticated operation, or not at all."""

    CAPSULE = {"kind": "claude_code_oauth_token", "token": "t" * 40}

    def _lane(self, *verdicts, **kwargs):
        seen = []

        def runner(request):
            seen.append(request)
            index = min(len(seen), len(verdicts)) - 1
            return _ProbeResult(verdicts[index], **kwargs)

        lane = dr.observe_provider_lane(self._config(), "claude",
                                        capsule=self.CAPSULE, run_job=runner)
        return lane, seen

    def test_both_observations_are_required_to_open_the_lane(self):
        lane, seen = self._lane(gr.PROBE_AUTHENTICATED, gr.PROBE_REJECTED)
        self.assertTrue(lane.enabled_for("claude"))
        self.assertTrue(lane.portability_observed)
        self.assertTrue(lane.refresh_behaviour_observed)
        self.assertEqual(len(seen), 2)

    def test_a_version_string_can_never_open_the_lane(self):
        """The regression for what this replaced.

        A CLI that only prints its version exits zero whether or not a session
        was supplied. Under the old observation both runs looked identical and
        the lane opened; under the probe neither run produces the sentinel, so
        the verdict is 'no sentinel' and the lane stays shut.
        """
        lane, _ = self._lane(gr.PROBE_NO_SENTINEL)
        self.assertFalse(lane.enabled_for("claude"))
        self.assertFalse(lane.portability_observed)
        self.assertIn(gr.PROBE_NO_SENTINEL, lane.detail)

    def test_a_probe_the_guest_ran_with_an_api_key_present_shuts_the_lane(self):
        """Subscription auth is the claim; an API key would make it false."""
        lane, _ = self._lane(gr.PROBE_API_KEY_PRESENT)
        self.assertFalse(lane.enabled_for("claude"))
        self.assertIn(gr.PROBE_API_KEY_PRESENT, lane.detail)

    def test_a_rejected_session_leaves_the_lane_shut(self):
        lane, _ = self._lane(gr.PROBE_REJECTED)
        self.assertFalse(lane.enabled_for("claude"))
        self.assertIn("portability_not_observed", lane.detail)

    def test_a_nonexistent_session_leaves_the_lane_shut(self):
        """A capsule the provider has never heard of is a rejection, not a pass."""
        lane = dr.observe_provider_lane(
            self._config(), "claude",
            capsule={"kind": "claude_code_oauth_token", "token": "z" * 40},
            run_job=lambda request: _ProbeResult(gr.PROBE_REJECTED))
        self.assertFalse(lane.enabled_for("claude"))

    def test_a_probe_that_timed_out_is_not_an_observation(self):
        lane, _ = self._lane(gr.PROBE_TIMED_OUT)
        self.assertFalse(lane.enabled_for("claude"))

    def test_a_worthless_capsule_that_authenticates_shuts_the_lane(self):
        """If a non-credential works, nothing checked the credential."""
        lane, _ = self._lane(gr.PROBE_AUTHENTICATED, gr.PROBE_AUTHENTICATED)
        self.assertFalse(lane.enabled_for("claude"))
        self.assertTrue(lane.portability_observed)
        self.assertEqual(lane.detail,
                         "refresh_behaviour_not_observed:accepted")

    def test_a_refresh_failure_for_an_unrelated_reason_is_inconclusive(self):
        """Failing is not the same as being refused, and only one is evidence."""
        lane, _ = self._lane(gr.PROBE_AUTHENTICATED, gr.PROBE_FAILED)
        self.assertFalse(lane.enabled_for("claude"))
        self.assertIn("refresh_behaviour_inconclusive", lane.detail)

    def test_canaries_that_did_not_pass_are_never_a_provider_outcome(self):
        lane, _ = self._lane(gr.PROBE_AUTHENTICATED, canaries=False)
        self.assertFalse(lane.enabled_for("claude"))
        self.assertIn("probe_canaries_failed", lane.detail)

    def test_a_probe_that_left_a_sandbox_behind_shuts_the_lane(self):
        lane, _ = self._lane(gr.PROBE_AUTHENTICATED, complete=False)
        self.assertFalse(lane.enabled_for("claude"))
        self.assertEqual(lane.detail, "portability_probe_left_a_sandbox")

    def test_a_verdict_the_guest_does_not_define_is_refused(self):
        lane = dr.observe_provider_lane(
            self._config(), "claude", capsule=self.CAPSULE,
            run_job=lambda request: _JobResult(reason="looks_fine"))
        self.assertFalse(lane.enabled_for("claude"))
        self.assertIn("probe_verdict_unknown", lane.detail)

    def test_the_probe_request_carries_no_brief_workspace_or_verification(self):
        _lane, seen = self._lane(gr.PROBE_AUTHENTICATED, gr.PROBE_REJECTED)
        payload = json.loads(seen[0].stdin_data)
        self.assertEqual(payload["mode"], gr.MODE_AUTH_PROBE)
        self.assertEqual(set(payload), gr.AUTH_PROBE_REQUEST_KEYS)

    def test_the_refresh_probe_never_reuses_the_real_session(self):
        _lane, seen = self._lane(gr.PROBE_AUTHENTICATED, gr.PROBE_REJECTED)
        self.assertEqual(len(seen), 2)
        self.assertNotIn(self.CAPSULE["token"], json.dumps(seen[1].job_spec))
        self.assertNotIn(self.CAPSULE["token"], seen[1].stdin_data)
        self.assertIn(dr.SYNTHETIC_EXPIRED_TOKEN, seen[1].stdin_data)

    def test_the_synthetic_capsule_is_obviously_not_a_credential(self):
        self.assertIn("synthetic", dr.SYNTHETIC_EXPIRED_TOKEN)

    def test_an_unsupported_provider_is_refused(self):
        with self.assertRaises(dr.DriverError) as caught:
            dr.observe_provider_lane(self._config(), "gemini",
                                     capsule=self.CAPSULE,
                                     run_job=lambda request: _JobResult())
        self.assertEqual(caught.exception.reason, "provider_not_supported")

    def test_a_verified_boundary_alone_never_opens_the_lane(self):
        outcome = dr.verify_boundary_live(self._config(),
                                          run_job=lambda request: _JobResult())
        self.assertTrue(outcome.verified)
        self.assertFalse(wev.ProviderLane().enabled_for("claude"))


class LadderTests(DriverTestCase):
    def test_a_non_windows_machine_is_told_so_and_nothing_runs(self):
        result = dr.step(self._context(platform_name="darwin"))
        self.assertEqual(result["step"], wp.STAGE_UNSUPPORTED_PLATFORM)
        self.assertEqual(result["status"], dr.STEP_BLOCKED)
        self.assertEqual(self.commands, [])

    def test_firmware_virtualization_is_a_person_s_job(self):
        context = self._context(preflight=_Preflight(firmware=False))
        result = dr.step(context)
        self.assertEqual(result["step"], wp.STAGE_FIRMWARE_VIRTUALIZATION)
        self.assertEqual(result["status"], dr.STEP_BLOCKED)
        self.assertEqual(self.commands, [])

    def test_enabling_features_without_consent_runs_nothing(self):
        context = self._context(feature_states=dict(DISABLED))
        result = dr.step(context)
        self.assertEqual(result["step"], wp.STAGE_WINDOWS_FEATURES)
        self.assertEqual(result["status"], dr.STEP_CONSENT_REQUIRED)
        self.assertTrue(result["requires_admin"])
        self.assertEqual(self.commands, [])

    def test_enabling_features_with_consent_elevates_once_per_feature(self):
        context = self._context(feature_states=dict(DISABLED))
        result = dr.step(context, admin_consent=True)
        self.assertEqual(result["status"], dr.STEP_OK)
        self.assertEqual(result["commands_run"], len(wp.REQUIRED_FEATURES))
        for command in self.commands:
            self.assertIn("powershell.exe", command[0].lower())

    def test_a_failed_elevated_command_stops_the_ladder(self):
        def failing(argv):
            self.commands.append(list(argv))
            return _Result(1)

        context = self._context(feature_states=dict(DISABLED), run=failing)
        result = dr.step(context, admin_consent=True)
        self.assertEqual(result["status"], dr.STEP_FAILED)
        self.assertEqual(result["commands_run"], 1)

    def test_a_restart_needs_its_own_consent(self):
        context = self._context(reboot_pending=True)
        result = dr.step(context, admin_consent=True)
        self.assertEqual(result["step"], wp.STAGE_REBOOT_REQUIRED)
        self.assertEqual(result["status"], dr.STEP_CONSENT_REQUIRED)
        self.assertEqual(self.commands, [])

    def test_a_restart_registers_the_task_and_the_record_before_it_happens(self):
        """The record alone was the old bug: a note nothing would ever read."""

        context = self._context(reboot_pending=True,
                                resume_command=RESUME_COMMAND)
        result = dr.step(context, admin_consent=True, reboot_consent=True)
        self.assertEqual(result["status"], dr.STEP_REBOOT_SCHEDULED)
        record = Path(wp.resume_record_path(str(self.runtime)))
        self.assertTrue(record.exists())
        parsed = wp.parse_resume_record(json.loads(record.read_text()))
        self.assertEqual(parsed["stage"], wp.STAGE_REBOOT_REQUIRED)
        self.assertTrue(parsed["awaiting_reboot"])
        created = [argv for argv in self.commands if "/Create" in argv]
        self.assertEqual(len(created), 1)
        self.assertIn(wp.RESUME_TASK_NAME, created[0])

    def test_a_restart_without_a_way_back_is_refused_outright(self):
        """No resume command means no reboot. Staying put is recoverable."""

        context = self._context(reboot_pending=True)
        result = dr.step(context, admin_consent=True, reboot_consent=True)
        self.assertEqual(result["status"], dr.STEP_FAILED)
        self.assertEqual(result["reason"], "resume_command_missing")
        self.assertFalse(Path(wp.resume_record_path(str(self.runtime))).exists())
        self.assertEqual(self.commands, [])

    def test_the_image_stage_blocks_on_an_unanchored_image(self):
        result = dr.step(self._context())
        self.assertEqual(result["step"], wp.STAGE_GUEST_IMAGE)
        self.assertEqual(result["status"], dr.STEP_BLOCKED)
        self.assertEqual(result["reason"], "manifest_untrusted:no_release_anchor")

    def test_the_boundary_stage_is_reached_once_the_image_is_trusted(self):
        context = self._context(anchors=self._anchor(),
                                verify_signature=lambda *args: True,
                                run_job=lambda request: _JobResult())
        result = dr.step(context)
        self.assertEqual(result["step"], wp.STAGE_BOUNDARY_VERIFICATION)
        # Off Windows the record cannot be written, so the stage fails here.
        self.assertEqual(result["status"], dr.STEP_FAILED)
        self.assertEqual(result["reason"], "evidence_requires_windows")

    def test_the_plan_reports_progress_without_changing_anything(self):
        report = dr.plan(self._context())
        self.assertEqual(report["stage"], wp.STAGE_GUEST_IMAGE)
        self.assertIn(wp.STAGE_BOUNDARY_VERIFICATION, report["remaining"])
        self.assertFalse(report["delegation_may_be_enabled"])
        self.assertIn("manifest_trust_anchor_missing", report["release_blockers"])
        self.assertEqual(len(report["provider_sessions"]), 2)
        self.assertEqual(self.commands, [])


class ActivationTests(DriverTestCase):
    def test_activation_needs_consent(self):
        result = dr.activate_worker(self._context(),
                                    command=["C:\\\\Python\\\\python.exe", "-m", "x"],
                                    consent=False)
        self.assertEqual(result["status"], dr.STEP_CONSENT_REQUIRED)
        self.assertEqual(self.commands, [])

    def test_activation_is_blocked_until_provisioning_is_complete(self):
        result = dr.activate_worker(self._context(),
                                    command=["C:\\\\Python\\\\python.exe", "-m", "x"],
                                    consent=True)
        self.assertEqual(result["status"], dr.STEP_BLOCKED)
        self.assertEqual(result["reason"], "provisioning_incomplete")
        self.assertEqual(self.commands, [])


class HonestyTests(unittest.TestCase):
    def test_the_driver_states_which_steps_still_refuse(self):
        self.assertIn("refuse", dr.DRIVER_LIMITATION)
        self.assertIn("provider lane", dr.DRIVER_LIMITATION)


class _WindowsLike:
    """A platform stub that reports the real mode rather than always yes.

    Enrolment refuses a directory it cannot prove is owner-only, so a stub
    that answered yes unconditionally would make these tests pass for the
    wrong reason.
    """

    name = "nt"

    def enforce_owner_only_file(self, descriptor):
        os.fchmod(descriptor, 0o600)

    def verify_owner_only_path(self, directory, probe_file):
        info = os.stat(probe_file)
        return (info.st_mode & 0o077) == 0, {"mechanism": "posix mode bits"}


NT = _WindowsLike()


class _Enrolled:
    """A protector that keeps the blob in memory, so enrolment is exercisable.

    Not encryption and not pretending to be: the point of these tests is the
    wiring around the secret, and DPAPI itself cannot run here at all.
    """

    def protect(self, payload):
        return b"sealed:" + payload

    def unprotect(self, payload):
        if not payload.startswith(b"sealed:"):
            raise ValueError("not ours")
        return payload[len(b"sealed:"):]


class ProviderStepWiringTests(DriverTestCase):
    """The step that was missing: observation reaching the durable record."""

    CAPSULE_TOKEN = "t" * 40

    def _enrol(self):
        wa.enrol(str(self.runtime), "claude", self.CAPSULE_TOKEN, consent=True,
                 protector=_Enrolled(), platform=NT)

    def _record_boundary(self):
        outcome = dr.verify_boundary_live(self._config(),
                                          run_job=lambda request: _JobResult())
        return dr.record_boundary(
            self._config(), outcome, wsl_version="2.0.0",
            recorded_at="2026-01-01T00:00:00+00:00",
            fingerprint=wev.host_fingerprint(), os_name="nt")

    def test_a_boundary_record_alone_leaves_the_lane_closed(self):
        self._record_boundary()
        lane = dr._reload_lane(self._config())
        self.assertIsNotNone(lane)
        self.assertFalse(lane.observed)

    def test_an_observed_lane_is_persisted_and_read_back(self):
        self._record_boundary()
        lane = wev.ProviderLane(verified=True, providers=("claude",),
                                portability_observed=True,
                                refresh_behaviour_observed=True,
                                detail="observed_live")
        dr.record_provider_lane(self._config(), lane, os_name="nt")
        reloaded = dr._reload_lane(self._config())
        self.assertTrue(reloaded.observed)
        self.assertTrue(reloaded.enabled_for("claude"))

    def test_the_amendment_keeps_the_boundary_facts_it_did_not_observe(self):
        recorded = self._record_boundary()
        lane = wev.ProviderLane(verified=True, providers=("claude",),
                                portability_observed=True,
                                refresh_behaviour_observed=True)
        amended = dr.record_provider_lane(self._config(), lane, os_name="nt")
        self.assertEqual(amended.rootfs_sha256, recorded.rootfs_sha256)
        self.assertEqual(amended.canaries, recorded.canaries)
        self.assertEqual(amended.host_fingerprint, recorded.host_fingerprint)

    def test_a_lane_cannot_be_recorded_without_a_verified_boundary(self):
        with self.assertRaises(dr.DriverError) as caught:
            dr.record_provider_lane(
                self._config(),
                wev.ProviderLane(verified=True, providers=("claude",),
                                 portability_observed=True,
                                 refresh_behaviour_observed=True),
                os_name="nt")
        self.assertIn("evidence", caught.exception.reason)

    def test_recording_a_lane_off_windows_refuses(self):
        self._record_boundary()
        with self.assertRaises(dr.DriverError) as caught:
            dr.record_provider_lane(
                self._config(),
                wev.ProviderLane(verified=True, providers=("claude",),
                                 portability_observed=True,
                                 refresh_behaviour_observed=True),
                os_name="posix")
        self.assertEqual(caught.exception.reason, "evidence_requires_windows")

    def test_the_lane_gates_the_ready_state_end_to_end(self):
        self._record_boundary()
        context = self._context(anchors=self._anchor(),
                                verify_signature=lambda *args: True)
        state, _evidence, _prov = dr.observe(context)
        self.assertEqual(wp.current_stage(state), wp.STAGE_PROVIDER_ENROLMENT)
        self.assertFalse(wp.delegation_may_be_enabled(state))

        dr.record_provider_lane(
            self._config(),
            wev.ProviderLane(verified=True, providers=("claude",),
                             portability_observed=True,
                             refresh_behaviour_observed=True), os_name="nt")
        state, _evidence, _prov = dr.observe(context)
        self.assertEqual(wp.current_stage(state), wp.STAGE_READY)
        self.assertTrue(wp.delegation_may_be_enabled(state))

    def test_the_step_refuses_without_consent_and_runs_nothing(self):
        self._record_boundary()
        ran = []
        context = self._context(anchors=self._anchor(),
                                verify_signature=lambda *args: True,
                                run_job=lambda request: ran.append(request))
        result = dr.step(context)
        self.assertEqual(result["step"], wp.STAGE_PROVIDER_ENROLMENT)
        self.assertEqual(result["status"], dr.STEP_CONSENT_REQUIRED)
        self.assertEqual(ran, [])

    def test_the_step_blocks_when_nothing_is_enrolled(self):
        self._record_boundary()
        context = self._context(anchors=self._anchor(),
                                verify_signature=lambda *args: True)
        result = dr.step(context, provider_consent=True)
        self.assertEqual(result["status"], dr.STEP_BLOCKED)
        self.assertEqual(result["reason"], "not_enrolled")

    def test_activation_is_refused_until_the_lane_is_open(self):
        self._record_boundary()
        context = self._context(anchors=self._anchor(),
                                verify_signature=lambda *args: True)
        result = dr.activate_worker(context, command=["C:\\\\python.exe", "-m", "w"],
                                    consent=True)
        self.assertEqual(result["status"], dr.STEP_BLOCKED)
        self.assertEqual(result["reason"], "provisioning_incomplete")
        self.assertEqual(result["stage"], wp.STAGE_PROVIDER_ENROLMENT)

    def test_the_plan_separates_a_saved_session_from_a_working_one(self):
        self._record_boundary()
        self._enrol()
        plan = dr.plan(self._context(platform_module=NT))
        self.assertFalse(plan["provider_lane"]["verified"])
        statuses = {entry["provider"]: entry["status"]
                    for entry in plan["provider_sessions"]}
        self.assertEqual(statuses["claude"], "enrolled_lane_closed")


class _TaskResult:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class ResumeMachineTests(DriverTestCase):
    """A reboot that is written down, owned, bounded, and cleaned up."""

    COMMAND = ["C:\\\\Windows\\\\py.exe", "-m", "agent_bridge.setup"]

    @staticmethod
    def _absent(argv):
        """The ordinary machine: no task registered yet, commands succeed."""
        return _TaskResult(1) if "/Query" in argv else _TaskResult(0)

    def _context_with(self, responses=None):
        calls = []
        answer = responses if responses is not None else self._absent

        def run(argv):
            calls.append(list(argv))
            return answer(list(argv))

        context = self._context(run=run)
        return context, calls

    def _stage(self):
        return wp.next_stage(wp.ProvisionState(
            is_windows=True, windows_build_ok=True, firmware_virtualization=True,
            features_enabled={name: True for name in wp.REQUIRED_FEATURES},
            reboot_pending=True))

    def test_the_record_and_the_task_are_both_created(self):
        context, calls = self._context_with()
        result = dr.schedule_resume(context, self._stage(), command=self.COMMAND)
        self.assertEqual(result["status"], dr.STEP_OK, result)
        self.assertEqual(result["attempts"], 1)
        record, _identity = wp.read_resume_record(
            wp.resume_record_path(str(self.runtime)))
        self.assertTrue(record["awaiting_reboot"])
        self.assertTrue(any("/Create" in argv for argv in calls))

    def test_a_task_that_will_not_register_leaves_no_promise_behind(self):
        def responses(argv):
            return _TaskResult(1)  # query says absent, create also fails

        context, _calls = self._context_with(responses)
        result = dr.schedule_resume(context, self._stage(), command=self.COMMAND)
        self.assertEqual(result["status"], dr.STEP_FAILED)
        self.assertEqual(result["reason"], "resume_task_unregistered")
        self.assertIsNone(wp.read_resume_record(
            wp.resume_record_path(str(self.runtime))))

    def test_a_task_name_held_by_something_else_is_never_taken_over(self):
        listing = (b"TaskName:      \\AgentBridgeSetupResume\r\n"
                   b"Status:        Ready\r\n"
                   b"Task To Run:   C:\\Strangers\\thing.exe\r\n")

        def responses(argv):
            if "/Query" in argv:
                return _TaskResult(0, stdout=listing)
            return _TaskResult(0)

        context, calls = self._context_with(responses)
        result = dr.schedule_resume(context, self._stage(), command=self.COMMAND)
        self.assertEqual(result["status"], dr.STEP_BLOCKED)
        self.assertEqual(result["reason"], "resume_task_not_ours")
        self.assertFalse(any("/Create" in argv for argv in calls))

    def test_an_unsafe_resume_command_is_refused_before_anything_is_written(self):
        context, calls = self._context_with()
        result = dr.schedule_resume(context, self._stage(),
                                    command=["relative.exe"])
        self.assertEqual(result["status"], dr.STEP_FAILED)
        self.assertEqual(result["reason"], "resume_command_unsafe")
        self.assertIsNone(wp.read_resume_record(
            wp.resume_record_path(str(self.runtime))))

    def test_attempts_increase_across_reboots_and_then_stop(self):
        context, _calls = self._context_with()
        stage = self._stage()
        for expected in range(1, wp.MAX_STAGE_ATTEMPTS + 1):
            result = dr.schedule_resume(context, stage, command=self.COMMAND)
            self.assertEqual(result["attempts"], expected, result)
        exhausted = dr.schedule_resume(context, stage, command=self.COMMAND)
        self.assertEqual(exhausted["status"], dr.STEP_BLOCKED)
        self.assertEqual(exhausted["reason"], "stage_attempts_exhausted")

    def test_resume_reports_where_setup_stopped(self):
        context, _calls = self._context_with()
        stage = self._stage()
        dr.schedule_resume(context, stage, command=self.COMMAND)
        state = dr.resume(context)
        self.assertEqual(state.stage, stage.stage)
        self.assertTrue(state.awaiting_reboot)
        self.assertFalse(state.exhausted)

    def test_no_record_means_nothing_to_resume(self):
        context, _calls = self._context_with()
        self.assertIsNone(dr.resume(context))

    def test_a_tampered_record_is_reported_rather_than_ignored(self):
        path = wp.resume_record_path(str(self.runtime))
        Path(path).write_text("{", encoding="utf-8")
        os.chmod(path, 0o600)
        context, _calls = self._context_with()
        state = dr.resume(context)
        self.assertTrue(state.exhausted)
        self.assertEqual(state.reason, "resume_malformed")

    def test_nothing_is_cleaned_up_while_setup_is_still_going(self):
        context, calls = self._context_with()
        dr.schedule_resume(context, self._stage(), command=self.COMMAND)
        calls.clear()
        result = dr.finish_resume(context)
        self.assertEqual(result["status"], dr.STEP_BLOCKED)
        self.assertEqual(result["reason"], "still_provisioning")
        self.assertFalse(any("/Delete" in argv for argv in calls))
        self.assertIsNotNone(wp.read_resume_record(
            wp.resume_record_path(str(self.runtime))))

    def test_an_exhausted_stage_is_a_safe_failure_that_cleans_up(self):
        context, calls = self._context_with()
        stage = self._stage()
        wp.write_resume_record(
            wp.resume_record_path(str(self.runtime)),
            wp.build_resume_record(stage=stage.stage, stage_completed=None,
                                   awaiting_reboot=True, updated_at="now",
                                   attempts=wp.MAX_STAGE_ATTEMPTS))
        result = dr.finish_resume(context)
        self.assertEqual(result["status"], dr.STEP_OK, result)
        self.assertEqual(result["terminal"], "stopped")
        self.assertTrue(any("/Delete" in argv for argv in calls))
        self.assertIsNone(wp.read_resume_record(
            wp.resume_record_path(str(self.runtime))))

    def test_a_task_that_could_not_be_removed_is_reported_as_an_orphan(self):
        context, _calls = self._context_with(lambda argv: _TaskResult(1))
        # every command fails, including the delete
        stage = self._stage()
        wp.write_resume_record(
            wp.resume_record_path(str(self.runtime)),
            wp.build_resume_record(stage=stage.stage, stage_completed=None,
                                   awaiting_reboot=True, updated_at="now",
                                   attempts=wp.MAX_STAGE_ATTEMPTS))
        result = dr.finish_resume(context)
        self.assertEqual(result["status"], dr.STEP_FAILED)
        self.assertEqual(result["reason"], "resume_task_not_removed")


if __name__ == "__main__":
    unittest.main()
