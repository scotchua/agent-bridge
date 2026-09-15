"""Pure-text and synthetic write-boundary tests for Windows ACL enforcement."""
from __future__ import annotations

import ast
import contextlib
import ctypes
import ntpath
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
from agent_bridge.platform.windows_acl import (
    icacls_listing_foreign_principals, icacls_listing_is_owner_only,
    icacls_tree_foreign_principals, icacls_tree_listing_is_owner_only,
    split_icacls_tree_listing,
)


OWNER_SID = "S-1-5-21-1-2-3-1001"
TARGET = r"C:\state\.permission-probe"
SYSTEM32 = r"C:\Windows\System32"
TOOL_ENV = {"SYSTEMROOT": r"C:\Windows", "SYSTEMDRIVE": "C:"}


def trusted_tool(name: str) -> str:
    """Stand-in for the real GetSystemDirectoryW-backed resolver."""
    return SYSTEM32 + "\\" + name


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
        "icacls_listing_foreign_principals": icacls_listing_foreign_principals,
        "_trusted_tool": trusted_tool,
        "_TOOL_ENV": TOOL_ENV,
    }
    exec(compile(ast.Module(body=[method], type_ignores=[]),
                 "windows_acl_verify_source", "exec"), scope)
    return scope["_set_and_verify_owner_acl"]


def source_system_directory(kernel32: object):
    """The real _system_directory, executed against a fake kernel32.

    The module cannot be imported off Windows (it binds WinDLL at import
    time), so the function is lifted out of the source and given exactly the
    names it uses. That keeps the test bound to the shipped code rather than
    to a paraphrase of it.
    """
    source = ast.parse((ROOT / "src/agent_bridge/platform/windows.py").read_text())
    function = next(
        node for node in source.body
        if isinstance(node, ast.FunctionDef) and node.name == "_system_directory")
    scope: dict[str, object] = {"ctypes": ctypes, "kernel32": kernel32}
    exec(compile(ast.Module(body=[function], type_ignores=[]),
                 "windows_system_directory_source", "exec"), scope)
    return scope["_system_directory"]


class FakeKernel32:
    """GetSystemDirectoryW with a caller-chosen outcome."""

    def __init__(self, *, length: int, value: str | None = None) -> None:
        self._length = length
        self._value = value

    def GetSystemDirectoryW(self, buffer, size: int) -> int:
        if self._value is not None:
            buffer.value = self._value
        return self._length


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
        self.envs: list[object] = []

    def run(self, args: list[str], **kwargs: object) -> types.SimpleNamespace:
        self.envs.append(kwargs.get("env"))
        self.calls.append(args)
        if args[0].endswith("whoami.exe"):
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


