"""Verification commands run with no egress, and prove it before they run.

Why this exists: verification executes repository code. That code is the
output of a model acting on a brief, and it is the least trusted thing in the
system. The provider phase legitimately needs public egress, because a
provider CLI cannot authenticate without reaching its provider. A test suite
needs none, and a check that can open a socket is a check that can post the
workspace somewhere.

The POSIX lane already had this: verification there runs under a macOS sandbox
profile carrying ``(deny network*)``. The Windows guest did not, so the two
lanes disagreed about what a check was allowed to do.

The simulation boundary, stated plainly: there is no nftables and no kernel to
load a table into on the machine these run on. What is proven here is that the
job calls the enforcement, that the enforcement refuses on every failure it
can observe, and that a refusal ends the job rather than downgrading it. What
is NOT proven here is that nftables drops the packet. Only the live canary on
a real guest can show that, and it has not been run.
"""

from __future__ import annotations

import base64
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge.orchestration import guest_runner as gr


class RulesetShapeTests(unittest.TestCase):
    def test_the_verification_policy_is_deny_by_default(self):
        """An accept-policy chain with drop rules denies only what it lists.
        The verification posture has to be the other way round."""

        self.assertIn("policy drop;", gr.verify_egress_ruleset())

    def test_only_loopback_is_accepted(self):
        ruleset = gr.verify_egress_ruleset()
        self.assertIn("oifname lo accept", ruleset)
        self.assertEqual(ruleset.count("accept"), 1)

    def test_it_is_a_separate_table_from_the_job_policy(self):
        """Separate so removing it restores the previous posture exactly,
        with nothing to reconstruct."""

        self.assertNotEqual(gr.VERIFY_EGRESS_TABLE, gr.EGRESS_TABLE)
        self.assertIn(gr.VERIFY_EGRESS_TABLE, gr.verify_egress_ruleset())
        self.assertNotIn(gr.EGRESS_TABLE, gr.verify_egress_ruleset())

    def test_the_job_policy_still_allows_public_egress(self):
        """The provider phase needs it. Removing that would break the lane
        rather than harden it."""

        self.assertIn("policy accept;", gr.egress_ruleset())

    def test_the_contract_is_stated_in_code(self):
        contract = gr.VERIFICATION_EGRESS_CONTRACT
        self.assertIn("deny", contract)
        self.assertIn("negative canary", contract)
        self.assertIn("does not run", contract)


