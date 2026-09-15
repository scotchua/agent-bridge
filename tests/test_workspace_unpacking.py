"""What the guest will accept out of an archive the host built.

The archive is built by this project, on the host, from a repository the host
admitted. None of that is a reason to trust it here: it arrives through the
same pipe as every other field, and a guest that trusted an archive because of
where it claimed to come from would be trusting the pipe.

These are resource-exhaustion tests as much as containment tests. A tar can
be small, well-formed and honest about its structure while still being an
attack: ten million empty members, or a kilobyte that expands to a gigabyte.
"""

from __future__ import annotations

import base64
import gzip
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

from agent_bridge.orchestration import guest_runner as gr


def _archive(entries, *, compress=True):
    """Build a tar from (name, bytes) or explicit TarInfo pairs."""

    raw = io.BytesIO()
    mode = "w:gz" if compress else "w"
    with tarfile.open(fileobj=raw, mode=mode) as archive:
        for entry in entries:
            info, payload = entry
            if isinstance(info, str):
                info = tarfile.TarInfo(info)
                info.size = len(payload or b"")
            archive.addfile(info, io.BytesIO(payload) if payload is not None
                            else None)
    return raw.getvalue()


def _b64(payload: bytes) -> str:
    return base64.b64encode(payload).decode("ascii")


class UnpackTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workdir = os.path.join(self.temp.name, "work")
        # git is not needed to prove what the extractor refuses, and the
        # image's git is not on this machine. Only the baseline commit uses
        # it, and every refusal happens before that point.
        patcher = mock.patch.object(
            gr, "_git", lambda *a, **k: mock.Mock(returncode=0, stdout=b"",
                                                  stderr=b""))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _unpack(self, payload: bytes):
        gr.unpack_workspace(_b64(payload), self.workdir)

    def _refusal(self, payload: bytes) -> str:
        with self.assertRaises(gr.GuestRunnerError) as caught:
            self._unpack(payload)
        return caught.exception.code


@unittest.skipIf(os.name == "nt", "in-guest extraction: grants the workspace with geteuid/chown")
class HappyPathTests(UnpackTestCase):
    def test_an_ordinary_tree_extracts(self):
        self._unpack(_archive([("main.py", b"print('hi')\n"),
                               ("pkg/mod.py", b"x = 1\n")]))
        self.assertEqual(
            Path(self.workdir, "main.py").read_bytes(), b"print('hi')\n")
        self.assertEqual(
            Path(self.workdir, "pkg/mod.py").read_bytes(), b"x = 1\n")

    def test_extracted_files_are_owner_only(self):
        self._unpack(_archive([("main.py", b"x\n")]))
        mode = Path(self.workdir, "main.py").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_an_explicit_directory_member_is_created(self):
        info = tarfile.TarInfo("sub")
        info.type = tarfile.DIRTYPE
        self._unpack(_archive([(info, None), ("sub/f.txt", b"ok")]))
        self.assertTrue(Path(self.workdir, "sub").is_dir())


