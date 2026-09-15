"""Host-independent tests for the Windows owner-only ACL guarantee.

The policy half (SID and ACL codecs, the owner-only judgment) is pure and
runs everywhere. The Windows half is bound to an open handle and cannot be
imported off Windows, so its methods are lifted from the source by AST and
driven against a scripted ``self``; that keeps the tests bound to the
shipped code rather than to a paraphrase of it. Nothing here mocks Windows
itself: the live read-back runs on the Windows VM and the hosted runner.
"""
from __future__ import annotations

import ast
import contextlib
import os
from pathlib import Path
import sys
import tempfile
from typing import Any
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge import store
from agent_bridge.platform import windows_acl as acl
from agent_bridge.platform.windows_acl import (
    ACCESS_ALLOWED_ACE_TYPE, ACCESS_DENIED_ACE_TYPE, CONTAINER_INHERIT_ACE,
    FILE_ALL_ACCESS, INHERIT_ONLY_ACE, INHERITED_ACE, OBJECT_INHERIT_ACE,
    SID_ADMINISTRATORS, SID_OWNER_RIGHTS, SID_SYSTEM,
    WINDOWS_OWNER_ONLY_GUARANTEE, Ace, SecurityState, build_owner_only_acl,
    encode_sid, is_exactly_owner_only, judge_security, owner_only_aces,
    parse_acl, parse_sid,
)


OWNER_SID = "S-1-5-21-1-2-3-1001"
OTHER_SID = "S-1-5-21-9-9-9-500"
EVERYONE = "S-1-1-0"
USERS = "S-1-5-32-545"
TARGET = r"C:\state\.permission-probe"
WINDOWS_SOURCE = (ROOT / "src/agent_bridge/platform/windows.py").read_text()


def allow(sid: str, *, flags: int = 0, mask: int = FILE_ALL_ACCESS) -> Ace:
    return Ace(ACCESS_ALLOWED_ACE_TYPE, flags, mask, sid)


_WRITTEN = object()


def state(owner: str = OWNER_SID, *, protected: bool = True, directory: bool = False,
          dacl: Any = _WRITTEN) -> SecurityState:
    """A decoded descriptor; ``dacl`` defaults to the exact written entry,
    and ``None`` means a NULL DACL, as it does in SecurityState."""
    if dacl is _WRITTEN:
        dacl = owner_only_aces(owner, directory=directory)
    return SecurityState(owner_sid=owner, protected=protected, dacl=dacl,
                         is_directory=directory)


def windows_method(name: str, scope: dict[str, object]):
    """The named WindowsPlatform method, compiled from the shipped source."""
    tree = ast.parse(WINDOWS_SOURCE)
    window_class = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "WindowsPlatform")
    method = next(
        node for node in window_class.body
        if isinstance(node, ast.FunctionDef) and node.name == name)
    exec(compile(ast.Module(body=[method], type_ignores=[]),
                 f"windows_{name}_source", "exec"), scope)
    return scope[name]


def method_source(name: str) -> str:
    tree = ast.parse(WINDOWS_SOURCE)
    window_class = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "WindowsPlatform")
    method = next(
        node for node in window_class.body
        if isinstance(node, ast.FunctionDef) and node.name == name)
    return ast.unparse(method)


class SidCodecTests(unittest.TestCase):
    def test_round_trips_and_matches_known_encodings(self):
        self.assertEqual(parse_sid(encode_sid(OWNER_SID)), OWNER_SID)
        self.assertEqual(parse_sid(bytes.fromhex("01020000000000052000000020020000")),
                         SID_ADMINISTRATORS)
        self.assertEqual(parse_sid(bytes.fromhex("010100000000000100000000")), EVERYONE)
        self.assertEqual(parse_sid(bytes.fromhex("010100000000000512000000")), SID_SYSTEM)

    def test_a_large_identifier_authority_prints_in_hex_and_round_trips(self):
        text = "S-1-0x123456789ABC-1"
        self.assertEqual(parse_sid(encode_sid(text)), text)

    def test_malformed_binary_sids_are_refused(self):
        good = encode_sid(OWNER_SID)
        for bad in (good[:7], bytes([2]) + good[1:], good + b"\0\0\0\0",
                    bytes([1, 16]) + good[2:]):
            with self.assertRaises(ValueError):
                parse_sid(bad)

    def test_malformed_text_sids_are_refused(self):
        for bad in ("", "runneradmin", "S-2-5-18", "S-1-5-x", "S-1-5-" + "-1" * 16,
                    "S-1-5-4294967296", "S-1-281474976710656-1"):
            with self.assertRaises(ValueError):
                encode_sid(bad)


