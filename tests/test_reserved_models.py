"""Models the operator keeps for themselves, enforced where jobs are created.

The reservation started as a refusal in the MCP ``execution_dispatch``
handler. A cross-provider review pointed out that this is one caller of
``ExecutionQueue.submit`` rather than the boundary, and that a rule holding at
one caller is a convention. These tests fix both layers in place: the handler
because it is where an agent gets told which reservation it hit, and the queue
because it is where a job actually comes into being.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge.orchestration import autoroute  # noqa: E402
from agent_bridge.orchestration import execution_queue as eq  # noqa: E402

OK = {"returncode": 0, "harness_ok": True,
      "harness_status": eq.HARNESS_COMPLETE, "harness_verdict": "read"}


class QueueAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.state = self.base / "state"
        (self.state / "routing").mkdir(parents=True)
        os.chmod(self.state, 0o700)
        self.calls = 0

    def write_policy(self, reserved):
        path = Path(autoroute.policy_path(str(self.state)))
        path.write_text(json.dumps({"version": 1, "repos": {},
                                    "reserved_models": reserved}),
                        encoding="utf-8")
        os.chmod(path, 0o600)

    def queue(self, matcher=None):
        if matcher is None:
            matcher = autoroute.model_reserved_for(str(self.state))

        def counted(model):
            self.calls += 1
            return matcher(model)

        return eq.ExecutionQueue(self.base / "q", lambda request, job_dir: dict(OK),
                                 clock=lambda: 1.0, model_reserved=counted)

    def submit(self, queue, model, index=[0]):
        index[0] += 1
        repo = Path(tempfile.mkdtemp(dir=str(self.base)))
        (repo / ".git").mkdir()
        brief = repo / "brief.txt"
        brief.write_text("synthetic brief", encoding="utf-8")
        return queue.submit(caller="codex", provider="claude", repo=str(repo),
                            brief=str(brief), base="HEAD",
                            classification="synthetic", model=model, effort="low",
                            item_id=f"item-{index[0]}", stage="stage-1",
                            owner_id="owner-1", stage_revision=1,
                            verify_argv=[["git", "status"]])

    def test_a_reserved_model_is_refused_at_admission(self):
        self.write_policy(["astra", "fable"])
        queue = self.queue()
        with self.assertRaises(eq.ExecutionAdmissionError) as caught:
            self.submit(queue, "gpt-6-astra")
        self.assertEqual(str(caught.exception), "model_reserved_to_operator:astra")

    def test_an_unreserved_model_is_admitted(self):
        self.write_policy(["astra", "fable"])
        queue = self.queue()
        self.assertIn("job_id", self.submit(queue, "gpt-5.6-terra"))

    def test_the_policy_is_read_per_submit_not_captured_at_construction(self):
        """A worker outlives the operator's edit, so the list cannot be frozen.

        Construct the queue under one policy, change the policy, and require
        the next submit to obey the new one. Captured at construction this
        passes only until someone restarts the worker, which is the same as
        not being enforced.
        """
        self.write_policy([])
        queue = self.queue()
        self.assertIn("job_id", self.submit(queue, "gpt-6-astra"))
        self.write_policy(["astra"])
        with self.assertRaises(eq.ExecutionAdmissionError) as caught:
            self.submit(queue, "gpt-6-astra")
        self.assertEqual(str(caught.exception), "model_reserved_to_operator:astra")

    def test_an_unreadable_policy_refuses_rather_than_admitting(self):
        """A reservation that fails open is not a reservation."""
        path = Path(autoroute.policy_path(str(self.state)))
        path.write_text("{not json", encoding="utf-8")
        os.chmod(path, 0o600)
        queue = self.queue()
        with self.assertRaises(eq.ExecutionAdmissionError) as caught:
            self.submit(queue, "gpt-5.6-terra")
        self.assertEqual(str(caught.exception), "reservation_policy_check_failed")

    def test_an_absent_policy_reserves_nothing(self):
        """No policy is a machine where nobody has reserved anything.

        Distinct from the unreadable case on purpose: ``load_policy`` treats a
        missing file as the retain-everything default, and turning that into a
        refusal would make every fresh install unable to dispatch at all.
        """
        queue = self.queue()
        self.assertIn("job_id", self.submit(queue, "gpt-6-astra"))

    def test_a_queue_cannot_be_built_without_saying_what_it_reserves(self):
        """The fail-open this used to permit, now a TypeError at construction.

        ``model_reserved`` started with a default of None, so a queue built
        without it enforced nothing and said nothing, and the only thing
        between that and production was a test grepping the source. Making it
        required is what actually prevents a future construction site from
        silently opting out; a caller that genuinely wants no restriction
        names ``reserve_nothing`` and is readable as having chosen it.
        """
        with self.assertRaises(TypeError):
            eq.ExecutionQueue(self.base / "q2", lambda request, job_dir: dict(OK),
                              clock=lambda: 1.0)
        explicit = eq.ExecutionQueue(self.base / "q3",
                                     lambda request, job_dir: dict(OK),
                                     clock=lambda: 1.0,
                                     model_reserved=eq.reserve_nothing)
        self.assertIn("job_id", self.submit(explicit, "gpt-6-astra"))

    def test_the_check_runs_before_a_job_is_created(self):
        """Refused means no job, not a job that fails later."""
        self.write_policy(["astra"])
        queue = self.queue()
        with self.assertRaises(eq.ExecutionAdmissionError):
            self.submit(queue, "gpt-6-astra")
        self.assertEqual(queue.run_once("worker"), None)


class SpawnTimeTests(unittest.TestCase):
    """The reservation has to hold where the harness is actually launched.

    Admission is not the boundary on its own. ``run_once`` reads the request
    back from ``request.json`` and hands ``request["model"]`` straight to the
    harness as ``--model``, so a job that was admitted before the operator
    reserved a model, or one written into the queue root by something other
    than ``submit``, would otherwise run unchecked. A cross-provider review
    raised exactly this: the worker was given the matcher and never called
    it. These are the tests that would have caught that.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.state = self.base / "state"
        (self.state / "routing").mkdir(parents=True)
        os.chmod(self.state, 0o700)
        self.spawned = []

    def write_policy(self, reserved):
        path = Path(autoroute.policy_path(str(self.state)))
        path.write_text(json.dumps({"version": 1, "repos": {},
                                    "reserved_models": reserved}),
                        encoding="utf-8")
        os.chmod(path, 0o600)

    def queue(self):
        def executor(request, job_dir):
            self.spawned.append(request["model"])
            return dict(OK)

        return eq.ExecutionQueue(
            self.base / "q", executor, clock=lambda: 1.0,
            model_reserved=autoroute.model_reserved_for(str(self.state)))

    def submit(self, queue, model, index=[0]):
        index[0] += 1
        repo = Path(tempfile.mkdtemp(dir=str(self.base)))
        (repo / ".git").mkdir()
        brief = repo / "brief.txt"
        brief.write_text("synthetic brief", encoding="utf-8")
        return queue.submit(caller="codex", provider="claude", repo=str(repo),
                            brief=str(brief), base="HEAD",
                            classification="synthetic", model=model, effort="low",
                            item_id=f"item-{index[0]}", stage="stage-1",
                            owner_id="owner-1", stage_revision=1,
                            verify_argv=[["git", "status"]])

    def refusal(self, queue):
        """The recorded reason, from the receipt rather than the status view.

        ``run_once`` returns the terse status dict (job_id, state, caller,
        provider, classification). The reason a job failed is in its receipt,
        which is where an operator reads it too.
        """
        job = next(d for d in (self.base / "q").iterdir() if d.is_dir())
        return json.loads((job / "receipt.json").read_text(encoding="utf-8"))

    def test_a_job_queued_before_the_reservation_does_not_run(self):
        """The gap the review found, stated as the case that produces it."""
        self.write_policy([])
        queue = self.queue()
        self.submit(queue, "gpt-6-astra")
        self.write_policy(["astra"])
        self.assertEqual(queue.run_once("worker")["state"], "failed")
        self.assertEqual(self.refusal(queue)["error_detail"],
                         "model_reserved_to_operator:astra")
        self.assertEqual(self.spawned, [])

    def test_a_model_written_into_the_queue_directly_does_not_run(self):
        """``submit`` is not the only way a request.json comes to exist."""
        self.write_policy(["astra"])
        queue = self.queue()
        self.submit(queue, "gpt-5.6-terra")
        job = next(d for d in (self.base / "q").iterdir() if d.is_dir())
        request = json.loads((job / "request.json").read_text(encoding="utf-8"))
        request["model"] = "gpt-6-astra"
        (job / "request.json").write_text(json.dumps(request), encoding="utf-8")
        self.assertEqual(queue.run_once("worker")["state"], "failed")
        self.assertEqual(self.refusal(queue)["error_detail"],
                         "model_reserved_to_operator:astra")
        self.assertEqual(self.spawned, [])

    def test_a_non_string_model_on_disk_is_refused_rather_than_spawned(self):
        """At spawn the value came from a file, so its shape is not known."""
        self.write_policy(["astra"])
        queue = self.queue()
        self.submit(queue, "gpt-5.6-terra")
        job = next(d for d in (self.base / "q").iterdir() if d.is_dir())
        request = json.loads((job / "request.json").read_text(encoding="utf-8"))
        request["model"] = None
        (job / "request.json").write_text(json.dumps(request), encoding="utf-8")
        self.assertEqual(queue.run_once("worker")["state"], "failed")
        self.assertEqual(self.refusal(queue)["error_detail"], "model_invalid")
        self.assertEqual(self.spawned, [])

    def test_an_unreserved_job_still_reaches_the_harness(self):
        """The guard against fixing this by refusing everything."""
        self.write_policy(["astra"])
        queue = self.queue()
        self.submit(queue, "gpt-5.6-terra")
        self.assertEqual(queue.run_once("worker")["state"], "complete")
        self.assertEqual(self.spawned, ["gpt-5.6-terra"])