class AclToolResolutionTests(unittest.TestCase):
    """The ACL round trip is the evidence owner-only rests on, so the programs
    that produce it must not be selectable through an inherited PATH."""

    def _verify(self):
        subprocess_module = FakeSubprocess()
        verify = source_acl_verification_method(subprocess_module)
        self.assertTrue(verify(object(), TARGET))
        return subprocess_module

    def test_every_tool_is_invoked_by_absolute_system32_path(self):
        calls = self._verify().calls
        self.assertTrue(calls)
        for argv in calls:
            self.assertTrue(argv[0].startswith(SYSTEM32 + "\\"), argv[0])
            self.assertTrue(argv[0].endswith(".exe"), argv[0])
        self.assertEqual(
            {argv[0] for argv in calls},
            {trusted_tool("whoami.exe"), trusted_tool("icacls.exe")})

    def test_no_tool_is_invoked_by_bare_name(self):
        for argv in self._verify().calls:
            self.assertNotIn(argv[0], ("whoami", "icacls", "whoami.exe", "icacls.exe"))

    def test_every_tool_runs_with_the_minimal_controlled_environment(self):
        module = self._verify()
        for env in module.envs:
            self.assertEqual(env, TOOL_ENV)
            self.assertNotIn("PATH", env)
            self.assertNotIn("PATHEXT", env)

    def test_a_fake_path_binary_cannot_affect_the_acl_proof(self):
        """A hostile whoami/icacls earlier on PATH is never consulted: the
        argv names an absolute System32 program and PATH is not passed."""
        hostile = "C:\\Users\\attacker\\bin"
        with mock.patch.dict(os.environ, {"PATH": hostile + ";" + SYSTEM32,
                                          "PATHEXT": ".EXE"}, clear=False):
            module = self._verify()
        for argv, env in zip(module.calls, module.envs):
            self.assertNotIn(hostile, argv[0])
            self.assertNotIn(hostile, str(env))
            self.assertEqual(argv[0], trusted_tool(ntpath.basename(argv[0])))

    def test_resolver_does_not_read_systemroot_from_the_environment(self):
        """The real resolver asks the OS, so a forged SystemRoot is inert."""
        tree = ast.parse((ROOT / "src/agent_bridge/platform/windows.py").read_text())
        resolver = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_system_directory")
        # Compare code, not prose: the docstring legitimately discusses the
        # environment it refuses to read.
        names = {ast.unparse(node) for node in ast.walk(resolver)
                 if isinstance(node, (ast.Attribute, ast.Name))}
        self.assertIn("kernel32.GetSystemDirectoryW", names)
        self.assertFalse([name for name in names if "environ" in name or "getenv" in name])


    def test_the_system_directory_comes_from_the_os_when_the_call_succeeds(self):
        resolve = source_system_directory(
            FakeKernel32(length=len(SYSTEM32), value=SYSTEM32))
        self.assertEqual(resolve(), SYSTEM32)

    def test_a_relocated_system_directory_is_honoured_not_overridden(self):
        """Proof the resolver reports what the OS said, so the fail-closed
        tests below are about a real lookup rather than a constant."""
        elsewhere = r"D:\Windows\System32"
        resolve = source_system_directory(
            FakeKernel32(length=len(elsewhere), value=elsewhere))
        self.assertEqual(resolve(), elsewhere)

    def test_a_failed_os_call_raises_instead_of_guessing_system32(self):
        for kernel32 in (FakeKernel32(length=0),
                         FakeKernel32(length=9999, value=SYSTEM32),
                         FakeKernel32(length=len(SYSTEM32), value="")):
            resolve = source_system_directory(kernel32)
            with self.assertRaises(OSError):
                resolve()

    def test_no_subprocess_runs_when_the_os_will_not_name_the_directory(self):
        """The fail-closed path must refuse before spawning anything. A
        guessed path would be used to run the program whose output IS the
        owner-only proof, so a failed lookup has to stop the proof."""
        def refuses(name: str) -> str:
            raise OSError(2, "GetSystemDirectoryW failed")

        subprocess_module = FakeSubprocess()
        source = ast.parse(
            (ROOT / "src/agent_bridge/platform/windows.py").read_text())
        window_class = next(
            node for node in source.body
            if isinstance(node, ast.ClassDef) and node.name == "WindowsPlatform")
        method = next(
            node for node in window_class.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_set_and_verify_owner_acl")
        scope: dict[str, object] = {
            "subprocess": subprocess_module,
            "icacls_listing_is_owner_only": icacls_listing_is_owner_only,
            "icacls_listing_foreign_principals": icacls_listing_foreign_principals,
            "_trusted_tool": refuses,
            "_TOOL_ENV": TOOL_ENV,
        }
        exec(compile(ast.Module(body=[method], type_ignores=[]),
                     "windows_acl_verify_source", "exec"), scope)
        verify = scope["_set_and_verify_owner_acl"]

        self.assertFalse(verify(object(), TARGET))
        self.assertEqual(subprocess_module.calls, [])

    def test_no_fallback_system_directory_constant_survives_in_the_source(self):
        body = (ROOT / "src/agent_bridge/platform/windows.py").read_text()
        self.assertNotIn("_FALLBACK_SYSTEM_DIRECTORY", body)


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


class AclTransitionSafetyTests(unittest.TestCase):
    """Protecting an object must never pass through a broader ACL than the
    one it found. Codex review of d9e93ab, finding F1: a ``/reset`` handed
    the object its parent's inheritable entries for the length of a process
    spawn, and for good if the next step failed."""

    def test_no_reset_is_ever_issued(self):
        subprocess_module = FakeSubprocess()
        verify = source_acl_verification_method(subprocess_module)
        self.assertTrue(verify(object(), TARGET))
        for argv in subprocess_module.calls:
            self.assertNotIn("/reset", [a.lower() for a in argv], argv)

    def test_the_owner_grant_precedes_every_removal(self):
        subprocess_module = FakeSubprocess()
        verify = source_acl_verification_method(subprocess_module)
        self.assertTrue(verify(object(), TARGET))
        icacls_calls = [argv for argv in subprocess_module.calls
                        if argv[0].endswith("icacls.exe")]
        modifying = [argv for argv in icacls_calls if len(argv) > 2]
        self.assertTrue(modifying)
        self.assertIn("/grant:r", modifying[0], modifying[0])
        for argv in modifying[1:]:
            self.assertTrue(argv[2] in ("/inheritance:r", "/remove:g"), argv)

    def test_a_foreign_explicit_entry_is_removed_by_name_then_reproved(self):
        class Listings(FakeSubprocess):
            def __init__(self):
                super().__init__()
                self.listings = [
                    (TARGET + " Everyone:(R)\n"
                     "        MACHINE\\owner:(F)\n"
                     "Successfully processed 1 files; Failed processing 0 files\n"),
                    (TARGET + " MACHINE\\owner:(F)\n"
                     "Successfully processed 1 files; Failed processing 0 files\n"),
                ]

            def run(self, args, **kwargs):
                if args[0].endswith("icacls.exe") and len(args) == 2:
                    self.calls.append(args)
                    return types.SimpleNamespace(
                        stdout=self.listings.pop(0).encode(), returncode=0)
                return super().run(args, **kwargs)

        subprocess_module = Listings()
        verify = source_acl_verification_method(subprocess_module)
        self.assertTrue(verify(object(), TARGET))
        removals = [argv for argv in subprocess_module.calls
                    if len(argv) > 2 and argv[2] == "/remove:g"]
        self.assertEqual(removals, [[trusted_tool("icacls.exe"), TARGET,
                                     "/remove:g", "EVERYONE",
                                     "/remove:d", "EVERYONE"]])

    def test_a_foreign_entry_that_survives_removal_fails_closed(self):
        class Sticky(FakeSubprocess):
            def run(self, args, **kwargs):
                if args[0].endswith("icacls.exe") and len(args) == 2:
                    self.calls.append(args)
                    listing = (TARGET + " Everyone:(R)\n"
                               "        MACHINE\\owner:(F)\n")
                    return types.SimpleNamespace(stdout=listing.encode(),
                                                 returncode=0)
                return super().run(args, **kwargs)

        verify = source_acl_verification_method(Sticky())
        self.assertFalse(verify(object(), TARGET))


class ForeignPrincipalListingTests(unittest.TestCase):
    def test_lists_explicit_strangers_once_and_skips_inherited_and_permitted(self):
        listing = (TARGET + " Everyone:(R)\n"
                   "        BUILTIN\\Users:(I)(R)\n"
                   "        MACHINE\\owner:(F)\n"
                   "        NT AUTHORITY\\SYSTEM:(F)\n"
                   "        Everyone:(W)\n")
        self.assertEqual(
            icacls_listing_foreign_principals(listing, OWNER_SID, "MACHINE\\owner",
                                              expected_path=TARGET),
            ["EVERYONE"])

    def test_a_malformed_listing_yields_none_not_an_empty_list(self):
        listing = TARGET + " OWNER RIGHTS:(F)\n        garbage here\n"
        self.assertIsNone(icacls_listing_foreign_principals(
            listing, OWNER_SID, expected_path=TARGET))


ROOT_DIR = r"C:\Users\owner\store"


def _tree(*blocks: str) -> str:
    return "\n".join(blocks) + "\nSuccessfully processed 6 files; Failed processing 0 files\n"


class TreeListingTests(unittest.TestCase):
    """One recursive icacls listing proves every object of a store."""

    owner = "MACHINE\\owner"
    root_block = (ROOT_DIR + " MACHINE\\owner:(OI)(CI)(F)\n"
                  "                      NT AUTHORITY\\SYSTEM:(OI)(CI)(F)")
    children = [ROOT_DIR + r"\a.json", ROOT_DIR + r"\sub dir",
                ROOT_DIR + r"\sub dir\b.json"]

    def test_splits_blocks_at_column_one_without_relying_on_blank_lines(self):
        text = _tree(self.root_block, "", ROOT_DIR + r"\a.json ",
                     ROOT_DIR + r"\sub dir MACHINE\owner:(OI)(CI)(F)")
        blocks = split_icacls_tree_listing(text)
        self.assertEqual(len(blocks), 3)
        self.assertTrue(blocks[0].startswith(ROOT_DIR + " "))

    def test_inherited_entries_are_accepted_on_descendants_only(self):
        text = _tree(self.root_block,
                     ROOT_DIR + r"\a.json MACHINE\owner:(I)(F)",
                     ROOT_DIR + r"\sub dir MACHINE\owner:(I)(OI)(CI)(F)",
                     ROOT_DIR + r"\sub dir\b.json MACHINE\owner:(I)(F)")
        self.assertEqual(icacls_tree_listing_is_owner_only(
            text, OWNER_SID, self.owner, ROOT_DIR, self.children), (True, []))
        inherited_root = _tree(
            ROOT_DIR + " MACHINE\\owner:(I)(OI)(CI)(F)",
            ROOT_DIR + r"\a.json MACHINE\owner:(I)(F)",
            ROOT_DIR + r"\sub dir MACHINE\owner:(I)(OI)(CI)(F)",
            ROOT_DIR + r"\sub dir\b.json MACHINE\owner:(I)(F)")
        verified, failed = icacls_tree_listing_is_owner_only(
            inherited_root, OWNER_SID, self.owner, ROOT_DIR, self.children)
        self.assertFalse(verified)
        self.assertEqual(failed, [ROOT_DIR])

    def test_a_nested_stranger_names_the_object_that_failed(self):
        text = _tree(self.root_block,
                     ROOT_DIR + r"\a.json MACHINE\owner:(I)(F)",
                     ROOT_DIR + r"\sub dir MACHINE\owner:(I)(OI)(CI)(F)",
                     ROOT_DIR + r"\sub dir\b.json Everyone:(R)",
                     "                             MACHINE\\owner:(I)(F)")
        verified, failed = icacls_tree_listing_is_owner_only(
            text, OWNER_SID, self.owner, ROOT_DIR, self.children)
        self.assertFalse(verified)
        self.assertEqual(failed, [ROOT_DIR + r"\sub dir\b.json"])

    def test_an_empty_acl_is_not_owner_only(self):
        text = _tree(self.root_block,
                     ROOT_DIR + r"\a.json ",
                     ROOT_DIR + r"\sub dir MACHINE\owner:(I)(OI)(CI)(F)",
                     ROOT_DIR + r"\sub dir\b.json MACHINE\owner:(I)(F)")
        verified, failed = icacls_tree_listing_is_owner_only(
            text, OWNER_SID, self.owner, ROOT_DIR, self.children)
        self.assertEqual((verified, failed), (False, [ROOT_DIR + r"\a.json"]))

    def test_unexpected_or_missing_objects_fail_the_proof(self):
        extra = _tree(self.root_block,
                      ROOT_DIR + r"\a.json MACHINE\owner:(I)(F)",
                      ROOT_DIR + r"\sub dir MACHINE\owner:(I)(OI)(CI)(F)",
                      ROOT_DIR + r"\sub dir\b.json MACHINE\owner:(I)(F)",
                      ROOT_DIR + r"\late.json MACHINE\owner:(I)(F)")
        self.assertFalse(icacls_tree_listing_is_owner_only(
            extra, OWNER_SID, self.owner, ROOT_DIR, self.children)[0])
        missing = _tree(self.root_block,
                        ROOT_DIR + r"\a.json MACHINE\owner:(I)(F)")
        verified, failed = icacls_tree_listing_is_owner_only(
            missing, OWNER_SID, self.owner, ROOT_DIR, self.children)
        self.assertFalse(verified)
        self.assertEqual(set(failed), set(self.children[1:]))

    def test_a_space_in_a_path_does_not_confuse_the_longest_match(self):
        # "sub dir" is a prefix of nothing here, but "sub" + " dir..." would
        # match ROOT\sub if such an object existed; longest match wins.
        children = [ROOT_DIR + r"\sub", ROOT_DIR + r"\sub dir"]
        text = _tree(self.root_block,
                     ROOT_DIR + r"\sub MACHINE\owner:(I)(OI)(CI)(F)",
                     ROOT_DIR + r"\sub dir MACHINE\owner:(I)(OI)(CI)(F)")
        self.assertEqual(icacls_tree_listing_is_owner_only(
            text, OWNER_SID, self.owner, ROOT_DIR, children), (True, []))

    def test_foreign_principals_are_collected_across_the_tree_by_object(self):
        """The first ACE shares its line with the path, so a stranger on a
        path line is only visible once the block is bound to its object.
        (Found on the Windows VM: the first version parsed the path as a
        principal, saw nothing, and left Everyone:(R) in place.)"""
        text = _tree(self.root_block,
                     ROOT_DIR + r"\a.json Everyone:(R)",
                     "                MACHINE\\owner:(I)(F)",
                     ROOT_DIR + r"\sub dir MACHINE\owner:(I)(OI)(CI)(F)",
                     ROOT_DIR + r"\sub dir\b.json BUILTIN\Users:(RX)",
                     "                MACHINE\\owner:(I)(F)")
        self.assertEqual(
            icacls_tree_foreign_principals(text, OWNER_SID, self.owner, ROOT_DIR,
                                           self.children),
            ["EVERYONE", "BUILTIN\\USERS"])
        unknown = _tree(self.root_block, ROOT_DIR + r"\stray Everyone:(R)")
        self.assertIsNone(icacls_tree_foreign_principals(
            unknown, OWNER_SID, self.owner, ROOT_DIR, self.children))
