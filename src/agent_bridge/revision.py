"""Safe, content-free revision markers for long-running bridge processes."""
from __future__ import annotations

import os
import re
import subprocess
from typing import Any

from . import config, store

_REVISION = re.compile(r"^[0-9a-f]{40,64}$")


def current_code_revision(checkout: str | None = None) -> str | None:
    """Return the checkout HEAD, or ``None`` when it cannot be proved."""
    root = os.path.abspath(checkout or config.REPO_ROOT)
    try:
        result = subprocess.run(
            ["git", "-C", root, "rev-parse", "HEAD"], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5,
            check=False, shell=False)
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.decode("ascii", "ignore").strip().lower()
    return value if result.returncode == 0 and _REVISION.fullmatch(value) else None


def capture(checkout: str | None = None) -> str | None:
    return current_code_revision(checkout)


def report(loaded: str | None, *, checkout: str | None = None) -> dict[str, Any]:
    """Report only an immutable revision and a conservative stale verdict."""
    current = current_code_revision(checkout)
    if loaded is None or current is None:
        return {"code_revision": loaded or "unavailable", "code_stale": True}
    return {"code_revision": loaded, "code_stale": loaded != current}


def record(path: str, loaded: str | None) -> None:
    store.atomic_write_json(path, {"code_revision": loaded or "unavailable"})


def read_record(path: str, *, checkout: str | None = None) -> dict[str, Any]:
    value = store.read_json_or_none(path)
    loaded = value.get("code_revision") if isinstance(value, dict) else None
    if not isinstance(loaded, str) or (loaded != "unavailable" and not _REVISION.fullmatch(loaded)):
        loaded = None
    return report(loaded, checkout=checkout)
