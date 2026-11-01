"""Private state and identity checks for the Claude Code plugin install.

This is intentionally separate from clone-mode configuration.  A plugin cache
is replaceable, so the only durable data it may use lives below the user's
owner-only bridge state directory.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

from . import store

PACKAGED_ENV = "AGENT_BRIDGE_PACKAGED"
PLUGIN_ROOT_ENV = "AGENT_BRIDGE_PLUGIN_ROOT"
SETUP_COMMAND = "/agent-bridge:setup"
MARKER = ".claude-plugin/plugin.json"
CONFIG_NAME = "config.json"
RECEIPT_NAME = "verification.json"
PRIVATE_FILES = frozenset({CONFIG_NAME, RECEIPT_NAME, "candidate.json", "canary-results.json"})


class PackagedStateError(ValueError):
    """The plugin's private state is absent or cannot safely be trusted."""


def plugin_root() -> str:
    """Return the shipped plugin root without guessing from an install path."""
    configured = os.environ.get(PLUGIN_ROOT_ENV)
    root = os.path.abspath(os.path.expanduser(configured)) if configured else str(
        Path(__file__).resolve().parents[2])
    return root


def enabled() -> bool:
    """Packaged mode is an explicit opt-in plus a shipped marker.

    Looking only at a path is unsafe: a clone can happen to have the same
    directory names as Claude's cache.  The launcher sets the environment bit
    and the marker proves the code it launched was actually packaged.
    """
    # Require the launcher-supplied root as well as the mode bit. A clone has
    # marketplace metadata too, so an inherited AGENT_BRIDGE_PACKAGED alone
    # must never retarget that clone's configuration.
    return (os.environ.get(PACKAGED_ENV) == "1"
            and bool(os.environ.get(PLUGIN_ROOT_ENV))
            and os.path.isfile(os.path.join(plugin_root(), MARKER)))


def state_root(home: str | None = None) -> str:
    base = os.path.abspath(os.path.expanduser(home or os.environ.get("HOME", "~")))
    return os.path.join(base, ".agent-bridge")


def directory(home: str | None = None) -> str:
    return os.path.join(state_root(home), "packaged")


def config_path(home: str | None = None) -> str:
    return os.path.join(directory(home), CONFIG_NAME)


def receipt_path(home: str | None = None) -> str:
    return os.path.join(directory(home), RECEIPT_NAME)


def _is_reparse(st: os.stat_result) -> bool:
    return bool(getattr(st, "st_file_attributes", 0) & 0x400)


def _validate(path: str, *, directory_expected: bool) -> os.stat_result:
    try:
        observed = os.lstat(path)
    except OSError as exc:
        raise PackagedStateError(f"packaged state cannot be read safely: {path}") from exc
    if stat.S_ISLNK(observed.st_mode) or _is_reparse(observed):
        raise PackagedStateError("packaged state must not contain symlinks or reparse points")
    if directory_expected != stat.S_ISDIR(observed.st_mode):
        raise PackagedStateError("packaged state has an unexpected file type")
    if os.name != "nt":
        if observed.st_uid != os.getuid():
            raise PackagedStateError("packaged state must be owned by the current user")
        if observed.st_mode & 0o022:
            raise PackagedStateError("packaged state must not be group or world writable")
    return observed


def _mkdir_checked(path: str) -> None:
    if os.path.lexists(path):
        _validate(path, directory_expected=True)
        return
    # Only create components this feature owns.  Do not repair a pre-existing
    # insecure directory: that would turn an unsafe read into a silent trust.
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        raise PackagedStateError("packaged state parent is missing")
    os.mkdir(path, 0o700)
    if os.name != "nt":
        os.chmod(path, 0o700)
    _validate(path, directory_expected=True)


def prepare(home: str | None = None) -> None:
    root = state_root(home)
    _mkdir_checked(root)
    _mkdir_checked(directory(home))


def validate_for_read(path: str) -> None:
    """Validate all state components under our control before opening a file."""
    root = state_root_for(path)
    _validate(root, directory_expected=True)
    _validate(os.path.dirname(path), directory_expected=True)
    _validate(path, directory_expected=False)


def state_root_for(path: str) -> str:
    # Config/receipt callers only pass the fixed files from this module.  The
    # helper still rejects an unexpected basename, preventing a peer workdir
    # from being smuggled in as a supposedly packaged path.
    absolute = os.path.abspath(path)
    parent = os.path.dirname(absolute)
    if os.path.basename(parent) != "packaged" or os.path.basename(absolute) not in PRIVATE_FILES:
        raise PackagedStateError("packaged state path is invalid")
    return os.path.dirname(parent)


