"""Tests for the machine-bound verification record.

These all run on the development machine, which is the point: they prove that
a portable test cannot produce a record the production loader will accept.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.orchestration import windows_evidence as we
from agent_bridge.orchestration import windows_wsl_provision as wp

ROOTFS = "a" * 64
RUNNER = "b" * 64
FINGERPRINT = "c" * 64
CANARIES = ("host-mount-absent", "wsl-interop-absent", "wsl-conf-sha256",
            "guest-runner-sha256", "pinned-versions", "network-egress-policy")


def _write_private(path: str, text: str) -> None:
    """Write a fixture record the way production writes one: owner-only."""

    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, text.encode("utf-8"))
    finally:
        os.close(descriptor)
    if os.name != "nt":
        os.chmod(path, 0o600)


def _open_lane() -> we.ProviderLane:
    """A lane with both live observations actually made."""

    return we.ProviderLane(verified=True, providers=("claude",),
                           portability_observed=True,
                           refresh_behaviour_observed=True,
                           detail="observed_live")


def _evidence(**extra) -> we.Evidence:
    fields = {
        "recorded_at": "2026-09-14T00:00:00Z",
        "host_fingerprint": FINGERPRINT,
        "wsl_version": "2.3.26.0",
        "rootfs_sha256": ROOTFS,
        "guest_runner_sha256": RUNNER,
        "canaries": CANARIES,
        "boundary_verified": True,
        "provider_lane": we.ProviderLane(),
    }
    fields.update(extra)
    return we.Evidence(**fields)


class ParseTests(unittest.TestCase):
    def test_a_well_formed_record_round_trips(self):
        parsed = we.parse_evidence(_evidence().as_dict())
        self.assertEqual(parsed.rootfs_sha256, ROOTFS)
        self.assertEqual(parsed.canaries, CANARIES)

    def test_an_extra_key_is_refused(self):
        payload = _evidence().as_dict()
        payload["extra"] = 1
        with self.assertRaisesRegex(we.EvidenceError, "evidence_keys_invalid"):
            we.parse_evidence(payload)

    def test_a_missing_key_is_refused(self):
        payload = _evidence().as_dict()
        del payload["boundary_verified"]
        with self.assertRaisesRegex(we.EvidenceError, "evidence_keys_invalid"):
            we.parse_evidence(payload)

    def test_a_future_schema_is_refused_rather_than_guessed_at(self):
        payload = _evidence().as_dict()
        payload["schema_version"] = we.SCHEMA_VERSION + 1
        with self.assertRaisesRegex(we.EvidenceError, "evidence_schema_unsupported"):
            we.parse_evidence(payload)

    def test_a_record_about_another_platform_is_refused(self):
        payload = _evidence().as_dict()
        payload["platform"] = "darwin"
        with self.assertRaisesRegex(we.EvidenceError, "evidence_platform_invalid"):
            we.parse_evidence(payload)

    def test_a_non_hex_hash_is_refused(self):
        payload = _evidence().as_dict()
        payload["rootfs_sha256"] = "not a hash"
        with self.assertRaisesRegex(we.EvidenceError,
                                    "evidence_rootfs_sha256_invalid"):
            we.parse_evidence(payload)

    def test_a_truncated_hash_is_refused(self):
        payload = _evidence().as_dict()
        payload["guest_runner_sha256"] = "b" * 63
        with self.assertRaisesRegex(we.EvidenceError,
                                    "evidence_guest_runner_sha256_invalid"):
            we.parse_evidence(payload)

    def test_a_boolean_boundary_flag_is_required(self):
        payload = _evidence().as_dict()
        payload["boundary_verified"] = "true"
        with self.assertRaisesRegex(we.EvidenceError, "evidence_boundary_invalid"):
            we.parse_evidence(payload)

    def test_a_malformed_timestamp_is_refused(self):
        payload = _evidence().as_dict()
        payload["recorded_at"] = "yesterday"
        with self.assertRaisesRegex(we.EvidenceError, "evidence_timestamp_invalid"):
            we.parse_evidence(payload)

    def test_a_provider_lane_missing_a_flag_is_refused(self):
        payload = _evidence().as_dict()
        del payload["provider_lane"]["refresh_behaviour_observed"]
        with self.assertRaisesRegex(we.EvidenceError,
                                    "evidence_provider_lane_invalid"):
            we.parse_evidence(payload)

    def test_a_provider_lane_with_a_non_boolean_flag_is_refused(self):
        payload = _evidence().as_dict()
        payload["provider_lane"]["verified"] = 1
        with self.assertRaisesRegex(we.EvidenceError,
                                    "evidence_provider_lane_invalid"):
            we.parse_evidence(payload)

    def test_a_record_that_is_not_an_object_is_refused(self):
        with self.assertRaisesRegex(we.EvidenceError, "evidence_not_an_object"):
            we.parse_evidence(["ready"])


class CheckTests(unittest.TestCase):
    def _check(self, evidence, **extra):
        kwargs = {"expected_rootfs_sha256": ROOTFS,
                  "expected_runner_sha256": RUNNER,
                  "required_canaries": CANARIES,
                  "fingerprint": FINGERPRINT}
        kwargs.update(extra)
        we.check_evidence(evidence, **kwargs)

    def test_a_matching_record_passes(self):
        self._check(_evidence())

    def test_a_record_from_another_machine_is_refused(self):
        with self.assertRaisesRegex(we.EvidenceError, "evidence_other_machine"):
            self._check(_evidence(), fingerprint="d" * 64)

    def test_a_record_for_a_replaced_image_is_refused(self):
        with self.assertRaisesRegex(we.EvidenceError, "evidence_image_changed"):
            self._check(_evidence(), expected_rootfs_sha256="e" * 64)

    def test_a_record_for_a_replaced_guest_runner_is_refused(self):
        with self.assertRaisesRegex(we.EvidenceError, "evidence_runner_changed"):
            self._check(_evidence(), expected_runner_sha256="f" * 64)

    def test_a_record_missing_a_canary_is_refused(self):
        partial = _evidence(canaries=CANARIES[:-1])
        with self.assertRaisesRegex(we.EvidenceError,
                                    "evidence_canaries_incomplete"):
            self._check(partial)

    def test_a_record_that_did_not_verify_the_boundary_is_refused(self):
        with self.assertRaisesRegex(we.EvidenceError,
                                    "evidence_boundary_not_verified"):
            self._check(_evidence(boundary_verified=False))


class ProviderLaneTests(unittest.TestCase):
    def test_the_default_lane_is_off(self):
        self.assertFalse(we.ProviderLane().enabled_for("claude"))

    def test_a_lane_without_portability_proof_is_off(self):
        lane = we.ProviderLane(verified=True, providers=("claude",),
                               refresh_behaviour_observed=True)
        self.assertFalse(lane.enabled_for("claude"))

    def test_a_lane_without_refresh_proof_is_off(self):
        lane = we.ProviderLane(verified=True, providers=("claude",),
                               portability_observed=True)
        self.assertFalse(lane.enabled_for("claude"))

    def test_a_lane_proven_for_one_provider_does_not_enable_the_other(self):
        lane = we.ProviderLane(verified=True, providers=("claude",),
                               portability_observed=True,
                               refresh_behaviour_observed=True)
        self.assertTrue(lane.enabled_for("claude"))
        self.assertFalse(lane.enabled_for("codex"))


class _StatLike:
    """A stat result with one field changed, since os.stat_result is fixed."""

    def __init__(self, info, inode):
        self._info = info
        self.st_ino = inode

    def __getattr__(self, name):
        return getattr(self._info, name)


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / we.EVIDENCE_NAME)

    def tearDown(self):
        self.temp.cleanup()

    def _load(self, **extra):
        kwargs = {"expected_rootfs_sha256": ROOTFS,
                  "expected_runner_sha256": RUNNER,
                  "required_canaries": CANARIES,
                  "fingerprint": FINGERPRINT}
        kwargs.update(extra)
        return we.load_verified_state(self.path, **kwargs)

    def _write(self, evidence, *, text=None):
        """Owner-only, because the loader refuses anything else."""
        _write_private(self.path,
                       text if text is not None
                       else json.dumps(evidence.as_dict()))

    def test_no_record_means_not_ready_and_says_why(self):
        state, evidence, reason = self._load()
        self.assertFalse(wp.delegation_may_be_enabled(state))
        self.assertIsNone(evidence)
        self.assertEqual(reason, "evidence_absent")

    def test_unparseable_json_is_not_ready(self):
        self._write(None, text="{")
        _, _, reason = self._load()
        self.assertEqual(reason, "evidence_malformed")

    def test_a_valid_record_produces_a_ready_state(self):
        self._write(_evidence(provider_lane=_open_lane()))
        state, evidence, reason = self._load()
        self.assertEqual(reason, "")
        self.assertTrue(wp.delegation_may_be_enabled(state))
        self.assertEqual(wp.current_stage(state), "ready")
        self.assertIsNotNone(evidence)

    def test_a_boundary_record_with_no_lane_stops_before_ready(self):
        """The record is valid; the lane it describes is simply not open."""
        self._write(_evidence())
        state, evidence, reason = self._load()
        self.assertEqual(reason, "")
        self.assertIsNotNone(evidence)
        self.assertFalse(wp.delegation_may_be_enabled(state))
        self.assertEqual(wp.current_stage(state), wp.STAGE_PROVIDER_ENROLMENT)

    def test_a_half_observed_lane_does_not_reach_ready(self):
        for lane in (we.ProviderLane(verified=True, providers=("claude",),
                                     portability_observed=True),
                     we.ProviderLane(verified=True, providers=("claude",),
                                     refresh_behaviour_observed=True),
                     we.ProviderLane(portability_observed=True,
                                     refresh_behaviour_observed=True,
                                     providers=("claude",)),
                     we.ProviderLane(verified=True, portability_observed=True,
                                     refresh_behaviour_observed=True)):
            with self.subTest(lane=lane.detail or lane.providers):
                self._write(_evidence(provider_lane=lane))
                state, _, reason = self._load()
                self.assertEqual(reason, "")
                self.assertFalse(wp.delegation_may_be_enabled(state))

    def test_a_record_copied_from_another_machine_does_not_enable_this_one(self):
        self._write(_evidence())
        state, evidence, reason = self._load(fingerprint="9" * 64)
        self.assertFalse(wp.delegation_may_be_enabled(state))
        self.assertIsNone(evidence)
        self.assertEqual(reason, "evidence_other_machine")

    def test_replacing_the_image_after_verification_disables_delegation(self):
        self._write(_evidence())
        state, _, reason = self._load(expected_rootfs_sha256="7" * 64)
        self.assertFalse(wp.delegation_may_be_enabled(state))
        self.assertEqual(reason, "evidence_image_changed")

    def test_a_directory_where_the_record_belongs_is_not_ready(self):
        Path(self.path).mkdir()
        _, _, reason = self._load()
        self.assertEqual(reason, "evidence_not_a_regular_file")

    def test_the_failure_reason_never_carries_a_path(self):
        _, _, reason = self._load()
        self.assertNotIn(self.temp.name, reason)
        self.assertNotIn("/", reason)

    # -- the gate is itself a security authority ---------------------------

    @unittest.skipIf(os.name == "nt", "POSIX mode bits")
    def test_a_record_anyone_could_read_is_not_evidence(self):
        self._write(_evidence())
        os.chmod(self.path, 0o644)
        state, evidence, reason = self._load()
        self.assertEqual(reason, "evidence_not_owner_only")
        self.assertIsNone(evidence)
        self.assertFalse(wp.delegation_may_be_enabled(state))

    @unittest.skipIf(os.name == "nt", "POSIX mode bits")
    def test_a_record_in_a_directory_anyone_could_read_is_not_evidence(self):
        self._write(_evidence())
        os.chmod(self.temp.name, 0o755)
        _, _, reason = self._load()
        self.assertEqual(reason, "evidence_root_not_owner_only")

    def test_a_record_reached_through_a_link_is_not_evidence(self):
        self._write(_evidence())
        alias = Path(self.temp.name).parent / f"alias-{os.getpid()}"
        alias.symlink_to(self.temp.name)
        self.addCleanup(alias.unlink)
        _, _, reason = we.load_verified_state(
            str(alias / we.EVIDENCE_NAME), expected_rootfs_sha256=ROOTFS,
            expected_runner_sha256=RUNNER, required_canaries=CANARIES,
            fingerprint=FINGERPRINT, runtime_root=str(alias))
        self.assertEqual(reason, "evidence_path_traverses_a_link")

    def test_a_record_that_is_a_symlink_is_not_evidence(self):
        real = Path(self.temp.name) / "real.json"
        _write_private(str(real), json.dumps(_evidence().as_dict()))
        Path(self.path).symlink_to(real)
        _, _, reason = self._load()
        self.assertEqual(reason, "evidence_path_traverses_a_link")

    def test_a_record_replaced_mid_read_is_refused(self):
        """The bytes and the checks must come from the same open file."""
        self._write(_evidence())
        real_fstat = os.fstat
        seen: list[int] = []

        def drifting(descriptor):
            info = real_fstat(descriptor)
            seen.append(descriptor)
            if len(seen) > 1:
                # The second look, taken after the read, sees a different
                # object: exactly what a swap underneath the reader looks like.
                return _StatLike(info, info.st_ino + 1)
            return info

        with mock.patch.object(we.wpv.os, "fstat", drifting):
            _, _, reason = self._load()
        self.assertEqual(reason, "evidence_changed_while_reading")

    def test_the_bytes_come_from_the_descriptor_that_was_checked(self):
        """A reader that re-opened the name would read the replacement."""
        self._write(_evidence())
        target = Path(self.path)
        opened: list[str] = []
        real_open = we.wpv.os.open

        def counting(path, *args, **kwargs):
            opened.append(str(path))
            return real_open(path, *args, **kwargs)

        with mock.patch.object(we.wpv.os, "open", counting):
            we.read_evidence(self.path, runtime_root=str(target.parent))
        self.assertEqual([name for name in opened if name == str(target)],
                         [str(target)])

    def test_a_platform_that_cannot_verify_an_acl_is_refused(self):
        self._write(_evidence())
        _, _, reason = we.load_verified_state(
            self.path, expected_rootfs_sha256=ROOTFS,
            expected_runner_sha256=RUNNER, required_canaries=CANARIES,
            fingerprint=FINGERPRINT, platform=object())
        self.assertEqual(reason, "evidence_acl_unenforceable")


class RecordTests(unittest.TestCase):
    """The gate that keeps a development machine from marking Windows ready."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "sub" / we.EVIDENCE_NAME)

    def tearDown(self):
        self.temp.cleanup()

    def test_writing_a_record_off_windows_is_refused(self):
        with self.assertRaisesRegex(we.EvidenceError, "evidence_requires_windows"):
            we.record_verification(self.path, _evidence(), os_name="posix")
        self.assertFalse(Path(self.path).exists())

    @unittest.skipIf(os.name == "nt", "POSIX mode bits")
    def test_a_record_is_written_owner_only(self):
        we.record_verification(self.path, _evidence(), os_name="nt")
        self.assertEqual(Path(self.path).stat().st_mode & 0o777, 0o600)
        self.assertEqual(we.read_evidence(self.path).rootfs_sha256, ROOTFS)

    def test_a_record_that_cannot_be_locked_down_is_not_written(self):
        def refuse(_fd):
            raise OSError("no acl")

        with self.assertRaisesRegex(we.EvidenceError, "evidence_acl_unverified"):
            we.record_verification(self.path, _evidence(), os_name="nt",
                                   secure=refuse)
        self.assertFalse(Path(self.path).exists())

    def test_a_failed_write_never_truncates_the_record_already_there(self):
        """Losing a good record looks exactly like tampering. It must not."""
        we.record_verification(self.path, _evidence(), os_name="nt")
        before = Path(self.path).read_bytes()

        def refuse(_fd):
            raise OSError("no acl")

        with self.assertRaises(we.EvidenceError):
            we.record_verification(self.path, _evidence(rootfs_sha256="9" * 64),
                                   os_name="nt", secure=refuse)
        self.assertEqual(Path(self.path).read_bytes(), before)

    def test_a_failed_write_leaves_no_temporary_behind(self):
        we.record_verification(self.path, _evidence(), os_name="nt")

        def refuse(_fd):
            raise OSError("no acl")

        with self.assertRaises(we.EvidenceError):
            we.record_verification(self.path, _evidence(), os_name="nt",
                                   secure=refuse)
        leftovers = [name for name in os.listdir(Path(self.path).parent)
                     if name.startswith(".tmp-")]
        self.assertEqual(leftovers, [])

    def test_a_rewrite_replaces_the_record_atomically(self):
        we.record_verification(self.path, _evidence(), os_name="nt")
        we.record_verification(self.path, _evidence(rootfs_sha256="9" * 64),
                               os_name="nt")
        self.assertEqual(we.read_evidence(self.path).rootfs_sha256, "9" * 64)

    def test_there_is_no_argument_that_writes_an_unprotected_record(self):
        """`secure=None` must mean the default protection, never none."""
        import inspect
        signature = inspect.signature(we.record_verification)
        self.assertIsNone(signature.parameters["secure"].default)
        we.record_verification(self.path, _evidence(), os_name="nt", secure=None)
        if os.name != "nt":
            self.assertEqual(Path(self.path).stat().st_mode & 0o777, 0o600)