class AclCodecTests(unittest.TestCase):
    def test_the_written_dacl_decodes_to_exactly_one_owner_entry(self):
        for directory, flags in ((False, 0),
                                 (True, OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE)):
            aces = parse_acl(build_owner_only_acl(OWNER_SID, directory=directory))
            self.assertEqual(aces, (Ace(ACCESS_ALLOWED_ACE_TYPE, flags,
                                        FILE_ALL_ACCESS, OWNER_SID),))
            self.assertEqual(aces, owner_only_aces(OWNER_SID, directory=directory))

    def test_the_acl_header_carries_its_own_size(self):
        data = build_owner_only_acl(OWNER_SID, directory=False)
        self.assertEqual(acl.acl_size(data[:8]), len(data))

    def test_inconsistent_acls_are_refused_not_guessed_at(self):
        data = bytearray(build_owner_only_acl(OWNER_SID, directory=False))
        with self.assertRaises(ValueError):
            parse_acl(bytes(data[:-1]))               # size disagrees with bytes
        with self.assertRaises(ValueError):
            parse_acl(bytes(data) + b"\0\0\0\0")      # trailing bytes
        truncated = bytearray(data)
        truncated[10] = 8                             # ACE claims only a header
        with self.assertRaises(ValueError):
            parse_acl(bytes(truncated))
        oversize = bytearray(data)
        oversize[10] = 0xFF                           # ACE runs past the ACL
        with self.assertRaises(ValueError):
            parse_acl(bytes(oversize))

    def test_an_uninterpreted_ace_type_is_kept_without_a_principal(self):
        # A callback (conditional) allow ACE, type 0x09: kept, not decoded.
        body = b"\0" * 12
        ace = bytes([0x09, 0x00]) + (4 + len(body)).to_bytes(2, "little") + body
        header = bytes([2, 0]) + (8 + len(ace)).to_bytes(2, "little") + b"\1\0\0\0"
        (decoded,) = parse_acl(header + ace)
        self.assertEqual(decoded.type, 0x09)
        self.assertIsNone(decoded.sid)

    def test_a_padded_ace_is_decoded_by_its_sid_length(self):
        sid = encode_sid(OWNER_SID)
        body = FILE_ALL_ACCESS.to_bytes(4, "little") + sid + b"\0\0\0\0"
        ace = bytes([0, 0]) + (4 + len(body)).to_bytes(2, "little") + body
        header = bytes([2, 0]) + (8 + len(ace)).to_bytes(2, "little") + b"\1\0\0\0"
        self.assertEqual(parse_acl(header + ace), owner_only_aces(OWNER_SID, directory=False))


