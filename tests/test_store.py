"""atomic_write_bytes's owner_only switch.

A file this project owns (its own state, receipts) should get the owner-only
lockdown atomic_write_bytes has always applied. A file this project does not
own -- pre-existing config another tool created and manages -- should not:
the lockdown is what silently strips inherited access (e.g. SYSTEM,
Administrators on Windows) an atomic replace would otherwise carry onto it.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from agent_bridge import store  # noqa: E402


class AtomicWriteOwnerOnlyTests(unittest.TestCase):
    def test_default_and_explicit_true_enforce_the_lockdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, kwargs in (("default", {}), ("explicit", {"owner_only": True})):
                path = os.path.join(tmp, name)
                with mock.patch.object(store.platform, "enforce_owner_only_file") as enforce:
                    store.atomic_write_bytes(path, b"data", **kwargs)
                enforce.assert_called_once()
                self.assertEqual(Path(path).read_bytes(), b"data")

    def test_owner_only_false_skips_the_lockdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "host_owned")
            with mock.patch.object(store.platform, "enforce_owner_only_file") as enforce:
                store.atomic_write_bytes(path, b"data", owner_only=False)
            enforce.assert_not_called()
            self.assertEqual(Path(path).read_bytes(), b"data")


class ReadJsonBomTests(unittest.TestCase):
    # A Windows editor or PowerShell's default encoding can prepend a UTF-8
    # BOM to a file this project never wrote itself. Strict utf-8 decoding
    # turns that into a JSONDecodeError on an otherwise-valid file.
    def test_read_json_strips_a_leading_bom(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            Path(path).write_bytes(b"\xef\xbb\xbf" + b'{"a": 1}')
            self.assertEqual(store.read_json(path), {"a": 1})

    def test_read_json_without_a_bom_is_unaffected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            Path(path).write_bytes(b'{"a": 1}')
            self.assertEqual(store.read_json(path), {"a": 1})

    def test_read_json_or_none_strips_a_leading_bom(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            Path(path).write_bytes(b"\xef\xbb\xbf" + b'{"a": 1}')
            self.assertEqual(store.read_json_or_none(path), {"a": 1})


if __name__ == "__main__":
    unittest.main()
