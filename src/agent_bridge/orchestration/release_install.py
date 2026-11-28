"""Install one operator-approved release into one existing task folder.

Standalone on purpose: standard library only, no ``agent_bridge`` imports and
nothing read from the task, so the gate can recognise exactly one canonical
invocation and run it isolated::

    /usr/bin/env -i LANG=en_US.UTF-8 <gate python> -I -S -B <this file> install \
        --state-root <SR> --tasks-root <TR> --capacity-db <DB> \
        --task <ITEM> --manifest-sha256 <HEX>

Trust comes from a separate root-owned authorization, never from the caller.
``<SR>/release-install/approved/<HEX>.json`` is
the manifest whose bytes hash to ``<HEX>``, and every payload byte comes from
``<SR>/release-install/blobs/<sha256>``. ``accept`` can populate that cache
only after privileged ``authorize`` approves the digest and state root.
The gate and installer call the same :func:`plan`, so both verify the same digest and the
same write set (:func:`write_targets`). See docs/RELEASE-INSTALL.md.

The module imports on every platform (the gate loads it everywhere); only
the three entry points require POSIX, and ``fcntl`` is imported lazily.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import math
import os
import re
import shlex
import sqlite3
import stat
import sys
import time
from pathlib import Path

SCHEMA = "agent-bridge.release-install.v1"
STORE_DIR = "release-install"
APPROVED_DIR = "approved"
# Fixed system trust anchor, deliberately not configurable via args or env.
# /etc is a system symlink on macOS; resolve that once, then walk without links.
AUTHORIZATION_ROOT = os.path.realpath("/etc/agent-bridge/release-approvals")
BLOB_DIR = "blobs"
TASK_RECORD = "task.json"
ACTIVATION_FIELD = "current_stable_nightly_assignment"
#: The task's own canonical lock files, relative to the task folder, in the
#: order they are acquired: nightly owner, nightly queue, task-record claim.
#: A manifest must name exactly these, in this order. They are opened
#: read-only and never created, written or replaced.
LOCKS = ("nightly/state/owner.lock", "nightly/queue.lock", "task.json.claim.lock")
SUBCOMMAND = "install"
FLAGS = ("--state-root", "--tasks-root", "--capacity-db", "--task", "--manifest-sha256")
ENV_PREFIX = ("/usr/bin/env", "-i", "LANG=en_US.UTF-8")
INTERPRETER_FLAGS = ("-I", "-S", "-B")
TEMP_SUFFIX = ".release-install-tmp"
#: Hex digits of the manifest digest in each task temporary's name.
TEMP_DIGEST_CHARS = 16
FILE_MODE = 0o600
DIR_MODE = 0o700
MAX_MANIFEST_BYTES = 1 << 20
MAX_RECORD_BYTES = 4 << 20
MAX_FILE_BYTES = 16 << 20
MAX_TOTAL_BYTES = 64 << 20
MAX_FILES = 64
MAX_DEPTH = 4
#: A claim or ledger lease must outlive the install by at least this much.
LEASE_MARGIN_SECONDS = 30.0

_HEX = re.compile(r"[0-9a-f]{64}")
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
_ITEM = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,100}")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_DIRFLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW | _CLOEXEC
_READFLAGS = os.O_RDONLY | _NOFOLLOW | _NONBLOCK | _CLOEXEC

#: Test-only fault injection, called with a file name right after it was
#: renamed into place and before its folder is fsynced.
_FAULT_HOOK = None


class InstallError(Exception):
    """A refusal with a fixed, closed code. ``detail`` never carries file content."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _supported() -> bool:
    return (os.name == "posix" and bool(_NOFOLLOW) and hasattr(os, "fchmod")
            and os.open in os.supports_dir_fd)


# ------------------------------------------------------------------ manifest


def _strict_json(data: bytes):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError("duplicate key")
            out[key] = value
        return out

    def constant(_name):
        raise ValueError("non-finite number")

    def number(text):
        value = float(text)
        if not math.isfinite(value):
            raise ValueError("non-finite number")
        return value

    return json.loads(data.decode("utf-8"), object_pairs_hook=pairs,
                      parse_constant=constant, parse_float=number)


