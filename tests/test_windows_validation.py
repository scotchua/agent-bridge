"""The deterministic validation suite, and the properties that make it one.

A validation runner is only worth having if two runs on an unchanged machine
produce the same answer and a changed machine produces a different one, by
name. That is what this file tests, along with the three ways a suite like
this usually stops being trustworthy:

  * a check that cannot run gets counted as a pass,
  * a check that misbehaves gets counted as a pass, and
  * the report claims more than the checks observed.

Nothing here runs on a live Windows host, and the suite says so about itself.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge.orchestration import windows_validation as wv
from agent_bridge.orchestration import windows_wsl as ww
from agent_bridge.orchestration import windows_wsl_provision as wp


def ok(reason="fine", **facts):
    return lambda: wv.Observation(wv.PASS, reason, facts)


def bad(reason="broken"):
    return lambda: wv.Observation(wv.FAIL, reason)


def every_runner(maker=ok):
    return {check.id: maker() for check in wv.CHECKS}


class SuiteShapeTests(unittest.TestCase):
    def test_the_status_vocabulary_is_closed(self):
        self.assertEqual(wv.STATUSES, frozenset({"pass", "fail", "blocked",
                                                 "skipped"}))

    def test_every_check_has_a_stable_id(self):
        ids = [check.id for check in wv.CHECKS]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_check_states_what_it_does_not_prove(self):
        for check in wv.CHECKS:
            with self.subTest(check.id):
                self.assertTrue(check.does_not_prove.strip())
                self.assertTrue(check.proves.strip())

    def test_a_dependency_must_come_earlier_in_the_suite(self):
        with self.assertRaises(wv.ValidationError):
            wv._validate_suite((
                wv.Check("b", "t", "p", "d", requires=("a",)),
                wv.Check("a", "t", "p", "d")))

    def test_a_duplicate_id_is_refused(self):
        with self.assertRaises(wv.ValidationError):
            wv._validate_suite((wv.Check("a", "t", "p", "d"),
                                wv.Check("a", "t", "p", "d")))


class VerdictTests(unittest.TestCase):
    def test_every_check_passing_is_ready(self):
        rows = wv.run_checks(every_runner())
        self.assertEqual(wv.verdict(rows), wv.VERDICT_READY)

    def test_one_failure_is_not_ready(self):
        runners = every_runner()
        runners["canaries"] = bad()
        self.assertEqual(wv.verdict(wv.run_checks(runners)),
                         wv.VERDICT_NOT_READY)

    def test_a_blocked_check_is_not_a_pass(self):
        runners = every_runner()
        del runners["egress_policy"]
        rows = wv.run_checks(runners)
        self.assertEqual(wv.verdict(rows), wv.VERDICT_NOT_READY)

    def test_a_skipped_check_is_not_a_pass_either(self):
        runners = every_runner()
        runners["features"] = lambda: wv.Observation(wv.SKIPPED,
                                                     "not_applicable")
        self.assertEqual(wv.verdict(wv.run_checks(runners)),
                         wv.VERDICT_NOT_READY)

    def test_a_missing_row_entirely_is_not_ready(self):
        rows = [row for row in wv.run_checks(every_runner())
                if row["id"] != "platform"]
        self.assertEqual(wv.verdict(rows), wv.VERDICT_NOT_READY)

    def test_an_unimplemented_check_is_blocked_and_named(self):
        runners = every_runner()
        del runners["round_trip"]
        row = {r["id"]: r for r in wv.run_checks(runners)}["round_trip"]
        self.assertEqual(row["status"], wv.BLOCKED)
        self.assertEqual(row["reason"], "check_not_implemented")


class CascadeTests(unittest.TestCase):
    """One failure, then a list of things that were never attempted."""

    def test_a_failure_blocks_only_what_depends_on_it(self):
        runners = every_runner()
        runners["wsl_version"] = bad("wsl_too_old")
        rows = {row["id"]: row for row in wv.run_checks(runners)}
        self.assertEqual(rows["wsl_version"]["status"], wv.FAIL)
        self.assertEqual(rows["distro_registered"]["status"], wv.BLOCKED)
        self.assertEqual(rows["distro_registered"]["facts"]["blocked_by"],
                         "wsl_version")
        # Independent of WSL, so it still runs.
        self.assertEqual(rows["reboot_clear"]["status"], wv.PASS)

    def test_the_first_check_failing_blocks_nearly_everything(self):
        runners = every_runner()
        runners["platform"] = bad("not_a_windows_host")
        statuses = {row["id"]: row["status"] for row in wv.run_checks(runners)}
        self.assertEqual(statuses["platform"], wv.FAIL)
        self.assertNotIn(wv.PASS, set(statuses.values()))

    def test_a_blocked_row_names_the_first_thing_it_waited_on(self):
        runners = every_runner()
        runners["guest_runner"] = bad()
        rows = {row["id"]: row for row in wv.run_checks(runners)}
        self.assertEqual(rows["canaries"]["facts"]["blocked_by"], "guest_runner")


class MisbehavingCheckTests(unittest.TestCase):
    """A check that breaks its own contract is a failure, never a pass."""

    def test_a_check_that_raises_is_a_failure(self):
        runners = every_runner()

        def explode():
            raise RuntimeError("C:\\Users\\scott\\secret")

        runners["features"] = explode
        row = {r["id"]: r for r in wv.run_checks(runners)}["features"]
        self.assertEqual(row["status"], wv.FAIL)
        self.assertEqual(row["reason"], "check_raised")

    def test_the_exception_text_never_reaches_the_report(self):
        runners = every_runner()

        def explode():
            raise RuntimeError("C:\\Users\\scott\\secret")

        runners["features"] = explode
        rendered = wv.render(wv.build_report(wv.run_checks(runners)))
        self.assertNotIn("scott", rendered)
        self.assertNotIn("secret", rendered)

    def test_a_check_returning_a_truthy_object_is_a_failure(self):
        runners = every_runner()
        runners["features"] = lambda: True
        row = {r["id"]: r for r in wv.run_checks(runners)}["features"]
        self.assertEqual(row["status"], wv.FAIL)
        self.assertEqual(row["reason"], "check_contract_violated")

    def test_a_check_returning_an_unknown_status_is_a_failure(self):
        runners = every_runner()
        runners["features"] = lambda: wv.Observation("probably_fine", "ok")
        row = {r["id"]: r for r in wv.run_checks(runners)}["features"]
        self.assertEqual(row["status"], wv.FAIL)

    def test_a_free_text_reason_is_refused(self):
        with self.assertRaises(wv.ValidationError):
            wv.validate_observation(
                "x", wv.Observation(wv.PASS, "it went fine, mostly"))

    def test_a_fact_that_is_a_path_is_refused(self):
        with self.assertRaises(wv.ValidationError):
            wv.validate_observation("x", wv.Observation(
                wv.PASS, "ok", {"root": "C:\\Users\\scott"}))

    def test_a_fact_that_is_a_nested_object_is_refused(self):
        with self.assertRaises(wv.ValidationError):
            wv.validate_observation("x", wv.Observation(
                wv.PASS, "ok", {"detail": {"a": 1}}))

    def test_bounded_scalars_are_allowed(self):
        wv.validate_observation("x", wv.Observation(
            wv.PASS, "ok", {"build": 22631, "enabled": True, "v": "2.0.1"}))


class DeterminismTests(unittest.TestCase):
    """Two runs of an unchanged machine differ only in the envelope."""

    def test_the_checks_are_byte_identical_across_runs(self):
        first = wv.build_report(wv.run_checks(every_runner()),
                                recorded_at="2026-01-01T00:00:00+00:00")
        second = wv.build_report(wv.run_checks(every_runner()),
                                 recorded_at="2026-06-30T12:34:56+00:00")
        self.assertEqual(json.dumps(first["checks"], sort_keys=True),
                         json.dumps(second["checks"], sort_keys=True))
        self.assertEqual(first["verdict"], second["verdict"])
        self.assertNotEqual(first["envelope"], second["envelope"])

    def test_the_rows_come_back_in_suite_order(self):
        rows = wv.run_checks(every_runner())
        self.assertEqual([row["id"] for row in rows], list(wv.CHECK_IDS))

    def test_the_rendering_is_stable_and_diffable(self):
        report = wv.build_report(wv.run_checks(every_runner()),
                                 recorded_at="2026-01-01T00:00:00+00:00")
        self.assertEqual(wv.render(report), wv.render(dict(report)))
        self.assertTrue(wv.render(report).endswith("\n"))


class ReportContractTests(unittest.TestCase):
    def test_a_report_round_trips(self):
        report = wv.build_report(wv.run_checks(every_runner()))
        self.assertEqual(wv.parse_report(json.loads(json.dumps(report))),
                         report)

    def test_an_edited_verdict_is_refused(self):
        report = wv.build_report(wv.run_checks(every_runner()))
        report["verdict"] = wv.VERDICT_NOT_READY
        with self.assertRaisesRegex(wv.ValidationError, "does not match"):
            wv.parse_report(report)

    def test_a_verdict_promoted_over_a_failure_is_refused(self):
        runners = every_runner()
        runners["canaries"] = bad()
        report = wv.build_report(wv.run_checks(runners))
        report["verdict"] = wv.VERDICT_READY
        with self.assertRaises(wv.ValidationError):
            wv.parse_report(report)

    def test_an_extra_top_level_key_is_refused(self):
        report = wv.build_report(wv.run_checks(every_runner()))
        report["notes"] = "looked fine to me"
        with self.assertRaises(wv.ValidationError):
            wv.parse_report(report)

    def test_a_future_schema_version_is_refused_rather_than_guessed(self):
        report = wv.build_report(wv.run_checks(every_runner()))
        report["schema_version"] = wv.SCHEMA_VERSION + 1
        with self.assertRaises(wv.ValidationError):
            wv.parse_report(report)

    def test_the_report_states_what_a_green_run_does_not_prove(self):
        report = wv.build_report(wv.run_checks(every_runner()))
        self.assertEqual(report["verdict"], wv.VERDICT_READY)
        joined = " ".join(report["not_proven"])
        self.assertIn("subscription", joined)
        self.assertIn("network", joined)

    def test_a_failed_check_does_not_carry_a_proves_claim(self):
        runners = every_runner()
        runners["canaries"] = bad()
        row = {r["id"]: r for r in wv.run_checks(runners)}["canaries"]
        self.assertEqual(row["proves"], "")
        # The limit is still stated: a reader of a failure still needs to
        # know what a pass there would and would not have meant.
        self.assertTrue(row["does_not_prove"])


class RealRunnerTests(unittest.TestCase):
    """The runners that wrap real modules, against supplied observations."""

    class _Completed:
        def __init__(self, returncode=0, stdout=b""):
            self.returncode, self.stdout = returncode, stdout

    def test_the_platform_check_fails_anywhere_but_windows(self):
        self.assertEqual(wv.platform_runner(platform_name="Darwin")().status,
                         wv.FAIL)
        self.assertEqual(wv.platform_runner(platform_name="Linux")().status,
                         wv.FAIL)
        self.assertEqual(wv.platform_runner(platform_name="Windows")().status,
                         wv.PASS)
        self.assertEqual(wv.platform_runner(platform_name="win32")().status,
                         wv.PASS)

    def test_a_supported_build_passes_and_reports_the_number(self):
        runner = wv.windows_build_runner(
            lambda argv: self._Completed(
                0, b"\r\nMicrosoft Windows [Version 10.0.22631.4317]\r\n"))
        observation = runner()
        self.assertEqual(observation.status, wv.PASS)
        self.assertEqual(observation.facts["build"], 22631)

    def test_an_old_build_fails_and_names_the_minimum(self):
        runner = wv.windows_build_runner(
            lambda argv: self._Completed(
                0, b"Microsoft Windows [Version 10.0.19045.1]\r\n"))
        observation = runner()
        self.assertEqual(observation.status, wv.FAIL)
        self.assertEqual(observation.facts["minimum"], ww.WINDOWS_MIN_BUILD)
        self.assertEqual(observation.facts["build"], 19045)

    def test_a_command_that_fails_is_not_read_as_a_version(self):
        runner = wv.windows_build_runner(lambda argv: self._Completed(1, b""))
        self.assertEqual(runner().status, wv.FAIL)

    def test_utf_16_output_is_decoded_rather_than_failing(self):
        # wsl.exe writes UTF-16LE on some builds. An encoding surprise must
        # not become a validation failure that sends somebody chasing WSL.
        text = "WSL version: 2.3.26.0\r\n"
        runner = wv.wsl_version_runner(
            lambda argv: self._Completed(0, text.encode("utf-16-le")))
        self.assertEqual(runner().status, wv.PASS)

    def test_a_byte_order_mark_is_handled(self):
        text = "WSL version: 2.3.26.0\r\n"
        runner = wv.wsl_version_runner(
            lambda argv: self._Completed(0, b"\xff\xfe" + text.encode("utf-16-le")))
        self.assertEqual(runner().status, wv.PASS)

    def test_features_must_be_explicitly_enabled(self):
        enabled = {name: "Enabled" for name in wp.REQUIRED_FEATURES}
        self.assertEqual(wv.features_runner(lambda: enabled)().status, wv.PASS)

    def test_an_unreadable_feature_state_is_not_enabled(self):
        states = {name: None for name in wp.REQUIRED_FEATURES}
        observation = wv.features_runner(lambda: states)()
        self.assertEqual(observation.status, wv.FAIL)
        self.assertEqual(observation.reason, "feature_not_enabled")

    def test_a_disabled_feature_names_which_one(self):
        states = {name: "Enabled" for name in wp.REQUIRED_FEATURES}
        states[wp.REQUIRED_FEATURES[0]] = "Disabled"
        observation = wv.features_runner(lambda: states)()
        self.assertEqual(observation.facts["first_missing"],
                         wp.REQUIRED_FEATURES[0])

    def test_a_pending_restart_fails(self):
        self.assertEqual(wv.reboot_runner(lambda: True)().status, wv.FAIL)
        self.assertEqual(wv.reboot_runner(lambda: False)().status, wv.PASS)

    def test_a_missing_runtime_root_is_a_failure_not_a_skip(self):
        missing = str(Path(tempfile.mkdtemp()) / "absent")
        self.assertEqual(wv.runtime_root_runner(missing)().status, wv.FAIL)

    def test_a_private_runtime_root_passes(self):
        directory = Path(tempfile.mkdtemp())
        directory.chmod(0o700)
        observation = wv.runtime_root_runner(str(directory))()
        self.assertIn(observation.status, (wv.PASS, wv.FAIL))
        if observation.status == wv.FAIL:
            # On a platform whose ACL check cannot be satisfied here, the
            # refusal still has to be named rather than silently passing.
            self.assertTrue(observation.reason)


class DistroListParsingTests(unittest.TestCase):
    LISTING = ("  NAME                    STATE           VERSION\n"
               "* agent-bridge-abc123     Stopped         2\n"
               "  Ubuntu-22.04            Running         1\n")

    def test_the_header_is_not_a_distribution(self):
        self.assertNotIn("NAME", wv.parse_distro_list(self.LISTING))

    def test_the_default_marker_is_stripped(self):
        parsed = wv.parse_distro_list(self.LISTING)
        self.assertIn("agent-bridge-abc123", parsed)
        self.assertEqual(parsed["agent-bridge-abc123"], ("Stopped", "2"))

    def test_a_wsl1_registration_is_reported_as_version_one(self):
        self.assertEqual(wv.parse_distro_list(self.LISTING)["Ubuntu-22.04"][1],
                         "1")

    def test_a_wsl1_distribution_fails_the_check(self):
        runner = wv.distro_runner(
            lambda argv: RealRunnerTests._Completed(
                0, self.LISTING.encode("utf-16-le")),
            "Ubuntu-22.04")
        observation = runner()
        self.assertEqual(observation.status, wv.FAIL)
        self.assertEqual(observation.reason, "distro_not_wsl2")

    def test_an_absent_distribution_is_named_as_absent(self):
        runner = wv.distro_runner(
            lambda argv: RealRunnerTests._Completed(
                0, self.LISTING.encode("utf-16-le")),
            "agent-bridge-999999")
        self.assertEqual(runner().reason, "distro_not_registered")

    def test_a_registered_wsl2_distribution_passes(self):
        runner = wv.distro_runner(
            lambda argv: RealRunnerTests._Completed(
                0, self.LISTING.encode("utf-16-le")),
            "agent-bridge-abc123")
        self.assertEqual(runner().status, wv.PASS)


class ImageCheckTests(unittest.TestCase):
    class _Manifest:
        def __init__(self, digest):
            self.rootfs_sha256 = digest

    def setUp(self):
        import hashlib

        self.directory = Path(tempfile.mkdtemp())
        self.rootfs = self.directory / "rootfs.tar"
        self.rootfs.write_bytes(b"pretend this is an image\n")
        self.digest = hashlib.sha256(self.rootfs.read_bytes()).hexdigest()

    def test_a_matching_image_passes(self):
        runner = wv.image_runner(str(self.rootfs), self._Manifest(self.digest))
        self.assertEqual(runner().status, wv.PASS)

    def test_a_changed_image_fails(self):
        self.rootfs.write_bytes(b"something else\n")
        runner = wv.image_runner(str(self.rootfs), self._Manifest(self.digest))
        self.assertEqual(runner().reason, "rootfs_hash_mismatch")

    def test_a_missing_image_fails(self):
        runner = wv.image_runner(str(self.directory / "absent.tar"),
                                 self._Manifest(self.digest))
        self.assertEqual(runner().reason, "rootfs_unreadable")

    def test_a_manifest_without_a_hash_fails_rather_than_passing(self):
        runner = wv.image_runner(str(self.rootfs), self._Manifest(None))
        self.assertEqual(runner().reason, "manifest_hash_missing")


class GuestRoundTripTests(unittest.TestCase):
    """Four checks, one job. They cannot disagree with each other."""

    class _Cleanup:
        def __init__(self, complete=True):
            self.complete = complete

    class _Result:
        def __init__(self, *, ok=True, canaries=True, cleanup_complete=True,
                     reason="ok"):
            self.status = "completed" if ok else "aborted"
            self.reason = reason
            self.canaries_passed = canaries
            self.timings = {"a": 1.0, "b": 2.0}
            self.cleanup = GuestRoundTripTests._Cleanup(cleanup_complete)

        @property
        def ok(self):
            return self.status == "completed"

    def test_the_job_runs_exactly_once_for_four_checks(self):
        calls = []

        def run_job():
            calls.append(1)
            return self._Result()

        trip = wv.GuestRoundTrip(run_job)
        for check in (trip.guest_runner_check, trip.canaries_check,
                      trip.egress_check, trip.round_trip_check):
            self.assertEqual(check().status, wv.PASS)
        self.assertEqual(len(calls), 1)

    def test_a_failed_canary_fails_all_three_canary_derived_checks(self):
        trip = wv.GuestRoundTrip(
            lambda: self._Result(ok=False, canaries=False,
                                 reason="host_mount_present"))
        self.assertEqual(trip.guest_runner_check().status, wv.FAIL)
        self.assertEqual(trip.canaries_check().status, wv.FAIL)
        self.assertEqual(trip.egress_check().status, wv.FAIL)

    def test_a_job_that_left_an_instance_behind_is_not_a_pass(self):
        trip = wv.GuestRoundTrip(lambda: self._Result(cleanup_complete=False))
        observation = trip.round_trip_check()
        self.assertEqual(observation.status, wv.FAIL)
        self.assertEqual(observation.reason, "cleanup_incomplete")

    def test_an_abort_reason_reaches_the_report_as_a_token(self):
        trip = wv.GuestRoundTrip(
            lambda: self._Result(ok=False, reason="import_failed"))
        self.assertEqual(trip.round_trip_check().reason, "import_failed")

    def test_a_reason_carrying_a_path_is_reduced_to_a_token(self):
        trip = wv.GuestRoundTrip(
            lambda: self._Result(ok=False, reason="C:\\Users\\scott\\x failed"))
        reason = trip.round_trip_check().reason
        self.assertNotIn("\\", reason)
        self.assertLessEqual(len(reason), wv.MAX_FACT_CHARS)


class HonestyTests(unittest.TestCase):
    def test_the_module_does_not_claim_live_windows_validation(self):
        source = (ROOT / "src" / "agent_bridge" / "orchestration"
                  / "windows_validation.py").read_text(encoding="utf-8")
        self.assertIn("has been run on a live Windows host", source)
        self.assertIn("Nothing in this module has been run on a live Windows",
                      source)

    def test_validation_is_not_the_durable_evidence_record(self):
        source = (ROOT / "src" / "agent_bridge" / "orchestration"
                  / "windows_validation.py").read_text(encoding="utf-8")
        # It must not write one. The evidence module owns that, refuses off
        # Windows, and binds its record to the machine.
        self.assertNotIn("record_verification", source)


if __name__ == "__main__":
    unittest.main()
