"""Safe, content-free revision markers for long-running bridge processes."""
from __future__ import annotations

import os
import re
import subprocess
import time
from typing import Any

from . import config, store
from .execution import hostenv

_REVISION = re.compile(r"^[0-9a-f]{40,64}$")
_HEAD_CACHE_SECONDS = 3.0
_head_cache: dict[str, tuple[float, str | None]] = {}


def _git_environment() -> dict[str, str]:
    """Use the execution lanes' non-interactive, config-free git environment."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": os.path.expanduser("~"), "LANG": "C.UTF-8",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_CONFIG_SYSTEM": os.devnull, "GIT_TERMINAL_PROMPT": "0"}
    for name in ("USER", "LOGNAME"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    if os.name == "nt":
        for name in ("SYSTEMROOT", "COMSPEC", "PATHEXT", "USERPROFILE"):
            if os.environ.get(name):
                env[name] = os.environ[name]
    return env


def current_code_revision(checkout: str | None = None) -> str | None:
    """Return the checkout HEAD, or ``None`` when it cannot be proved."""
    root = os.path.abspath(checkout or config.REPO_ROOT)
    now = time.monotonic()
    cached = _head_cache.get(root)
    if cached is not None and now - cached[0] < _HEAD_CACHE_SECONDS:
        return cached[1]
    env = _git_environment()
    try:
        result = subprocess.run(
            [str(hostenv.resolve_git(env)), "--no-optional-locks", "-C", root,
             "rev-parse", "HEAD"], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5,
            check=False, shell=False, env=env)
    except (OSError, subprocess.SubprocessError, hostenv.HostCapabilityError):
        value = None
    else:
        value = result.stdout.decode("ascii", "ignore").strip().lower()
        value = value if result.returncode == 0 and _REVISION.fullmatch(value) else None
    _head_cache[root] = (now, value)
    return value


def capture(checkout: str | None = None) -> str | None:
    return current_code_revision(checkout)


def report(loaded: str | None, *, checkout: str | None = None) -> dict[str, Any]:
    """Report only an immutable revision and a conservative stale verdict."""
    current = current_code_revision(checkout)
    if loaded is None or current is None:
        return {"code_revision": loaded or "unavailable", "code_stale": True}
    return {"code_revision": loaded, "code_stale": loaded != current}


def record(path: str, loaded: str | None, *, pid: int | None = None,
           started_at: str | None = None) -> None:
    value: dict[str, Any] = {"code_revision": loaded or "unavailable"}
    if pid is not None:
        value["pid"] = pid
    if started_at is not None:
        value["started_at"] = started_at
    store.atomic_write_json(path, value)


def process_record_path(state_root: str, kind: str, caller: str, pid: int | None = None) -> str:
    """Return the fixed, owner-state marker name for one server process."""
    return os.path.join(state_root, f"{kind}-{caller}-{pid if pid is not None else os.getpid()}.json")


def remove_record(path: str) -> None:
    """Remove a marker only after its caller derived the fixed local pathname."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def read_record(path: str, *, checkout: str | None = None) -> dict[str, Any]:
    value = store.read_json_or_none(path)
    loaded = value.get("code_revision") if isinstance(value, dict) else None
    if not isinstance(loaded, str) or (loaded != "unavailable" and not _REVISION.fullmatch(loaded)):
        loaded = None
    return report(loaded, checkout=checkout)
