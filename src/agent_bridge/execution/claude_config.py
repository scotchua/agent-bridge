"""The one configuration directory the Claude execution lane may sign in to.

The lane used to inherit the default store at ``~/.claude``, the same one the
desktop app and interactive sessions refresh. A concurrent invalid-grant
cleanup there blanks the tokens, and the lane then reports a lost login. The
repair is not "set a different directory": it is that the lane has exactly one
directory, that directory is not the shared store, and nothing can quietly
point it back.

So this module answers one question, in one place, for both the harness and
the dispatcher: *is this the lane's own store, and is it private?*

Four separate refusals, because they are four different mistakes:

* it is the shared ``~/.claude`` store, directly;
* it is a link, alias, junction or reparse point that resolves to that store;
* it is some other directory somebody configured, which may be perfectly
  private and is still not this lane's;
* it is the right directory but not owner-only, so another account on the
  machine can read a subscription session out of it.

The last one is enforced before it is verified. A directory the lane is about
to authenticate against is worth tightening, and tightening it and then
reading the mode back is the only way to know the tightening took.

Every message here is fixed text chosen for an operator. None of them quote a
path: these strings reach a harness receipt and an installer transcript.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

#: Where the lane's own store lives, relative to the user's home directory.
#: One canonical location, not a setting: a configurable answer to "which
#: store" is how the lane ended up on the shared one.
CANONICAL_PARTS = (".agent-bridge", "claude-home")

#: The shared store this lane must never use. Named explicitly so the refusal
#: can say which mistake was made rather than "not the expected directory".
SHARED_STORE_NAME = ".claude"

#: An upper bound on the entries whose permissions are tightened. A store with
#: more files than this is not one this lane created, and walking an unbounded
#: tree before authenticating is its own problem.
MAX_STORE_ENTRIES = 512


class ConfigDirError(ValueError):
    """The configuration directory is missing, wrong, or not private."""


def canonical_config_path(home: Path | str | None = None) -> str:
    """The lane's store as a plain path string.

    Built with ``os.path`` rather than ``Path`` because the installer builds a
    configuration for a named home directory, which in its own tests is a
    Windows-shaped path on a POSIX machine. ``Path`` refuses to represent one;
    ``os.path.join`` has no opinion.
    """

    base = str(home) if home is not None else str(Path.home())
    return os.path.join(os.path.abspath(base), *CANONICAL_PARTS)


def canonical_config_dir(home: Path | str | None = None) -> Path:
    """The lane's store, derived rather than configured."""

    base = Path(home) if home is not None else Path.home()
    return base.joinpath(*CANONICAL_PARTS)


def shared_store_dir(home: Path | str | None = None) -> Path:
    """The store this lane must not touch."""

    base = Path(home) if home is not None else Path.home()
    return base / SHARED_STORE_NAME


def _real(path: Path) -> Path:
    """Resolve symlinks, aliases, junctions and reparse points."""

    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def _is_reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_reparse_tag", 0))


def assert_canonical(value: Path | str | None, *,
                     home: Path | str | None = None) -> Path:
    """The gate. Returns the directory, or refuses with fixed text.

    Resolution happens before comparison, which is the whole point: a symlink
    named ``claude-home`` pointing at ``~/.claude`` passes a string equality
    check and defeats the entire isolation.
    """

    if value is None:
        raise ConfigDirError("Claude configuration directory is not configured")
    candidate = Path(value)
    if not candidate.is_absolute():
        raise ConfigDirError("Claude configuration directory must be absolute")

    shared = _real(shared_store_dir(home))
    resolved = _real(candidate)
    if resolved == shared:
        raise ConfigDirError(
            "Claude configuration directory must not be the shared ~/.claude "
            "store used by the desktop app and interactive sessions")
    if resolved != _real(canonical_config_dir(home)):
        raise ConfigDirError(
            "Claude configuration directory must be the lane's own "
            "~/.agent-bridge/claude-home store")

    try:
        info = os.lstat(candidate)
    except FileNotFoundError:
        raise ConfigDirError(
            "Claude configuration directory does not exist") from None
    except OSError:
        raise ConfigDirError(
            "Claude configuration directory could not be read") from None
    if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
        # Reached only when the link resolves to the canonical path anyway; it
        # is still refused, because what a link points at can change between
        # this check and the login.
        raise ConfigDirError(
            "Claude configuration directory must not be a link or alias")
    if not stat.S_ISDIR(info.st_mode):
        raise ConfigDirError("Claude configuration directory is not a directory")
    return candidate


