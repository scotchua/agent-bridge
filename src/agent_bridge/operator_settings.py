"""The operator's settings file: one switch for client-derived cloud routes.

Whether client-derived content may use the cloud "peer" (consultation) and
"execution" routes is decided in one file, read the same way by every part of
the bridge:

    <state home>/operator-settings.json

    {"version": 1,
     "client_derived_routes": ["peer", "execution"],
     "allow_client_derived": true}

``client_derived_routes`` is a list drawn from ``peer`` and ``execution``. The
optional ``allow_client_derived`` key belongs to the job runner, which reads
the same file from a separate repository. It never widens anything here.

The file shares its state home with that runner and its trust checks are the
runner's: the file and its folder must be owned by the running user, neither
may be writable by group or others, the file must be a regular file and not a
symlink. The folder is opened first and checked through its descriptor, then
the file is opened inside that folder, so a swapped parent cannot pass the
check while the file comes from elsewhere.

Fail closed. A file that is present but untrusted, unreadable, malformed, or
that names an unknown route or a wrong type gives an empty set, and the reason
is recorded on the result. Nothing here raises to a caller on a bad file.

Legacy transition. Older installs carry ``client_derived_routes`` in the
broker config, the orchestration config and routing-policy.json. When the file
is absent, or has no ``client_derived_routes`` key, the caller passes the
legacy values it holds and the answer is their INTERSECTION, never their
union. A route stays on only while every legacy holder agrees. With nothing
passed the answer is the default, empty. A file with a valid
``client_derived_routes`` is authoritative and the legacy values are ignored.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

FILE_NAME = "operator-settings.json"
SETTINGS_VERSION = 1
ROUTES = frozenset({"peer", "execution"})

SOURCE_OPERATOR = "operator_settings"
SOURCE_LEGACY = "legacy"
SOURCE_DEFAULT = "default"

#: Names the state home exactly as the job runner does, so both read one file.
STATE_ENV = "CODEX_JOB_RUNNER_STATE"
NEW_HOME_NAME = ".codex-job-runner"
OLD_HOME_NAME = ".codex-bridge"

# Reasons recorded on a result. Fixed strings, so callers can log them.
REASON_ABSENT = "absent"
REASON_NO_ROUTES_KEY = "no_client_derived_routes_key"
REASON_UNSUPPORTED_PLATFORM = "unsupported_platform"
REASON_UNREADABLE = "unreadable"
REASON_UNTRUSTED = "untrusted"
REASON_BAD_JSON = "bad_json"
REASON_NOT_OBJECT = "not_an_object"
REASON_BAD_VERSION = "bad_version"
REASON_WRONG_TYPE = "wrong_type"
REASON_UNKNOWN_ROUTE = "unknown_route"


@dataclass(frozen=True)
class RouteSetting:
    """The routes that may carry client-derived content, and where that came from.

    ``source`` is ``operator_settings`` (the trusted file decided),
    ``legacy`` (the intersection of legacy values the caller passed) or
    ``default`` (nothing decided anything). ``reason`` is None when the file
    decided; otherwise it says why it did not, which for a bad file is the
    fail-closed cause.
    """

    routes: frozenset[str]
    source: str
    reason: str | None
    path: str
    fail_closed: bool = False


def state_home(home: str | os.PathLike[str] | None = None,
               environ: Mapping[str, str] | None = None) -> str:
    """The runner's state directory: explicit env, then migrated, then legacy.

    Plain strings, not Path objects, so the answer is the same wherever a test
    or caller has changed what ``os.name`` reports.
    """
    env = os.environ if environ is None else environ
    raw = env.get(STATE_ENV)
    if raw:
        return os.path.expanduser(raw)
    user_home = os.path.expanduser("~") if home is None else os.fspath(home)
    new = os.path.join(user_home, NEW_HOME_NAME)
    old = os.path.join(user_home, OLD_HOME_NAME)
    return old if not os.path.exists(new) and os.path.exists(old) else new


def settings_path(home: str | os.PathLike[str] | None = None,
                  environ: Mapping[str, str] | None = None) -> str:
    return os.path.join(state_home(home=home, environ=environ), FILE_NAME)


def _trusted(st: os.stat_result) -> bool:
    getuid = getattr(os, "getuid", None)
    if getuid is not None and st.st_uid != getuid():
        return False
    return not st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)


_HAS_SAFE_OPEN = (all(hasattr(os, name) for name in ("O_NOFOLLOW", "O_NONBLOCK", "O_DIRECTORY"))
                  and os.open in os.supports_dir_fd)


class _Refused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _read_trusted(path: str) -> str | None:
    """The file's text, None when it is absent, or _Refused when it cannot be trusted.

    There is no existence check first: a path that cannot be looked up (a
    folder the user cannot search, say) would look absent and widen. Absence
    is only what an open reports as ENOENT; every other failure refuses.
    """
    if not _HAS_SAFE_OPEN:
        # Absence is still only ENOENT; a present file cannot be read safely.
        try:
            os.lstat(path)
        except FileNotFoundError:
            return None
        except OSError:
            raise _Refused(REASON_UNREADABLE) from None
        raise _Refused(REASON_UNSUPPORTED_PLATFORM)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        # The state folder itself must not be a symlink. Its ancestors may be
        # (macOS keeps /tmp and /var behind one).
        dir_fd = os.open(os.path.dirname(path),
                         os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError:
        raise _Refused(REASON_UNREADABLE) from None
    fd = -1
    text: str | None = None
    refusal: _Refused | None = None
    try:
        st = os.fstat(dir_fd)
        if not stat.S_ISDIR(st.st_mode) or not _trusted(st):
            raise _Refused(REASON_UNTRUSTED)
        try:
            fd = os.open(os.path.basename(path), flags, dir_fd=dir_fd)
        except FileNotFoundError:
            fd = -1
        else:
            # Checked on the descriptor before it is read, so a directory or
            # a FIFO that won the race is refused without a read.
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or not _trusted(st):
                raise _Refused(REASON_UNTRUSTED)
            chunks = []
            while chunk := os.read(fd, 65536):
                chunks.append(chunk)
            text = b"".join(chunks).decode("utf-8")
    except _Refused as exc:
        refusal = exc
    except (OSError, UnicodeDecodeError):
        # A symlink (ELOOP), a permission error or any other failure on a
        # file that exists is not a reason to widen.
        refusal = _Refused(REASON_UNREADABLE)
    # Closed one at a time, outside any handler: a failed close refuses
    # rather than escaping, and never hides an earlier refusal.
    for descriptor in (fd, dir_fd):
        if descriptor < 0:
            continue
        try:
            os.close(descriptor)
        except OSError:
            refusal = refusal or _Refused(REASON_UNREADABLE)
    if refusal is not None:
        raise refusal
    return text


def parse_routes(value: Any) -> frozenset[str] | None:
    """A strict route list, or None for anything else."""
    if (not isinstance(value, list)
            or any(not isinstance(route, str) or route not in ROUTES for route in value)):
        return None
    return frozenset(value)


def _read_document(path: str) -> tuple[dict[str, Any] | None, str | None]:
    """(document, reason). A document of None with reason None means absent."""
    try:
        text = _read_trusted(path)
    except _Refused as exc:
        return None, exc.reason
    if text is None:
        return None, REASON_ABSENT
    try:
        data = json.loads(text)
    except ValueError:
        return None, REASON_BAD_JSON
    if not isinstance(data, dict):
        return None, REASON_NOT_OBJECT
    return data, None


def _supported_version(version: Any) -> bool:
    # An int exactly: not True, not 1.0.
    return type(version) is int and version == SETTINGS_VERSION


def _intersection(legacy: Iterable[Iterable[str]] | None) -> frozenset[str] | None:
    if legacy is None:
        return None
    sets = [frozenset(values) for values in legacy]
    if not sets:
        return None
    return frozenset.intersection(*sets)


def resolve(home: str | os.PathLike[str] | None = None,
            environ: Mapping[str, str] | None = None, *,
            legacy: Iterable[Iterable[str]] | None = None) -> RouteSetting:
    """The routes allowed for client-derived content, with source and reason.

    ``legacy`` holds the legacy ``client_derived_routes`` values the caller
    has, one iterable per holder. Only the intersection is ever used, and only
    when the file does not decide.
    """
    path = settings_path(home, environ)
    document, reason = _read_document(path)

    def closed(why: str) -> RouteSetting:
        return RouteSetting(frozenset(), SOURCE_OPERATOR, why, path, fail_closed=True)

    if document is None:
        if reason != REASON_ABSENT:
            return closed(reason or REASON_UNREADABLE)
        fallback_reason = REASON_ABSENT
    elif "version" in document and not _supported_version(document["version"]):
        # Checked before the missing-key fallback: a file from a newer or
        # unknown schema must not fall back to the legacy values.
        return closed(REASON_BAD_VERSION)
    elif "client_derived_routes" not in document:
        # The runner's own legacy shape, allow_client_derived alone with no
        # version, still falls back.
        fallback_reason = REASON_NO_ROUTES_KEY
    else:
        if "version" not in document:
            return closed(REASON_BAD_VERSION)
        raw = document["client_derived_routes"]
        if not isinstance(raw, list) or any(not isinstance(route, str) for route in raw):
            return closed(REASON_WRONG_TYPE)
        routes = parse_routes(raw)
        if routes is None:
            return closed(REASON_UNKNOWN_ROUTE)
        return RouteSetting(routes, SOURCE_OPERATOR, None, path)

    merged = _intersection(legacy)
    if merged is None:
        return RouteSetting(frozenset(), SOURCE_DEFAULT, fallback_reason, path)
    return RouteSetting(merged, SOURCE_LEGACY, fallback_reason, path)


def client_derived_routes(home: str | os.PathLike[str] | None = None,
                          environ: Mapping[str, str] | None = None, *,
                          legacy: Iterable[Iterable[str]] | None = None) -> frozenset[str]:
    """The one call every reader uses. Never raises on a bad file."""
    return resolve(home, environ, legacy=legacy).routes


def existing_allow_client_derived(home: str | os.PathLike[str] | None = None,
                                  environ: Mapping[str, str] | None = None) -> Any:
    """The runner's legacy key as found in a trusted file, or None when unset."""
    document, _ = _read_document(settings_path(home, environ))
    return None if document is None else document.get("allow_client_derived")


