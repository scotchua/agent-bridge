"""Platform-aware helpers for tests that assert owner-only protection.

Mode bits on POSIX; the real ACL read-back on Windows. Nothing here mocks the
Windows boundary: a test that passes on Windows through this module passed
against the security descriptor Windows actually holds for the object. The one stand-in, the POSIX mode-bit platform, exists so a
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

    The real platform on Windows, so the test exercises the real ACL path;
    the mode-bit stand-in elsewhere, where there is no Windows layer to exercise.
    """

    if os.name == "nt":
        return host_platform
    return PosixModeBitPlatform()


def assert_owner_only(test: unittest.TestCase, path, mode: int,
                      message: str = "", *, inside_protected_store: bool = False
                      ) -> None:
    """Mode bits on POSIX, the ACL read-back on Windows. Never repairs.

    ``inside_protected_store`` is for an object under the Claude lane's
    store, which Windows enforcement proves as a tree: the object may carry
    entries inherited from the protected root. Anything the bridge writes
    on its own is proved strictly, with inherited entries refused.
    """

    if os.name != "nt":
        test.assertEqual(os.stat(path).st_mode & 0o777, mode, message or str(path))
        return
    verified, evidence = host_platform.observe_owner_only_acl(
        str(path), allow_inherited=inside_protected_store)
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


def drop_acl_bypass_privileges() -> list[str]:
    """Remove SeBackupPrivilege and SeRestorePrivilege from this process.

    Measured on a hosted GitHub Windows runner (run 34936853093): a step
    launched through Git bash inherits a token in which both privileges are
    ENABLED (MSYS2 enables them for its own file handling and children keep
    them), while pwsh and cmd steps hold them disabled. With them enabled the
    kernel bypasses the DACL for the very operations these tests restrict
    (listing a directory, creating inside one), so every deny-based fixture
    restricts nothing and every "access is denied" assertion is void.

    Removing, not disabling: a removed privilege cannot be re-enabled by the
    process or anything it spawns, which is what "the tests run as an
    ordinary account would" has to mean. Returns the names removed, for the
    record; an empty list off Windows or when the token never held them.
    """

    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    TOKEN_ADJUST_PRIVILEGES, TOKEN_QUERY = 0x0020, 0x0008
    SE_PRIVILEGE_REMOVED = 0x00000004

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    class LUID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]

    class TOKEN_PRIVILEGES(ctypes.Structure):
        _fields_ = [("PrivilegeCount", wintypes.DWORD),
                    ("Privileges", LUID_AND_ATTRIBUTES * 1)]

    # Prototypes, not defaults. GetCurrentProcess returns the pseudo-handle
    # -1; with ctypes' default int return and int argument it reaches
    # OpenProcessToken as 0xFFFFFFFF on x64, not 0xFFFFFFFFFFFFFFFF, and the
    # call fails with ERROR_INVALID_HANDLE (seen on the hosted x64 runner,
    # run 34938146871, while the ARM64 VM happened to sign-extend it).
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.LookupPrivilegeValueW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(LUID)]
    advapi32.LookupPrivilegeValueW.restype = wintypes.BOOL
    advapi32.AdjustTokenPrivileges.argtypes = [
        wintypes.HANDLE, wintypes.BOOL, ctypes.POINTER(TOKEN_PRIVILEGES),
        wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
    advapi32.AdjustTokenPrivileges.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(),
                                     TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
                                     ctypes.byref(token)):
        raise OSError(ctypes.get_last_error(), "OpenProcessToken failed")
    removed: list[str] = []
    try:
        for name in ("SeBackupPrivilege", "SeRestorePrivilege"):
            luid = LUID()
            if not advapi32.LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
                # Every Windows this runs on defines both names. A lookup
                # failure means the fixture cannot know whether the token
                # holds the privilege, which is not a state to test in.
                raise OSError(ctypes.get_last_error(),
                              f"LookupPrivilegeValueW({name}) failed")
            privileges = TOKEN_PRIVILEGES()
            privileges.PrivilegeCount = 1
            privileges.Privileges[0].Luid = luid
            privileges.Privileges[0].Attributes = SE_PRIVILEGE_REMOVED
            adjusted = advapi32.AdjustTokenPrivileges(
                token, False, ctypes.byref(privileges), 0, None, None)
            error = ctypes.get_last_error()
            if not adjusted:
                raise OSError(error, f"AdjustTokenPrivileges({name}) failed")
            # A TRUE return with ERROR_NOT_ALL_ASSIGNED (1300) means the
            # token never held it, which is the state wanted; anything else
            # nonzero is a real failure.
            if error not in (0, 1300):
                raise OSError(error, f"AdjustTokenPrivileges({name}) failed")
            if error == 0:
                removed.append(name)
    finally:
        kernel32.CloseHandle(token)
    return removed


#: Done once, at import, by every test module that reaches this file and by
#: tests/harness.py: the tests must run as an ordinary account would.
ACL_BYPASS_PRIVILEGES_REMOVED = drop_acl_bypass_privileges()


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
