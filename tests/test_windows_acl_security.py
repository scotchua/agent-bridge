"""The Windows owner-only guarantee, tested where it can be: on the bytes.

The policy half (``windows_acl``) is pure and imported directly. The Win32
half (``windows.py``) cannot be imported off Windows, so its methods are
lifted from the source by AST and exercised against scripted platforms,
and structural facts about the source (what it calls, what it never calls,
in what order) are asserted as text. Live Windows evidence comes from the
CI matrix and the ARM64 VM runs recorded in the task record, not from here.
"""
from __future__ import annotations

import ast
import contextlib
import os
from pathlib import Path
import struct
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
    FILE_ALL_ACCESS, FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_REPARSE_POINT,
    FILE_GENERIC_READ, FILE_GENERIC_WRITE, GENERIC_ALL, GENERIC_READ,
    GENERIC_WRITE, INHERIT_ONLY_ACE, INHERITED_ACE, OBJECT_INHERIT_ACE,
    SE_DACL_PRESENT, SE_DACL_PROTECTED, SE_SELF_RELATIVE, SID_ADMINISTRATORS,
    SID_OWNER_RIGHTS, SID_SYSTEM, WINDOWS_OWNER_ONLY_GUARANTEE, Ace,
    DirectoryEntry, SecurityState, accepted_owners, build_owner_only_acl,
    build_owner_only_descriptor, encode_object_name, encode_sid,
    grants_effective_access, is_exactly_owner_only, judge_security,
    map_generic_rights, owner_only_aces, parse_acl, parse_directory_listing,
    parse_security_descriptor, parse_sid,
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


def directory_records(*entries: tuple[str, int]) -> bytes:
    """A FILE_FULL_DIR_INFO buffer as GetFileInformationByHandleEx fills it."""
    out = b""
    for index, (name, attributes) in enumerate(entries):
        encoded = name.encode("utf-16-le")
        record = bytearray(68) + encoded
        struct.pack_into("<I", record, 56, attributes)
        struct.pack_into("<I", record, 60, len(encoded))
        if index < len(entries) - 1:
            padding = (-len(record)) % 8
            struct.pack_into("<I", record, 0, len(record) + padding)
            record += b"\0" * padding
        out += bytes(record)
    return out


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
            parse_acl(bytes(data) + b"\0\0\0\0")      # bytes the size does not claim
        truncated = bytearray(data)
        truncated[10] = 8                             # ACE claims only a header
        with self.assertRaises(ValueError):
            parse_acl(bytes(truncated))
        oversize = bytearray(data)
        oversize[10] = 0xFF                           # ACE runs past the ACL
        with self.assertRaises(ValueError):
            parse_acl(bytes(oversize))

    def test_valid_allocation_slack_after_the_last_ace_decodes(self):
        """Codex review of ccb85ef, R3: AclSize is the allocation, and Windows
        may leave unused space after the last ACE. Such an ACL is valid and
        must receive the same judgment as its compact form."""
        compact = build_owner_only_acl(OWNER_SID, directory=False)
        padded = bytearray(compact + b"\0" * 8)
        struct.pack_into("<H", padded, 2, len(padded))
        self.assertEqual(parse_acl(bytes(padded)), parse_acl(compact))
        self.assertEqual(judge_security(state(dacl=parse_acl(bytes(padded))), OWNER_SID), [])

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


class SecurityDescriptorCodecTests(unittest.TestCase):
    """The self-relative descriptor NtQuerySecurityObject returns and
    NtSetSecurityObject is handed."""

    def test_the_written_descriptor_decodes_to_the_protected_owner_only_state(self):
        for directory in (False, True):
            raw = build_owner_only_descriptor(OWNER_SID, directory=directory)
            decoded = parse_security_descriptor(raw, is_directory=directory)
            self.assertTrue(decoded.protected)
            self.assertEqual(decoded.dacl, owner_only_aces(OWNER_SID, directory=directory))
            self.assertEqual(decoded.owner_sid, "")   # the write carries no owner
            control = struct.unpack_from("<H", raw, 2)[0]
            self.assertEqual(control & (SE_DACL_PRESENT | SE_DACL_PROTECTED | SE_SELF_RELATIVE),
                             SE_DACL_PRESENT | SE_DACL_PROTECTED | SE_SELF_RELATIVE)

    def test_a_descriptor_with_an_owner_decodes_the_owner(self):
        dacl = build_owner_only_acl(OWNER_SID, directory=False)
        owner = encode_sid(OTHER_SID)
        header = struct.pack("<BBHIIII", 1, 0, SE_DACL_PRESENT | SE_SELF_RELATIVE,
                             20 + len(dacl), 0, 0, 20)
        decoded = parse_security_descriptor(header + dacl + owner, is_directory=False)
        self.assertEqual(decoded.owner_sid, OTHER_SID)
        self.assertFalse(decoded.protected)
        self.assertEqual(decoded.dacl, owner_only_aces(OWNER_SID, directory=False))

    def test_an_absent_dacl_is_null_and_malformed_descriptors_are_refused(self):
        header = struct.pack("<BBHIIII", 1, 0, SE_SELF_RELATIVE, 0, 0, 0, 0)
        self.assertIsNone(parse_security_descriptor(header, is_directory=False).dacl)
        dacl = build_owner_only_acl(OWNER_SID, directory=False)
        for bad in (
            struct.pack("<BBHIIII", 2, 0, SE_SELF_RELATIVE, 0, 0, 0, 0),      # revision
            struct.pack("<BBHIIII", 1, 0, 0, 0, 0, 0, 0),                     # not self-relative
            struct.pack("<BBHIIII", 1, 0, SE_DACL_PRESENT | SE_SELF_RELATIVE,
                        0, 0, 0, 200) + dacl,                                 # DACL offset outside
            struct.pack("<BBHIIII", 1, 0, SE_SELF_RELATIVE, 200, 0, 0, 0),    # owner offset outside
            header[:10],                                                      # truncated
        ):
            with self.assertRaises(ValueError):
                parse_security_descriptor(bad, is_directory=False)


