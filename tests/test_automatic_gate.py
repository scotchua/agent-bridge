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
             tool: str | None = None, *args) -> dict:
        tool = tool if tool is not None else self.EDIT_TOOL[client]
        if tool == "apply_patch":
            tool_input = {"input": "*** Begin Patch\n*** Update File: "
                                   f"{relative}\n@@\n-x = 1\n+x = 2\n*** End Patch\n"}
        else:
            tool_input = {"file_path": str(repo / relative)}
        payload = {"hook_event_name": "PreToolUse", "tool_name": tool,
                   "tool_input": tool_input,
                   "cwd": str(repo)}
        completed = subprocess.run(
            [sys.executable, "-P", "-m", "agent_bridge.orchestration.gate",
             "--client", client, "--config", str(self.config), *args],
            input=json.dumps(payload).encode("utf-8"), capture_output=True,
            timeout=120, env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

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
    """Both were live failures, not hypotheticals. See the module docstring."""

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
        completed = subprocess.run(
            [sys.executable, "-P", "-m", "agent_bridge.orchestration.gate",
             "--client", client, "--config", str(self.config)],
            input=json.dumps(payload).encode("utf-8"), capture_output=True,
            timeout=120, env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

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


if __name__ == "__main__":
    unittest.main()