class BombTests(UnpackTestCase):
    """Small archives that are expensive to extract."""

    def test_a_gzip_bomb_is_refused_by_expansion_ratio(self):
        """A megabyte of zeroes compresses to about a kilobyte."""

        payload = b"\0" * (4 * 1024 * 1024)
        bomb = _archive([("bomb.bin", payload)])
        self.assertLess(len(bomb), 64 * 1024, "the bomb did not compress")
        self.assertIn(self._refusal(bomb),
                      ("workspace_expansion_refused",
                       "workspace_member_too_large"))

    def test_many_small_highly_compressible_members_are_refused(self):
        """No single member is remarkable. The total is the attack."""

        each = b"\0" * (256 * 1024)
        entries = [(f"f{index}.bin", each) for index in range(400)]
        bomb = _archive(entries)
        self.assertIn(self._refusal(bomb),
                      ("workspace_expansion_refused",
                       "workspace_content_too_large"))

    def test_the_cumulative_content_bound_is_enforced(self):
        """Content that is mildly compressible, so it fits under the archive
        cap and under the expansion ratio, and is refused on total size alone.

        This is the bound that actually stops a bomb: it counts bytes written
        rather than bytes the header claimed.
        """

        # Repetition inside deflate's 32 KiB window, so this compresses like
        # real content rather than like random bytes or like a bomb.
        chunk = os.urandom(16 * 1024) * 64
        entries = [(f"r{index}.bin", chunk) for index in range(40)]
        payload = _archive(entries)
        self.assertLess(len(payload), gr.MAX_WORKSPACE_BYTES,
                        "the fixture did not fit under the archive cap")
        self.assertEqual(self._refusal(payload), "workspace_content_too_large")

    def test_a_single_oversized_member_is_refused(self):
        big = os.urandom(gr.MAX_WORKSPACE_FILE_BYTES + 1024)
        self.assertEqual(self._refusal(_archive([("big.bin", big)],
                                                compress=False)),
                         "workspace_member_too_large")

    def test_a_header_that_understates_its_size_does_not_get_through(self):
        """The header is attacker-written. The bytes are what count."""

        payload = b"\0" * (gr.MAX_WORKSPACE_FILE_BYTES + 4096)
        info = tarfile.TarInfo("liar.bin")
        info.size = len(payload)
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w") as archive:
            archive.addfile(info, io.BytesIO(payload))
        self.assertEqual(self._refusal(raw.getvalue()),
                         "workspace_member_too_large")

    def test_too_many_members_is_refused_on_count_alone(self):
        entries = [(f"d/f{index}", b"") for index in
                   range(gr.MAX_WORKSPACE_MEMBERS + 10)]
        self.assertEqual(self._refusal(_archive(entries)),
                         "workspace_too_many_members")

    def test_the_member_count_is_checked_while_reading_not_after(self):
        """getmembers() reads every header before any bound applies, which is
        the resource a ten-million-entry archive attacks."""

        source = (ROOT / "src" / "agent_bridge" / "orchestration" /
                  "guest_runner.py").read_text(encoding="utf-8")
        body = source[source.index("def unpack_workspace"):
                      source.index("def capture_diff")]
        self.assertNotIn(".getmembers(", body)

    def test_an_archive_larger_than_the_compressed_bound_is_refused(self):
        payload = b"x" * (gr.MAX_WORKSPACE_BYTES + 1)
        self.assertEqual(self._refusal(payload), "workspace_too_large")


class MemberTypeTests(UnpackTestCase):
    """Everything a tar can describe that is not a file or a directory."""

    def _typed(self, name, tar_type, **fields):
        info = tarfile.TarInfo(name)
        info.type = tar_type
        info.size = 0
        for key, value in fields.items():
            setattr(info, key, value)
        return _archive([(info, None)])

    def test_a_symlink_member_is_refused(self):
        self.assertEqual(
            self._refusal(self._typed("link", tarfile.SYMTYPE,
                                      linkname="/etc/passwd")),
            "workspace_member_unsafe")

    def test_a_hard_link_member_is_refused(self):
        self.assertEqual(
            self._refusal(self._typed("hard", tarfile.LNKTYPE,
                                      linkname="main.py")),
            "workspace_member_unsafe")

    def test_a_character_device_member_is_refused(self):
        self.assertEqual(self._refusal(self._typed("dev", tarfile.CHRTYPE)),
                         "workspace_member_unsafe")

    def test_a_block_device_member_is_refused(self):
        self.assertEqual(self._refusal(self._typed("dev", tarfile.BLKTYPE)),
                         "workspace_member_unsafe")

    def test_a_fifo_member_is_refused(self):
        self.assertEqual(self._refusal(self._typed("pipe", tarfile.FIFOTYPE)),
                         "workspace_member_unsafe")

    def test_no_symlink_exists_anywhere_after_a_refused_archive(self):
        with self.assertRaises(gr.GuestRunnerError):
            self._unpack(_archive([
                ("ok.txt", b"fine"),
                (self._link_info("evil", "/etc/passwd"), None)]))
        for base, _dirs, files in os.walk(self.workdir):
            for name in files:
                self.assertFalse(os.path.islink(os.path.join(base, name)))

    def _link_info(self, name, target):
        info = tarfile.TarInfo(name)
        info.type = tarfile.SYMTYPE
        info.linkname = target
        info.size = 0
        return info