def read_json(path: str) -> Any:
    validate_for_read(path)
    return store.read_json(path)


def write_json(path: str, value: Any, *, home: str | None = None) -> None:
    expected = config_path(home) if os.path.basename(path) == CONFIG_NAME else receipt_path(home)
    if os.path.normcase(os.path.abspath(path)) != os.path.normcase(os.path.abspath(expected)):
        raise PackagedStateError("packaged state must not be written outside its private directory")
    prepare(home)
    # Validate immediately before the atomic replace as well as before every
    # read.  store.atomic_write_json uses a same-directory temp and os.replace.
    _validate(state_root(home), directory_expected=True)
    _validate(directory(home), directory_expected=True)
    if os.path.lexists(path):
        _validate(path, directory_expected=False)
    store.atomic_write_json(path, value)
    _validate(path, directory_expected=False)


def _files_for_digest(root: str) -> list[Path]:
    base = Path(root)
    ignored = {".git", "__pycache__", ".pytest_cache"}
    files: list[Path] = []
    for item in base.rglob("*"):
        relative = item.relative_to(base)
        if any(part in ignored for part in relative.parts):
            continue
        if item.is_file() and not item.is_symlink():
            files.append(item)
    return sorted(files, key=lambda item: item.relative_to(base).as_posix())


def package_identity(root: str | None = None) -> dict[str, str]:
    root = os.path.abspath(root or plugin_root())
    marker = os.path.join(root, MARKER)
    try:
        raw = json.loads(Path(marker).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise PackagedStateError("agent-bridge plugin marker is missing or invalid") from exc
    version = raw.get("version") if isinstance(raw, dict) else None
    if not isinstance(version, str) or not version:
        raise PackagedStateError("agent-bridge plugin version is missing")
    digest = hashlib.sha256()
    for item in _files_for_digest(root):
        relative = item.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        with item.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    return {"version": version, "digest": digest.hexdigest()}


def record_verified(identity: dict[str, str], *, python: str | None = None,
                    home: str | None = None) -> None:
    if set(identity) != {"version", "digest"}:
        raise PackagedStateError("invalid packaged identity")
    path = receipt_path(home)
    previous: dict[str, Any] = {}
    if os.path.exists(path):
        loaded = read_json(path)
        if not isinstance(loaded, dict):
            raise PackagedStateError("packaged verification record is invalid")
        previous = loaded
    known = previous.get("verified", [])
    if not isinstance(known, list) or not all(isinstance(item, dict) for item in known):
        raise PackagedStateError("packaged verification record is invalid")
    if identity not in known:
        known.append(identity)
    value: dict[str, Any] = {"version": 1, "verified": known,
                             "current": identity}
    if python is not None:
        value["launcher_python"] = python
    elif isinstance(previous.get("launcher_python"), str):
        value["launcher_python"] = previous["launcher_python"]
    write_json(path, value, home=home)


def verification_status() -> tuple[bool, str]:
    """Return whether this exact package was verified; known rollback is OK."""
    try:
        identity = package_identity()
        receipt = read_json(receipt_path())
    except (OSError, ValueError, PackagedStateError):
        return False, f"agent-bridge plugin setup is required; run {SETUP_COMMAND}"
    if not isinstance(receipt, dict) or receipt.get("version") != 1:
        return False, f"agent-bridge plugin setup is required; run {SETUP_COMMAND}"
    known = receipt.get("verified")
    if not isinstance(known, list) or identity not in known:
        return False, f"agent-bridge plugin changed; run {SETUP_COMMAND}"
    return True, ""


def recorded_python() -> str | None:
    """Return the setup-recorded interpreter only after state validation."""
    try:
        value = read_json(receipt_path())
    except (OSError, ValueError, PackagedStateError):
        return None
    candidate = value.get("launcher_python") if isinstance(value, dict) else None
    if not isinstance(candidate, str) or not os.path.isabs(candidate):
        return None
    try:
        observed = os.stat(candidate)
        if not stat.S_ISREG(observed.st_mode):
            return None
        probe = subprocess.run(
            [candidate, "-P", "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=5, check=False, shell=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return candidate if probe.returncode == 0 else None
