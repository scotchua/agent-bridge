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

    def test_client_derived_work_is_eligible_only_as_local_mechanical_work(self):
        base = {"client": "claude", "peer": "codex", "classification": "client_derived",
                "allowed_routes": ["claude", "codex", "local"]}
        self.decision(considered={**base, "mechanical_ok": True, "task_type": "mechanical"})
        self.decision(considered={**base, "mechanical_ok": True, "task_type": "implementation"})
        self.decision(considered={**base, "mechanical_ok": "yes", "task_type": "mechanical"})
        self.decision(considered={**base, "allowed_routes": ["claude", "codex"],
                                  "mechanical_ok": True, "task_type": "mechanical"})
        self.assertEqual(self.run_audit()["eligible"]["count"], 1)

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


class CountsThatHaveToAddUp(AuditCase):
    def test_two_identical_decisions_are_both_counted(self):
        """They compare equal as dicts, which is how the partition broke.

        ``retained = [row for row in decisions if row not in routed]`` put
        both identical rows in whichever bucket the first one landed in, so
        routed + retained stopped equalling the total.
        """
        for _ in range(2):
            self.decision(code="routed_peer_implementation", owner_route="codex",
                          decision="peer")
        for _ in range(2):
            self.decision(code="retained_repo_unclassified")
        document = self.run_audit()
        self.assertEqual(document["routed"]["count"], 2)
        self.assertEqual(document["retained"]["count"], 2)
        self.assertEqual(document["automatic_share"]["decisions"], 4)
        self.assertEqual(document["routed"]["count"] + document["retained"]["count"],
                         document["automatic_share"]["decisions"])

    def test_the_local_queue_is_read_from_its_own_store(self):
        """It keeps state in SQLite, not per-job receipt directories."""
        import sqlite3

        root = self.base / "local-queue"
        root.mkdir()
        connection = sqlite3.connect(root / "localq.sqlite3")
        try:
            connection.execute(
                "CREATE TABLE jobs (job_id TEXT, status TEXT, task_type TEXT, "
                "caller TEXT, purpose TEXT, error TEXT, updated_at REAL)")
            connection.executemany(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?)",
                [("j1", "complete", "summarize", "claude", "work", None, 1.0),
                 ("j2", "complete", "extract", "codex", "work", None, 2.0),
                 ("j3", "failed", "log_triage", "claude", "work",
                  "private worker rejected request", 3.0)])
            connection.commit()
        finally:
            connection.close()
        config = self.base / "orchestration.json"
        config.write_text(json.dumps({
            "state_root": str(self.state),
            "capacity_db": str(self.state / "c.sqlite3"),
            "local_queue_root": str(root)}), encoding="utf-8")
        queue = self.run_audit(config_path=str(config))["failures"]["local_queue"]
        self.assertEqual(queue["states"], {"complete": 2, "failed": 1})
        self.assertEqual(len(queue["failures"]), 1)
        self.assertEqual(queue["failures"][0]["error"],
                         "private worker rejected request")

    def test_a_local_queue_directory_with_no_database_yet_says_so(self):
        root = self.base / "empty-local"
        root.mkdir()
        config = self.base / "orchestration.json"
        config.write_text(json.dumps({
            "state_root": str(self.state),
            "capacity_db": str(self.state / "c.sqlite3"),
            "local_queue_root": str(root)}), encoding="utf-8")
        queue = self.run_audit(config_path=str(config))["failures"]["local_queue"]
        self.assertTrue(queue["present"])
        self.assertEqual(queue["states"], {})
        self.assertIn("note", queue)


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


