"""A small, deterministic JSON Schema subset validator.

Deliberately dependency-free and deliberately narrow.  It implements only the
keywords the response contract uses, and it *rejects* a schema that contains a
keyword it does not implement, so a future schema edit cannot silently become
unenforced.  The broker's copy of the schema is the source of truth; the peer
CLIs are handed the same file but their compliance is never trusted.
"""

from __future__ import annotations

from typing import Any

SUPPORTED_KEYWORDS = frozenset({
    "$schema", "title", "description",
    "type", "enum", "const",
    "properties", "required", "additionalProperties",
    "items", "minItems", "maxItems",
    "minLength", "maxLength",
    "minimum", "maximum",
})

_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "boolean": (bool,),
    "null": (type(None),),
}


class SchemaSupportError(Exception):
    """The schema uses a keyword this validator does not implement."""


def assert_supported(schema: Any, path: str = "$") -> None:
    """Raise SchemaSupportError if the schema uses an unimplemented keyword."""
    if not isinstance(schema, dict):
        raise SchemaSupportError(f"{path}: schema node must be an object")
    unknown = sorted(set(schema) - SUPPORTED_KEYWORDS)
    if unknown:
        raise SchemaSupportError(f"{path}: unsupported schema keywords {unknown}")
    if "type" in schema:
        t = schema["type"]
        if not isinstance(t, str) or t not in _TYPE_MAP and t not in ("number", "integer"):
            raise SchemaSupportError(f"{path}: unsupported type {t!r}")
    for name, sub in (schema.get("properties") or {}).items():
        assert_supported(sub, f"{path}.{name}")
    if "items" in schema:
        assert_supported(schema["items"], f"{path}[]")
    ap = schema.get("additionalProperties", False)
    if not isinstance(ap, bool):
        raise SchemaSupportError(f"{path}: additionalProperties must be boolean")


#: Hard ceiling on collected violations. A bounded response can still contain
#: an enormous number of individually invalid items, and emitting one error
#: string per item turned a few megabytes of input into far more memory than
#: the input itself. The first violations are what a human reads; the rest only
#: cost memory.
MAX_VIOLATIONS = 100


def validate(instance: Any, schema: Any, path: str = "$") -> list[str]:
    """Validate and return at most MAX_VIOLATIONS structural violations."""
    errs = _validate(instance, schema, path)
    if len(errs) > MAX_VIOLATIONS:
        remaining = len(errs) - MAX_VIOLATIONS
        return errs[:MAX_VIOLATIONS] + [f"and {remaining} further violations, truncated"]
    return errs


def _validate(instance: Any, schema: Any, path: str = "$") -> list[str]:
    """Return a list of human-readable violations. Empty list means valid.

    Violation strings describe *structure* only (paths, expected types, allowed
    enum values).  They never echo the instance's string values, so a violation
    list is safe to record in provenance without leaking peer prose.
    """
    errs: list[str] = []
    t = schema.get("type")

    if t is not None:
        if t == "integer":
            ok = isinstance(instance, int) and not isinstance(instance, bool)
        elif t == "number":
            ok = isinstance(instance, (int, float)) and not isinstance(instance, bool)
        elif t == "string":
            ok = isinstance(instance, str)
        elif t == "boolean":
            ok = isinstance(instance, bool)
        else:
            expected = _TYPE_MAP[t]
            ok = isinstance(instance, expected)
            if t == "object" and isinstance(instance, bool):
                ok = False
        if not ok:
            errs.append(f"{path}: expected type {t}")
            return errs  # further checks would be meaningless

    if "const" in schema and instance != schema["const"]:
        errs.append(f"{path}: value not equal to required const")
    if "enum" in schema and instance not in schema["enum"]:
        allowed = ", ".join(repr(v) for v in schema["enum"])
        errs.append(f"{path}: value not in enum [{allowed}]")

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errs.append(f"{path}: shorter than minLength {schema['minLength']}")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errs.append(f"{path}: longer than maxLength {schema['maxLength']}")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errs.append(f"{path}: below minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errs.append(f"{path}: above maximum {schema['maximum']}")

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errs.append(f"{path}: fewer than minItems {schema['minItems']}")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errs.append(f"{path}: more than maxItems {schema['maxItems']}")
        item_schema = schema.get("items")
        if item_schema is not None:
            # Stop once enough violations exist to fail the document. Walking
            # every element of a million-item array to describe each one is
            # pure amplification: the verdict is already decided.
            for i, item in enumerate(instance):
                if len(errs) > MAX_VIOLATIONS:
                    errs.append(f"{path}: further items not examined, "
                                "violation limit reached")
                    break
                errs.extend(_validate(item, item_schema, f"{path}[{i}]"))

    if isinstance(instance, dict):
        props: dict[str, Any] = schema.get("properties") or {}
        for name in schema.get("required") or []:
            if name not in instance:
                errs.append(f"{path}: missing required property {name!r}")
        if schema.get("additionalProperties", True) is False:
            for name in sorted(instance):
                if name not in props:
                    errs.append(f"{path}: unexpected property {name!r}")
        for name, sub in props.items():
            if name in instance:
                errs.extend(_validate(instance[name], sub, f"{path}.{name}"))

    return errs