def _canon(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _finite(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def valid_item(item) -> bool:
    return isinstance(item, str) and bool(_ITEM.fullmatch(item)) and ".." not in item


def _overlaps(path: str, reserved: list[str]) -> bool:
    """``path`` is a reserved path, one of its folders, or inside one (casefolded)."""
    return any(path == r or r.startswith(path + "/") or path.startswith(r + "/")
               for r in reserved)


def validate_manifest(value, item: str) -> dict:
    """Exact schema; anything else is ``manifest_invalid``."""
    def bad(detail: str):
        raise InstallError("manifest_invalid", detail)

    if not isinstance(value, dict) or set(value) != {"schema", "task", "claim", "locks",
                                                     "files", "activation"}:
        bad("top-level keys")
    if value["schema"] != SCHEMA:
        bad("schema")
    if value["task"] != item:
        raise InstallError("manifest_task_mismatch")
    claim = value["claim"]
    if (not isinstance(claim, dict) or set(claim) != {"owner", "stage"}
            or not all(isinstance(claim[k], str) and _TOKEN.fullmatch(claim[k]) for k in claim)):
        bad("claim")
    if value["locks"] != list(LOCKS):
        bad("locks must be exactly " + ", ".join(LOCKS) + " in that order")
    reserved = [TASK_RECORD.casefold(), *(lock.casefold() for lock in LOCKS)]
    files = value["files"]
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        bad("files")
    seen: set[str] = set()
    folders: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "previous_sha256"}:
            bad("file entry keys")
        path = entry["path"]
        parts = path.split("/") if isinstance(path, str) else []
        if (not 1 <= len(parts) <= MAX_DEPTH
                or not all(_SEGMENT.fullmatch(part) for part in parts)
                or path.casefold() in seen
                or _overlaps(path.casefold(), reserved)):
            bad("file path")
        if not isinstance(entry["sha256"], str) or not _HEX.fullmatch(entry["sha256"]):
            bad("sha256")
        previous = entry["previous_sha256"]
        if previous is not None and (not isinstance(previous, str) or not _HEX.fullmatch(previous)):
            bad("previous_sha256")
        seen.add(path.casefold())
        folders.update("/".join(parts[:i]).casefold() for i in range(1, len(parts)))
    if seen & folders:
        bad("a file path is also a folder")
    activation = value["activation"]
    if not isinstance(activation, dict) or set(activation) != {"field", "previous", "value"}:
        bad("activation keys")
    if activation["field"] != ACTIVATION_FIELD:
        bad("activation field")
    previous = activation["previous"]
    if not (isinstance(previous, dict) and (previous == {"absent": True} or set(previous) == {"value"})):
        bad("activation previous")
    return value


def _store(state_root: str) -> str:
    if not isinstance(state_root, str) or not os.path.isabs(state_root) \
            or os.path.realpath(state_root) != state_root:
        raise InstallError("argument_not_canonical", "--state-root")
    return os.path.join(state_root, STORE_DIR)


def _read_fd(fd: int, limit: int, name: str, type_code: str, io_code: str) -> tuple[bytes, int]:
    """Read at most ``limit + 1`` bytes from an open regular file.

    Takes ownership of ``fd`` and closes it exactly once on every path. The
    type is checked with ``os.fstat`` before any file object exists, so a
    folder, FIFO, socket or device never reaches ``os.fdopen``. An unbuffered
    file object is used because a failed ``FileIO`` construction leaves a
    passed descriptor open, so the ``except`` below is its only closer."""
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise InstallError(type_code, name + ": not a regular file")
        handle = os.fdopen(fd, "rb", buffering=0)
    except InstallError:
        os.close(fd)
        raise
    except OSError as exc:
        os.close(fd)
        raise InstallError(io_code, name + ": " + type(exc).__name__) from None
    except BaseException:
        os.close(fd)
        raise
    data = bytearray()
    try:
        with handle:
            while len(data) <= limit:
                chunk = handle.read(limit + 1 - len(data))
                if chunk is None:
                    raise InstallError(io_code, name + ": read would block")
                if not chunk:
                    break
                data += chunk
    except OSError as exc:
        raise InstallError(io_code, name + ": " + type(exc).__name__) from None
    return bytes(data), stat.S_IMODE(info.st_mode)


def _read_regular(path: str, limit: int, code: str) -> bytes:
    """Read a regular file reached without any link; else ``code``."""
    if os.path.realpath(path) != path:
        raise InstallError(code, "link or non-canonical path")
    try:
        fd = os.open(path, _READFLAGS)
    except OSError as exc:
        raise InstallError(code, type(exc).__name__) from None
    data, _ = _read_fd(fd, limit, os.path.basename(path), code, code)
    if len(data) > limit:
        raise InstallError(code, "too large")
    return data


def _authorization(state_root: str, digest: str) -> tuple[str, bytes]:
    if not isinstance(digest, str) or not _HEX.fullmatch(digest):
        raise InstallError("manifest_digest_invalid")
    if not os.path.isabs(state_root) or os.path.realpath(state_root) != state_root:
        raise InstallError("argument_not_canonical", "--state-root")
    evidence = (_canon({"version": 1, "state_root": state_root,
                        "manifest_sha256": digest}) + "\n").encode("utf-8")
    return _sha(evidence) + ".json", evidence


def _trusted_authority(path: str, info: os.stat_result, *, directory: bool) -> None:
    """Check ownership, DAC, and this account's effective access (including ACLs).

    Root-run clients cannot establish independence from the authority and are
    refused by verification. Root is admitted here only by ``authorize``.
    """
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if info.st_uid != 0 or info.st_mode & 0o022 or not kind(info.st_mode):
        raise InstallError("operator_approval_untrusted")
    if os.geteuid() != 0:
        try:
            writable = os.access(path, os.W_OK, effective_ids=True)
        except (NotImplementedError, OSError):
            raise InstallError("operator_approval_unavailable") from None
        if writable:
            raise InstallError("operator_approval_untrusted")


def _authority_dir(*, create: bool = False) -> int:
    """Pin each root-owned ancestor by descriptor before reading/creating below it."""
    fd = os.open(os.path.sep, _DIRFLAGS)
    current = os.path.sep
    try:
        _trusted_authority(current, os.fstat(fd), directory=True)
        for part in Path(AUTHORIZATION_ROOT).parts[1:]:
            created = False
            if create:
                try:
                    os.mkdir(part, mode=0o755, dir_fd=fd)
                    created = True
                    os.fsync(fd)
                except FileExistsError:
                    pass
            child = os.open(part, _DIRFLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
            current = os.path.join(current, part)
            if created:
                os.fchmod(fd, 0o755)  # readable authority even with operator umask 077
            _trusted_authority(current, os.fstat(fd), directory=True)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _verify_approval(state_root: str, digest: str) -> None:
    if not _supported():
        raise InstallError("unsupported_platform")
    if os.geteuid() == 0:
        raise InstallError("operator_approval_untrusted", "clients must run without root privileges")
    name, expected = _authorization(state_root, digest)
    try:
        folder = _authority_dir()
        try:
            fd = os.open(name, _READFLAGS, dir_fd=folder)
            try:
                _trusted_authority(os.path.join(AUTHORIZATION_ROOT, name), os.fstat(fd),
                                   directory=False)
            except BaseException:
                os.close(fd)
                raise
            data, _ = _read_fd(fd, MAX_MANIFEST_BYTES, name,
                               "operator_approval_untrusted", "operator_approval_unavailable")
        finally:
            os.close(folder)
    except OSError:
        raise InstallError("operator_approval_unavailable") from None
    if data != expected:
        raise InstallError("operator_approval_untrusted")


def authorize(state_root: str, manifest_sha256: str) -> dict:
    """Privileged operator entry point; never reads or writes the user's cache.

    Use a trusted root-owned deployment of this file and interpreter. Human
    control comes from OS privilege elevation, not shell spelling or a TTY.
    """
    if not _supported():
        raise InstallError("unsupported_platform")
    if os.geteuid() != 0:
        raise InstallError("operator_privilege_required")
    name, evidence = _authorization(state_root, manifest_sha256)
    try:
        folder = _authority_dir(create=True)
        try:
            _write_at(folder, name, "." + name + TEMP_SUFFIX, evidence, 0o644)
        finally:
            os.close(folder)
    except OSError:
        raise InstallError("operator_approval_unavailable") from None
    return {"status": "authorized", "manifest_sha256": manifest_sha256,
            "authorization": os.path.join(AUTHORIZATION_ROOT, name)}


def load_manifest(state_root: str, item: str, digest: str) -> dict:
    if not isinstance(digest, str) or not _HEX.fullmatch(digest):
        raise InstallError("manifest_digest_invalid")
    if not valid_item(item):
        raise InstallError("task_invalid")
    _verify_approval(state_root, digest)
    path = os.path.join(_store(state_root), APPROVED_DIR, digest + ".json")
    data = _read_regular(path, MAX_MANIFEST_BYTES, "manifest_not_approved")
    if _sha(data) != digest:
        raise InstallError("manifest_digest_mismatch")
    try:
        value = _strict_json(data)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise InstallError("manifest_invalid", "json") from None
    return validate_manifest(value, item)


def temp_name(name: str, digest: str) -> str:
    """The one sibling temporary for ``name`` under the manifest ``digest``.

    Scoped to the manifest, so a file that merely shares the generic suffix is
    never this install's temporary. Payload segments cannot start with ``.``,
    so it never collides with a payload, a folder or a lock."""
    return "." + name + "." + digest[:TEMP_DIGEST_CHARS] + TEMP_SUFFIX


def write_targets(manifest: dict, digest: str) -> list[tuple[str, ...]]:
    """Every task-relative path the installer may create, replace, rename or
    unlink, each once, in write order.

    Per payload: each folder on its way (``_walk(create=True)`` may create
    it), its temporary, then the file. Last, ``task.json``'s temporary and
    ``task.json``. The task folder itself and everything above it are never
    targets. Lock files are only opened read-only and locked, so they are not
    targets either. The installer derives every name it touches from the same
    :func:`temp_name` and folder chain."""
    out: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()

    def add(parts: tuple[str, ...]) -> None:
        if parts not in seen:
            seen.add(parts)
            out.append(parts)

    for entry in manifest["files"]:
        parts = tuple(entry["path"].split("/"))
        for index in range(1, len(parts)):
            add(parts[:index])
        add(parts[:-1] + (temp_name(parts[-1], digest),))
        add(parts)
    add((temp_name(TASK_RECORD, digest),))
    add((TASK_RECORD,))
    return out


def destinations(tasks_root: str, item: str, manifest: dict, digest: str) -> list[str]:
    """:func:`write_targets` as absolute paths under the task folder."""
    base = os.path.join(tasks_root, item)
    return [os.path.join(base, *parts) for parts in write_targets(manifest, digest)]


def plan(state_root: str, tasks_root: str, item: str, digest: str) -> tuple[dict, list[str]]:
    """The one plan both the gate and the installer derive from the arguments:
    the verified manifest and its complete write set."""
    if not isinstance(tasks_root, str) or not os.path.isabs(tasks_root) \
            or os.path.realpath(tasks_root) != tasks_root:
        raise InstallError("argument_not_canonical", "--tasks-root")
    manifest = load_manifest(state_root, item, digest)
    return manifest, destinations(tasks_root, item, manifest, digest)


# ------------------------------------------------------------------ command


def command_words(python: str, installer: str, state_root: str, tasks_root: str,
                  capacity_db: str, task: str, manifest_sha256: str) -> list[str]:
    return [*ENV_PREFIX, python, *INTERPRETER_FLAGS, installer, SUBCOMMAND,
            "--state-root", state_root, "--tasks-root", tasks_root,
            "--capacity-db", capacity_db, "--task", task, "--manifest-sha256", manifest_sha256]


def parse_install_args(args: list[str]) -> dict | None:
    """Exactly ``install`` and the five flags in order; nothing else."""
    if len(args) != 1 + 2 * len(FLAGS) or args[0] != SUBCOMMAND:
        return None
    values = {}
    for index, flag in enumerate(FLAGS):
        if args[1 + 2 * index] != flag:
            return None
        value = args[2 + 2 * index]
        if not value or "\x00" in value:
            return None
        values[flag[2:].replace("-", "_")] = value
    return values


def parse_command_words(words: list[str]) -> dict | None:
    if words[:1] == ["exec"]:
        words = words[1:]
    n = len(ENV_PREFIX)
    if tuple(words[:n]) != ENV_PREFIX or len(words) < n + 5:
        return None
    if tuple(words[n + 1:n + 4]) != INTERPRETER_FLAGS:
        return None
    parsed = parse_install_args(words[n + 5:])
    if parsed is None:
        return None
    return {"python": words[n], "installer": words[n + 4], **parsed}


# ------------------------------------------------------------------ task folder


def _os_refusal(exc: OSError, dir_fd: int | None = None, name: str = "") -> InstallError:
    if dir_fd is not None and name:
        try:
            if stat.S_ISLNK(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode):
                return InstallError("link_refused", name)
        except OSError:
            pass
    if exc.errno in (errno.ELOOP, errno.EMLINK):
        return InstallError("link_refused", name)
    if exc.errno == errno.ENOTDIR:
        return InstallError("destination_type_refused", name)
    return InstallError("io_error", type(exc).__name__)


def _no_repo(dir_fd: int) -> None:
    try:
        os.stat(".git", dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise InstallError("nested_repository")


def _open_root(tasks_root: str) -> int:
    """No-follow walk from ``/`` to the already canonical task root."""
    fd = os.open("/", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _CLOEXEC)
    part = ""
    try:
        for part in [p for p in tasks_root.split(os.sep) if p]:
            child = os.open(part, _DIRFLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
            part = ""
            _no_repo(fd)
    except FileNotFoundError:
        os.close(fd)
        raise InstallError("task_root_missing") from None
    except OSError as exc:
        refusal = _os_refusal(exc, fd, part)
        os.close(fd)
        raise refusal from None
    except BaseException:
        os.close(fd)
        raise
    return fd


def _walk(item_fd: int, parts: list[str], *, create: bool = False,
          created: list[str] | None = None) -> int | None:
    """Open a folder chain under the task without following links.

    Missing folders are None, or created owner-only when ``create``."""
    fd = os.dup(item_fd)
    part = ""
    try:
        for index, part in enumerate(parts):
            try:
                child = os.open(part, _DIRFLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    os.close(fd)
                    return None
                os.mkdir(part, DIR_MODE, dir_fd=fd)
                if created is not None:
                    created.append("/".join(parts[:index + 1]))
                os.fsync(fd)
                child = os.open(part, _DIRFLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
            part = ""
            _no_repo(fd)
    except OSError as exc:
        refusal = _os_refusal(exc, fd, part)
        os.close(fd)
        raise refusal from None
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_at(dir_fd: int, name: str, limit: int) -> tuple[bytes, int] | None:
    try:
        fd = os.open(name, _READFLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _os_refusal(exc, dir_fd, name) from None
    data, mode = _read_fd(fd, limit, name, "destination_type_refused", "io_error")
    if len(data) > limit:
        raise InstallError("destination_too_large", name)
    return data, mode


def _current(item_fd: int, parts: list[str], limit: int = MAX_FILE_BYTES) -> tuple[bytes, int] | None:
    dir_fd = _walk(item_fd, parts[:-1])
    if dir_fd is None:
        return None
    try:
        return _read_at(dir_fd, parts[-1], limit)
    finally:
        os.close(dir_fd)


def _check_temp(dir_fd: int, name: str, digest: str, expected: list[bytes], limit: int) -> None:
    """A leftover temporary must be this install's own: a plain file whose
    bytes are a prefix of what this manifest writes to ``name``, or of
    ``name``'s current bytes. An interrupted rollback can leave previous bytes
    matching neither; those require operator recovery. Anything else is
    refused and left in place, never deleted."""
    temporary = temp_name(name, digest)
    found = _read_at(dir_fd, temporary, limit)
    if found is not None and not any(data.startswith(found[0]) for data in expected):
        raise InstallError("temporary_conflict", temporary)


def _write_at(dir_fd: int, name: str, temporary: str, data: bytes, mode: int, *,
              fault: bool = False) -> None:
    """Owner-only sibling temporary, fsync, rename, fsync the folder.

    ``temporary`` must be the caller's own to replace: the installer passes
    only :func:`temp_name` paths its preflight verified. A failure before the
    rename removes the temporary and leaves ``name`` untouched. A failure
    after it (the folder fsync) leaves ``name`` at the new bytes; callers
    record their undo entry before calling this."""
    try:
        os.unlink(temporary, dir_fd=dir_fd)
    except FileNotFoundError:
        pass
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
                 FILE_MODE, dir_fd=dir_fd)
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fchmod(fd, mode)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        try:
            os.unlink(temporary, dir_fd=dir_fd)
        except OSError:
            pass
        raise
    if fault and _FAULT_HOOK is not None:
        _FAULT_HOOK(name)
    os.fsync(dir_fd)


def _lock(item_fd: int, relative: str) -> int:
    import fcntl  # POSIX only; imported here so the gate can load this module anywhere.

    parts = relative.split("/")
    dir_fd = _walk(item_fd, parts[:-1])
    if dir_fd is None:
        raise InstallError("lock_missing", relative)
    try:
        try:
            fd = os.open(parts[-1], _READFLAGS, dir_fd=dir_fd)
        except FileNotFoundError:
            raise InstallError("lock_missing", relative) from None
        except OSError as exc:
            raise _os_refusal(exc, dir_fd, parts[-1]) from None
    finally:
        os.close(dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise InstallError("lock_unusable", relative)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise InstallError("lock_busy", relative) from None
    except OSError as exc:
        os.close(fd)
        raise InstallError("lock_unusable", relative + " " + type(exc).__name__) from None
    except BaseException:
        os.close(fd)
        raise
    return fd


# ------------------------------------------------------------------ ownership


def _verify_ledger(capacity_db: str, item: str, claim: dict, now: float) -> None:
    try:
        db = sqlite3.connect(Path(capacity_db).as_uri() + "?mode=ro", uri=True, timeout=1.0)
        try:
            columns = {row[1] for row in db.execute("PRAGMA table_info(stages)")}
            if not columns:
                raise InstallError("ledger_unavailable", "stages table missing")
            where = " AND archived_at IS NULL" if "archived_at" in columns else ""
            rows = db.execute("SELECT state, owner_id, lease_until FROM stages "
                              "WHERE item_id = ? AND stage = ?" + where,
                              (item, claim["stage"])).fetchall()
        finally:
            db.close()
    except sqlite3.Error as exc:
        raise InstallError("ledger_unavailable", type(exc).__name__) from None
    if not any(state == "owned" and owner == claim["owner"] and _finite(lease)
               and float(lease) > now + LEASE_MARGIN_SECONDS for state, owner, lease in rows):
        raise InstallError("ledger_not_owned")


def _verify_owner(item_fd: int, manifest: dict, capacity_db: str, item: str,
                  now: float) -> tuple[dict, bytes, int]:
    """The task's live claim must be the manifest's owner and stage, in both records."""
    found = _read_at(item_fd, TASK_RECORD, MAX_RECORD_BYTES)
    if found is None:
        raise InstallError("task_record_missing")
    raw, mode = found
    try:
        record = _strict_json(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise InstallError("task_record_unreadable") from None
    workload = record.get("workload") if isinstance(record, dict) else None
    claim = workload.get("claim") if isinstance(workload, dict) else None
    if not isinstance(claim, dict):
        raise InstallError("claim_not_live")
    expected = manifest["claim"]
    if claim.get("owner") != expected["owner"] or claim.get("stage") != expected["stage"]:
        raise InstallError("claim_mismatch")
    lease = claim.get("lease_expires_at")
    if claim.get("released_at") is not None or not _finite(lease) \
            or float(lease) <= now + LEASE_MARGIN_SECONDS:
        raise InstallError("claim_not_live")
    _verify_ledger(capacity_db, item, expected, now)
    return record, raw, mode


def _activation_done(record: dict, activation: dict) -> bool:
    field = activation["field"]
    if field in record and _canon(record[field]) == _canon(activation["value"]):
        return True
    previous = activation["previous"]
    if "absent" in previous:
        matches = field not in record
    else:
        matches = field in record and _canon(record[field]) == _canon(previous["value"])
    if not matches:
        raise InstallError("stale_activation")
    return False


def _activated(record: dict, activation: dict) -> bytes:
    """``task.json`` with only the activation field changed."""
    field = activation["field"]
    updated = dict(record)
    updated[field] = activation["value"]
    data = (json.dumps(updated, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    check = _strict_json(data)
    if _canon({k: v for k, v in check.items() if k != field}) \
            != _canon({k: v for k, v in record.items() if k != field}):
        raise InstallError("activation_not_narrow")
    return data


# ------------------------------------------------------------------ install


def _load_blobs(state_root: str, manifest: dict) -> list[bytes]:
    base = os.path.join(_store(state_root), BLOB_DIR)
    total = 0
    blobs = []
    for entry in manifest["files"]:
        data = _read_regular(os.path.join(base, entry["sha256"]), MAX_FILE_BYTES, "blob_missing")
        if _sha(data) != entry["sha256"]:
            raise InstallError("blob_hash_mismatch", entry["path"])
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise InstallError("manifest_too_large")
        blobs.append(data)
    return blobs


def _preflight(item_fd: int, manifest: dict, blobs: list[bytes], digest: str,
               record_bytes: list[bytes]) -> list:
    """Classify every target before anything is written.

    Each payload must be at its new hash (skipped) or at ``previous_sha256``/
    absent (pending); its existing folders must be plain folders, not links,
    files or repositories. Every temporary in the write set, including
    ``task.json``'s, must be absent or this install's own (``_check_temp``)."""
    pending = []
    for entry, blob in zip(manifest["files"], blobs):
        parts = entry["path"].split("/")
        dir_fd = _walk(item_fd, parts[:-1])
        current = None
        if dir_fd is not None:
            try:
                current = _read_at(dir_fd, parts[-1], MAX_FILE_BYTES)
                _check_temp(dir_fd, parts[-1], digest,
                            [blob] + ([] if current is None else [current[0]]), MAX_FILE_BYTES)
            finally:
                os.close(dir_fd)
        current_hash = None if current is None else _sha(current[0])
        if current_hash == entry["sha256"]:
            continue
        if current_hash != entry["previous_sha256"]:
            raise InstallError("stale_state", entry["path"])
        pending.append((parts, blob, current))
    _check_temp(item_fd, TASK_RECORD, digest, record_bytes, MAX_RECORD_BYTES)
    return pending


def _keep(mode: int) -> int:
    """An existing file keeps its mode, never gaining group or other write."""
    return mode & 0o755


def _restore(item_fd: int, parts: list[str], previous, digest: str) -> None:
    dir_fd = _walk(item_fd, parts[:-1])
    if dir_fd is None:
        if previous is None:
            return
        raise InstallError("rollback_incomplete", "/".join(parts))
    try:
        current = _read_at(dir_fd, parts[-1], MAX_FILE_BYTES)
        if previous is None:
            if current is not None:
                os.unlink(parts[-1], dir_fd=dir_fd)
                os.fsync(dir_fd)
        elif current is None or current[0] != previous[0] or current[1] != previous[1]:
            _write_at(dir_fd, parts[-1], temp_name(parts[-1], digest), previous[0], previous[1])
    finally:
        os.close(dir_fd)


def _rollback(item_fd: int, undo: list, created: list[str], digest: str) -> bool:
    """Undo in reverse, so ``task.json`` is restored before any payload.

    Each restore is re-read and verified. The first one that cannot be
    verified stops the rollback: the payloads are never reverted while the
    record may still carry the activation, and every file stays at either
    its previous or its new hash, so rerunning the command completes."""
    for parts, previous in reversed(undo):
        try:
            _restore(item_fd, parts, previous, digest)
            current = _current(item_fd, parts)
        except (OSError, InstallError):
            return False
        if (current is None) != (previous is None) or (
                current is not None and current[0] != previous[0]):
            return False
    for relative in reversed(created):
        parts = relative.split("/")
        try:
            dir_fd = _walk(item_fd, parts[:-1])
            if dir_fd is not None:
                try:
                    os.rmdir(parts[-1], dir_fd=dir_fd)
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
        except (OSError, InstallError):
            pass                         # an empty folder left behind changes nothing
    return True


def _verify_final(item_fd: int, manifest: dict, record_bytes: bytes) -> None:
    """Re-open every destination and compare its final bytes."""
    for entry in manifest["files"]:
        current = _current(item_fd, entry["path"].split("/"))
        if current is None or _sha(current[0]) != entry["sha256"]:
            raise InstallError("verify_failed", entry["path"])
    found = _read_at(item_fd, TASK_RECORD, MAX_RECORD_BYTES)
    if found is None or found[0] != record_bytes:
        raise InstallError("verify_failed", TASK_RECORD)


def install(state_root: str, tasks_root: str, capacity_db: str, task: str,
            manifest_sha256: str, *, now: float | None = None) -> dict:
    """Verify everything, write the files, activate last. Idempotent on retry."""
    if not _supported():
        raise InstallError("unsupported_platform")
    manifest, _ = plan(state_root, tasks_root, task, manifest_sha256)
    if not isinstance(capacity_db, str) or not os.path.isabs(capacity_db) \
            or os.path.realpath(capacity_db) != capacity_db:
        raise InstallError("argument_not_canonical", "--capacity-db")
    blobs = _load_blobs(state_root, manifest)
    clock = time.time if now is None else (lambda: now)
    digest = manifest_sha256
    held = [_open_root(tasks_root)]
    try:
        try:
            item_fd = os.open(task, _DIRFLAGS, dir_fd=held[0])
        except FileNotFoundError:
            raise InstallError("task_missing") from None
        except OSError as exc:
            raise _os_refusal(exc, held[0], task) from None
        held.append(item_fd)
        _no_repo(item_fd)
        for relative in LOCKS:           # owner, then queue, then claim; never blocks
            held.append(_lock(item_fd, relative))
        record, raw, _ = _verify_owner(item_fd, manifest, capacity_db, task, clock())
        pending = _preflight(item_fd, manifest, blobs, digest,
                             [raw, _activated(record, manifest["activation"])])
        if _activation_done(record, manifest["activation"]):
            if pending:
                raise InstallError("stale_state", "activated while files differ")
            return {"status": "already_installed", "task": task,
                    "manifest_sha256": manifest_sha256, "written": 0, "activated": True}
        undo: list = []
        created: list[str] = []
        try:
            for parts, blob, current in pending:
                dir_fd = _walk(item_fd, parts[:-1], create=True, created=created)
                try:
                    undo.append((parts, current))
                    _write_at(dir_fd, parts[-1], temp_name(parts[-1], digest), blob,
                              FILE_MODE if current is None else _keep(current[1]), fault=True)
                finally:
                    os.close(dir_fd)
            # Activation last, against a fresh read under the same locks.
            record, raw, mode = _verify_owner(item_fd, manifest, capacity_db, task, clock())
            if _activation_done(record, manifest["activation"]):
                raise InstallError("stale_activation", "changed during install")
            data = _activated(record, manifest["activation"])
            undo.append(([TASK_RECORD], (raw, mode)))
            _write_at(item_fd, TASK_RECORD, temp_name(TASK_RECORD, digest), data, _keep(mode),
                      fault=True)
            _verify_final(item_fd, manifest, data)
        except BaseException as exc:
            if not _rollback(item_fd, undo, created, digest):
                raise InstallError("rollback_incomplete",
                                   "rerun the same install command to complete it") from exc
            if isinstance(exc, InstallError):
                exc.detail = (exc.detail + "; " if exc.detail else "") + "rolled back"
                raise
            raise InstallError("write_failed", type(exc).__name__ + "; rolled back") from exc
        return {"status": "installed", "task": task, "manifest_sha256": manifest_sha256,
                "written": len(pending), "activated": True}
    finally:
        for fd in reversed(held):
            os.close(fd)


# ------------------------------------------------------------------ operator acceptance


def _publish(path: str, data: bytes) -> None:
    if os.path.lexists(path):
        if _read_regular(path, max(len(data), 1), "store_conflict") != data:
            raise InstallError("store_conflict", os.path.basename(path))
        return
    directory, name = os.path.split(path)
    fd = os.open(directory, _DIRFLAGS)
    try:
        # This is a disposable content cache. Authorization is independent;
        # the manifest and payload hashes are checked again before installing.
        _write_at(fd, name, "." + name + TEMP_SUFFIX, data, FILE_MODE)
    finally:
        os.close(fd)


def accept(state_root: str, manifest_file: str, payload_dir: str, tasks_root: str,
           capacity_db: str) -> dict:
    """Cache a payload that already has independent privileged authorization.

    Everything is read and verified before the first store write."""
    if not _supported():
        raise InstallError("unsupported_platform")
    state_root, tasks_root, capacity_db = (os.path.realpath(p) for p in
                                           (state_root, tasks_root, capacity_db))
    data = _read_regular(os.path.realpath(manifest_file), MAX_MANIFEST_BYTES, "manifest_unreadable")
    try:
        value = _strict_json(data)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise InstallError("manifest_invalid", "json") from None
    item = value.get("task") if isinstance(value, dict) else None
    if not valid_item(item):
        raise InstallError("task_invalid")
    manifest = validate_manifest(value, item)
    digest = _sha(data)
    _verify_approval(state_root, digest)
    payload_root = os.path.realpath(payload_dir)
    blobs = []
    for entry in manifest["files"]:
        blob = _read_regular(os.path.join(payload_root, *entry["path"].split("/")),
                             MAX_FILE_BYTES, "payload_missing")
        if _sha(blob) != entry["sha256"]:
            raise InstallError("payload_hash_mismatch", entry["path"])
        blobs.append(blob)
    store = _store(state_root)
    for folder in (store, os.path.join(store, BLOB_DIR), os.path.join(store, APPROVED_DIR)):
        os.makedirs(folder, mode=DIR_MODE, exist_ok=True)
    for entry, blob in zip(manifest["files"], blobs):
        _publish(os.path.join(store, BLOB_DIR, entry["sha256"]), blob)
    _publish(os.path.join(store, APPROVED_DIR, digest + ".json"), data)
    words = command_words(os.path.realpath(sys.executable), os.path.realpath(__file__),
                          state_root, tasks_root, capacity_db, item, digest)
    return {"status": "accepted", "task": item, "manifest_sha256": digest,
            "command": shlex.join(words)}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if args[:1] == [SUBCOMMAND]:
            parsed = parse_install_args(args)
            if parsed is None:
                raise InstallError("usage", "install takes exactly the five flags in order")
            result = install(**parsed)
        elif args[:1] == ["authorize"]:
            parser = argparse.ArgumentParser(prog="release_install.py authorize", allow_abbrev=False)
            parser.add_argument("--state-root", required=True)
            parser.add_argument("--manifest-sha256", required=True)
            options = parser.parse_args(args[1:])
            result = authorize(options.state_root, options.manifest_sha256)
        elif args[:1] == ["accept"]:
            parser = argparse.ArgumentParser(prog="release_install.py accept", allow_abbrev=False)
            for flag in ("--state-root", "--manifest", "--payload-dir", "--tasks-root",
                         "--capacity-db"):
                parser.add_argument(flag, required=True)
            options = parser.parse_args(args[1:])
            result = accept(options.state_root, options.manifest, options.payload_dir,
                            options.tasks_root, options.capacity_db)
        else:
            raise InstallError("usage", "subcommand is install, accept or authorize")
    except InstallError as exc:
        print(json.dumps({"status": "refused", "code": exc.code, "detail": exc.detail}))
        return 2
    except Exception as exc:  # noqa: BLE001 - name the class, never a traceback
        print(json.dumps({"status": "failed", "code": type(exc).__name__}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
