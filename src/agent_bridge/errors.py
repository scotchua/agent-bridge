"""Closed error vocabulary.

Every failure surfaced to a calling agent must map to exactly one member of
ErrorCategory.  Hints are constant strings looked up by category.  Nothing in
this module interpolates peer output, peer stderr, or any other untrusted text
into a caller-visible field.  That is the whole point of the module: the
previous bridge's free-form "safe detail" allowlist is deliberately absent.
"""

from __future__ import annotations

import enum


class ErrorCategory(str, enum.Enum):
    OK = "ok"

    # Request-side, deterministic.  Never retried.
    INPUT_SCHEMA_INVALID = "input_schema_invalid"
    INPUT_UNKNOWN_FIELD = "input_unknown_field"
    INPUT_TOO_LARGE = "input_too_large"
    SOURCE_CLASSIFICATION_REFUSED = "source_classification_refused"
    CONVERSATION_NOT_FOUND = "conversation_not_found"
    CONVERSATION_CLOSED = "conversation_closed"
    CONVERSATION_BUSY = "conversation_busy"
    CONVERSATION_NOT_OWNED = "conversation_not_owned"
    CONVERSATION_INDETERMINATE = "conversation_indeterminate"
    CONVERSATION_TURN_LIMIT = "conversation_turn_limit"
    CONCURRENCY_LIMIT = "concurrency_limit"
    JOB_NOT_FOUND = "job_not_found"
    JOB_NOT_COMPLETE = "job_not_complete"
    JOB_ALREADY_TERMINAL = "job_already_terminal"

    # Environment, deterministic.  Never retried.
    PREFLIGHT_EXECUTABLE_MISSING = "preflight_executable_missing"
    PREFLIGHT_VERSION_MISMATCH = "preflight_version_mismatch"
    PREFLIGHT_SCHEMA_MISSING = "preflight_schema_missing"
    WORKSPACE_CONTAMINATED = "workspace_contaminated"
    WORKSPACE_UNVERIFIABLE = "workspace_unverifiable"
    PEER_HOME_CONFIG_PRESENT = "peer_home_config_present"
    PEER_AUTH_FAILURE = "peer_auth_failure"

    # Peer-side, deterministic.  One corrective retry allowed for the schema
    # case only, and only from a corrective message built entirely from this
    # code plus schema metadata.
    PEER_OUTPUT_MALFORMED = "peer_output_malformed"
    PEER_OUTPUT_SCHEMA_INVALID = "peer_output_schema_invalid"
    PEER_OUTPUT_TOO_LARGE = "peer_output_too_large"
    PEER_OUTPUT_INCOMPLETE = "peer_output_incomplete"
    PEER_SESSION_ID_MISSING = "peer_session_id_missing"
    PEER_SESSION_MIGRATED = "peer_session_migrated"
    PEER_CONTRACT_VERSION_MISMATCH = "peer_contract_version_mismatch"

    # Transient.  At most one retry.
    PEER_SPAWN_FAILURE = "peer_spawn_failure"
    PEER_NONZERO_EXIT = "peer_nonzero_exit"
    PEER_TIMEOUT = "peer_timeout"
    GATE_TIMEOUT = "gate_timeout"

    # Terminal bookkeeping.
    WORKER_DIED = "worker_died"
    RETRY_EXHAUSTED = "retry_exhausted"
    CANCELLED = "cancelled"
    INTERNAL_ERROR = "internal_error"