class ObjectNameCodecTests(unittest.TestCase):
    """Codex review of ccb85ef..2e9ed0f, B3: a UNICODE_STRING's Length is
    a UTF-16 byte count, which ``len(name) * 2`` understates for a
    supplementary character, and an understated Length opens a shorter
    name."""

    def test_length_counts_utf16_bytes_not_code_points(self):
        name = "\U0001f9ea.txt"
        self.assertEqual(len(name), 5)
        encoded = encode_object_name(name)
        self.assertEqual(len(encoded), 12)
        self.assertEqual(encoded, name.encode("utf-16-le"))
        self.assertNotEqual(len(encoded), len(name) * 2)
        self.assertEqual(encode_object_name("a.txt"), "a.txt".encode("utf-16-le"))

    def test_a_name_longer_than_a_unicode_string_holds_is_refused(self):
        longest = "x" * (acl.UNICODE_STRING_MAX_BYTES // 2)
        self.assertLessEqual(len(encode_object_name(longest)), acl.UNICODE_STRING_MAX_BYTES)
        with self.assertRaises(ValueError):
            encode_object_name(longest + "x")
        with self.assertRaises(ValueError):
            encode_object_name("\ud800")


class DirectoryListingCodecTests(unittest.TestCase):
    def test_entries_decode_with_their_attributes_and_dot_entries_are_dropped(self):
        raw = directory_records((".", 0x10), ("..", 0x10), ("f.txt", 0x20),
                                ("jx", 0x10 | FILE_ATTRIBUTE_REPARSE_POINT), ("sub", 0x10))
        entries = parse_directory_listing(raw)
        self.assertEqual([entry.name for entry in entries], ["f.txt", "jx", "sub"])
        self.assertTrue(entries[2].is_directory)
        self.assertTrue(entries[1].is_reparse_point and entries[1].is_directory)
        self.assertFalse(entries[0].is_directory)

    def test_a_name_that_is_not_one_component_is_refused(self):
        for name in ("a\\b", "a/b", "a\0b", "f.txt:stream", ""):
            with self.assertRaises(ValueError):
                parse_directory_listing(directory_records((name, 0x20)))

    def test_a_broken_chain_or_truncated_record_is_refused(self):
        good = bytearray(directory_records(("a", 0x20), ("b", 0x20)))
        struct.pack_into("<I", good, 0, 4)                 # next entry inside this one
        with self.assertRaises(ValueError):
            parse_directory_listing(bytes(good))
        with self.assertRaises(ValueError):
            parse_directory_listing(directory_records(("abc", 0x20))[:-2])
        with self.assertRaises(ValueError):
            parse_directory_listing(directory_records(("a", 0x20), ("A", 0x20)))


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

    def test_the_tokens_default_owner_is_the_callers_own(self):
        """CI run 34940818792: an elevated administrator's freshly created
        files are owned by Administrators, and the token says so
        (TokenOwner). Such an object is the caller's own; the same object
        seen by a token whose default owner is the user is not."""
        theirs = state(SID_ADMINISTRATORS, dacl=(allow(OWNER_SID),))
        self.assertEqual(judge_security(theirs, OWNER_SID,
                                        default_owner_sid=SID_ADMINISTRATORS), [])
        self.assertTrue(is_exactly_owner_only(theirs, OWNER_SID,
                                              default_owner_sid=SID_ADMINISTRATORS))
        self.assertTrue(judge_security(theirs, OWNER_SID))
        self.assertTrue(judge_security(theirs, OWNER_SID, default_owner_sid=OWNER_SID))
        self.assertEqual(accepted_owners(OWNER_SID, SID_ADMINISTRATORS),
                         frozenset({OWNER_SID, SID_ADMINISTRATORS}))
        self.assertEqual(accepted_owners(OWNER_SID, None), frozenset({OWNER_SID}))

    def test_a_default_owner_never_admits_a_third_account(self):
        observed = state(OTHER_SID, dacl=(allow(OWNER_SID),))
        self.assertTrue(judge_security(observed, OWNER_SID,
                                       default_owner_sid=SID_ADMINISTRATORS))
        with self.assertRaises(ValueError):
            accepted_owners(OWNER_SID, "nonsense")

    def test_only_administrators_is_accepted_as_a_default_owner(self):
        """Codex review of ccb85ef..2e9ed0f, B2: a token whose default
        owner is some other group (a shared group the caller belongs to,
        say) must not make that group's objects the caller's own."""
        for group in (USERS, OTHER_SID, EVERYONE):
            self.assertEqual(accepted_owners(OWNER_SID, group), frozenset({OWNER_SID}),
                             group)
            theirs = state(group, dacl=(allow(OWNER_SID),))
            self.assertEqual(judge_security(theirs, OWNER_SID, default_owner_sid=group),
                             [f"owned by {group}, not the caller"])
            self.assertFalse(is_exactly_owner_only(theirs, OWNER_SID,
                                                   default_owner_sid=group))
        # The caller as its own default owner changes nothing.
        self.assertEqual(accepted_owners(OWNER_SID, OWNER_SID), frozenset({OWNER_SID}))
        # The elevated administrator's case still passes.
        self.assertEqual(accepted_owners(OWNER_SID, SID_ADMINISTRATORS),
                         frozenset({OWNER_SID, SID_ADMINISTRATORS}))
        self.assertEqual(judge_security(state(), OWNER_SID, default_owner_sid="nonsense"),
                         ["caller identity unknown"])

    def test_naming_the_caller_is_not_admitting_the_caller(self):
        """Codex review of ccb85ef, R4: an entry with an empty mask, or one
        that applies only to future children, names the caller without
        granting any access to this object."""
        empty = state(dacl=(allow(OWNER_SID, mask=0),))
        self.assertIn("the caller's entries grant no effective access",
                      judge_security(empty, OWNER_SID))
        children_only = state(directory=True, dacl=(
            allow(OWNER_SID, flags=INHERIT_ONLY_ACE | OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE),))
        self.assertIn("the caller's entries grant no effective access",
                      judge_security(children_only, OWNER_SID))
        read_only = state(dacl=(allow(OWNER_SID, mask=FILE_GENERIC_READ),))
        self.assertTrue(judge_security(read_only, OWNER_SID))
        split = state(dacl=(allow(OWNER_SID, mask=FILE_GENERIC_READ),
                            allow(OWNER_SID, mask=FILE_GENERIC_WRITE)))
        self.assertEqual(judge_security(split, OWNER_SID), [])
        owner_rights = state(dacl=(allow(SID_OWNER_RIGHTS, mask=GENERIC_ALL),))
        self.assertEqual(judge_security(owner_rights, OWNER_SID), [])
        self.assertTrue(grants_effective_access(GENERIC_READ | GENERIC_WRITE))
        self.assertFalse(grants_effective_access(GENERIC_READ))
        self.assertFalse(grants_effective_access(FILE_GENERIC_WRITE))

    def test_generic_rights_are_mapped_before_they_are_judged(self):
        """A mask mixing generic and specific bits grants what the kernel
        maps it to, so GENERIC_READ beside FILE_GENERIC_WRITE is read and
        write, and GENERIC_ALL alone is everything."""
        self.assertTrue(grants_effective_access(GENERIC_READ | FILE_GENERIC_WRITE))
        self.assertTrue(grants_effective_access(FILE_GENERIC_READ | GENERIC_WRITE))
        self.assertTrue(grants_effective_access(GENERIC_ALL))
        self.assertFalse(grants_effective_access(GENERIC_READ | acl.GENERIC_EXECUTE))
        self.assertEqual(map_generic_rights(GENERIC_ALL), FILE_ALL_ACCESS)
        self.assertEqual(map_generic_rights(GENERIC_READ | 0x1), FILE_GENERIC_READ | 0x1)
        self.assertEqual(map_generic_rights(FILE_GENERIC_WRITE), FILE_GENERIC_WRITE)
        self.assertEqual(map_generic_rights(0), 0)
        mixed = state(dacl=(allow(OWNER_SID, mask=GENERIC_READ | FILE_GENERIC_WRITE),))
        self.assertEqual(judge_security(mixed, OWNER_SID), [])

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

    def __init__(self, states: list[SecurityState | None],
                 caller: str | None = OWNER_SID, default_owner: str | None = None):
        self.states = list(states)
        self.caller = caller
        self.default_owner = default_owner if default_owner is not None else caller
        self.writes: list[tuple[int, str, bool]] = []

    def _caller_sid(self):
        return self.caller

    def _default_owner_sid(self):
        return self.default_owner

    def _read_security(self, handle, is_directory):
        observed = self.states.pop(0)
        return (observed, {}) if observed is not None else (None, {"read_error": 5})

    def _write_owner_only(self, handle, caller, is_directory):
        self.writes.append((handle, caller, is_directory))
        return None


PROTECT_SCOPE: dict[str, object] = {
    "judge_security": judge_security,
    "is_exactly_owner_only": is_exactly_owner_only,
    "accepted_owners": accepted_owners,
    "WINDOWS_OWNER_ONLY_GUARANTEE": WINDOWS_OWNER_ONLY_GUARANTEE,
    "SecurityState": SecurityState,
    "MAX_TREE_OBJECTS": 20000,
    "MAX_TREE_DEPTH": 32,
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
        self.assertNotIn("default_owner_sid", evidence)

    def test_an_elevated_administrators_own_object_is_protected_for_the_user(self):
        """The hosted runner's case: owner Administrators, token default
        owner Administrators. The write grants the user SID, and the exact
        read-back is judged with the same default owner."""
        before = state(SID_ADMINISTRATORS, protected=False, dacl=(
            allow(SID_SYSTEM, flags=INHERITED_ACE), allow(SID_ADMINISTRATORS, flags=INHERITED_ACE),
            allow(OWNER_SID, flags=INHERITED_ACE)))
        after = state(SID_ADMINISTRATORS, dacl=owner_only_aces(OWNER_SID, directory=False))
        platform = ScriptedPlatform([before, after], default_owner=SID_ADMINISTRATORS)
        verified, evidence = self.protect(platform, 42, False)
        self.assertTrue(verified, evidence)
        self.assertEqual(platform.writes, [(42, OWNER_SID, False)])
        self.assertEqual(evidence["default_owner_sid"], SID_ADMINISTRATORS)

    def test_a_foreign_owned_object_is_refused_before_anything_is_written(self):
        platform = ScriptedPlatform([state(OTHER_SID, dacl=(allow(SID_OWNER_RIGHTS),))])
        verified, evidence = self.protect(platform, 42, False)
        self.assertFalse(verified)
        self.assertEqual(platform.writes, [])
        self.assertEqual(evidence["refused"], "owned by another account")
        self.assertEqual(evidence["owner_sid"], OTHER_SID)

    def test_an_administrators_owned_object_is_foreign_to_a_non_elevated_user(self):
        platform = ScriptedPlatform([state(SID_ADMINISTRATORS, dacl=(allow(OWNER_SID),))])
        verified, evidence = self.protect(platform, 42, False)
        self.assertFalse(verified)
        self.assertEqual(platform.writes, [])
        self.assertEqual(evidence["refused"], "owned by another account")

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
                return "NtSetSecurityObject failed with 0xC0000022"

        verified, evidence = self.protect(FailingWrite([state()]), 42, False)
        self.assertFalse(verified)
        self.assertEqual(evidence["error"], "NtSetSecurityObject failed with 0xC0000022")
        verified, evidence = self.protect(ScriptedPlatform([None]), 42, False)
        self.assertFalse(verified)
        self.assertEqual(evidence["read_error"], 5)

    def test_an_unknown_caller_identity_writes_nothing(self):
        platform = ScriptedPlatform([state()], caller=None)
        verified, evidence = self.protect(platform, 42, False)
        self.assertFalse(verified)
        self.assertEqual(platform.writes, [])
        self.assertEqual(evidence["error"], "caller identity unavailable")


class FakeKernel32:
    def __init__(self, closed: list[int]):
        self.closed = closed

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1


class TreePlatform:
    """A scripted tree: ``objects`` maps a label to its decoded state (or
    None for an unreadable one), and ``children`` maps a directory label to
    the entries its handle enumerates. Root label is ``"."``."""

    def __init__(self, test: unittest.TestCase, objects: dict[str, SecurityState | None],
                 children: dict[str, list[tuple[str, int]]], *,
                 caller: str | None = OWNER_SID, default_owner: str | None = None,
                 unopenable: frozenset[str] = frozenset(),
                 sticky: frozenset[str] = frozenset()):
        self.test = test
        self.objects = objects
        self.children = children
        self.caller = caller
        self.default_owner = default_owner if default_owner is not None else caller
        self.unopenable = unopenable
        self.sticky = sticky
        self.handles: dict[int, str] = {}
        self.protected: list[str] = []
        self.writes_seen: list[str] = []
        self.opened_roots: list[tuple[str, bool, bool]] = []
        self.child_opens: list[tuple[str, str, bool, bool]] = []
        self.pins: list[bool] = []

    def _caller_sid(self):
        return self.caller

    def _default_owner_sid(self):
        return self.default_owner

    def _new_handle(self, label: str) -> int:
        handle = 100 + len(self.handles)
        self.handles[handle] = label
        return handle

    def _open_securable(self, path, *, write, listing=False):
        self.opened_roots.append((path, write, listing))
        observed = self.objects.get(".")
        return self._new_handle("."), bool(observed and observed.is_directory), {}

    def _open_child(self, parent, name, *, write, listing, pin=False):
        prefix = self.handles[parent]
        label = name if prefix == "." else f"{prefix}\\{name}"
        self.child_opens.append((prefix, name, write, listing))
        self.pins.append(pin)
        self.test.assertNotIn("\\", name)
        if label in self.unopenable or label not in self.objects:
            return None, False, {"open_status": "0xC0000034"}
        observed = self.objects[label]
        return self._new_handle(label), bool(observed and observed.is_directory), {}

    def _list_directory(self, handle):
        label = self.handles[handle]
        return tuple(DirectoryEntry(name, attributes)
                     for name, attributes in self.children.get(label, [])), {}

    def _read_security(self, handle, is_directory):
        observed = self.objects[self.handles[handle]]
        return (observed, {}) if observed is not None else (None, {"read_status": "0xC0000022"})

    def _protect_handle(self, handle, is_directory):
        label = self.handles[handle]
        self.protected.append(label)
        return label not in self.sticky, {}


DIRECTORY = FILE_ATTRIBUTE_DIRECTORY
FILE = 0x20


class TreeEnforcementTests(unittest.TestCase):
    """Per-object, handle-relative tree enforcement against a scripted tree."""

    def setUp(self):
        self.closed: list[int] = []
        scope = dict(PROTECT_SCOPE, kernel32=FakeKernel32(self.closed))
        self.enforce = windows_method("enforce_owner_only_tree", scope)
        self.observe = windows_method("observe_owner_only_tree", scope)
        for name in ("_walk_tree", "_open_tree"):
            windows_method(name, scope)
        self.scope = scope

    def platform(self, objects, children, **kwargs) -> TreePlatform:
        platform = TreePlatform(self, objects, children, **kwargs)
        # The lifted methods call each other through ``self``; bind them.
        for name in ("_walk_tree", "_open_tree"):
            setattr(platform, name, self.scope[name].__get__(platform))
        return platform

    def test_clean_objects_are_left_alone_and_failing_ones_replaced(self):
        objects = {
            ".": state(directory=True),
            "ok": state(protected=False, dacl=(allow(OWNER_SID, flags=INHERITED_ACE),)),
            "leaky": state(protected=False, dacl=(
                allow(OWNER_SID, flags=INHERITED_ACE), allow(EVERYONE, mask=0x1))),
        }
        platform = self.platform(objects, {".": [("ok", FILE), ("leaky", FILE)]})
        verified, evidence = self.enforce(platform, r"C:\store")
        self.assertTrue(verified, evidence)
        self.assertEqual(platform.protected, ["leaky"])
        self.assertEqual(evidence["objects_repaired"], 1)
        self.assertEqual(evidence["objects_seen"], 3)
        self.assertEqual(sorted(self.closed), sorted(platform.handles))

    def test_every_descendant_is_opened_relative_to_its_parent_and_enumerated_now(self):
        """Codex review of ccb85ef, R1: no absolute path is resolved below
        the root, and what is judged is what the directory handle lists
        now, so a directory replaced wholesale is seen with its extra
        objects included."""
        objects = {
            ".": state(directory=True),
            "sub": state(directory=True, protected=False,
                         dacl=(allow(OWNER_SID, flags=INHERITED_ACE | 3),)),
            "sub\\deep": state(protected=False, dacl=(allow(OWNER_SID, flags=INHERITED_ACE),)),
            "sub\\extra": state(protected=False, dacl=(
                allow(OWNER_SID, flags=INHERITED_ACE), allow(EVERYONE, mask=0x1))),
        }
        children = {".": [("sub", DIRECTORY)], "sub": [("deep", FILE), ("extra", FILE)]}
        platform = self.platform(objects, children)
        verified, evidence = self.observe(platform, r"C:\store")
        self.assertFalse(verified)
        self.assertEqual(evidence["objects_failed"], ["sub\\extra"])
        self.assertEqual(evidence["objects_seen"], 4)
        self.assertEqual(platform.opened_roots, [(r"C:\store", False, True)])
        self.assertEqual([(parent, name) for parent, name, _w, _l in platform.child_opens],
                         [(".", "sub"), ("sub", "deep"), ("sub", "extra")])
        self.assertEqual([listing for _p, name, _w, listing in platform.child_opens],
                         [True, False, False])

    def test_the_root_is_judged_strictly_and_descendants_leniently(self):
        objects = {
            ".": state(protected=False, directory=True,
                       dacl=(allow(OWNER_SID, flags=INHERITED_ACE | 3),)),
            "child": state(protected=False, dacl=(allow(OWNER_SID, flags=INHERITED_ACE),)),
        }
        platform = self.platform(objects, {".": [("child", FILE)]})
        verified, _ = self.enforce(platform, r"C:\store")
        self.assertTrue(verified)
        self.assertEqual(platform.protected, ["."])

    def test_a_foreign_owned_object_anywhere_stops_every_write(self):
        """Codex review of ccb85ef, R2: ownership of the whole tree is read
        before anything is written, so a parent that needs repair is not
        written while a foreign-owned child sits under it."""
        objects = {
            ".": state(protected=False, directory=True, dacl=(allow(EVERYONE),)),
            "theirs": state(OTHER_SID, dacl=(allow(SID_OWNER_RIGHTS),)),
        }
        platform = self.platform(objects, {".": [("theirs", FILE)]})
        verified, evidence = self.enforce(platform, r"C:\store")
        self.assertFalse(verified)
        self.assertEqual(platform.protected, [])
        self.assertEqual(evidence["objects_foreign_owned"], ["theirs"])
        self.assertEqual(sorted(self.closed), sorted(platform.handles))

    def test_an_administrators_owned_tree_is_the_elevated_callers_own(self):
        objects = {
            ".": state(SID_ADMINISTRATORS, protected=False, directory=True,
                       dacl=(allow(SID_SYSTEM, flags=3), allow(SID_ADMINISTRATORS, flags=3),
                             allow(OWNER_SID, flags=3))),
            "f": state(SID_ADMINISTRATORS, protected=False,
                       dacl=(allow(OWNER_SID, flags=INHERITED_ACE),)),
        }
        platform = self.platform(objects, {".": [("f", FILE)]},
                                 default_owner=SID_ADMINISTRATORS)
        verified, evidence = self.enforce(platform, r"C:\store")
        self.assertTrue(verified, evidence)
        self.assertEqual(platform.protected, ["."])
        self.assertEqual(evidence["objects_foreign_owned"], [])
        platform = self.platform(objects, {".": [("f", FILE)]})
        verified, evidence = self.enforce(platform, r"C:\store")
        self.assertFalse(verified)
        self.assertEqual(evidence["objects_foreign_owned"], [".", "f"])

    def test_an_object_that_cannot_be_opened_or_repaired_fails_the_pass(self):
        objects = {".": state(directory=True),
                   "sticky": state(dacl=(allow(OWNER_SID), allow(EVERYONE, mask=0x1)))}
        platform = self.platform(objects, {".": [("sticky", FILE)]}, sticky=frozenset({"sticky"}))
        verified, evidence = self.enforce(platform, r"C:\store")
        self.assertFalse(verified)
        self.assertEqual(evidence["objects_failed"], ["sticky"])
        objects = {".": state(directory=True), "gone": state()}
        platform = self.platform(objects, {".": [("gone", FILE), ("later", FILE)]},
                                 unopenable=frozenset({"gone"}))
        verified, evidence = self.enforce(platform, r"C:\store")
        self.assertFalse(verified)
        self.assertEqual(evidence["objects_failed"], ["gone"])
        self.assertEqual(evidence["open_status"], "0xC0000034")
        self.assertEqual(platform.protected, [])

    def test_a_reparse_point_inside_the_tree_refuses_the_tree(self):
        class ReparsePlatform(TreePlatform):
            def _open_child(self, parent, name, *, write, listing, pin=False):
                if name == "jx":
                    return None, False, {"refused": "reparse point"}
                return super()._open_child(parent, name, write=write, listing=listing, pin=pin)

        objects = {".": state(directory=True), "f": state()}
        platform = ReparsePlatform(self, objects, {".": [("f", FILE), ("jx", DIRECTORY | 0x400)]})
        for name in ("_walk_tree", "_open_tree"):
            setattr(platform, name, self.scope[name].__get__(platform))
        verified, evidence = self.observe(platform, r"C:\store")
        self.assertFalse(verified)
        self.assertEqual(evidence["refused"], "reparse point")
        self.assertIn("jx", evidence["objects_failed"])

    def test_an_object_that_changes_kind_between_listing_and_open_stops_the_walk(self):
        objects = {".": state(directory=True), "swap": state(directory=False)}
        platform = self.platform(objects, {".": [("swap", DIRECTORY)]})
        verified, evidence = self.enforce(platform, r"C:\store")
        self.assertFalse(verified)
        self.assertEqual(evidence["error"], "object changed kind between listing and open")
        self.assertEqual(platform.protected, [])

    def test_a_tree_larger_than_the_lane_creates_is_refused(self):
        objects = {".": state(directory=True), **{f"f{i}": state() for i in range(5)}}
        platform = self.platform(objects, {".": [(f"f{i}", FILE) for i in range(5)]})
        verified, evidence = self.enforce(platform, r"C:\store", max_objects=3)
        self.assertFalse(verified)
        self.assertIn("more objects than the lane creates", evidence["error"])
        self.assertEqual(platform.protected, [])
        self.assertEqual(sorted(self.closed), sorted(platform.handles))

    def test_the_object_limit_is_exact(self):
        """A tree of exactly ``max_objects`` objects, the root included,
        passes; one more is refused before it is opened."""
        objects = {".": state(directory=True), "a": state(), "b": state(), "c": state()}
        platform = self.platform(objects, {".": [("a", FILE), ("b", FILE)]})
        verified, evidence = self.observe(platform, r"C:\store", max_objects=3)
        self.assertTrue(verified, evidence)
        self.assertEqual(evidence["objects_seen"], 3)
        platform = self.platform(objects, {".": [("a", FILE), ("b", FILE), ("c", FILE)]})
        verified, evidence = self.observe(platform, r"C:\store", max_objects=3)
        self.assertFalse(verified)
        self.assertEqual(evidence["path"], "c")
        self.assertEqual(evidence["objects_seen"], 3)
        self.assertEqual([name for _p, name, _w, _l in platform.child_opens], ["a", "b"])

    def test_an_unreadable_descriptor_stops_the_pass_before_any_write(self):
        """Codex review of ccb85ef..2e9ed0f, B4: an object whose owner
        could not be read may be another account's. Nothing is written
        while any ownership read failed, even with no owner found foreign
        and the walk complete."""
        objects = {
            ".": state(directory=True),
            "leaky": state(protected=False, dacl=(
                allow(OWNER_SID, flags=INHERITED_ACE), allow(EVERYONE, mask=0x1))),
            "opaque": None,
        }
        platform = self.platform(objects, {".": [("leaky", FILE), ("opaque", FILE)]})
        verified, evidence = self.enforce(platform, r"C:\store")
        self.assertFalse(verified)
        self.assertEqual(platform.protected, [])
        self.assertEqual(evidence["objects_repaired"], 0)
        self.assertEqual(evidence["objects_failed"], ["opaque"])
        self.assertEqual(evidence["objects_foreign_owned"], [])
        self.assertEqual(sorted(self.closed), sorted(platform.handles))

    def test_the_root_open_asks_for_write_only_when_enforcing(self):
        objects = {".": state(directory=True)}
        platform = self.platform(objects, {".": []})
        self.observe(platform, r"C:\store")
        self.enforce(platform, r"C:\store")
        self.assertEqual(platform.opened_roots,
                         [(r"C:\store", False, True), (r"C:\store", True, True)])

    def test_only_the_proving_pass_pins_files(self):
        """Codex re-review of d1699c9: the read-only pass asks read access
        on every child so a file keeps its name until judged; enforcement
        does not, so a file the owner cannot read can still be repaired."""
        objects = {".": state(directory=True), "f": state()}
        platform = self.platform(objects, {".": [("f", FILE)]})
        self.observe(platform, r"C:\store")
        self.assertEqual(platform.pins, [True])
        platform = self.platform(objects, {".": [("f", FILE)]})
        self.enforce(platform, r"C:\store")
        self.assertEqual(platform.pins, [False])


class HandleBoundSourceTests(unittest.TestCase):
    """Structural facts about the shipped Windows layer that the scripted
    tests above cannot see: what it calls and what it never calls."""

    ACL_METHODS = (
        "enforce_owner_only_file", "verify_owner_only_path", "_token_sid",
        "_caller_sid", "_default_owner_sid", "_examine_handle",
        "_open_securable", "_open_child", "_reopen_descriptor",
        "_list_directory", "_read_security", "_write_owner_only",
        "_protect_handle", "_protect_path", "_protect_descriptor",
        "_set_and_verify_owner_acl", "_observe_handle",
        "observe_owner_only_acl", "acl_diagnostics", "_walk_tree",
        "_open_tree", "observe_owner_only_tree", "enforce_owner_only_tree",
    )

    def test_no_helper_program_and_no_text_parsing_remain_on_the_acl_path(self):
        for name in self.ACL_METHODS:
            source = method_source(name)
            self.assertNotIn("subprocess", source, name)
            self.assertNotIn("icacls", source, name)
            self.assertNotIn("whoami", source, name)
            self.assertNotIn("scandir", source, name)
            self.assertNotIn("os.path.join", source, name)

    def test_the_dacl_is_written_by_exactly_one_non_propagating_call(self):
        writers = [name for name in self.ACL_METHODS
                   if "NtSetSecurityObject" in method_source(name)]
        self.assertEqual(writers, ["_write_owner_only"])
        writer = method_source("_write_owner_only")
        self.assertIn("PROTECTED_DACL_SECURITY_INFORMATION", writer)
        self.assertEqual(writer.count("ntdll.NtSetSecurityObject("), 1)
        # advapi32's handle-based writer propagates inheritable entries to
        # existing descendants; it must not appear anywhere in the module.
        self.assertNotIn("SetSecurityInfo", WINDOWS_SOURCE)
        self.assertNotIn("SetNamedSecurityInfo", WINDOWS_SOURCE)
        self.assertNotIn("UNPROTECTED_DACL_SECURITY_INFORMATION", WINDOWS_SOURCE)
        self.assertNotIn("/remove", WINDOWS_SOURCE)
        self.assertNotIn("/reset", WINDOWS_SOURCE)

    def test_every_open_refuses_reparse_points_and_goes_through_one_examiner(self):
        for name in ("_open_securable", "_open_child", "_reopen_descriptor"):
            self.assertIn("_examine_handle", method_source(name), name)
        self.assertIn("FILE_FLAG_OPEN_REPARSE_POINT", method_source("_open_securable"))
        self.assertIn("FILE_FLAG_OPEN_REPARSE_POINT", method_source("_reopen_descriptor"))
        self.assertIn("FILE_OPEN_REPARSE_POINT", method_source("_open_child"))
        self.assertIn("FILE_ATTRIBUTE_REPARSE_POINT", method_source("_examine_handle"))
        self.assertEqual(WINDOWS_SOURCE.count("kernel32.CreateFileW("), 1)
        self.assertEqual(WINDOWS_SOURCE.count("ntdll.NtCreateFile("), 1)

    def test_descendants_are_opened_relative_to_their_parent_handle(self):
        child = method_source("_open_child")
        self.assertIn("RootDirectory", WINDOWS_SOURCE)
        self.assertIn("_OBJECT_ATTRIBUTES(", child)
        self.assertIn("parent", child)
        walk = method_source("_walk_tree")
        self.assertIn("_open_child(", walk)
        self.assertIn("_list_directory(", walk)
        self.assertNotIn("_open_securable", walk)
        self.assertNotIn("CreateFileW", walk)
        self.assertEqual(method_source("_open_tree").count("_open_securable("), 1)

    def test_the_open_file_is_protected_through_its_own_handle(self):
        source = method_source("enforce_owner_only_file")
        self.assertIn("_protect_descriptor", source)
        self.assertNotIn("_set_and_verify_owner_acl", source)
        self.assertIn("ReOpenFile", method_source("_reopen_descriptor"))

    def test_ownership_is_checked_before_the_write(self):
        source = method_source("_protect_handle")
        self.assertLess(source.index("owner_sid not in owners"),
                        source.index("_write_owner_only"))

    def test_the_tree_pass_reads_every_owner_before_it_writes_anything(self):
        source = method_source("enforce_owner_only_tree")
        self.assertIn("judge_security", source)
        self.assertLess(source.index("owner_sid not in owners"),
                        source.index("_protect_handle"))
        self.assertRegex(
            source, r"if not foreign_owned and \(?not walk_error\)? and \(?not failed\)?:")

    def test_every_open_denies_delete_sharing(self):
        """Codex review of ccb85ef..2e9ed0f, B1: a handle held without
        FILE_SHARE_DELETE pins the object's name, so a directory cannot be
        renamed away and replaced between its open and the judgment of
        what it lists. No open here may offer delete sharing."""
        self.assertNotIn("FILE_SHARE_ALL", WINDOWS_SOURCE)
        self.assertNotIn("FILE_SHARE_DELETE", WINDOWS_SOURCE)
        self.assertNotIn("0x00000007", WINDOWS_SOURCE)
        self.assertIn("FILE_SHARE_KEEP_NAME = 0x00000003", WINDOWS_SOURCE)
        for name in ("_open_securable", "_open_child", "_reopen_descriptor"):
            self.assertIn("FILE_SHARE_KEEP_NAME", method_source(name), name)

    def test_the_proving_pass_asks_read_access_so_files_are_pinned(self):
        child = method_source("_open_child")
        self.assertIn("FILE_READ_DATA if pin else 0", child)
        self.assertIn("pin=True", method_source("observe_owner_only_tree"))
        self.assertNotIn("pin=True", method_source("enforce_owner_only_tree"))
        self.assertIn("pin=pin", method_source("_walk_tree"))

    def test_child_names_are_measured_in_utf16_bytes(self):
        child = method_source("_open_child")
        self.assertIn("encode_object_name(", child)
        self.assertNotIn("create_unicode_buffer", child)
        self.assertNotIn("len(name) * 2", child)
        self.assertIn("ctypes.sizeof(buffer)", child)

    def test_the_caller_identity_comes_from_the_token_not_from_a_name(self):
        source = method_source("_token_sid")
        self.assertIn("OpenProcessToken", source)
        self.assertIn("GetTokenInformation", source)
        self.assertNotIn("environ", source)
        self.assertNotIn("getlogin", source)
        self.assertIn("TOKEN_USER_CLASS", method_source("_caller_sid"))
        self.assertIn("TOKEN_OWNER_CLASS", method_source("_default_owner_sid"))


@unittest.skipUnless(sys.platform == "win32", "native Windows ACL behaviour")
class NativeTreeTests(unittest.TestCase):
    """Run against the real kernel: what the scripted tests cannot show.
    The store is created by this account; on an elevated CI runner its
    objects are owned by Administrators, which the token's default owner
    admits."""

    def setUp(self) -> None:
        import platform_support  # noqa: F401  (drops ACL-bypass privileges)
        from agent_bridge.platform import platform as host
        self.host = host
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "store"
        self.root.mkdir()
        protected, evidence = host.enforce_owner_only_tree(str(self.root))
        self.assertTrue(protected, evidence)

    def test_a_directory_under_an_open_handle_cannot_be_renamed_or_replaced(self):
        """Acceptance for B1: while the tree pass holds ``sub``, a rename
        of ``sub`` (the first half of swapping it for a directory holding
        an extra permissive file) fails with a sharing violation, so the
        swap cannot happen and what is judged is what was opened."""
        from platform_support import make_permissive
        sub = self.root / "sub"
        sub.mkdir()
        (sub / "deep.txt").write_bytes(b"d")
        protected, evidence = self.host.enforce_owner_only_tree(str(self.root))
        self.assertTrue(protected, evidence)
        attempts: list[BaseException | None] = []
        original = self.host._list_directory

        def list_directory(handle):
            entries, error = original(handle)
            if entries is not None and any(e.name == "deep.txt" for e in entries):
                # We are inside ``sub`` now, with its handle open: attempt the swap.
                try:
                    os.rename(sub, self.root / "sub.old")
                    replacement = self.root / "sub"
                    replacement.mkdir()
                    (replacement / "extra.txt").write_bytes(b"x")
                    make_permissive(replacement / "extra.txt")
                    attempts.append(None)
                except OSError as exc:
                    attempts.append(exc)
            return entries, error

        with mock.patch.object(self.host, "_list_directory", list_directory):
            verified, evidence = self.host.observe_owner_only_tree(str(self.root))
        self.assertEqual(len(attempts), 1, attempts)
        self.assertIsInstance(attempts[0], PermissionError, attempts)
        self.assertTrue(verified, evidence)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["sub"])
        self.assertEqual(evidence["objects_seen"], 3)
        # The handle is released with the pass: the rename works afterwards.
        os.rename(sub, self.root / "sub.old")
        self.assertTrue((self.root / "sub.old" / "deep.txt").exists())

    def test_the_root_itself_cannot_be_renamed_while_a_pass_holds_it(self):
        attempts: list[BaseException | None] = []
        original = self.host._read_security

        def read_security(handle, is_directory):
            if not attempts:
                try:
                    os.rename(self.root, self.root.with_name("store.old"))
                    attempts.append(None)
                except OSError as exc:
                    attempts.append(exc)
            return original(handle, is_directory)

        with mock.patch.object(self.host, "_read_security", read_security):
            verified, evidence = self.host.observe_owner_only_tree(str(self.root))
        self.assertTrue(verified, evidence)
        self.assertIsInstance(attempts[0], PermissionError, attempts)
        self.assertTrue(self.root.exists())

    def test_a_file_cannot_be_replaced_while_the_proving_pass_holds_it(self):
        """Codex re-review of d1699c9: with the file's handle open in the
        read-only pass, renaming it away (the first half of swapping in a
        permissive file under its name) fails with a sharing violation."""
        from platform_support import make_permissive
        target = self.root / "private.txt"
        target.write_bytes(b"p")
        protected, evidence = self.host.enforce_owner_only_tree(str(self.root))
        self.assertTrue(protected, evidence)
        attempts: list[BaseException | None] = []
        original = self.host._read_security

        def read_security(handle, is_directory):
            if not is_directory and not attempts:
                try:
                    os.rename(target, self.root / "private.old")
                    replacement = self.root / "private.txt"
                    replacement.write_bytes(b"x")
                    make_permissive(replacement)
                    attempts.append(None)
                except OSError as exc:
                    attempts.append(exc)
            return original(handle, is_directory)

        with mock.patch.object(self.host, "_read_security", read_security):
            verified, evidence = self.host.observe_owner_only_tree(str(self.root))
        self.assertEqual(len(attempts), 1, attempts)
        self.assertIsInstance(attempts[0], PermissionError, attempts)
        self.assertTrue(verified, evidence)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["private.txt"])
        # Enforcement does not pin files: the same rename succeeds under it.
        attempts.clear()
        with mock.patch.object(self.host, "_read_security", read_security):
            protected, evidence = self.host.enforce_owner_only_tree(str(self.root))
        self.assertEqual(attempts, [None], attempts)
        self.assertTrue((self.root / "private.old").exists())

    def test_a_supplementary_character_name_opens_that_name_and_no_shorter_one(self):
        """Acceptance for B3: with ``\U0001f9ea.tx`` owner-only beside a
        permissive ``\U0001f9ea.txt``, a Length short by one code unit would
        open the former and pass; the pass must judge the latter and fail."""
        from platform_support import make_permissive
        short = self.root / "\U0001f9ea.tx"
        full = self.root / "\U0001f9ea.txt"
        short.write_bytes(b"s")
        full.write_bytes(b"f")
        protected, evidence = self.host.enforce_owner_only_tree(str(self.root))
        self.assertTrue(protected, evidence)
        verified, evidence = self.host.observe_owner_only_tree(str(self.root))
        self.assertTrue(verified, evidence)
        self.assertEqual(evidence["objects_seen"], 3)
        make_permissive(full)
        verified, evidence = self.host.observe_owner_only_tree(str(self.root))
        self.assertFalse(verified, evidence)
        self.assertEqual(evidence["objects_failed"], ["\U0001f9ea.txt"])
        protected, evidence = self.host.enforce_owner_only_tree(str(self.root))
        self.assertTrue(protected, evidence)
        self.assertEqual(evidence["objects_repaired"], 1)
        self.assertTrue(self.host.observe_owner_only_tree(str(self.root))[0])


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

    def test_store_never_writes_bytes_through_a_rejected_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = os.path.join(temp, "state.json")
            rejecting = RejectingFilePlatform()
            with mock.patch.object(store, "platform", rejecting):
                with self.assertRaises(PermissionError):
                    store.atomic_write_bytes(target, b"secret")
            self.assertFalse(os.path.exists(target))
            self.assertEqual(os.listdir(temp), [])
            with self.assertRaises(OSError):
                os.fstat(rejecting.fd)

    def test_a_rejected_write_leaves_an_existing_file_and_no_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = os.path.join(directory, "must-not-change")
            store.atomic_write_bytes(target, b"original")
            rejecting = RejectingFilePlatform()
            with mock.patch.object(store, "platform", rejecting):
                with self.assertRaises(PermissionError):
                    store.atomic_write_bytes(target, b"blocked")
            self.assertEqual(Path(target).read_bytes(), b"original")
            self.assertEqual(os.listdir(directory), ["must-not-change"])
            with self.assertRaises(OSError):
                os.fstat(rejecting.fd)


if __name__ == "__main__":
    unittest.main()