class OwnerOnlyJudgmentTests(unittest.TestCase):
    """The guarantee, as a decision about decoded bytes."""

    def test_the_written_state_passes_and_is_exact(self):
        for directory in (False, True):
            observed = state(directory=directory)
            self.assertEqual(judge_security(observed, OWNER_SID), [])
            self.assertTrue(is_exactly_owner_only(observed, OWNER_SID))

    def test_the_caller_sid_is_canonicalised_before_comparison(self):
        self.assertEqual(judge_security(state(), OWNER_SID.lower()), [])
        self.assertTrue(is_exactly_owner_only(state(), OWNER_SID.lower()))

    def test_cpython_mkdir_0700_shape_passes_judgment_but_is_not_exact(self):
        flags = OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE
        observed = state(directory=True, dacl=(
            allow(SID_SYSTEM, flags=flags), allow(SID_ADMINISTRATORS, flags=flags),
            allow(SID_OWNER_RIGHTS, flags=flags)))
        self.assertEqual(judge_security(observed, OWNER_SID), [])
        self.assertFalse(is_exactly_owner_only(observed, OWNER_SID))

    def test_owner_rights_on_a_foreign_owned_object_is_refused(self):
        """Codex delta review of 055976f, F2 (ownership): OWNER RIGHTS grants
        whoever owns the object, so it must never stand in for the caller on
        an object the caller does not own."""
        observed = state(OTHER_SID, dacl=(allow(SID_OWNER_RIGHTS),))
        problems = judge_security(observed, OWNER_SID)
        self.assertTrue(any("not the caller" in item for item in problems), problems)
        self.assertFalse(is_exactly_owner_only(observed, OWNER_SID))

    def test_a_foreign_owned_object_with_the_exact_dacl_is_still_refused(self):
        observed = state(OTHER_SID, dacl=owner_only_aces(OWNER_SID, directory=False))
        self.assertTrue(judge_security(observed, OWNER_SID))
        self.assertFalse(is_exactly_owner_only(observed, OWNER_SID))

    def test_other_principals_are_refused_whatever_their_rights(self):
        for stranger in (EVERYONE, USERS, OTHER_SID):
            observed = state(dacl=(allow(OWNER_SID), allow(stranger, mask=0x1)))
            self.assertIn(f"{stranger} has access", judge_security(observed, OWNER_SID))

    def test_an_inherit_only_entry_for_a_stranger_is_refused(self):
        observed = state(directory=True, dacl=(
            *owner_only_aces(OWNER_SID, directory=True),
            allow(EVERYONE, flags=INHERIT_ONLY_ACE | OBJECT_INHERIT_ACE)))
        self.assertIn(f"{EVERYONE} has access", judge_security(observed, OWNER_SID))

    def test_a_deny_entry_is_refused_even_for_a_stranger(self):
        """Codex delta review of 055976f, F1: a deny can mask a grant, so the
        shape with denies in it is never accepted as owner-only; the writer
        replaces the whole DACL instead of removing denies one by one."""
        observed = state(dacl=(allow(OWNER_SID),
                               Ace(ACCESS_DENIED_ACE_TYPE, 0, 0x1, EVERYONE)))
        self.assertTrue(any("non-allow" in item for item in judge_security(observed, OWNER_SID)))

    def test_an_uninterpreted_ace_is_refused(self):
        observed = state(dacl=(allow(OWNER_SID), Ace(0x09, 0, 0, None)))
        self.assertTrue(any("not interpreted" in item
                            for item in judge_security(observed, OWNER_SID)))

    def test_a_null_or_empty_dacl_is_refused(self):
        self.assertIn("no DACL (everyone has every access)",
                      judge_security(state(dacl=None), OWNER_SID))
        self.assertIn("empty DACL", judge_security(state(dacl=()), OWNER_SID))

    def test_inherited_entries_are_refused_strictly_and_allowed_for_descendants(self):
        inherited = state(protected=False, dacl=(allow(OWNER_SID, flags=INHERITED_ACE),))
        strict = judge_security(inherited, OWNER_SID)
        self.assertIn("DACL inherits from the parent", strict)
        self.assertIn(f"{OWNER_SID} entry is inherited", strict)
        self.assertEqual(judge_security(inherited, OWNER_SID, allow_inherited=True), [])
        self.assertFalse(is_exactly_owner_only(inherited, OWNER_SID))

    def test_a_descendant_with_a_stranger_is_refused_even_with_inheritance_allowed(self):
        observed = state(protected=False, dacl=(
            allow(OWNER_SID, flags=INHERITED_ACE), allow(EVERYONE, mask=0x1)))
        self.assertIn(f"{EVERYONE} has access",
                      judge_security(observed, OWNER_SID, allow_inherited=True))

    def test_system_and_administrators_without_the_caller_are_refused(self):
        observed = state(dacl=(allow(SID_SYSTEM), allow(SID_ADMINISTRATORS)))
        self.assertIn("the caller has no entry", judge_security(observed, OWNER_SID))

    def test_an_unknown_caller_identity_fails_closed(self):
        self.assertEqual(judge_security(state(), ""), ["caller identity unknown"])
        self.assertEqual(judge_security(state(), "runneradmin"), ["caller identity unknown"])

    def test_the_guarantee_text_is_unchanged(self):
        self.assertEqual(
            WINDOWS_OWNER_ONLY_GUARANTEE,
            "No principal other than the owner, SYSTEM, and Administrators has any access.")