class MatcherTests(unittest.TestCase):
    def test_both_layers_match_through_one_implementation(self):
        """Not a style point: two copies of this rule would drift apart.

        ``mcp.py`` and ``execution_queue.py`` both refuse a reserved model.
        The queue gets a matcher rather than a list precisely so there is one
        definition of what a reserved name is, and this asserts the source
        contains no second one.
        """
        source = (ROOT / "src" / "agent_bridge" / "orchestration"
                  / "execution_queue.py").read_text(encoding="utf-8")
        self.assertNotIn("casefold", source)
        self.assertIn("self._model_reserved", source)

    def test_matching_is_case_folded_and_names_the_token_hit(self):
        self.assertEqual(
            autoroute.reserved_model_match("GPT-6-ASTRA", ("astra",)), "astra")
        self.assertIsNone(autoroute.reserved_model_match("gpt-5.6-terra", ("astra",)))

    def test_a_substring_token_matches_more_than_the_exact_name(self):
        """The reason the tokens are substrings, and its cost, in one place.

        A provider spells one model several ways -- shorthand, full
        identifier, dated snapshot -- so an exact list is one release away
        from reserving nothing. The cost is that a short token can collide
        with an unrelated name, which the reviewer raised. Recorded rather
        than argued: the tokens in use are ``astra`` and ``fable``, and the
        collisions to watch for are words that contain them.
        """
        self.assertEqual(
            autoroute.reserved_model_match("gpt-6-astra-2026-09-01", ("astra",)),
            "astra")
        # The collision, real and accepted. If this ever names a model
        # somebody wants, the tokens need delimiters, not a longer list.
        self.assertEqual(autoroute.reserved_model_match("astral-7b", ("astra",)),
                         "astra")


