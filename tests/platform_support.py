"""Platform-aware helpers for tests that assert owner-only protection.

Mode bits on POSIX; the real ACL read-back on Windows. Nothing here mocks the
Windows boundary: a test that passes on Windows through this module passed
against ``icacls``. The one stand-in, the POSIX mode-bit platform, exists so a
test that hands code a ``platform=`` seam runs on macOS and Linux, where the
Windows layer cannot; on Windows the same call returns the real platform.

Not a test module: the name deliberately fails the ``test_*.py`` pattern.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_bridge.platform import platform as host_platform  # noqa: E402

#: Everyone, the well-known SID. Granting it read is how a test makes a
#: directory "permissive" on Windows, where chmod 0o755 changes nothing.
_EVERYONE_SID = "S-1-1-0"


class PosixModeBitPlatform:
    """A platform layer that enforces owner-only the way the real one claims to.

    Not a permissive stub: ``verify_owner_only_path`` reads the mode actually
    on the file, so a test that loosens permissions genuinely fails the check
    rather than passing through a fake that always says yes. Reports itself as
    ``nt`` because the code under test is the Windows lane.
    """

    name = "nt"

    def enforce_owner_only_file(self, descriptor: int) -> None:
        os.fchmod(descriptor, 0o600)

    def verify_owner_only_path(self, directory, probe_file):
        info = os.stat(probe_file)
        return (info.st_mode & 0o077) == 0, "mode"


def owner_only_platform():
    """The object to pass through a ``platform=`` seam in a test.

    The real platform on Windows, so the test exercises icacls; the mode-bit
    stand-in elsewhere, where there is no Windows layer to exercise.
    """

    if os.name == "nt":
        return host_platform
    return PosixModeBitPlatform()


def assert_owner_only(test: unittest.TestCase, path, mode: int,
                      message: str = "") -> None:
    """Mode bits on POSIX, the ACL read-back on Windows. Never repairs."""

    if os.name != "nt":
        test.assertEqual(os.stat(path).st_mode & 0o777, mode, message or str(path))
        return
    verified, evidence = host_platform.observe_owner_only_acl(str(path))
    test.assertTrue(verified, (message, str(path), evidence))


def assert_not_owner_only(test: unittest.TestCase, path,
                          expected_mode: int | None = None) -> None:
    """The negative: something other than the owner can reach it."""

    if os.name != "nt":
        observed = os.stat(path).st_mode & 0o777
        if expected_mode is not None:
            test.assertEqual(observed, expected_mode, str(path))
        else:
            test.assertNotEqual(observed & 0o077, 0, str(path))
        return
    verified, evidence = host_platform.observe_owner_only_acl(str(path))
    test.assertFalse(verified, (str(path), evidence))


def make_permissive(path) -> None:
    """Let another account read it: chmod on POSIX, an Everyone ACE on Windows."""

    if os.name != "nt":
        os.chmod(path, 0o755 if os.path.isdir(path) else 0o644)
        return
    icacls = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                          "System32", "icacls.exe")
    completed = subprocess.run(
        [icacls, str(path), "/grant", f"*{_EVERYONE_SID}:(R)"],
        capture_output=True, timeout=15, check=False, shell=False)
    if completed.returncode != 0:
        raise OSError(completed.stdout.decode("utf-8", "replace")
                      + completed.stderr.decode("utf-8", "replace"))


def write_private_bytes(path, data: bytes) -> None:
    """Write a fixture record the way production writes one: owner-only.

    Through the platform layer, so on Windows the file carries the applied
    ACL the loaders now insist on reading back, rather than whatever the
    temporary directory happened to grant.
    """

    flags = os.O_CREAT | os.O_WRONLY | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        host_platform.enforce_owner_only_file(descriptor)
        os.write(descriptor, data)
    finally:
        os.close(descriptor)


_SYMLINK_SUPPORT: bool | None = None


def can_symlink() -> bool:
    """Whether this account may create symlinks at all.

    On Windows that is a privilege (SeCreateSymbolicLinkPrivilege) that an
    ordinary account does not hold unless Developer Mode is on; CI runners
    are administrators and hold it, a developer's VM usually does not.
    """

    global _SYMLINK_SUPPORT
    if _SYMLINK_SUPPORT is None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "target"
            target.write_bytes(b"")
            try:
                os.symlink(target, Path(temp) / "link")
            except (OSError, NotImplementedError):
                _SYMLINK_SUPPORT = False
            else:
                _SYMLINK_SUPPORT = True
    return _SYMLINK_SUPPORT


def require_symlinks(test: unittest.TestCase) -> None:
    """Skip, by name, a test whose fixture needs a symlink this account cannot make."""

    if not can_symlink():
        test.skipTest("symlink creation needs a privilege this account does not hold")
