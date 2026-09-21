"""One definition of a successful execution, used by every path that judges one.

The defect this guards against is not subtle and it has already happened
twice. A harness exits zero after its verification commands failed; something
downstream reads the exit code, calls it a pass, and the receipt says work was
verified that was not.

The fix is not "check the status here too". It is that there is exactly one
function, :func:`execution_queue.outcome_is_success`, and every path that
decides whether an execution succeeded calls it. This file asserts that
property three ways: by exercising each path, by reading the source for
re-derivations of the rule, and by feeding every path the same adversarial
outcome and requiring the same verdict.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge.orchestration import delegation, execution_queue as eq
from agent_bridge.orchestration import windows_delegation as wdg


#: Outcomes that are not successes, each for a different reason, and each of
#: which some earlier version of some path in this project accepted.
NOT_SUCCESSES = (
    ("zero exit, verification failed",
     {"returncode": 0, "harness_ok": False,
      "harness_status": eq.HARNESS_VERIFICATION_FAILED, "harness_verdict": "read"}),
    ("zero exit, no receipt at all",
     {"returncode": 0, "harness_ok": False,
      "harness_status": eq.HARNESS_ABORTED, "harness_verdict": "receipt_absent"}),
    ("zero exit, receipt unparseable",
     {"returncode": 0, "harness_ok": False,
      "harness_status": eq.HARNESS_ABORTED, "harness_verdict": "receipt_unparseable"}),
    ("nonzero exit, receipt claims complete",
     {"returncode": 1, "harness_ok": True,
      "harness_status": eq.HARNESS_COMPLETE, "harness_verdict": "read"}),
    ("harness failed outright",
     {"returncode": 2, "harness_ok": False,
      "harness_status": eq.HARNESS_FAILED, "harness_verdict": "read"}),
)

SUCCESS = {"returncode": 0, "harness_ok": True,
           "harness_status": eq.HARNESS_COMPLETE, "harness_verdict": "read"}


class TheGateItselfTests(unittest.TestCase):
    def test_the_one_success_shape_passes(self):
        self.assertTrue(eq.outcome_is_success(SUCCESS))

    def test_no_near_miss_passes(self):
        for label, outcome in NOT_SUCCESSES:
            with self.subTest(label):
                self.assertFalse(eq.outcome_is_success(outcome))

    def test_an_outcome_missing_the_verdict_fields_is_not_a_success(self):
        self.assertFalse(eq.outcome_is_success({"returncode": 0}))

    def test_the_status_vocabulary_is_closed(self):
        self.assertEqual(
            eq.HARNESS_STATUSES,
            frozenset({"complete", "verification_failed", "failed", "aborted"}))


class QueuePathTests(unittest.TestCase):
    """The POSIX subprocess harness, through the queue that records state."""

    def _queue(self, outcome):
        root = Path(tempfile.mkdtemp())

        def executor(request, job_dir):
            return dict(outcome)

        return eq.ExecutionQueue(root / "q", executor, clock=lambda: 1.0,
                                  model_reserved=eq.reserve_nothing)

    def _submit(self, queue, counter=[0]):
        counter[0] += 1
        repo = Path(tempfile.mkdtemp())
        (repo / ".git").mkdir()
        brief = repo / "brief.txt"
        brief.write_text("synthetic brief", encoding="utf-8")
        return queue.submit(caller="codex", provider="claude", repo=str(repo),
                            brief=str(brief), base="HEAD",
                            classification="synthetic", model="default",
                            effort="low", item_id=f"item-{counter[0]}",
                            stage="stage-1", owner_id="owner-1",
                            stage_revision=1,
                            verify_argv=[["git", "status"]])

    def test_a_success_records_complete(self):
        queue = self._queue(SUCCESS)
        self._submit(queue)
        self.assertEqual(queue.run_once("worker")["state"], "complete")

    def test_every_near_miss_records_failed(self):
        for label, outcome in NOT_SUCCESSES:
            with self.subTest(label):
                queue = self._queue(outcome)
                self._submit(queue)
                self.assertEqual(queue.run_once("worker")["state"], "failed")

    def test_an_executor_that_omits_the_verdict_is_refused_by_name(self):
        queue = self._queue({"returncode": 0})
        self._submit(queue)
        status = queue.run_once("worker")
        self.assertEqual(status["state"], "failed")
        # The public status view is deliberately redacted, so the named
        # refusal is read from the receipt the queue actually wrote.
        receipt = json.loads((queue.root / status["job_id"] / "receipt.json")
                             .read_text(encoding="utf-8"))
        self.assertEqual(receipt["error"], "ExecutionAdmissionError")
        self.assertNotIn("harness", receipt)


class HarnessReceiptReadingTests(unittest.TestCase):
    """stdout to verdict, for the shapes a real harness prints."""

    def test_a_complete_receipt_is_read(self):
        line = json.dumps({"ok": True, "status": "complete"}).encode()
        self.assertTrue(eq.outcome_is_success({"returncode": 0,
                                               **eq._harness_summary(line)}))

    def test_a_failed_verification_receipt_is_read_as_such(self):
        line = json.dumps({"ok": False, "status": "verification_failed"}).encode()
        summary = eq._harness_summary(line)
        self.assertEqual(summary["harness_status"], "verification_failed")
        self.assertFalse(eq.outcome_is_success({"returncode": 0, **summary}))

    def test_ok_true_with_a_non_complete_status_is_not_a_success(self):
        """A harness contradicting itself is refused, not believed."""

        line = json.dumps({"ok": True, "status": "failed"}).encode()
        self.assertFalse(eq._harness_summary(line)["harness_ok"])

    def test_a_status_outside_the_vocabulary_is_not_renamed(self):
        line = json.dumps({"ok": True, "status": "finished"}).encode()
        summary = eq._harness_summary(line)
        self.assertEqual(summary["harness_verdict"], "receipt_status_unknown")
        self.assertEqual(summary["harness_status"], eq.HARNESS_ABORTED)

    def test_trailing_noise_after_the_receipt_does_not_hide_it(self):
        stdout = (json.dumps({"ok": True, "status": "complete"}) + "\n\n\n").encode()
        self.assertTrue(eq._harness_summary(stdout)["harness_ok"])

    def test_the_last_line_wins_over_an_earlier_one(self):
        stdout = (json.dumps({"ok": True, "status": "complete"}) + "\n"
                  + json.dumps({"ok": False, "status": "verification_failed"})
                  + "\n").encode()
        self.assertFalse(eq._harness_summary(stdout)["harness_ok"])


class WindowsPathTests(unittest.TestCase):
    """The WSL2 executor builds the same outcome shape the queue validates."""

    def test_the_windows_outcome_passes_the_queue_contract(self):
        source = (ROOT / "src" / "agent_bridge" / "orchestration"
                  / "windows_delegation.py").read_text(encoding="utf-8")
        for key in eq.OUTCOME_REQUIRED_KEYS:
            self.assertIn(f'"{key}"', source)

    def test_the_windows_lane_derives_ok_from_the_guest_status(self):
        source = (ROOT / "src" / "agent_bridge" / "orchestration"
                  / "windows_delegation.py").read_text(encoding="utf-8")
        self.assertIn('outcome["harness_ok"] = harness_status == '
                      'guest_runner.HARNESS_COMPLETE', source)

    def test_a_refusal_outcome_is_aborted_rather_than_absent(self):
        source = (ROOT / "src" / "agent_bridge" / "orchestration"
                  / "windows_delegation.py").read_text(encoding="utf-8")
        self.assertIn("guest_runner.HARNESS_ABORTED", source)


class EvidencePathTests(unittest.TestCase):
    """The offline evidence gate, which used to re-derive the rule itself."""

    ROW = {"attempted": True, "reason": None, "state": "complete",
           "source_classification": "synthetic", "worktree_removed": True,
           "permission_to_apply": False, "permission_to_commit": False,
           "permission_to_push": False, "permission_to_merge": False}

    def _row(self, outcome):
        return {**self.ROW, **outcome}

    def test_a_real_pass_is_enabled(self):
        self.assertEqual(delegation._direction_status("codex->claude",
                                                      self._row(SUCCESS)),
                         "enabled")

    def test_every_near_miss_is_refused(self):
        for label, outcome in NOT_SUCCESSES:
            with self.subTest(label):
                with self.assertRaises(delegation.DelegationVerificationError):
                    delegation._direction_status("codex->claude", self._row(outcome))

    def test_a_version_one_row_is_refused_by_shape(self):
        """Evidence written before the verdict fields existed is not graded
        against the weaker rule it was produced under."""

        old = {k: v for k, v in self._row(SUCCESS).items()
               if not k.startswith("harness_")}
        old["returncode"] = 0
        with self.assertRaisesRegex(delegation.DelegationVerificationError,
                                    "unexpected shape"):
            delegation._direction_status("codex->claude", old)

    def test_the_profile_was_bumped_so_the_refusal_is_named(self):
        self.assertEqual(delegation.VERIFICATION_PROFILE, "automatic-delegation-v2")


class NoSecondDefinitionTests(unittest.TestCase):
    """Source-level: nothing re-implements the rule next to the one function."""

    #: Modules that decide whether an *execution* succeeded. Deliberately not
    #: every module: health checks and `schtasks` calls legitimately read a
    #: process exit code, because a process is all they ran.
    EXECUTION_MODULES = (
        "orchestration/execution_queue.py",
        "orchestration/execution_worker.py",
        "orchestration/delegation.py",
        "orchestration/delegation_verify.py",
        "orchestration/windows_delegation.py",
    )

    #: Functions that read an outcome exit code for a reason that is not
    #: "did this succeed". Each one is listed by name with why, because an
    #: unexplained exemption is how a re-derived rule gets back in.
    EXEMPT = {
        # Cross-checks the guest's claimed harness status against its own
        # per-command evidence. It refuses a contradictory report; it never
        # promotes one to a success.
        ("orchestration/windows_delegation.py", "parse_guest_response"):
            "consistency check between a claimed status and its evidence",
    }

    @staticmethod
    def _reads_an_outcome_returncode(node: ast.AST) -> bool:
        """True for `d["returncode"]` and `d.get("returncode")`.

        Deliberately not true for `proc.returncode`. An attribute read is a
        `CompletedProcess`: code holding one of those ran a process and a
        process result is all it is entitled to judge. A *subscript* read is
        an outcome record, and an outcome record has a harness verdict in it,
        so reading only its exit code is the defect this test exists for.
        """

        if (isinstance(node, ast.Subscript)
                and isinstance(node.slice, ast.Constant)
                and node.slice.value == "returncode"):
            return True
        return (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "returncode")

    def _compare_to_zero_returncode(self, tree: ast.AST) -> list[int]:
        """Line numbers of `outcome["returncode"] == 0` style comparisons."""

        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            operands = [node.left, *node.comparators]
            if not any(self._reads_an_outcome_returncode(o) for o in operands):
                continue
            for op, right in zip(node.ops, node.comparators):
                if not isinstance(op, (ast.Eq, ast.NotEq)):
                    continue
                if isinstance(right, ast.Constant) and right.value == 0:
                    found.append(node.lineno)
        return found

    def test_only_the_gate_compares_a_returncode_to_zero(self):
        for name in self.EXECUTION_MODULES:
            path = ROOT / "src" / "agent_bridge" / name
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            lines = source.splitlines()
            for lineno in self._compare_to_zero_returncode(tree):
                inside = self._enclosing_function(tree, lineno)
                if (name, inside) in self.EXEMPT:
                    continue
                self.assertEqual(
                    inside, "outcome_is_success",
                    # One function reads an outcome's exit code and decides.
                    # A second one deciding for itself is how the two shipped
                    # regressions happened.
                    f"{name}:{lineno} re-derives the success rule in "
                    f"{inside or '<module>'}(): {lines[lineno - 1].strip()}")

    @staticmethod
    def _enclosing_function(tree: ast.AST, lineno: int) -> str:
        best = ""
        best_line = -1
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            end = getattr(node, "end_lineno", node.lineno)
            if node.lineno <= lineno <= end and node.lineno > best_line:
                best, best_line = node.name, node.lineno
        return best

    def test_the_evidence_gate_calls_the_queue_s_function(self):
        source = (ROOT / "src" / "agent_bridge" / "orchestration"
                  / "delegation.py").read_text(encoding="utf-8")
        self.assertIn("outcome_is_success(row)", source)

    def test_the_queue_calls_it_too(self):
        source = (ROOT / "src" / "agent_bridge" / "orchestration"
                  / "execution_queue.py").read_text(encoding="utf-8")
        self.assertIn("outcome_is_success(outcome)", source)


if __name__ == "__main__":
    unittest.main()
