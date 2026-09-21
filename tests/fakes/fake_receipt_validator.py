"""Exact-shape offline substitute for the installed receipt validator."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def validate_success(record, output, *, invocation_id, input_sha256,
                     canonical_input_sha256, effective_options,
                     parent_task_id=None, attempt_id=None):
    expected = {
        "invocation_id": invocation_id,
        "input_sha256": input_sha256,
        "canonical_input_sha256": canonical_input_sha256,
        "effective_options": effective_options,
        "parent_task_id": parent_task_id,
        "attempt_id": attempt_id,
        "output_sha256": hashlib.sha256(output).hexdigest(),
    }
    if record.get("result_status") != "success" or any(
            record.get(key) != value for key, value in expected.items()):
        raise ValueError("delegation result does not match the expected invocation")
    return record


def read_success(path, output, *, receipt_sha256, **bindings):
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != receipt_sha256:
        raise ValueError("delegation receipt bytes changed")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate receipt key")
            result[key] = value
        return result

    record = json.loads(raw, object_pairs_hook=unique)
    return validate_success(record, output, **bindings)