class EnforcementTests(unittest.TestCase):
    """Three steps, each ruling out a different failure."""

    def _enforce(self, *, applied=True, present=True, reachable=None):
        with mock.patch.object(gr, "apply_verify_egress_policy",
                               mock.Mock() if applied else mock.Mock(
                                   side_effect=gr.GuestRunnerError(
                                       "verify_egress_apply_failed"))), \
             mock.patch.object(gr, "_verify_egress_rules_present",
                               lambda: present), \
             mock.patch.object(gr, "public_egress_reachable",
                               lambda *a, **k: reachable):
            return gr.enforce_verification_egress()

    def test_a_loaded_and_proven_policy_returns_a_receipt(self):
        receipt = self._enforce()
        self.assertTrue(receipt.startswith("verify-egress-denied:"))
        self.assertEqual(len(receipt.split(":")[1]), 64)

    def test_a_policy_that_would_not_load_is_refused(self):
        with self.assertRaises(gr.GuestRunnerError) as caught:
            self._enforce(applied=False)
        self.assertEqual(caught.exception.code, "verify_egress_apply_failed")

    def test_a_policy_the_kernel_does_not_hold_is_refused(self):
        """A zero exit from the tool that installed it is not evidence that
        the kernel holds the rules."""

        with self.assertRaises(gr.GuestRunnerError) as caught:
            self._enforce(present=False)
        self.assertEqual(caught.exception.code, "verify_egress_policy_absent")

    def test_a_policy_that_is_loaded_but_bypassed_is_refused(self):
        """The failure the read-back cannot see: the table is present and a
        second interface or an unexpected route carries the packet anyway."""

        with self.assertRaises(gr.GuestRunnerError) as caught:
            self._enforce(reachable="1.1.1.1:443")
        self.assertEqual(caught.exception.code, "verify_egress_not_enforced")

    def test_the_refusal_never_names_the_address_that_answered(self):
        with self.assertRaises(gr.GuestRunnerError) as caught:
            self._enforce(reachable="203.0.113.9:443")
        self.assertNotIn("203.0.113", str(caught.exception))

    def test_the_read_back_requires_the_drop_policy_not_just_a_rule(self):
        """A chain that accepts loopback but forgot its policy denies
        nothing at all."""

        listing = f"table inet {gr.VERIFY_EGRESS_TABLE} {{ chain output {{ " \
                  "type filter hook output priority 0; policy accept; " \
                  "oifname lo accept }} }}"
        completed = mock.Mock(returncode=0, stdout=listing.encode("utf-8"))
        with mock.patch.object(gr.subprocess, "run", return_value=completed):
            self.assertFalse(gr._verify_egress_rules_present())

    def test_the_read_back_accepts_a_correct_listing(self):
        listing = f"table inet {gr.VERIFY_EGRESS_TABLE} {{ chain output {{ " \
                  "type filter hook output priority 0; policy drop; " \
                  "oifname lo accept }} }}"
        completed = mock.Mock(returncode=0, stdout=listing.encode("utf-8"))
        with mock.patch.object(gr.subprocess, "run", return_value=completed):
            self.assertTrue(gr._verify_egress_rules_present())

    def test_a_missing_nft_binary_refuses_rather_than_skipping(self):
        with mock.patch.object(gr.os.path, "isfile", lambda path: False):
            with self.assertRaises(gr.GuestRunnerError) as caught:
                gr.apply_verify_egress_policy()
        self.assertEqual(caught.exception.code, "verify_egress_tool_missing")


class ExfiltrationProbeTests(unittest.TestCase):
    """The negative canary: a public address must not answer."""

    def _probe(self, behaviour):
        class _Socket:
            @staticmethod
            def create_connection(address, timeout=None):
                return behaviour(address)

        with mock.patch.dict(sys.modules, {"socket": _Socket}):
            return gr.public_egress_reachable(timeout=0.01)

    def test_a_destination_that_answers_is_reported_as_reachable(self):
        def answers(address):
            class _Conn:
                def __enter__(self): return self
                def __exit__(self, *a): return False
            return _Conn()

        self.assertIsNotNone(self._probe(answers))

    def test_a_destination_that_refuses_is_also_reachable(self):
        """A RST means the packet left this guest and something replied,
        which is exactly what the drop exists to prevent."""

        def refuses(address):
            raise ConnectionRefusedError

        self.assertIsNotNone(self._probe(refuses))

    def test_a_destination_that_drops_is_not_reachable(self):
        def drops(address):
            raise OSError("timed out")

        self.assertIsNone(self._probe(drops))

    def test_every_target_is_tried_before_declaring_success(self):
        tried = []

        def drops(address):
            tried.append(address)
            raise OSError("timed out")

        self._probe(drops)
        self.assertEqual(len(tried), len(gr.PUBLIC_EGRESS_PROBE_TARGETS))

    def test_the_targets_are_public_addresses(self):
        """Probing a private address would test the other canary's claim."""

        for host, _port in gr.PUBLIC_EGRESS_PROBE_TARGETS:
            first = int(host.split(".")[0])
            self.assertNotIn(first, (10, 127, 169))
            self.assertNotEqual(host.split(".")[0:2], ["192", "168"])


