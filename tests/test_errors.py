"""Offline contracts for the closed error vocabulary."""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from agent_bridge import errors
from agent_bridge.errors import (BrokerError, ErrorCategory, category_from_status,
                                 hint, is_retryable)


class CategoryFromStatusTests(unittest.TestCase):
    def test_diagnostic_requires_matching_base_and_code(self):
        diagnostic = ErrorCategory.PEER_STRUCTURED_OUTPUT_EXHAUSTED.value
        schema = ErrorCategory.PEER_OUTPUT_SCHEMA_INVALID.value
        self.assertEqual(category_from_status({"error_category": schema,
                                               "diagnostic_category": diagnostic}),
                         ErrorCategory.PEER_STRUCTURED_OUTPUT_EXHAUSTED)
        for status, expected in [
            ({"error_category": schema, "diagnostic_category": "other"},
             ErrorCategory.PEER_OUTPUT_SCHEMA_INVALID),
            ({"error_category": ErrorCategory.PEER_TIMEOUT.value,
              "diagnostic_category": diagnostic}, ErrorCategory.PEER_TIMEOUT),
        ]:
            with self.subTest(status=status):
                self.assertEqual(category_from_status(status), expected)

    def test_known_missing_and_unknown_categories(self):
        self.assertEqual(category_from_status({"error_category": "peer_nonzero_exit"}),
                         ErrorCategory.PEER_NONZERO_EXIT)
        for status in ({}, {"error_category": ""},
                       {"error_category": "invented_category"}):
            with self.subTest(status=status):
                self.assertEqual(category_from_status(status), ErrorCategory.INTERNAL_ERROR)


class HintTests(unittest.TestCase):
    def test_known_hints_are_closed_and_unknown_is_empty(self):
        self.assertIn("temporary directory", hint(ErrorCategory.PREFLIGHT_EXECUTABLE_MISSING))
        self.assertEqual(hint(ErrorCategory.PEER_NONZERO_EXIT), "Peer process exited nonzero.")
        self.assertTrue(all(hint(category) for category in ErrorCategory
                            if category is not ErrorCategory.OK))
        self.assertEqual(hint(ErrorCategory.OK), "")
        self.assertEqual(hint("invented_category"), "")


class RetryTests(unittest.TestCase):
    def test_retry_classes_have_distinct_decisions(self):
        for category in errors.TRANSIENT | errors.CORRECTIVE:
            with self.subTest(category=category):
                self.assertTrue(is_retryable(category))
        for category in errors.DETERMINISTIC | errors.TERMINAL_BOOKKEEPING | {ErrorCategory.OK}:
            with self.subTest(category=category):
                self.assertFalse(is_retryable(category))


class BrokerErrorTests(unittest.TestCase):
    def test_valid_category_is_preserved_in_exception(self):
        error = BrokerError(ErrorCategory.PEER_TIMEOUT)
        self.assertEqual(error.category, ErrorCategory.PEER_TIMEOUT)
        self.assertEqual(str(error), ErrorCategory.PEER_TIMEOUT.value)

    def test_non_category_is_rejected_with_type_error(self):
        for value in ("peer_timeout", None, False):
            with self.subTest(value=value), self.assertRaisesRegex(
                    TypeError, "category must be an ErrorCategory"):
                BrokerError(value)


class ImportGuardTests(unittest.TestCase):
    def test_unclassified_category_fails_import(self):
        source = Path(errors.__file__).read_text(encoding="utf-8")
        source = source.replace("    ErrorCategory.PEER_TIMEOUT,\n", "", 1)
        with self.assertRaisesRegex(AssertionError, "unclassified error categories"):
            exec(compile(source, "<unclassified errors>", "exec"), {"__name__": "test_errors_unclassified"})

    def test_overlapping_category_fails_import(self):
        source = Path(errors.__file__).read_text(encoding="utf-8")
        source = source.replace("    ErrorCategory.PEER_NONZERO_EXIT,\n",
                                "    ErrorCategory.PEER_NONZERO_EXIT,\n"
                                "    ErrorCategory.PEER_TIMEOUT,\n", 1)
        with self.assertRaisesRegex(AssertionError, "more than one class"):
            exec(compile(source, "<overlapping errors>", "exec"), {"__name__": "test_errors_overlapping"})


class VocabularyTests(unittest.TestCase):
    def test_peer_timeout_has_stable_wire_value(self):
        self.assertEqual(ErrorCategory.PEER_TIMEOUT.value, "peer_timeout")
