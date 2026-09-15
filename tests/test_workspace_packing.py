"""Packing the host worktree: nothing outside the repository may get in.

Three separate mechanisms can put foreign bytes into the archive, and each has
its own defence and its own tests here:

* a symlink, refused because it is a symlink;
* a directory junction or other reparse point, refused by its reparse tag,
  because on Windows a junction answers yes to ``is_dir`` and no to
  ``is_symlink`` and ``O_NOFOLLOW`` does not exist to catch it;
* a swap between the approval and the read, caught by comparing the identity
  of the object approved against the identity of the descriptor read.

The Windows-only cases are exercised by attaching a reparse tag to the stat
results the packer sees, because a junction cannot be created on the machine
these tests run on. That is a simulation of the *signal*, stated plainly: it
proves the packer refuses what Windows would report, not that Windows reports
it. The symlink cases below are real on this machine.
"""

from __future__ import annotations

import base64
import io
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge.orchestration import windows_delegation as wd
import platform_support


class _TaggedStat:
    """A stat result that also reports a Windows reparse tag."""

    #: IO_REPARSE_TAG_MOUNT_POINT, the tag a directory junction carries.
    MOUNT_POINT = 0xA0000003

    def __init__(self, info, tag=MOUNT_POINT):
        self._info = info
        self.st_reparse_tag = tag

    def __getattr__(self, name):
        return getattr(self._info, name)


class _Altered:
    """A stat result with one field changed; os.stat_result is not mutable."""

    def __init__(self, info, *, inode_shift=0, size=None):
        self._info = info
        self.st_ino = info.st_ino + inode_shift
        self.st_size = info.st_size if size is None else size

    def __getattr__(self, name):
        return getattr(self._info, name)


class PackingTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        (self.repo / "inside.txt").write_text("in-repo content", encoding="utf-8")

        self.outside = self.base / "outside"
        self.outside.mkdir()
        self.secret = self.outside / "secret.txt"
        self.secret.write_text("SENSITIVE-OUTSIDE-REPO", encoding="utf-8")

    def _members(self, encoded=None):
        if encoded is None:
            encoded, _counts = wd.pack_workspace(self.repo)
        payload = base64.b64decode(encoded)
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            return {member.name: archive.extractfile(member).read()
                    for member in archive.getmembers()}

    def assertNoOutsideBytes(self, members):
        for name, payload in members.items():
            self.assertNotIn(b"SENSITIVE-OUTSIDE-REPO", payload, name)
            self.assertFalse(name.startswith("/"), name)
            self.assertNotIn("..", Path(name).parts, name)


class BaselineTests(PackingTestCase):
    def test_ordinary_files_are_packed(self):
        members = self._members()
        self.assertEqual(members["inside.txt"], b"in-repo content")

    def test_the_archive_is_deterministic(self):
        first, _ = wd.pack_workspace(self.repo)
        second, _ = wd.pack_workspace(self.repo)
        self.assertEqual(first, second)

    def test_a_repository_that_is_not_a_directory_is_refused(self):
        with self.assertRaises(wd.DelegationRefused):
            wd.pack_workspace(self.repo / "inside.txt")


class SymlinkTests(PackingTestCase):
    """Real symlinks, on this machine."""

    def test_a_symlinked_file_pointing_outside_is_not_packed(self):
        platform_support.require_symlinks(self)
        (self.repo / "link.txt").symlink_to(self.secret)
        members = self._members()
        self.assertNotIn("link.txt", members)
        self.assertNoOutsideBytes(members)

    def test_a_symlinked_directory_pointing_outside_is_not_traversed(self):
        platform_support.require_symlinks(self)
        (self.repo / "linked").symlink_to(self.outside, target_is_directory=True)
        members = self._members()
        self.assertNoOutsideBytes(members)
        self.assertEqual(set(members), {"inside.txt"})

    def test_a_symlinked_repository_root_is_refused(self):
        platform_support.require_symlinks(self)
        alias = self.base / "alias"
        alias.symlink_to(self.repo, target_is_directory=True)
        with self.assertRaises(wd.DelegationRefused) as caught:
            wd.pack_workspace(alias)
        self.assertEqual(caught.exception.reason, "workspace_unreadable")

    def test_refused_links_are_counted_rather_than_passed_over_silently(self):
        platform_support.require_symlinks(self)
        (self.repo / "link.txt").symlink_to(self.secret)
        _encoded, counts = wd.pack_workspace(self.repo)
        self.assertEqual(counts["links_refused"], 1)