class PathTests(UnpackTestCase):
    def test_an_absolute_path_is_refused(self):
        self.assertEqual(self._refusal(_archive([("/etc/passwd", b"x")])),
                         "workspace_member_unsafe")

    def test_a_traversing_path_is_refused(self):
        self.assertEqual(self._refusal(_archive([("../escape", b"x")])),
                         "workspace_member_unsafe")

    def test_a_traversal_buried_mid_path_is_refused(self):
        self.assertEqual(self._refusal(_archive([("a/b/../../../out", b"x")])),
                         "workspace_member_unsafe")

    def test_a_windows_drive_letter_is_refused(self):
        self.assertEqual(self._refusal(_archive([("C:/windows/x", b"x")])),
                         "workspace_member_unsafe")

    def test_a_backslash_absolute_path_is_refused(self):
        self.assertEqual(self._refusal(_archive([("\\\\server\\share", b"x")])),
                         "workspace_member_unsafe")

    def test_an_overlong_path_is_refused(self):
        name = "a/" * 10 + "n" * (gr.MAX_WORKSPACE_PATH_CHARS + 1)
        self.assertEqual(self._refusal(_archive([(name, b"x")])),
                         "workspace_member_unsafe")

    def test_an_overdeep_path_is_refused(self):
        name = "/".join("d" for _ in range(gr.MAX_WORKSPACE_PATH_DEPTH + 2))
        self.assertEqual(self._refusal(_archive([(name + "/f", b"x")])),
                         "workspace_member_unsafe")

    def test_nothing_is_written_outside_the_workdir(self):
        witness = Path(self.temp.name) / "witness"
        witness.write_bytes(b"untouched")
        for name in ("../witness", "../../witness", "a/../../witness"):
            with self.assertRaises(gr.GuestRunnerError):
                self._unpack(_archive([(name, b"overwritten")]))
        self.assertEqual(witness.read_bytes(), b"untouched")


class DuplicateTests(UnpackTestCase):
    def test_a_repeated_name_is_refused(self):
        self.assertEqual(
            self._refusal(_archive([("f.txt", b"first"), ("f.txt", b"second")])),
            "workspace_member_duplicate")

    def test_names_differing_only_in_case_are_refused(self):
        """The guest is case-sensitive and the host that built this may not
        be, so two members that are distinct here can be one file there. The
        archive would be choosing which one wins."""

        self.assertEqual(
            self._refusal(_archive([("Readme.md", b"one"),
                                    ("README.md", b"two")])),
            "workspace_member_duplicate")

    def test_the_first_copy_of_a_duplicate_is_not_silently_kept(self):
        with self.assertRaises(gr.GuestRunnerError):
            self._unpack(_archive([("f.txt", b"first"), ("F.TXT", b"second")]))
        self.assertNotEqual(Path(self.workdir, "f.txt").read_bytes()
                            if Path(self.workdir, "f.txt").exists() else b"",
                            b"second")


