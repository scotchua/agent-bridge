"""The automatic part of the delegation-first gate.

The gate already refused an edit without a routing receipt. What it could not
do was create the decision: an agent had to choose to call ``stage_register``,
``stage_claim`` and ``routing_decide``, and it named its own
``allowed_routes`` when it did, so the route was requested rather than
decided. These tests cover the mechanism that closes that gap, including the
two defects found while building it:

* one receipt per repository answered for work of a different kind, so a
  single decision governed every later call in that repository;
* a stage that reached ``complete`` left the repository permanently
  un-editable, because every later decision tried to claim a closed stage.

They run the hook as a real process wherever the behaviour is the process's
(exit status, the JSON on stdout, state on disk surviving into the next
invocation), because a decision that only holds in-process is not the thing
being claimed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge.capacity_router import (  # noqa: E402
    MAX_FRESHNESS_SECONDS, CapacityObservation, RoutingError, StageRouter)
from agent_bridge.orchestration import autodecide, autoroute, gate  # noqa: E402


def git_repo(path: Path) -> Path:
    """A real git checkout: the gate keys decisions on repository roots."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, timeout=60,
                   capture_output=True)
    (path / "app.py").write_text("x = 1\n", encoding="utf-8")
    (path / "tests").mkdir(exist_ok=True)
    (path / "tests" / "test_app.py").write_text("def test_x():\n    assert True\n",
                                                encoding="utf-8")
    return path


class AutoCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.state = self.base / "state"
        (self.state / "routing").mkdir(parents=True)
        os.chmod(self.state, 0o700)
        self.db = self.state / "capacity.sqlite3"
        self.config = self.base / "orchestration.json"
        self.config.write_text(json.dumps(
            {"state_root": str(self.state), "capacity_db": str(self.db)}),
            encoding="utf-8")
        self.repo = git_repo(self.base / "repo")

    # ---------------------------------------------------------------- helpers

    def write_policy(self, repos: dict, **extra):
        document = {"version": 1, "repos": repos, **extra}
        path = Path(autoroute.policy_path(str(self.state)))
        path.write_text(json.dumps(document), encoding="utf-8")
        os.chmod(path, 0o600)

    def observe(self, route: str, available: bool = True, seconds: float = 3600,
                trusted: bool = True, source: str = "test-operator"):
        router = StageRouter(str(self.db))
        now = time.time()
        router.observe_capacity(CapacityObservation(
            route=route, observed_at=now, fresh_until=now + seconds,
            available=available, source=source), trusted=trusted)

    #: Each client's own editing tool. Codex does not have an "Edit" tool, so
    #: a codex call naming one classifies as "not gated" and is allowed
    #: without ever reaching the receipt. A test that drove codex that way
    #: passed while exercising nothing, and it was the test guarding against
    #: the two clients livelocking each other.
    EDIT_TOOL = {"claude": "Edit", "codex": "apply_patch"}

    def hook(self, client: str, repo: Path, relative: str = "app.py",
             tool: str | None = None, *args, timeout_is_host_limit: bool = True) -> dict:
        tool = tool if tool is not None else self.EDIT_TOOL[client]
        if tool == "apply_patch":
            tool_input = {"input": "*** Begin Patch\n*** Update File: "
                                   f"{relative}\n@@\n-x = 1\n+x = 2\n*** End Patch\n"}
        else:
            tool_input = {"file_path": str(repo / relative)}
        payload = {"hook_event_name": "PreToolUse", "tool_name": tool,
                   "tool_input": tool_input,
                   "cwd": str(repo)}
        return self.run_hook(client, payload, *args, timeout_is_host_limit=timeout_is_host_limit)

    def run_hook(self, client: str, payload: dict, *args,
                 timeout_is_host_limit: bool = True) -> dict:
        """Run the real hook process once. Every hook call in this file goes
        through here, so a slow host is reported the same way everywhere."""
        started = time.monotonic()
        completed = subprocess.run(
            [sys.executable, "-P", "-m", "agent_bridge.orchestration.gate",
             "--client", client, "--config", str(self.config), *args],
            input=json.dumps(payload).encode("utf-8"), capture_output=True,
            timeout=120, env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
        elapsed = time.monotonic() - started
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        # The hook's watchdog denies once a judgment has run for
        # HOOK_WATCHDOG_SECONDS, whatever the reason; that is the gate doing
        # its job on a slow host, not the behavior these tests are about. A
        # loaded Windows runner hit it (run 36688606676, attempt 1: a 341 s
        # suite, five failures in one class, the later ones cascading from a
        # decision the watchdog cut short). Report it as the host's limit, the
        # way requires_confinement reports a host that cannot confine. The
        # watchdog itself is tested directly, in its own process, below, and
        # a test about the gate's speed passes timeout_is_host_limit=False so
        # a slow gate still fails it.
        reason = result.get("hookSpecificOutput", {}).get("permissionDecisionReason", "")
        if timeout_is_host_limit and reason.endswith("[gate_timeout]"):
            self.skipTest(f"this host ran one {client} hook for {elapsed:.1f} s, past the "
                          f"gate's {gate.HOOK_WATCHDOG_SECONDS:.0f} s watchdog")
        return result

    def assertAllowed(self, result):
        self.assertEqual(result, {}, result)

    def assertDenied(self, result, code):
        self.assertIn("hookSpecificOutput", result)
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertTrue(reason.endswith(f"[{code}]"), reason)
        return reason

    def receipt_for(self, repo: Path) -> dict:
        return gate.read_receipt(str(self.state), str(repo))


class DecisionIsCreatedWithoutBeingAsked(AutoCase):
    def test_an_unclassified_repository_is_retained_and_recorded(self):
        """The decision exists before the edit, with no agent and no user."""
        self.assertIsNone(self.receipt_for(self.repo))
        self.assertAllowed(self.hook("claude", self.repo))
        receipt = self.receipt_for(self.repo)
        self.assertIsNotNone(receipt)
        self.assertTrue(receipt["automatic"])
        self.assertEqual(receipt["code"], "retained_repo_unclassified")
        self.assertEqual(receipt["owner_route"], "claude")
        self.assertEqual(receipt["decision"], "self")
        self.assertTrue(receipt["reason"])

    def test_the_receipt_records_what_the_decision_considered(self):
        self.hook("claude", self.repo)
        considered = self.receipt_for(self.repo)["considered"]
        for key in ("client", "peer", "classification", "allowed_routes",
                    "fresh_routes", "load_per_core", "task_type"):
            self.assertIn(key, considered)

    def test_a_classified_repository_routes_to_the_peer_and_refuses_the_edit(self):
        self.write_policy({str(self.repo): {
            "classification": "internal_nonclient",
            "allowed_routes": ["claude", "codex"]}}, prefer=["codex", "claude", "local"])
        self.observe("codex")
        reason = self.assertDenied(self.hook("claude", self.repo), "routed_elsewhere")
        self.assertIn("routed to codex", reason)
        self.assertIn("execution_dispatch", reason)
        self.assertEqual(self.receipt_for(self.repo)["owner_route"], "codex")

    def test_the_route_that_owns_the_stage_may_edit_it(self):
        """The two-way half: the same repository, the other client."""
        self.write_policy({str(self.repo): {
            "classification": "internal_nonclient",
            "allowed_routes": ["claude", "codex"]}}, prefer=["codex", "claude", "local"])
        self.observe("codex")
        self.assertDenied(self.hook("claude", self.repo), "routed_elsewhere")
        self.assertAllowed(self.hook("codex", self.repo))

    def test_no_user_sentence_and_no_agent_call_was_needed(self):
        """Nothing wrote a receipt but the hook itself.

        The audit ledger is the evidence: the only routing_decided line comes
        from the automatic path, marked as such.
        """
        self.write_policy({str(self.repo): {
            "classification": "public", "allowed_routes": ["claude", "codex"]}},
            prefer=["codex", "claude", "local"])
        self.observe("codex")
        self.hook("claude", self.repo)
        lines = [json.loads(line) for line in
                 (self.state / "routing" / "audit.jsonl").read_text(
                     encoding="utf-8").splitlines()]
        decided = [line for line in lines if line.get("event") == "routing_decided"]
        self.assertEqual(len(decided), 1)
        self.assertTrue(decided[0]["automatic"])


class PrivacyIsCheckedBeforeEverythingElse(AutoCase):
    def test_client_derived_material_is_never_dispatched(self):
        self.write_policy({str(self.repo): {
            "classification": "client_derived",
            "allowed_routes": ["claude", "codex", "local"], "mechanical_ok": True}})
        for route in ("codex", "local"):
            self.observe(route)
        self.assertAllowed(self.hook("claude", self.repo))
        receipt = self.receipt_for(self.repo)
        self.assertEqual(receipt["code"], "retained_classification_ineligible")
        self.assertEqual(receipt["owner_route"], "claude")
        self.assertIsNone(autodecide.read_intent(str(self.state), str(self.repo)))

    def test_a_local_only_repository_leaves_each_assistant_its_own_edits(self):
        # The wider policy lists new repositories with allowed_routes
        # ["local"]. An assistant's own edit there is retained with it, with
        # the other assistant fresh, so it is never handed off. (A second
        # assistant then meets the holder's receipt, exactly as it does in an
        # unclassified repository today; that is not changed here.)
        for classification in ("internal_nonclient", "client_derived"):
            with self.subTest(classification=classification):
                self.write_policy({str(self.repo): {
                    "classification": classification,
                    "allowed_routes": ["local"], "mechanical_ok": True}})
                for route in ("codex", "claude", "local"):
                    self.observe(route)
                self.assertAllowed(self.hook("claude", self.repo))
                self.assertEqual(self.receipt_for(self.repo)["owner_route"], "claude")

    def test_capacity_cannot_override_privacy(self):
        """Every route fresh and available still does not move the work."""
        self.write_policy({str(self.repo): {"classification": "client_derived",
                                            "allowed_routes": ["codex"]}})
        self.observe("codex", seconds=MAX_FRESHNESS_SECONDS)
        self.assertAllowed(self.hook("claude", self.repo))
        self.assertEqual(self.receipt_for(self.repo)["code"],
                         "retained_classification_ineligible")

    def test_an_unreadable_policy_denies_rather_than_permitting(self):
        Path(autoroute.policy_path(str(self.state))).write_text(
            "{not json at all", encoding="utf-8")
        reason = self.assertDenied(self.hook("claude", self.repo),
                                   "gate_auto_decision_failed")
        self.assertIn("policy_unreadable", reason)

    def test_a_policy_with_the_wrong_version_is_refused(self):
        path = Path(autoroute.policy_path(str(self.state)))
        path.write_text(json.dumps({"version": 99, "repos": {}}), encoding="utf-8")
        self.assertDenied(self.hook("claude", self.repo), "gate_auto_decision_failed")


class CapacityEvidenceIsFirstHandOnly(AutoCase):
    def test_the_hook_observes_its_own_route_and_never_the_peer(self):
        self.hook("claude", self.repo)
        capacity = StageRouter(str(self.db)).report()["capacity"]
        self.assertIn("claude", capacity)
        self.assertEqual(capacity["claude"]["source"],
                         autodecide.CLIENT_PRESENCE_SOURCE)
        self.assertNotIn("codex", capacity)
        self.assertNotIn("local", capacity)

    def test_a_client_turned_away_by_a_receipt_is_still_observed(self):
        # Measured live 2026-09-24: presence was written only when the hook
        # made a decision, and a valid receipt naming the other client means
        # no decision is made. So a client that kept arriving at a repository
        # the other held was denied without ever being observed, went stale,
        # and the next decision anywhere had only the holder to choose from.
        self.write_policy({str(self.repo): {
            "classification": "internal_nonclient",
            "allowed_routes": ["claude", "codex"]}})
        self.assertAllowed(self.hook("claude", self.repo))
        self.assertEqual(self.receipt_for(self.repo)["owner_route"], "claude")

        self.assertDenied(self.hook("codex", self.repo), "routed_elsewhere")
        capacity = StageRouter(str(self.db)).report()["capacity"]
        self.assertEqual(capacity["codex"]["status"], "available")
        self.assertEqual(capacity["codex"]["source"], autodecide.CLIENT_PRESENCE_SOURCE)
        self.assertEqual(self.receipt_for(self.repo)["capacity_fingerprint"],
                         "claude,codex",
                         "the arrival changed eligible capacity, so the receipt is re-made")

    def test_a_client_is_observed_even_outside_any_repository(self):
        outside = self.base / "not-a-repo"
        outside.mkdir()
        StageRouter(str(self.db))       # the router exists; presence never creates it
        self.hook("codex", outside)
        capacity = StageRouter(str(self.db)).report()["capacity"]
        self.assertEqual(capacity["codex"]["status"], "available")
        self.assertNotIn("claude", capacity)

    def test_a_peer_without_a_fresh_observation_keeps_the_work_here(self):
        self.write_policy({str(self.repo): {
            "classification": "internal_nonclient",
            "allowed_routes": ["claude", "codex"]}}, prefer=["codex", "claude", "local"])
        # No observation for codex at all.
        self.assertAllowed(self.hook("claude", self.repo))
        self.assertEqual(self.receipt_for(self.repo)["code"],
                         "retained_no_fresh_capacity")

    def test_a_stale_observation_does_not_make_a_route_eligible(self):
        self.write_policy({str(self.repo): {
            "classification": "internal_nonclient",
            "allowed_routes": ["claude", "codex"]}}, prefer=["codex", "claude", "local"])
        router = StageRouter(str(self.db))
        now = time.time()
        router.observe_capacity(CapacityObservation(
            route="codex", observed_at=now - 7200, fresh_until=now - 3600,
            available=True, source="test-operator"), trusted=True)
        self.assertAllowed(self.hook("claude", self.repo))
        self.assertEqual(self.receipt_for(self.repo)["code"],
                         "retained_no_fresh_capacity")


class DispatchIntent(AutoCase):
    def setUp(self):
        super().setUp()
        self.write_policy({str(self.repo): {
            "classification": "internal_nonclient",
            "allowed_routes": ["claude", "codex"]}}, prefer=["codex", "claude", "local"])
        self.observe("codex")

    def test_routing_away_writes_a_durable_intent(self):
        self.assertDenied(self.hook("claude", self.repo), "routed_elsewhere")
        intent = autodecide.read_intent(str(self.state), str(self.repo))
        self.assertEqual(intent["route"], "codex")
        self.assertEqual(intent["state"], "awaiting_brief")
        self.assertEqual(intent["next_call"], "execution_dispatch")
        self.assertEqual(intent["code"], "routed_peer_implementation")

    def test_the_intent_carries_the_binding_the_dispatch_call_needs(self):
        """So the assistant makes one call, not three."""
        self.assertDenied(self.hook("claude", self.repo), "routed_elsewhere")
        intent = autodecide.read_intent(str(self.state), str(self.repo))
        current = StageRouter(str(self.db)).get(intent["item_id"], intent["stage"])
        self.assertEqual(current["state"], "owned")
        self.assertEqual(current["owner_id"], intent["owner_id"])
        self.assertEqual(current["owner_route"], "codex")
        self.assertEqual(current["revision"], intent["stage_revision"])

    def test_the_intent_is_retired_once_it_is_met(self):
        self.assertDenied(self.hook("claude", self.repo), "routed_elsewhere")
        self.assertTrue(autodecide.clear_intent(str(self.state), str(self.repo)))
        self.assertIsNone(autodecide.read_intent(str(self.state), str(self.repo)))
        self.assertFalse(autodecide.clear_intent(str(self.state), str(self.repo)))

    def test_retiring_an_intent_is_recorded(self):
        self.assertDenied(self.hook("claude", self.repo), "routed_elsewhere")
        autodecide.clear_intent(str(self.state), str(self.repo))
        events = [json.loads(line) for line in
                  (self.state / "routing" / "audit.jsonl").read_text(
                      encoding="utf-8").splitlines()]
        self.assertTrue(any(e.get("event") == "dispatch_intent_met" for e in events))


class DefectsFoundWhileBuildingThis(AutoCase):
    """All were live failures, not hypotheticals. See the module docstring."""

    def test_a_completed_stage_does_not_brick_the_repository(self):
        self.assertAllowed(self.hook("claude", self.repo))
        first = self.receipt_for(self.repo)
        router = StageRouter(str(self.db))
        current = router.get(first["item_id"], first["stage"])
        router.complete(first["item_id"], first["stage"],
                        owner_id=current["owner_id"],
                        expected_revision=current["revision"])
        # Before the fix this denied with stage_terminal for ever after.
        self.assertAllowed(self.hook("claude", self.repo))
        second = self.receipt_for(self.repo)
        self.assertNotEqual(second["stage"], first["stage"])
        self.assertEqual(second["stage"], "implementation#2")

    def test_a_manual_receipt_outliving_its_stage_does_not_brick_the_repository(self):
        """The same brick one layer down, and the one nothing could clear.

        A hand-made receipt is deliberately not re-decided when policy,
        capacity or task type change. That left the terminal-stage case with
        no way out at all: editing needs a receipt, ``routing_decide`` needs
        an owned stage, ``stage_claim`` needs a route with fresh capacity, and
        the only writer of this client's own freshness is the auto-decide
        branch that a surviving receipt skips. The receipt prevented the write
        that would have replaced it. Observed live with three unrelated stages
        stuck at once, and unrecoverable without editing the gate, which the
        gate itself was denying.
        """
        self.assertAllowed(self.hook("claude", self.repo))
        first = self.receipt_for(self.repo)
        router = StageRouter(str(self.db))
        current = router.get(first["item_id"], first["stage"])
        router.complete(first["item_id"], first["stage"],
                        owner_id=current["owner_id"],
                        expected_revision=current["revision"])
        from agent_bridge import store
        store.atomic_write_json(gate.receipt_path(str(self.state), str(self.repo)),
                                {**first, "automatic": False})
        self.assertTrue(gate.manual_receipt_overtaken(
            {**first, "automatic": False}, time.time(), str(self.db)))
        self.assertAllowed(self.hook("claude", self.repo))
        self.assertNotEqual(self.receipt_for(self.repo)["stage"], first["stage"])

    def test_a_manual_receipt_survives_an_ordinary_change(self):
        """Only a gone stage retires a manual receipt. Nothing else.

        The operator chose that route by hand, so re-deciding it whenever
        policy or capacity moved would quietly overrule them. This is the
        guard against fixing the deadlock by widening the exception until the
        distinction between the two kinds of receipt stops meaning anything.
        """
        self.assertAllowed(self.hook("claude", self.repo))
        first = self.receipt_for(self.repo)
        from agent_bridge import store
        manual = {**first, "automatic": False,
                  "policy_fingerprint": "no-longer-current",
                  "capacity_fingerprint": "no-longer-current"}
        store.atomic_write_json(
            gate.receipt_path(str(self.state), str(self.repo)), manual)
        self.assertFalse(gate.manual_receipt_overtaken(
            manual, time.time(), str(self.db)))
        # The same receipt marked automatic would be re-decided on either
        # fingerprint, which is what makes this a real distinction.
        self.assertTrue(gate.automatic_receipt_overtaken(
            {**manual, "automatic": True}, time.time(), str(self.db),
            "implementation", "some-other-policy"))
        self.assertAllowed(self.hook("claude", self.repo))
        self.assertEqual(self.receipt_for(self.repo)["stage"], first["stage"])

    def test_generations_are_bounded_rather_than_searched_for_ever(self):
        self.assertLessEqual(autodecide.MAX_STAGE_GENERATIONS, 10_000)
        router = StageRouter(str(self.db))
        item = autodecide.item_id_for(str(self.repo))
        self.assertEqual(autodecide.stage_name(router, item, "implementation"),
                         "implementation")

    def test_a_receipt_for_one_kind_of_work_does_not_answer_for_another(self):
        """The automatic receipt is re-decided when the task type differs."""
        self.assertAllowed(self.hook("claude", self.repo))
        receipt = self.receipt_for(self.repo)
        self.assertEqual(receipt["stage"], "implementation")
        # Rewrite the stage to a different task type, as a decision for other
        # work would have. The next call must re-decide rather than reuse it.
        from agent_bridge import store
        store.atomic_write_json(gate.receipt_path(str(self.state), str(self.repo)),
                                {**receipt, "stage": "review"})
        self.assertAllowed(self.hook("claude", self.repo))
        self.assertEqual(self.receipt_for(self.repo)["stage"], "implementation")

    def test_an_expired_automatic_receipt_is_re_decided_not_refused(self):
        self.assertAllowed(self.hook("claude", self.repo))
        from agent_bridge import store
        receipt = self.receipt_for(self.repo)
        store.atomic_write_json(gate.receipt_path(str(self.state), str(self.repo)),
                                {**receipt, "valid_until": time.time() - 1})
        # Under the strict posture this is routing_receipt_expired. Under
        # automatic routing an expiry is not a question for the agent.
        self.assertDenied(self.hook("claude", self.repo, "app.py", "Edit",
                                    "--no-automatic-routing"),
                          "routing_receipt_expired")
        self.assertAllowed(self.hook("claude", self.repo))
        self.assertGreater(self.receipt_for(self.repo)["valid_until"], time.time())

    def test_an_unreadable_router_stays_a_deny_and_does_not_re_decide(self):
        """Overtaken is ordinary. An unreadable router is not."""
        self.assertAllowed(self.hook("claude", self.repo))
        receipt = self.receipt_for(self.repo)
        self.assertFalse(gate.automatic_receipt_overtaken(
            receipt, time.time(), str(self.base / "no-such.sqlite3"),
            "implementation"))
        self.assertEqual(gate.stage_binding(str(self.base / "no-such.sqlite3"),
                                            receipt, time.time()),
                         "stage_db_unavailable")

    def test_a_file_edit_is_never_routed_to_the_local_worker(self):
        """The local worker does not edit files, so such an intent is unmeetable."""
        self.write_policy({str(self.repo): {
            "classification": "synthetic", "allowed_routes": ["claude", "local"],
            "mechanical_ok": True}}, prefer=["local", "claude", "codex"])
        self.observe("local")
        for relative in ("tests/test_app.py", "app.py"):
            self.assertAllowed(self.hook("claude", self.repo, relative))
            self.assertEqual(self.receipt_for(self.repo)["owner_route"], "claude")
        self.assertEqual(gate.infer_task_type(["tests/test_app.py"]), "implementation")


class AFailedDecisionSaysLittleAndDeniesAnyway(AutoCase):
    """``errors.py``'s rule reaches the gate too: no unvetted text escapes."""

    def judge_with(self, decider):
        return gate.judge("claude", "Edit", {"file_path": str(self.repo / "app.py")},
                          str(self.repo), state_root=str(self.state),
                          capacity_db=str(self.db), decide=decider)

    def test_an_unexpected_exception_contributes_its_class_and_nothing_else(self):
        secret = "SENTINEL_TOKEN_sk_live_do_not_leak"

        class Surprising(RuntimeError):
            pass

        def decider(repo, task_type):
            raise Surprising(secret)

        decision = self.judge_with(decider)
        self.assertEqual(decision.permission, "deny")
        self.assertEqual(decision.code, "gate_auto_decision_failed")
        self.assertIn("Surprising", decision.reason)
        self.assertNotIn(secret, decision.reason)

    def test_our_own_reason_code_is_repeated_because_we_wrote_it(self):
        def decider(repo, task_type):
            raise autodecide.AutoDecisionError("policy_unreadable:ValueError")

        decision = self.judge_with(decider)
        self.assertEqual(decision.code, "gate_auto_decision_failed")
        self.assertIn("policy_unreadable", decision.reason)

    def test_the_deny_stands_even_when_the_decider_returns_nothing_useful(self):
        """A decider that writes no receipt must not become an allow."""
        decision = self.judge_with(lambda repo, task_type: None)
        self.assertEqual(decision.permission, "deny")
        self.assertEqual(decision.code, "no_routing_receipt")


class TheOperatorsPolicyTakesEffectAtOnce(AutoCase):
    """Editing the policy must not wait for a receipt to age out.

    Found by walking the documented workflow: classifying a repository
    changed nothing, because the retained receipt was still valid, still for
    the same task type, and its stage was still owned. It would have taken
    effect up to four hours later, which makes the operator's own document
    look inert. Every automatic receipt now records the fingerprint of the
    policy it was decided under.
    """

    def classify(self, **entry):
        self.write_policy({str(self.repo): entry})

    def consistent(self, receipt):
        """A routed receipt names another route; a retained one names the caller."""
        if receipt["code"].startswith("routed_"):
            return receipt["owner_route"] != receipt["caller"]
        return receipt["owner_route"] == receipt["caller"]

    def test_a_receipt_records_the_policy_it_was_decided_under(self):
        self.hook("claude", self.repo)
        self.assertEqual(self.receipt_for(self.repo)["policy_fingerprint"],
                         autoroute.NO_POLICY)
        self.classify(classification="public", allowed_routes=["claude"])
        self.hook("claude", self.repo)
        self.assertEqual(self.receipt_for(self.repo)["policy_fingerprint"],
                         autoroute.policy_fingerprint(str(self.state)))

    def test_classifying_a_repository_is_acted_on_by_the_very_next_call(self):
        self.observe("codex")
        self.assertAllowed(self.hook("claude", self.repo))
        self.assertEqual(self.receipt_for(self.repo)["code"],
                         "retained_repo_unclassified")
        self.classify(classification="internal_nonclient",
                      allowed_routes=["claude", "codex"])
        self.assertDenied(self.hook("claude", self.repo), "routed_elsewhere")
        self.assertEqual(self.receipt_for(self.repo)["owner_route"], "codex")

    def test_withdrawing_a_route_is_acted_on_by_the_very_next_call(self):
        self.observe("codex")
        self.classify(classification="internal_nonclient",
                      allowed_routes=["claude", "codex"])
        self.assertDenied(self.hook("claude", self.repo), "routed_elsewhere")
        self.classify(classification="internal_nonclient", allowed_routes=["claude"])
        self.assertAllowed(self.hook("claude", self.repo))
        self.assertEqual(self.receipt_for(self.repo)["owner_route"], "claude")

    def test_a_receipt_never_names_a_route_its_decision_did_not_choose(self):
        """The bug the fingerprint exposed, and the worse one.

        The router never reassigns an owned stage. So once the route changed,
        the old stage was still owned on the old route, the receipt was
        written naming *that* route while the decision said the new one, and
        the gate allowed the edit. A decision to delegate had silently become
        a decision to retain.
        """
        self.observe("codex")
        states = [
            {"classification": "internal_nonclient", "allowed_routes": ["claude", "codex"]},
            {"classification": "internal_nonclient", "allowed_routes": ["claude"]},
            {"classification": "internal_nonclient", "allowed_routes": ["claude", "codex"]},
            {"classification": "client_derived", "allowed_routes": ["claude", "codex"]},
        ]
        seen = []
        self.hook("claude", self.repo)
        seen.append(self.receipt_for(self.repo))
        for entry in states:
            self.classify(**entry)
            self.hook("claude", self.repo)
            seen.append(self.receipt_for(self.repo))
        for receipt in seen:
            self.assertTrue(self.consistent(receipt), receipt)
        # And each change really did produce a new decision, not a reused one.
        self.assertEqual(len({receipt["stage"] for receipt in seen}), len(seen))

    def test_the_stage_a_superseded_decision_held_is_not_left_leased(self):
        self.observe("codex")
        self.assertAllowed(self.hook("claude", self.repo))
        first = self.receipt_for(self.repo)
        self.classify(classification="internal_nonclient",
                      allowed_routes=["claude", "codex"])
        self.assertDenied(self.hook("claude", self.repo), "routed_elsewhere")
        # The claude-owned stage it replaced was completed on the way past.
        previous = StageRouter(str(self.db)).get(first["item_id"], first["stage"])
        self.assertEqual(previous["state"], "complete")

    def test_an_unreadable_policy_invalidates_every_automatic_receipt(self):
        self.classify(classification="public", allowed_routes=["claude"])
        self.assertAllowed(self.hook("claude", self.repo))
        Path(autoroute.policy_path(str(self.state))).write_text("{broken",
                                                                encoding="utf-8")
        # Not an allow on the strength of the old receipt: the fingerprint no
        # longer matches, so it re-decides, and the decision refuses.
        self.assertDenied(self.hook("claude", self.repo),
                          "gate_auto_decision_failed")

    def test_the_fingerprint_never_raises_on_a_broken_policy(self):
        Path(autoroute.policy_path(str(self.state))).write_text("{broken",
                                                                encoding="utf-8")
        self.assertIsInstance(autoroute.policy_fingerprint(str(self.state)), str)
        missing = autoroute.policy_fingerprint(str(self.base / "nowhere"))
        self.assertEqual(missing, autoroute.NO_POLICY)


class TheRoutingEscapeAnAdversarialReviewFound(AutoCase):
    """A nested .git could shadow an outer routed decision.

    Found by an adversarial review of commit 58e4d8c and confirmed still
    reachable four commits later, by a different mechanism than the one
    reported. ``gate.repo_key`` returns the NEAREST ancestor holding .git, so
    a client denied at a routed repository root could edit under a nested
    repository whose own receipt said retained_repo_unclassified. It was
    reachable end to end with gate-allowed commands only, because ``git
    init`` was not in the shell write verbs, and reachable with NO command at
    all when a submodule or a linked worktree already existed.

    Both halves are tested because either alone leaves a hole: judging the
    whole chain without gating creation still lets a client make nested repos
    for other reasons, and gating creation without judging the chain leaves
    every pre-existing submodule open.
    """

    def setUp(self):
        super().setUp()
        self.write_policy({str(self.repo): {
            "classification": "internal_nonclient",
            "allowed_routes": ["claude", "codex"]}})
        self.observe("codex")
        (self.repo / "src").mkdir(exist_ok=True)
        (self.repo / "src" / "existing.py").write_text("a = 1\n", encoding="utf-8")

    def bash(self, client, command, cwd=None):
        payload = {"tool_name": "Bash", "tool_input": {"command": command},
                   "cwd": str(cwd or self.repo)}
        return self.run_hook(client, payload)

    def nest(self):
        """A nested repository, as a submodule or a stray git init leaves one."""
        subprocess.run(["git", "init", "-q", str(self.repo / "src")],
                       check=True, timeout=60, capture_output=True)

    def test_the_root_is_routed_away_to_begin_with(self):
        self.assertDenied(self.hook("claude", self.repo, "app.py"), "routed_elsewhere")

    def test_creating_a_nested_repository_is_itself_a_gated_write(self):
        self.assertDenied(self.bash("claude", f"git init -q {self.repo / 'src'}"),
                          "routed_elsewhere")

    def test_an_edit_under_a_pre_existing_nested_repository_is_still_denied(self):
        """The case needing no command at all: a submodule already there."""
        self.nest()
        self.assertDenied(self.hook("claude", self.repo, "src/existing.py"),
                          "routed_elsewhere")

    def test_a_shell_write_under_a_nested_repository_is_still_denied(self):
        self.nest()
        self.assertDenied(
            self.bash("claude", f"sed -i s/a/z/ {self.repo / 'src' / 'existing.py'}",
                      cwd=self.repo / "src"),
            "routed_elsewhere")

    def test_the_route_the_decision_chose_may_still_edit_the_nested_repository(self):
        """The fix must not over-block: codex owns this work."""
        self.nest()
        self.assertAllowed(self.hook("codex", self.repo, "src/existing.py"))

    def test_judgment_sees_every_enclosing_repository(self):
        self.nest()
        seen = gate.enclosing_repos(str(self.repo / "src" / "existing.py"))
        self.assertEqual([os.path.realpath(str(self.repo / "src")),
                          os.path.realpath(str(self.repo))], seen)

    def test_repository_creation_reads_as_a_write_and_reads_still_read(self):
        for command in ("git init -q vendor", "git clone /r /out",
                        "git -C /r init sub", "git submodule add x y"):
            self.assertTrue(gate.shell_writes(command), command)
        for command in ("git init-db-not-a-verb", "git status", "git log --oneline",
                        "git diff --check"):
            self.assertFalse(gate.shell_writes(command), command)

    def test_an_unclassified_parent_does_not_block_its_own_nested_repository(self):
        """Judging the chain must not break the ordinary nested-repo case."""
        other = git_repo(self.base / "plain")
        (other / "lib").mkdir()
        (other / "lib" / "f.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(other / "lib")], check=True,
                       timeout=60, capture_output=True)
        self.assertAllowed(self.hook("claude", other, "lib/f.py"))


class RestartAndReuse(AutoCase):
    def test_the_decision_survives_into_the_next_hook_process(self):
        """Each tool call is a new process; the decision must be on disk."""
        self.assertAllowed(self.hook("claude", self.repo))
        first = self.receipt_for(self.repo)
        self.assertAllowed(self.hook("claude", self.repo))
        second = self.receipt_for(self.repo)
        self.assertEqual(first["item_id"], second["item_id"])
        self.assertEqual(first["stage"], second["stage"])
        self.assertEqual(first["owner_id"], second["owner_id"])

    def test_one_decision_per_repository_not_one_per_tool_call(self):
        for _ in range(4):
            self.assertAllowed(self.hook("claude", self.repo))
        lines = [json.loads(line) for line in
                 (self.state / "routing" / "audit.jsonl").read_text(
                     encoding="utf-8").splitlines()]
        stages = {line["stage"] for line in lines
                  if line.get("event") == "routing_decided"}
        self.assertEqual(stages, {"implementation"})

    def test_a_second_repository_gets_its_own_decision(self):
        other = git_repo(self.base / "other")
        self.assertAllowed(self.hook("claude", self.repo))
        self.assertAllowed(self.hook("claude", other))
        self.assertNotEqual(self.receipt_for(self.repo)["item_id"],
                            self.receipt_for(other)["item_id"])


class TheStrictPostureIsStillAvailable(AutoCase):
    def test_no_automatic_routing_refuses_instead_of_deciding(self):
        self.assertDenied(self.hook("claude", self.repo, "app.py", "Edit",
                                    "--no-automatic-routing"),
                          "no_routing_receipt")
        self.assertIsNone(self.receipt_for(self.repo))

    def test_the_protected_state_rule_still_applies_under_automatic_routing(self):
        """An agent cannot write itself a receipt through a covered tool."""
        payload_target = self.state / "routing" / "anything.json"
        result = self.hook("claude", self.repo, str(payload_target))
        self.assertDenied(result, "gate_state_protected")


class TheTieBreakIsSymmetricUnlessAskedOtherwise(unittest.TestCase):
    """With both providers eligible, no preference must not favour one.

    The default used to be ``prefer = ROUTES``, which ranks claude above
    codex, so a Claude client kept every repository classified for both while
    a Codex client handed every one of them over. That reads harmlessly in
    the source and is a bias nobody chose.
    """

    def decide(self, client, **policy_kwargs):
        policy = autoroute.Policy(
            repos={"/r": autoroute.RepoPolicy("public", ("claude", "codex"))},
            **policy_kwargs)
        return autoroute.decide(
            autoroute.Signal(client=client, repo="/r"), policy,
            fresh_routes=frozenset({"claude", "codex"}),
            load=autoroute.Load(0.1, True))

    def test_no_preference_routes_both_clients_to_their_peer(self):
        self.assertEqual(autoroute.Policy().prefer, ())
        self.assertEqual(self.decide("claude").route, "codex")
        self.assertEqual(self.decide("codex").route, "claude")

    def test_a_named_preference_is_honoured_in_one_direction_only(self):
        self.assertEqual(self.decide("claude", prefer=("claude",)).route,
                         autoroute.RETAIN)
        self.assertEqual(self.decide("codex", prefer=("claude",)).route, "claude")

    def test_an_absent_prefer_key_parses_as_no_preference(self):
        policy = autoroute.parse_policy({"version": 1, "repos": {}})
        self.assertEqual(policy.prefer, ())

    def test_review_independence_overrides_any_preference(self):
        policy = autoroute.Policy(
            repos={"/r": autoroute.RepoPolicy("public", ("claude", "codex"))},
            prefer=("claude",))
        decision = autoroute.decide(
            autoroute.Signal(client="claude", repo="/r", task_type="review",
                             is_review=True, author_route="claude"),
            policy, fresh_routes=frozenset({"claude", "codex"}),
            load=autoroute.Load(0.1, True))
        self.assertEqual(decision.route, "codex")
        self.assertEqual(decision.code, "routed_peer_review_independence")


class MeasuredCpuIdleRatioForAutomaticRouting(unittest.TestCase):
    """The plumbing ``ensure_decision()`` uses to source the same idle
    rescue ``readiness()`` applies, read from the same heartbeat file."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.local_queue_root = str(Path(self.temp.name))

    def write_heartbeat(self, resource):
        path = Path(autodecide.localfirst.heartbeat_path(self.local_queue_root))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"updated_at": time.time(),
                                    "queue": {"resource": resource}}),
                        encoding="utf-8")

    def test_no_local_queue_root_reads_nothing(self):
        self.assertIsNone(autodecide._measured_cpu_idle_ratio(None))

    def test_no_heartbeat_file_reads_nothing(self):
        self.assertIsNone(autodecide._measured_cpu_idle_ratio(self.local_queue_root))

    def test_a_real_reading_is_returned(self):
        self.write_heartbeat({"cpu_idle_ratio": 0.42})
        self.assertEqual(
            autodecide._measured_cpu_idle_ratio(self.local_queue_root), 0.42)

    def test_a_malformed_heartbeat_reads_nothing_rather_than_raising(self):
        path = Path(autodecide.localfirst.heartbeat_path(self.local_queue_root))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json", encoding="utf-8")
        self.assertIsNone(autodecide._measured_cpu_idle_ratio(self.local_queue_root))


class ClientDerivedWorkGoesOnlyToTheLocalModel(unittest.TestCase):
    """Scott, 2026-09-24: the on-device model is the safest processor of
    client data. Mechanical client-derived work goes local; nothing
    client-derived ever goes to a peer."""

    def decide(self, routes, task_type="mechanical", fresh=("local", "codex")):
        policy = autoroute.Policy(repos={"/r": autoroute.RepoPolicy(
            "client_derived", tuple(routes), mechanical_ok=True)})
        return autoroute.decide(
            autoroute.Signal(client="claude", repo="/r", task_type=task_type),
            policy, fresh_routes=frozenset(fresh), load=autoroute.Load(0.1, True))

    def test_mechanical_client_derived_work_is_routed_local(self):
        decision = self.decide(("claude", "codex", "local"))
        self.assertEqual(decision.route, "local")
        self.assertEqual(decision.code, "routed_local_mechanical")

    def test_non_mechanical_client_derived_work_never_reaches_a_peer(self):
        decision = self.decide(("claude", "codex", "local"), task_type="implementation")
        self.assertEqual(decision.route, autoroute.RETAIN)
        self.assertEqual(decision.code, "retained_classification_ineligible")

    def test_client_derived_work_without_the_local_route_stays_put(self):
        decision = self.decide(("claude", "codex"))
        self.assertEqual(decision.route, autoroute.RETAIN)
        self.assertEqual(decision.code, "retained_classification_ineligible")

    def test_a_policy_that_excludes_client_derived_locally_never_falls_to_a_peer(self):
        policy = autoroute.Policy(
            repos={"/r": autoroute.RepoPolicy("client_derived", ("claude", "codex", "local"),
                                              mechanical_ok=True)},
            local_classifications=frozenset({"synthetic", "public", "internal_nonclient"}),
            peer_classifications=frozenset({"synthetic", "public", "internal_nonclient",
                                            "client_derived"}))
        for task_type in ("mechanical", "implementation", "review"):
            with self.subTest(task_type=task_type):
                decision = autoroute.decide(
                    autoroute.Signal(client="claude", repo="/r", task_type=task_type),
                    policy, fresh_routes=frozenset({"local", "codex"}),
                    load=autoroute.Load(0.1, True))
                self.assertEqual(decision.route, autoroute.RETAIN)
                self.assertEqual(decision.code, "retained_classification_ineligible")

    def test_a_stale_local_route_retains_rather_than_falling_to_a_peer(self):
        decision = self.decide(("claude", "codex", "local"), fresh=("codex",))
        self.assertEqual(decision.route, autoroute.RETAIN)
        self.assertEqual(decision.code, "retained_no_fresh_capacity")


class LinkedWorktreesInheritLocalRoutingOnly(unittest.TestCase):
    """Landing and review checkouts are linked worktrees with their own git
    root. Measured 2026-09-24: many sessions run there, and a policy keyed on
    the main checkout missed them all."""

    def setUp(self):
        import subprocess
        self.temp = tempfile.TemporaryDirectory()
        base = Path(os.path.realpath(self.temp.name))
        self.main = base / "main"
        self.main.mkdir()
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        run = lambda *args: subprocess.run(  # noqa: E731
            ["git", *args], cwd=self.main, env=env, check=True, capture_output=True)
        run("init", "-q")
        run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "x")
        self.linked = base / "landing"
        run("worktree", "add", "-q", str(self.linked))

    def tearDown(self):
        self.temp.cleanup()

    def policy(self, entry):
        return autoroute.Policy(repos={str(self.main): entry})

    def test_a_linked_worktree_inherits_classification_and_the_local_route(self):
        entry = autoroute.RepoPolicy("client_derived", ("claude", "codex", "local"),
                                     mechanical_ok=True, mechanical_globs=("**/*.log",))
        inherited = self.policy(entry).for_repo(str(self.linked))
        self.assertEqual(inherited.classification, "client_derived")
        self.assertEqual(inherited.allowed_routes, ("local",))
        self.assertTrue(inherited.mechanical_ok)
        self.assertEqual(inherited.mechanical_globs, ("**/*.log",))

    def test_peer_routes_are_not_inherited(self):
        entry = autoroute.RepoPolicy("internal_nonclient", ("claude", "codex"), mechanical_ok=True)
        self.assertEqual(self.policy(entry).for_repo(str(self.linked)).allowed_routes, ())

    def test_the_main_checkout_keeps_its_own_entry(self):
        entry = autoroute.RepoPolicy("internal_nonclient", ("claude", "codex", "local"))
        self.assertIs(self.policy(entry).for_repo(str(self.main)), entry)

    def test_an_explicit_worktree_entry_wins(self):
        own = autoroute.RepoPolicy("public", ("claude", "codex"))
        policy = autoroute.Policy(repos={
            str(self.main): autoroute.RepoPolicy("client_derived", ("local",)),
            str(self.linked): own})
        self.assertIs(policy.for_repo(str(self.linked)), own)

    def test_a_forged_git_file_cannot_borrow_a_registered_worktree(self):
        forged = Path(self.temp.name) / "forged"
        forged.mkdir()
        admin = (self.linked / ".git").read_text(encoding="utf-8").strip()
        (forged / ".git").write_text(admin + "\n", encoding="utf-8")
        policy = self.policy(autoroute.RepoPolicy("client_derived", ("local",), mechanical_ok=True))
        self.assertIs(policy.for_repo(str(forged)), policy.default)

    def test_a_relative_path_worktree_also_inherits(self):
        import subprocess
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        relative = Path(os.path.realpath(self.temp.name)) / "relative"
        done = subprocess.run(["git", "worktree", "add", "-q", "--relative-paths", str(relative)],
                              cwd=self.main, env=env, capture_output=True)
        if done.returncode != 0:
            self.skipTest("this git has no --relative-paths")
        entry = autoroute.RepoPolicy("client_derived", ("claude", "local"), mechanical_ok=True)
        self.assertEqual(self.policy(entry).for_repo(str(relative)).allowed_routes, ("local",))

    def test_a_symlinked_git_file_cannot_borrow_a_registered_worktree(self):
        forged = Path(self.temp.name) / "symlinked"
        forged.mkdir()
        (forged / ".git").symlink_to(self.linked / ".git")
        policy = self.policy(autoroute.RepoPolicy("client_derived", ("local",), mechanical_ok=True))
        self.assertIs(policy.for_repo(str(forged)), policy.default)

    def test_a_git_file_naming_an_unregistered_worktree_is_refused(self):
        forged = Path(self.temp.name) / "unregistered"
        forged.mkdir()
        (forged / ".git").write_text(f"gitdir: {self.main}/.git/worktrees/nothing\n",
                                     encoding="utf-8")
        policy = self.policy(autoroute.RepoPolicy("client_derived", ("local",), mechanical_ok=True))
        self.assertIs(policy.for_repo(str(forged)), policy.default)

    def test_a_malformed_git_file_falls_back_to_the_default(self):
        fake = Path(self.temp.name) / "fake"
        fake.mkdir()
        (fake / ".git").write_text(f"gitdir: {self.main}/.git\n", encoding="utf-8")
        entry = autoroute.RepoPolicy("internal_nonclient", ("local",))
        policy = self.policy(entry)
        self.assertIs(policy.for_repo(str(fake)), policy.default)


class LocalLoadCanBeRescuedByMeasuredIdle(unittest.TestCase):
    """Mirrors ``localfirst.readiness()``'s rescue: load-average-per-core
    conflates waiting-on-I/O with genuine CPU contention, so a directly
    measured idle fraction can still let mechanical local work through."""

    def decide(self, load_ratio, cpu_idle_ratio):
        policy = autoroute.Policy(
            repos={"/r": autoroute.RepoPolicy("public", ("local",), mechanical_ok=True)},
            max_local_load_ratio=0.5)
        return autoroute.decide(
            autoroute.Signal(client="claude", repo="/r", task_type="mechanical"),
            policy, fresh_routes=frozenset({"local"}),
            load=autoroute.Load(load_ratio, True), cpu_idle_ratio=cpu_idle_ratio)

    def test_high_load_is_retained_without_a_measured_idle_reading(self):
        decision = self.decide(0.9, None)
        self.assertEqual(decision.route, autoroute.RETAIN)
        self.assertEqual(decision.code, "retained_local_load_high")
        self.assertIsNone(decision.considered["cpu_idle_ratio"])

    def test_high_load_is_rescued_by_a_high_measured_idle_reading(self):
        decision = self.decide(0.9, 0.9)
        self.assertEqual(decision.route, "local")
        self.assertEqual(decision.code, "routed_local_mechanical")
        self.assertEqual(decision.considered["cpu_idle_ratio"], 0.9)

    def test_a_low_measured_idle_reading_does_not_rescue_it(self):
        decision = self.decide(0.9, autoroute.MIN_LOCAL_IDLE_RATIO / 2)
        self.assertEqual(decision.route, autoroute.RETAIN)
        self.assertEqual(decision.code, "retained_local_load_high")

    def test_low_load_needs_no_rescue_and_still_records_the_reading(self):
        decision = self.decide(0.1, 0.4)
        self.assertEqual(decision.route, "local")
        self.assertEqual(decision.considered["cpu_idle_ratio"], 0.4)


class PolicyParsing(unittest.TestCase):
    def test_an_absent_policy_retains_everything(self):
        with tempfile.TemporaryDirectory() as temporary:
            policy = autoroute.load_policy(temporary)
            self.assertEqual(policy.repos, {})
            self.assertEqual(policy.default.allowed_routes, ())
            self.assertEqual(policy.default.classification, "unclassified")

    def test_a_bom_prefixed_policy_is_not_refused(self):
        # A Windows editor or PowerShell's default encoding can prepend a
        # UTF-8 BOM to this operator-edited file. An unreadable policy turns
        # into a deny for every repository, so a BOM must not raise here.
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(autoroute.policy_path(temporary))
            path.parent.mkdir(parents=True)
            path.write_bytes(b"\xef\xbb\xbf" + json.dumps(
                {"version": 1, "repos": {}}).encode())
            policy = autoroute.load_policy(temporary)
            self.assertEqual(policy.repos, {})

    def test_a_relative_repository_key_is_refused(self):
        with self.assertRaises(autoroute.PolicyError):
            autoroute.parse_policy({"version": 1, "repos": {"relative/path": {}}})

    def test_an_unknown_key_in_an_entry_is_refused(self):
        with self.assertRaises(autoroute.PolicyError):
            autoroute.parse_policy({"version": 1, "repos": {
                "/tmp": {"classification": "public", "allow_everything": True}}})

    def test_an_unknown_classification_is_refused(self):
        with self.assertRaises(autoroute.PolicyError):
            autoroute.parse_policy({"version": 1, "repos": {
                "/tmp": {"classification": "totally_fine"}}})

    def test_a_paid_route_cannot_be_expressed(self):
        with self.assertRaises(autoroute.PolicyError):
            autoroute.parse_policy({"version": 1, "repos": {
                "/tmp": {"allowed_routes": ["anthropic_api"]}}})
        self.assertEqual(autoroute.ROUTES, ("claude", "codex", "local"))

    def test_reserved_models_is_empty_by_default(self):
        """A policy written before the field existed reserves nothing."""
        self.assertEqual(autoroute.parse_policy(
            {"version": 1, "repos": {}}).reserved_models, ())

    def test_reserved_models_is_normalised_and_deduplicated(self):
        policy = autoroute.parse_policy({
            "version": 1, "repos": {},
            "reserved_models": ["Astra", " astra ", "FABLE"]})
        self.assertEqual(policy.reserved_models, ("astra", "fable"))

    def test_reserved_models_rejects_a_bad_shape(self):
        for bad in ("astra", [""], ["   "], [None], [1]):
            with self.subTest(bad=bad):
                with self.assertRaises(autoroute.PolicyError):
                    autoroute.parse_policy({"version": 1, "repos": {},
                                            "reserved_models": bad})

    def test_reserved_model_match_names_the_token_it_hit(self):
        """Named, not just refused, so an operator can see which reservation
        fired without reading the policy file to guess."""
        reserved = ("astra", "fable")
        self.assertEqual(autoroute.reserved_model_match("gpt-6-astra", reserved), "astra")
        self.assertEqual(autoroute.reserved_model_match("claude-fable-5-1", reserved), "fable")
        self.assertIsNone(autoroute.reserved_model_match("gpt-5.6-terra", reserved))
        self.assertIsNone(autoroute.reserved_model_match("gpt-6-astra", ()))

    def test_reserved_model_match_leaves_shape_errors_to_the_validator(self):
        """A non-string or blank model is a shape fault the dispatch validator
        already refuses. Answering it here too would give one fault two
        different messages."""
        for value in (None, "", "   ", 5, ["astra"]):
            with self.subTest(value=value):
                self.assertIsNone(autoroute.reserved_model_match(value, ("astra",)))

    def test_route_classifications_is_absent_by_default(self):
        policy = autoroute.parse_policy({"version": 1, "repos": {}})
        self.assertEqual(policy.route_classifications, {})

    def test_route_classifications_narrows_one_route(self):
        policy = autoroute.parse_policy({
            "version": 1, "repos": {},
            "route_classifications": {"codex": ["public"]}})
        self.assertEqual(policy.route_classifications, {"codex": frozenset({"public"})})

    def test_route_classifications_unknown_route_key_is_refused(self):
        with self.assertRaises(autoroute.PolicyError):
            autoroute.parse_policy({"version": 1, "repos": {},
                                    "route_classifications": {"local": ["public"]}})

    def test_route_classifications_must_be_drawn_from_peer_classifications(self):
        with self.assertRaises(autoroute.PolicyError):
            autoroute.parse_policy({"version": 1, "repos": {},
                                    "route_classifications": {"codex": ["client_derived"]}})

    def test_route_classifications_rejects_a_non_list_value(self):
        with self.assertRaises(autoroute.PolicyError):
            autoroute.parse_policy({"version": 1, "repos": {},
                                    "route_classifications": {"codex": "public"}})

    def test_route_classifications_rejects_an_empty_list(self):
        with self.assertRaises(autoroute.PolicyError):
            autoroute.parse_policy({"version": 1, "repos": {},
                                    "route_classifications": {"codex": []}})


class ARouteCanBeNarrowedBelowTheOtherPeer(unittest.TestCase):
    """route_classifications (design closing an adversarial review's finding):
    the consultation bridge's own peer_allowed_classifications already lets
    an operator narrow one peer below the other; this is the routing/dispatch
    side's equivalent, closing the gap where a caller refused a narrower
    peer through consultation could still reach that same peer's cloud CLI
    through execution_dispatch, because the routing decision had only ever
    consulted one classification set shared by both peers.
    """

    def decide(self, client, classification="internal_nonclient", **policy_kwargs):
        policy = autoroute.Policy(
            repos={"/r": autoroute.RepoPolicy(classification, ("claude", "codex"))},
            **policy_kwargs)
        return autoroute.decide(
            autoroute.Signal(client=client, repo="/r"), policy,
            fresh_routes=frozenset({"claude", "codex"}),
            load=autoroute.Load(0.1, True))

    def test_an_unnarrowed_route_still_uses_the_global_set(self):
        decision = self.decide("claude")
        self.assertEqual(decision.route, "codex")

    def test_a_narrowed_peer_retains_material_outside_its_own_set(self):
        decision = self.decide("claude", route_classifications={"codex": frozenset({"public"})})
        self.assertEqual(decision.route, autoroute.RETAIN)
        self.assertEqual(decision.code, "retained_is_the_policy")

    def test_the_same_repository_still_reaches_the_unnarrowed_peer(self):
        # Narrowing codex specifically must not narrow claude too: the
        # asking client here is codex, so its peer is claude, which has no
        # override and keeps the full global set.
        decision = self.decide("codex", route_classifications={"codex": frozenset({"public"})})
        self.assertEqual(decision.route, "claude")

    def test_a_narrowed_peer_still_admits_what_its_own_set_allows(self):
        decision = self.decide("claude", classification="public",
                               route_classifications={"codex": frozenset({"public"})})
        self.assertEqual(decision.route, "codex")


class CapacityIsNotSomethingTheAssistantSays(AutoCase):
    """The review's third finding: capacity evidence was model-controlled.

    ``capacity_observe`` let either assistant report arbitrary availability
    for any route, name its own source, and make it last years. The tool is
    gone. What is left is the operator's standing declaration in
    ``routing-policy.json`` and the gate hook's own first-hand presence, and
    these tests exercise both through the real hook rather than the library.
    """

    def audit_decisions(self) -> list[dict]:
        path = self.state / "routing" / gate.AUDIT_LEDGER
        if not path.exists():
            return []
        return [json.loads(line) for line in
                path.read_text(encoding="utf-8").splitlines() if line.strip()
                and json.loads(line).get("event") == "routing_decided"]

    def classify(self, **extra):
        self.write_policy({str(self.repo): {
            "classification": "internal_nonclient",
            "allowed_routes": ["claude", "codex"]}}, **extra)

    def test_the_operator_declaration_alone_makes_the_peer_eligible(self):
        """No observation, no tool call, no user asking: the file is enough."""
        self.classify(declared_available=["codex"])
        reason = self.assertDenied(self.hook("claude", self.repo), "routed_elsewhere")
        self.assertIn("routed to codex", reason)
        self.assertEqual(self.receipt_for(self.repo)["owner_route"], "codex")

    def test_the_declaration_is_recorded_as_a_declaration(self):
        """It must not be able to pass itself off as a measurement."""
        self.classify(declared_available=["codex"])
        self.hook("claude", self.repo)
        entry = StageRouter(str(self.db)).report()["capacity"]["codex"]
        self.assertEqual(entry["source"], autodecide.DECLARED_SOURCE)
        self.assertTrue(entry["trusted"])

    def test_an_untrusted_row_routes_nothing(self):
        """A row written without trust is exactly what the removed tool wrote."""
        self.classify()
        self.observe("codex", trusted=False)
        self.assertAllowed(self.hook("claude", self.repo))
        self.assertEqual(self.receipt_for(self.repo)["code"],
                         "retained_no_fresh_capacity")

    def test_a_declaration_cannot_outlast_the_operator_removing_it(self):
        """Writing it with a short life was not enough on its own.

        The row the last replay wrote stayed eligible until it aged out, so
        deleting a route from the file took up to fifteen minutes to mean
        anything. The withdrawal is replayed as well.
        """
        self.classify(declared_available=["codex"])
        self.assertDenied(self.hook("claude", self.repo), "routed_elsewhere")
        self.classify()
        self.assertAllowed(self.hook("claude", self.repo))
        self.assertEqual(self.receipt_for(self.repo)["code"],
                         "retained_no_fresh_capacity")

    def test_withdrawal_cannot_erase_a_peer_that_really_is_running(self):
        """The withdrawal names its own source for this reason.

        A peer that has itself run the gate hook is first-hand evidence and
        has nothing to do with the operator's list. A withdrawal that swept
        the route rather than its own row would silently retain work the
        peer was available for.
        """
        self.classify()
        self.observe("codex", source=autodecide.CLIENT_PRESENCE_SOURCE)
        reason = self.assertDenied(self.hook("claude", self.repo), "routed_elsewhere")
        self.assertIn("routed to codex", reason)
        entry = StageRouter(str(self.db)).report()["capacity"]["codex"]
        self.assertEqual(entry["source"], autodecide.CLIENT_PRESENCE_SOURCE)


class ACapacityChangeReDecidesAtOnce(AutoCase):
    """The review's fourth finding: decisions went stale for four hours.

    A receipt that says "the peer has no fresh capacity observation" is the
    one that should stop being true the moment the peer appears. These tests
    change capacity *without touching the policy*, so only the capacity
    fingerprint can be what notices.
    """

    def setUp(self):
        super().setUp()
        self.write_policy({str(self.repo): {
            "classification": "internal_nonclient",
            "allowed_routes": ["claude", "codex"]}})

    def decisions(self) -> int:
        path = self.state / "routing" / gate.AUDIT_LEDGER
        if not path.exists():
            return 0
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines()
                   if line.strip() and json.loads(line).get("event") == "routing_decided")

    def test_a_peer_appearing_re_decides_with_the_policy_untouched(self):
        self.assertAllowed(self.hook("claude", self.repo))
        self.assertEqual(self.receipt_for(self.repo)["code"],
                         "retained_no_fresh_capacity")
        before = self.receipt_for(self.repo)["policy_fingerprint"]

        self.observe("codex")
        reason = self.assertDenied(self.hook("claude", self.repo), "routed_elsewhere")
        self.assertIn("routed to codex", reason)
        receipt = self.receipt_for(self.repo)
        self.assertEqual(receipt["owner_route"], "codex")
        self.assertEqual(receipt["policy_fingerprint"], before,
                         "the policy did not change, so only capacity can have")

    def test_a_peer_going_away_re_decides_too(self):
        self.observe("codex")
        self.assertDenied(self.hook("claude", self.repo), "routed_elsewhere")
        self.observe("codex", available=False)
        self.assertAllowed(self.hook("claude", self.repo))
        self.assertEqual(self.receipt_for(self.repo)["code"],
                         "retained_no_fresh_capacity")

    def test_the_receipt_records_the_capacity_it_was_decided_under(self):
        self.observe("codex")
        self.hook("claude", self.repo)
        # Both routes: codex from the fixture, claude from the hook's own
        # first-hand presence. The digest names every eligible route and says
        # nothing about who asked, which is what keeps it comparable.
        self.assertEqual(self.receipt_for(self.repo)["capacity_fingerprint"],
                         "claude,codex")

    def test_nothing_re_decides_while_nothing_changes(self):
        """The narrowing that makes this affordable.

        The hook refreshes this client's own presence row on every call. A
        fingerprint over the whole table would therefore differ on every
        call, and every call would re-decide: measured at eight decisions
        for this workload instead of one.
        """
        for _ in range(4):
            self.hook("claude", self.repo)
        self.assertEqual(self.decisions(), 1)

    def test_the_route_the_decision_chose_may_act_on_it(self):
        """The livelock guard, and the reason the digest takes no client.

        A receipt is one shared per-repository artifact. While the digest
        subtracted the asking client's own presence row it was
        client-relative, so claude and codex computed different values from
        the identical ledger, each found the other's receipt overtaken, and
        each re-decided it to route the work to the other. Both ended up
        permanently denied, each holding an instruction to dispatch to the
        other.

        This test previously drove codex with ``tool="Edit"``, which Codex
        does not gate, so it passed without reaching any of this.
        """
        self.observe("codex")
        self.assertDenied(self.hook("claude", self.repo), "routed_elsewhere")
        decided = self.decisions()
        self.assertAllowed(self.hook("codex", self.repo))
        self.assertEqual(self.decisions(), decided,
                         "codex re-decided a receipt that already named codex")
        self.assertEqual(self.receipt_for(self.repo)["owner_route"], "codex")

    def test_neither_client_can_route_the_work_at_the_other_forever(self):
        """Alternate the two clients and require the work to land somewhere."""
        self.observe("codex")
        outcomes = []
        for _ in range(4):
            outcomes.append(self.hook("claude", self.repo) == {})
            outcomes.append(self.hook("codex", self.repo) == {})
        self.assertTrue(any(outcomes), "both clients were denied on every call")
        # And the denials are all the same client, the one routed away.
        self.assertEqual(outcomes, [False, True] * 4)

    def test_presence_alone_does_not_re_decide_while_both_stay_fresh(self):
        """Presence is now written on every call; its refreshed timestamp must
        not count as a capacity change. Only arrivals and departures do."""
        self.assertAllowed(self.hook("claude", self.repo))
        self.hook("codex", self.repo)                 # the arrival: one re-decision
        decided = self.decisions()
        for _ in range(3):
            self.hook("claude", self.repo)
            self.hook("codex", self.repo)
        self.assertEqual(self.decisions(), decided)
        self.assertEqual(self.receipt_for(self.repo)["capacity_fingerprint"],
                         "claude,codex")

    def test_a_client_that_expires_and_returns_re_decides_once_each_way(self):
        self.assertAllowed(self.hook("claude", self.repo))
        self.hook("codex", self.repo)
        now = time.time()                             # codex's presence lapses
        StageRouter(str(self.db)).observe_capacity(CapacityObservation(
            route="codex", observed_at=now - 1000, fresh_until=now - 10,
            available=True, source=autodecide.CLIENT_PRESENCE_SOURCE), trusted=True)
        before = self.decisions()
        self.hook("claude", self.repo)                # the departure
        self.assertEqual(self.decisions(), before + 1)
        self.assertEqual(self.receipt_for(self.repo)["capacity_fingerprint"], "claude")
        self.hook("codex", self.repo)                 # the return
        self.assertEqual(self.decisions(), before + 2)
        self.assertEqual(self.receipt_for(self.repo)["capacity_fingerprint"],
                         "claude,codex")

    def test_no_presence_is_recorded_when_automatic_routing_is_off(self):
        self.hook("codex", self.repo, "app.py", None, "--no-automatic-routing")
        self.assertNotIn("codex", StageRouter(str(self.db)).report()["capacity"])


class AFailedPresenceWriteIsVisibleAndChangesNothing(AutoCase):
    """Codex's review of the presence fix: a swallowed failure would leave the
    lock-out in place with nothing to show for it."""

    def test_the_failure_is_logged_once_per_interval_and_reported(self):
        unwritable = self.base / "not-a-database.sqlite3"
        unwritable.write_bytes(b"this is not an sqlite file" * 100)
        moment = [0.0]
        for at in (1000.0, 1010.0, 1070.0):
            moment[0] = at
            self.assertFalse(gate.observe_presence_best_effort(
                "codex", str(self.state), str(unwritable), clock=lambda: moment[0]))
        path = Path(gate.receipt_dir(str(self.state))) / gate.PRESENCE_FAILURE_LEDGER
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["at"] for row in rows], [1000.0, 1070.0],
                         "the call at 1010 falls inside the interval and is not logged")
        self.assertTrue(all(row["client"] == "codex" and row["error"] for row in rows))

    def test_a_success_writes_no_failure_line(self):
        StageRouter(str(self.db))
        self.assertTrue(gate.observe_presence_best_effort(
            "claude", str(self.state), str(self.db)))
        path = Path(gate.receipt_dir(str(self.state))) / gate.PRESENCE_FAILURE_LEDGER
        self.assertFalse(path.exists())

    def test_a_missing_database_is_never_created_by_presence(self):
        # Found by the full suite: creating it turned test_delegation_gate's
        # stage_db_unavailable deny into an automatic decision.
        missing = self.base / "never-created.sqlite3"
        self.assertFalse(gate.observe_presence_best_effort(
            "codex", str(self.state), str(missing)))
        self.assertFalse(missing.exists())

    def test_an_empty_file_is_never_initialized_by_presence(self):
        # Codex's delta review: StageRouter creates its schema on open, so an
        # empty file would have become a valid, empty router.
        empty = self.base / "empty.sqlite3"
        empty.write_bytes(b"")
        self.assertFalse(gate.observe_presence_best_effort(
            "codex", str(self.state), str(empty)))
        import sqlite3
        db = sqlite3.connect(str(empty))
        try:
            tables = db.execute("SELECT name FROM sqlite_master").fetchall()
        finally:
            db.close()
        self.assertEqual(tables, [])

    def test_a_contended_router_does_not_hold_the_hook(self):
        StageRouter(str(self.db))
        import sqlite3
        holder = sqlite3.connect(str(self.db), isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        try:
            started = time.monotonic()
            self.assertFalse(gate.observe_presence_best_effort(
                "codex", str(self.state), str(self.db)))
            self.assertLess(time.monotonic() - started, 1.5)
        finally:
            holder.execute("ROLLBACK")
            holder.close()
        self.assertTrue(gate.observe_presence_best_effort(
            "codex", str(self.state), str(self.db)))

    def test_an_unknown_client_is_refused_and_logged_not_observed(self):
        self.assertFalse(gate.observe_presence_best_effort(
            "local", str(self.state), str(self.db)))
        self.assertNotIn("local", StageRouter(str(self.db)).report()["capacity"])


class ReviewIsNeverRoutedBackToItsAuthor(unittest.TestCase):
    """The second half of the independence rule, which was missing.

    The guard only fired when the asking client was itself the author. A
    review of the *peer's* work fell through to the ordinary branch, where
    the peer was allowed and had capacity, and was routed straight back to
    the author.
    """

    def decide(self, client, author_route, *, fresh=("claude", "codex")):
        policy = autoroute.Policy(
            repos={"/r": autoroute.RepoPolicy("public", ("claude", "codex"))})
        return autoroute.decide(
            autoroute.Signal(client=client, repo="/r", task_type="review",
                             is_review=True, author_route=author_route),
            policy, fresh_routes=frozenset(fresh),
            load=autoroute.Load(0.1, True))

    def test_a_review_of_the_peers_work_is_not_sent_to_the_peer(self):
        decision = self.decide("claude", "codex")
        self.assertEqual(decision.route, autoroute.RETAIN)
        self.assertEqual(decision.code, "retained_review_independence")

    def test_a_review_of_our_own_work_still_goes_to_the_peer(self):
        decision = self.decide("claude", "claude")
        self.assertEqual(decision.route, "codex")
        self.assertEqual(decision.code, "routed_peer_review_independence")

    def test_neither_direction_can_self_review(self):
        for client, peer in (("claude", "codex"), ("codex", "claude")):
            self.assertNotEqual(self.decide(client, peer).route, peer)
            self.assertNotEqual(self.decide(client, client).route, client)

    def test_a_review_of_local_output_may_still_go_to_the_peer(self):
        """Only the author is excluded, not every other route."""
        decision = self.decide("claude", "local")
        self.assertEqual(decision.route, "codex")

    def test_the_code_is_in_the_closed_vocabulary(self):
        self.assertIn("retained_review_independence", autoroute.CODES)


class AnIntentIsRetiredOnlyByTheJobItNames(AutoCase):
    """Cleanup was keyed on the repository, so any job answered for any intent."""

    def setUp(self):
        super().setUp()
        self.write_policy({str(self.repo): {
            "classification": "internal_nonclient",
            "allowed_routes": ["claude", "codex"]}})
        self.observe("codex")
        self.assertDenied(self.hook("claude", self.repo), "routed_elsewhere")
        self.intent = autodecide.read_intent(str(self.state), str(self.repo))

    def events(self) -> list[dict]:
        path = self.state / "routing" / gate.AUDIT_LEDGER
        return [json.loads(line) for line in
                path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def binding(self, **overrides) -> dict:
        return {field: self.intent[field] for field
                in autodecide.INTENT_BINDING} | overrides

    def test_the_exact_binding_retires_it(self):
        self.assertTrue(autodecide.clear_intent(
            str(self.state), str(self.repo), binding=self.binding()))
        self.assertIsNone(autodecide.read_intent(str(self.state), str(self.repo)))

    def test_a_different_stage_does_not(self):
        self.assertFalse(autodecide.clear_intent(
            str(self.state), str(self.repo),
            binding=self.binding(stage="implementation#9")))
        self.assertIsNotNone(autodecide.read_intent(str(self.state), str(self.repo)))

    def test_every_field_of_the_binding_is_load_bearing(self):
        for field in autodecide.INTENT_BINDING:
            wrong = "elsewhere" if field != "stage_revision" else 999
            self.assertFalse(
                autodecide.clear_intent(str(self.state), str(self.repo),
                                        binding=self.binding(**{field: wrong})),
                f"{field} was not checked")
            self.assertIsNotNone(
                autodecide.read_intent(str(self.state), str(self.repo)))

    def test_a_mismatch_is_recorded_rather_than_silent(self):
        autodecide.clear_intent(str(self.state), str(self.repo),
                                binding=self.binding(owner_id="someone-else"))
        unmatched = [e for e in self.events()
                     if e.get("event") == "dispatch_intent_unmatched"]
        self.assertTrue(unmatched)
        self.assertEqual(unmatched[-1]["mismatched"], ["owner_id"])

    def test_a_decision_that_retains_the_work_retires_the_intent(self):
        """Otherwise the audit reports a routing the policy has reversed."""
        self.observe("codex", available=False)
        self.assertAllowed(self.hook("claude", self.repo))
        self.assertIsNone(autodecide.read_intent(str(self.state), str(self.repo)))
        superseded = [e for e in self.events()
                      if e.get("event") == "dispatch_intent_superseded"]
        self.assertTrue(superseded)
        self.assertIn("retained_no_fresh_capacity", superseded[-1]["reason"])


class AStageSomebodyElseOwnsIsNotAdopted(AutoCase):
    """A receipt naming a foreign owner points at a lease it cannot renew."""

    def test_the_decision_uses_the_next_generation_instead(self):
        self.write_policy({str(self.repo): {
            "classification": "internal_nonclient",
            "allowed_routes": ["claude"]}})
        self.observe("claude")          # so the foreign claim below succeeds
        router = StageRouter(str(self.db))
        item = autodecide.item_id_for(str(self.repo))
        registered = router.register(item, "implementation",
                                     allowed_routes=["claude"])
        router.assign(item, "implementation", owner_id="some-agents-own-owner",
                      lease_seconds=600, expected_revision=registered["revision"])

        self.assertAllowed(self.hook("claude", self.repo))
        receipt = self.receipt_for(self.repo)
        self.assertEqual(receipt["owner_id"], autodecide.owner_id_for("claude"))
        self.assertNotEqual(receipt["stage"], "implementation")
        self.assertEqual(router.get(item, "implementation")["owner_id"],
                         "some-agents-own-owner")


class ReviewFindingsFromTheOtherProvider(AutoCase):
    """Raised by codex reviewing the receipt fix. One was real; two were not.

    Kept together because the two that were not are the more useful half: each
    describes a plausible failure this design is claimed to be free of, and a
    claim nothing checks is the kind that stops being true quietly.
    """

    def test_a_manual_receipt_does_not_compute_the_automatic_fingerprints(self):
        """The real one, and a regression introduced by the fix itself.

        The guard it replaced was ``receipt.get("automatic") and
        automatic_receipt_overtaken(...)``, and Python does not evaluate a
        call's arguments until the ``and`` reaches it, so a manual receipt
        never read the policy file or scanned the capacity table. Passing
        those two reads as arguments made every receipt pay for both: a
        sha256 of the policy file and a ``SELECT * FROM capacity`` on every
        gated write, for two values the manual branch then ignores.

        Only the cost is real. The reviewer who raised this expected the
        eager reads to be able to raise past ``stage_binding``'s fail-closed
        handling, but neither can: ``policy_fingerprint`` documents that it
        never raises and ``capacity_digest`` returns None on an unreadable
        table. Checked rather than assumed, and recorded here so the next
        reader does not re-derive it.
        """
        def boom():
            raise AssertionError("the manual path must not compute this")

        manual = {"item_id": "i", "stage": "implementation", "owner_id": "o",
                  "owner_route": "claude", "valid_until": time.time() + 3600,
                  "automatic": False}
        self.assertFalse(gate.receipt_overtaken(
            manual, time.time(), None, "implementation", boom, boom))

        # The automatic path still reads both, which is what makes the
        # difference above a deferral rather than a removal.
        seen = []
        gate.receipt_overtaken({**manual, "automatic": True}, time.time(), None,
                               "implementation",
                               lambda: seen.append("policy") or "p",
                               lambda: seen.append("capacity") or "c")
        self.assertEqual(seen, ["policy", "capacity"])

    def test_an_expired_manual_receipt_over_a_live_stage_is_not_a_wedge(self):
        """Not a defect: the stage is still owned, so it is still renewable.

        The worry was a second deadlock of the same shape -- an expired
        receipt stays non-None, so it skips the re-decision and is then denied
        as ``routing_receipt_expired`` for ever. It is not, and the difference
        is what the deny message tells the operator to do. The deadlock this
        fix addressed was unrecoverable because the stage was terminal, so
        nothing could renew it. An expired receipt over a live stage leaves
        that stage owned by this owner, and ``stage_renew`` on it works.
        """
        self.assertAllowed(self.hook("claude", self.repo))
        receipt = self.receipt_for(self.repo)
        from agent_bridge import store
        store.atomic_write_json(
            gate.receipt_path(str(self.state), str(self.repo)),
            {**receipt, "automatic": False, "valid_until": time.time() - 1})
        self.assertDenied(self.hook("claude", self.repo), "routing_receipt_expired")
        # The way out the deny message names, taken here to prove it exists.
        router = StageRouter(str(self.db))
        current = router.get(receipt["item_id"], receipt["stage"])
        self.assertEqual(current["state"], "owned")
        renewed = router.renew(receipt["item_id"], receipt["stage"],
                               owner_id=current["owner_id"], lease_seconds=3600,
                               expected_revision=current["revision"])
        self.assertEqual(renewed["state"], "owned")

    def test_a_stage_owned_by_another_owner_is_skipped_rather_than_taken(self):
        """Not a defect: discarding a receipt cannot hand its stage to anyone.

        The worry was that clearing a ``stage_reassigned`` receipt and calling
        the decider immediately afterwards could overturn a deliberate
        reassignment. It cannot. ``stage_name`` picks the first generation
        this owner can own and ``_own_stage`` reports a stage somebody else
        holds rather than claiming it, so the decision lands on a new
        generation and the reassigned one is left exactly as it was.

        The larger reading -- that reassigning a stage revokes a client's
        right to edit the repository -- was never true here either: a
        repository with no receipt at all gets a fresh generation by the same
        path. Stages are units of work, not access.
        """
        repo = git_repo(self.base / "other")
        item = autodecide.item_id_for(str(repo))
        self.observe("claude")
        router = StageRouter(str(self.db))
        router.register(item, "implementation", allowed_routes=("claude",),
                        preferred_routes=("claude",))
        before = router.get(item, "implementation")
        router.assign(item, "implementation", owner_id="somebody-else",
                      lease_seconds=3600, expected_revision=before["revision"])

        self.assertAllowed(self.hook("claude", repo))
        self.assertEqual(self.receipt_for(repo)["stage"], "implementation#2")
        untouched = router.get(item, "implementation")
        self.assertEqual(untouched["owner_id"], "somebody-else")
        self.assertEqual(untouched["state"], "owned")


class ARetainIsNotAClaimOnTheRepository(AutoCase):
    """Reproduced 2026-09-24: a retain receipt denied the other assistant.

    A retain decision means "the caller keeps its own work". Enforced as the
    shared receipt it read as "this repository belongs to the caller", so the
    second assistant was denied ``routed_elsewhere`` ("is routed to claude;
    this client does not implement it") in every repository the policy
    retains, until the receipt expired four hours later.
    """

    RETAINING_POLICIES = {
        "no policy at all": {},
        "local only": {"allowed_routes": ["local"]},
        "both assistants, unclassified": {"allowed_routes": ["claude", "codex"]},
    }

    def decisions(self) -> list[dict]:
        path = self.state / "routing" / gate.AUDIT_LEDGER
        if not path.exists():
            return []
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        return [row for row in rows if row.get("event") == "routing_decided"]

    def own_receipt(self, client: str) -> dict | None:
        return gate.read_receipt(str(self.state), str(self.repo), client)

    def arrange(self, entry: dict) -> None:
        self.write_policy({str(self.repo): entry} if entry else {})
        for route in ("claude", "codex", "local"):
            self.observe(route)

    def test_the_second_assistant_gets_its_own_retain_under_each_retaining_policy(self):
        for label, entry in self.RETAINING_POLICIES.items():
            for first, second in (("claude", "codex"), ("codex", "claude")):
                with self.subTest(policy=label, first=first):
                    self.setUp()
                    self.arrange(entry)
                    self.assertAllowed(self.hook(first, self.repo))
                    shared = self.receipt_for(self.repo)
                    self.assertTrue(shared["code"].startswith("retained_"), shared["code"])
                    self.assertEqual(shared["owner_route"], first)

                    self.assertAllowed(self.hook(second, self.repo))
                    own = self.own_receipt(second)
                    self.assertIsNotNone(own)
                    self.assertTrue(own["code"].startswith("retained_"), own["code"])
                    self.assertEqual(own["caller"], second)
                    self.assertEqual(own["owner_route"], second)
                    self.assertEqual(own["owner_id"], autodecide.owner_id_for(second))
                    self.assertTrue(own["retained_alongside"])
                    # The first assistant's decision is untouched, not overwritten.
                    self.assertEqual(self.receipt_for(self.repo), shared)
                    self.assertAllowed(self.hook(first, self.repo))

    def test_the_two_retains_hold_different_stages(self):
        """Stage ownership: neither edits under a lease the other holds."""
        self.arrange({})
        self.hook("claude", self.repo)
        self.hook("codex", self.repo)
        shared, own = self.receipt_for(self.repo), self.own_receipt("codex")
        self.assertEqual(shared["item_id"], own["item_id"])
        self.assertNotEqual(shared["stage"], own["stage"])
        router = StageRouter(str(self.db))
        self.assertEqual(router.get(own["item_id"], own["stage"])["owner_id"],
                         autodecide.owner_id_for("codex"))
        self.assertEqual(router.get(shared["item_id"], shared["stage"])["owner_id"],
                         autodecide.owner_id_for("claude"))

    def test_alternating_assistants_decide_once_each_and_then_never_again(self):
        """No churn and no livelock: nobody re-decides the other's retain."""
        self.arrange({})
        outcomes = []
        for _ in range(4):
            outcomes.append(self.hook("claude", self.repo) == {})
            outcomes.append(self.hook("codex", self.repo) == {})
        self.assertEqual(outcomes, [True] * 8)
        self.assertEqual([row["caller"] for row in self.decisions()], ["claude", "codex"])

    def test_a_routed_away_decision_still_binds(self):
        """The first client routed to its peer stays denied; the peer proceeds."""
        self.write_policy({str(self.repo): {
            "classification": "internal_nonclient",
            "allowed_routes": ["claude", "codex"]}})
        self.observe("codex")
        self.assertDenied(self.hook("claude", self.repo), "routed_elsewhere")
        self.assertAllowed(self.hook("codex", self.repo))
        self.assertDenied(self.hook("claude", self.repo), "routed_elsewhere")
        self.assertIsNone(self.own_receipt("claude"))
        self.assertIsNone(self.own_receipt("codex"))

    def test_a_decision_routing_away_replaces_the_other_retain_and_binds(self):
        """The arriving client's own decision can route its work away. That is
        written to the shared receipt, not beside it, and it binds."""
        self.write_policy({str(self.repo): {
            "classification": "internal_nonclient",
            "allowed_routes": ["claude", "codex"]}}, prefer=["claude", "codex"])
        self.observe("codex")
        self.assertAllowed(self.hook("claude", self.repo))
        self.assertEqual(self.receipt_for(self.repo)["code"], "retained_is_the_policy")
        # codex's own decision: both are eligible and claude is preferred, so
        # codex's work goes to claude. That is a routing, not a retain.
        self.assertDenied(self.hook("codex", self.repo), "routed_elsewhere")
        shared = self.receipt_for(self.repo)
        self.assertEqual((shared["caller"], shared["owner_route"], shared["code"]),
                         ("codex", "claude", "routed_peer_implementation"))
        self.assertIsNone(self.own_receipt("codex"))
        self.assertAllowed(self.hook("claude", self.repo))
        self.assertDenied(self.hook("codex", self.repo), "routed_elsewhere")

    def test_a_manual_retain_still_binds_the_other_client(self):
        """Only an automatic retain is read as the caller keeping its work."""
        self.arrange({})
        self.assertAllowed(self.hook("claude", self.repo))
        shared = dict(self.receipt_for(self.repo))
        shared.pop("automatic")
        store_path = gate.receipt_path(str(self.state), str(self.repo))
        Path(store_path).write_text(json.dumps(shared), encoding="utf-8")
        self.assertDenied(self.hook("codex", self.repo), "routed_elsewhere")

    def test_an_unreadable_own_slot_fails_closed(self):
        self.arrange({})
        self.hook("claude", self.repo)
        self.hook("codex", self.repo)
        Path(gate.receipt_path(str(self.state), str(self.repo), "codex")).write_text(
            "{not json", encoding="utf-8")
        self.assertDenied(self.hook("codex", self.repo), "gate_state_unavailable")
        self.assertAllowed(self.hook("claude", self.repo))

    def test_a_forged_own_slot_is_not_a_receipt(self):
        """A routed-away document planted in the slot cannot bind or free anyone."""
        self.arrange({})
        self.hook("claude", self.repo)
        self.hook("codex", self.repo)
        path = Path(gate.receipt_path(str(self.state), str(self.repo), "codex"))
        forged = json.loads(path.read_text(encoding="utf-8"))
        forged.update(owner_route="local", decision="local", code="routed_local_mechanical")
        path.write_text(json.dumps(forged), encoding="utf-8")
        self.assertDenied(self.hook("codex", self.repo), "gate_state_unavailable")

    def test_a_slot_document_copied_into_the_shared_receipt_is_refused(self):
        self.arrange({})
        self.hook("claude", self.repo)
        self.hook("codex", self.repo)
        own = Path(gate.receipt_path(str(self.state), str(self.repo), "codex"))
        Path(gate.receipt_path(str(self.state), str(self.repo))).write_text(
            own.read_text(encoding="utf-8"), encoding="utf-8")
        self.assertDenied(self.hook("claude", self.repo), "gate_state_unavailable")

    def test_an_unreadable_policy_still_fails_closed_for_the_arriving_client(self):
        self.arrange({})
        self.assertAllowed(self.hook("claude", self.repo))
        Path(autoroute.policy_path(str(self.state))).write_text("{", encoding="utf-8")
        # codex has no receipt of its own yet, so it must decide, and the
        # decision cannot read the policy.
        self.assertDenied(self.hook("codex", self.repo), "gate_auto_decision_failed")

    def test_the_own_retain_is_re_made_when_it_is_overtaken(self):
        """Made and re-made like a shared one: here, its stage completed."""
        self.arrange({})
        self.hook("claude", self.repo)
        self.hook("codex", self.repo)
        first = self.own_receipt("codex")
        router = StageRouter(str(self.db))
        current = router.get(first["item_id"], first["stage"])
        router.complete(first["item_id"], first["stage"], owner_id=first["owner_id"],
                        expected_revision=current["revision"])
        self.assertAllowed(self.hook("codex", self.repo))
        again = self.own_receipt("codex")
        self.assertNotEqual(again["stage"], first["stage"])
        self.assertEqual(self.receipt_for(self.repo)["caller"], "claude")

    def test_the_other_retain_is_never_re_decided_by_the_arrival(self):
        """Whether their retain is stale is theirs to find out: a policy change
        overtakes it, but codex re-decides only its own slot."""
        self.arrange({})
        self.hook("claude", self.repo)
        shared = self.receipt_for(self.repo)
        self.write_policy({}, prefer=["codex"])
        self.assertAllowed(self.hook("codex", self.repo))
        self.assertEqual(self.receipt_for(self.repo), shared)
        self.assertIsNotNone(self.own_receipt("codex"))
        # claude, arriving, re-decides its own shared retain under the new policy.
        self.assertAllowed(self.hook("claude", self.repo))
        self.assertNotEqual(self.receipt_for(self.repo)["policy_fingerprint"],
                            shared["policy_fingerprint"])

    def judge_in_process(self, client: str, decide=None):
        patch = ("*** Begin Patch\n*** Update File: app.py\n@@\n-x = 1\n+x = 2\n"
                 "*** End Patch\n")
        tool, tool_input = (("apply_patch", {"input": patch}) if client == "codex"
                            else ("Edit", {"file_path": str(self.repo / "app.py")}))
        return gate.judge(client, tool, tool_input, str(self.repo), state_root=str(self.state),
                          capacity_db=str(self.db),
                          decide=decide or gate.automatic_decider(client, str(self.state),
                                                                  str(self.db)))

    def test_a_concurrent_retain_cannot_resurrect_a_stale_own_slot(self):
        """Codex's finding 1: the post-decision read must be the decision made.

        codex holds an old slot retain; the policy changes so codex's own
        decision now routes its work to claude. Between that write and the
        gate's read, claude re-decides and puts a retain back in the shared
        slot. Re-reading found codex's stale slot behind it and allowed codex.
        """
        self.arrange({})
        self.hook("claude", self.repo)
        self.hook("codex", self.repo)
        self.assertIsNotNone(self.own_receipt("codex"))
        self.write_policy({str(self.repo): {
            "classification": "internal_nonclient",
            "allowed_routes": ["claude", "codex"]}}, prefer=["claude", "codex"])
        codex_decide = gate.automatic_decider("codex", str(self.state), str(self.db))
        claude_decide = gate.automatic_decider("claude", str(self.state), str(self.db))

        def racing(repo, task_type, **kwargs):
            outcome = codex_decide(repo, task_type, **kwargs)
            self.assertEqual(outcome.receipt["code"], "routed_peer_implementation")
            claude_decide(repo, task_type)             # the interleaved write
            self.assertTrue(gate.is_another_clients_retain(self.receipt_for(self.repo),
                                                           "codex"))
            return outcome

        decision = self.judge_in_process("codex", racing)
        self.assertEqual((decision.permission, decision.code), ("deny", "routed_elsewhere"))

    def test_no_receipt_writer_can_enter_while_a_decision_is_in_flight(self):
        """The lock, not an ordering patch, closes the post-decision races:
        while the gate is deciding, nobody else can take the decision lock."""
        from unittest import mock

        self.arrange({})
        self.hook("claude", self.repo)
        codex_decide = gate.automatic_decider("codex", str(self.state), str(self.db))
        attempts = []

        def deciding(repo, task_type, **kwargs):
            try:
                with gate.decision_lock(str(self.state), repo):
                    attempts.append("entered")
            except TimeoutError:
                attempts.append("excluded")
            return codex_decide(repo, task_type, **kwargs)

        with mock.patch.object(gate, "DECISION_LOCK_TIMEOUT_SECONDS", 0.2):
            decision = self.judge_in_process("codex", deciding)
        self.assertEqual(attempts, ["excluded"])
        self.assertEqual(decision.permission, "allow")

    def test_a_held_decision_lock_fails_closed(self):
        from unittest import mock

        self.arrange({})
        self.hook("claude", self.repo)
        with mock.patch.object(gate, "DECISION_LOCK_TIMEOUT_SECONDS", 0.2), \
                gate.decision_lock(str(self.state), str(self.repo)):
            decision = self.judge_in_process("codex")
        self.assertEqual((decision.permission, decision.code),
                         ("deny", "gate_state_unavailable"))
        self.assertIsNone(self.own_receipt("codex"))

    def test_a_decision_made_while_waiting_is_taken_not_repeated(self):
        """The double check under the lock: another process decided first."""
        import threading

        self.arrange({})
        self.hook("claude", self.repo)
        codex_decide = gate.automatic_decider("codex", str(self.state), str(self.db))
        held = threading.Event()
        other: dict = {}
        gate_calls = []

        def other_process():
            # Holds the lock before the gate below starts, so the gate's
            # first read happens only after this decision is written.
            try:
                with gate.decision_lock(str(self.state), str(self.repo)):
                    held.set()
                    time.sleep(0.5)
                    other["outcome"] = codex_decide(str(self.repo), "implementation",
                                                    retained_alongside=True)
            except BaseException as exc:  # noqa: BLE001  reported below
                other["error"] = exc

        def counting(repo, task_type, **kwargs):
            gate_calls.append(repo)
            return codex_decide(repo, task_type, **kwargs)

        thread = threading.Thread(target=other_process)
        thread.start()
        self.assertTrue(held.wait(10))
        decision = self.judge_in_process("codex", counting)
        thread.join(10)
        self.assertNotIn("error", other)
        self.assertEqual(decision.permission, "allow")
        self.assertEqual(gate_calls, [], "the gate decided a second time")
        self.assertEqual(decision.receipt, other["outcome"].receipt)

    def test_even_an_existing_receipt_is_read_under_the_lock(self):
        """Codex's round-four finding 1: no unlocked fast path. While a writer
        holds the lock, a client with a valid receipt waits rather than
        allowing from a receipt the writer may be replacing."""
        from unittest import mock

        self.arrange({})
        self.hook("claude", self.repo)
        self.hook("codex", self.repo)
        self.assertEqual(self.judge_in_process("codex").permission, "allow")
        with mock.patch.object(gate, "DECISION_LOCK_TIMEOUT_SECONDS", 0.2), \
                gate.decision_lock(str(self.state), str(self.repo)):
            decision = self.judge_in_process("codex")
        self.assertEqual((decision.permission, decision.code),
                         ("deny", "gate_state_unavailable"))

    def test_nested_repositories_are_judged_under_all_their_locks(self):
        """Codex's round-four finding 2: an outer and an inner receipt are read
        as one state, so an edit in the inner repository waits for a writer
        holding only the outer repository's lock."""
        from unittest import mock

        inner = git_repo(self.repo / "inner")
        self.arrange({})
        self.assertAllowed(self.hook("codex", inner, "mod.py"))
        patch = ("*** Begin Patch\n*** Update File: mod.py\n@@\n-x = 1\n+x = 2\n"
                 "*** End Patch\n")

        def judge_inner():
            return gate.judge("codex", "apply_patch", {"input": patch}, str(inner),
                              state_root=str(self.state), capacity_db=str(self.db),
                              decide=gate.automatic_decider("codex", str(self.state),
                                                            str(self.db)))

        self.assertEqual(judge_inner().permission, "allow")
        with mock.patch.object(gate, "DECISION_LOCK_TIMEOUT_SECONDS", 0.2), \
                gate.decision_lock(str(self.state), str(self.repo)):
            decision = judge_inner()
        self.assertEqual((decision.permission, decision.code),
                         ("deny", "gate_state_unavailable"))

    def test_nested_locks_share_one_wait_budget(self):
        """Two enclosing repositories must not stack two full waits against
        the hook's own timeout."""
        from unittest import mock

        inner = git_repo(self.repo / "inner")
        self.arrange({})
        self.hook("codex", inner, "mod.py")
        patch = ("*** Begin Patch\n*** Update File: mod.py\n@@\n-x = 1\n+x = 2\n"
                 "*** End Patch\n")
        with mock.patch.object(gate, "DECISION_LOCK_TIMEOUT_SECONDS", 0.5), \
                gate.decision_lock(str(self.state), str(inner)):
            started = time.monotonic()
            decision = gate.judge("codex", "apply_patch", {"input": patch}, str(inner),
                                  state_root=str(self.state), capacity_db=str(self.db),
                                  decide=gate.automatic_decider("codex", str(self.state),
                                                                str(self.db)))
            waited = time.monotonic() - started
        self.assertEqual(decision.code, "gate_state_unavailable")
        self.assertLess(waited, 0.9, "each repository waited a full budget of its own")

    def test_an_existing_own_slot_is_no_escape_from_a_binding_shared_receipt(self):
        """Once codex holds its own retain, a binding shared receipt (manual,
        or routed away) still denies it: the slot governs only while the
        shared receipt is another client's automatic retain."""
        for kind in ("manual", "routed-away"):
            with self.subTest(kind=kind):
                self.setUp()
                self.arrange({})
                self.hook("claude", self.repo)
                self.assertAllowed(self.hook("codex", self.repo))
                self.assertIsNotNone(self.own_receipt("codex"))
                shared_path = Path(gate.receipt_path(str(self.state), str(self.repo)))
                binding = json.loads(shared_path.read_text(encoding="utf-8"))
                if kind == "manual":
                    binding.pop("automatic")
                else:
                    binding.update(decision="peer", code="routed_peer_implementation",
                                   caller="codex", owner_route="claude")
                shared_path.write_text(json.dumps(binding), encoding="utf-8")
                self.assertDenied(self.hook("codex", self.repo), "routed_elsewhere")

    def test_the_judgment_denies_rather_than_outrun_the_hook_timeout(self):
        """A host runs the tool when a hook produces no decision, so a gate
        killed for running long fails open. Past its own budget the judgment
        denies instead of starting more work."""
        from unittest import mock

        self.arrange({})
        with mock.patch.object(gate, "JUDGMENT_BUDGET_SECONDS", 0.0):
            decision = self.judge_in_process("codex")
        self.assertEqual((decision.permission, decision.code),
                         ("deny", "gate_state_unavailable"))
        self.assertIsNone(self.receipt_for(self.repo))

    def test_the_hook_process_denies_on_its_watchdog_when_the_judgment_is_stuck(self):
        """The hard bound, end to end in a real hook process: the judgment is
        stuck (here, waiting on a lock the test holds), and the watchdog
        writes a deny and exits before the host could time the hook out."""
        self.arrange({})
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Edit",
                   "tool_input": {"file_path": str(self.repo / "app.py")}, "cwd": str(self.repo)}
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), gate.HOOK_WATCHDOG_ENV: "0.5"}
        with gate.decision_lock(str(self.state), str(self.repo)):
            started = time.monotonic()
            completed = subprocess.run(
                [sys.executable, "-P", "-m", "agent_bridge.orchestration.gate",
                 "--client", "claude", "--config", str(self.config)],
                input=json.dumps(payload).encode("utf-8"), capture_output=True,
                timeout=60, env=env)
            elapsed = time.monotonic() - started
        self.assertEqual(completed.returncode, 0, completed.stderr)
        out = json.loads(completed.stdout)
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertTrue(reason.endswith("[gate_timeout]"), reason)
        self.assertEqual(completed.stdout.count(b"hookSpecificOutput"), 1)
        self.assertLess(elapsed, gate.DECISION_LOCK_TIMEOUT_SECONDS,
                        "the deny waited for the lock timeout, not the watchdog")

    def test_the_watchdog_can_only_be_lowered(self):
        from unittest import mock

        for value, expected in (("0.5", 0.5), ("60", gate.HOOK_WATCHDOG_SECONDS),
                                ("nan", gate.HOOK_WATCHDOG_SECONDS),
                                ("-1", gate.HOOK_WATCHDOG_SECONDS),
                                ("x", gate.HOOK_WATCHDOG_SECONDS)):
            with self.subTest(value=value), \
                    mock.patch.dict(os.environ, {gate.HOOK_WATCHDOG_ENV: value}):
                self.assertEqual(gate._watchdog_seconds(), expected)

    def test_a_deeply_nested_routed_away_deny_arrives_well_inside_the_budget(self):
        """Measured on the previous design: five nested repositories, first
        decision, routed-away innermost, 10.3 s -- past the 10 s hook timeout,
        with no contention at all. Under the lock a missing receipt is read
        without the store's one-second retry window."""
        deepest = self.repo
        for depth in range(4):
            deepest = git_repo(deepest / f"n{depth}")
        self.write_policy({str(deepest): {
            "classification": "internal_nonclient",
            "allowed_routes": ["claude", "codex"]}}, prefer=["claude", "codex", "local"])
        for route in ("claude", "codex", "local"):
            self.observe(route)
        started = time.monotonic()
        reason = self.assertDenied(self.hook("codex", deepest, "mod.py", timeout_is_host_limit=False),
                                   "routed_elsewhere")
        elapsed = time.monotonic() - started
        self.assertIn("routed to claude", reason)
        # Measured 0.14 s here; one retry window per missing receipt would be
        # about 5 s. The bound leaves room for a slow CI host's interpreter
        # start-up and still catches that regression.
        self.assertLess(elapsed, 3.0, f"a routed-away deny took {elapsed:.1f} s")

    def test_routing_decide_takes_the_same_lock(self):
        """The hand-made writer is serialised with the gate too."""
        from unittest import mock

        from agent_bridge.localq.intake import AutomaticIntake
        from agent_bridge.localq.spool import FakeBackend, LocalQueue, ResourceSnapshot
        from agent_bridge.orchestration import mcp

        class Idle:
            def sample(self):
                return ResourceSnapshot(0.0, "normal", "normal", True, 10_000, cpu_load_ratio=0.1)

        self.observe("claude")
        router = StageRouter(str(self.db))
        registered = router.register("manual-item", "implementation", allowed_routes=["claude"])
        owned = router.assign("manual-item", "implementation", owner_id="an-agent",
                              lease_seconds=600, expected_revision=registered["revision"])
        queue = LocalQueue(self.base / "queue", sampler=Idle(), backend=FakeBackend())
        tools = mcp.build_tools("claude", router, queue, AutomaticIntake(queue),
                                state_root=str(self.state))
        args = {"item_id": "manual-item", "stage": "implementation", "owner_id": "an-agent",
                "stage_revision": owned["revision"], "repo": str(self.repo),
                "reason": "chosen by hand"}
        with mock.patch.object(gate, "DECISION_LOCK_TIMEOUT_SECONDS", 0.2), \
                gate.decision_lock(str(self.state), str(self.repo)):
            refused = tools["routing_decide"]["handler"](args)
        self.assertFalse(refused["ok"], refused)
        self.assertIsNone(self.receipt_for(self.repo))
        self.assertTrue(tools["routing_decide"]["handler"](args)["ok"])

    def test_a_decider_reporting_nothing_does_not_revive_an_overtaken_slot(self):
        self.arrange({})
        self.hook("claude", self.repo)
        self.hook("codex", self.repo)
        own = self.own_receipt("codex")
        router = StageRouter(str(self.db))
        router.complete(own["item_id"], own["stage"], owner_id=own["owner_id"],
                        expected_revision=router.get(own["item_id"], own["stage"])["revision"])
        decision = self.judge_in_process("codex", lambda repo, task_type, **kwargs: None)
        self.assertEqual((decision.permission, decision.code), ("deny", "no_routing_receipt"))

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                     "POSIX permission bits; root reads anything")
    def test_an_os_error_on_either_receipt_fails_closed(self):
        """Codex's finding 2: only a missing file is "no receipt"."""
        self.arrange({})
        self.hook("claude", self.repo)
        self.hook("codex", self.repo)
        for slot in ("codex", None):
            with self.subTest(slot=slot):
                path = gate.receipt_path(str(self.state), str(self.repo), slot)
                os.chmod(path, 0)
                try:
                    decision = self.judge_in_process("codex")
                    self.assertEqual((decision.permission, decision.code),
                                     ("deny", "gate_state_unavailable"))
                finally:
                    os.chmod(path, 0o600)
        self.assertEqual(self.judge_in_process("codex").permission, "allow")

    def test_a_different_task_type_does_not_make_the_clients_take_turns(self):
        """Codex's finding 3. The hook's task type is always implementation
        today; forced here, neither client re-decides the other's retain."""
        from unittest import mock

        self.arrange({})
        self.assertEqual(self.judge_in_process("claude").permission, "allow")
        before = len(self.decisions())
        with mock.patch.object(gate, "infer_task_type", return_value="review"):
            for _ in range(3):
                self.assertEqual(self.judge_in_process("codex").permission, "allow")
        self.assertEqual(self.judge_in_process("claude").permission, "allow")
        self.assertEqual(len(self.decisions()), before + 1)
        self.assertEqual(self.receipt_for(self.repo)["caller"], "claude")

    def test_every_enclosing_repository_is_judged_on_its_own_receipt(self):
        """An outer retain is not a claim; each repository gets its own decision."""
        inner = git_repo(self.repo / "inner")
        self.arrange({})
        self.assertAllowed(self.hook("claude", self.repo))
        self.assertAllowed(self.hook("codex", inner, "mod.py"))
        self.assertIsNotNone(self.own_receipt("codex"))
        self.assertEqual(gate.read_receipt(str(self.state), str(inner))["caller"], "codex")

    def test_an_outer_routing_binds_an_edit_in_a_nested_repository(self):
        inner = git_repo(self.repo / "inner")
        self.write_policy({str(self.repo): {
            "classification": "internal_nonclient",
            "allowed_routes": ["claude", "codex"]}})
        self.observe("codex")
        self.assertDenied(self.hook("claude", inner, "mod.py"), "routed_elsewhere")

    def test_the_retain_beside_another_leaves_the_intent_alone(self):
        self.arrange({})
        self.hook("claude", self.repo)
        intent = Path(autodecide.intent_path(str(self.state), str(self.repo)))
        intent.parent.mkdir(parents=True, exist_ok=True)
        intent.write_text(json.dumps({"version": 1, "route": "local"}), encoding="utf-8")
        self.assertAllowed(self.hook("codex", self.repo))
        self.assertTrue(intent.exists())


if __name__ == "__main__":
    unittest.main()
