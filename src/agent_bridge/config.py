"""Versioned broker configuration.

Executable paths, allowed versions, timeouts, concurrency, retention and the
Claude per-call budget live in exactly one file.  Nothing in the implementation
hard-codes them.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

from . import store
from .errors import BrokerError, ErrorCategory

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_CONFIG_PATH = os.path.join(REPO_ROOT, "config", "broker.json")
SUPPORTED_CONFIG_VERSIONS = ("1",)
PEERS = ("claude", "codex")

#: Env var used to point the broker at a specific config file.
CONFIG_ENV = "AGENT_BRIDGE_CONFIG"

#: Machine-specific overrides, written by `agent-bridge-setup` and gitignored.
#: Layered on top of the committed defaults so nobody has to edit a tracked
#: file to run this on their own machine.
LOCAL_CONFIG_NAME = "local.json"

REQUIRED_KEYS = ("state_root", "schema_path", "limits", "retention", "peers")


def canonical_uuid(identifier: Any) -> str:
    """Return a caller-visible identifier only when it has UUID's canonical form.

    Job and conversation identifiers become path components.  Keeping this
    check adjacent to the derived-path helpers makes every caller, including
    operator commands, reject traversal and absolute paths before joining them
    to the state root.
    """
    if not isinstance(identifier, str):
        raise BrokerError(ErrorCategory.INPUT_SCHEMA_INVALID)
    try:
        parsed = uuid.UUID(identifier)
    except (AttributeError, TypeError, ValueError) as exc:
        raise BrokerError(ErrorCategory.INPUT_SCHEMA_INVALID) from exc
    if str(parsed) != identifier:
        raise BrokerError(ErrorCategory.INPUT_SCHEMA_INVALID)
    return identifier


class Config:
    def __init__(self, raw: dict[str, Any], path: str):
        self.raw = raw
        self.path = path
        self.repo_root = REPO_ROOT
        version = raw.get("config_version")
        if version not in SUPPORTED_CONFIG_VERSIONS:
            raise ValueError(f"unsupported config_version {version!r}")
        for key in REQUIRED_KEYS:
            if key not in raw:
                raise ValueError(f"config missing required key {key!r}")
        for peer in PEERS:
            if peer not in raw["peers"]:
                raise ValueError(f"config missing peers.{peer}")

    # ---- scalars -------------------------------------------------------
    @property
    def contract_version(self) -> str:
        return str(self.raw.get("contract_version", "1"))

    @property
    def state_root(self) -> str:
        return os.path.expanduser(self.raw["state_root"])

    @property
    def schema_path(self) -> str:
        candidate = self.raw["schema_path"]
        if not os.path.isabs(candidate):
            candidate = os.path.join(self.repo_root, candidate)
        return candidate

    def limit(self, name: str) -> int:
        try:
            return int(self.raw["limits"][name])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"missing or non-integer limits.{name}") from exc

    def retention_days(self, name: str) -> int:
        return int(self.raw["retention"][name])

    @property
    def allowed_classifications(self) -> tuple[str, ...]:
        return tuple(self.raw.get("allowed_source_classifications") or ())

    #: Effort levels both CLIs accept. Kept identical so one setting means the
    #: same thing on each side.
    EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

    def peer_reasoning_effort(self, peer: str) -> str | None:
        """Reasoning effort for a peer, or None to use the CLI's own default.

        This is worth setting deliberately. Neither CLI inherits anything here:
        the Codex peer runs with --ignore-user-config, so a personal
        model_reasoning_effort is not read, and the Claude peer is invoked
        without --effort. Left unset, both run at whatever their default is,
        which for a tool whose entire job is adversarial review is a decision
        worth making rather than inheriting by omission.
        """
        value = self.peer(peer).get("reasoning_effort")
        if value is None:
            return None
        if not isinstance(value, str) or value not in self.EFFORT_LEVELS:
            raise ValueError(
                f"peers.{peer}.reasoning_effort must be one of "
                f"{list(self.EFFORT_LEVELS)}, got {value!r}")
        return value

    def peer_allowed_classifications(self, peer: str) -> tuple[str, ...]:
        """What a specific peer may receive.

        Defaults to the global list. Overriding it per peer exists because the
        two peers are different companies under different accounts and possibly
        different plans: if one side's terms are weaker than the other's, the
        weaker side should be allowed to receive less. Exposure is per
        direction, not an average of the two.
        """
        override = self.peer(peer).get("allowed_source_classifications")
        if override is None:
            return self.allowed_classifications
        if not isinstance(override, list) or not all(isinstance(x, str) for x in override):
            raise ValueError(f"peers.{peer}.allowed_source_classifications must be a list of strings")
        return tuple(override)

    @property
    def refused_classifications(self) -> tuple[str, ...]:
        return tuple(self.raw.get("refused_source_classifications") or ())

    def _local_first(self) -> dict[str, Any]:
        raw = self.raw.get("local_first")
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            raise ValueError("local_first must be an object")
        return raw

    def local_first_enabled(self) -> bool:
        """Consultation accountability (design section 2.8), off by default.

        A separate, optional switch from the orchestration read gate's own
        `local_first.enabled` (routing-policy.json): this bridge and the
        orchestration subsystem are different packages with different
        config files, and this flag only says whether a large `*_start`/
        `*_continue` prompt must carry a `local_first` declaration. Turning
        it on is the operator's decision, same as the read gate's.
        """
        value = self._local_first().get("enabled", False)
        if not isinstance(value, bool):
            raise ValueError("local_first.enabled must be a boolean")
        return value

    def local_first_min_bytes(self) -> int:
        """The prompt-size floor above which local_first is required.

        Defaults to 8,000, the same `read_gate_min_bytes` default the
        orchestration read gate uses and for the same reason (design
        section 4): a prompt no larger than the largest possible local
        draft cannot be shortened by digesting it first.
        """
        value = self._local_first().get("read_gate_min_bytes", 8000)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("local_first.read_gate_min_bytes must be a non-negative integer")
        return value

    def local_first_queue_root(self) -> str:
        """Where the orchestration subsystem's local queue lives, so a
        supplied `digest_receipt_id` can be checked against its
        `routing_receipts` table. Empty when local_first is not configured
        to point at one; a receipt then never verifies, so the only way to
        satisfy a live requirement is a typed `bypass` reason."""
        value = self._local_first().get("local_queue_root", "")
        if not isinstance(value, str):
            raise ValueError("local_first.local_queue_root must be a string")
        return os.path.expanduser(value) if value else ""

    def peer(self, name: str) -> dict[str, Any]:
        if name not in PEERS:
            raise ValueError(f"unknown peer {name!r}")
        return dict(self.raw["peers"][name])

    #: Environment names that must never be supplied through configuration.
    #: Credentials reach a peer through its own credential store, never through
    #: this file and never through an MCP server definition.
    BANNED_ENV = frozenset({
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
    })

    def peer_extra_env(self, name: str) -> dict[str, str]:
        """Declared, non-secret environment additions for a peer invocation."""
        raw = (self.peer(name).get("extra_env") or {})
        if not isinstance(raw, dict):
            raise ValueError(f"peers.{name}.extra_env must be an object")
        env: dict[str, str] = {}
        for key, value in raw.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError(f"peers.{name}.extra_env must map strings to strings")
            if key in self.BANNED_ENV:
                raise ValueError(f"peers.{name}.extra_env must not carry {key}")
            env[key] = value
        return env

    @property
    def poll_interval(self) -> float:
        return float((self.raw.get("worker") or {}).get("poll_interval_seconds", 0.25))

    @property
    def python_executable(self) -> str:
        import sys
        configured = (self.raw.get("worker") or {}).get("python_executable")
        return configured or sys.executable

    # ---- derived paths -------------------------------------------------
    def state(self, *parts: str) -> str:
        return os.path.join(self.state_root, *parts)

    def _state_identifier_path(self, directory: str, identifier: Any,
                               suffix: str = "") -> str:
        """Build an identifier path without following an escaped state component."""
        name = canonical_uuid(identifier) + suffix
        candidate = self.state(directory, name)
        root = os.path.realpath(self.state_root)
        resolved = os.path.realpath(candidate)
        try:
            contained = os.path.commonpath((root, resolved)) == root
        except ValueError:  # Different Windows volumes cannot be contained.
            contained = False
        if not contained:
            raise BrokerError(ErrorCategory.STATE_ROOT_INSECURE)
        return candidate

    @property
    def ledger_path(self) -> str:
        return self.state("ledger", "exchanges.jsonl")

    def job_dir(self, job_id: str) -> str:
        return self._state_identifier_path("jobs", job_id)

    def conversation_path(self, conversation_id: str) -> str:
        return self._state_identifier_path("conversations", conversation_id, ".json")

    def workspace(self, peer: str, conversation_id: str) -> str:
        return self.state("workspaces", peer, conversation_id)

    # ---- contract schema ----------------------------------------------
    def load_schema(self) -> tuple[dict[str, Any], str]:
        """Return (schema, sha256). The broker's copy is the source of truth."""
        path = self.schema_path
        if not os.path.isfile(path):
            raise BrokerError(ErrorCategory.PREFLIGHT_SCHEMA_MISSING)
        with open(path, "rb") as handle:
            blob = handle.read()
        import json
        schema = json.loads(blob.decode("utf-8-sig"))
        from . import schema_validate
        schema_validate.assert_supported(schema)
        return schema, store.sha256_bytes(blob)


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Return defaults with an overlay applied, without mutating either input."""
    return _deep_merge(base, overlay)


def effective_config_sha256(raw: dict[str, Any]) -> str:
    """Hash the canonical file representation used for effective configs."""
    payload = json.dumps(raw, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    return store.sha256_bytes(payload.encode("utf-8"))


def _is_complete(raw: Any) -> bool:
    # Overlay fragments deliberately carry no config_version. Once a document
    # declares one, treat it as a full config and let Config reject omissions;
    # silently filling a damaged snapshot or candidate from defaults would
    # change the artifact that was meant to be measured.
    return isinstance(raw, dict) and raw.get("config_version") is not None


def load_effective(path: str) -> Config:
    """Load a complete effective-config artifact without applying an overlay."""
    raw = store.read_json(path)
    if not _is_complete(raw):
        raise ValueError(f"effective config {path!r} is not complete")
    return Config(raw, path)


def build_effective(overlay_path: str | None = None,
                    base_path: str | None = None) -> Config:
    """Build and validate defaults plus one selected machine-local overlay."""
    base = base_path or DEFAULT_CONFIG_PATH
    raw = store.read_json(base)
    if overlay_path:
        overlay = store.read_json(overlay_path)
        if not isinstance(overlay, dict):
            raise ValueError(f"config overlay {overlay_path!r} must be an object")
        raw = _deep_merge(raw, overlay)
    return Config(raw, overlay_path or base)


def local_config_path() -> str:
    return os.path.join(REPO_ROOT, "config", LOCAL_CONFIG_NAME)


def load(path: str | None = None) -> Config:
    """Load one complete config, or build defaults plus a selected overlay.

    Complete explicit configs, including setup candidate artifacts and job
    snapshots, are consumed verbatim. An explicit fragment such as local.json
    is treated as the selected overlay, matching the normal runtime build.
    """
    explicit = path or os.environ.get(CONFIG_ENV)
    if explicit:
        raw = store.read_json(explicit)
        if _is_complete(raw):
            return Config(raw, explicit)
        return build_effective(explicit)
    local = local_config_path()
    if os.path.isfile(local):
        overlay = store.read_json_or_none(local)
        if isinstance(overlay, dict):
            return Config(_deep_merge(store.read_json(DEFAULT_CONFIG_PATH), overlay),
                          DEFAULT_CONFIG_PATH)
    return build_effective()