class JobWiringTests(unittest.TestCase):
    """The enforcement is not optional, and a failure ends the job."""

    def setUp(self):
        from test_guest_runner import (VERSIONS, _ProviderCapsule,
                                       _provider_request)
        self.versions = VERSIONS
        self.capsule = _ProviderCapsule
        self.request = _provider_request

    def _execute(self, *, egress, responses=None):
        replies = list(responses or [])

        def runner(argv, cwd, env, stdin_data, timeout):
            return replies.pop(0) if replies else (0, b"", b"")

        with mock.patch.object(gr, "read_versions",
                               return_value=self.versions["tools"]), \
             mock.patch.object(gr, "_AuthCapsule", self.capsule), \
             mock.patch.object(gr, "unpack_workspace", lambda *a: None), \
             mock.patch.object(gr, "capture_diff", lambda workdir: b""), \
             mock.patch.object(gr, "enforce_verification_egress", egress), \
             mock.patch.object(gr, "verify_program_path",
                               lambda name: "/usr/bin/" + name):
            return gr.execute(gr.validate_request(self.request()),
                              runner=runner)

    def test_a_job_with_checks_enforces_the_policy_before_running_them(self):
        order = []

        def egress():
            order.append("egress")
            return "verify-egress-denied:x"

        replies = [(0, b"", b"")]

        def runner(argv, cwd, env, stdin_data, timeout):
            order.append(argv[0])
            return replies.pop(0) if replies else (0, b"", b"")

        with mock.patch.object(gr, "read_versions",
                               return_value=self.versions["tools"]), \
             mock.patch.object(gr, "_AuthCapsule", self.capsule), \
             mock.patch.object(gr, "unpack_workspace", lambda *a: None), \
             mock.patch.object(gr, "capture_diff", lambda workdir: b""), \
             mock.patch.object(gr, "enforce_verification_egress", egress), \
             mock.patch.object(gr, "verify_program_path",
                               lambda name: "/usr/bin/" + name):
            gr.execute(gr.validate_request(self.request()), runner=runner)
        self.assertTrue(order[0].endswith("claude"), "provider ran first")
        self.assertEqual(order[1], "egress",
                         "the policy was not loaded before the first check")
        self.assertTrue(order[2].endswith("git"))

    def test_a_policy_that_cannot_be_enforced_aborts_the_job(self):
        def failing():
            raise gr.GuestRunnerError("verify_egress_not_enforced")

        response = self._execute(egress=failing)
        self.assertEqual(response["status"], "aborted")
        self.assertEqual(response["harness_status"], gr.HARNESS_ABORTED)
        self.assertEqual(response["reason"], "verify_egress_not_enforced")

    def test_a_policy_failure_is_never_reported_as_a_completed_job(self):
        def failing():
            raise gr.GuestRunnerError("verify_egress_policy_absent")

        response = self._execute(egress=failing)
        self.assertNotEqual(response["harness_status"], gr.HARNESS_COMPLETE)

    def test_no_check_runs_when_the_policy_could_not_be_enforced(self):
        ran = []

        def failing():
            raise gr.GuestRunnerError("verify_egress_apply_failed")

        def runner(argv, cwd, env, stdin_data, timeout):
            ran.append(argv[0])
            return 0, b"", b""

        with mock.patch.object(gr, "read_versions",
                               return_value=self.versions["tools"]), \
             mock.patch.object(gr, "_AuthCapsule", self.capsule), \
             mock.patch.object(gr, "unpack_workspace", lambda *a: None), \
             mock.patch.object(gr, "capture_diff", lambda workdir: b""), \
             mock.patch.object(gr, "enforce_verification_egress", failing), \
             mock.patch.object(gr, "verify_program_path",
                               lambda name: "/usr/bin/" + name):
            gr.execute(gr.validate_request(self.request()), runner=runner)
        self.assertEqual([name for name in ran if name.endswith("git")], [])

    def test_the_receipt_records_which_posture_each_check_ran_under(self):
        response = self._execute(egress=lambda: "verify-egress-denied:abc")
        for record in response["verification"]:
            self.assertEqual(record["egress"], "verify-egress-denied:abc")

    def test_the_posture_is_part_of_the_verification_contract(self):
        self.assertIn("egress", gr.VERIFICATION_KEYS)


if __name__ == "__main__":
    unittest.main()
