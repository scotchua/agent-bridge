"""The delegation audit report.

The spec this was built to says a generic error class name is not acceptable
diagnostic evidence, and that the report must show eligible, routed, retained,
bypassed and failed work with reasons. So these tests check the numbers add
up, and they check the two things an audit is easiest to get wrong:

* that a failure row carries the harness's own detail and not just
  ``TaskError``;
* that the bypass column distinguishes what the gate observed from what its
  hook surface cannot see, so a zero is never mistaken for an absence.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge import store  # noqa: E402
from agent_bridge.orchestration import audit, autodecide, autoroute, gate  # noqa: E402


class AuditCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.state = self.base / "state"
        self.routing = self.state / "routing"
        self.routing.mkdir(parents=True)
        self.home = self.base / "home"
        self.home.mkdir()
        self.now = 1_000_000.0

    def decision(self, **fields):
        row = {"event": "routing_decided", "decided_at": self.now,
               "repo": str(self.base / "repo"), "owner_route": "claude",
               "decision": "self", "code": "retained_repo_unclassified",
               "reason": "nobody classified it", "automatic": True,
               "considered": {"client": "claude", "peer": "codex",
                              "classification": "unclassified",
                              "allowed_routes": []}}
        row.update(fields)
        store.append_ledger(str(self.routing / gate.AUDIT_LEDGER), row)

    def event(self, **fields):
        row = {"at": self.now, "client": "claude", "tool": "Edit",
               "permission": "allow", "code": "routing_receipt_valid", "repos": []}
        row.update(fields)
        store.append_ledger(str(self.routing / gate.EVENT_LEDGER), row)

    def run_audit(self, **kwargs):
        return audit.report(str(self.state), home=str(self.home),
                            clock=lambda: self.now + 1, since_hours=0.0, **kwargs)


class Counting(AuditCase):
    def test_routed_retained_and_eligible_are_counted_separately(self):
        self.decision()                                         # retained
        self.decision(code="retained_no_fresh_capacity", considered={
            "client": "claude", "peer": "codex",
            "classification": "internal_nonclient",
            "allowed_routes": ["claude", "codex"]})              # retained, eligible
        self.decision(code="routed_peer_implementation", owner_route="codex",
                      decision="peer", considered={
                          "client": "claude", "peer": "codex",
                          "classification": "public",
                          "allowed_routes": ["claude", "codex"]})  # routed, eligible
        document = self.run_audit()
        self.assertEqual(document["routed"]["count"], 1)
        self.assertEqual(document["retained"]["count"], 2)
        self.assertEqual(document["eligible"]["count"], 2)
        self.assertEqual(document["routed"]["by_route"], {"codex": 1})

    def test_every_routed_and_retained_row_carries_its_reason(self):
        self.decision(reason="a specific stated reason")
        document = self.run_audit()
        self.assertEqual(document["retained"]["reasons"][0]["reason"],
                         "a specific stated reason")
        self.assertIn("code", document["retained"]["reasons"][0])

    def test_unclassified_and_client_derived_work_is_not_eligible(self):
        for classification in ("unclassified", "client_derived"):
            self.decision(considered={"client": "claude", "peer": "codex",
                                      "classification": classification,
                                      "allowed_routes": ["claude", "codex"]})
        self.assertEqual(self.run_audit()["eligible"]["count"], 0)

    def test_automatic_and_agent_requested_decisions_are_told_apart(self):
        self.decision(automatic=True)
        row = {"event": "routing_decided", "decided_at": self.now,
               "repo": str(self.base / "repo"), "owner_route": "claude",
               "decision": "self", "reason": "an agent asked for this one"}
        store.append_ledger(str(self.routing / gate.AUDIT_LEDGER), row)
        share = self.run_audit()["automatic_share"]
        self.assertEqual(share["decisions"], 2)
        self.assertEqual(share["made_automatically"], 1)
        self.assertEqual(share["made_by_an_agent_calling_routing_decide"], 1)

    def test_the_window_excludes_older_records(self):
        self.decision(decided_at=self.now - 100_000)
        self.decision(decided_at=self.now)
        recent = audit.report(str(self.state), home=str(self.home),
                              clock=lambda: self.now + 1, since_hours=1.0)
        self.assertEqual(recent["automatic_share"]["decisions"], 1)
        everything = self.run_audit()
        self.assertEqual(everything["automatic_share"]["decisions"], 2)

    def test_an_unreadable_ledger_line_is_kept_rather_than_dropped(self):
        path = self.routing / gate.AUDIT_LEDGER
        path.write_text('{"event":"routing_decided","code":"x"}\nnot json\n',
                        encoding="utf-8")
        document = self.run_audit()
        # The bad line is not a decision, but it was not silently discarded
        # either: it survives as an entry with an error marker.
        entries = audit._read_ledger(str(path), None)
        self.assertTrue(any(entry.get("error") == "unreadable_line" for entry in entries))
        self.assertEqual(document["automatic_share"]["decisions"], 1)


class Bypasses(AuditCase):
    def test_work_routed_but_never_dispatched_is_an_observed_bypass(self):
        intents = self.routing / autodecide.INTENT_DIR
        intents.mkdir()
        store.atomic_write_json(str(intents / "abc.json"), {
            "version": 1, "repo": "/repo", "route": "codex",
            "code": "routed_peer_implementation", "reason": "because",
            "state": "awaiting_brief", "next_call": "execution_dispatch",
            "created_at": self.now})
        observed = self.run_audit()["bypasses"]["observed"]
        self.assertEqual(observed["routed_but_never_dispatched"]["count"], 1)
        self.assertEqual(observed["routed_but_never_dispatched"]["items"][0]["route"],
                         "codex")

    def test_a_client_with_no_hook_is_named_as_a_total_bypass(self):
        observed = self.run_audit()["bypasses"]["observed"]
        self.assertEqual(observed["clients_without_the_hook"], ["claude", "codex"])
        coverage = self.run_audit()["hook_coverage"]
        self.assertIn("un-gated", coverage["claude"]["consequence"])

    def test_an_installed_hook_with_automatic_routing_off_is_reported(self):
        settings = self.home / ".claude"
        settings.mkdir()
        (settings / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": [
            {"matcher": "Edit", "hooks": [{"type": "command",
             "command": "/x/bin/agent-bridge-gate-hook --client claude "
                        "--config /c --no-automatic-routing"}]}]}}),
            encoding="utf-8")
        coverage = self.run_audit()["hook_coverage"]["claude"]
        self.assertTrue(coverage["installed"])
        self.assertFalse(coverage["automatic_routing"])
        self.assertIn("only when an assistant asks", coverage["consequence"])

    def test_writes_aimed_at_the_gate_are_counted_apart_from_routing(self):
        self.event(permission="deny", code="gate_state_protected")
        self.event(permission="deny", code="routed_elsewhere")
        observed = self.run_audit()["bypasses"]["observed"]
        self.assertEqual(observed["writes_aimed_at_the_gate_itself"]["count"], 1)

    def test_what_cannot_be_counted_is_enumerated_not_omitted(self):
        bypasses = self.run_audit()["bypasses"]
        self.assertTrue(bypasses["not_countable"])
        joined = " ".join(bypasses["not_countable"]).lower()
        for surface in ("desktop", "terminal", "safe-mode", "heuristic"):
            self.assertIn(surface, joined)
        self.assertIn("not none", bypasses["warning"])


class Failures(AuditCase):
    def test_gate_failures_are_separated_from_ordinary_denials(self):
        self.event(permission="deny", code="gate_auto_decision_failed")
        self.event(permission="deny", code="routed_elsewhere")
        document = self.run_audit()
        self.assertEqual(document["failures"]["gate"]["count"], 1)
        self.assertEqual(document["gate_events"]["denials"], 2)

    def test_a_queue_failure_row_carries_the_detail_not_just_the_class(self):
        """A generic TaskError is not diagnostic evidence, so print the detail."""
        queue = self.base / "execution-queue"
        job = queue / "job1"
        job.mkdir(parents=True)
        store.atomic_write_json(str(job / "receipt.json"), {
            "job_id": "job1", "state": "failed", "caller": "claude",
            "provider": "codex", "error": "TaskError",
            "error_detail": "could not start '/Library/.../git': no such file",
            "harness": {"harness_status": "failed", "harness_verdict": "read_failure",
                        "error": "TaskError",
                        "error_detail": "verification_confinement_unavailable"}})
        config = self.base / "orchestration.json"
        config.write_text(json.dumps({"state_root": str(self.state),
                                      "capacity_db": str(self.state / "c.sqlite3"),
                                      "execution_queue_root": str(queue)}),
                          encoding="utf-8")
        document = self.run_audit(config_path=str(config))
        failures = document["failures"]["execution_queue"]["failures"]
        self.assertEqual(len(failures), 1)
        self.assertIn("no such file", failures[0]["error_detail"])
        self.assertEqual(failures[0]["harness_verdict"], "read_failure")
        self.assertIn("confinement", failures[0]["harness_error_detail"])

    def test_an_unreadable_policy_is_reported_with_its_consequence(self):
        Path(autoroute.policy_path(str(self.state))).write_text("{oops",
                                                                encoding="utf-8")
        policy = self.run_audit()["policy"]
        self.assertFalse(policy["readable"])
        self.assertIn("denies every gated call", policy["consequence"])

    def test_a_missing_queue_is_reported_as_missing_not_as_healthy(self):
        config = self.base / "orchestration.json"
        config.write_text(json.dumps({
            "state_root": str(self.state),
            "capacity_db": str(self.state / "c.sqlite3"),
            "execution_queue_root": str(self.base / "never-created")}),
            encoding="utf-8")
        queue = self.run_audit(config_path=str(config))["failures"]["execution_queue"]
        self.assertTrue(queue["configured"])
        self.assertFalse(queue["present"])


class Hygiene(AuditCase):
    def test_reporting_creates_nothing(self):
        """An audit must not bring the thing it measures into existence."""
        before = sorted(os.listdir(self.state))
        self.run_audit()
        self.assertEqual(sorted(os.listdir(self.state)), before)

    def test_it_works_on_a_state_root_that_has_never_been_used(self):
        empty = self.base / "fresh"
        document = audit.report(str(empty), home=str(self.home), since_hours=0.0)
        self.assertEqual(document["eligible"]["count"], 0)
        self.assertEqual(document["routed"]["count"], 0)
        self.assertTrue(document["bypasses"]["not_countable"])

    def test_render_never_crashes_on_any_shape_it_produces(self):
        self.decision()
        self.decision(code="routed_peer_implementation", owner_route="codex",
                      decision="peer")
        self.event(permission="deny", code="gate_error")
        text = audit.render(self.run_audit())
        self.assertIn("Delegation audit", text)
        self.assertIn("routed away", text)
        self.assertIn("bypasses the gate can see", text)

    def test_the_cli_prints_both_shapes(self):
        import subprocess

        config = self.base / "orchestration.json"
        config.write_text(json.dumps({"state_root": str(self.state),
                                      "capacity_db": str(self.state / "c.sqlite3")}),
                          encoding="utf-8")
        self.decision()
        for extra, check in ((["--json"], lambda out: json.loads(out)["eligible"]),
                             ([], lambda out: "Delegation audit" in out)):
            completed = subprocess.run(
                [sys.executable, "-P", "-m", "agent_bridge.orchestration.gate",
                 "audit", "--config", str(config), "--since-hours", "0",
                 "--home", str(self.home), *extra],
                capture_output=True, timeout=120,
                env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(check(completed.stdout.decode("utf-8")))


if __name__ == "__main__":
    unittest.main()