class TheAuditNamesWhoSaidARouteWasAvailable(unittest.TestCase):
    """Capacity provenance is reportable because it is now recorded.

    The removed ``capacity_observe`` tool left no way to tell an operator's
    statement from a model's assertion: every row looked the same. There are
    two writers now and the report names them.
    """

    def test_the_render_names_the_source_of_each_row(self):
        rendered = audit.render({
            "window_hours": 24,
            "policy": {"readable": True, "classified_repositories": 1},
            "automatic_share": {"decisions": 1, "made_automatically": 1,
                                "made_by_an_agent_calling_routing_decide": 0},
            "eligible": {"count": 1}, "routed": {"count": 1, "by_route": {"codex": 1}},
            "retained": {"count": 0},
            "bypasses": {"warning": "w", "observed": {
                "routed_but_never_dispatched": {"count": 0},
                "clients_without_the_hook": [],
                "writes_aimed_at_the_gate_itself": {"count": 0}}},
            "failures": {"gate": {"count": 0, "by_code": {}},
                         "execution_queue": {"present": False},
                         "local_queue": {"present": False}},
            "hook_coverage": {"claude": {"installed": True, "automatic_routing": True},
                              "codex": {"installed": True, "automatic_routing": True},
                              "codex_trust": "trusted"},
            "stages": {"available": True, "capacity": {
                "codex": {"status": "available", "trusted": True,
                          "source": "policy:operator-declared"},
                "local": {"status": "untrusted", "trusted": False,
                          "source": "something-else"}}},
        })
        self.assertIn("policy:operator-declared", rendered)
        self.assertIn("untrusted", rendered)

    def test_an_empty_table_says_what_that_means(self):
        rendered = audit.render({
            "window_hours": 24,
            "policy": {"readable": True, "classified_repositories": 0},
            "automatic_share": {"decisions": 0, "made_automatically": 0,
                                "made_by_an_agent_calling_routing_decide": 0},
            "eligible": {"count": 0}, "routed": {"count": 0, "by_route": {}},
            "retained": {"count": 0},
            "bypasses": {"warning": "w", "observed": {
                "routed_but_never_dispatched": {"count": 0},
                "clients_without_the_hook": [],
                "writes_aimed_at_the_gate_itself": {"count": 0}}},
            "failures": {"gate": {"count": 0, "by_code": {}},
                         "execution_queue": {"present": False},
                         "local_queue": {"present": False}},
            "hook_coverage": {"claude": {}, "codex": {}, "codex_trust": "unknown"},
            "stages": {"available": False},
        })
        self.assertIn("no peer is dispatched to", rendered)


