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
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.localq import backend_select, gemma_child, worker_child
from agent_bridge.localq.intake import AutomaticIntake, IntakePolicy
from agent_bridge.localq.service import Service
from agent_bridge.localq.spool import (FakeBackend, LocalQueue, QueueCaps,
                                       ResourceSnapshot, SubprocessBackend,
                                       UNSUPPORTED_KIND_REASON)
from agent_bridge.orchestration.config import OrchestrationConfigError, load
from agent_bridge.orchestration import delegation_verify, localfirst


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

    def test_selected_backend_is_fixed_and_summarize_only(self):
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

    @unittest.skipUnless(os.name == "posix", "certified Gemma backend requires POSIX process groups")
    def test_gemma_calibration_uses_only_certified_params_and_runs_every_sample(self):
        config_path = self.write(config_doc(self.root, self.fx))
        result = delegation_verify.calibrate(str(config_path), sampler=Sampler())
        self.assertTrue(result["ok"], result)
        expected = len(localfirst.CALIBRATION_SIZES) * localfirst.CALIBRATION_RUNS_PER_SIZE
        self.assertEqual(int((self.fx["install"] / "calls.txt").read_text()), expected)
        samples = [delegation_verify._synthetic_calibration_input(8000, index)
                   for index in range(localfirst.CALIBRATION_RUNS_PER_SIZE)]
        self.assertEqual({len(sample.encode("utf-8")) for sample in samples}, {8000})
        self.assertEqual(len(set(samples)), localfirst.CALIBRATION_RUNS_PER_SIZE)


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

    def test_unsupported_kind_custom_instruction_and_provider_refuse_before_spawn(self):
        with self.assertRaisesRegex(ValueError, UNSUPPORTED_KIND_REASON):
            self.invoke(task_type="extract")
        with self.assertRaisesRegex(gemma_child.GemmaRefusal, "custom_instruction_unsupported"):
            self.invoke(params={"instruction": "invent facts"})
        with self.assertRaisesRegex(ValueError, "params_invalid_for_gemma_certified"):
            self.invoke(params={"provider": "qwen"})
        self.assertFalse((self.fx["install"] / "calls.txt").exists())

    def test_fixed_certified_instruction_is_accepted(self):
        result = self.invoke(params={"instruction": gemma_child.CERTIFIED_SUMMARIZE_INSTRUCTION})
        self.assertEqual(result["provider"], "gemma_certified")

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
