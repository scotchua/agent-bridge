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
    state = _ValidationState()
    _validate(instance, schema, path, state)
    if state.truncated:
        state.errors.append("further violations truncated")
    return state.errors


class _ValidationState:
    """Collect a fixed number of violations without building an unbounded list."""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.truncated = False

    def add(self, error: str) -> bool:
        if len(self.errors) >= MAX_VIOLATIONS:
            self.truncated = True
            return False
        self.errors.append(error)
        return True

    @property
    def full(self) -> bool:
        return len(self.errors) >= MAX_VIOLATIONS


def _validate(instance: Any, schema: Any, path: str,
              state: _ValidationState) -> None:
    """Add human-readable structural violations to *state*.

    Violation strings describe *structure* only (paths, expected types, allowed
    enum values).  They never echo the instance's string values, so a violation
    list is safe to record in provenance without leaking peer prose.
    """
    if state.full:
        state.truncated = True
        return
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
            state.add(f"{path}: expected type {t}")
            return  # further checks would be meaningless

    if "const" in schema and instance != schema["const"]:
        state.add(f"{path}: value not equal to required const")
    if "enum" in schema and instance not in schema["enum"]:
        allowed = ", ".join(repr(v) for v in schema["enum"])
        state.add(f"{path}: value not in enum [{allowed}]")

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            state.add(f"{path}: shorter than minLength {schema['minLength']}")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            state.add(f"{path}: longer than maxLength {schema['maxLength']}")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            state.add(f"{path}: below minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            state.add(f"{path}: above maximum {schema['maximum']}")

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            state.add(f"{path}: fewer than minItems {schema['minItems']}")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            state.add(f"{path}: more than maxItems {schema['maxItems']}")
            # The maxItems failure already rejects the document. Do not walk
            # a peer-controlled overlong array just to accumulate redundant
            # failures for its elements.
            return
        item_schema = schema.get("items")
        if item_schema is not None:
            for i, item in enumerate(instance):
                if state.full:
                    state.truncated = True
                    break
                _validate(item, item_schema, f"{path}[{i}]", state)

    if isinstance(instance, dict):
        props: dict[str, Any] = schema.get("properties") or {}
        for name in schema.get("required") or []:
            if state.full:
                state.truncated = True
                return
            if name not in instance:
                state.add(f"{path}: missing required property {name!r}")
        if schema.get("additionalProperties", True) is False:
            for name in instance:
                if state.full:
                    state.truncated = True
                    return
                if name not in props:
                    # Object keys come from the instance too. Do not put an
                    # unexpected key into a corrective prompt or provenance.
                    state.add(f"{path}: unexpected property")
        for name, sub in props.items():
            if state.full:
                state.truncated = True
                return
            if name in instance:
                _validate(instance[name], sub, f"{path}.{name}", state)