class LocalFirstAccounting(AuditCase):
    """The local_first section (design section 2.6): every gated-shape read
    partitioned into compelled == digested + pending + waived + declined +
    outstanding, the acceptance criterion the design's own build plan names."""

    def repo_path(self):
        return str(self.base / "repo")

    def intent(self, **fields):
        row = {"event": "digest_intent", "path": str(self.base / "repo" / "app.log"),
              "repo": self.repo_path(), "size": 8_500, "mtime_ns": 1,
              "matched_glob": "**/*.log", "classification": "internal_nonclient",
              "client": "claude", "readiness": {"ready": True}, "created_at": self.now,
              "expires_at": self.now + 900, "state": "awaiting_digest",
              "next_call": "work_digest_file"}
        row.update(fields)
        store.append_ledger(str(self.routing / gate.AUDIT_LEDGER), row)

    def submission(self, **fields):
        row = {"event": "digest_submitted", "path": str(self.base / "repo" / "app.log"),
              "repo": self.repo_path(), "size": 8_500, "mtime_ns": 1, "offset": 0,
              "window_bytes": 8_500, "window_sha256": "x", "decode_replacements": 0,
              "task_type": "log_triage", "classification": "internal_nonclient",
              "caller": "codex", "job_id": "j1", "intake_receipt_id": "r1",
              "created_at": self.now}
        row.update(fields)
        store.append_ledger(str(self.routing / gate.AUDIT_LEDGER), row)

    def test_the_five_buckets_add_up_to_compelled(self):
        repo = self.repo_path()
        self.event(code="local_digest_present", client="claude", repos=[repo], bytes_estimate=8_500)
        self.event(code="local_digest_pending", client="codex", repos=[repo],
                  job_id="j2", bytes_estimate=9_000)
        self.event(code="local_first_waived", client="claude", repos=[repo],
                  waiver_reason="calibration_missing", bytes_estimate=7_000)
        self.event(code="local_digest_required", client="codex", repos=[repo],
                  matched_glob="**/*.log", bytes_estimate=8_200)
        self.intent(expires_at=self.now + 900)             # not yet expired: outstanding
        self.event(code="local_digest_required", client="claude", repos=[repo],
                  matched_glob="**/*.log", bytes_estimate=8_300)
        self.intent(expires_at=self.now - 1)                # already expired: declined

        lf = self.run_audit()["local_first"]
        self.assertEqual(lf["compelled"]["count"], 5)
        self.assertEqual(lf["digested"]["count"], 1)
        self.assertEqual(lf["pending"]["count"], 1)
        self.assertEqual(lf["waived"]["count"], 1)
        self.assertEqual(lf["declined"]["count"], 1)
        self.assertEqual(lf["outstanding"]["count"], 1)
        self.assertTrue(lf["adds_up"])
        self.assertEqual(lf["waived"]["by_reason"], {"calibration_missing": 1})
        self.assertEqual(lf["compelled"]["by_client"], {"claude": 3, "codex": 2})
        self.assertEqual(lf["compelled"]["by_glob"], {"**/*.log": 2})

    def test_bytes_split_estimated_upper_bound_from_exact_digested(self):
        self.event(code="local_digest_present", bytes_estimate=1_000)
        self.event(code="local_first_waived", bytes_estimate=2_000, waiver_reason="x")
        # A deny never reaches the cloud, so its bytes must not count here.
        self.event(code="local_digest_pending", bytes_estimate=99_999)
        self.submission(window_bytes=500)
        lf = self.run_audit()["local_first"]
        self.assertEqual(lf["bytes"]["estimated_reaching_cloud_context"], 3_000)
        self.assertEqual(lf["bytes"]["digested_locally_exact"], 500)

    def test_a_malformed_bytes_estimate_does_not_crash_the_report(self):
        """int(event.get("bytes_estimate") or 0) crashed on any non-numeric
        truthy value (the adjacent declined count already guards its own
        ledger field with isinstance; this one did not). A hand-edited or
        otherwise corrupted ledger line should degrade this one number, not
        take the whole audit report down."""
        self.event(code="local_digest_present", bytes_estimate="not-a-number")
        self.event(code="local_first_waived", bytes_estimate=[1, 2], waiver_reason="x")
        self.submission(window_bytes="also-not-a-number")
        lf = self.run_audit()["local_first"]
        self.assertEqual(lf["bytes"]["estimated_reaching_cloud_context"], 0)
        self.assertEqual(lf["bytes"]["digested_locally_exact"], 0)

    def test_read_gate_strength_is_reported(self):
        lf = self.run_audit()["local_first"]
        self.assertEqual(lf["read_gate_strength"], {"claude": "deterministic", "codex": "heuristic"})

    def test_readiness_now_is_reported_unknown_without_a_configured_lane(self):
        """run_audit here passes no config_path, so local_root/worker_executable
        are unavailable; the section must say so rather than crash or guess."""
        lf = self.run_audit()["local_first"]
        self.assertFalse(lf["readiness_now"]["ready"])

    def test_an_empty_window_adds_up_to_zero(self):
        lf = self.run_audit()["local_first"]
        self.assertEqual(lf["compelled"]["count"], 0)
        self.assertTrue(lf["adds_up"])

    def test_render_includes_the_local_first_summary_without_a_mismatch(self):
        self.event(code="local_digest_present", client="claude", repos=[self.repo_path()],
                  bytes_estimate=100)
        rendered = audit.render(self.run_audit())
        self.assertIn("local-first read gate", rendered)
        self.assertIn("compelled 1", rendered)
        self.assertNotIn("MISMATCH", rendered)


