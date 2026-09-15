"""Owner-only directories and files: enforced, verified, and refused when not.

These run on the development machine, so they exercise the POSIX mechanism.
That is not evidence about Windows ACLs; what they do establish is that every
caller goes through a layer that *asks* the platform and refuses when the
answer is no, which is the part that was previously missing on both systems.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge.orchestration import execution_queue as eq
from agent_bridge.orchestration import windows_privacy as wpv
import platform_support


class _NoAclPlatform:
    """A platform that cannot answer the question at all."""


class _RefusingPlatform:
    def verify_owner_only_path(self, directory, probe_file):
        return False, {"mechanism": "test"}

    def enforce_owner_only_file(self, descriptor):
        raise OSError("no acl here")


class _BrokenPlatform:
    def verify_owner_only_path(self, directory, probe_file):
        raise NotImplementedError

    def enforce_owner_only_file(self, descriptor):
        raise NotImplementedError


class DirectoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_an_owner_only_directory_passes(self):
        wpv.require_private_directory(self.root, root=self.root)

    @unittest.skipIf(os.name == "nt", "POSIX mode bits")
    def test_a_world_readable_directory_is_refused(self):
        os.chmod(self.root, 0o755)
        with self.assertRaises(wpv.PrivacyError) as caught:
            wpv.require_private_directory(self.root, root=self.root)
        self.assertEqual(caught.exception.reason, "directory_not_owner_only")

    def test_an_absent_directory_is_refused(self):
        with self.assertRaises(wpv.PrivacyError) as caught:
            wpv.require_private_directory(self.root / "gone", root=self.root)
        self.assertEqual(caught.exception.reason, "directory_absent")

    def test_a_platform_that_cannot_verify_is_refused_not_assumed(self):
        with self.assertRaises(wpv.PrivacyError) as caught:
            wpv.require_private_directory(self.root, root=self.root,
                                          platform=_NoAclPlatform())
        self.assertEqual(caught.exception.reason, "acl_unenforceable")

    def test_a_platform_that_raises_is_refused_not_assumed(self):
        with self.assertRaises(wpv.PrivacyError) as caught:
            wpv.require_private_directory(self.root, root=self.root,
                                          platform=_BrokenPlatform())
        self.assertEqual(caught.exception.reason, "acl_unverified")

    def test_the_probe_file_never_survives_the_check(self):
        wpv.require_private_directory(self.root, root=self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_a_stale_probe_does_not_block_the_check(self):
        (self.root / ".agent-bridge-acl-probe").write_text("stale")
        wpv.require_private_directory(self.root, root=self.root)
        self.assertEqual(list(self.root.iterdir()), [])


class ReparseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_a_link_in_the_path_is_refused(self):
        platform_support.require_symlinks(self)
        real = self.root / "real"
        real.mkdir(mode=0o700)
        alias = self.root / "alias"
        alias.symlink_to(real)
        with self.assertRaises(wpv.PrivacyError) as caught:
            wpv.assert_no_reparse_ancestors(alias / "file.json", stop=self.root)
        self.assertEqual(caught.exception.reason, "path_traverses_a_link")

    def test_a_link_above_the_trusted_root_is_not_this_modules_business(self):
        """The operator's own machine layout is not something this polices."""
        platform_support.require_symlinks(self)
        real = self.root / "real"
        real.mkdir(mode=0o700)
        alias = self.root / "alias"
        alias.symlink_to(real)
        inner = alias / "inner"
        inner.mkdir(mode=0o700)
        wpv.assert_no_reparse_ancestors(inner / "file.json", stop=inner)

    def test_a_plain_path_passes(self):
        nested = self.root / "a" / "b"
        nested.mkdir(parents=True, mode=0o700)
        wpv.assert_no_reparse_ancestors(nested / "file.json", stop=self.root)

    def test_an_absent_leaf_is_not_itself_a_refusal(self):
        wpv.assert_no_reparse_ancestors(self.root / "not-there", stop=self.root)


class FileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / "record.json"

    def _write(self, payload=b"{}"):
        wpv.atomic_private_write(self.target, payload,
                                 secure=wpv.platform_secure_writer(),
                                 root=self.root)

    def test_a_protected_file_passes_and_reports_its_identity(self):
        self._write()
        identity = wpv.require_private_file(self.target, root=self.root)
        self.assertEqual(identity.size, 2)
        self.assertEqual(identity, wpv.file_identity(self.target))

    @unittest.skipIf(os.name == "nt", "POSIX mode bits")
    def test_a_world_readable_file_is_refused(self):
        self._write()
        os.chmod(self.target, 0o644)
        with self.assertRaises(wpv.PrivacyError) as caught:
            wpv.require_private_file(self.target, root=self.root)
        self.assertEqual(caught.exception.reason, "file_not_owner_only")

    def test_a_directory_is_not_a_file(self):
        self.target.mkdir(mode=0o700)
        with self.assertRaises(wpv.PrivacyError) as caught:
            wpv.require_private_file(self.target, root=self.root)
        self.assertEqual(caught.exception.reason, "file_not_regular")

    def test_a_symlink_is_refused_rather_than_followed(self):
        platform_support.require_symlinks(self)
        real = self.root / "real.json"
        real.write_bytes(b"{}")
        os.chmod(real, 0o600)
        self.target.symlink_to(real)
        with self.assertRaises(wpv.PrivacyError) as caught:
            wpv.require_private_file(self.target, root=self.root)
        self.assertEqual(caught.exception.reason, "path_traverses_a_link")

    def test_a_fifo_is_refused_without_blocking(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("no FIFOs on this platform")
        os.mkfifo(self.target)
        with self.assertRaises(wpv.PrivacyError) as caught:
            wpv.require_private_file(self.target, root=self.root)
        self.assertEqual(caught.exception.reason, "file_not_regular")

    def test_an_absent_file_is_refused_by_name(self):
        with self.assertRaises(wpv.PrivacyError) as caught:
            wpv.require_private_file(self.target, root=self.root)
        self.assertEqual(caught.exception.reason, "file_absent")


class AtomicWriteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / "record.json"

    def test_there_is_no_unprotected_write(self):
        with self.assertRaises(wpv.PrivacyError) as caught:
            wpv.atomic_private_write(self.target, b"{}", secure=None)
        self.assertEqual(caught.exception.reason, "secure_writer_required")
        self.assertFalse(self.target.exists())

    def test_a_protection_failure_leaves_the_previous_record_intact(self):
        wpv.atomic_private_write(self.target, b'{"first":1}',
                                 secure=wpv.platform_secure_writer(),
                                 root=self.root)

        def refuse(_descriptor):
            raise OSError("no acl")

        with self.assertRaises(wpv.PrivacyError):
            wpv.atomic_private_write(self.target, b'{"second":2}', secure=refuse,
                                     root=self.root)
        self.assertEqual(self.target.read_bytes(), b'{"first":1}')

    def test_a_protection_failure_leaves_no_temporary_behind(self):
        def refuse(_descriptor):
            raise OSError("no acl")

        with self.assertRaises(wpv.PrivacyError):
            wpv.atomic_private_write(self.target, b"{}", secure=refuse,
                                     root=self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_the_target_is_never_opened_for_truncation(self):
        """The previous record survives every failure path, including this."""
        wpv.atomic_private_write(self.target, b"original",
                                 secure=wpv.platform_secure_writer(),
                                 root=self.root)
        before = wpv.file_identity(self.target)

        def refuse(_descriptor):
            raise wpv.PrivacyError("acl_unverified")

        with self.assertRaises(wpv.PrivacyError):
            wpv.atomic_private_write(self.target, b"replacement", secure=refuse,
                                     root=self.root)
        self.assertEqual(wpv.file_identity(self.target), before)

    def test_a_successful_write_replaces_rather_than_edits(self):
        writer = wpv.platform_secure_writer()
        wpv.atomic_private_write(self.target, b"first", secure=writer,
                                 root=self.root)
        before = wpv.file_identity(self.target)
        wpv.atomic_private_write(self.target, b"second", secure=writer,
                                 root=self.root)
        self.assertEqual(self.target.read_bytes(), b"second")
        self.assertNotEqual(wpv.file_identity(self.target).inode, before.inode)

    def test_a_platform_that_cannot_protect_is_a_refusal(self):
        with self.assertRaises(wpv.PrivacyError) as caught:
            wpv.atomic_private_write(
                self.target, b"{}",
                secure=wpv.platform_secure_writer(_NoAclPlatform()),
                root=self.root)
        self.assertEqual(caught.exception.reason, "acl_unenforceable")


class QueuePrivacyAtCreationTests(unittest.TestCase):
    """Blocker 6: the ACL is established before any request content exists."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "queue"
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        self.brief = Path(self.temp.name) / "brief.txt"
        self.brief.write_text("do the thing\n", encoding="utf-8")

    def _queue(self, **kwargs):
        return eq.ExecutionQueue(self.root, None, recover_interrupted=False,
                                 **kwargs)

    def _submit(self, queue):
        return queue.submit(
            caller="codex", provider="claude", repo=str(self.repo),
            brief=str(self.brief), base="HEAD", classification="synthetic",
            model="sonnet", effort="low", item_id="i", stage="s", owner_id="o",
            stage_revision=0, verify_argv=[["git", "status"]])

    def test_a_queue_root_that_cannot_be_protected_is_refused_at_creation(self):
        with self.assertRaises(eq.ExecutionAdmissionError) as caught:
            self._queue(platform=_RefusingPlatform())
        self.assertEqual(str(caught.exception), "queue_directory_not_owner_only")

    def test_a_platform_that_cannot_answer_is_refused_at_creation(self):
        with self.assertRaises(eq.ExecutionAdmissionError) as caught:
            self._queue(platform=_NoAclPlatform())
        self.assertEqual(str(caught.exception), "queue_acl_unenforceable")

    def test_a_job_directory_is_protected_before_the_request_is_written(self):
        queue = self._queue()
        seen = []
        real = eq.require_private_queue

        def watching(path, **kwargs):
            seen.append((Path(path).name,
                         sorted(p.name for p in Path(path).iterdir())))
            return real(path, **kwargs)

        eq.require_private_queue = watching
        self.addCleanup(setattr, eq, "require_private_queue", real)
        submitted = self._submit(queue)
        directory = [row for row in seen if row[0] == submitted["job_id"]]
        self.assertEqual(len(directory), 1)
        self.assertEqual(directory[0][1], [],
                         "the job directory must be empty when it is protected")

    def test_a_submission_into_an_unprotectable_directory_writes_nothing(self):
        queue = self._queue()
        real = eq.require_private_queue

        def refuse(path, **kwargs):
            if Path(path) != queue.root:
                raise eq.ExecutionAdmissionError("queue_directory_not_owner_only")
            return real(path, **kwargs)

        eq.require_private_queue = refuse
        self.addCleanup(setattr, eq, "require_private_queue", real)
        with self.assertRaises(eq.ExecutionAdmissionError):
            self._submit(queue)
        for directory in queue.root.iterdir():
            self.assertFalse((directory / "request.json").exists())

    @unittest.skipIf(os.name == "nt", "POSIX mode bits")
    def test_a_real_submission_leaves_an_owner_only_job_directory(self):
        queue = self._queue()
        submitted = self._submit(queue)
        directory = queue.root / submitted["job_id"]
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual((directory / "request.json").stat().st_mode & 0o777,
                         0o600)


class ReadPrivateFileTests(unittest.TestCase):
    """Verify and read through one descriptor, not one name checked twice."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / "record.json"
        platform_support.write_private_bytes(self.target, b'{"a": 1}')

    def test_the_contents_come_back_with_an_identity(self):
        payload, identity = wpv.read_private_file(self.target, root=self.root)
        self.assertEqual(payload, b'{"a": 1}')
        self.assertEqual(identity.size, len(payload))

    def test_the_name_is_opened_exactly_once(self):
        """A second open is the gap this function exists to close."""
        opened = []
        real_open = wpv.os.open

        def counting(path, *args, **kwargs):
            opened.append(str(path))
            return real_open(path, *args, **kwargs)

        with mock.patch.object(wpv.os, "open", counting):
            wpv.read_private_file(self.target, root=self.root)
        self.assertEqual(opened.count(str(self.target)), 1)

    @unittest.skipIf(os.name == "nt", "POSIX mode bits")
    def test_a_file_others_can_read_is_refused_and_not_repaired(self):
        """Reading must not fix. A repair hides that it was ever open."""
        os.chmod(self.target, 0o644)
        with self.assertRaises(wpv.PrivacyError) as caught:
            wpv.read_private_file(self.target, root=self.root)
        self.assertEqual(caught.exception.reason, "file_not_owner_only")
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o644)

    def test_a_missing_file_says_so(self):
        with self.assertRaises(wpv.PrivacyError) as caught:
            wpv.read_private_file(self.root / "absent", root=self.root)
        self.assertEqual(caught.exception.reason, "file_absent")

    def test_a_directory_is_not_a_regular_file(self):
        with self.assertRaises(wpv.PrivacyError) as caught:
            wpv.read_private_file(self.root, root=self.root)
        self.assertEqual(caught.exception.reason, "file_not_regular")

    def test_a_symlink_is_refused(self):
        platform_support.require_symlinks(self)
        link = self.root / "alias"
        link.symlink_to(self.target)
        with self.assertRaises(wpv.PrivacyError):
            wpv.read_private_file(link, root=self.root)

    def test_a_file_larger_than_the_bound_is_refused_before_it_is_read(self):
        self.target.write_bytes(b"x" * 4096)
        os.chmod(self.target, 0o600)
        with self.assertRaises(wpv.PrivacyError) as caught:
            wpv.read_private_file(self.target, root=self.root, max_bytes=16)
        self.assertEqual(caught.exception.reason, "file_too_large")

    def test_a_file_swapped_between_the_check_and_the_read_is_refused(self):
        real_fstat = wpv.os.fstat
        seen = []

        class _Drifted:
            def __init__(self, info):
                self._info = info
                self.st_ino = info.st_ino + 1

            def __getattr__(self, name):
                return getattr(self._info, name)

        def drifting(descriptor):
            info = real_fstat(descriptor)
            seen.append(descriptor)
            return _Drifted(info) if len(seen) > 1 else info

        with mock.patch.object(wpv.os, "fstat", drifting):
            with self.assertRaises(wpv.PrivacyError) as caught:
                wpv.read_private_file(self.target, root=self.root)
        self.assertEqual(caught.exception.reason, "file_changed_while_reading")

    def test_a_platform_that_cannot_verify_is_refused(self):
        with self.assertRaises(wpv.PrivacyError) as caught:
            wpv.read_private_file(self.target, root=self.root, platform=object())
        self.assertEqual(caught.exception.reason, "acl_unenforceable")


class IdentityPinnedWriteTests(unittest.TestCase):
    """A read-modify-write must not discard somebody else's write."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / "record.json"
        self.secure = wpv.platform_secure_writer()
        wpv.atomic_private_write(self.target, b"first", secure=self.secure,
                                 root=self.root)

    def test_a_write_pinned_to_the_current_identity_succeeds(self):
        _payload, identity = wpv.read_private_file(self.target, root=self.root)
        wpv.atomic_private_write(self.target, b"second", secure=self.secure,
                                 root=self.root, expect_identity=identity)
        self.assertEqual(self.target.read_bytes(), b"second")

    def test_a_write_pinned_to_a_stale_identity_is_refused(self):
        _payload, identity = wpv.read_private_file(self.target, root=self.root)
        wpv.atomic_private_write(self.target, b"somebody else", secure=self.secure,
                                 root=self.root)
        with self.assertRaises(wpv.PrivacyError) as caught:
            wpv.atomic_private_write(self.target, b"stale", secure=self.secure,
                                     root=self.root, expect_identity=identity)
        self.assertEqual(caught.exception.reason, "replace_identity_changed")
        self.assertEqual(self.target.read_bytes(), b"somebody else")

    def test_a_write_pinned_to_a_file_that_vanished_is_refused(self):
        _payload, identity = wpv.read_private_file(self.target, root=self.root)
        self.target.unlink()
        with self.assertRaises(wpv.PrivacyError) as caught:
            wpv.atomic_private_write(self.target, b"x", secure=self.secure,
                                     root=self.root, expect_identity=identity)
        self.assertEqual(caught.exception.reason, "replace_identity_changed")

    def test_an_unpinned_write_still_works_for_a_first_write(self):
        fresh = self.root / "new.json"
        wpv.atomic_private_write(fresh, b"x", secure=self.secure, root=self.root)
        self.assertEqual(fresh.read_bytes(), b"x")


if __name__ == "__main__":
    unittest.main()