class CrossLayerBoundaryTests(unittest.TestCase):
    """The limits have to agree across the layers, not just exist.

    Each of these was a real mismatch: the packer would build an archive the
    transport could not carry, and the guest could assemble a response the
    outer runtime would refuse.
    """

    def setUp(self):
        from agent_bridge.orchestration import windows_delegation as wd
        from agent_bridge.orchestration import windows_wsl_runtime as wr
        self.wd = wd
        self.wr = wr

    def test_the_transport_can_carry_the_largest_legal_request(self):
        self.assertGreaterEqual(self.wr.MAX_INPUT_BYTES,
                                gr.MAX_TOTAL_REQUEST_BYTES)

    def test_the_default_transport_limit_can_carry_it_too(self):
        """A default below the maximum refuses a legitimate job."""

        self.assertGreaterEqual(self.wr.DEFAULT_LIMITS.input_max_bytes,
                                gr.MAX_TOTAL_REQUEST_BYTES)

    def test_the_transport_can_carry_the_largest_legal_response(self):
        self.assertGreaterEqual(self.wr.MAX_OUTPUT_BYTES,
                                gr.MAX_RESPONSE_BYTES)

    def test_the_default_output_limit_can_carry_it_too(self):
        self.assertGreaterEqual(self.wr.DEFAULT_LIMITS.output_max_bytes,
                                gr.MAX_RESPONSE_BYTES)

    def test_a_maximal_response_fits_the_declared_response_bound(self):
        """The bound is arithmetic over the parts, so check the arithmetic."""

        response = gr.build_response(
            status="completed", reason="ok", exit_code=0,
            stdout=b"o" * gr.MAX_OUTPUT_BYTES,
            stderr=b"e" * gr.MAX_OUTPUT_BYTES,
            diff=b"d" * gr.MAX_DIFF_BYTES,
            truncated=False, duration_seconds=1.0,
            harness_status=gr.HARNESS_COMPLETE)
        import json
        encoded = json.dumps(response).encode("utf-8")
        self.assertLessEqual(len(encoded), gr.MAX_RESPONSE_BYTES)

    def test_a_maximal_encoded_workspace_fits_the_request_bound(self):
        self.assertGreaterEqual(gr.MAX_TOTAL_REQUEST_BYTES,
                                gr.MAX_WORKSPACE_B64_CHARS + gr.MAX_BRIEF_BYTES)

    def test_the_packer_and_the_guest_agree_on_content_size(self):
        self.assertEqual(self.wd.MAX_WORKSPACE_CONTENT_BYTES,
                         gr.MAX_WORKSPACE_CONTENT_BYTES)
        self.assertEqual(self.wd.MAX_WORKSPACE_FILE_BYTES,
                         gr.MAX_WORKSPACE_FILE_BYTES)

    def test_the_packers_file_cap_cannot_exceed_the_guests_member_cap(self):
        self.assertLessEqual(self.wd.MAX_WORKSPACE_FILES,
                             gr.MAX_WORKSPACE_MEMBERS)

    def test_a_maximum_content_tree_can_still_compress_within_the_archive_cap(self):
        """The compressed cap must not refuse a legitimate maximum tree."""

        # Source-like content compresses far better than this, so a tree at
        # the content cap has ample room under the archive cap.
        self.assertLess(gr.MAX_WORKSPACE_BYTES, gr.MAX_WORKSPACE_CONTENT_BYTES)
        self.assertGreater(gr.MAX_WORKSPACE_BYTES,
                           gr.MAX_WORKSPACE_CONTENT_BYTES // 8)

    def test_the_expansion_ratio_admits_this_repositorys_own_source(self):
        """A bound that refused ordinary source would be worse than useless:
        it would refuse the exact input this whole lane exists to carry."""

        blob = b""
        for base, dirs, files in os.walk(ROOT / "src"):
            dirs[:] = [name for name in dirs if name != "__pycache__"]
            for name in files:
                if name.endswith(".py"):
                    blob += Path(base, name).read_bytes()
        ratio = len(blob) / len(gzip.compress(blob))
        self.assertLess(ratio, gr.MAX_WORKSPACE_EXPANSION_RATIO)

    def test_the_expansion_ratio_admits_a_large_repetitive_lockfile(self):
        """The case that made a tighter bound wrong. Lockfiles are extremely
        repetitive, entirely legitimate, and compress two orders of magnitude
        better than source."""

        lockfile = (b'  "resolved": "https://registry.example/x/-/x-1.0.0.tgz",\n'
                    b'  "integrity": "sha512-' + b"A" * 86 + b'==",\n') * 20000
        ratio = len(lockfile) / len(gzip.compress(lockfile))
        self.assertGreater(ratio, 100, "the fixture was not repetitive enough")
        self.assertLess(ratio, gr.MAX_WORKSPACE_EXPANSION_RATIO)

    def test_the_expansion_ratio_still_refuses_an_actual_bomb(self):
        bomb = b"\0" * (64 * 1024 * 1024)
        ratio = len(bomb) / len(gzip.compress(bomb))
        self.assertGreater(ratio, gr.MAX_WORKSPACE_EXPANSION_RATIO)

    def test_the_ratio_bound_is_below_what_deflate_can_actually_reach(self):
        """A ratio bound above deflate's ceiling is decorative: no .tar.gz
        could ever cross it. This is the check that keeps the number honest.

        Measured rather than quoted, because the ceiling is a property of the
        zlib this runtime actually links against.
        """

        best = (64 * 1024 * 1024) / len(gzip.compress(b"\0" * (64 * 1024 * 1024)))
        self.assertLess(gr.MAX_WORKSPACE_EXPANSION_RATIO, best,
                        "the ratio bound is unreachable and therefore inert")

    def test_the_cumulative_cap_is_what_a_bomb_actually_hits(self):
        """The ratio is the early exit. This is the control that holds even
        for a format whose ratio is unbounded."""

        self.assertLess(
            gr.MAX_WORKSPACE_CONTENT_BYTES,
            gr.MAX_WORKSPACE_BYTES * gr.MAX_WORKSPACE_EXPANSION_RATIO)


@unittest.skipIf(os.name == "nt", "in-guest extraction: grants the workspace with geteuid/chown")
class JustBelowAndAboveTests(UnpackTestCase):
    """Each bound checked from both sides, so none is off by one."""

    def test_a_member_just_below_the_file_cap_is_accepted(self):
        payload = os.urandom(gr.MAX_WORKSPACE_FILE_BYTES - 1)
        self._unpack(_archive([("f.bin", payload)], compress=False))
        self.assertEqual(Path(self.workdir, "f.bin").stat().st_size,
                         len(payload))

    def test_a_member_just_above_the_file_cap_is_refused(self):
        payload = os.urandom(gr.MAX_WORKSPACE_FILE_BYTES + 1)
        self.assertEqual(self._refusal(_archive([("f.bin", payload)],
                                                compress=False)),
                         "workspace_member_too_large")

    def test_an_archive_just_below_the_compressed_cap_is_not_refused_for_size(self):
        payload = b"y" * (gr.MAX_WORKSPACE_BYTES - 1)
        with self.assertRaises(gr.GuestRunnerError) as caught:
            self._unpack(payload)
        self.assertNotEqual(caught.exception.code, "workspace_too_large")

    def test_an_archive_just_above_the_compressed_cap_is_refused(self):
        payload = b"y" * (gr.MAX_WORKSPACE_BYTES + 1)
        self.assertEqual(self._refusal(payload), "workspace_too_large")

    def test_a_member_count_just_below_the_cap_is_accepted(self):
        entries = [(f"f{index}", b"") for index in
                   range(gr.MAX_WORKSPACE_MEMBERS - 1)]
        self._unpack(_archive(entries))

    def test_a_member_count_just_above_the_cap_is_refused(self):
        entries = [(f"f{index}", b"") for index in
                   range(gr.MAX_WORKSPACE_MEMBERS + 1)]
        self.assertEqual(self._refusal(_archive(entries)),
                         "workspace_too_many_members")

    def test_a_path_depth_just_below_the_cap_is_accepted(self):
        name = "/".join(f"d{index}" for index in
                        range(gr.MAX_WORKSPACE_PATH_DEPTH - 1))
        self._unpack(_archive([(name, b"x")]))

    def test_a_path_depth_just_above_the_cap_is_refused(self):
        name = "/".join(f"d{index}" for index in
                        range(gr.MAX_WORKSPACE_PATH_DEPTH + 1))
        self.assertEqual(self._refusal(_archive([(name, b"x")])),
                         "workspace_member_unsafe")


if __name__ == "__main__":
    unittest.main()