class ReparsePointTests(PackingTestCase):
    """What Windows would report for a junction, and what the packer does.

    A junction cannot be created here, so the tag is attached to the stat
    results instead. The claim is narrow and deliberate: given the signal
    Windows gives, the packer refuses. Whether Windows gives that signal is
    not established by any test on this machine.
    """

    def test_a_directory_carrying_a_reparse_tag_is_not_traversed(self):
        junction = self.repo / "junction"
        junction.mkdir()
        (junction / "pulled-in.txt").write_bytes(b"SENSITIVE-OUTSIDE-REPO")
        real_stat = os.DirEntry.stat

        def tagged(entry, **kwargs):
            info = real_stat(entry, **kwargs)
            return _TaggedStat(info) if entry.name == "junction" else info

        with mock.patch.object(os.DirEntry, "stat", tagged):
            members = self._members()
        self.assertNoOutsideBytes(members)
        self.assertEqual(set(members), {"inside.txt"})

    def test_a_file_carrying_a_reparse_tag_is_not_packed(self):
        (self.repo / "placeholder.txt").write_bytes(b"SENSITIVE-OUTSIDE-REPO")
        real_stat = os.DirEntry.stat

        def tagged(entry, **kwargs):
            info = real_stat(entry, **kwargs)
            return (_TaggedStat(info, 0x8000001E)
                    if entry.name == "placeholder.txt" else info)

        with mock.patch.object(os.DirEntry, "stat", tagged):
            members = self._members()
        self.assertNoOutsideBytes(members)
        self.assertEqual(set(members), {"inside.txt"})

    def test_is_symlink_alone_would_not_have_caught_it(self):
        """The reason the tag check exists rather than relying on is_symlink."""
        junction = self.repo / "junction"
        junction.mkdir()
        info = os.lstat(junction)
        self.assertEqual(wd._reparse_tag(info), 0)
        self.assertNotEqual(wd._reparse_tag(_TaggedStat(info)), 0)


class SwapTests(PackingTestCase):
    """The window between approving an entry and reading its bytes.

    On Windows there is no ``O_NOFOLLOW``, so the identity comparison is the
    only thing standing between an approved regular file and a replacement
    opened in its place.
    """

    def test_a_file_replaced_after_approval_is_refused(self):
        """The descriptor opened is not the object that was approved."""
        real_fstat = os.fstat

        def drifting(descriptor):
            return _Altered(real_fstat(descriptor), inode_shift=1)

        with mock.patch.object(wd.os, "fstat", drifting):
            with self.assertRaises(wd.DelegationRefused) as caught:
                wd.pack_workspace(self.repo)
        self.assertEqual(caught.exception.reason, "workspace_file_changed")

    def test_a_file_that_grew_after_it_was_measured_is_refused(self):
        """Whatever is being packed is not the thing that was measured."""
        real_fstat = os.fstat

        def undersized(descriptor):
            info = real_fstat(descriptor)
            return _Altered(info, size=max(info.st_size - 4, 0))

        with mock.patch.object(wd.os, "fstat", undersized):
            with self.assertRaises(wd.DelegationRefused) as caught:
                wd.pack_workspace(self.repo)
        self.assertEqual(caught.exception.reason, "workspace_file_changed")


class ContainmentTests(PackingTestCase):
    """The explicit proof, on top of the structural one."""

    def test_every_packed_path_resolves_inside_the_repository(self):
        platform_support.require_symlinks(self)
        (self.repo / "nested").mkdir()
        (self.repo / "nested" / "deep.txt").write_text("deep", encoding="utf-8")
        (self.repo / "link.txt").symlink_to(self.secret)
        encoded, _counts = wd.pack_workspace(self.repo)
        members = self._members(encoded)
        self.assertNoOutsideBytes(members)
        real_root = os.path.realpath(self.repo)
        for name in members:
            resolved = os.path.realpath(os.path.join(real_root, name))
            self.assertTrue(resolved.startswith(real_root + os.sep), name)

    def test_a_file_whose_path_escapes_the_root_is_refused_outright(self):
        """The belt, tested independently of the braces."""
        boundary = wd._PackRoot(self.repo)
        self.assertFalse(boundary.contains(str(self.secret)))
        self.assertTrue(boundary.contains(str(self.repo / "inside.txt")))


if __name__ == "__main__":
    unittest.main()
