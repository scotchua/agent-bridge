"""Owner-only directories and files, enforced *and* verified, on both systems.

Three separate mistakes live in this area, and each one has a function here.

* Requesting protection and assuming it. ``mkdir(mode=0o700)`` is a no-op on
  Windows, so a directory created that way inherits whatever its parent grants
  and nothing says so. :func:`require_private_directory` applies the ACL
  through the platform layer and reads it back.
* Writing sensitive bytes into a file that is not yet protected, or truncating
  a good record before the replacement is known to be writable.
  :func:`atomic_private_write` writes a fresh, protected temporary file and
  replaces the target only once the bytes are on disk.
* Trusting a path because its final component looked right.
  :func:`require_private_file` checks the whole ancestry for a junction,
  symlink or other reparse point, checks that the target is a regular file,
  and returns its identity so the caller can confirm it did not change between
  the check and the read.
* Checking one file and reading another. Every caller that verified a path and
  then opened that path again to get the bytes had a window between the two.
  :func:`read_private_file` closes it by returning the bytes from the
  descriptor it verified, and is what the evidence, enrolment and resume
  records are read through.

Every refusal is a fixed, path-free code, because these codes end up in
receipts and an operator-facing status line.
"""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, NamedTuple

from ..platform import platform as host_platform


class PrivacyError(RuntimeError):
    """A fixed reason code. Never an OS message and never a path."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class FileIdentity(NamedTuple):
    """Enough of a stat to notice the file was swapped underneath us."""

    device: int
    inode: int
    size: int
    mtime_ns: int


def _reparse_tag(info: os.stat_result) -> int:
    # Present only on Windows, and 0 for an ordinary directory or file there.
    return int(getattr(info, "st_reparse_tag", 0) or 0)


def assert_no_reparse_ancestors(path: str | os.PathLike[str], *,
                                stop: str | os.PathLike[str] | None = None) -> None:
    """Refuse a path reached through a link, junction or mount point.

    A verified ACL on the final component says nothing if an ancestor is a
    junction pointing somewhere with a different one. Checked from the target
    upwards with ``lstat``, so the link itself is examined rather than what it
    points at.

    ``stop`` is the trusted boundary, checked and then not passed: the
    configured runtime or queue root. Everything from the target up to and
    including that root is this program's own layout and must be free of
    links. What is *above* the root is the operator's machine layout, which
    this cannot police and does not claim to: an installation directory
    deliberately placed behind a mount point is the operator's decision. With
    no ``stop``, only the target and its immediate parent are checked, which
    is the weaker guarantee and is stated as such.
    """

    target = Path(path).absolute()
    boundary = Path(stop).absolute() if stop is not None else target.parent
    for candidate in (target, *target.parents):
        try:
            info = os.lstat(candidate)
        except FileNotFoundError:
            # An absent component cannot be a reparse point; the caller's own
            # existence checks decide whether absence is acceptable.
            info = None
        except OSError as exc:
            raise PrivacyError("path_unreadable") from exc
        if info is not None and (stat.S_ISLNK(info.st_mode) or _reparse_tag(info)):
            raise PrivacyError("path_traverses_a_link")
        if candidate == boundary or candidate == candidate.parent:
            return


def require_private_directory(path: str | os.PathLike[str], *,
                              root: str | os.PathLike[str] | None = None,
                              platform: Any = None) -> None:
    """Apply and read back an owner-only ACL on a directory. Fail closed.

    Called before anything sensitive is written inside, not afterwards. A
    directory whose protection cannot be established is one this process must
    not use, which is different from one whose protection was never requested.
    """

    target = Path(path)
    if not target.is_dir():
        raise PrivacyError("directory_absent")
    assert_no_reparse_ancestors(target, stop=root)
    active = platform if platform is not None else host_platform
    verify = getattr(active, "verify_owner_only_path", None)
    if verify is None:
        raise PrivacyError("acl_unenforceable")
    probe = target / ".agent-bridge-acl-probe"
    descriptor = None
    try:
        flags = os.O_CREAT | os.O_RDWR | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(probe, flags, 0o600)
        except FileExistsError:
            # A leftover probe from an interrupted run. Replace it rather than
            # trusting a file this process did not just create.
            with contextlib.suppress(OSError):
                os.unlink(probe)
            descriptor = os.open(probe, flags, 0o600)
        except OSError as exc:
            raise PrivacyError("acl_unverified") from exc
        try:
            verified, _evidence = verify(str(target), str(probe))
        except (OSError, AttributeError, NotImplementedError) as exc:
            raise PrivacyError("acl_unverified") from exc
        if not verified:
            raise PrivacyError("directory_not_owner_only")
    finally:
        if descriptor is not None:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        with contextlib.suppress(OSError):
            os.unlink(probe)


def require_private_file(path: str | os.PathLike[str], *,
                         root: str | os.PathLike[str] | None = None,
                         platform: Any = None) -> FileIdentity:
    """Confirm a file is a regular file this owner alone can read.

    Returns its identity so the caller can re-check after reading. Everything
    this function proves is proven about the object the descriptor refers to,
    not about the name that was passed in.
    """

    target = Path(path)
    assert_no_reparse_ancestors(target, stop=root)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(target, flags)
    except FileNotFoundError as exc:
        raise PrivacyError("file_absent") from exc
    except OSError as exc:
        raise PrivacyError("file_unreadable") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise PrivacyError("file_not_regular")
        active = platform if platform is not None else host_platform
        verify = getattr(active, "verify_owner_only_path", None)
        if verify is None:
            raise PrivacyError("acl_unenforceable")
        try:
            verified, _evidence = verify(str(target.parent), str(target))
        except (OSError, AttributeError, NotImplementedError) as exc:
            raise PrivacyError("acl_unverified") from exc
        if not verified:
            raise PrivacyError("file_not_owner_only")
        return FileIdentity(info.st_dev, info.st_ino, info.st_size,
                            info.st_mtime_ns)
    finally:
        os.close(descriptor)


#: Everything this module reads is a small control file: an evidence record,
#: an enrolment record, a protected blob, a resume record. Anything larger is
#: not one of ours, and reading it to find that out is the mistake.
MAX_PRIVATE_FILE_BYTES = 1024 * 1024


def read_private_file(path: str | os.PathLike[str], *,
                      root: str | os.PathLike[str] | None = None,
                      platform: Any = None,
                      max_bytes: int = MAX_PRIVATE_FILE_BYTES
                      ) -> tuple[bytes, FileIdentity]:
    """Verify and read in one descriptor. The reason this exists is the gap.

    :func:`require_private_file` proves things about the object a descriptor
    refers to and then closes it. Every caller then opened the *name* again to
    get the bytes, which means everything proven was proven about a file that
    may no longer be the one being read. Re-checking the identity afterwards
    narrows the window; it does not close it, because the read in between
    already happened against whatever was there.

    So the bytes come out of the descriptor that was verified, and the
    identity is re-taken from that same descriptor afterwards. A file swapped
    underneath this call is not read at all: the descriptor still refers to
    the object that passed the checks, and if that object was truncated,
    extended or replaced in place, the second ``fstat`` says so.

    The ACL check prefers the platform's handle-bound path where one exists.
    On Windows that resolves the descriptor to the object it actually refers
    to, so a name substituted after the open cannot redirect the check.
    """

    target = Path(path)
    assert_no_reparse_ancestors(target, stop=root)
    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    try:
        descriptor = os.open(target, flags)
    except FileNotFoundError as exc:
        raise PrivacyError("file_absent") from exc
    except OSError as exc:
        raise PrivacyError("file_unreadable") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise PrivacyError("file_not_regular")
        if info.st_size > max_bytes:
            raise PrivacyError("file_too_large")
        _verify_descriptor_acl(descriptor, target, platform)
        payload = _read_all(descriptor, info.st_size, max_bytes)
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
                info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns):
            raise PrivacyError("file_changed_while_reading")
        return payload, FileIdentity(info.st_dev, info.st_ino, info.st_size,
                                     info.st_mtime_ns)
    finally:
        os.close(descriptor)


def _verify_descriptor_acl(descriptor: int, target: Path, platform: Any) -> None:
    """Owner-only, checked against the descriptor, and never repaired here.

    Reading does not fix. That distinction matters more here than anywhere
    else in this module: a record another account could write may already have
    been written by one, and tightening its permissions on the way past would
    turn the one observable sign of that into a silent repair. Enforcement
    belongs on the write path, where the bytes are ours.

    Where the platform can resolve a descriptor back to the object it refers
    to, the ACL is checked against *that* name rather than the one the caller
    passed. A name substituted after the open then cannot redirect the check.
    """

    active = platform if platform is not None else host_platform

    # The descriptor's own metadata, which no rename or replacement can alter.
    if hasattr(os, "getuid"):
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid():
            raise PrivacyError("file_owned_by_another_account")
        if info.st_mode & 0o077:
            raise PrivacyError("file_not_owner_only")

    verify = getattr(active, "verify_owner_only_path", None)
    if verify is None:
        raise PrivacyError("acl_unenforceable")
    checked = _descriptor_path(descriptor, target, active)
    try:
        verified, _evidence = verify(str(checked.parent), str(checked))
    except (OSError, AttributeError, NotImplementedError) as exc:
        raise PrivacyError("acl_unverified") from exc
    if not verified:
        raise PrivacyError("file_not_owner_only")


def _descriptor_path(descriptor: int, fallback: Path, active: Any) -> Path:
    """The path the open descriptor actually refers to, where that is knowable.

    On Windows this is ``GetFinalPathNameByHandleW``, which answers about the
    handle rather than about any name. On POSIX the descriptor-level checks
    above already carry the weight, so the caller's path is used unchanged.
    """

    resolve = getattr(active, "_path_from_fd", None)
    if resolve is None:
        return fallback
    try:
        return Path(resolve(descriptor))
    except (OSError, AttributeError, NotImplementedError, ValueError):
        raise PrivacyError("acl_unverified") from None


def _read_all(descriptor: int, size: int, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    remaining = min(size, max_bytes) + 1
    while remaining > 0:
        try:
            chunk = os.read(descriptor, min(remaining, 65536))
        except OSError as exc:
            raise PrivacyError("file_unreadable") from exc
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    if len(payload) > max_bytes:
        raise PrivacyError("file_too_large")
    return payload


#: How long a caller waits for a lock another process is holding before
#: refusing. Enrolment holds it for two small file writes, so a wait longer
#: than this means something is wrong rather than merely busy.
LOCK_TIMEOUT_SECONDS = 10.0

#: How often the wait re-tries. Short enough that an uncontended handoff is
#: not perceptible, long enough not to spin.
_LOCK_POLL_SECONDS = 0.02


@contextlib.contextmanager
def exclusive_lock(path: str | os.PathLike[str], *,
                   timeout: float = LOCK_TIMEOUT_SECONDS,
                   root: str | os.PathLike[str] | None = None):
    """An advisory exclusive lock held on an OS handle, not on a marker file.

    A lock file created with ``O_EXCL`` and deleted afterwards is the usual
    shortcut and it is wrong here: a process that dies holding one leaves a
    file nobody can distinguish from a live holder, so the next run either
    hangs forever or "recovers" by deleting a lock somebody is using. Both
    ``flock`` and ``LockFile`` are released by the kernel when the handle
    closes, including on a crash, so there is no stale state to reason about.

    Advisory, and that is the honest word for it. It coordinates the writers
    in this project with each other. It does not stop an unrelated process
    from writing the same path, which is what the ACL and the identity-pinned
    replace are for.
    """

    target = Path(path)
    assert_no_reparse_ancestors(target.parent, stop=root)
    try:
        descriptor = os.open(target, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        # No directory to lock in, or no permission to. Either way there is
        # nothing to serialise on, and proceeding unserialised is the thing
        # this function exists to prevent.
        raise PrivacyError("lock_unavailable") from exc
    try:
        try:
            os.fchmod(descriptor, 0o600)
        except (AttributeError, OSError):
            pass
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            if _try_lock(descriptor):
                break
            if time.monotonic() >= deadline:
                raise PrivacyError("lock_unavailable")
            time.sleep(_LOCK_POLL_SECONDS)
        try:
            yield descriptor
        finally:
            _unlock(descriptor)
    finally:
        with contextlib.suppress(OSError):
            os.close(descriptor)


def _try_lock(descriptor: int) -> bool:
    """One non-blocking attempt, on whichever mechanism this platform has."""

    try:
        import fcntl
    except ImportError:
        pass
    else:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False
    try:
        import msvcrt
    except ImportError as exc:  # no locking primitive at all
        raise PrivacyError("lock_unsupported") from exc
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


def _unlock(descriptor: int) -> None:
    try:
        import fcntl
    except ImportError:
        pass
    else:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        return
    try:
        import msvcrt
    except ImportError:
        return
    with contextlib.suppress(OSError):
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)


def file_identity(path: str | os.PathLike[str]) -> FileIdentity:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise PrivacyError("file_unreadable") from exc
    return FileIdentity(info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def atomic_private_write(path: str | os.PathLike[str], payload: bytes, *,
                         secure: Any,
                         root: str | os.PathLike[str] | None = None,
                         expect_identity: FileIdentity | None = None) -> None:
    """Replace a file's content atomically, protecting before writing.

    ``secure`` receives the descriptor of the new, still-empty temporary file
    and must raise if it cannot make it owner-only. It is required, not
    optional: the one case this function exists to prevent is sensitive bytes
    reaching a file whose protection was never established.

    ``expect_identity`` carries an identity through the replace. A caller that
    read a record, decided something from it, and is now writing the result
    passes the identity it read; if the file on disk is no longer that one,
    the replace is refused and the other writer's version survives. Without
    it, two processes that both read the same record both write, and the
    later one silently wins with a decision made from stale content.

    The existing file is never opened for truncation. A failure anywhere in
    here leaves whatever was already recorded exactly as it was, which matters
    most for the record that decides whether delegation is enabled at all.
    """

    if secure is None:
        raise PrivacyError("secure_writer_required")
    target = Path(path)
    directory = target.parent
    assert_no_reparse_ancestors(directory, stop=root)
    if expect_identity is not None:
        try:
            current = file_identity(target)
        except PrivacyError:
            raise PrivacyError("replace_identity_changed") from None
        if current != expect_identity:
            raise PrivacyError("replace_identity_changed")
    descriptor, temporary = tempfile.mkstemp(dir=str(directory), prefix=".tmp-")
    try:
        try:
            os.fchmod(descriptor, 0o600)
        except (AttributeError, OSError):
            # No POSIX modes here; `secure` is what actually has to work.
            pass
        try:
            secure(descriptor)
        except PrivacyError:
            raise
        except (OSError, AttributeError, NotImplementedError) as exc:
            raise PrivacyError("acl_unverified") from exc
        os.write(descriptor, payload)
        os.fsync(descriptor)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    os.close(descriptor)
    try:
        os.replace(temporary, target)
    except OSError as exc:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise PrivacyError("replace_failed") from exc


def platform_secure_writer(platform: Any = None) -> Any:
    """The default ``secure`` for :func:`atomic_private_write`.

    Applies and verifies the owner-only ACL on the descriptor through the
    platform layer, and turns a platform that cannot do it at all into a
    refusal rather than a silent pass.
    """

    active = platform if platform is not None else host_platform

    def secure(descriptor: int) -> None:
        enforce = getattr(active, "enforce_owner_only_file", None)
        if enforce is None:
            raise PrivacyError("acl_unenforceable")
        try:
            enforce(descriptor)
        except (AttributeError, NotImplementedError) as exc:
            raise PrivacyError("acl_unenforceable") from exc
        except OSError as exc:
            raise PrivacyError("acl_unverified") from exc

    return secure
