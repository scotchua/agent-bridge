"""Boundary and failure cases for the dependency-free schema validator."""
from __future__ import annotations

import unittest

from agent_bridge import schema_validate as sv


class SupportedSchemaTests(unittest.TestCase):
    def test_rejects_non_object_and_nested_schema_nodes(self) -> None:
        cases = [None, [], {"properties": {"account": None}}, {"items": []}]
        for schema in cases:
            with self.subTest(schema=schema):
                with self.assertRaises(sv.SchemaSupportError):
                    sv.assert_supported(schema)

    def test_rejects_unknown_keywords_and_types(self) -> None:
        cases = [{"format": "email"}, {"type": 7}, {"type": "currency"},
                 {"additionalProperties": "false"}]
        for schema in cases:
            with self.subTest(schema=schema):
                with self.assertRaises(sv.SchemaSupportError):
                    sv.assert_supported(schema)

    def test_accepts_supported_nested_schema(self) -> None:
        schema = {"type": "object", "additionalProperties": False,
                  "properties": {"accounts": {"type": "array", "items": {
                      "type": "number", "minimum": 0}}}}
        self.assertIsNone(sv.assert_supported(schema))
        self.assertIsNone(sv.assert_supported({"type": "integer"}))


class ValidationStateTests(unittest.TestCase):
    def test_initial_state_and_exact_capacity(self) -> None:
        self.assertEqual(sv.MAX_VIOLATIONS, 100)
        state = sv._ValidationState()
        self.assertEqual(state.errors, [])
        self.assertFalse(state.truncated)
        self.assertFalse(state.full)
        for number in range(100):
            self.assertTrue(state.add(f"invented account {number}"))
        self.assertTrue(state.full)
        self.assertEqual(len(state.errors), 100)

    def test_add_rejects_the_next_violation(self) -> None:
        state = sv._ValidationState()
        for number in range(100):
            state.add(f"invented account {number}")
        self.assertIs(state.add("extra"), False)
        self.assertTrue(state.truncated)
        self.assertEqual(len(state.errors), 100)
        self.assertNotIn("extra", state.errors)


class ValidateTests(unittest.TestCase):
    def test_returns_clean_and_single_violation_lists(self) -> None:
        self.assertEqual(sv.validate("Acme Fictional", {"type": "string"}), [])
        self.assertEqual(sv.validate(42, {"type": "string"}, "$.account"),
                         ["$.account: expected type string"])

    def test_truncation_marker_follows_exactly_one_hundred_errors(self) -> None:
        errors = sv.validate([0] * 101, {"items": {"type": "string"}})
        self.assertEqual(len(errors), 101)
        self.assertEqual(errors[0], "$[0]: expected type string")
        self.assertEqual(errors[99], "$[99]: expected type string")
        self.assertEqual(errors[100], "further violations truncated")


class StructuralValidationTests(unittest.TestCase):
    def test_checks_all_supported_types(self) -> None:
        good = [(4, "integer"), (4.5, "number"), ("Northstar Co", "string"),
                (False, "boolean"), (None, "null"), ([], "array"), ({}, "object")]
        bad = [(True, "integer"), (True, "number"), (4.5, "integer"),
               (0, "boolean"), (None, "string"), ({}, "array"), ([], "object")]
        for instance, kind in good:
            with self.subTest(instance=instance, kind=kind, valid=True):
                self.assertEqual(sv.validate(instance, {"type": kind}), [])
        for instance, kind in bad:
            with self.subTest(instance=instance, kind=kind, valid=False):
                self.assertEqual(sv.validate(instance, {"type": kind}),
                                 [f"$: expected type {kind}"])

    def test_checks_const_and_enum_without_echoing_instance(self) -> None:
        schema = {"const": "Invented Co", "enum": ["Invented Co", "Sample LLC"]}
        self.assertEqual(sv.validate("Invented Co", schema), [])
        self.assertEqual(sv.validate("Unknown Ledger", schema), [
            "$: value not equal to required const",
            "$: value not in enum ['Invented Co', 'Sample LLC']"])

    def test_string_length_boundaries(self) -> None:
        schema = {"type": "string", "minLength": 2, "maxLength": 4}
        self.assertEqual(sv.validate("a", schema), ["$: shorter than minLength 2"])
        self.assertEqual(sv.validate("ab", schema), [])
        self.assertEqual(sv.validate("abcd", schema), [])
        self.assertEqual(sv.validate("abcde", schema), ["$: longer than maxLength 4"])

    def test_numeric_boundaries_and_boolean_exclusion(self) -> None:
        schema = {"minimum": 2, "maximum": 4}
        self.assertEqual(sv.validate(1, schema), ["$: below minimum 2"])
        self.assertEqual(sv.validate(2, schema), [])
        self.assertEqual(sv.validate(4, schema), [])
        self.assertEqual(sv.validate(5, schema), ["$: above maximum 4"])
        self.assertEqual(sv.validate(True, schema), [])

    def test_array_boundaries_and_indexed_items(self) -> None:
        schema = {"type": "array", "minItems": 1, "maxItems": 2,
                  "items": {"type": "string"}}
        self.assertEqual(sv.validate([], schema), ["$: fewer than minItems 1"])
        self.assertEqual(sv.validate(["cash"], schema), [])
        self.assertEqual(sv.validate(["cash", 7], schema),
                         ["$[1]: expected type string"])
        self.assertEqual(sv.validate(["a", "b", "c"], schema),
                         ["$: more than maxItems 2"])

    def test_object_required_extra_and_nested_properties(self) -> None:
        schema = {"type": "object", "required": ["account"],
                  "additionalProperties": False,
                  "properties": {"account": {"type": "string"}}}
        self.assertEqual(sv.validate({}, schema), ["$: missing required property 'account'"])
        self.assertEqual(sv.validate({"account": "cash"}, schema), [])
        self.assertEqual(sv.validate({"account": 7, "surprise": "secret"}, schema),
                         ["$: unexpected property", "$.account: expected type string"])

    def test_default_additional_properties_are_allowed(self) -> None:
        self.assertEqual(sv.validate({"invented account": 3}, {"type": "object"}), [])


if __name__ == "__main__":
    unittest.main()
