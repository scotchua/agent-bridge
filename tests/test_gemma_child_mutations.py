"""Offline boundary regressions for the certified Gemma child adapter."""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.localq import gemma_child


class ExistingFileTests(unittest.TestCase):
    def test_relative_and_missing_paths_are_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            for path in ("relative-delegate.py", str(Path(temp) / "missing.py")):
                with self.subTest(path=path):
                    with self.assertRaisesRegex(ValueError, "delegate must be"):
                        gemma_child._existing_file(path, "delegate")


class ExistingDirectoryTests(unittest.TestCase):
    def test_relative_and_missing_directories_are_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            for path in (".", str(Path(temp) / "missing")):
                with self.subTest(path=path):
                    with self.assertRaisesRegex(ValueError, "receipt_root must be"):
                        gemma_child._existing_dir(path, "receipt_root")


class DigestShapeTests(unittest.TestCase):
    def test_only_lowercase_hex_digests_of_exact_length_are_accepted(self):
        self.assertTrue(gemma_child._hex64("a" * 64))
        for invalid in ("a" * 63, "g" * 64, 12):
            with self.subTest(invalid=invalid):
                self.assertFalse(gemma_child._hex64(invalid))


class ValidatorLoaderTests(unittest.TestCase):
    def test_absent_import_spec_has_fixed_receipt_error(self):
        with mock.patch.object(gemma_child.importlib.util, "spec_from_file_location", return_value=None):
            with self.assertRaisesRegex(gemma_child.GemmaReceiptError,
                                        "receipt_validator_unloadable"):
                with gemma_child._load_validator(Path("/synthetic/validator.py")):
                    self.fail("validator with no import spec was loaded")


class InvokePreflightTests(unittest.TestCase):
    def invoke(self, payload):
        return gemma_child.invoke(
            payload, delegate="/synthetic/delegate.py", python="/synthetic/python",
            receipt_root="/synthetic/receipts", receipt_validator="/synthetic/validator.py",
            model_digest="a" * 64, delegate_sha256="b" * 64,
            validator_sha256="c" * 64)

    def test_empty_job_id_is_refused_before_file_access(self):
        with self.assertRaisesRegex(ValueError, "job_id_invalid"):
            self.invoke({"task_type": "summarize", "input": "Aurora invoice",
                         "params": {}, "job_id": ""})

    def test_unknown_task_has_fixed_reason_before_file_access(self):
        with self.assertRaisesRegex(ValueError, "unsupported_kind"):
            self.invoke({"task_type": "invent", "input": "Aurora invoice",
                         "params": {}, "job_id": "job-synthetic"})


class DocumentParameterTests(unittest.TestCase):
    def test_exact_document_limit_is_valid(self):
        documents = [{"id": f"doc-{index}", "text": "Aurora invoice"}
                     for index in range(gemma_child.MAX_EXTRACT_DOCUMENTS)]
        self.assertEqual(gemma_child._document_params(documents), documents)


class SpanTests(unittest.TestCase):
    def test_repeated_value_has_every_occurrence(self):
        self.assertEqual(gemma_child._spans("Aurora Aurora", "Aurora"), [[0, 6], [7, 13]])


class ExtractDeadlineTests(unittest.TestCase):
    def test_zero_remaining_budget_refuses_before_delegate(self):
        documents = [{"id": "invoice-1", "text": "Aurora invoice"}]
        with mock.patch.object(gemma_child.time, "monotonic", side_effect=[0.0, 1.0]), \
             mock.patch.object(gemma_child, "_invoke_validated") as delegate:
            with self.assertRaisesRegex(gemma_child.GemmaRefusal,
                                        "extract_document_0_deadline_exceeded"):
                gemma_child._extract("manifest", documents, "job-synthetic", Path("/delegate"),
                                     Path("/python"), Path("/receipts"), "a" * 64, 1.0, object())
            delegate.assert_not_called()


class MainTests(unittest.TestCase):
    def arguments(self):
        return ["--delegate", "/synthetic/delegate.py", "--python", "/synthetic/python",
                "--receipt-root", "/synthetic/receipts", "--receipt-validator",
                "/synthetic/validator.py", "--delegate-sha256", "a" * 64,
                "--validator-sha256", "b" * 64, "--model-digest", "c" * 64]

    def test_non_object_payload_is_reported_as_invalid(self):
        output = io.StringIO()
        with mock.patch.object(gemma_child.sys, "stdin", io.StringIO("[]")), \
             mock.patch.object(gemma_child, "invoke") as invoke, \
             contextlib.redirect_stdout(output):
            status = gemma_child.main(self.arguments())
        self.assertEqual(status, 1)
        self.assertEqual(output.getvalue().strip(),
                         '{"error": "ValueError", "error_detail": "payload_invalid", "ok": false}')
        invoke.assert_not_called()

    def test_success_returns_zero(self):
        output = io.StringIO()
        with mock.patch.object(gemma_child.sys, "stdin", io.StringIO("{}")), \
             mock.patch.object(gemma_child, "invoke", return_value={"task": "summarize"}), \
             contextlib.redirect_stdout(output):
            status = gemma_child.main(self.arguments())
        self.assertEqual(status, 0)
        self.assertEqual(output.getvalue().strip(), '{"task": "summarize"}')


if __name__ == "__main__":
    unittest.main()
