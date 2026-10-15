"""Focused offline checks for registry mutation survivors."""
from __future__ import annotations

import datetime
import os
import unittest
import uuid
from unittest import mock

from harness import Sandbox
from agent_bridge import registry, store
from agent_bridge.errors import BrokerError, ErrorCategory


class RegistryMutationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.cfg = self.sandbox.cfg

    def tearDown(self) -> None:
        self.sandbox.cleanup()

    def identifier(self) -> str:
        return str(uuid.uuid4())

    def conversation(self, **changes) -> str:
        cid = self.identifier()
        record = {
            "conversation_id": cid,
            "caller": "codex",
            "peer_session_id": "synthetic-session",
            "turns": 0,
            "closed": False,
            "active_job_id": None,
        }
        record.update(changes)
        registry.create_conversation(self.cfg, record)
        return cid

    def test_create_conversation_persists_record(self) -> None:
        cid = self.conversation()
        self.assertEqual(registry.load_conversation(self.cfg, cid)["caller"], "codex")

    def test_load_conversation_returns_record(self) -> None:
        cid = self.identifier()
        store.atomic_write_json(self.cfg.conversation_path(cid), {"conversation_id": cid})
        self.assertEqual(registry.load_conversation(self.cfg, cid), {"conversation_id": cid})

    def test_claim_conversation_slot_refuses_exact_turn_limit(self) -> None:
        cid = self.conversation(turns=2)
        with self.assertRaises(BrokerError) as caught:
            registry.claim_conversation_slot(self.cfg, cid, "codex", self.identifier(), 2)
        self.assertEqual(caught.exception.category, ErrorCategory.CONVERSATION_TURN_LIMIT)

    def test_claim_grace_falls_back_on_invalid_limit(self) -> None:
        with mock.patch.object(self.cfg, "limit", side_effect=ValueError("invalid")):
            self.assertEqual(registry.claim_grace(self.cfg), float(registry.CLAIM_GRACE_SECONDS))

    def test_assert_conversation_ownership_rejects_other_job(self) -> None:
        cid = self.conversation(active_job_id=self.identifier())
        with self.assertRaises(BrokerError) as caught:
            registry.assert_conversation_ownership(self.cfg, cid, self.identifier())
        self.assertEqual(caught.exception.category, ErrorCategory.CONVERSATION_NOT_OWNED)

    def test_job_still_running_expires_at_grace_boundary(self) -> None:
        with (mock.patch.object(registry.time, "time", return_value=100.0),
              mock.patch.object(registry, "claim_grace", return_value=30.0)):
            self.assertFalse(registry._job_still_running(self.cfg, self.identifier(), 70.0))

    def test_claim_epoch_parses_synthetic_timestamp(self) -> None:
        instant = "2031-04-05T12:30:00+00:00"
        expected = datetime.datetime.fromisoformat(instant).timestamp()
        self.assertEqual(registry._claim_epoch({"active_job_claimed_at": instant}), expected)

    def test_retire_attempt_markers_renames_marker(self) -> None:
        job_id = self.identifier()
        attempt = store.secure_mkdir(os.path.join(self.cfg.job_dir(job_id), "attempts", "1"))
        marker = os.path.join(attempt, registry.INFLIGHT_MARKER)
        store.atomic_write_json(marker, {"phase": "peer_exited_uncommitted"})
        self.assertEqual(registry.retire_attempt_markers(self.cfg, job_id), ["1"])
        self.assertTrue(os.path.isfile(marker + ".committed"))

    def test_resolve_indeterminate_clears_hold(self) -> None:
        cid = self.conversation(indeterminate=True, indeterminate_job_id=self.identifier())
        self.assertFalse(registry.resolve_indeterminate(self.cfg, cid)["indeterminate"])
        self.assertFalse(registry.load_conversation(self.cfg, cid)["indeterminate"])

    def test_release_conversation_slot_increments_turns(self) -> None:
        owner = self.identifier()
        cid = self.conversation(active_job_id=owner, turns=2)
        self.assertEqual(registry.release_conversation_slot(
            self.cfg, cid, owner, increment_turns=True)["turns"], 3)

    def test_update_conversation_applies_changes(self) -> None:
        cid = self.conversation()
        self.assertEqual(registry.update_conversation(
            self.cfg, cid, peer_session_id="synthetic-next")["peer_session_id"],
            "synthetic-next")

    def test_write_status_starts_sequence_at_one(self) -> None:
        status = registry.write_status(self.cfg, self.identifier(), "queued")
        self.assertEqual(status["seq"], 1)

    def test_attach_worker_pid_records_pid(self) -> None:
        job_id = self.identifier()
        registry.write_status(self.cfg, job_id, "queued")
        self.assertEqual(registry.attach_worker_pid(self.cfg, job_id, 32145)["worker_pid"], 32145)

    def test_read_status_rejects_unknown_job(self) -> None:
        with self.assertRaises(BrokerError) as caught:
            registry.read_status(self.cfg, self.identifier())
        self.assertEqual(caught.exception.category, ErrorCategory.JOB_NOT_FOUND)

    def test_read_result_returns_saved_result(self) -> None:
        job_id = self.identifier()
        store.atomic_write_json(os.path.join(self.cfg.job_dir(job_id), "result.json"),
                                {"response": "synthetic answer"})
        self.assertEqual(registry.read_result(self.cfg, job_id),
                         {"response": "synthetic answer"})

    def test_pid_alive_rejects_missing_pid(self) -> None:
        self.assertFalse(registry.pid_alive(None))

    def test_inflight_attempts_finds_marker(self) -> None:
        job_id = self.identifier()
        attempt = store.secure_mkdir(os.path.join(self.cfg.job_dir(job_id), "attempts", "1"))
        store.atomic_write_json(os.path.join(attempt, registry.INFLIGHT_MARKER),
                                {"phase": "peer_exited_uncommitted"})
        self.assertEqual(registry.inflight_attempts(self.cfg, job_id)[0]["attempt"], "1")

    def test_group_identity_matches_verified_group(self) -> None:
        marker = {"pgid": 12345, "leader_pid": 23456, "leader_start": "synthetic-start"}
        with (mock.patch.object(registry.platform, "is_own_process_group", return_value=False),
              mock.patch.object(registry.platform, "process_group_identity",
                                return_value=("synthetic-start", "12345"))):
            self.assertEqual(registry._group_identity_matches(marker), (True, "identity verified"))

    def test_reap_orphaned_peers_leaves_unverified_group_alone(self) -> None:
        marker = {"attempt": "1", "pgid": 12345}
        with (mock.patch.object(registry, "inflight_attempts", return_value=[marker]),
              mock.patch.object(registry, "_group_identity_matches",
                                return_value=(False, "unverified")),
              mock.patch.object(registry.platform, "terminate_process_tree") as terminate):
            result = registry.reap_orphaned_peers(self.cfg, self.identifier())
        terminate.assert_not_called()
        self.assertFalse(result[0]["signalled"])

    def test_reconcile_terminal_job_skips_reaper(self) -> None:
        job_id = self.identifier()
        registry.write_status(self.cfg, job_id, "complete")
        with mock.patch.object(registry, "reap_orphaned_peers") as reap:
            self.assertEqual(registry.reconcile(self.cfg, job_id)["status"], "complete")
        reap.assert_not_called()

    def test_active_marker_uses_job_id(self) -> None:
        job_id = self.identifier()
        self.assertEqual(registry._active_marker(self.cfg, job_id),
                         self.cfg.state("active", job_id))

    def test_index_active_adds_running_job(self) -> None:
        job_id = self.identifier()
        registry.write_status(self.cfg, job_id, "running", worker_pid=os.getpid())
        self.assertTrue(os.path.isfile(self.cfg.state("active", job_id)))

    def test_count_active_counts_live_job(self) -> None:
        registry.write_status(self.cfg, self.identifier(), "running", worker_pid=os.getpid())
        self.assertEqual(registry.count_active(self.cfg), 1)

    def test_admission_gate_enters_context(self) -> None:
        with registry.admission_gate(self.cfg):
            self.assertTrue(True)

    def test_conversation_has_active_job_without_claim_is_false(self) -> None:
        cid = self.conversation()
        self.assertFalse(registry.conversation_has_active_job(self.cfg, cid))


if __name__ == "__main__":
    unittest.main()