class ScriptedPlatform:
    """A stand-in ``self`` for methods lifted from WindowsPlatform.

    ``states`` is consumed by successive ``_read_security`` calls, so a test
    scripts what the object looked like before and after the write.
    """

    def __init__(self, states: list[SecurityState | None], caller: str | None = OWNER_SID):
        self.states = list(states)
        self.caller = caller
        self.writes: list[tuple[int, str, bool]] = []

    def _caller_sid(self):
        return self.caller

    def _read_security(self, handle, is_directory):
        observed = self.states.pop(0)
        return (observed, {}) if observed is not None else (None, {"read_error": 5})

    def _write_owner_only(self, handle, caller, is_directory):
        self.writes.append((handle, caller, is_directory))
        return None


PROTECT_SCOPE: dict[str, object] = {
    "judge_security": judge_security,
    "is_exactly_owner_only": is_exactly_owner_only,
    "WINDOWS_OWNER_ONLY_GUARANTEE": WINDOWS_OWNER_ONLY_GUARANTEE,
    "Any": Any,
}


class ProtectHandleTests(unittest.TestCase):
    """The single-write protection sequence, driven against scripted reads."""

    def setUp(self):
        self.protect = windows_method("_protect_handle", dict(PROTECT_SCOPE))

    def test_an_owned_object_is_written_once_and_the_exact_readback_passes(self):
        before = state(dacl=(allow(OWNER_SID), allow(EVERYONE, mask=0x1)))
        platform = ScriptedPlatform([before, state()])
        verified, evidence = self.protect(platform, 42, False)
        self.assertTrue(verified, evidence)
        self.assertEqual(platform.writes, [(42, OWNER_SID, False)])
        self.assertEqual(evidence["problems"], [])

    def test_a_foreign_owned_object_is_refused_before_anything_is_written(self):
        platform = ScriptedPlatform([state(OTHER_SID, dacl=(allow(SID_OWNER_RIGHTS),))])
        verified, evidence = self.protect(platform, 42, False)
        self.assertFalse(verified)
        self.assertEqual(platform.writes, [])
        self.assertEqual(evidence["refused"], "owned by another account")
        self.assertEqual(evidence["owner_sid"], OTHER_SID)

    def test_a_readback_that_is_not_the_written_dacl_fails_closed(self):
        survived = state(dacl=(allow(OWNER_SID), allow(EVERYONE, mask=0x1)))
        platform = ScriptedPlatform([state(), survived])
        verified, evidence = self.protect(platform, 42, False)
        self.assertFalse(verified)
        self.assertIn(f"{EVERYONE} has access", evidence["problems"])

    def test_a_readback_that_passes_judgment_but_differs_from_the_write_fails(self):
        flags = OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE
        cpython_shape = state(directory=True, dacl=(
            allow(SID_SYSTEM, flags=flags), allow(SID_ADMINISTRATORS, flags=flags),
            allow(SID_OWNER_RIGHTS, flags=flags)))
        platform = ScriptedPlatform([state(directory=True), cpython_shape])
        verified, evidence = self.protect(platform, 42, True)
        self.assertFalse(verified)
        self.assertEqual(evidence["problems"], ["read-back is not the DACL that was written"])

    def test_a_failed_write_or_read_fails_closed(self):
        class FailingWrite(ScriptedPlatform):
            def _write_owner_only(self, handle, caller, is_directory):
                return "SetSecurityInfo failed with 5"

        verified, evidence = self.protect(FailingWrite([state()]), 42, False)
        self.assertFalse(verified)
        self.assertEqual(evidence["error"], "SetSecurityInfo failed with 5")
        verified, evidence = self.protect(ScriptedPlatform([None]), 42, False)
        self.assertFalse(verified)
        self.assertEqual(evidence["read_error"], 5)

    def test_an_unknown_caller_identity_writes_nothing(self):
        platform = ScriptedPlatform([state()], caller=None)
        verified, evidence = self.protect(platform, 42, False)
        self.assertFalse(verified)
        self.assertEqual(platform.writes, [])
        self.assertEqual(evidence["error"], "caller identity unavailable")