#: Categories that must never trigger an automatic retry of any kind.
DETERMINISTIC: frozenset[ErrorCategory] = frozenset({
    ErrorCategory.INPUT_SCHEMA_INVALID,
    ErrorCategory.INPUT_UNKNOWN_FIELD,
    ErrorCategory.INPUT_TOO_LARGE,
    ErrorCategory.SOURCE_CLASSIFICATION_REFUSED,
    ErrorCategory.CONVERSATION_NOT_FOUND,
    ErrorCategory.CONVERSATION_CLOSED,
    ErrorCategory.CONVERSATION_BUSY,
    ErrorCategory.CONVERSATION_NOT_OWNED,
    ErrorCategory.CONVERSATION_INDETERMINATE,
    ErrorCategory.CONVERSATION_TURN_LIMIT,
    ErrorCategory.CONCURRENCY_LIMIT,
    ErrorCategory.JOB_NOT_FOUND,
    ErrorCategory.JOB_NOT_COMPLETE,
    ErrorCategory.JOB_ALREADY_TERMINAL,
    ErrorCategory.PREFLIGHT_EXECUTABLE_MISSING,
    ErrorCategory.PREFLIGHT_VERSION_MISMATCH,
    ErrorCategory.PREFLIGHT_SCHEMA_MISSING,
    ErrorCategory.WORKSPACE_CONTAMINATED,
    ErrorCategory.WORKSPACE_UNVERIFIABLE,
    ErrorCategory.PEER_HOME_CONFIG_PRESENT,
    ErrorCategory.PEER_AUTH_FAILURE,
    ErrorCategory.PEER_CONTRACT_VERSION_MISMATCH,
    ErrorCategory.PEER_SESSION_MIGRATED,
    ErrorCategory.PEER_OUTPUT_INCOMPLETE,
    ErrorCategory.GATE_TIMEOUT,
    ErrorCategory.CANCELLED,
})

#: Transient categories eligible for at most one plain retry (same prompt).
TRANSIENT: frozenset[ErrorCategory] = frozenset({
    ErrorCategory.PEER_SPAWN_FAILURE,
    ErrorCategory.PEER_NONZERO_EXIT,
    ErrorCategory.PEER_TIMEOUT,
})

#: Categories eligible for exactly one *corrective* retry, where the second
#: prompt differs from the first and is generated only from schema metadata.
CORRECTIVE: frozenset[ErrorCategory] = frozenset({
    ErrorCategory.PEER_OUTPUT_MALFORMED,
    ErrorCategory.PEER_OUTPUT_SCHEMA_INVALID,
})