class InlineMeasurementAccounting(AuditCase):
    """The inline_measurement section (design section 2.9): a count and a
    byte total from gate.INLINE_LEDGER, never a permission split -- Phase 5
    never denies anything, so there is nothing here shaped like local_first's
    own compelled/digested/pending/waived/declined/outstanding partition."""

    def measurement(self, **fields):
        row = {"at": self.now, "client": "claude", "matched_runner": "pytest", "bytes": 500}
        row.update(fields)
        store.append_ledger(str(self.routing / gate.INLINE_LEDGER), row)

    def test_an_empty_window_is_zero(self):
        im = self.run_audit()["inline_measurement"]
        self.assertEqual(im["count"], 0)
        self.assertEqual(im["bytes_measured"], 0)

    def test_count_and_bytes_and_by_runner_are_correct(self):
        self.measurement(matched_runner="pytest", bytes=500)
        self.measurement(matched_runner="pytest", bytes=300)
        self.measurement(matched_runner="jest", bytes=1_200)
        im = self.run_audit()["inline_measurement"]
        self.assertEqual(im["count"], 3)
        self.assertEqual(im["bytes_measured"], 2_000)
        self.assertEqual(im["by_runner"], {"pytest": 2, "jest": 1})

    def test_a_malformed_bytes_value_degrades_that_one_row_not_the_report(self):
        # The same isinstance-guard precedent local_first's own bytes
        # computation already uses (audit.py, after the crash-safety fix):
        # a hand-edited or otherwise malformed ledger row is counted as 0
        # rather than raising and taking the whole report down with it.
        self.measurement(bytes="not a number")
        self.measurement(bytes=500)
        im = self.run_audit()["inline_measurement"]
        self.assertEqual(im["count"], 2)
        self.assertEqual(im["bytes_measured"], 500)

    def test_a_nan_or_infinite_bytes_value_does_not_crash_the_report(self):
        # Confirmed by adversarial review: isinstance(x, (int, float)) alone
        # is True for NaN and Infinity too, and int(nan)/int(inf) raise --
        # reintroducing the exact crash class this guard exists to prevent.
        self.measurement(bytes=float("nan"))
        self.measurement(bytes=float("inf"))
        self.measurement(bytes=500)
        im = self.run_audit()["inline_measurement"]
        self.assertEqual(im["count"], 3)
        self.assertEqual(im["bytes_measured"], 500)

    def test_an_unparseable_ledger_line_is_not_counted(self):
        # Confirmed by adversarial review: _read_ledger deliberately keeps an
        # unparseable line as its own {"error": ...} row rather than dropping
        # it, so this section must filter to its own rows before counting --
        # otherwise a single corrupt line inflated count by one and added a
        # spurious "None" bucket to by_runner.
        path = self.routing / gate.INLINE_LEDGER
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("not json at all\n")
        self.measurement(bytes=500)
        im = self.run_audit()["inline_measurement"]
        self.assertEqual(im["count"], 1)
        self.assertNotIn("None", im["by_runner"])

    def test_codex_is_named_not_countable(self):
        im = self.run_audit()["inline_measurement"]
        self.assertTrue(any("Codex" in item for item in im["not_countable"]))

    def test_a_window_before_this_phase_existed_is_its_own_bucket(self):
        # Reading the same ledger since_hours already filters by "at";
        # a row entirely outside the window is simply absent, not an error.
        self.measurement(at=self.now - 10_000)
        document = audit.report(str(self.state), home=str(self.home),
                                clock=lambda: self.now + 1, since_hours=0.001)
        self.assertEqual(document["inline_measurement"]["count"], 0)

    def test_render_includes_the_inline_measurement_summary(self):
        self.measurement(matched_runner="pytest", bytes=500)
        rendered = audit.render(self.run_audit())
        self.assertIn("inline output measured: 1 calls, 500 bytes", rendered)


if __name__ == "__main__":
    unittest.main()
