"""Offline regressions for the opt-in certified extract and classify pilot."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.localq import gemma_child
from agent_bridge.localq.intake import AutomaticIntake, IntakePolicy
from agent_bridge.localq.spool import AdmissionError, FakeBackend, LocalQueue, QueueCaps, ResourceSnapshot
from agent_bridge.capacity_router import StageRouter
from agent_bridge.orchestration import autoroute, gate, localfirst, mcp


FAKES = Path(__file__).resolve().parent / "fakes"
PYTHON = os.path.realpath(sys.executable)
MODEL = "ab" * 32


class Sampler:
    def sample(self):
        return ResourceSnapshot(time.time(), "normal", "normal", True, 120.0)


class HealthGate:
    def __init__(self, reason=None):
        self.reason = reason

    def check(self):
        return self.reason


class ExtractClassifyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.install = self.root / "install"
        self.install.mkdir()
        self.delegate = self.install / "delegate.py"
        self.validator = self.install / "validator.py"
        shutil.copy(FAKES / "fake_gemma_delegate.py", self.delegate)
        shutil.copy(FAKES / "fake_receipt_validator.py", self.validator)
        self.receipts = self.root / "receipts"
        self.receipts.mkdir()

    def invoke(self, text="synthetic\n", *, task="summarize", params=None, timeout=5.0):
        return gemma_child.invoke(
            {"task_type": task, "input": text, "params": params or {}, "job_id": "job"},
            delegate=str(self.delegate), python=PYTHON, receipt_root=str(self.receipts),
            receipt_validator=str(self.validator), model_digest=MODEL,
            delegate_sha256=hashlib.sha256(self.delegate.read_bytes()).hexdigest(),
            validator_sha256=hashlib.sha256(self.validator.read_bytes()).hexdigest(), timeout=timeout)

    def test_pinned_summarize_regression_shape_and_receipt_task(self):
        text = "synthetic summarize input\n"
        result = self.invoke(text)
        self.assertEqual(result["output"], {"task": "summarize", "text": "SUMMARY: " + text})
        self.assertEqual(result["input_sha256"], hashlib.sha256(text.encode()).hexdigest())
        argv = json.loads((self.install / "observed-argv.json").read_text())
        self.assertEqual(argv[0:2], ["--task", "summarize"])
        self.assertIn("--invocation-id", argv)
        self.assertIn("--parent-task-id", argv)
        receipt = json.loads((self.receipts / f"local-delegate-{result['invocation_id']}.json").read_text())
        self.assertEqual(receipt["task"], "summarize")
        self.assertEqual(receipt["effective_options"]["schema"], False)

    def test_extract_statuses_and_provenance(self):
        documents = [{"id": "one", "text": "Alpha"}]
        source = json.dumps(documents, separators=(",", ":"))
        fields = {name: None for name in gemma_child.EXTRACT_FIELDS}
        for records, status in (([], "no_record"), ([fields, fields], "multiple_records")):
            with self.subTest(status=status):
                (self.install / "extract_records.json").write_text(json.dumps(records))
                result = self.invoke(source, task="extract", params={"documents": documents})
                document = result["output"]["documents"][0]
                self.assertEqual(document["record_status"], status)
                self.assertEqual(document["input_sha256"], hashlib.sha256(b"Alpha").hexdigest())
                self.assertEqual(document["model_digest"], MODEL)
                self.assertTrue(document["invocation_id"])
                self.assertEqual(result["model_digest"], MODEL)

    def test_extract_value_in_another_document_is_rejected(self):
        documents = [{"id": "one", "text": "Alpha"}, {"id": "two", "text": "Beta"}]
        record = {name: None for name in gemma_child.EXTRACT_FIELDS}
        record["party"] = "Beta"
        (self.install / "extract_records.json").write_text(json.dumps([record]))
        with self.assertRaisesRegex(gemma_child.GemmaRefusal, "extract_document_0_value_absent"):
            self.invoke(json.dumps(documents), task="extract", params={"documents": documents})

    def test_wrong_task_or_input_receipt_is_rejected(self):
        for override in ({"task": "classify"}, {"input_sha256": "0" * 64}):
            with self.subTest(override=override):
                (self.install / "receipt_override.json").write_text(json.dumps(override))
                with self.assertRaises(gemma_child.GemmaReceiptError):
                    self.invoke()
                (self.install / "receipt_override.json").unlink()

    def test_extract_deadline_is_terminal_without_partial_result(self):
        documents = [{"id": "one", "text": "one"}, {"id": "two", "text": "two"}]
        completed = {"output": {"text": "[]"}, "model_digest": MODEL,
                     "invocation_id": "a" * 32, "input_sha256": "b" * 64}
        with mock.patch.object(gemma_child, "_invoke_validated", return_value=completed), \
             mock.patch.object(gemma_child.time, "monotonic", side_effect=[0.0, 0.0, 2.0]):
            with self.assertRaisesRegex(gemma_child.GemmaRefusal, "extract_document_1_deadline_exceeded"):
                gemma_child._extract("manifest", documents, "job", self.delegate, Path(PYTHON),
                                     self.receipts, MODEL, 1.0, object())

    def test_classify_provenance_count_and_labels(self):
        result = self.invoke("a\nb", task="classify")
        self.assertEqual(result["model_digest"], MODEL)
        self.assertTrue(result["invocation_id"])
        self.assertEqual(result["input_sha256"], hashlib.sha256(b"a\nb").hexdigest())
        bad = {"output": {"text": "NOT_A_LABEL | a"}, "model_digest": MODEL,
               "invocation_id": "a" * 32, "input_sha256": "b" * 64}
        with mock.patch.object(gemma_child, "_invoke_validated", return_value=bad):
            with self.assertRaisesRegex(gemma_child.GemmaRefusal, "classify_line_1_invalid"):
                gemma_child._classify("a", "job", self.delegate, Path(PYTHON), self.receipts,
                                      MODEL, 1.0, object())
        bad["output"]["text"] = "UNSURE | a\nUNSURE | b"
        with mock.patch.object(gemma_child, "_invoke_validated", return_value=bad):
            with self.assertRaisesRegex(gemma_child.GemmaRefusal, "classify_line_count_mismatch"):
                gemma_child._classify("a", "job", self.delegate, Path(PYTHON), self.receipts,
                                      MODEL, 1.0, object())

    def intake(self, health_gate=None):
        queue = LocalQueue(self.root / "queue", sampler=Sampler(), backend=FakeBackend(),
                           caps=QueueCaps(max_input_bytes=400_000), backend_id="gemma_certified",
                           allowed_task_types=frozenset({"summarize", "extract", "classify"}),
                           client_data_health_gate=health_gate)
        return AutomaticIntake(queue, policy=IntakePolicy(min_input_chars=1, min_nonblank_lines=1))

    def test_extract_admission_uses_shared_document_validator(self):
        intake = self.intake()
        cases = [
            ([{"id": "same", "text": "a"}, {"id": "same", "text": "b"}], "extract_document_1_invalid"),
            ([{"id": "one", "text": "x" * 24_001}], "extract_document_0_oversize"),
            ([{"id": str(i), "text": "x"} for i in range(17)], "extract_documents_invalid"),
        ]
        for documents, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(AdmissionError, reason):
                    intake.route(task_type="extract", input="x", params={"documents": documents},
                                 priority="interactive", classification="synthetic", caller="codex", purpose="test")

    def test_client_derived_tasks_share_the_health_gate(self):
        documents = [{"id": "one", "text": "x"}]
        tasks = (("summarize", {}), ("extract", {"documents": documents}), ("classify", {}))
        for gate_value in (None, "client_data_health_failed"):
            for task, params in tasks:
                with self.subTest(gate_value=gate_value, task=task):
                    intake = self.intake(None if gate_value is None else HealthGate(gate_value))
                    reason = "client_data_health_unverified" if gate_value is None else gate_value
                    with self.assertRaisesRegex(AdmissionError, reason):
                        intake.route(task_type=task, input="x", params=params, priority="interactive",
                                     classification="client_derived", caller="codex", purpose="test")
        for task, params in tasks:
            with self.subTest(admitted_task=task):
                intake = self.intake(HealthGate())
                admitted = intake.route(task_type=task, input="x", params=params, priority="interactive",
                                         classification="client_derived", caller="codex", purpose="test")
                self.assertEqual(admitted["decision"], "local")

    def test_summarize_and_classify_retain_the_old_input_cap(self):
        intake = self.intake()
        for task in ("summarize", "classify"):
            with self.subTest(task=task):
                with self.assertRaisesRegex(AdmissionError, "input_too_large"):
                    intake.route(task_type=task, input="x" * 24_001, params={}, priority="interactive",
                                 classification="synthetic", caller="codex", purpose="test")

    def test_file_tools_are_only_exposed_for_enabled_pilot_tasks(self):
        router = StageRouter(str(self.root / "router.sqlite3"))
        default_queue = LocalQueue(self.root / "default", sampler=Sampler(), backend=FakeBackend(),
            backend_id="gemma_certified", allowed_task_types=frozenset({"summarize"}))
        default_tools = mcp.build_tools("codex", router, default_queue, AutomaticIntake(default_queue))
        self.assertNotIn("work_extract_file", default_tools)
        self.assertNotIn("work_classify_file", default_tools)
        enabled_queue = LocalQueue(self.root / "enabled", sampler=Sampler(), backend=FakeBackend(),
            backend_id="gemma_certified", allowed_task_types=frozenset({"summarize", "extract"}))
        enabled_tools = mcp.build_tools("codex", router, enabled_queue, AutomaticIntake(enabled_queue),
                                        state_root=str(self.root))
        self.assertIn("work_extract_file", enabled_tools)
        self.assertNotIn("work_classify_file", enabled_tools)
        self.assertNotIn("extract", enabled_tools["work_digest_file"]["inputSchema"]["properties"]["task_type"]["enum"])
        self.assertEqual(enabled_tools["work_digest_file"]["handler"]({"path": "/not-read", "task_type": "extract"}),
                         {"ok": False, "error": "digest_task_refused:unsupported_kind"})
        private_queue = LocalQueue(self.root / "private", sampler=Sampler(), backend=FakeBackend(),
            backend_id="private_worker", allowed_task_types=frozenset({"summarize", "extract"}))
        private_tools = mcp.build_tools("codex", router, private_queue, AutomaticIntake(private_queue))
        digest_schema = private_tools["work_digest_file"]["inputSchema"]["properties"]
        self.assertIn("extract", digest_schema["task_type"]["enum"])
        self.assertIn("fields", digest_schema)

    def test_digest_extract_refuses_backslash_field_name(self):
        with self.assertRaisesRegex(localfirst.TemplateError, "fields_invalid"):
            localfirst.render_instruction("extract", max_chars=100, fields=("bad\\field",))

    def test_gate_selects_enabled_file_task_for_both_clients(self):
        state, repo, queue_root = self.root / "state", self.root / "repo", self.root / "queue"
        (state / "routing").mkdir(parents=True)
        (repo / ".git").mkdir(parents=True)
        queue_root.mkdir()
        policy_path = Path(autoroute.policy_path(str(state)))
        policy_path.write_text(json.dumps({"version": 1, "declared_available": ["local"],
            "local_first": {"enabled": True}, "repos": {str(repo): {
                "classification": "internal_nonclient", "allowed_routes": ["local"],
                "mechanical_ok": True, "mechanical_globs": ["**/*.txt"],
                "extract_globs": ["**/*.txt"], "classify_globs": ["**/*.txt"]}}}),
            encoding="utf-8")
        ready = SimpleNamespace(ready=True, code="ready", reason="ready", considered={})
        for task, tool in (("extract", "work_extract_file"), ("classify", "work_classify_file")):
            target = repo / f"{task}.txt"
            target.write_text("x" * 9000, encoding="utf-8")
            with mock.patch.object(gate.localfirst, "readiness", return_value=ready):
                for client in ("claude", "codex"):
                    with self.subTest(task=task, client=client):
                        payload = ({"file_path": str(target)} if client == "claude"
                                   else {"command": f"cat {target}"})
                        decision = gate.judge(client, "Read" if client == "claude" else "Bash", payload,
                                              str(repo), state_root=str(state), local_queue_root=str(queue_root),
                                              worker_executable="fixture", allowed_pilot_tasks=frozenset({task}))
                        self.assertEqual(decision.code, "local_digest_required")
                        self.assertIn(tool, decision.reason)
        disabled = repo / "disabled.txt"
        disabled.write_text("x" * 9000, encoding="utf-8")
        unmatched = repo / "unmatched.bin"
        unmatched.write_text("x" * 9000, encoding="utf-8")
        with mock.patch.object(gate.localfirst, "readiness", return_value=ready):
            fallback = gate.judge("claude", "Read", {"file_path": str(disabled)}, str(repo),
                                  state_root=str(state), local_queue_root=str(queue_root),
                                  worker_executable="fixture")
            self.assertIn("work_digest_file", fallback.reason)
            untouched = gate.judge("codex", "Bash", {"command": f"cat {unmatched}"}, str(repo),
                                   state_root=str(state), local_queue_root=str(queue_root),
                                   worker_executable="fixture", allowed_pilot_tasks=frozenset({"extract"}))
            self.assertEqual(untouched.code, "not_gated_shape")

    def test_certified_file_binds_the_preflight_file_and_policy(self):
        state, repo = self.root / "state", self.root / "repo"
        (repo / ".git").mkdir(parents=True)
        target = repo / "transactions.txt"
        target.write_text("original\n", encoding="utf-8")
        policy_path = Path(autoroute.policy_path(str(state)))
        policy_path.parent.mkdir(parents=True, exist_ok=True)
        policy_path.write_text(json.dumps({"version": 1, "declared_available": ["local"],
            "local_first": {"enabled": True}, "repos": {str(repo): {
                "classification": "internal_nonclient", "allowed_routes": ["local"],
                "mechanical_ok": True, "classify_globs": ["**/*.txt"]}}}), encoding="utf-8")
        queue = LocalQueue(self.root / "bound", sampler=Sampler(), backend=FakeBackend(),
                           backend_id="gemma_certified", allowed_task_types=frozenset({"summarize", "classify"}))
        intake = AutomaticIntake(queue, policy=IntakePolicy(min_input_chars=1, min_nonblank_lines=1))
        tools = mcp.build_tools("codex", StageRouter(str(self.root / "bound.sqlite3")), queue, intake,
                                state_root=str(state))
        actual_route = intake.route
        seen = {}
        def replace_after_route(**kwargs):
            result = actual_route(**kwargs)
            seen["input_sha256"] = result["input_sha256"]
            target.write_text("replacement\n", encoding="utf-8")
            return result
        with mock.patch.object(intake, "route", side_effect=replace_after_route):
            result = tools["work_classify_file"]["handler"]({"path": str(target)})
        self.assertEqual(result, {"ok": False, "error": "classify_path_refused:file_changed"})
        self.assertEqual(seen["input_sha256"], hashlib.sha256(b"original\n").hexdigest())
        self.assertEqual(queue.state_report()["counts"].get("cancelled"), 1)

    def test_certified_file_refuses_oversize_before_read_and_without_local_route(self):
        state, repo = self.root / "state", self.root / "repo"
        (repo / ".git").mkdir(parents=True)
        target = repo / "source.txt"
        target.write_text("small", encoding="utf-8")
        policy_path = Path(autoroute.policy_path(str(state)))
        policy_path.parent.mkdir(parents=True, exist_ok=True)
        base = {"version": 1, "declared_available": ["local"], "local_first": {"enabled": True},
                "repos": {str(repo): {"classification": "internal_nonclient", "allowed_routes": ["local"],
                "mechanical_ok": True, "extract_globs": ["**/*.txt"]}}}
        policy_path.write_text(json.dumps(base), encoding="utf-8")
        queue = LocalQueue(self.root / "cap", sampler=Sampler(), backend=FakeBackend(),
                           caps=QueueCaps(max_input_bytes=100), backend_id="gemma_certified",
                           allowed_task_types=frozenset({"summarize", "extract"}))
        tools = mcp.build_tools("codex", StageRouter(str(self.root / "cap.sqlite3")), queue,
                                AutomaticIntake(queue), state_root=str(state))
        fake_stat = SimpleNamespace(st_size=101)
        handle = mock.MagicMock()
        handle.__enter__.return_value = handle
        policy = autoroute.load_policy(str(state))
        with mock.patch.object(mcp.autoroute, "load_policy", return_value=policy), \
             mock.patch("builtins.open", return_value=handle), \
             mock.patch.object(mcp.os, "fstat", return_value=fake_stat):
            refused = tools["work_extract_file"]["handler"]({"path": str(target)})
        self.assertEqual(refused, {"ok": False, "error": "extract_path_refused:input_too_large"})
        handle.read.assert_not_called()
        base["repos"][str(repo)]["allowed_routes"] = ["claude"]
        policy_path.write_text(json.dumps(base), encoding="utf-8")
        self.assertEqual(tools["work_extract_file"]["handler"]({"path": str(target)}),
                         {"ok": False, "error": "extract_path_refused:policy"})

    def test_extract_feedback_and_delegate_failure_reasons(self):
        queue = LocalQueue(self.root / "feedback", sampler=Sampler(), backend=FakeBackend(),
                           backend_id="gemma_certified", allowed_task_types=frozenset({"summarize", "extract"}))
        tools = mcp.build_tools("codex", StageRouter(str(self.root / "feedback.sqlite3")), queue,
                                AutomaticIntake(queue))
        job = queue.submit(task_type="extract", input='[{"id":"one","text":"x"}]',
                           params={"documents": [{"id": "one", "text": "x"}]}, priority="interactive",
                           classification="synthetic", caller="codex", purpose="work")
        queue.run_once("runner")
        feedback = tools["work_feedback"]["handler"]({"job_id": job["job_id"], "outcome": "used"})
        self.assertEqual(feedback["feedback"]["outcome"], "used")
        documents = [{"id": "one", "text": "x"}]
        with mock.patch.object(gemma_child, "_invoke_validated", side_effect=gemma_child.GemmaRefusal("fixed")):
            with self.assertRaisesRegex(gemma_child.GemmaRefusal, "extract_document_0_failed:fixed"):
                gemma_child._extract("manifest", documents, "job", self.delegate, Path(PYTHON), self.receipts,
                                     MODEL, 1.0, object())
            with self.assertRaisesRegex(gemma_child.GemmaRefusal, "classify_failed:fixed"):
                gemma_child._classify("x", "job", self.delegate, Path(PYTHON), self.receipts,
                                      MODEL, 1.0, object())

    def test_pilot_activation_is_empty_for_malformed_or_unrecognized_config(self):
        missing = self.root / "missing.json"
        self.assertEqual(gate.gemma_pilot_tasks_from_config(str(missing)), frozenset())
        config = self.root / "activation.json"
        for value in ([], ["unknown"], ["extract", "extract"]):
            with self.subTest(value=value):
                config.write_text(json.dumps({"local_backend": "gemma_certified",
                                              "gemma_pilot_tasks": value}), encoding="utf-8")
                self.assertEqual(gate.gemma_pilot_tasks_from_config(str(config)), frozenset())
        config.write_text("[]", encoding="utf-8")
        self.assertEqual(gate.gemma_pilot_tasks_from_config(str(config)), frozenset())


if __name__ == "__main__":
    unittest.main()