_HINTS: dict[ErrorCategory, str] = {
    ErrorCategory.INPUT_SCHEMA_INVALID: "Request did not match the tool input schema.",
    ErrorCategory.INPUT_UNKNOWN_FIELD: "Request contained a field this tool does not accept.",
    ErrorCategory.INPUT_TOO_LARGE: "Request exceeded a configured size limit.",
    ErrorCategory.SOURCE_CLASSIFICATION_REFUSED: (
        "This source_classification is refused in contract version 1. "
        "Allowed values are internal, synthetic, public."
    ),
    ErrorCategory.CONVERSATION_NOT_FOUND: "No such conversation_id.",
    ErrorCategory.CONVERSATION_CLOSED: "Conversation is closed to further turns.",
    ErrorCategory.CONVERSATION_BUSY: "Conversation already has a job in flight.",
    ErrorCategory.CONVERSATION_INDETERMINATE: (
        "A previous turn's worker died while a peer call was in flight, so it "
        "is unknown whether the peer completed that turn. This conversation is "
        "held until an operator resolves it, because resuming it as though the "
        "call failed could drive one peer session from two places."
    ),
    ErrorCategory.CONVERSATION_NOT_OWNED: (
        "This job no longer holds the conversation claim, so it may not "
        "change the conversation. Nothing was modified."
    ),
    ErrorCategory.CONVERSATION_TURN_LIMIT: "Conversation reached its configured turn limit.",
    ErrorCategory.CONCURRENCY_LIMIT: "Too many consultations already in flight.",
    ErrorCategory.JOB_NOT_FOUND: "No such job_id.",
    ErrorCategory.JOB_NOT_COMPLETE: "Job has not reached a terminal state yet.",
    ErrorCategory.JOB_ALREADY_TERMINAL: (
        "The job reached a terminal state before its worker could publish a "
        "running status, so the worker abandoned it without contacting the "
        "peer. Nothing was sent."
    ),
    ErrorCategory.PREFLIGHT_EXECUTABLE_MISSING: "Configured peer executable was not found.",
    ErrorCategory.PREFLIGHT_VERSION_MISMATCH: (
        "Peer CLI version is not in the allowed list. Re-ratify the pin before use."
    ),
    ErrorCategory.PREFLIGHT_SCHEMA_MISSING: "Response contract schema file was not found.",
    ErrorCategory.WORKSPACE_CONTAMINATED: (
        "Isolated workspace or an ancestor contains agent instruction files."
    ),
    ErrorCategory.WORKSPACE_UNVERIFIABLE: (
        "An ancestor of the isolated workspace could not be enumerated, so it "
        "cannot be shown to be free of agent instruction files. Failed closed."
    ),
    ErrorCategory.PEER_HOME_CONFIG_PRESENT: (
        "The isolated peer home contains a config.toml. That is the file "
        "`codex mcp add` writes to, so it is the one path by which this bridge "
        "could be handed back to the peer. Remove it before running."
    ),
    ErrorCategory.PEER_AUTH_FAILURE: (
        "Peer CLI is not authenticated. Operator action is required; the bridge "
        "will not attempt to authenticate."
    ),
    ErrorCategory.PEER_OUTPUT_MALFORMED: "Peer output was not parseable JSON. It was quarantined.",
    ErrorCategory.PEER_OUTPUT_SCHEMA_INVALID: (
        "Peer output did not satisfy the response contract. It was quarantined."
    ),
    ErrorCategory.PEER_OUTPUT_TOO_LARGE: "Peer output exceeded a configured size limit.",
    ErrorCategory.PEER_OUTPUT_INCOMPLETE: (
        "The peer's output stream never reached end-of-file, because a process "
        "it spawned still held the pipe open. What was captured may be a "
        "prefix rather than the whole reply, so it was not accepted."
    ),
    ErrorCategory.GATE_TIMEOUT: (
        "Timed out waiting for the global admission lock. Another broker "
        "process is holding it."
    ),
    ErrorCategory.PEER_SESSION_ID_MISSING: (
        "Peer did not emit a session identifier, so the conversation cannot be continued."
    ),
    ErrorCategory.PEER_CONTRACT_VERSION_MISMATCH: "Peer replied with an unexpected contract_version.",
    ErrorCategory.PEER_SESSION_MIGRATED: (
        "The peer resumed a different session than the one requested, so this "
        "turn did not continue the intended conversation. Failed closed rather "
        "than silently migrating the conversation to the new session."
    ),
    ErrorCategory.PEER_SPAWN_FAILURE: "Peer process could not be started.",
    ErrorCategory.PEER_NONZERO_EXIT: "Peer process exited nonzero.",
    ErrorCategory.PEER_TIMEOUT: "Peer process exceeded its timeout and its process group was killed.",
    ErrorCategory.WORKER_DIED: "Worker process died without recording a terminal state.",
    ErrorCategory.RETRY_EXHAUSTED: "Retry budget exhausted.",
    ErrorCategory.CANCELLED: "Job was cancelled.",
    ErrorCategory.INTERNAL_ERROR: "Broker internal error.",
    ErrorCategory.OK: "",
}


def hint(category: ErrorCategory) -> str:
    """Return the constant operator hint for a category. Never interpolated."""
    return _HINTS.get(category, "")


def is_retryable(category: ErrorCategory) -> bool:
    return category in TRANSIENT or category in CORRECTIVE


class BrokerError(Exception):
    """Raised for a categorised failure. Carries no untrusted text."""

    def __init__(self, category: ErrorCategory):
        if not isinstance(category, ErrorCategory):
            raise TypeError("category must be an ErrorCategory")
        super().__init__(category.value)
        self.category = category