class TreeEnforcementTests(unittest.TestCase):
    """Per-object, handle-bound tree enforcement against scripted objects."""

    def setUp(self):
        self.closed: list[int] = []
        closer = self

        class FakeKernel32:
            def CloseHandle(self, handle):
                closer.closed.append(handle)
                return 1

        self.scope = dict(PROTECT_SCOPE, kernel32=FakeKernel32())
        self.enforce = windows_method("enforce_owner_only_tree", self.scope)

    def platform(self, objects: dict[str, SecurityState | None]):
        tests = self

        class TreePlatform:
            def __init__(self):
                self.protected: list[str] = []
                self.handles: dict[int, str] = {}

            def _caller_sid(self):
                return OWNER_SID

            def _open_securable(self, path, *, write):
                tests.assertTrue(write)
                if path not in objects:
                    return None, False, {"open_error": 2}
                handle = 100 + len(self.handles)
                self.handles[handle] = path
                observed = objects[path]
                return handle, bool(observed and observed.is_directory), {}

            def _read_security(self, handle, is_directory):
                observed = objects[self.handles[handle]]
                return (observed, {}) if observed is not None else (None, {"read_error": 5})

            def _protect_handle(self, handle, is_directory):
                path = self.handles[handle]
                self.protected.append(path)
                return path != r"C:\store\sticky", {}

        return TreePlatform()

    def test_clean_objects_are_left_alone_and_failing_ones_replaced(self):
        root = r"C:\store"
        objects = {
            root: state(directory=True),
            root + r"\ok": state(protected=False,
                                 dacl=(allow(OWNER_SID, flags=INHERITED_ACE),)),
            root + r"\leaky": state(protected=False, dacl=(
                allow(OWNER_SID, flags=INHERITED_ACE), allow(EVERYONE, mask=0x1))),
        }
        platform = self.platform(objects)
        verified, evidence = self.enforce(platform, root, list(objects)[1:])
        self.assertTrue(verified, evidence)
        self.assertEqual(platform.protected, [root + r"\leaky"])
        self.assertEqual(evidence["objects_repaired"], 1)
        self.assertEqual(sorted(self.closed), sorted(platform.handles))

    def test_the_root_is_judged_strictly_and_descendants_leniently(self):
        root = r"C:\store"
        objects = {
            root: state(protected=False, directory=True,
                        dacl=(allow(OWNER_SID, flags=INHERITED_ACE | 3),)),
            root + r"\child": state(protected=False,
                                    dacl=(allow(OWNER_SID, flags=INHERITED_ACE),)),
        }
        platform = self.platform(objects)
        verified, _ = self.enforce(platform, root, [root + r"\child"])
        self.assertTrue(verified)
        self.assertEqual(platform.protected, [root])

    def test_a_foreign_owned_object_is_never_touched_and_fails_the_pass(self):
        root = r"C:\store"
        theirs = root + r"\theirs"
        objects = {root: state(directory=True),
                   theirs: state(OTHER_SID, dacl=(allow(SID_OWNER_RIGHTS),))}
        platform = self.platform(objects)
        verified, evidence = self.enforce(platform, root, [theirs])
        self.assertFalse(verified)
        self.assertEqual(platform.protected, [])
        self.assertEqual(evidence["objects_foreign_owned"], [theirs])

    def test_an_object_that_cannot_be_opened_or_repaired_fails_the_pass(self):
        root = r"C:\store"
        sticky = root + r"\sticky"
        objects = {root: state(directory=True),
                   sticky: state(dacl=(allow(OWNER_SID), allow(EVERYONE, mask=0x1)))}
        platform = self.platform(objects)
        verified, evidence = self.enforce(platform, root, [sticky, root + r"\gone"])
        self.assertFalse(verified)
        self.assertEqual(evidence["objects_failed"], [sticky, root + r"\gone"])


