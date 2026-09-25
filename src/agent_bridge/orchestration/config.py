"""Strict configuration for the additive orchestration MCP server."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import windows_wsl


class OrchestrationConfigError(ValueError):
    """Configuration is malformed or unsafe to interpret."""


@dataclass(frozen=True)
class OrchestrationConfig:
    state_root: Path
    local_queue_root: Path
    capacity_db: Path
    worker_executable: Path
    worker_state: Path
    interval_seconds: float = 5.0
    execution_queue_root: Path | None = None
    codex_task_executable: Path | None = None
    claude_task_executable: Path | None = None
    python_executable: Path | None = None
    # Optional at load so an existing config keeps working and Codex jobs keep
    # running. The Claude execution lane itself fails closed without it.
    claude_config_dir: Path | None = None
    # Windows delegation paths. Kept as strings, not Path, because they are
    # native Windows paths that this configuration must load and validate on
    # any platform; turning "C:\\x" into a Path off Windows silently makes it
    # a relative path called "C:\\x".
    windows_wsl_runtime_root: str | None = None
    windows_wsl_rootfs_path: str | None = None
    windows_wsl_manifest_path: str | None = None
    windows_wsl_sidecar_path: str | None = None
    # Which local backend the queue's own Service dispatches mechanical work
    # to. Chosen once, here, by the operator; nothing an assistant-facing
    # request supplies can change it. "private_worker" (the existing
    # Apple/Qwen-fallback worker) is the default so an existing config keeps
    # its current behavior unchanged.
    local_backend: str = "private_worker"
    gemma_delegate_executable: Path | None = None
    gemma_python_executable: Path | None = None
    gemma_receipt_root: Path | None = None
    gemma_receipt_validator_executable: Path | None = None
    gemma_delegate_sha256: str | None = None
    gemma_receipt_validator_sha256: str | None = None
    gemma_model_digest: str | None = None
    gemma_timeout_seconds: float = 600.0


_KEYS = frozenset({
    "config_version", "state_root", "local_queue_root", "capacity_db",
    "worker_executable", "worker_state", "interval_seconds",
    "execution_queue_root", "codex_task_executable", "claude_task_executable",
    "python_executable", "claude_config_dir", "windows_wsl_runtime_root", "windows_wsl_rootfs_path",
    "windows_wsl_manifest_path", "windows_wsl_sidecar_path",
    "local_backend", "gemma_delegate_executable", "gemma_python_executable",
    "gemma_receipt_root", "gemma_receipt_validator_executable", "gemma_timeout_seconds",
    "gemma_delegate_sha256", "gemma_receipt_validator_sha256", "gemma_model_digest",
})

#: The certified delegate's own current budget. Config may only widen this,
#: never quietly narrow it: see the ``gemma_timeout_seconds`` check in
#: ``load``.
GEMMA_CERTIFIED_MIN_TIMEOUT_SECONDS = 600.0

_GEMMA_REQUIRED_FIELDS = ("gemma_delegate_executable", "gemma_python_executable",
                         "gemma_receipt_root", "gemma_receipt_validator_executable",
                         "gemma_delegate_sha256", "gemma_receipt_validator_sha256",
                         "gemma_model_digest")

#: The three paths Windows delegation cannot run without. All or none: a
#: half-configured runtime would be discovered at dispatch time, when the
#: honest answer is already available at load time.
_WINDOWS_REQUIRED = ("windows_wsl_runtime_root", "windows_wsl_rootfs_path",
                     "windows_wsl_manifest_path")


def _windows_paths(raw: dict[str, Any]) -> dict[str, str | None]:
    supplied = [raw.get(name) for name in _WINDOWS_REQUIRED]
    sidecar = raw.get("windows_wsl_sidecar_path")
    if not any(value is not None for value in supplied):
        if sidecar is not None:
            raise OrchestrationConfigError("windows_wsl_configuration_incomplete")
        return {name: None for name in (*_WINDOWS_REQUIRED, "windows_wsl_sidecar_path")}
    if not all(value is not None for value in supplied):
        raise OrchestrationConfigError("windows_wsl_configuration_incomplete")
    resolved: dict[str, str | None] = {}
    for name in (*_WINDOWS_REQUIRED, "windows_wsl_sidecar_path"):
        value = raw.get(name)
        if value is None:
            resolved[name] = None
            continue
        try:
            resolved[name] = windows_wsl._validate_windows_host_path(name, value)
        except windows_wsl.WindowsWslContractError as exc:
            raise OrchestrationConfigError(f"{name}_invalid") from exc
    return resolved


def _absolute_path(value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise OrchestrationConfigError(f"{field}_invalid")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise OrchestrationConfigError(f"{field}_must_be_absolute")
    return path


def _existing_file(value: Any, field: str) -> Path:
    """Like ``_absolute_path``, but the certified delegate's own paths must
    already exist and be an ordinary file, checked at config-load time
    rather than discovered only when the queue tries to dispatch to it.
    Never a symlink: a config pointing at a name is a config pointing at
    whatever that name resolves to at run time, which this adapter's own
    "explicit configured installed path, never ... a versioned cache path
    discovered at runtime" requirement rules out.
    """
    path = _absolute_path(value, field)
    if path.is_symlink() or not path.is_file():
        raise OrchestrationConfigError(f"{field}_must_be_an_existing_regular_file")
    return path


def _existing_dir(value: Any, field: str) -> Path:
    path = _absolute_path(value, field)
    if path.is_symlink() or not path.is_dir():
        raise OrchestrationConfigError(f"{field}_must_be_an_existing_directory")
    return path


def _gemma_paths(raw: dict[str, Any]) -> dict[str, Any]:
    local_backend = raw.get("local_backend", "private_worker")
    if not isinstance(local_backend, str) or local_backend not in ("private_worker", "gemma_certified"):
        raise OrchestrationConfigError("local_backend_unsupported")
    supplied = [raw.get(name) for name in _GEMMA_REQUIRED_FIELDS]
    if local_backend != "gemma_certified":
        # Never partially configured for a backend that is not selected:
        # a stray gemma_* path left in a private_worker config is exactly
        # the kind of ambiguity "validate strictly and fail closed" rules
        # out, rather than silently ignoring it.
        if any(value is not None for value in supplied) or "gemma_timeout_seconds" in raw:
            raise OrchestrationConfigError("gemma_certified_configuration_must_be_absent")
        return {"local_backend": local_backend, "gemma_delegate_executable": None,
               "gemma_python_executable": None, "gemma_receipt_root": None,
               "gemma_receipt_validator_executable": None,
               "gemma_delegate_sha256": None, "gemma_receipt_validator_sha256": None,
               "gemma_model_digest": None,
               "gemma_timeout_seconds": GEMMA_CERTIFIED_MIN_TIMEOUT_SECONDS}
    if any(value is None for value in supplied):
        raise OrchestrationConfigError("gemma_certified_configuration_incomplete")
    delegate = _existing_file(raw.get("gemma_delegate_executable"), "gemma_delegate_executable")
    python_executable = _existing_file(raw.get("gemma_python_executable"), "gemma_python_executable")
    receipt_root = _existing_dir(raw.get("gemma_receipt_root"), "gemma_receipt_root")
    validator = _existing_file(raw.get("gemma_receipt_validator_executable"),
                               "gemma_receipt_validator_executable")
    digests = {
        "gemma_delegate_sha256": raw.get("gemma_delegate_sha256"),
        "gemma_receipt_validator_sha256": raw.get("gemma_receipt_validator_sha256"),
        "gemma_model_digest": raw.get("gemma_model_digest"),
    }
    for field, digest in digests.items():
        if (not isinstance(digest, str) or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)):
            raise OrchestrationConfigError(f"{field}_invalid")
    timeout = raw.get("gemma_timeout_seconds", GEMMA_CERTIFIED_MIN_TIMEOUT_SECONDS)
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or float(timeout) < GEMMA_CERTIFIED_MIN_TIMEOUT_SECONDS):
        # Never silently shorten the delegate's own certified budget.
        raise OrchestrationConfigError("gemma_timeout_seconds_below_certified_budget")
    return {"local_backend": local_backend, "gemma_delegate_executable": delegate,
           "gemma_python_executable": python_executable, "gemma_receipt_root": receipt_root,
           "gemma_receipt_validator_executable": validator,
           **digests, "gemma_timeout_seconds": float(timeout)}


def load(path: str | Path) -> OrchestrationConfig:
    config_path = Path(path).expanduser()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise OrchestrationConfigError("config_unreadable") from exc
    if not isinstance(raw, dict) or set(raw) - _KEYS:
        raise OrchestrationConfigError("config_shape_invalid")
    if raw.get("config_version") != "1":
        raise OrchestrationConfigError("config_version_unsupported")
    interval = raw.get("interval_seconds", 5.0)
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or interval <= 0:
        raise OrchestrationConfigError("interval_seconds_invalid")
    state_root = _absolute_path(raw.get("state_root"), "state_root")
    local_root = _absolute_path(raw.get("local_queue_root"), "local_queue_root")
    capacity_db = _absolute_path(raw.get("capacity_db"), "capacity_db")
    worker = _absolute_path(raw.get("worker_executable"), "worker_executable")
    worker_state = _absolute_path(raw.get("worker_state"), "worker_state")
    execution_values = [raw.get("execution_queue_root"), raw.get("codex_task_executable"),
                        raw.get("claude_task_executable"), raw.get("python_executable")]
    if any(value is not None for value in execution_values) and not all(value is not None for value in execution_values):
        raise OrchestrationConfigError("execution_configuration_incomplete")
    execution_paths = ([None] * 4 if not any(value is not None for value in execution_values)
                       else [_absolute_path(value, field) for value, field in zip(execution_values,
                            ("execution_queue_root", "codex_task_executable",
                             "claude_task_executable", "python_executable"))])
    raw_config_dir = raw.get("claude_config_dir")
    claude_config_dir = (None if raw_config_dir is None
                         else _absolute_path(raw_config_dir, "claude_config_dir"))
    windows_paths = _windows_paths(raw)
    gemma_paths = _gemma_paths(raw)
    if capacity_db == local_root or capacity_db == state_root:
        raise OrchestrationConfigError("capacity_db_must_be_file")
    return OrchestrationConfig(
        state_root=state_root, local_queue_root=local_root,
        capacity_db=capacity_db, worker_executable=worker,
        worker_state=worker_state, interval_seconds=float(interval),
        execution_queue_root=execution_paths[0], codex_task_executable=execution_paths[1],
        claude_task_executable=execution_paths[2], python_executable=execution_paths[3],
        claude_config_dir=claude_config_dir,
        **windows_paths,
        **gemma_paths,
    )
