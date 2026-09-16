"""Owner-only filesystem primitives: secure mkdir, atomic write, flock, ledger.

Every directory this module creates is 0700 and every file 0600.  Writes are
atomic (same-directory temp plus os.replace) so a poll that races a worker, or
a poll after an MCP restart, never reads a HALF-WRITTEN document.

That is not the same as always reading a document, and the difference is the
whole of Windows.  os.replace is a clean swap on POSIX, where a reader sees the
old bytes or the new ones.  On Windows a reader can arrive in the instant the
path has no file at all, or be refused for sharing mid-replace.  This docstring
used to promise the stronger thing, and believing it is what let a live job
report as JOB_NOT_FOUND there.  Read our own atomically-written files with
read_json_atomic, which covers that window.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import time
from typing import Any, Iterator

from .platform import platform

DIR_MODE = 0o700
FILE_MODE = 0o600
WINDOWS = os.name == "nt"
REPLACE = os.replace
ATOMIC_REPLACE_GRACE_SECONDS = 1.0


def replace(source: str, destination: str) -> None:
    """Replace destination, retrying transient Windows sharing violations."""
    deadline = time.monotonic() + ATOMIC_REPLACE_GRACE_SECONDS
    while True:
        try:
            REPLACE(source, destination)
            return
        except PermissionError:
            if not WINDOWS or time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


def set_umask() -> None:
    platform.set_owner_only_umask()


def secure_mkdir(path: str) -> str:
    """Create path and any missing parents, every one of them 0700.

    os.makedirs(mode=...) applies the mode to the leaf only, and even that is
    masked by the caller's umask, so intermediate directories were inheriting
    whatever umask the calling process happened to have.  This creates each
    missing component explicitly and enforces the mode on it.

    Only directories this call creates are chmod'ed, plus the leaf if it already
    existed.  Pre-existing ancestors are left alone, so this can never loosen
    or tighten a directory outside the state tree.
    """
    path = os.path.abspath(path)
    missing: list[str] = []
    cursor = path
    while not os.path.isdir(cursor):
        missing.append(cursor)
        parent = os.path.dirname(cursor)
        if parent == cursor:
            break
        cursor = parent
    for directory in reversed(missing):
        try:
            os.mkdir(directory, DIR_MODE)
        except FileExistsError:
            pass
        except OSError:
            raise
        try:
            os.chmod(directory, DIR_MODE)
        except OSError:
            pass
    if not missing:
        try:
            if (os.stat(path).st_mode & 0o777) != DIR_MODE:
                os.chmod(path, DIR_MODE)
        except OSError:
            pass
    return path


def atomic_write_bytes(path: str, data: bytes, *, owner_only: bool = True) -> None:
    """Write data to path atomically.

    With ``owner_only`` (the default) the result is mode 0600 / an owner-only
    ACL, correct for this project's own state.  ``owner_only=False`` is for a
    path this project does not own -- a config file another tool created and
    manages (Claude Code's settings.json, Codex's config.toml/hooks.json) --
    where the temp file is left with whatever ACL its directory would give any
    new file, so the replace does not strip inherited access (e.g. SYSTEM,
    Administrators) that file already had before this project touched it.
    """
    directory = os.path.dirname(os.path.abspath(path))
    secure_mkdir(directory)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    try:
        try:
            if owner_only:
                platform.enforce_owner_only_file(fd)
        except BaseException:
            # The descriptor still belongs to this function until fdopen()
            # succeeds.  On Windows it must be closed before unlinking the
            # rejected temporary file, while preserving the ACL error itself.
            with contextlib.suppress(OSError):
                os.close(fd)
            raise
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def atomic_write_json(path: str, obj: Any) -> None:
    payload = json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False)
    atomic_write_bytes(path, payload.encode("utf-8") + b"\n")


def read_json(path: str) -> Any:
    with open(path, "rb") as handle:
        # utf-8-sig: a leading BOM (Windows editors, PowerShell defaults) is
        # stripped instead of raising; plain utf-8 files decode unchanged.
        return json.loads(handle.read().decode("utf-8-sig"))


# Long enough to cover a replace, short enough that a genuinely missing file
# is still reported promptly.
ATOMIC_READ_GRACE_SECONDS = 1.0


def read_json_atomic(path: str) -> Any:
    """Read a file this codebase wrote with atomic_write_bytes.

    os.replace is a clean swap on POSIX: a reader sees the old bytes or the new
    ones and never nothing. On Windows it is not. A reader can open the path in
    the instant it has no file, or be refused for sharing while the replace is
    in flight, and both surface as OSError.

    That is why a live job intermittently reported as JOB_NOT_FOUND on Windows
    and never once on macOS or Linux. Retry across the window; a file that is
    genuinely absent still raises once the grace is spent. Only OSError is
    retried, so a corrupt document still fails immediately and loudly.
    """
    deadline = time.monotonic() + ATOMIC_READ_GRACE_SECONDS
    while True:
        try:
            return read_json(path)
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


def read_json_or_none(path: str) -> Any:
    """A safe read: any reason the file cannot be read or parsed is ``None``.

    ``RecursionError`` joins the caught set for the same reason
    ``errors.py``'s own JSON-parsing paths already catch it (see the commit
    fixing ``_result_detail``): ``json.loads`` recurses per nesting level
    with no bound of its own, so a few tens of thousands of nested arrays or
    objects exhausts the interpreter's recursion limit rather than raising
    a parse error. Every caller of this function already treats a read
    failure as "the document is not there to trust"; a document too deeply
    nested to even finish parsing is exactly that, not a different case.
    """
    try:
        return read_json(path)
    except (OSError, ValueError, RecursionError):
        return None


@contextlib.contextmanager
def file_lock(lock_path: str, timeout: float = 10.0) -> Iterator[None]:
    """Exclusive advisory lock, used to serialise registry and ledger writes."""
    secure_mkdir(os.path.dirname(os.path.abspath(lock_path)))
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, FILE_MODE)
    try:
        with platform.lock_exclusive(fd, lock_path, timeout):
            yield
    finally:
        os.close(fd)


def append_ledger(ledger_path: str, record: dict[str, Any]) -> None:
    """Append one JSON record to the append-only ledger, under lock, fsynced."""
    secure_mkdir(os.path.dirname(os.path.abspath(ledger_path)))
    line = json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n"
    with file_lock(ledger_path + ".lock"):
        fd = os.open(ledger_path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, FILE_MODE)
        try:
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds")