class HandleBoundSourceTests(unittest.TestCase):
    """Structural facts about the shipped Windows layer that the scripted
    tests above cannot see: what it calls and what it never calls."""

    ACL_METHODS = (
        "enforce_owner_only_file", "verify_owner_only_path", "_caller_sid",
        "_examine_handle", "_open_securable", "_reopen_descriptor",
        "_read_security", "_write_owner_only", "_protect_handle",
        "_protect_path", "_protect_descriptor", "_set_and_verify_owner_acl",
        "_observe_handle", "observe_owner_only_acl", "acl_diagnostics",
        "observe_owner_only_tree", "enforce_owner_only_tree",
    )

    def test_no_helper_program_and_no_text_parsing_remain_on_the_acl_path(self):
        for name in self.ACL_METHODS:
            source = method_source(name)
            self.assertNotIn("subprocess", source, name)
            self.assertNotIn("icacls", source, name)
            self.assertNotIn("whoami", source, name)

    def test_the_dacl_is_written_by_exactly_one_protected_single_call(self):
        writers = [name for name in self.ACL_METHODS
                   if "SetSecurityInfo" in method_source(name)]
        self.assertEqual(writers, ["_write_owner_only"])
        writer = method_source("_write_owner_only")
        self.assertIn("PROTECTED_DACL_SECURITY_INFORMATION", writer)
        self.assertEqual(writer.count("advapi32.SetSecurityInfo("), 1)
        self.assertNotIn("UNPROTECTED_DACL_SECURITY_INFORMATION", WINDOWS_SOURCE)
        self.assertNotIn("/remove", WINDOWS_SOURCE)
        self.assertNotIn("/reset", WINDOWS_SOURCE)

    def test_every_open_refuses_reparse_points_and_goes_through_one_examiner(self):
        for name in ("_open_securable", "_reopen_descriptor"):
            self.assertIn("_examine_handle", method_source(name), name)
        self.assertIn("FILE_FLAG_OPEN_REPARSE_POINT", method_source("_open_securable"))
        self.assertIn("FILE_ATTRIBUTE_REPARSE_POINT", method_source("_examine_handle"))
        self.assertEqual(WINDOWS_SOURCE.count("kernel32.CreateFileW("), 1)

    def test_the_open_file_is_protected_through_its_own_handle(self):
        source = method_source("enforce_owner_only_file")
        self.assertIn("_protect_descriptor", source)
        self.assertNotIn("_set_and_verify_owner_acl", source)
        self.assertIn("ReOpenFile", method_source("_reopen_descriptor"))

    def test_ownership_is_checked_before_the_write(self):
        source = method_source("_protect_handle")
        self.assertLess(source.index("owner_sid != caller"),
                        source.index("_write_owner_only"))

    def test_the_tree_pass_touches_only_objects_that_fail_judgment(self):
        source = method_source("enforce_owner_only_tree")
        self.assertIn("judge_security", source)
        self.assertLess(source.index("owner_sid != caller"),
                        source.index("_protect_handle"))

    def test_the_caller_identity_comes_from_the_token_not_from_a_name(self):
        source = method_source("_caller_sid")
        self.assertIn("OpenProcessToken", source)
        self.assertIn("GetTokenInformation", source)
        self.assertNotIn("environ", source)
        self.assertNotIn("getlogin", source)


class RejectingWindowsPlatform:
    def __init__(self) -> None:
        self.supports_owner_only_permissions = True

    def _path_from_fd(self, fd: int) -> str:
        del fd
        return TARGET

    def _protect_descriptor(self, fd: int) -> tuple[bool, dict[str, object]]:
        self.verified_fd = fd
        return False, {"refused": "synthetic"}


class RejectingFilePlatform:
    def enforce_owner_only_file(self, fd: int) -> None:
        self.fd = fd
        raise PermissionError("synthetic ACL verification failure")


class WriteBoundaryTests(unittest.TestCase):
    def test_acl_failure_raises_before_a_file_is_written(self) -> None:
        enforce = windows_method("enforce_owner_only_file",
                                 {"contextlib": contextlib, "os": os})
        platform = RejectingWindowsPlatform()
        with self.assertRaises(PermissionError) as caught:
            enforce(platform, 123)
        self.assertFalse(platform.supports_owner_only_permissions)
        self.assertEqual(platform.verified_fd, 123)
        self.assertIn(TARGET, str(caught.exception))
        self.assertIn("synthetic", str(caught.exception))

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