class FingerprintTests(unittest.TestCase):
    def test_a_fingerprint_is_a_sha256_and_not_the_raw_identity(self):
        value = we.host_fingerprint(machine_guid="GUID-1", node="host-a")
        self.assertRegex(value, r"^[0-9a-f]{64}$")
        self.assertNotIn("GUID-1", value)

    def test_different_machines_fingerprint_differently(self):
        self.assertNotEqual(we.host_fingerprint(machine_guid="GUID-1", node="a"),
                            we.host_fingerprint(machine_guid="GUID-2", node="a"))

    def test_the_same_machine_fingerprints_stably(self):
        self.assertEqual(we.host_fingerprint(machine_guid="G", node="a"),
                         we.host_fingerprint(machine_guid="G", node="a"))

    def test_a_missing_machine_guid_still_produces_a_fingerprint(self):
        self.assertRegex(we.host_fingerprint(machine_guid=None, node="a"),
                         r"^[0-9a-f]{64}$")


class LimitationTests(unittest.TestCase):
    def test_the_limitations_are_stated_in_code(self):
        self.assertIn("live Windows host", we.LIVE_EVIDENCE_LIMITATION)
        self.assertIn("portability", we.PROVIDER_LANE_LIMITATION)
        self.assertIn("refresh", we.PROVIDER_LANE_LIMITATION)


if __name__ == "__main__":
    unittest.main()
