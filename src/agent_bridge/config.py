"""Versioned broker configuration.

Executable paths, allowed versions, timeouts, concurrency, retention and the
Claude per-call budget live in exactly one file.  Nothing in the implementation
hard-codes them.
"""

from __future__ import annotations

import os
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


class Config:
    def __init__(self, raw: dict[str, Any], path: str):
        self.raw = raw
        self.path = path
        self.repo_root = REPO_ROOT
        version = raw.get("config_version")
        if version not in SUPPORTED_CONFIG_VERSIONS:
            raise ValueError(f"unsupported config_version {version!r}")
        for key in ("state_root", "schema_path", "limits", "retention", "peers"):
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

    @property
    def refused_classifications(self) -> tuple[str, ...]:
        return tuple(self.raw.get("refused_source_classifications") or ())

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

    @property
    def ledger_path(self) -> str:
        return self.state("ledger", "exchanges.jsonl")

    def job_dir(self, job_id: str) -> str:
        return self.state("jobs", job_id)

    def conversation_path(self, conversation_id: str) -> str:
        return self.state("conversations", f"{conversation_id}.json")

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
        schema = json.loads(blob.decode("utf-8"))
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


def local_config_path() -> str:
    return os.path.join(REPO_ROOT, "config", LOCAL_CONFIG_NAME)


def load(path: str | None = None) -> Config:
    """Load the committed defaults, then layer machine-local overrides.

    An explicit path or AGENT_BRIDGE_CONFIG is used verbatim and is NOT layered,
    so the test suite and the canary runner stay fully deterministic.
    """
    explicit = path or os.environ.get(CONFIG_ENV)
    if explicit:
        return Config(store.read_json(explicit), explicit)
    raw = store.read_json(DEFAULT_CONFIG_PATH)
    local = local_config_path()
    if os.path.isfile(local):
        overlay = store.read_json_or_none(local)
        if isinstance(overlay, dict):
            raw = _deep_merge(raw, overlay)
    return Config(raw, DEFAULT_CONFIG_PATH)
