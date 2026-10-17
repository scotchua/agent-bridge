"""The owner-only interpreter record consumed by POSIX launchers."""
from __future__ import annotations

import os
import stat
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


def record(executable: str | None = None, *, home: str | None = None) -> str | None:
    """Best-effort save of the interpreter that completed an installation path.

    This is a launcher convenience record, never a reason to fail an install
    or a diagnostic command after its substantive work has completed.
    """
    path = os.path.abspath(executable or sys.executable)
    try:
        store.atomic_write_bytes(record_path(home), (path + "\n").encode("utf-8"))
    except OSError:
        return None
    return path


def _recorded_value(home: str | None = None) -> tuple[str | None, str | None]:
    """Return a safe launcher record, or the fixed reason it was rejected."""
    path = record_path(home)
    parent = os.path.dirname(path)
    try:
        record_status = os.lstat(path)
        parent_status = os.stat(parent)
    except FileNotFoundError:
        return None, None
    except OSError:
        return None, "record_not_owner_only"
    uid = os.getuid() if hasattr(os, "getuid") else None
    owner_only = (
        stat.S_ISREG(record_status.st_mode)
        and (uid is None or record_status.st_uid == uid)
        and not (record_status.st_mode & (stat.S_IWGRP | stat.S_IWOTH))
        and stat.S_ISDIR(parent_status.st_mode)
        and (uid is None or parent_status.st_uid == uid)
        and not (parent_status.st_mode & (stat.S_IWGRP | stat.S_IWOTH))
    )
    if not owner_only:
        return None, "record_not_owner_only"
    try:
        with open(path, encoding="utf-8") as handle:
            value = handle.readline().rstrip("\r\n")
    except OSError:
        return None, "record_not_owner_only"
    if not value:
        return None, "record_invalid_interpreter"
    try:
        executable_status = os.stat(value) if os.path.isabs(value) else None
    except OSError:
        executable_status = None
    if (executable_status is None or not stat.S_ISREG(executable_status.st_mode)
            or not os.access(value, os.X_OK)):
        return value, "record_invalid_interpreter"
    return value, None


def recorded(home: str | None = None) -> str | None:
    """Return the recorded interpreter only when its trust boundary holds."""
    value, issue = _recorded_value(home)
    return value if issue is None else None


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
    recorded_path, record_issue = _recorded_value(home)
    selected = os.environ.get("AGENT_BRIDGE_PYTHON") or (recorded_path if record_issue is None else None)
    source = "AGENT_BRIDGE_PYTHON" if os.environ.get("AGENT_BRIDGE_PYTHON") else "recorded"
    issue = record_issue
    if selected is None:
        source = "PATH fallback" if issue is None else "PATH fallback (record rejected)"
        selected = _path_fallback()
    elif source == "recorded":
        # The current process already established this interpreter's version.
        # Avoid a redundant child process on ordinary repeated doctor runs;
        # another recorded path is checked explicitly.
        if os.path.realpath(selected) == os.path.realpath(sys.executable):
            ok, version = sys.version_info >= (3, 11), ".".join(map(str, sys.version_info[:3]))
        else:
            ok, version = _probe(selected)
        if not ok:
            issue = "record_incompatible_interpreter"
            source = "PATH fallback (record rejected)"
            selected = _path_fallback()
        elif version:
            selected = os.path.abspath(selected)
    return {
        "record_path": path,
        "recorded": recorded_path,
        "recorded_issue": issue,
        "launchers": {name: {"interpreter": selected, "source": source} for name in LAUNCHERS},
    }
