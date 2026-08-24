"""Backend interface shared by both peers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..errors import ErrorCategory


@dataclass
class PeerOutcome:
    """Result of one peer invocation, before contract validation."""

    category: ErrorCategory
    payload: Any = None                    # parsed JSON object, if any
    peer_session_id: str | None = None
    observed_model: str | None = None
    argv: list[str] = field(default_factory=list)
    returncode: int | None = None
    duration_seconds: float = 0.0
    timed_out: bool = False
    group_kill: dict[str, Any] = field(default_factory=dict)
    extraction_path: str | None = None     # which documented channel produced payload
    raw_stdout: bytes = b""                # for quarantine only, never returned
    raw_stderr: bytes = b""                # for quarantine only, never returned
    #: The peer's own final-message channel, when it has one distinct from
    #: stdout. For Codex that is --output-last-message, which is where a
    #: malformed response actually lives: stdout carries only JSONL events, so
    #: quarantining stdout alone preserved everything except the offending text.
    raw_last_message: bytes = b""
    cost_usd: float | None = None
    notes: dict[str, Any] = field(default_factory=dict)


#: Substrings that identify an authentication failure.  Matching is not
#: echoing: the matched text never reaches a caller-visible field.
AUTH_MARKERS = (
    "failed to authenticate",
    "oauth session expired",
    "not logged in",
    "please run `claude login`",
    "please run claude login",
    "401 unauthorized",
    "missing bearer",
    "invalid api key",
    "unauthorized",
    "no credentials",
    "run `codex login`",
    "codex login",
)


def looks_like_auth_failure(*blobs: bytes | str | None) -> bool:
    for blob in blobs:
        if not blob:
            continue
        text = blob.decode("utf-8", "replace") if isinstance(blob, bytes) else blob
        lowered = text.lower()
        for marker in AUTH_MARKERS:
            if marker in lowered:
                return True
    return False


def strip_fences(text: str) -> str:
    """Remove a single markdown fence wrapper if the peer added one anyway."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) < 2:
        return stripped
    lines = lines[1:]
    while lines and lines[-1].strip() in ("```", ""):
        lines.pop()
    return "\n".join(lines).strip()


def parse_single_object(text: str) -> Any | None:
    """Parse text as exactly one JSON object, after stripping one fence.

    Deliberately strict. An earlier version scanned for the first balanced
    JSON object anywhere in the text, which broke the single-object contract:
    a schema-shaped example appearing before the real answer would be selected
    as the response. The contract tells the peer to emit one object and no
    prose, so anything else fails closed and is quarantined for a human.
    """
    import json

    candidate = strip_fences(text)
    if not candidate:
        return None
    try:
        parsed = json.loads(candidate)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


#: argv values long enough to be a payload rather than a setting are recorded
#: as a shape, not verbatim, so the attempt log stays readable and bounded.
ARGV_VALUE_MAX = 60


def argv_shape(argv: list[str]) -> list[str]:
    """Sanitized argv for provenance.

    No secret is ever placed in argv by this bridge, so nothing is redacted for
    secrecy.  Long values (the inlined JSON Schema) are summarised by length so
    the recorded shape stays diffable.
    """
    shaped: list[str] = []
    for item in argv:
        if len(item) > ARGV_VALUE_MAX:
            shaped.append(f"<{len(item)} chars>")
        else:
            shaped.append(item)
    return shaped
