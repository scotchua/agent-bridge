"""Regression tests for Windows delegation behavior exposed by mutation probes."""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge.orchestration import windows_delegation as wd


class IdentityOfTests(unittest.TestCase):
    def test_known_identity_is_kept_without_reexamining_the_path(self):
        approved = mock.Mock(st_dev=7, st_ino=11)
        entry = mock.Mock(path="invented-repository/entry.txt")
        with mock.patch.object(wd.os, "lstat") as lstat:
            self.assertIs(wd._identity_of(entry, approved), approved)
        lstat.assert_not_called()


class ResolveBaseTests(unittest.TestCase):
    def test_a_base_one_character_over_the_limit_is_refused(self):
        run = mock.Mock(return_value="a" * 40)
        with self.assertRaises(wd.DelegationRefused) as caught:
            wd.resolve_base("invented-repository", "r" * 257, run=run)
        self.assertEqual(caught.exception.reason, "base_invalid")
        run.assert_not_called()


class WriteOwnerOnlyTests(unittest.TestCase):
    def test_existing_regular_result_is_replaced_with_new_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "guest.stdout"
            target.write_bytes(b"old invented answer")
            wd._write_owner_only(target, b"new invented answer")
            self.assertEqual(target.read_bytes(), b"new invented answer")


if __name__ == "__main__":
    unittest.main()