def enforce_private(directory: Path) -> Path:
    """Tighten the store to owner-only, then read it back. Fail closed.

    Enforce and verify, in that order, and never only one of them. Verifying
    without enforcing leaves a fixable problem as a hard failure; enforcing
    without verifying assumes a ``chmod`` that may have been refused.

    The mechanism is the platform's. On POSIX that is mode bits; on Windows
    ``st_mode`` carries only a read-only flag, so the same assertions would
    refuse a correctly protected store forever, and the protection is an
    owner-only ACL applied and read back through the platform layer. The
    guarantee is the same on both: no other account can read the store.
    """

    if os.name == "nt":
        return _enforce_private_nt(directory)

    # chmod before listing: an owned directory left unreadable (mode 000,
    # say) is one this call exists to repair, and the repair is what makes
    # the listing possible. Listing first would fail exactly the case that
    # was fixable.
    try:
        os.chmod(directory, 0o700)
    except OSError:
        raise ConfigDirError(
            "Claude configuration directory permissions could not be set") from None
    try:
        entries = _store_entries(directory)
    except OSError:
        raise ConfigDirError(
            "Claude configuration directory could not be read") from None
    # Only the top level. A subdirectory tightened to 0700 is one no other
    # account can traverse, so what is inside it is already unreachable, and
    # walking an arbitrary tree before authenticating is its own hazard.
    for name in entries:
        child = directory / name
        try:
            info = os.lstat(child)
            _refuse_links(info)
            if info.st_mode & 0o077:
                os.chmod(child, 0o700 if stat.S_ISDIR(info.st_mode) else 0o600)
        except ConfigDirError:
            raise
        except OSError:
            raise ConfigDirError(
                "Claude configuration directory contents could not be "
                "protected") from None

    observed = os.stat(directory)
    if observed.st_mode & 0o077:
        raise ConfigDirError(
            "Claude configuration directory is readable by other accounts")
    if hasattr(os, "getuid") and observed.st_uid != os.getuid():
        raise ConfigDirError(
            "Claude configuration directory is owned by another account")
    for name in entries:
        if os.lstat(directory / name).st_mode & 0o077:
            raise ConfigDirError(
                "Claude configuration directory contains a file readable by "
                "other accounts")
    return directory


def _store_entries(directory: Path) -> list[str]:
    entries = sorted(os.listdir(directory))
    if len(entries) > MAX_STORE_ENTRIES:
        raise ConfigDirError(
            "Claude configuration directory holds more files than this lane "
            "created")
    return entries


def _refuse_links(info: os.stat_result) -> None:
    if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
        raise ConfigDirError(
            "Claude configuration directory contains a link or alias")


#: Objects (files and directories, at any depth) a lane's store may hold
#: before enforcement refuses it as something this lane did not create.
MAX_STORE_OBJECTS = 20000


def _enforce_private_nt(directory: Path) -> Path:
    """The Windows half of :func:`enforce_private`: ACLs, not mode bits.

    The POSIX half stops at the top level because a 0700 directory cannot be
    traversed, so nothing inside it is reachable by name. Windows grants
    every account "bypass traverse checking" by default: a nested file with
    its own permissive entry is readable by anyone who knows its path,
    whatever its parents allow. So the whole tree is enforced, by the
    platform layer and through handles: the root is opened once, every
    object under it is enumerated and opened relative to its parent's
    handle, every object's owner is read before anything is written, and
    only then is each failing object's DACL replaced, in one write to that
    object alone, with the exact owner-only DACL. One read-only pass proves
    every object afterwards, so that "enforced" and "verified" remain two
    observations. An object owned by another account is never touched and
    refuses the store. Every refusal keeps the fixed operator text this
    module promises.
    """

    from agent_bridge.platform import platform as host

    try:
        _store_entries(directory)
    except OSError:
        raise ConfigDirError(
            "Claude configuration directory could not be read") from None
    try:
        protected, evidence = host.enforce_owner_only_tree(
            str(directory), max_objects=MAX_STORE_OBJECTS)
    except OSError:
        protected, evidence = False, {}
    if not protected:
        _refuse_nt(evidence)
        if "." in (evidence.get("objects_failed") or []):
            raise ConfigDirError(
                "Claude configuration directory permissions could not be set")
        raise ConfigDirError(
            "Claude configuration directory contents could not be protected")

    verified, evidence = host.observe_owner_only_tree(
        str(directory), max_objects=MAX_STORE_OBJECTS)
    if not verified:
        _refuse_nt(evidence)
        failed = evidence.get("objects_failed") or []
        if not failed or "." in failed:
            raise ConfigDirError(
                "Claude configuration directory is readable by other accounts")
        raise ConfigDirError(
            "Claude configuration directory contains a file readable by "
            "other accounts")
    return directory


def _refuse_nt(evidence: dict[str, object]) -> None:
    """The refusals a tree pass can report that have their own fixed text."""

    if evidence.get("objects_foreign_owned"):
        raise ConfigDirError(
            "Claude configuration directory contains a file owned by "
            "another account")
    if evidence.get("refused") == "reparse point":
        raise ConfigDirError(
            "Claude configuration directory contains a link or alias")
    if "than the lane creates" in str(evidence.get("error", "")):
        raise ConfigDirError(
            "Claude configuration directory holds more files than this lane "
            "created")


def checked_config_dir(value: Path | str | None, *,
                       home: Path | str | None = None) -> Path:
    """Everything above, in the order the lane needs it, before any auth use."""

    return enforce_private(assert_canonical(value, home=home))


def is_ready(value: object, *, home: Path | str | None = None) -> bool:
    """A yes/no for a readiness report. Never raises, never changes anything.

    Deliberately does not enforce: a readiness check is something an installer
    runs to describe the machine, and describing should not modify.
    """

    if not isinstance(value, (str, Path)) or not str(value):
        return False
    try:
        directory = assert_canonical(value, home=home)
    except ConfigDirError:
        return False
    try:
        info = os.stat(directory)
    except OSError:
        return False
    if os.name == "nt":
        # A read-back, never a grant: the platform's applying call would make
        # this report true by making it true, which is the one thing a
        # readiness check must not do. The whole tree, for the reason given
        # on _enforce_private_nt: a nested file another account can read is
        # a store that is not ready, wherever it sits.
        from agent_bridge.platform import platform as host
        try:
            verified, _evidence = host.observe_owner_only_tree(
                str(directory), max_objects=MAX_STORE_OBJECTS)
        except OSError:
            return False
        return bool(verified)
    if info.st_mode & 0o077:
        return False
    return not (hasattr(os, "getuid") and info.st_uid != os.getuid())
