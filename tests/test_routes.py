"""The route registry: what it permits, and everything it must refuse.

Weighted toward denial. A registry that allows the four intended routes but
also allows a fifth nobody noticed is worse than no registry, because it looks
like a control.
"""
from __future__ import annotations

import unittest
from unittest import mock

from agent_bridge import routes


class TestIntendedRoutes(unittest.TestCase):
    def test_the_four_routes_the_operator_asked_for_all_work(self):
        for src, dst, task in (
            ("claude", "codex", None),
            ("codex", "claude", None),
            ("claude", "local", "summarize"),
            ("codex", "local", "summarize"),
        ):
            with self.subTest(route=f"{src}->{dst}"):
                self.assertEqual(dst, routes.authorize(src, dst, "internal", task).name)

    def test_destinations_for_lists_only_authorized_targets(self):
        self.assertEqual(("codex", "local"), routes.destinations_for("claude"))
        self.assertEqual(("claude", "local"), routes.destinations_for("codex"))

    def test_local_is_not_a_caller_today(self):
        # It answers; it does not originate. Nothing routes out of it.
        self.assertEqual((), routes.destinations_for("local"))


class TestDenials(unittest.TestCase):
    def test_no_peer_may_consult_itself(self):
        for name in routes.PEERS:
            with self.subTest(peer=name), self.assertRaises(routes.RouteDenied):
                routes.authorize(name, name, "internal")

    def test_unknown_caller_and_unknown_destination_are_refused(self):
        with self.assertRaises(routes.RouteDenied):
            routes.authorize("gemini", "claude", "internal")
        with self.assertRaises(routes.RouteDenied):
            routes.authorize("claude", "gemini", "internal")

    def test_a_route_absent_from_the_table_is_denied(self):
        # Absence is denial, not a default-allow with exceptions.
        with self.assertRaises(routes.RouteDenied):
            routes.authorize("local", "claude", "internal")

    def test_unknown_classification_is_refused(self):
        with self.assertRaises(routes.RouteDenied):
            routes.authorize("claude", "codex", "confidential")

    def test_client_derived_is_refused_to_every_destination(self):
        for src, dst in routes.ROUTES:
            with self.subTest(route=f"{src}->{dst}"):
                with self.assertRaises(routes.RouteDenied) as cm:
                    routes.authorize(src, dst, "client-derived",
                                     "summarize" if dst == "local" else None)
                self.assertIn("7216", str(cm.exception))

    def test_the_gate_alone_does_not_open_client_derived(self):
        """Two locks. Lifting the gate is not enough while ceilings stay put.

        Guards against a future change that flips CLIENT_DERIVED_GATE and
        assumes that settled it.
        """
        with mock.patch.object(routes, "CLIENT_DERIVED_GATE", True):
            with self.assertRaises(routes.RouteDenied) as cm:
                routes.authorize("claude", "codex", "client-derived")
            self.assertIn("may receive at most", str(cm.exception))

    def test_a_ceiling_below_the_request_denies(self):
        low = routes.Peer(name="rented", locality="cloud", max_classification="public")
        with mock.patch.dict(routes.PEERS, {"rented": low}), \
             mock.patch.object(routes, "ROUTES",
                               routes.ROUTES | {("claude", "rented")}):
            routes.authorize("claude", "rented", "public")          # allowed
            for worse in ("synthetic", "internal"):
                with self.subTest(classification=worse), \
                     self.assertRaises(routes.RouteDenied):
                    routes.authorize("claude", "rented", worse)


