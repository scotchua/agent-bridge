"""Boundary cases found by offline mutation testing of the Windows ACL codec."""

from __future__ import annotations

from pathlib import Path
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_bridge.platform import windows_acl as acl


class ParseSidBoundaryTests(unittest.TestCase):
    def test_zero_and_maximum_sub_authority_counts(self):
        self.assertEqual(acl.parse_sid(acl.encode_sid("S-1-5")), "S-1-5")
        sid = "S-1-5-" + "-".join(str(value) for value in range(15))
        self.assertEqual(acl.parse_sid(acl.encode_sid(sid)), sid)

    def test_authority_at_the_hex_boundary(self):
        raw = bytes((1, 0)) + (1 << 32).to_bytes(6, "big")
        self.assertEqual(acl.parse_sid(raw), "S-1-0x000100000000")


class EncodeSidBoundaryTests(unittest.TestCase):
    def test_sid_without_sub_authorities_is_valid(self):
        self.assertEqual(acl.encode_sid("S-1-5"), b"\x01\x00\x00\x00\x00\x00\x00\x05")

    def test_incomplete_sid_raises_value_error(self):
        with self.assertRaises(ValueError):
            acl.encode_sid("S")


class AclSizeBoundaryTests(unittest.TestCase):
    def test_header_only_acl_has_a_valid_size(self):
        header = struct.pack("<BBHHH", 2, 0, 8, 0, 0)
        self.assertEqual(acl.acl_size(header), 8)


class ParseAclBoundaryTests(unittest.TestCase):
    def test_header_only_acl_is_empty(self):
        header = struct.pack("<BBHHH", 2, 0, 8, 0, 0)
        self.assertEqual(acl.parse_acl(header), ())

    def test_header_only_uninterpreted_ace_is_retained(self):
        header = struct.pack("<BBHHH", 2, 0, 12, 1, 0)
        ace = struct.pack("<BBH", 9, 0, 4)
        self.assertEqual(acl.parse_acl(header + ace), (acl.Ace(9, 0, 0, None),))

    def test_zero_length_ace_is_refused(self):
        header = struct.pack("<BBHHH", 2, 0, 12, 1, 0)
        ace = struct.pack("<BBH", 9, 0, 0)
        with self.assertRaises(ValueError):
            acl.parse_acl(header + ace)


class ParseSecurityDescriptorBoundaryTests(unittest.TestCase):
    def test_owner_sid_ending_at_buffer_boundary(self):
        owner = acl.encode_sid("S-1-5")
        header = struct.pack("<BBHIIII", 1, 0, acl.SE_SELF_RELATIVE, 20, 0, 0, 0)
        state = acl.parse_security_descriptor(header + owner, is_directory=False)
        self.assertEqual(state.owner_sid, "S-1-5")

    def test_empty_dacl_ending_at_buffer_boundary(self):
        dacl = struct.pack("<BBHHH", 2, 0, 8, 0, 0)
        header = struct.pack("<BBHIIII", 1, 0,
                             acl.SE_SELF_RELATIVE | acl.SE_DACL_PRESENT, 0, 0, 0, 20)
        state = acl.parse_security_descriptor(header + dacl, is_directory=False)
        self.assertEqual(state.dacl, ())

    def test_dacl_offset_is_ignored_without_present_bit(self):
        dacl = struct.pack("<BBHHH", 2, 0, 8, 0, 0)
        header = struct.pack("<BBHIIII", 1, 0, acl.SE_SELF_RELATIVE, 0, 0, 0, 20)
        state = acl.parse_security_descriptor(header + dacl, is_directory=False)
        self.assertIsNone(state.dacl)


if __name__ == "__main__":
    unittest.main()
