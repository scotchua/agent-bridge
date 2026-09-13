"""Pure-text and synthetic write-boundary tests for Windows ACL enforcement."""
from __future__ import annotations

import ast
import contextlib
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge import store
from agent_bridge.platform.windows_acl import icacls_listing_is_owner_only


OWNER_SID = "S-1-5-21-1-2-3-1001"
TARGET = r"C:\state\.permission-probe"


def source_enforcement_method():
    source = ast.parse((ROOT / "src/agent_bridge/platform/windows.py").read_text())
    window_class = next(
        node for node in source.body
        if isinstance(node, ast.ClassDef) and node.name == "WindowsPlatform"
    )
    method = next(
        node for node in window_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "enforce_owner_only_file"
    )
    scope: dict[str, object] = {"contextlib": contextlib, "os": os}
    exec(compile(ast.Module(body=[method], type_ignores=[]),
                 "windows_enforce_source", "exec"), scope)
    return scope["enforce_owner_only_file"]


def source_acl_verification_method(subprocess_module: object):
    source = ast.parse((ROOT / "src/agent_bridge/platform/windows.py").read_text())
    window_class = next(
        node for node in source.body
        if isinstance(node, ast.ClassDef) and node.name == "WindowsPlatform"
    )
    method = next(
        node for node in window_class.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_set_and_verify_owner_acl"
    )
    scope: dict[str, object] = {
        "subprocess": subprocess_module,
        "icacls_listing_is_owner_only": icacls_listing_is_owner_only,
    }
    exec(compile(ast.Module(body=[method], type_ignores=[]),
                 "windows_acl_verify_source", "exec"), scope)
    return scope["_set_and_verify_owner_acl"]


class RejectingWindowsPlatform:
    def __init__(self) -> None:
        self.supports_owner_only_permissions = True

    def _path_from_fd(self, fd: int) -> str:
        del fd
        return TARGET

    def _set_and_verify_owner_acl(self, path: str) -> bool:
        self.verified_path = path
        return False


class RejectingFilePlatform:
    def enforce_owner_only_file(self, fd: int) -> None:
        self.fd = fd
        raise PermissionError("synthetic ACL verification failure")


class FakeSubprocess:
    SubprocessError = RuntimeError

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, args: list[str], **kwargs: object) -> types.SimpleNamespace:
        del kwargs
        self.calls.append(args)
        if args[0] == "whoami":
            return types.SimpleNamespace(
                stdout=b'"MACHINE\\owner","S-1-5-21-1-2-3-1001"\n',
                returncode=0,
            )
        if len(args) > 2:
            return types.SimpleNamespace(stdout=b"", returncode=0)
        listing = (
            TARGET + " OWNER RIGHTS:(F)\n"
            "        NT AUTHORITY\\SYSTEM:(F)\n"
            "Successfully processed 1 files; Failed processing 0 files\n"
        )
        return types.SimpleNamespace(stdout=listing.encode(), returncode=0)


class WindowsAclSecurityTests(unittest.TestCase):
    def test_path_bound_parser_refuses_suffix_spoofed_system_principal(self) -> None:
        listing = (
            TARGET + r" MACHINE\Fake SYSTEM:(F)" + "\n"
            "        OWNER RIGHTS:(F)\n"
        )
        self.assertFalse(icacls_listing_is_owner_only(
            listing, OWNER_SID, expected_path=TARGET))

    def test_path_bound_parser_refuses_unrecognized_ace_looking_line(self) -> None:
        listing = (
            TARGET + " OWNER RIGHTS:(F)\n"
            "        this is not an ACE but used to be ignored\n"
        )
        self.assertFalse(icacls_listing_is_owner_only(
            listing, OWNER_SID, expected_path=TARGET))

    def test_path_bound_parser_accepts_complete_known_listing(self) -> None:
        listing = (
            TARGET + " OWNER RIGHTS:(OI)(CI)(F)\n"
            "        NT AUTHORITY\\SYSTEM:(OI)(CI)(F)\n"
            "        BUILTIN\\Administrators:(OI)(CI)(F)\n"
            "Successfully processed 1 files; Failed processing 0 files\n"
        )
        self.assertTrue(icacls_listing_is_owner_only(
            listing, OWNER_SID, expected_path=TARGET))

    def test_ambiguous_first_line_fails_closed_without_expected_path(self) -> None:
        listing = TARGET + " OWNER RIGHTS:(F)\n"
        self.assertFalse(icacls_listing_is_owner_only(listing, OWNER_SID))

    def test_platform_verification_binds_the_parser_to_the_observed_path(self) -> None:
        subprocess_module = FakeSubprocess()
        verify = source_acl_verification_method(subprocess_module)
        self.assertTrue(verify(object(), TARGET))

    def test_acl_failure_raises_before_a_file_is_written(self) -> None:
        enforce = source_enforcement_method()
        platform = RejectingWindowsPlatform()
        with self.assertRaises(PermissionError):
            enforce(platform, 123)
        self.assertFalse(platform.supports_owner_only_permissions)
        self.assertEqual(platform.verified_path, TARGET)

        with tempfile.TemporaryDirectory() as directory:
            target = os.path.join(directory, "must-not-exist")
            store.atomic_write_bytes(target, b"original")
            rejecting = RejectingFilePlatform()
            with mock.patch.object(store, "platform", rejecting):
                with self.assertRaises(PermissionError):
                    store.atomic_write_bytes(target, b"blocked")
            self.assertEqual(Path(target).read_bytes(), b"original")
            self.assertFalse(any(
                entry.name.startswith(".tmp-") for entry in Path(directory).iterdir()))
            with self.assertRaises(OSError):
                os.fstat(rejecting.fd)


if __name__ == "__main__":
    unittest.main()