class TestTaskEnvelope(unittest.TestCase):
    def test_restricted_peer_refuses_an_open_question(self):
        """The property the measured evidence demands.

        A model that silently drops lines must never be handed an open-ended
        question, because the bridge validates reply shape and not content.
        """
        with self.assertRaises(routes.RouteDenied) as cm:
            routes.authorize("claude", "local", "internal", task=None)
        self.assertIn("does not take open questions", str(cm.exception))

    def test_restricted_peer_refuses_an_uncertified_task(self):
        with self.assertRaises(routes.RouteDenied) as cm:
            routes.authorize("claude", "local", "internal", task="advise")
        self.assertIn("no certified route", str(cm.exception))

    def test_every_certified_task_is_accepted(self):
        for task in routes.PEERS["local"].tasks:
            with self.subTest(task=task):
                routes.authorize("claude", "local", "internal", task)

    def test_judgment_work_is_not_among_the_certified_tasks(self):
        # Named explicitly: certification for bulk tasks is not competence for
        # professional judgment, and the owning skill says so.
        certified = set(routes.PEERS["local"].tasks)
        for forbidden in ("advise", "opine", "materiality", "tax_position",
                          "review", "conclude", "sign"):
            self.assertNotIn(forbidden, certified)

    def test_general_peer_rejects_a_task_parameter(self):
        with self.assertRaises(routes.RouteDenied):
            routes.authorize("claude", "codex", "internal", task="summarize")


class TestRegistryInvariants(unittest.TestCase):
    def test_every_route_names_peers_that_exist(self):
        for src, dst in routes.ROUTES:
            self.assertIn(src, routes.PEERS, f"route source {src} is not a peer")
            self.assertIn(dst, routes.PEERS, f"route dest {dst} is not a peer")

    def test_no_self_route_is_present_in_the_table(self):
        # authorize() also refuses these; this catches one being added.
        self.assertEqual([], [r for r in routes.ROUTES if r[0] == r[1]])

    def test_every_peer_ceiling_is_a_known_classification(self):
        for p in routes.PEERS.values():
            self.assertIn(p.max_classification, routes.CLASSIFICATIONS)

    def test_every_peer_discloses_something(self):
        for p in routes.PEERS.values():
            self.assertTrue(p.disclosure.strip(), f"{p.name} has no disclosure")

    def test_local_peers_require_a_certificate_and_cloud_peers_do_not(self):
        for p in routes.PEERS.values():
            with self.subTest(peer=p.name):
                self.assertEqual(p.locality == "local", p.requires_certificate)

    def test_no_cloud_peer_may_exceed_internal_while_the_gate_is_shut(self):
        for p in routes.PEERS.values():
            if p.locality == "cloud":
                self.assertLessEqual(routes.rank(p.max_classification),
                                     routes.rank("internal"), p.name)


class TestFlowMatrix(unittest.TestCase):
    def test_matrix_covers_exactly_the_permitted_routes(self):
        rows = routes.flow_matrix()
        self.assertEqual(
            {(r["source"], r["destination"]) for r in rows}, set(routes.ROUTES))

    def test_matrix_never_advertises_client_derived_while_the_gate_is_shut(self):
        for row in routes.flow_matrix():
            self.assertNotIn("client-derived", row["classifications_permitted"],
                             f"{row['source']}->{row['destination']}")

    def test_matrix_marks_which_destinations_leave_the_machine(self):
        by = {(r["source"], r["destination"]): r for r in routes.flow_matrix()}
        self.assertTrue(by[("claude", "codex")]["leaves_hardware"])
        self.assertFalse(by[("claude", "local")]["leaves_hardware"])


if __name__ == "__main__":
    unittest.main(verbosity=1)


class TestMatrixDocumentIsCurrent(unittest.TestCase):
    def test_generated_doc_matches_the_registry(self):
        """The counsel-facing document is generated, so it can go stale on disk.

        A matrix approved by counsel that no longer matches what the code
        enforces is worse than none: it is a control everyone believes in.
        """
        import pathlib, subprocess, sys
        root = pathlib.Path(__file__).resolve().parents[1]
        doc = root / "docs" / "data-flow-matrix.md"
        self.assertTrue(doc.is_file(), "docs/data-flow-matrix.md is missing")
        fresh = subprocess.run(
            [sys.executable, str(root / "bin" / "agent-bridge-flow-matrix")],
            capture_output=True, text=True, check=True).stdout
        self.assertEqual(
            fresh, doc.read_text(),
            "docs/data-flow-matrix.md is stale; regenerate with "
            "bin/agent-bridge-flow-matrix > docs/data-flow-matrix.md")
