"""Conservative reader for operator-maintained task closure records."""

from __future__ import annotations

import json
import os
from pathlib import Path


_CLOSED = frozenset({"closed", "complete", "completed", "done"})


def _safe_item_id(item_id: object) -> bool:
    """Whether an identifier can name exactly one child of a task root."""
    return (isinstance(item_id, str) and bool(item_id) and not item_id.startswith(".")
            and ".." not in item_id and "/" not in item_id and "\\" not in item_id
            and (os.sep == "/" or os.sep not in item_id)
            and (os.altsep is None or os.altsep not in item_id))


def task_record_state(tasks_root: str | Path, item_id: object) -> str:
    """Return ``closed``, ``not_closed``, or ``no_record`` for one task.

    All parse, permission, containment, and shape failures are conservative.
    The caller gets no text from task.json, only this narrow classification.
    """
    if not _safe_item_id(item_id):
        return "no_record"
    try:
        root = Path(tasks_root).resolve(strict=True)
        record = (root / item_id / "task.json").resolve(strict=True)
        record.relative_to(root)
        if not record.is_file():
            return "no_record"
    except FileNotFoundError:
        return "no_record"
    except (OSError, ValueError):
        return "not_closed"
    try:
        value = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return "not_closed"
    if not isinstance(value, dict):
        return "not_closed"
    for key in ("status", "state"):
        status = value.get(key)
        if isinstance(status, str) and status.casefold() in _CLOSED:
            return "closed"
    return "not_closed"


def is_task_closed(tasks_root: str | Path, item_id: object) -> bool:
    """Whether the closed task record for ``item_id`` is safely readable."""
    return task_record_state(tasks_root, item_id) == "closed"
