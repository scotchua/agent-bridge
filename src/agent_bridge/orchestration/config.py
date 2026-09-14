"""Strict configuration for the additive orchestration MCP server."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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


_KEYS = frozenset({
    "config_version", "state_root", "local_queue_root", "capacity_db",
    "worker_executable", "worker_state", "interval_seconds",
    "execution_queue_root", "codex_task_executable", "claude_task_executable",
    "python_executable",
})


def _absolute_path(value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise OrchestrationConfigError(f"{field}_invalid")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise OrchestrationConfigError(f"{field}_must_be_absolute")
    return path


def load(path: str | Path) -> OrchestrationConfig:
    config_path = Path(path).expanduser()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
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
    if capacity_db == local_root or capacity_db == state_root:
        raise OrchestrationConfigError("capacity_db_must_be_file")
    return OrchestrationConfig(
        state_root=state_root, local_queue_root=local_root,
        capacity_db=capacity_db, worker_executable=worker,
        worker_state=worker_state, interval_seconds=float(interval),
        execution_queue_root=execution_paths[0], codex_task_executable=execution_paths[1],
        claude_task_executable=execution_paths[2], python_executable=execution_paths[3],
    )
