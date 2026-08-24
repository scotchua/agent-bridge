"""Owner-only filesystem primitives: secure mkdir, atomic write, flock, ledger.

Every directory this module creates is 0700 and every file 0600.  Writes are
atomic (same-directory temp plus os.replace) so a poll that races a worker, or
a poll after an MCP restart, always reads a complete document.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import tempfile
import time
from typing import Any, Iterator

DIR_MODE = 0o700
FILE_MODE = 0o600


def set_umask() -> None:
    os.umask(0o077)


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


def atomic_write_bytes(path: str, data: bytes) -> None:
    """Write data to path atomically with mode 0600."""
    directory = os.path.dirname(os.path.abspath(path))
    secure_mkdir(directory)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    try:
        os.fchmod(fd, FILE_MODE)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
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
        return json.loads(handle.read().decode("utf-8"))


def read_json_or_none(path: str) -> Any:
    try:
        return read_json(path)
    except (OSError, ValueError):
        return None


@contextlib.contextmanager
def file_lock(lock_path: str, timeout: float = 10.0) -> Iterator[None]:
    """Exclusive advisory lock, used to serialise registry and ledger writes."""
    secure_mkdir(os.path.dirname(os.path.abspath(lock_path)))
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, FILE_MODE)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"could not lock {lock_path}")
                time.sleep(0.02)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
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
