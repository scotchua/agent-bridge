"""Offline contract tests for the certified Gemma queue adapter."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.localq import backend_select, gemma_child, worker_child
from agent_bridge.capacity_router import StageRouter
from agent_bridge.localq.intake import AutomaticIntake, IntakePolicy
from agent_bridge.localq.service import Service
from agent_bridge.localq.spool import (FakeBackend, LocalQueue, QueueCaps,
                                       ResourceSnapshot, SubprocessBackend,
                                       UNSUPPORTED_KIND_REASON, AdmissionError)
from agent_bridge.orchestration.config import OrchestrationConfigError, load
from agent_bridge.orchestration import audit, autoroute, delegation_verify, gate, localfirst, mcp


FAKES = Path(__file__).resolve().parent / "fakes"
REAL_PYTHON = os.path.realpath(sys.executable)
MODEL_DIGEST = "ab" * 32


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Sampler:
    def sample(self):
        return ResourceSnapshot(time.time(), "normal", "normal", True, 120.0)


def fixture(root: Path) -> dict[str, Path]:
    install = root / "delegate-install"
    install.mkdir()
    delegate = install / "delegate.py"
    validator = install / "delegation_receipt.py"
    shutil.copy(FAKES / "fake_gemma_delegate.py", delegate)
    shutil.copy(FAKES / "fake_receipt_validator.py", validator)
    receipts = root / "receipts"
    receipts.mkdir()
    return {"install": install, "delegate": delegate,
            "validator": validator, "receipts": receipts}


def config_doc(root: Path, fx: dict[str, Path], backend: str = "gemma_certified") -> dict:
    doc = {
        "config_version": "1", "state_root": str(root / "state"),
        "local_queue_root": str(root / "state" / "local-queue"),
        "capacity_db": str(root / "state" / "capacity.sqlite3"),
        "worker_executable": str(root / "worker"),
        "worker_state": str(root / "worker-state"), "local_backend": backend,
    }
    if backend == "gemma_certified":
        doc.update({
            "gemma_delegate_executable": str(fx["delegate"]),
            "gemma_python_executable": REAL_PYTHON,
            "gemma_receipt_root": str(fx["receipts"]),
            "gemma_receipt_validator_executable": str(fx["validator"]),
            "gemma_delegate_sha256": file_sha256(fx["delegate"]),
            "gemma_receipt_validator_sha256": file_sha256(fx["validator"]),
            "gemma_model_digest": MODEL_DIGEST,
        })
    return doc


class ConfigAndSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fx = fixture(self.root)
        (self.root / "worker").write_text("fixture", encoding="utf-8")
        (self.root / "worker-state").mkdir()

    def write(self, doc: dict) -> Path:
        path = self.root / "config.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path

    def test_default_is_compatibility_private_worker(self):
        doc = config_doc(self.root, self.fx, "private_worker")
        cfg = load(self.write(doc))
        self.assertEqual(cfg.local_backend, "private_worker")
        self.assertIsNone(cfg.gemma_model_digest)

    def test_gemma_requires_complete_existing_paths_and_digest(self):
        doc = config_doc(self.root, self.fx)
        doc.pop("gemma_model_digest")
        with self.assertRaisesRegex(OrchestrationConfigError, "configuration_incomplete"):
            load(self.write(doc))
        doc = config_doc(self.root, self.fx)
        doc["gemma_model_digest"] = "not-a-digest"
        with self.assertRaisesRegex(OrchestrationConfigError, "model_digest_invalid"):
            load(self.write(doc))

    def test_gemma_timeout_cannot_shorten_certified_budget(self):
        doc = config_doc(self.root, self.fx)
        doc["gemma_timeout_seconds"] = 30
        with self.assertRaisesRegex(OrchestrationConfigError, "below_certified_budget"):
            load(self.write(doc))

    def test_gemma_timeout_rejects_nonfinite_values(self):
        for timeout in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(timeout=timeout):
                doc = config_doc(self.root, self.fx)
                doc["gemma_timeout_seconds"] = timeout
                with self.assertRaisesRegex(OrchestrationConfigError,
                                            "gemma_timeout_seconds_below_certified_budget"):
                    load(self.write(doc))

    def test_gemma_timeout_accepts_default_and_finite_budget(self):
        doc = config_doc(self.root, self.fx)
        self.assertEqual(load(self.write(doc)).gemma_timeout_seconds, 600.0)
        for timeout in (600, 720.5):
            with self.subTest(timeout=timeout):
                doc["gemma_timeout_seconds"] = timeout
                self.assertEqual(load(self.write(doc)).gemma_timeout_seconds, timeout)

    def test_selected_backend_is_fixed_and_summarize_only_by_default(self):
        cfg = load(self.write(config_doc(self.root, self.fx)))
        if os.name != "posix":
            with self.assertRaisesRegex(backend_select.BackendSelectionError,
                                        "platform_unsupported"):
                backend_select.build_backend_and_caps(cfg, str(self.root / "queue"))
            return
        backend, caps, allowed = backend_select.build_backend_and_caps(
            cfg, str(self.root / "queue"))
        self.assertIsInstance(backend, SubprocessBackend)
        self.assertIn(str(Path(gemma_child.__file__).resolve()), backend.command)
        self.assertNotIn(str(Path(worker_child.__file__).resolve()), backend.command)
        self.assertIn(MODEL_DIGEST, backend.command)
        self.assertEqual(allowed, frozenset({"summarize"}))
        self.assertGreater(caps.lease_seconds, caps.timeout_seconds)
        service = Service.for_config(cfg, root=str(self.root / "service"), sampler=Sampler())
        self.assertEqual(service.queue.allowed_task_types, frozenset({"summarize"}))
        self.assertEqual(service.queue.backend_id, "gemma_certified")

    @unittest.skipUnless(os.name == "posix", "certified Gemma backend requires POSIX process groups")
    def test_live_digest_uses_certified_gemma_call_shape(self):
        doc = config_doc(self.root, self.fx)
        cfg = load(self.write(doc))
        repo = self.root / "repo"
        (repo / ".git").mkdir(parents=True)
        target = repo / "synthetic.log"
        target.write_text("Synthetic log entry.\n" * 500, encoding="utf-8")
        policy_path = Path(autoroute.policy_path(str(self.root / "state")))
        policy_path.parent.mkdir(parents=True, exist_ok=True)
        policy_path.write_text(json.dumps({
            "version": 1, "declared_available": ["local"],
            "local_first": {"enabled": True},
            "repos": {str(repo): {"classification": "internal_nonclient",
                                  "allowed_routes": ["claude", "codex", "local"],
                                  "mechanical_ok": True, "mechanical_globs": ["**/*.log"]}},
        }), encoding="utf-8")
        service = Service.for_config(cfg, root=str(self.root / "queue"), sampler=Sampler())
        intake = AutomaticIntake(service.queue, policy=IntakePolicy(min_input_chars=1,
                                                                      min_nonblank_lines=1))
        tools = mcp.build_tools("codex", StageRouter(str(self.root / "router.sqlite3")),
                                service.queue, intake, state_root=str(self.root / "state"))
        digest = tools["work_digest_file"]
        self.assertEqual(digest["inputSchema"]["properties"]["task_type"]["enum"],
                         ["summarize"])
        refused = digest["handler"]({"path": str(target), "task_type": "log_triage"})
        self.assertEqual(refused["error"], "digest_task_refused:unsupported_kind")
        self.assertEqual(service.queue.state_report()["counts"], {})
        submitted = digest["handler"]({"path": str(target), "task_type": "summarize"})
        self.assertTrue(submitted["ok"], submitted)
        self.assertTrue(submitted["job_id"])
        service.once()
        result = tools["work_result"]["handler"]({"job_id": submitted["job_id"]})
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(result["result"]["provider"], "gemma_certified")

    def test_explicit_pilot_opt_in_selects_only_named_tasks(self):
        for tasks in (["extract"], ["log_triage"], ["extract", "classify", "log_triage"]):
            with self.subTest(tasks=tasks):
                doc = config_doc(self.root, self.fx)
                doc["gemma_pilot_tasks"] = tasks
                path = self.write(doc)
                cfg = load(path)
                self.assertEqual(gate.gemma_pilot_tasks_from_config(str(path)), frozenset(tasks))
                if os.name == "posix":
                    _, _, allowed = backend_select.build_backend_and_caps(cfg, str(self.root / "queue"))
                    self.assertEqual(allowed, frozenset({"summarize", *tasks}))

    def test_pilot_config_rejects_unknown_duplicate_and_private_worker_values(self):
        for value in (["extract", "extract"], ["log_triage", "log_triage"],
                      ["summarize"], ["unknown"], ["test_draft"], ["checklist"],
                      [[]], [{}], "extract"):
            with self.subTest(value=value):
                doc = config_doc(self.root, self.fx)
                doc["gemma_pilot_tasks"] = value
                with self.assertRaisesRegex(OrchestrationConfigError, "gemma_pilot_tasks_invalid"):
                    load(self.write(doc))
                self.assertEqual(gate.gemma_pilot_tasks_from_config(str(self.write(doc))), frozenset())
                if os.name == "posix":
                    cfg = SimpleNamespace(**config_doc(self.root, self.fx), gemma_pilot_tasks=value)
                    with self.assertRaisesRegex(backend_select.BackendSelectionError,
                                                "gemma_pilot_tasks_invalid"):
                        backend_select.build_backend_and_caps(cfg, str(self.root / "queue"))
        doc = config_doc(self.root, self.fx, "private_worker")
        doc["gemma_pilot_tasks"] = []
        with self.assertRaisesRegex(OrchestrationConfigError, "configuration_must_be_absent"):
            load(self.write(doc))

    @unittest.skipUnless(os.name == "posix", "certified Gemma backend requires POSIX process groups")
    def test_log_triage_opt_in_controls_queue_intake_and_digest_surface(self):
        source = "[L1] Synthetic warning\n[L2] Synthetic debug\n"
        for enabled in (False, True):
            doc = config_doc(self.root, self.fx)
            doc["gemma_pilot_tasks"] = ["log_triage"] if enabled else []
            service = Service.for_config(load(self.write(doc)),
                                         root=str(self.root / f"queue-{enabled}"), sampler=Sampler())
            intake = AutomaticIntake(service.queue, policy=IntakePolicy(min_input_chars=1))
            tools = mcp.build_tools("codex", StageRouter(str(self.root / "router.sqlite3")),
                                    service.queue, intake, state_root=str(self.root / "state"))
            advertised = tools["work_digest_file"]["inputSchema"]["properties"]["task_type"]["enum"]
            self.assertEqual("log_triage" in advertised, enabled)
            if not enabled:
                with self.assertRaisesRegex(AdmissionError, "^unsupported_kind$"):
                    service.queue.submit(task_type="log_triage", input=source, params={},
                                         priority="interactive", classification="synthetic",
                                         caller="codex", purpose="work")
                refused = intake.route(task_type="log_triage", input=source, params={},
                                       priority="interactive", classification="synthetic",
                                       caller="codex", purpose="test")
                self.assertEqual(refused["reason"], "unsupported_kind")
                self.assertIsNone(refused["job_id"])
                continue
            for bad_input, params, reason in (
                    ("Missing identity", {}, "log_triage_input_invalid"),
                    (source, {"instruction": ""}, "params_invalid_for_gemma_certified")):
                with self.subTest(reason=reason):
                    with self.assertRaisesRegex(AdmissionError, "^" + reason + "$"):
                        intake.route(task_type="log_triage", input=bad_input, params=params,
                                     priority="interactive", classification="synthetic",
                                     caller="codex", purpose="test")
            self.assertEqual(service.queue.state_report()["counts"], {})
            # A passing client health check must never widen this pilot.
            service.queue.client_data_health_gate = SimpleNamespace(check=lambda: None)
            for classification in ("client_derived", "unclassified"):
                with self.subTest(classification=classification):
                    checkpoint = intake.checkpoint(
                        task_id=classification, task_type="log_triage", classification=classification,
                        caller="codex", input_bytes=len(source.encode()), nonblank_lines=2)
                    self.assertEqual(checkpoint["reason"], "classification_refused")
                    with self.assertRaisesRegex(AdmissionError, "^classification_refused$"):
                        service.queue.submit(task_type="log_triage", input=source, params={},
                                             priority="interactive", classification=classification,
                                             caller="codex", purpose="work")
            for classification in ("synthetic", "public", "internal_nonclient"):
                checkpoint = intake.checkpoint(
                    task_id=classification, task_type="log_triage", classification=classification,
                    caller="codex", input_bytes=len(source.encode()), nonblank_lines=2)
                routed = intake.route(task_type="log_triage", input=source, params={},
                                      priority="interactive", classification=classification,
                                      caller="codex", purpose="work", checkpoint_id=checkpoint["checkpoint_id"])
                self.assertEqual(routed["decision"], "local")
                result = service.once()["last_result"]
                self.assertEqual(result["status"], "complete", result)
                self.assertEqual(result["result"]["output"]["task"], "log_triage")

            repo = self.root / "repo"
            (repo / ".git").mkdir(parents=True)
            target = repo / "synthetic.log"
            target.write_text(source, encoding="utf-8")
            policy_path = Path(autoroute.policy_path(str(self.root / "state")))
            policy_path.parent.mkdir(parents=True, exist_ok=True)
            policy_path.write_text(json.dumps({"version": 1, "local_first": {"enabled": True},
                "repos": {str(repo): {"classification": "internal_nonclient",
                                      "allowed_routes": ["local"], "mechanical_ok": True,
                                      "mechanical_globs": ["**/*.log"]}}}), encoding="utf-8")
            routed = tools["work_digest_file"]["handler"]({"path": str(target), "task_type": "log_triage"})
            self.assertTrue(routed["ok"], routed)
            self.assertTrue(routed["job_id"], routed)
            service.once()
            result = tools["work_result"]["handler"]({"job_id": routed["job_id"]})
            self.assertEqual(result["status"], "complete", result)
            self.assertEqual(result["result"]["output"]["task"], "log_triage")

    def test_gate_offers_log_triage_only_when_enabled_and_nonclient(self):
        state, repo = self.root / "state", self.root / "repo"
        (repo / ".git").mkdir(parents=True)
        policy_path = Path(autoroute.policy_path(str(state)))
        policy_path.parent.mkdir(parents=True)
        ready = SimpleNamespace(ready=True, code="ready", reason="ready", considered={})
        for classification in ("internal_nonclient", "client_derived"):
            policy_path.write_text(json.dumps({"version": 1, "local_first": {"enabled": True},
                "repos": {str(repo): {"classification": classification, "allowed_routes": ["local"],
                                      "mechanical_ok": True, "mechanical_globs": ["**/*.log"]}}}))
            for enabled in (False, True):
                for client in ("claude", "codex"):
                    with self.subTest(classification=classification, enabled=enabled, client=client):
                        target = repo / f"{classification}-{enabled}-{client}.log"
                        target.write_text("[synthetic] Warning\n" * 500, encoding="utf-8")
                        payload = ({"file_path": str(target)} if client == "claude"
                                   else {"command": f"cat {target}"})
                        with mock.patch.object(gate.localfirst, "readiness", return_value=ready):
                            decision = gate.judge(
                                client, "Read" if client == "claude" else "Bash", payload, str(repo),
                                state_root=str(state), local_queue_root=str(self.root / "queue"),
                                worker_executable="fixture",
                                allowed_pilot_tasks=frozenset({"log_triage"}) if enabled else frozenset())
                        self.assertEqual(decision.code, "local_digest_required")
                        self.assertEqual("summarize|log_triage" in decision.reason,
                                         enabled and classification == "internal_nonclient")
                        if enabled and classification == "internal_nonclient":
                            self.assertIn("1-6 ASCII letters then 1-4 digits, e.g. [L1]", decision.reason)

    def test_audit_selects_gemma_delegate_for_readiness(self):
        doc = config_doc(self.root, self.fx)
        config_path = self.write(doc)
        state = self.root / "state"
        policy_path = Path(autoroute.policy_path(str(state)))
        policy_path.parent.mkdir(parents=True, exist_ok=True)
        policy_path.write_text(json.dumps({"version": 1, "declared_available": ["local"],
                                           "local_first": {"enabled": True}, "repos": {}}),
                               encoding="utf-8")
        ready = SimpleNamespace(ready=True, code="ready", reason="ready", considered={})
        with mock.patch.object(audit.localfirst, "readiness", return_value=ready) as check:
            audit.report(str(state), config_path=str(config_path))
        self.assertEqual(check.call_args.kwargs["worker_executable"],
                         str(self.fx["delegate"]))

    def test_gate_readiness_uses_the_backend_calibration_target(self):
        private = config_doc(self.root, self.fx, "private_worker")
        private_path = self.write(private)
        self.assertEqual(gate.gate_paths_from_config(str(private_path))[3],
                         private["worker_executable"])

        gemma = config_doc(self.root, self.fx)
        gemma_path = self.write(gemma)
        self.assertEqual(gate.gate_paths_from_config(str(gemma_path))[3],
                         gemma["gemma_delegate_executable"])

    @unittest.skipUnless(os.name == "posix", "certified Gemma backend requires POSIX process groups")
    def test_checkpoint_and_route_measure_the_same_unicode_payload(self):
        cfg = load(self.write(config_doc(self.root, self.fx)))
        service = Service.for_config(cfg, root=str(self.root / "unicode"), sampler=Sampler())
        intake = AutomaticIntake(
            service.queue, policy=IntakePolicy(min_input_chars=1, min_nonblank_lines=1))
        samples = ("caf\u00e9\r\n", "cafe\u0301\n", "\ufeff\u4e16\u754c\n", "\U0001f680\n")
        for index, text in enumerate(samples):
            with self.subTest(index=index):
                checkpoint = intake.checkpoint(
                    task_id=f"unicode-{index}", task_type="summarize",
                    classification="internal_nonclient", caller="codex",
                    input_bytes=len(text.encode("utf-8")),
                    nonblank_lines=sum(1 for line in text.splitlines() if line.strip()))
                routed = intake.route(
                    task_type="summarize", input=text, params=None,
                    priority="interactive", classification="internal_nonclient",
                    caller="codex", purpose="work",
                    checkpoint_id=checkpoint["checkpoint_id"])
                self.assertEqual(routed["decision"], "local")

    @unittest.skipUnless(os.name == "posix", "certified Gemma backend requires POSIX process groups")
    def test_gemma_calibration_uses_only_certified_params_and_runs_every_sample(self):
        config_path = self.write(config_doc(self.root, self.fx))
        result = delegation_verify.calibrate(str(config_path), sampler=Sampler())
        self.assertTrue(result["ok"], result)
        record = localfirst.load_calibration_record(str(self.root / "state"))
        self.assertEqual(record["backend_id"], "gemma_certified")
        expected = len(localfirst.CALIBRATION_SIZES) * localfirst.CALIBRATION_RUNS_PER_SIZE
        self.assertEqual(int((self.fx["install"] / "calls.txt").read_text()), expected)
        samples = [delegation_verify._synthetic_calibration_input(8000, index)
                   for index in range(localfirst.CALIBRATION_RUNS_PER_SIZE)]
        self.assertEqual({len(sample.encode("utf-8")) for sample in samples}, {8000})
        self.assertEqual(len(set(samples)), localfirst.CALIBRATION_RUNS_PER_SIZE)
        self.assertTrue(all(sample.count(". ") == 15 and sample.endswith(".")
                            for sample in samples))
        self.assertTrue(all(max(len(sentence) + 1 for sentence in sample.split(". "))
                            < len(sample) // 10 for sample in samples))


class GemmaInvokeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fx = fixture(self.root)

    def invoke(self, text: str = "raw café → 世界\n", *, params=None,
               task_type: str = "summarize", timeout: float = 5.0):
        return gemma_child.invoke(
            {"task_type": task_type, "input": text, "params": params or {}, "job_id": "job-1"},
            delegate=str(self.fx["delegate"]), python=REAL_PYTHON,
            receipt_root=str(self.fx["receipts"]),
            receipt_validator=str(self.fx["validator"]),
            delegate_sha256=file_sha256(self.fx["delegate"]),
            validator_sha256=file_sha256(self.fx["validator"]),
            model_digest=MODEL_DIGEST, timeout=timeout)

    def mode(self, value: str) -> None:
        (self.fx["install"] / "mode.txt").write_text(value, encoding="utf-8")

    def override(self, value: dict) -> None:
        (self.fx["install"] / "receipt_override.json").write_text(
            json.dumps(value), encoding="utf-8")

    def output_sequence(self, *outputs):
        (self.fx["install"] / "output_sequence.json").write_text(json.dumps(outputs), encoding="utf-8")

    def extract_json(self, *, party="Alpha"):
        record = dict.fromkeys(gemma_child.EXTRACT_FIELDS)
        record["party"] = party
        return json.dumps([record])

    def test_fenced_extract_is_receipt_validated_before_repair(self):
        raw = "```json\n" + self.extract_json() + "\n```\n"
        self.output_sequence(raw)
        result = self.invoke("manifest", task_type="extract",
                             params={"documents": [{"id": "one", "text": "Alpha invoice"}]})
        doc = result["output"]["documents"][0]
        self.assertEqual(doc["records"][0]["fields"]["party"],
                         {"value": "Alpha", "spans": [[0, 5]], "ambiguous": False})
        receipt = json.loads((self.fx["receipts"] / f"local-delegate-{doc['invocation_id']}.json").read_text())
        self.assertEqual(receipt["output_sha256"], hashlib.sha256(raw.encode()).hexdigest())
        self.assertEqual((self.fx["install"] / "calls.txt").read_text(), "1")

    def test_extract_retries_invalid_draft_once_with_exact_source_and_options(self):
        for invalid in ("not JSON", "{}", self.extract_json(party="Invented")):
            with self.subTest(invalid=invalid):
                (self.fx["install"] / "calls.txt").unlink(missing_ok=True)
                self.output_sequence(invalid, self.extract_json())
                with mock.patch.object(gemma_child, "_invoke_validated",
                                       wraps=gemma_child._invoke_validated) as calls:
                    result = self.invoke("manifest", task_type="extract",
                        params={"documents": [{"id": "one", "text": "Alpha invoice"}]})
                self.assertEqual(calls.call_count, 2)
                first, second = calls.call_args_list
                self.assertEqual(first.args, second.args)
                self.assertEqual(first.args[0], "Alpha invoice")
                self.assertLessEqual(second.kwargs["capture_timeout"], first.kwargs["capture_timeout"])
                self.assertEqual(result["output"]["documents"][0]["record_status"], "ok")

    def test_extract_does_not_salvage_prose_truncation_or_invalid_fields(self):
        valid = self.extract_json()
        cases = [("Here are records:\n" + valid, "json_invalid"),
                 (valid[:-1], "json_invalid"),
                 ("```json\n" + valid + "\n```\nextra", "json_invalid"),
                 ('[{"party":"Alpha"}]', "schema_invalid"),
                 (self.extract_json(party="Absent"), "value_absent")]
        for output, reason in cases:
            with self.subTest(reason=reason, output=output):
                (self.fx["install"] / "calls.txt").unlink(missing_ok=True)
                self.output_sequence(output)
                with self.assertRaisesRegex(gemma_child.GemmaRefusal, f"^extract_document_0_{reason}$"):
                    self.invoke("manifest", task_type="extract",
                        params={"documents": [{"id": "one", "text": "Alpha invoice"}]})
                self.assertEqual((self.fx["install"] / "calls.txt").read_text(), "2")

    def test_extract_retry_preserves_document_association_without_repeating_completed_document(self):
        self.output_sequence(self.extract_json(), "bad JSON", self.extract_json(party="Beta"))
        documents = [{"id": "first", "text": "Alpha invoice"},
                     {"id": "second", "text": "Beta invoice"}]
        with mock.patch.object(gemma_child, "_invoke_validated",
                               wraps=gemma_child._invoke_validated) as calls:
            result = self.invoke("manifest", task_type="extract", params={"documents": documents})
        self.assertEqual([call.args[0] for call in calls.call_args_list],
                         ["Alpha invoice", "Beta invoice", "Beta invoice"])
        self.assertEqual([call.args[1] for call in calls.call_args_list], ["job-1:0", "job-1:1", "job-1:1"])
        self.assertEqual([doc["id"] for doc in result["output"]["documents"]], ["first", "second"])
        self.assertEqual([doc["records"][0]["fields"]["party"]["value"]
                          for doc in result["output"]["documents"]], ["Alpha", "Beta"])

    def test_extract_receipt_failure_is_not_repaired_or_retried(self):
        self.output_sequence("```json\n" + self.extract_json() + "\n```")
        self.override({"output_sha256": "0" * 64})
        with self.assertRaisesRegex(gemma_child.GemmaRefusal, "^extract_document_0_failed:receipt_invalid$"):
            self.invoke("manifest", task_type="extract",
                        params={"documents": [{"id": "one", "text": "Alpha invoice"}]})
        self.assertEqual((self.fx["install"] / "calls.txt").read_text(), "1")

    def test_blank_summary_retries_once_and_returns_second_bound_draft(self):
        self.output_sequence(" \n", "Alpha completed the chore.")
        with mock.patch.object(gemma_child, "_invoke_validated",
                               wraps=gemma_child._invoke_validated) as calls:
            result = self.invoke("Alpha source\n")
        first, second = calls.call_args_list
        self.assertEqual(first.args, second.args)
        self.assertEqual(first.args[0], "Alpha source\n")
        self.assertLessEqual(second.kwargs["capture_timeout"], first.kwargs["capture_timeout"])
        self.assertEqual(result["output"]["text"], "Alpha completed the chore.")
        self.assertEqual(len(list(self.fx["receipts"].glob("local-delegate-*.json"))), 2)
        receipt = json.loads((self.fx["receipts"] / f"local-delegate-{result['invocation_id']}.json").read_text())
        self.assertEqual(result["output_sha256"], receipt["output_sha256"])

    def test_blank_summary_cannot_be_a_successful_draft(self):
        self.output_sequence("\n\t ")
        with self.assertRaisesRegex(gemma_child.GemmaRefusal, "^summary_empty_output$"):
            self.invoke()
        self.assertEqual((self.fx["install"] / "calls.txt").read_text(), "2")

    def test_output_retry_never_restarts_original_deadline(self):
        for task in ("summarize", "extract"):
            with self.subTest(task=task):
                reason = "gemma_delegate_timeout" if task == "summarize" else "extract_document_0_deadline_exceeded"
                draft = {"output": {"text": " " if task == "summarize" else "bad JSON"}}
                with mock.patch.object(gemma_child.time, "monotonic", side_effect=[0, 0, 5]), \
                     mock.patch.object(gemma_child, "_invoke_validated", return_value=draft) as calls, \
                     self.assertRaisesRegex(gemma_child.GemmaRefusal, f"^{reason}$"):
                    self.invoke(task_type=task, params={"documents": [{"id": "one", "text": "Alpha"}]}
                                if task == "extract" else {})
                self.assertEqual(calls.call_count, 1)

    def test_raw_stdin_plain_stdout_and_full_bindings(self):
        text = "raw café → 世界\n"
        result = self.invoke(text)
        self.assertEqual((self.fx["install"] / "observed-stdin.bin").read_bytes(), text.encode())
        self.assertEqual(result["output"]["text"], "SUMMARY: " + text)
        self.assertEqual(result["model_digest"], MODEL_DIGEST)
        self.assertEqual(result["receipt_contract"], "local-delegate/v2")
        self.assertEqual(result["input_sha256"], hashlib.sha256(text.encode()).hexdigest())
        exact = self.fx["receipts"] / f"local-delegate-{result['invocation_id']}.json"
        self.assertTrue(exact.is_file())

    def test_chatty_delegate_is_capped_before_receipt_validation(self):
        self.mode("chatty")
        with mock.patch.object(gemma_child, "GEMMA_DELEGATE_CAPTURE_MAX_BYTES", 1024):
            with self.assertRaisesRegex(gemma_child.GemmaRefusal,
                                        "gemma_delegate_output_overflow"):
                self.invoke()
        # The capped draft is never left at rest beside the receipts.
        self.assertEqual(list(self.fx["receipts"].glob(".delegate-*")), [])

    def test_delegate_stderr_does_not_compete_with_bounded_stdout(self):
        self.mode("stderr-chatty")
        self.assertEqual(self.invoke()["provider"], "gemma_certified")

    def test_successful_delegate_leaves_no_captured_draft_beside_receipts(self):
        self.invoke()
        self.assertEqual(list(self.fx["receipts"].glob(".delegate-*")), [])

    def test_installed_sibling_import_succeeds_without_import_pollution(self):
        sibling = self.fx["install"] / "state_home.py"
        sibling.write_text("MARKER = 'configured-install'\n", encoding="utf-8")
        validator = self.fx["validator"]
        validator.write_text(
            validator.read_text(encoding="utf-8") +
            "\nimport state_home\n"
            "if state_home.MARKER != 'configured-install':\n"
            "    raise ValueError('wrong sibling')\n", encoding="utf-8")
        prior_path = sys.path[:]
        prior = sys.modules.get("state_home")
        unrelated = types.ModuleType("state_home")
        unrelated.MARKER = "unrelated"
        sys.modules["state_home"] = unrelated
        try:
            result = self.invoke()
            self.assertEqual(result["provider"], "gemma_certified")
            self.assertIs(sys.modules["state_home"], unrelated)
            self.assertEqual(sys.path, prior_path)
        finally:
            if prior is None:
                sys.modules.pop("state_home", None)
            else:
                sys.modules["state_home"] = prior

    def test_missing_sibling_refuses_before_delegate_even_if_cached(self):
        validator = self.fx["validator"]
        validator.write_text(validator.read_text(encoding="utf-8") +
                             "\nimport missing_installed_dependency\n", encoding="utf-8")
        decoy = self.root / "unrelated"
        decoy.mkdir()
        (decoy / "missing_installed_dependency.py").write_text(
            "MARKER = 'wrong path'\n", encoding="utf-8")
        prior_path = sys.path[:]
        cached = types.ModuleType("missing_installed_dependency")
        sys.modules["missing_installed_dependency"] = cached
        sys.path.insert(0, str(decoy))
        try:
            with self.assertRaisesRegex(gemma_child.GemmaReceiptError,
                                        "receipt_validator_unloadable"):
                self.invoke()
            self.assertFalse((self.fx["install"] / "calls.txt").exists())
            self.assertIs(sys.modules["missing_installed_dependency"], cached)
            self.assertEqual(sys.path, [str(decoy)] + prior_path)
        finally:
            sys.modules.pop("missing_installed_dependency", None)
            sys.path[:] = prior_path

    def test_malformed_receipt_rejected_with_sibling_validator(self):
        (self.fx["install"] / "state_home.py").write_text("MARKER = True\n", encoding="utf-8")
        validator = self.fx["validator"]
        validator.write_text(validator.read_text(encoding="utf-8") +
                             "\nimport state_home\n", encoding="utf-8")
        self.mode("malformed")
        with self.assertRaisesRegex(gemma_child.GemmaReceiptError, "receipt_invalid"):
            self.invoke()
        self.assertNotIn("state_home", sys.modules)

    def test_unsupported_kind_custom_instruction_and_provider_refuse_before_spawn(self):
        with self.assertRaisesRegex(ValueError, "params_invalid_for_gemma_certified"):
            self.invoke(task_type="extract")
        with self.assertRaisesRegex(gemma_child.GemmaRefusal, "custom_instruction_unsupported"):
            self.invoke(params={"instruction": "invent facts"})
        with self.assertRaisesRegex(ValueError, "params_invalid_for_gemma_certified"):
            self.invoke(params={"provider": "qwen"})
        self.assertFalse((self.fx["install"] / "calls.txt").exists())

    def test_fixed_certified_instruction_is_accepted(self):
        result = self.invoke(params={"instruction": gemma_child.CERTIFIED_SUMMARIZE_INSTRUCTION})
        self.assertEqual(result["provider"], "gemma_certified")

    def test_extract_is_per_document_and_keeps_record_status(self):
        documents = [{"id": "one", "text": "Alpha invoice"}]
        self.fx["install"].joinpath("extract_records.json").write_text(json.dumps([
            {"party": "Alpha", "date": None, "doc_type": None, "reference": None,
             "description": None, "amount": None, "terms": None, "due_date": None,
             "account": None}]), encoding="utf-8")
        source = json.dumps(documents, separators=(",", ":"))
        result = self.invoke(source, task_type="extract", params={"documents": documents})
        output = result["output"]
        self.assertEqual(output["documents"][0]["record_status"], "ok")
        self.assertEqual(output["documents"][0]["records"][0]["fields"]["party"]["spans"], [[0, 5]])

    def test_extract_passes_fixed_certified_job_timeout(self):
        # The job timeout is part of the route certificate binding. A computed
        # remainder (e.g. 4.9987) would miss the certificate on every call.
        documents = [{"id": "one", "text": "Alpha invoice"}, {"id": "two", "text": "Beta invoice"}]
        source = json.dumps(documents, separators=(",", ":"))
        real = gemma_child._invoke_validated
        seen = []

        def record(*args, **kwargs):
            result = real(*args, **kwargs)
            argv = json.loads((self.fx["install"] / "observed-argv.json").read_text())
            seen.append((args[6], kwargs.get("capture_timeout"),
                         argv[argv.index("--job-timeout") + 1]))
            return result

        with mock.patch.object(gemma_child, "_invoke_validated", side_effect=record):
            self.invoke(source, task_type="extract", params={"documents": documents})
        self.assertEqual(len(seen), 2)
        for timeout, capture_timeout, argv_timeout in seen:
            self.assertEqual(timeout, 5)
            self.assertEqual(argv_timeout, "5.0")
            self.assertIsNotNone(capture_timeout)
            self.assertGreater(capture_timeout, 0)
            self.assertLessEqual(capture_timeout, 5)
        # The second call waits only on what is left of the batch deadline.
        self.assertLess(seen[1][1], seen[0][1])

    def test_noisy_stderr_is_bounded_and_refusal_still_found(self):
        self.mode("cert-refused-noisy")
        documents = [{"id": "one", "text": "Alpha invoice"}]
        source = json.dumps(documents, separators=(",", ":"))
        with mock.patch.object(gemma_child.tempfile, "TemporaryFile",
                               wraps=gemma_child.tempfile.TemporaryFile) as temp:
            with self.assertRaises(gemma_child.GemmaRefusal) as caught:
                self.invoke(source, task_type="extract", params={"documents": documents})
        self.assertEqual(
            str(caught.exception),
            "extract_document_0_failed:gemma_certification_refused_matching_certificate_missing")
        # Only the stdout draft file is disk-backed; stderr never is.
        self.assertEqual(temp.call_count, 1)

    def test_refusal_marker_outside_tail_or_followed_by_prose_is_generic(self):
        for mode in ("cert-refused-early", "cert-refused-trailing-prose"):
            with self.subTest(mode=mode):
                self.mode(mode)
                with self.assertRaisesRegex(gemma_child.GemmaRefusal, "^gemma_delegate_unavailable$"):
                    self.invoke()

    def test_last_certification_refusal_wins(self):
        self.mode("cert-refused-twice")
        with self.assertRaisesRegex(gemma_child.GemmaRefusal,
                                    "^gemma_certification_refused_second_code$"):
            self.invoke()

    def test_certification_refusal_code_is_surfaced_without_stderr_content(self):
        self.mode("cert-refused")
        documents = [{"id": "one", "text": "Alpha invoice"}]
        source = json.dumps(documents, separators=(",", ":"))
        with self.assertRaises(gemma_child.GemmaRefusal) as caught:
            self.invoke(source, task_type="extract", params={"documents": documents})
        self.assertEqual(
            str(caught.exception),
            "extract_document_0_failed:gemma_certification_refused_matching_certificate_missing")
        self.assertNotIn("Alpha", str(caught.exception))

    def test_unpatterned_certification_refusal_stays_generic(self):
        self.mode("cert-refused-prose")
        with self.assertRaisesRegex(gemma_child.GemmaRefusal, "^gemma_delegate_unavailable$"):
            self.invoke()

    def test_classify_preserves_unsure_and_blank_lines(self):
        result = self.invoke("a\n\nb", task_type="classify", params={})
        self.assertEqual([row["label"] for row in result["output"]["lines"]],
                         ["UNSURE", "UNSURE", "UNSURE"])

    def test_log_triage_uses_triage_argv_raw_input_and_receipt_bindings(self):
        source = "[A1] Synthetic café warning\r\n\r\n[B2] Synthetic debug\r\n"
        candidate = "ACT:\n- [B2] Check debug\nWATCH:\n- [A1] Check warning\nNOISE:\n"
        # Write bytes: write_text would translate newlines to CRLF on Windows.
        (self.fx["install"] / "triage_output.txt").write_bytes(candidate.encode("utf-8"))
        result = self.invoke(source, task_type="log_triage")
        self.assertEqual(result["task"], "log_triage")
        self.assertEqual(result["output"], {"task": "log_triage", "text": candidate})
        self.assertEqual(result["input_sha256"], hashlib.sha256(source.encode()).hexdigest())
        self.assertEqual(result["output_sha256"], hashlib.sha256(candidate.encode()).hexdigest())
        self.assertEqual((self.fx["install"] / "observed-stdin.bin").read_bytes(), source.encode())
        argv = json.loads((self.fx["install"] / "observed-argv.json").read_text())
        self.assertEqual(argv, ["--task", "triage", "--invocation-id", result["invocation_id"],
                                "--parent-task-id", "job-1", "--job-timeout", "5.0"])
        receipt = json.loads((self.fx["receipts"] / f"local-delegate-{result['invocation_id']}.json").read_text())
        self.assertEqual(receipt["task"], "triage")

    def test_log_triage_bad_input_and_nonempty_params_never_spawn(self):
        for source in ("no identity", "[A1] First\n[A1] Duplicate", "[] Empty id",
                       "[broken Missing bracket", "[[nested]] Bad", "[two words] Bad",
                       " [A1] Bad", "[A1]", "[A1]   ", "\n \t\n",
                       "[A1] First\nmissing continuation", "[alpha] Bad", "[A12345] Bad",
                       "[ABCDEFG1] Bad", "[Á1] Bad", "[A1] Contains [B2] token"):
            with self.subTest(source=source):
                with self.assertRaisesRegex(ValueError, "^log_triage_input_invalid$"):
                    self.invoke(source, task_type="log_triage")
        for params in ({"instruction": ""}, {"documents": []}, {"provider": "auto"}):
            with self.subTest(params=params):
                with self.assertRaisesRegex(ValueError, "^params_invalid_for_gemma_certified$"):
                    self.invoke("[A1] Synthetic", task_type="log_triage", params=params)
        self.assertFalse((self.fx["install"] / "calls.txt").exists())

    def test_log_triage_default_output_matches_delegate_empty_buckets(self):
        source = "[L1] Synthetic warning\n\n[AbCdEf1234] Synthetic debug\n"
        result = self.invoke(source, task_type="log_triage")
        self.assertEqual(result["output"], {"task": "log_triage", "text":
            "ACT\n- none\n\nWATCH\n- none\n\nNOISE\n"
            "- [L1] Synthetic warning\n- [AbCdEf1234] Synthetic debug\n"})

    def test_log_triage_rejects_bad_output_even_with_valid_success_receipt(self):
        for candidate in (
                "ACT\n- [A1] First\nWATCH\nNOISE\n",  # missing two
                "ACT\n- [A1] First\nWATCH\n- [A1] Duplicate\n- [B2] Second\nNOISE\n",
                "ACT\n- [A1] First\n- [B2] Second\nWATCH\nNOISE\n- [Z9] Extra\n",
                "ACT\n- [A1] First\n- [B2] Second\nWATCH\n",  # missing section
                "ACT\n- [A1] First\nWATCH\n- [B2] Second\nNOISE\nNOISE\n",
                "- [A1] First\nACT\nWATCH\n- [B2] Second\nNOISE\n",
                "ACT\n[A1] Malformed\nWATCH\n- [B2] Second\nNOISE\n",
                "ACT\n- [A1] First\nWATCH\n- [B2] Second\nNOISE\nextra prose",
                "incomplete"):
            with self.subTest(candidate=candidate):
                (self.fx["install"] / "triage_output.txt").write_text(candidate, encoding="utf-8")
                with self.assertRaisesRegex(gemma_child.GemmaRefusal, "^log_triage_output_invalid$"):
                    self.invoke("[A1] Synthetic first\n[B2] Synthetic second", task_type="log_triage")

    def test_log_triage_receipt_task_must_be_delegate_triage(self):
        for field, value, reason in (("task", "log_triage", "receipt_task_mismatch"),
                                     ("result_status", "incomplete", "receipt_invalid"),
                                     ("input_sha256", "0" * 64, "receipt_invalid")):
            with self.subTest(field=field):
                self.override({field: value})
                with self.assertRaisesRegex(gemma_child.GemmaReceiptError, reason):
                    self.invoke("[A1] Synthetic", task_type="log_triage")
                (self.fx["install"] / "receipt_override.json").unlink()

    # Expected options come from the installed delegate's own derivation
    # (local-delegate delegate.py): schema is on for extract, and triage keeps
    # two repair retries unless --no-repair, which this adapter never passes.
    DELEGATE_OPTIONS = {
        "summarize": {"schema": False, "repair_retries": 0},
        "extract": {"schema": True, "repair_retries": 0},
        "classify": {"schema": False, "repair_retries": 0},
        "triage": {"schema": False, "repair_retries": 2},
    }

    def _full_options(self, delegate_task: str, timeout: float = 5.0) -> dict:
        return {"chunk_chars": 24000, "job_timeout": float(timeout), "think": False,
                "second_pass": False, "map_scope": "run", "roster_sha256": None,
                **self.DELEGATE_OPTIONS[delegate_task]}

    def test_expected_options_match_delegate_derivation_per_task(self):
        for task, delegate_task in (("summarize", "summarize"), ("extract", "extract"),
                                    ("classify", "classify"), ("log_triage", "triage")):
            with self.subTest(task=task):
                self.assertEqual(gemma_child._effective_options(5, delegate_task),
                                 self._full_options(delegate_task))

    def test_extract_receipt_binds_schema_on(self):
        documents = [{"id": "one", "text": "Alpha invoice"}]
        source = json.dumps(documents, separators=(",", ":"))
        self.invoke(source, task_type="extract", params={"documents": documents})
        receipts = list(self.fx["receipts"].glob("local-delegate-*.json"))
        self.assertEqual(len(receipts), 1)
        options = json.loads(receipts[0].read_text())["effective_options"]
        self.assertEqual(options, self._full_options("extract"))

    def test_receipt_options_that_differ_from_delegate_derivation_are_refused(self):
        documents = [{"id": "one", "text": "Alpha invoice"}]
        source = json.dumps(documents, separators=(",", ":"))
        cases = (
            ("extract", {**self._full_options("extract"), "schema": False},
             lambda: self.invoke(source, task_type="extract", params={"documents": documents}),
             gemma_child.GemmaRefusal, "^extract_document_0_failed:receipt_invalid$"),
            ("triage", {**self._full_options("triage"), "repair_retries": 0},
             lambda: self.invoke("[A1] Synthetic", task_type="log_triage"),
             gemma_child.GemmaReceiptError, "receipt_invalid"),
            ("summarize", {**self._full_options("summarize"), "schema": True},
             lambda: self.invoke("Synthetic text"),
             gemma_child.GemmaReceiptError, "receipt_invalid"),
        )
        for name, options, call, error, reason in cases:
            with self.subTest(case=name):
                self.override({"effective_options": options})
                with self.assertRaisesRegex(error, reason):
                    call()
                (self.fx["install"] / "receipt_override.json").unlink()

    def test_changed_delegate_or_validator_source_refuses_before_spawn(self):
        delegate_digest = file_sha256(self.fx["delegate"])
        validator_digest = file_sha256(self.fx["validator"])
        with self.fx["delegate"].open("a", encoding="utf-8") as handle:
            handle.write("\n# changed after configuration\n")
        with self.assertRaisesRegex(gemma_child.GemmaRefusal, "delegate_source_changed"):
            gemma_child.invoke(
                {"task_type": "summarize", "input": "synthetic", "params": {}, "job_id": "j"},
                delegate=str(self.fx["delegate"]), python=REAL_PYTHON,
                receipt_root=str(self.fx["receipts"]),
                receipt_validator=str(self.fx["validator"]),
                delegate_sha256=delegate_digest, validator_sha256=validator_digest,
                model_digest=MODEL_DIGEST, timeout=5)
        self.assertFalse((self.fx["install"] / "calls.txt").exists())

    def test_nonzero_delegate_is_final_no_retry(self):
        self.mode("unavailable")
        with self.assertRaisesRegex(gemma_child.GemmaRefusal, "gemma_delegate_unavailable"):
            self.invoke()
        self.assertEqual((self.fx["install"] / "calls.txt").read_text(), "1")

    def test_missing_malformed_and_duplicate_receipts_refuse(self):
        for mode in ("missing-receipt", "malformed", "duplicate-key"):
            with self.subTest(mode=mode):
                self.mode(mode)
                with self.assertRaises(gemma_child.GemmaReceiptError):
                    self.invoke()

    def test_every_binding_mismatch_refuses(self):
        wrong = {
            "invocation_id": "0" * 32, "input_sha256": "0" * 64,
            "canonical_input_sha256": "0" * 64,
            "effective_options": {}, "parent_task_id": "0" * 64,
            "attempt_id": "0" * 64, "output_sha256": "0" * 64,
        }
        for field, value in wrong.items():
            with self.subTest(field=field):
                self.override({field: value})
                with self.assertRaisesRegex(gemma_child.GemmaReceiptError, "receipt_invalid"):
                    self.invoke()
                (self.fx["install"] / "receipt_override.json").unlink()

    def test_contract_task_status_digest_and_hash_kind_are_pinned(self):
        cases = {
            "contract": "local-delegate/v1", "task": "extract",
            "result_status": "failed", "model_digest": "cd" * 32,
            "output_hash_kind": "response_bodies",
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                self.override({field: value})
                with self.assertRaises(gemma_child.GemmaReceiptError):
                    self.invoke()
                (self.fx["install"] / "receipt_override.json").unlink()


class ValidatorLoaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def validator(self, sibling: str) -> Path:
        path = self.root / "validator.py"
        path.write_text(
            f"import {sibling}\n"
            "def read_success(*args, **kwargs):\n"
            f"    return {{'source': {sibling}.SOURCE}}\n", encoding="utf-8")
        return path

    def test_installed_sibling_import_succeeds_without_import_pollution(self):
        sibling = "gemma_validator_sibling"
        (self.root / f"{sibling}.py").write_text("SOURCE = 'installed'\n", encoding="utf-8")
        with gemma_child._load_validator(self.validator(sibling)) as validator:
            self.assertEqual(validator.read_success()["source"], "installed")
            self.assertEqual(sys.modules[sibling].SOURCE, "installed")
        self.assertNotIn(sibling, sys.modules)

    def test_installed_sibling_does_not_replace_a_preexisting_module(self):
        sibling = "gemma_validator_sibling_saved"
        (self.root / f"{sibling}.py").write_text("SOURCE = 'installed'\n", encoding="utf-8")
        prior = types.ModuleType(sibling)
        prior.SOURCE = "prior"
        sys.modules[sibling] = prior
        self.addCleanup(sys.modules.pop, sibling, None)
        with gemma_child._load_validator(self.validator(sibling)) as validator:
            self.assertEqual(validator.read_success()["source"], "installed")
            self.assertIsNot(sys.modules[sibling], prior)
        self.assertIs(sys.modules[sibling], prior)


class QueueIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fx = fixture(self.root)

    def command(self, timeout: float = 5.0) -> list[str]:
        return [REAL_PYTHON, str(Path(gemma_child.__file__).resolve()),
                "--delegate", str(self.fx["delegate"]), "--python", REAL_PYTHON,
                "--receipt-root", str(self.fx["receipts"]),
                "--receipt-validator", str(self.fx["validator"]),
                "--delegate-sha256", file_sha256(self.fx["delegate"]),
                "--validator-sha256", file_sha256(self.fx["validator"]),
                "--model-digest", MODEL_DIGEST, "--timeout", str(timeout)]

    def queue(self, timeout: float = 10.0) -> LocalQueue:
        return LocalQueue(self.root, sampler=Sampler(),
                          backend=SubprocessBackend(self.command()),
                          caps=QueueCaps(timeout_seconds=timeout),
                          allowed_task_types=frozenset({"summarize"}))

    def test_queue_success_and_unsupported_kind_refusal(self):
        queue = self.queue()
        with self.assertRaisesRegex(Exception, UNSUPPORTED_KIND_REASON):
            queue.submit(task_type="extract", input="x" * 1000, params={},
                         priority="interactive", classification="synthetic",
                         caller="codex", purpose="work")
        job = queue.submit(task_type="summarize", input="synthetic text " * 20, params={},
                           priority="interactive", classification="synthetic",
                           caller="codex", purpose="work")
        terminal = queue.run_once("runner")
        self.assertEqual(terminal["job_id"], job["job_id"])
        self.assertEqual(terminal["status"], "complete")
        self.assertEqual(terminal["result"]["provider"], "gemma_certified")

    def test_log_triage_bad_output_fails_job_with_fixed_reason(self):
        (self.fx["install"] / "triage_output.txt").write_text(
            "ACT\n- [A1] Synthetic first\nWATCH\nNOISE\n", encoding="utf-8")
        queue = LocalQueue(self.root, sampler=Sampler(),
                           backend=SubprocessBackend(self.command()), backend_id="gemma_certified",
                           allowed_task_types=frozenset({"summarize", "log_triage"}))
        queue.submit(task_type="log_triage", input="[A1] Synthetic first\n[B2] Private second",
                     params={}, priority="interactive", classification="synthetic",
                     caller="codex", purpose="work")
        terminal = queue.run_once("runner")
        self.assertEqual(terminal["status"], "failed", terminal)
        self.assertIn("log_triage_output_invalid", terminal["error"])
        self.assertNotIn("Private second", terminal["error"])

    @unittest.skipUnless(os.name == "posix", "process-group termination is POSIX-only")
    def test_outer_timeout_kills_adapter_and_delegate_group(self):
        (self.fx["install"] / "mode.txt").write_text("slow", encoding="utf-8")
        (self.fx["install"] / "sleep_seconds.txt").write_text("2", encoding="utf-8")
        queue = LocalQueue(self.root, sampler=Sampler(),
                           backend=SubprocessBackend(self.command(timeout=30)),
                           caps=QueueCaps(timeout_seconds=0.2),
                           allowed_task_types=frozenset({"summarize"}))
        queue.submit(task_type="summarize", input="synthetic text " * 20, params={},
                     priority="interactive", classification="synthetic",
                     caller="codex", purpose="work")
        terminal = queue.run_once("runner")
        self.assertEqual(terminal["status"], "unknown")
        time.sleep(2.2)
        self.assertEqual(list(self.fx["receipts"].glob("*.json")), [])


class SourceSafetyTests(unittest.TestCase):
    def test_no_other_model_or_fallback_in_gemma_adapter(self):
        source = inspect.getsource(gemma_child).lower()
        for banned in ("qwen", "apple", "cloud fallback"):
            self.assertNotIn(banned, source)
        self.assertNotIn("worker_child", source)
        self.assertNotIn("start_new_session", source)


if __name__ == "__main__":
    unittest.main()
