"""Boundary cases exposed by the offline delegation-audit mutation sweep."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge.orchestration import audit  # noqa: E402


class LedgerMutationCases(unittest.TestCase):
    def test_record_at_window_boundary_is_included(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = Path(temporary) / "audit.jsonl"
            ledger.write_text('{"at": 400, "event": "routing_decided"}\n', encoding="utf-8")
            self.assertEqual(len(audit._read_ledger(str(ledger), 400)), 1)

    def test_unreadable_ledger_returns_error_row(self):
        with mock.patch.object(audit.os.path, "exists", return_value=True), \
                mock.patch("builtins.open", side_effect=OSError("unreadable")):
            self.assertEqual(audit._read_ledger("/invented/ledger.jsonl", None),
                             [{"error": "ledger_unreadable", "path": "/invented/ledger.jsonl"}])


class EligibilityMutationCases(unittest.TestCase):
    def test_unclassified_decision_is_explicitly_ineligible(self):
        self.assertIs(audit._eligible({"considered": {"classification": "unclassified",
                                                     "allowed_routes": ["codex"],
                                                     "client": "claude"}}), False)

    def test_opted_in_client_work_accepts_either_local_or_permitted_peer(self):
        base = {"classification": "client_derived", "client": "claude",
                "allowed_routes": ["local", "codex"], "mechanical_ok": True,
                "task_type": "mechanical", "client_derived_execution": True,
                "client_derived_peer_routes": ["codex"]}
        self.assertTrue(audit._eligible({"considered": {**base,
            "client_derived_peer_routes": []}}))
        self.assertTrue(audit._eligible({"considered": {**base,
            "allowed_routes": ["codex"], "mechanical_ok": False}}))
        self.assertFalse(audit._eligible({"considered": {**base,
            "allowed_routes": ["claude"], "mechanical_ok": False}}))
        self.assertFalse(audit._eligible({"considered": {**base,
            "allowed_routes": ["claude"], "mechanical_ok": False,
            "client_derived_peer_routes": ["claude"]}}))

    def test_nonclient_route_must_differ_from_caller(self):
        self.assertFalse(audit._eligible({"considered": {
            "classification": "public", "client": "claude",
            "allowed_routes": ["claude"]}}))


class HookCoverageMutationCases(unittest.TestCase):
    def test_untrusted_codex_hook_explains_ungated_calls(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            hooks = home / "hooks.json"
            hooks.write_text(json.dumps({"hooks": {"PreToolUse": [
                {"hooks": [{"command": "agent-bridge-gate-hook"}]}]}}), encoding="utf-8")
            paths = {"claude_settings": str(home / "missing.json"),
                     "codex_hooks": str(hooks), "codex_toml": str(home / "config.toml")}
            with mock.patch.object(audit.gate, "install_paths", return_value=paths), \
                    mock.patch.object(audit.gate, "_is_ours", return_value=True), \
                    mock.patch.object(audit.gate, "codex_trust_state", return_value="needs_review"):
                coverage = audit._hook_coverage(str(home))
            self.assertTrue(coverage["codex"]["installed"])
            self.assertIn("not trusted", coverage["codex"]["consequence"])


class QueueMutationCases(unittest.TestCase):
    def test_local_queue_uses_five_second_read_only_timeout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "localq.sqlite3").touch()
            with mock.patch.object(sqlite3, "connect", side_effect=sqlite3.OperationalError("offline")) as connect:
                result = audit._local_queue_report(str(root))
            self.assertEqual(result["error"], "OperationalError")
            self.assertEqual(connect.call_args.kwargs["timeout"], 5.0)
            self.assertTrue(connect.call_args.kwargs["uri"])

    def test_execution_queue_counts_each_receipt_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, payload in (("job-alpha", {"state": "complete"}),
                                  ("job-beta", {"state": "complete"}),
                                  ("job-gamma", "invalid")):
                directory = root / name
                directory.mkdir()
                (directory / "receipt.json").write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(audit._queue_report(str(root))["states"],
                             {"complete": 2, "unreadable": 1})


class LocalFirstMutationCases(unittest.TestCase):
    def test_missing_readiness_inputs_report_unknown(self):
        result = audit._local_first_report("/invented/state", events=[], audit_entries=[],
                                           local_root=None, worker_executable=None,
                                           policy=None, now=500)
        self.assertEqual(result["readiness_now"]["code"], "unknown")

    def test_compelled_reads_are_counted_by_repository(self):
        result = audit._local_first_report("/invented/state", events=[
            {"code": "local_digest_present", "client": "claude",
             "repos": ["/invented/aster", "/invented/birch"]}],
            audit_entries=[], local_root=None, worker_executable=None,
            policy=None, now=500)
        self.assertEqual(result["compelled"]["by_repo"],
                         {"/invented/aster": 1, "/invented/birch": 1})


class ReportMutationCases(unittest.TestCase):
    def test_default_window_is_24_hours(self):
        with tempfile.TemporaryDirectory() as temporary:
            document = audit.report(str(Path(temporary) / "state"), home=temporary,
                                    clock=lambda: 500_000)
            self.assertEqual(document["window_hours"], 24.0)

    def test_gemma_backend_uses_its_delegate_executable_for_readiness(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "orchestration.json"
            config.write_text(json.dumps({"local_backend": "gemma_certified",
                "local_queue_root": str(root / "localq"),
                "gemma_delegate_executable": "/invented/gemma-worker",
                "worker_executable": "/invented/default-worker"}), encoding="utf-8")
            ready = SimpleNamespace(ready=True, code="ready", reason="offline",
                                    considered={})
            with mock.patch.object(audit.autoroute, "load_policy", return_value=object()), \
                    mock.patch.object(audit.localfirst, "readiness", return_value=ready) as readiness:
                document = audit.report(str(root / "state"), home=temporary,
                                        config_path=str(config), clock=lambda: 500_000)
            self.assertEqual(document["local_first"]["readiness_now"]["code"], "ready")
            self.assertEqual(readiness.call_args.kwargs["worker_executable"],
                             "/invented/gemma-worker")


class RenderMutationCases(unittest.TestCase):
    def document(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return audit.report(str(Path(temporary.name) / "state"), home=temporary.name,
                            clock=lambda: 500_000)

    def test_installed_hook_with_automatic_routing_off_is_labelled(self):
        document = self.document()
        document["hook_coverage"]["claude"] = {
            "installed": True, "automatic_routing": False, "consequence": None}
        text = audit.render(document)
        self.assertIn("hook (claude): installed, automatic routing off", text)

    def test_automatic_hook_is_not_labelled_off(self):
        document = self.document()
        document["hook_coverage"]["claude"] = {
            "installed": True, "automatic_routing": True, "consequence": None}
        self.assertIn("hook (claude): installed\n", audit.render(document))

    def test_gate_failure_code_and_queue_state_are_rendered(self):
        document = self.document()
        document["failures"]["gate"] = {"count": 1, "by_code": {"gate_error": 1}}
        document["failures"]["execution_queue"] = {
            "present": True, "states": {"failed": 1}, "failures": []}
        text = audit.render(document)
        self.assertIn("gate failures: 1 {'gate_error': 1}", text)
        self.assertIn("execution_queue: {'failed': 1}", text)


if __name__ == "__main__":
    unittest.main()
