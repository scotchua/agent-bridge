"""Private state and identity checks for the Claude Code plugin install.

This is intentionally separate from clone-mode configuration.  A plugin cache
is replaceable, so the only durable data it may use lives below the user's
owner-only bridge state directory.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import stat
import subprocess
from pathlib import Path
from typing import Any, Iterator

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


# Trust policy (POSIX).  Every directory from "/" down to HOME is opened by
# descriptor, never re-resolved by path, so a component swapped after it was
# checked cannot redirect a later open.  Ancestors above HOME must be owned by
# the current user or root and must not be group or world writable unless the
# sticky bit is set (as on /tmp).  Symlinks on the way to HOME are followed
# only when owned by the current user or root.  HOME itself must be owned by
# the current user and not group or world writable.  The bridge's own
# directories and files below HOME must be owner-only, owned by the current
# user, and of the expected type.  POSIX ACLs are not inspected.
#
# Windows has no descriptor-relative opens in Python, so it keeps the
# path-based checks; the clone install remains the supported Windows path.
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_SEARCH = getattr(os, "O_PATH", 0) or getattr(os, "O_SEARCH", 0) or os.O_RDONLY
_DESCRIPTORS = (os.name != "nt" and hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY")
                and {os.open, os.stat, os.mkdir, os.readlink, os.rename, os.unlink}
                <= os.supports_dir_fd)
_MAX_LINKS = 40


def _check(observed: os.stat_result, *, directory_expected: bool) -> None:
    if stat.S_ISLNK(observed.st_mode) or _is_reparse(observed):
        raise PackagedStateError("packaged state must not contain symlinks or reparse points")
    expected_type = stat.S_ISDIR if directory_expected else stat.S_ISREG
    if not expected_type(observed.st_mode):
        raise PackagedStateError("packaged state has an unexpected file type")
    if os.name != "nt":
        if observed.st_uid != os.getuid():
            raise PackagedStateError("packaged state must be owned by the current user")
        # Owner-only, matching what this module creates.  Group or world read
        # access would expose the recorded interpreter and canary transcripts.
        if observed.st_mode & 0o077:
            raise PackagedStateError("packaged state must not be readable or writable by group or others")


def _check_ancestor(observed: os.stat_result) -> None:
    if observed.st_uid not in (os.getuid(), 0):
        raise PackagedStateError("home directory ancestors must be owned by the current user or root")
    if observed.st_mode & 0o022 and not observed.st_mode & stat.S_ISVTX:
        raise PackagedStateError("home directory ancestors must not be group or world writable unless sticky")


def _check_home(observed: os.stat_result) -> None:
    if not stat.S_ISDIR(observed.st_mode):
        raise PackagedStateError("home directory has an unexpected file type")
    if observed.st_uid != os.getuid():
        raise PackagedStateError("home directory must be owned by the current user")
    if observed.st_mode & 0o022:
        raise PackagedStateError("home directory must not be group or world writable")


def _walk_home(home: str) -> int:
    """Open HOME by descriptor from "/", checking every ancestor on the way."""
    pending = [part for part in reversed(os.path.abspath(home).split("/")) if part]
    stack: list[int] = []
    links = 0
    try:
        stack.append(os.open("/", _SEARCH | os.O_DIRECTORY | _CLOEXEC))
        while pending:
            part = pending.pop()
            if part == ".":
                continue
            if part == "..":
                if len(stack) > 1:
                    os.close(stack.pop())
                continue
            _check_ancestor(os.fstat(stack[-1]))
            observed = os.stat(part, dir_fd=stack[-1], follow_symlinks=False)
            if stat.S_ISLNK(observed.st_mode):
                links += 1
                if links > _MAX_LINKS:
                    raise PackagedStateError("home directory path has too many symlinks")
                if observed.st_uid not in (os.getuid(), 0):
                    raise PackagedStateError("home directory path symlinks must be owned by the current user or root")
                target = os.readlink(part, dir_fd=stack[-1])
                if not target:
                    raise PackagedStateError("home directory path has an empty symlink")
                if target.startswith("/"):
                    while len(stack) > 1:
                        os.close(stack.pop())
                pending.extend(item for item in reversed(target.split("/")) if item)
                continue
            if not stat.S_ISDIR(observed.st_mode):
                raise PackagedStateError("home directory has an unexpected file type")
            stack.append(os.open(part, _SEARCH | os.O_DIRECTORY | os.O_NOFOLLOW | _CLOEXEC,
                                 dir_fd=stack[-1]))
        _check_home(os.fstat(stack[-1]))
        return stack.pop()
    except OSError as exc:
        raise PackagedStateError("home directory cannot be checked safely") from exc
    finally:
        for fd in stack:
            os.close(fd)


def _refuse(parent: int, name: str, exc: OSError, *, directory_expected: bool) -> None:
    """Explain a refused no-follow open (symlink, wrong type), then fail closed."""
    try:
        observed = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except OSError:
        observed = None
    if observed is not None:
        _check(observed, directory_expected=directory_expected)
    raise PackagedStateError("packaged state cannot be read safely") from exc


def _open_child_dir(parent: int, name: str, *, create: bool) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _CLOEXEC
    created = False
    try:
        fd = os.open(name, flags, dir_fd=parent)
    except FileNotFoundError:
        if not create:
            raise PackagedStateError("packaged state is missing; run " + SETUP_COMMAND) from None
        # Only create components this feature owns.  Never repair a
        # pre-existing insecure directory: that would silently trust it.
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
            created = True
        except FileExistsError:
            pass
        try:
            fd = os.open(name, flags, dir_fd=parent)
        except OSError as exc:
            _refuse(parent, name, exc, directory_expected=True)
    except OSError as exc:
        _refuse(parent, name, exc, directory_expected=True)
    try:
        if created:
            os.fchmod(fd, 0o700)
        _check(os.fstat(fd), directory_expected=True)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _require_descriptors() -> None:
    if not _DESCRIPTORS:
        raise PackagedStateError("this platform cannot check packaged state safely")


@contextlib.contextmanager
def _state_directory(home: str, *, create: bool) -> Iterator[int]:
    """Yield a descriptor for HOME/.agent-bridge/packaged after full checks."""
    _require_descriptors()
    fds = [_walk_home(home)]
    try:
        fds.append(_open_child_dir(fds[-1], ".agent-bridge", create=create))
        fds.append(_open_child_dir(fds[-1], "packaged", create=create))
        yield fds[-1]
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _stat_child(parent: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _replace_at(source: str, destination: str, parent: int) -> None:
    os.replace(source, destination, src_dir_fd=parent, dst_dir_fd=parent)


def _validate(path: str, *, directory_expected: bool) -> os.stat_result:
    """Path-based check, used only on Windows."""
    try:
        observed = os.lstat(path)
    except OSError as exc:
        raise PackagedStateError(f"packaged state cannot be read safely: {path}") from exc
    _check(observed, directory_expected=directory_expected)
    return observed


def _mkdir_checked(path: str) -> None:
    """Path-based create, used only on Windows."""
    if os.path.lexists(path):
        _validate(path, directory_expected=True)
        return
    if not os.path.isdir(os.path.dirname(path)):
        raise PackagedStateError("packaged state parent is missing")
    os.mkdir(path, 0o700)
    _validate(path, directory_expected=True)


def _home_for(home: str | None) -> str:
    return os.path.dirname(state_root(home))


def prepare(home: str | None = None) -> None:
    if os.name == "nt" and not _DESCRIPTORS:
        _mkdir_checked(state_root(home))
        _mkdir_checked(directory(home))
        return
    with _state_directory(_home_for(home), create=True):
        pass


def validate_for_read(path: str) -> None:
    """Validate HOME, its ancestors, the state directories and the file."""
    root = state_root_for(path)
    name = os.path.basename(path)
    if os.name == "nt" and not _DESCRIPTORS:
        _validate(root, directory_expected=True)
        _validate(os.path.dirname(os.path.abspath(path)), directory_expected=True)
        _validate(path, directory_expected=False)
        return
    with _state_directory(os.path.dirname(root), create=False) as parent:
        observed = _stat_child(parent, name)
        if observed is None:
            raise PackagedStateError("packaged state is missing; run " + SETUP_COMMAND)
        _check(observed, directory_expected=False)


def state_root_for(path: str) -> str:
    # Config/receipt callers only pass the fixed files from this module.  The
    # helper still rejects an unexpected basename, preventing a peer workdir
    # from being smuggled in as a supposedly packaged path.
    absolute = os.path.abspath(path)
    parent = os.path.dirname(absolute)
    root = os.path.dirname(parent)
    if (os.path.basename(parent) != "packaged" or os.path.basename(root) != ".agent-bridge"
            or os.path.basename(absolute) not in PRIVATE_FILES):
        raise PackagedStateError("packaged state path is invalid")
    return root


def read_json(path: str) -> Any:
    """Read a private file through the checked state directory descriptor."""
    root = state_root_for(path)
    if os.name == "nt" and not _DESCRIPTORS:
        validate_for_read(path)
        return store.read_json(path)
    name = os.path.basename(path)
    with _state_directory(os.path.dirname(root), create=False) as parent:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | _CLOEXEC,
                         dir_fd=parent)
        except FileNotFoundError:
            raise PackagedStateError("packaged state is missing; run " + SETUP_COMMAND) from None
        except OSError as exc:
            _refuse(parent, name, exc, directory_expected=False)
        try:
            _check(os.fstat(fd), directory_expected=False)
            chunks = []
            while chunk := os.read(fd, 1024 * 1024):
                chunks.append(chunk)
        finally:
            os.close(fd)
    # utf-8-sig, matching store.read_json.
    return json.loads(b"".join(chunks).decode("utf-8-sig"))


def private_path(name: str, home: str | None = None) -> str:
    if name not in PRIVATE_FILES:
        raise PackagedStateError("packaged state path is invalid")
    return os.path.join(directory(home), name)


def write_json(path: str, value: Any, *, home: str | None = None) -> None:
    """The writer for config, receipt and candidate.

    canary-results.json is written by the canary runner, not here; setup reads
    it back through read_json, so it gets the same checks on the way in.
    """
    expected = private_path(os.path.basename(path), home)
    if os.path.normcase(os.path.abspath(path)) != os.path.normcase(os.path.abspath(expected)):
        raise PackagedStateError("packaged state must not be written outside its private directory")
    if os.name == "nt" and not _DESCRIPTORS:
        prepare(home)
        _validate(state_root(home), directory_expected=True)
        _validate(directory(home), directory_expected=True)
        if os.path.lexists(path):
            _validate(path, directory_expected=False)
        store.atomic_write_json(path, value)
        _validate(path, directory_expected=False)
        return
    name = os.path.basename(expected)
    payload = store.json_bytes(value)
    with _state_directory(_home_for(home), create=True) as parent:
        existing = _stat_child(parent, name)
        if existing is not None:
            _check(existing, directory_expected=False)
        temporary = f".tmp-{name}-{secrets.token_hex(8)}"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _CLOEXEC,
                     0o600, dir_fd=parent)
        try:
            try:
                os.fchmod(fd, 0o600)
                view = memoryview(payload)
                while view:
                    view = view[os.write(fd, view):]
                os.fsync(fd)
            finally:
                os.close(fd)
            _replace_at(temporary, name, parent)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary, dir_fd=parent)
            raise
        written = _stat_child(parent, name)
        if written is None:
            raise PackagedStateError("packaged state disappeared after writing")
        _check(written, directory_expected=False)
        with contextlib.suppress(OSError):
            os.fsync(parent)


def _files_for_digest(root: str) -> list[Path]:
    # __pycache__ is excluded from the digest because the launchers point
    # PYTHONPYCACHEPREFIX under /dev/null, where no cache can exist, so
    # bytecode is never imported.
    base = Path(root)
    ignored = {".git", "__pycache__", ".pytest_cache"}
    files: list[Path] = []
    for item in base.rglob("*"):
        relative = item.relative_to(base)
        if any(part in ignored for part in relative.parts):
            continue
        # A symlink could point outside the package, so its target would run
        # without being part of the verified identity.  Refuse, never skip.
        if item.is_symlink():
            raise PackagedStateError("agent-bridge plugin must not contain symlinks")
        if item.is_file():
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
