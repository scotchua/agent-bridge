"""Boundary and helper checks found by offline windows_privacy mutations."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge.orchestration import windows_privacy as wpv
import platform_support


class ReparseTagMutationTests(unittest.TestCase):
    def test_reparse_tag_is_preserved(self):
        self.assertEqual(wpv._reparse_tag(SimpleNamespace(st_reparse_tag=42)), 42)


class ReadPrivateFileMutationTests(unittest.TestCase):
    def test_file_exactly_at_limit_is_read(self):
        with tempfile.TemporaryDirectory() as home:
            target = Path(home) / "record.bin"
            platform_support.write_private_bytes(target, b"abcd")
            payload, identity = wpv.read_private_file(target, root=home, max_bytes=4)
            self.assertEqual(payload, b"abcd")
            self.assertEqual(identity.size, 4)


class DescriptorAclMutationTests(unittest.TestCase):
    @unittest.skipIf(not hasattr(os, "getuid"), "POSIX mode bits")
    def test_other_execute_bit_is_refused_before_platform_check(self):
        with tempfile.TemporaryDirectory() as home:
            target = Path(home) / "record.bin"
            target.write_bytes(b"data")
            target.chmod(0o601)
            descriptor = os.open(target, os.O_RDONLY)
            platform = SimpleNamespace(observe_owner_only_acl=lambda _path: (True, {}))
            try:
                with self.assertRaises(wpv.PrivacyError) as caught:
                    wpv._verify_descriptor_acl(descriptor, target, platform)
            finally:
                os.close(descriptor)
            self.assertEqual(caught.exception.reason, "file_not_owner_only")


class DescriptorPathMutationTests(unittest.TestCase):
    def test_descriptor_resolver_supplies_the_checked_path(self):
        fallback = Path("fallback.bin")
        resolved = Path("resolved.bin")
        platform = SimpleNamespace(_path_from_fd=lambda _descriptor: str(resolved))
        self.assertEqual(wpv._descriptor_path(7, fallback, platform), resolved)

    def test_resolver_failure_is_refused(self):
        def fail(_descriptor):
            raise OSError("unavailable")

        platform = SimpleNamespace(_path_from_fd=fail)
        with self.assertRaises(wpv.PrivacyError) as caught:
            wpv._descriptor_path(7, Path("fallback.bin"), platform)
        self.assertEqual(caught.exception.reason, "acl_unverified")


class ExclusiveLockMutationTests(unittest.TestCase):
    def test_available_lock_enters_context_at_zero_timeout(self):
        with tempfile.TemporaryDirectory() as home:
            with wpv.exclusive_lock(Path(home) / "record.lock", timeout=0, root=home):
                pass

    def test_positive_timeout_allows_a_second_attempt(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.object(wpv, "_try_lock", side_effect=[False, True]), \
                    mock.patch.object(wpv.time, "monotonic", side_effect=[100, 100.1]), \
                    mock.patch.object(wpv.time, "sleep"):
                with wpv.exclusive_lock(Path(home) / "record.lock", timeout=1,
                                        root=home):
                    pass


class TryLockMutationTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX flock")
    def test_flock_success_is_reported(self):
        with tempfile.TemporaryFile() as target:
            self.assertTrue(wpv._try_lock(target.fileno()))
            wpv._unlock(target.fileno())


class UnlockMutationTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX flock")
    def test_unlock_releases_the_handle_before_close(self):
        import fcntl

        with tempfile.TemporaryDirectory() as home:
            target = Path(home) / "record.lock"
            first = os.open(target, os.O_RDWR | os.O_CREAT, 0o600)
            second = os.open(target, os.O_RDWR)
            try:
                fcntl.flock(first, fcntl.LOCK_EX | fcntl.LOCK_NB)
                wpv._unlock(first)
                fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(second)
                os.close(first)


class FileIdentityMutationTests(unittest.TestCase):
    def test_symlink_identity_is_its_own_name(self):
        with tempfile.TemporaryDirectory() as home:
            target = Path(home) / "record.bin"
            target.write_bytes(b"data")
            link = Path(home) / "alias.bin"
            platform_support.require_symlinks(self)
            link.symlink_to(target)
            self.assertEqual(wpv.file_identity(link).inode, link.lstat().st_ino)
            self.assertNotEqual(wpv.file_identity(link).inode, target.stat().st_ino)


class AtomicPrivateWriteMutationTests(unittest.TestCase):
    def test_new_record_is_synced_before_replace(self):
        with tempfile.TemporaryDirectory() as home:
            target = Path(home) / "record.bin"
            with mock.patch.object(wpv.os, "fsync", wraps=os.fsync) as synced:
                wpv.atomic_private_write(target, b"data", secure=lambda _fd: None,
                                         root=home)
            self.assertEqual(synced.call_count, 1)


class PlatformSecureWriterMutationTests(unittest.TestCase):
    def test_writer_enforces_on_its_descriptor(self):
        seen = []
        platform = SimpleNamespace(enforce_owner_only_file=seen.append)
        wpv.platform_secure_writer(platform)(7)
        self.assertEqual(seen, [7])


if __name__ == "__main__":
    unittest.main()
