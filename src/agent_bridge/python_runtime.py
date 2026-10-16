"""The owner-only interpreter record consumed by POSIX launchers."""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

from . import store

RECORD_NAME = "launcher-python"
LAUNCHERS = (
    "agent-bridge-admin",
    "agent-bridge-execution-worker",
    "agent-bridge-gate-hook",
    "agent-bridge-mcp",
    "agent-bridge-orchestration",
    "agent-bridge-orchestration-verify",
    "agent-bridge-setup",
    "agent-bridge-windows-setup",
)


def record_path(home: str | None = None) -> str:
    base = os.path.abspath(os.path.expanduser(home or os.environ.get("HOME", "~")))
    return os.path.join(base, ".agent-bridge", RECORD_NAME)


def record(executable: str | None = None, *, home: str | None = None) -> str:
    """Atomically save the interpreter that completed an installation path."""
    path = os.path.abspath(executable or sys.executable)
    store.atomic_write_bytes(record_path(home), (path + "\n").encode("utf-8"))
    return path


def recorded(home: str | None = None) -> str | None:
    try:
        with open(record_path(home), encoding="utf-8") as handle:
            value = handle.readline().rstrip("\r\n")
    except OSError:
        return None
    return value or None


def _probe(path: str) -> tuple[bool, str | None]:
    """Whether an existing recorded path has the launcher-compatible Python."""
    if not os.path.isfile(path) or not os.access(path, os.X_OK):
        return False, None
    try:
        result = subprocess.run(
            [path, "-P", "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=5, check=False, shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False, None
    version = result.stdout.decode("ascii", "replace").strip()
    return result.returncode == 0, version or None


def _path_fallback() -> str | None:
    """Mirror the launcher's newest-first PATH fallback for doctor output."""
    import shutil
    for name in ("python3.14", "python3.13", "python3.12", "python3.11", "python3"):
        candidate = shutil.which(name)
        if candidate and _probe(candidate)[0]:
            return candidate
    return None


def doctor(home: str | None = None) -> dict[str, Any]:
    """Report the common launcher selection and any stale recorded path."""
    path = record_path(home)
    selected = os.environ.get("AGENT_BRIDGE_PYTHON") or recorded(home)
    source = "AGENT_BRIDGE_PYTHON" if os.environ.get("AGENT_BRIDGE_PYTHON") else "recorded"
    issue = None
    if selected is None:
        source = "PATH fallback"
        selected = _path_fallback()
    elif source == "recorded":
        if not os.path.exists(selected):
            issue = "recorded interpreter no longer exists"
            source = "PATH fallback (recorded path missing)"
            selected = _path_fallback()
        else:
            # The current process already established this interpreter's
            # version. Avoid a redundant child process on ordinary repeated
            # doctor runs; another recorded path is checked explicitly.
            if os.path.realpath(selected) == os.path.realpath(sys.executable):
                ok, version = sys.version_info >= (3, 11), ".".join(map(str, sys.version_info[:3]))
            else:
                ok, version = _probe(selected)
            if not ok:
                issue = "recorded interpreter is too old or does not support -P"
            elif version:
                selected = os.path.abspath(selected)
    return {
        "record_path": path,
        "recorded": recorded(home),
        "recorded_issue": issue,
        "launchers": {name: {"interpreter": selected, "source": source} for name in LAUNCHERS},
    }
