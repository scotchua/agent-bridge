"""Direct behavior checks for owner-only filesystem primitives."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from agent_bridge import store  # noqa: E402


class ReplaceTests(unittest.TestCase):
    def test_permission_error_on_posix_is_raised_without_retry(self):
        with mock.patch.object(store, "WINDOWS", False), \
             mock.patch.object(store, "REPLACE", side_effect=PermissionError), \
             mock.patch.object(store.time, "sleep", side_effect=AssertionError("retried")):
            with self.assertRaises(PermissionError):
                store.replace("source", "destination")


class SetUmaskTests(unittest.TestCase):
    def test_set_umask_calls_platform(self):
        with mock.patch.object(store.platform, "set_owner_only_umask") as set_mode:
            store.set_umask()
        set_mode.assert_called_once_with()


class SecureMkdirTests(unittest.TestCase):
    def test_creates_all_missing_parents(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "invented" / "archive"
            store.secure_mkdir(str(target))
            self.assertTrue(target.is_dir())

    def test_existing_leaf_is_chmodded_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "invented"
            target.mkdir()
            mode = mock.Mock(st_mode=stat.S_IFDIR | 0o755)
            with mock.patch.object(store.os, "stat", return_value=mode), \
                 mock.patch.object(store.os, "chmod") as chmod:
                store.secure_mkdir(str(target))
            chmod.assert_called_once_with(str(target), store.DIR_MODE)

    def test_directory_created_by_racer_is_chmodded(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "invented"
            real_mkdir = os.mkdir

            def raced_mkdir(path, mode):
                real_mkdir(path, 0o755)
                raise FileExistsError(path)

            with mock.patch.object(store.os, "mkdir", side_effect=raced_mkdir), \
                 mock.patch.object(store.os, "chmod") as chmod:
                store.secure_mkdir(str(target))
            chmod.assert_called_once_with(str(target), store.DIR_MODE)

    def test_returns_absolute_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "invented"
            self.assertEqual(store.secure_mkdir(str(target)), os.path.abspath(target))


class AtomicWriteJsonTests(unittest.TestCase):
    def test_json_write_ends_with_one_newline(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "invented.json"
            store.atomic_write_json(str(path), {"account": "Orchid", "amount": 17})
            self.assertEqual(path.read_bytes(),
                             b'{\n  "account": "Orchid",\n  "amount": 17\n}\n')


class ReadJsonAtomicTests(unittest.TestCase):
    def test_returns_decoded_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "invented.json"
            path.write_text(json.dumps({"account": "Orchid"}), encoding="utf-8")
            self.assertEqual(store.read_json_atomic(str(path)), {"account": "Orchid"})

    def test_raises_at_grace_deadline(self):
        with mock.patch.object(store.time, "monotonic", side_effect=[0.0, 1.0]), \
             mock.patch.object(store, "read_json", side_effect=FileNotFoundError), \
             mock.patch.object(store.time, "sleep", side_effect=AssertionError("retried")):
            with self.assertRaises(FileNotFoundError):
                store.read_json_atomic("missing.json")

    def test_grace_is_one_second(self):
        with mock.patch.object(store.time, "monotonic", side_effect=[0.0, 1.1]), \
             mock.patch.object(store, "read_json", side_effect=FileNotFoundError), \
             mock.patch.object(store.time, "sleep", side_effect=AssertionError("retried")):
            with self.assertRaises(FileNotFoundError):
                store.read_json_atomic("missing.json")


class ReadJsonOrNoneTests(unittest.TestCase):
    def test_malformed_document_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "invented.json"
            path.write_text("{bad json", encoding="utf-8")
            self.assertIsNone(store.read_json_or_none(str(path)))


class FileLockTests(unittest.TestCase):
    def test_lock_descriptor_is_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(store.os, "close", wraps=os.close) as close:
                with store.file_lock(str(Path(tmp) / "invented.lock")):
                    pass
                close.assert_called()


class AppendLedgerTests(unittest.TestCase):
    def test_record_is_fsynced(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(store.os, "fsync", wraps=os.fsync) as fsync:
                store.append_ledger(str(Path(tmp) / "invented.jsonl"), {"amount": 17})
            fsync.assert_called()

    def test_records_are_newline_delimited(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "invented.jsonl"
            store.append_ledger(str(path), {"amount": 17})
            store.append_ledger(str(path), {"amount": 23})
            self.assertEqual(path.read_bytes().splitlines(),
                             [b'{"amount": 17}', b'{"amount": 23}'])
            self.assertTrue(path.read_bytes().endswith(b"\n"))


class Sha256BytesTests(unittest.TestCase):
    def test_hashes_bytes(self):
        payload = b"Orchid account 17"
        self.assertEqual(store.sha256_bytes(payload), hashlib.sha256(payload).hexdigest())


class Sha256TextTests(unittest.TestCase):
    def test_hashes_utf8_text(self):
        payload = "Orchid account 17 \u20ac"
        self.assertEqual(store.sha256_text(payload),
                         hashlib.sha256(payload.encode("utf-8")).hexdigest())


class Sha256FileTests(unittest.TestCase):
    def test_hashes_file_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "invented.txt"
            payload = b"Orchid account 17"
            path.write_bytes(payload)
            self.assertEqual(store.sha256_file(str(path)), hashlib.sha256(payload).hexdigest())


class UtcNowTests(unittest.TestCase):
    def test_timestamp_has_utc_timezone_and_milliseconds(self):
        value = store.utc_now()
        parsed = datetime.fromisoformat(value)
        self.assertEqual(parsed.utcoffset(), timezone.utc.utcoffset(parsed))
        self.assertRegex(value, r"\.\d{3}\+00:00$")


if __name__ == "__main__":
    unittest.main()