def render(routes: Iterable[str], allow_client_derived: Any = None) -> bytes:
    """The file's bytes. A legacy ``allow_client_derived`` value is kept as it was."""
    document: dict[str, Any] = {"version": SETTINGS_VERSION,
                                "client_derived_routes": sorted(set(routes))}
    if allow_client_derived is not None:
        document["allow_client_derived"] = allow_client_derived
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def create(routes: Iterable[str], home: str | os.PathLike[str] | None = None,
           environ: Mapping[str, str] | None = None) -> str:
    """Create the file with owner-only permissions (mode 0600). Never replaces.

    The bytes are written and synced to a private temporary file in the state
    folder, then hard-linked into place. The link fails with FileExistsError
    when anything already holds the name, however recently it appeared, so an
    operator's or the runner's file is never overwritten.
    """
    import contextlib
    import tempfile

    from . import store
    from .platform import platform

    chosen = frozenset(routes)
    if not chosen <= ROUTES:
        raise ValueError("client_derived_routes must be drawn from peer, execution")
    path = settings_path(home, environ)
    directory = store.secure_mkdir(os.path.dirname(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as handle:
            platform.enforce_owner_only_file(handle.fileno())
            handle.write(render(chosen))
            handle.flush()
            os.fsync(handle.fileno())
        os.link(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
    with contextlib.suppress(OSError):
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    return path