class WiringTests(unittest.TestCase):
    """Which production queues opt out of the reservation, and why.

    The constructor now refuses a queue that does not say what it reserves,
    so accidental omission is a TypeError rather than a silent fail-open.
    What that cannot catch is a deliberate opt-out: a future production
    module passing ``reserve_nothing`` compiles and enforces nothing. A
    reviewer called the earlier source-grep weak and was right, so this one
    is narrower -- it does not check that the wiring exists, the constructor
    does that; it checks that the set of modules choosing to enforce nothing
    is still the one module that has a reason to.
    """

    SRC = ROOT / "src" / "agent_bridge"

    #: Creates and destroys its own queue root for one fixed synthetic job
    #: with model="default", and has no cfg to read a policy from.
    EXPECTED_OPT_OUTS = {"delegation_verify.py"}

    def test_only_the_known_module_enforces_nothing(self):
        opted_out = {path.name for path in self.SRC.rglob("*.py")
                     if "model_reserved=reserve_nothing" in
                     path.read_text(encoding="utf-8")}
        self.assertEqual(opted_out, self.EXPECTED_OPT_OUTS)

    def test_both_dispatch_queues_use_the_operator_policy(self):
        for name in ("server.py", "execution_worker.py"):
            with self.subTest(name):
                source = (self.SRC / "orchestration" / name).read_text(encoding="utf-8")
                self.assertIn(
                    "model_reserved=autoroute.model_reserved_for(str(cfg.state_root))",
                    source)


if __name__ == "__main__":
    unittest.main()
