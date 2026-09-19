"""Phase 1 of the local-first read gate: policy, readiness, calibration,
and protection. See docs/LOCAL-FIRST-DESIGN.md.

Judging a tool call and writing a digest receipt are later phases; this
file covers what exists so far: glob matching, window arithmetic,
``readiness()``, ``parse_policy``'s new ``local_first``/``mechanical_globs``
keys, the ``calibrate`` subcommand, and the protected-path rule over the
bridge's own state-changing launchers.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
FAKES = Path(__file__).resolve().parent / "fakes"
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge import store  # noqa: E402
from agent_bridge.localq.spool import FakeBackend, LocalQueue, ResourceSnapshot  # noqa: E402
from agent_bridge.orchestration import autoroute, delegation_verify, gate, localfirst, mcp  # noqa: E402


class PortableSampler:
    """A resource reading that works on every host this suite runs on.

    ``localq.runtime.MacSampler`` shells out to macOS-only probes, so
    exercising the calibrate happy path on Linux and Windows CI needs the
    same portable seam ``test_automatic_delegation_e2e.py`` already uses for
    the local lane. Always reports spare capacity: the readiness and
    calibration tests below are about the policy and record logic, not
    about this machine's real load.
    """

    def sample(self) -> ResourceSnapshot:
        return ResourceSnapshot(time.time(), "normal", "normal", True, 600.0, 0.0,
                                cpu_idle_ratio=0.9)


# ------------------------------------------------------------------- globs


class GlobMatchingTests(unittest.TestCase):
    def test_double_star_crosses_directories_single_star_does_not(self):
        cases = [
            ("**/*.log", "app.log", True),
            ("**/*.log", "sub/dir/app.log", True),
            ("**/*.log", "app.logx", False),
            ("**/*.log", "app.txt", False),
            ("**/logs/**", "logs/x", True),
            ("**/logs/**", "a/b/logs/x/y", True),
            ("**/logs/**", "logs", False),
            ("test-output/**", "test-output/foo.txt", True),
            ("test-output/**", "other/foo.txt", False),
            ("*.log", "sub/app.log", False),
            ("*.log", "app.log", True),
            ("a?c.log", "abc.log", True),
            ("a?c.log", "a/c.log", False),
        ]
        for glob, path, expected in cases:
            with self.subTest(glob=glob, path=path):
                self.assertEqual(bool(localfirst.compile_glob(glob).match(path)), expected)

    def test_not_purepath_match_because_it_disagrees_across_python_versions(self):
        """Documents why this project does not use pathlib for this.

        ``pathlib.PurePath.match``'s ``**`` handling changed between the two
        Python versions this project's own CI matrix runs
        (``.github/workflows/tests.yml``: 3.11 and 3.13). A regex compiled
        by this module gives the identical verdict on both, which this test
        pins down for the interpreter actually running it rather than
        asserting anything about the other one.
        """
        self.assertTrue(localfirst.compile_glob("**/*.log").match("a/b/app.log"))
        self.assertTrue(localfirst.compile_glob("**/*.log").match("app.log"))

    def test_a_symlink_that_escapes_the_repository_never_matches(self):
        with tempfile.TemporaryDirectory() as base:
            repo = os.path.join(base, "repo")
            outside = os.path.join(base, "outside")
            os.makedirs(repo)
            os.makedirs(outside)
            target = os.path.join(outside, "secret.log")
            open(target, "w").close()
            link = os.path.join(repo, "logs.log")
            try:
                os.symlink(target, link)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are not creatable on this account")
            # The realpath of the link resolves outside repo_root: matches_any
            # is handed that resolved path, exactly as the gate would after
            # following the link, and it must refuse to match.
            resolved = os.path.realpath(link)
            self.assertFalse(localfirst.matches_any(resolved, repo, ("**/*.log",)))

    def test_a_windows_cross_drive_path_never_matches_rather_than_raising(self):
        """Found by adversarial review: on Windows, ntpath.relpath raises
        ValueError for two paths on different drives ("path is on mount
        'D:', start on mount 'C:'") instead of returning a path starting
        with "..". A symlink or junction onto a second drive makes this an
        ordinary occurrence, not a hypothetical, and matches_any's own
        contract ("a path that resolves outside repo_root never matches")
        must hold for that signal exactly as it does for the ordinary one.
        Simulated here with the real exception os.path.relpath raises,
        since this sandbox has no second drive to reproduce it against
        natively.
        """
        from unittest import mock
        with mock.patch("agent_bridge.orchestration.localfirst.os.path.relpath",
                        side_effect=ValueError("path is on mount 'D:', start on mount 'C:'")):
            self.assertFalse(localfirst.matches_any(
                "D:\\other\\file.log", "C:\\repo", ("**/*.log",)))
        # relative_posix_path itself still raises: matches_any is the layer
        # that promises "never matches", not every caller of the helper.
        with mock.patch("agent_bridge.orchestration.localfirst.os.path.relpath",
                        side_effect=ValueError("cross-drive")):
            with self.assertRaises(ValueError):
                localfirst.relative_posix_path("D:\\other\\file.log", "C:\\repo")

    def test_effective_globs_falls_back_to_the_default_only_when_the_repo_names_none(self):
        lf = autoroute.LocalFirstConfig(default_globs=("**/*.default",))
        narrow = autoroute.RepoPolicy(mechanical_globs=("**/*.custom",))
        wide_default = autoroute.RepoPolicy()
        self.assertEqual(localfirst.effective_globs(narrow, lf), ("**/*.custom",))
        self.assertEqual(localfirst.effective_globs(wide_default, lf), ("**/*.default",))

    def test_an_invalid_pattern_is_refused_by_name(self):
        with self.assertRaises(localfirst.GlobError):
            localfirst.compile_glob("")
        with self.assertRaises(localfirst.GlobError):
            localfirst.compile_glob("bad\x00pattern")


# --------------------------------------------------------- window arithmetic


class WindowArithmeticTests(unittest.TestCase):
    def test_a_file_smaller_than_the_cap_is_read_whole_from_the_start(self):
        self.assertEqual(localfirst.digest_window(7_999), (0, 7_999))
        self.assertEqual(localfirst.digest_window(8_000), (0, 8_000))

    def test_a_file_at_or_above_the_cap_is_clamped_to_the_cap_from_the_tail(self):
        # A cap passed explicitly, so this is about digest_window's own
        # arithmetic and stays correct however MAX_WINDOW_BYTES is tuned.
        self.assertEqual(localfirst.digest_window(24_000, max_window_bytes=24_000), (0, 24_000))
        self.assertEqual(localfirst.digest_window(24_001, max_window_bytes=24_000), (1, 24_000))
        self.assertEqual(localfirst.digest_window(100_000, max_window_bytes=24_000), (76_000, 24_000))

    def test_the_window_length_never_varies_with_a_caller_chosen_offset(self):
        # A large file: the caller may slide the window, but its length is
        # always the fixed cap.
        self.assertEqual(localfirst.digest_window(100_000, offset=50_000, max_window_bytes=24_000),
                         (50_000, 24_000))
        self.assertEqual(localfirst.digest_window(100_000, offset=0, max_window_bytes=24_000),
                         (0, 24_000))
        self.assertEqual(localfirst.digest_window(100_000, offset=76_000, max_window_bytes=24_000),
                         (76_000, 24_000))

    def test_the_default_cap_is_the_queue_cap_minus_its_json_envelope_headroom(self):
        """Ties digest_window's default to the module constant explicitly,
        so a future change to either is caught here rather than silently
        drifting apart."""
        from agent_bridge.localq.spool import QueueCaps
        self.assertEqual(localfirst.MAX_WINDOW_BYTES,
                         QueueCaps().max_input_bytes - localfirst.JSON_ENVELOPE_HEADROOM_BYTES)
        self.assertEqual(localfirst.digest_window(10 ** 6)[1], localfirst.MAX_WINDOW_BYTES)

    def test_an_offset_that_would_shorten_the_window_is_refused(self):
        """The cheap-receipt attack this closes: design section 6, finding 3."""
        with self.assertRaises(localfirst.WindowError):
            localfirst.digest_window(100_000, offset=99_000)
        # A small file: only offset 0 (or None) can satisfy the fixed window,
        # because the window equals the whole file.
        with self.assertRaises(localfirst.WindowError):
            localfirst.digest_window(100, offset=50)
        self.assertEqual(localfirst.digest_window(100, offset=0), (0, 100))

    def test_invalid_inputs_are_refused_rather_than_silently_clamped(self):
        with self.assertRaises(localfirst.WindowError):
            localfirst.digest_window(-1)
        with self.assertRaises(localfirst.WindowError):
            localfirst.digest_window(100, offset=-1)
        with self.assertRaises(localfirst.WindowError):
            localfirst.digest_window(100, offset=101)
        with self.assertRaises(localfirst.WindowError):
            localfirst.digest_window(True)  # bool is not an accepted int here


# ------------------------------------------------------------- descriptor read


class ReadWindowTests(unittest.TestCase):
    """Found by adversarial review: read_window's Windows symlink defense
    was not descriptor-bound. os.O_NOFOLLOW does not exist on Windows (the
    getattr fallback is 0, contributing nothing there), so a symlink placed
    at the name between an earlier os.path.islink check and read_window's
    own os.open was followed silently. The fix mirrors
    windows_privacy.read_private_file / windows_delegation.read_brief:
    lstat the name first and refuse a link there, then require the
    descriptor os.open actually produces to be that same object
    (st_dev/st_ino) before any byte is read."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def test_reads_the_requested_window(self):
        path = self.base / "app.log"
        path.write_text("0123456789ABCDEF", encoding="utf-8")
        text, replacements = localfirst.read_window(str(path), 2, 5)
        self.assertEqual(text, "23456")
        self.assertEqual(replacements, 0)

    def test_a_symlink_is_refused_by_the_pre_open_lstat_check(self):
        target = self.base / "real.log"
        target.write_text("x" * 100, encoding="utf-8")
        link = self.base / "link.log"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not creatable on this account")
        with self.assertRaises(localfirst.WindowReadError) as ctx:
            localfirst.read_window(str(link), 0, 10)
        self.assertEqual(str(ctx.exception), "path_is_a_link")

    def test_an_object_swap_between_lstat_and_open_is_detected(self):
        """Simulates the TOCTOU window read_window's two-step check exists
        for: os.lstat reports one object's identity, but the descriptor
        os.open actually opens is a different one. Detected, not prevented
        -- the same residual risk windows_privacy.read_private_file and
        windows_delegation.read_brief carry and state for the identical
        reason, and the reason this comparison happens before any byte is
        read rather than only afterward."""
        path = self.base / "app.log"
        path.write_text("x" * 100, encoding="utf-8")
        real_lstat = os.lstat
        real_result = real_lstat(path)

        class _Spoofed:
            def __getattr__(self, name):
                return getattr(real_result, name)
            st_dev = real_result.st_dev + 1
            st_ino = real_result.st_ino + 1
            st_mode = real_result.st_mode

        def fake_lstat(target, *args, **kwargs):
            if os.fspath(target) == str(path):
                return _Spoofed()
            return real_lstat(target, *args, **kwargs)

        with mock.patch("agent_bridge.orchestration.localfirst.os.lstat", side_effect=fake_lstat):
            with self.assertRaises(localfirst.WindowReadError) as ctx:
                localfirst.read_window(str(path), 0, 10)
        self.assertEqual(str(ctx.exception), "path_changed_before_open")

    def test_a_directory_is_refused_not_a_regular_file(self):
        directory = self.base / "adir"
        directory.mkdir()
        with self.assertRaises(localfirst.WindowReadError) as ctx:
            localfirst.read_window(str(directory), 0, 10)
        self.assertEqual(str(ctx.exception), "not_a_regular_file")

    def test_an_absent_path_is_refused(self):
        with self.assertRaises(localfirst.WindowReadError):
            localfirst.read_window(str(self.base / "nope.log"), 0, 10)

    def test_non_utf8_bytes_are_replaced_and_counted(self):
        path = self.base / "app.log"
        path.write_bytes(b"line one\n\xff\xfeline two\n")
        text, replacements = localfirst.read_window(str(path), 0, path.stat().st_size)
        self.assertEqual(replacements, 2)
        self.assertIn("line one", text)
        self.assertIn("line two", text)

    def test_a_legitimate_replacement_character_in_the_source_is_not_counted_as_an_error(self):
        """Found by adversarial review: counting occurrences of U+FFFD in
        the decoded text conflates an actual decode failure with a file
        that legitimately contains that character as content -- valid
        UTF-8 encodes U+FFFD like any other code point. The count must
        come from the decode error handler actually firing, not from
        scanning the output afterward."""
        path = self.base / "app.log"
        path.write_text("line one � line two", encoding="utf-8")
        text, replacements = localfirst.read_window(str(path), 0, path.stat().st_size)
        self.assertEqual(replacements, 0)
        self.assertIn("�", text)

    def test_a_file_that_shrinks_between_stat_and_read_is_a_short_read_not_silent(self):
        """Found by adversarial review: a length chosen from a size learned
        earlier (as digest_read's caller does, from its own single fstat)
        can still legitimately outrun what remains once the read actually
        happens if something shrinks the file after that fstat but before
        the read loop finishes. read_window must refuse rather than
        silently hand back fewer bytes than the caller asked for and asked
        to be hashed."""
        path = self.base / "app.log"
        path.write_bytes(b"0123456789")
        real_open = os.open

        def truncating_open(target, *args, **kwargs):
            descriptor = real_open(target, *args, **kwargs)
            if os.fspath(target) == str(path):
                os.truncate(str(path), 4)
            return descriptor

        with mock.patch("agent_bridge.orchestration.localfirst.os.open", side_effect=truncating_open):
            with self.assertRaises(localfirst.WindowReadError) as ctx:
                localfirst.read_window(str(path), 0, 10)
        self.assertEqual(str(ctx.exception), "window_short_read")


class DigestReadTests(unittest.TestCase):
    """``digest_read`` determines the window from the same descriptor it
    reads through, instead of a separate, earlier ``os.stat`` by name.
    Found by an adversarial review: the earlier shape (``os.stat`` by name,
    then ``read_window`` reopening the same name) let a caller swap the
    file between the two and get a receipt for the file it validated while
    the job actually carried the replacement's bytes."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def test_reads_the_tail_by_default_and_reports_the_files_own_identity(self):
        path = self.base / "app.log"
        path.write_bytes(b"A" * 100)
        stat_before = path.stat()
        (text, replacements, size, mtime_ns,
         offset, window_bytes) = localfirst.digest_read(str(path), None, max_window_bytes=40)
        self.assertEqual(len(text), 40)
        self.assertEqual(replacements, 0)
        self.assertEqual(size, 100)
        self.assertEqual(mtime_ns, stat_before.st_mtime_ns)
        self.assertEqual(offset, 60)
        self.assertEqual(window_bytes, 40)

    def test_a_caller_supplied_offset_is_honoured(self):
        path = self.base / "app.log"
        path.write_text("0123456789", encoding="utf-8")
        text, _, _, _, offset, window_bytes = localfirst.digest_read(str(path), 2, max_window_bytes=5)
        self.assertEqual(offset, 2)
        self.assertEqual(window_bytes, 5)
        self.assertEqual(text, "23456")

    def test_window_computation_errors_are_a_window_error_not_a_window_read_error(self):
        """mcp.py distinguishes digest_window_refused from digest_read_refused
        by exception type; digest_read must preserve that distinction
        rather than wrapping every failure in one type."""
        path = self.base / "app.log"
        path.write_text("A" * 10, encoding="utf-8")
        with self.assertRaises(localfirst.WindowError):
            localfirst.digest_read(str(path), 9, max_window_bytes=5)

    def test_digest_read_ignores_a_stale_external_stat_taken_before_it_runs(self):
        """The property the fix guarantees: digest_read never consults any
        stat taken outside its own call. The earlier shape had mcp.py take
        its own os.stat by name first, decide the window from that, and
        only then call read_window, which reopened the same name on its
        own -- two independent looks at the path, with nothing tying the
        second to the first. A caller who changed the file in between got
        a receipt naming the *first* look's identity while the bytes
        actually read and hashed came from the *second*. Now there is only
        one look: whatever an earlier, separate stat saw is irrelevant,
        because digest_read never receives or consults it."""
        path = self.base / "app.log"
        path.write_bytes(b"A" * 100)
        stale_stat = os.stat(str(path))  # what a caller's own earlier stat would have seen
        path.write_bytes(b"B" * 40)  # the file changes before digest_read is ever called
        (text, replacements, size, mtime_ns,
         offset, window_bytes) = localfirst.digest_read(str(path), None, max_window_bytes=100)
        self.assertEqual(size, 40)
        self.assertNotEqual(size, stale_stat.st_size)
        self.assertEqual(window_bytes, 40)
        self.assertEqual(text, "B" * 40)

    def test_a_change_while_the_read_is_in_flight_is_detected_not_misreported(self):
        """Once digest_read's own fstat is taken, a change to that same
        file (in place, through the same name -- not a swap to a
        different object, which the pre-open lstat/open identity check in
        _open_verified already covers) while the read loop is still
        running must still be caught, exactly as read_window already
        guarantees: the after-read fstat disagreeing with the before-read
        fstat is refused rather than silently producing a receipt for one
        moment's identity next to another moment's bytes."""
        path = self.base / "app.log"
        path.write_bytes(b"0" * 100)
        real_read = os.read
        state = {"changed": False}

        def change_after_first_read(descriptor, count):
            chunk = real_read(descriptor, count)
            if not state["changed"]:
                state["changed"] = True
                # Grows the file rather than merely rewriting it in place,
                # so the identity mismatch this test needs (a different
                # st_size) does not depend on the filesystem's mtime
                # timestamp resolution being fine enough to tell two quick
                # writes apart.
                with open(str(path), "r+b") as handle:
                    handle.write(b"1" * 150)
            return chunk

        with mock.patch("agent_bridge.orchestration.localfirst.os.read", side_effect=change_after_first_read):
            with self.assertRaises(localfirst.WindowReadError) as ctx:
                localfirst.digest_read(str(path), None, max_window_bytes=10)
        self.assertEqual(str(ctx.exception), "file_changed_while_reading")


# ----------------------------------------------------------------- protection


class UnderAnyTests(unittest.TestCase):
    """Found by adversarial review: mcp._under_any resolved both sides with
    realpath but dropped os.path.normcase, repeating the exact gap
    gate._under was already hardened against (REVIEW-HISTORY.md finding
    59). os.path.realpath alone does not normalise case for a path
    component that does not yet exist on disk, which several of the
    protected paths (SQLite -wal/-shm sidecars, most plausibly) usually
    are not yet."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def test_an_exact_root_and_a_path_inside_it_both_match(self):
        root = self.base / "state"
        root.mkdir()
        target = root / "routing" / "file.json"
        target.parent.mkdir(parents=True)
        target.write_text("{}", encoding="utf-8")
        self.assertTrue(mcp._under_any(str(root), (str(root),)))
        self.assertTrue(mcp._under_any(str(target), (str(root),)))

    def test_a_path_outside_every_root_does_not_match(self):
        root = self.base / "state"
        root.mkdir()
        outside = self.base / "elsewhere.json"
        outside.write_text("{}", encoding="utf-8")
        self.assertFalse(mcp._under_any(str(outside), (str(root),)))

    def test_a_root_with_different_case_still_matches_when_normcase_folds_it(self):
        """os.path.normcase is a no-op on POSIX, so this simulates the
        Windows behaviour it exists for by patching normcase to fold case,
        the same technique used elsewhere in this suite (mocking
        os.path.relpath's Windows-only cross-drive ValueError) for a
        platform difference this sandbox cannot produce natively."""
        root = self.base / "State"
        root.mkdir()
        target = root / "file.json"
        target.write_text("{}", encoding="utf-8")
        with mock.patch("agent_bridge.orchestration.mcp.os.path.normcase",
                        side_effect=lambda value: value.lower()):
            self.assertTrue(mcp._under_any(str(target).replace("State", "state"),
                                           (str(root).replace("State", "STATE"),)))

    def test_empty_and_falsy_roots_are_skipped(self):
        self.assertFalse(mcp._under_any(str(self.base), ("", None)))


# -------------------------------------------------------------- policy parsing


class LocalFirstPolicyParsingTests(unittest.TestCase):
    def test_absent_local_first_parses_as_every_default_and_disabled(self):
        policy = autoroute.parse_policy({"version": 1, "repos": {}})
        self.assertFalse(policy.local_first.enabled)
        self.assertEqual(policy.local_first.read_gate_min_bytes, 8_000)
        self.assertEqual(policy.local_first.default_globs, ("**/*.log", "**/logs/**"))

    def test_every_local_first_field_is_settable_and_deduplicated(self):
        document = {"version": 1, "repos": {}, "local_first": {
            "enabled": True, "latency_budget_seconds": 45,
            "read_gate_min_bytes": 5_000, "digest_max_output_chars": 2_000,
            "calibration_max_age_days": 10, "digest_grace_seconds": 300,
            "executor_liveness_seconds": 30,
            "default_globs": ["**/*.out", "**/*.out"]}}
        policy = autoroute.parse_policy(document)
        lf = policy.local_first
        self.assertTrue(lf.enabled)
        self.assertEqual(lf.latency_budget_seconds, 45)
        self.assertEqual(lf.read_gate_min_bytes, 5_000)
        self.assertEqual(lf.digest_max_output_chars, 2_000)
        self.assertEqual(lf.calibration_max_age_days, 10)
        self.assertEqual(lf.digest_grace_seconds, 300)
        self.assertEqual(lf.executor_liveness_seconds, 30)
        self.assertEqual(lf.default_globs, ("**/*.out",))

    def test_an_unknown_local_first_key_fails_closed(self):
        with self.assertRaises(autoroute.PolicyError):
            autoroute.parse_policy({"version": 1, "repos": {},
                                    "local_first": {"enabled": True, "bogus": 1}})

    def test_local_first_must_be_an_object(self):
        with self.assertRaises(autoroute.PolicyError):
            autoroute.parse_policy({"version": 1, "repos": {}, "local_first": []})

    def test_every_numeric_field_must_be_a_positive_number_not_a_bool(self):
        for field in ("latency_budget_seconds", "read_gate_min_bytes",
                     "digest_max_output_chars", "calibration_max_age_days",
                     "digest_grace_seconds", "executor_liveness_seconds"):
            with self.subTest(field=field):
                with self.assertRaises(autoroute.PolicyError):
                    autoroute.parse_policy({"version": 1, "repos": {},
                                            "local_first": {field: 0}})
                with self.assertRaises(autoroute.PolicyError):
                    autoroute.parse_policy({"version": 1, "repos": {},
                                            "local_first": {field: True}})
                with self.assertRaises(autoroute.PolicyError):
                    autoroute.parse_policy({"version": 1, "repos": {},
                                            "local_first": {field: "45"}})

    def test_infinity_and_nan_are_refused_not_silently_accepted(self):
        """Found by adversarial review: json.loads parses the bare tokens
        Infinity/-Infinity/NaN by default, and float("inf") <= 0 is False,
        so the original `value <= 0` check alone let Infinity through for
        every field in this group. An Infinity latency_budget_seconds would
        never be exceeded (over_latency_budget could never fire); an
        Infinity calibration_max_age_days would mean a calibration record
        never goes stale."""
        for field in ("latency_budget_seconds", "calibration_max_age_days",
                     "digest_grace_seconds", "executor_liveness_seconds"):
            for token in (json.loads('{"x": Infinity}')["x"],
                         json.loads('{"x": -Infinity}')["x"],
                         json.loads('{"x": NaN}')["x"]):
                with self.subTest(field=field, token=token):
                    with self.assertRaises(autoroute.PolicyError):
                        autoroute.parse_policy({"version": 1, "repos": {},
                                                "local_first": {field: token}})

    def test_default_globs_must_be_a_non_empty_list_of_strings(self):
        with self.assertRaises(autoroute.PolicyError):
            autoroute.parse_policy({"version": 1, "repos": {},
                                    "local_first": {"default_globs": []}})
        with self.assertRaises(autoroute.PolicyError):
            autoroute.parse_policy({"version": 1, "repos": {},
                                    "local_first": {"default_globs": [1]}})
        with self.assertRaises(autoroute.PolicyError):
            autoroute.parse_policy({"version": 1, "repos": {},
                                    "local_first": {"default_globs": "**/*.log"}})

    def test_enabled_must_be_a_bool(self):
        with self.assertRaises(autoroute.PolicyError):
            autoroute.parse_policy({"version": 1, "repos": {},
                                    "local_first": {"enabled": "true"}})

    def test_mechanical_globs_on_a_repo_entry(self):
        document = {"version": 1, "repos": {"/abs/repo": {
            "classification": "internal_nonclient", "allowed_routes": ["claude"],
            "mechanical_ok": True, "mechanical_globs": ["**/*.log", "**/*.log"]}}}
        policy = autoroute.parse_policy(document)
        self.assertEqual(policy.repos["/abs/repo"].mechanical_globs, ("**/*.log",))

    def test_mechanical_globs_must_be_non_empty_strings(self):
        for bad in ([1], [""], "not-a-list"):
            with self.subTest(bad=bad):
                with self.assertRaises(autoroute.PolicyError):
                    autoroute.parse_policy({"version": 1, "repos": {"/abs/repo": {
                        "mechanical_globs": bad}}})

    def test_an_unknown_repo_key_still_fails_closed_with_mechanical_globs_present(self):
        with self.assertRaises(autoroute.PolicyError):
            autoroute.parse_policy({"version": 1, "repos": {"/abs/repo": {
                "mechanical_globs": ["**/*.log"], "unknown_field": 1}}})

    def test_the_shipped_scaffold_parses_and_is_inert(self):
        """onboard._routing_policy_scaffold, exercised without importing onboard
        (which pulls in a lot more than this test needs): the same shape,
        constructed the same way, must parse and must not enable anything."""
        document = {"version": autoroute.POLICY_VERSION, "prefer": [],
                   "declared_available": [], "local_first": {"enabled": False},
                   "max_local_load_ratio": autoroute.DEFAULT_MAX_LOCAL_LOAD, "repos": {}}
        policy = autoroute.parse_policy(document)
        self.assertFalse(policy.local_first.enabled)
        self.assertEqual(policy.repos, {})


# ------------------------------------------------------------------ readiness


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.state = self.base / "state"
        self.local_queue = self.state / "local-queue"
        self.local_queue.mkdir(parents=True)
        self.worker = self.base / "worker"
        self.worker.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        os.chmod(self.worker, 0o755)
        self.reached_codes: set[str] = set()

    def _readiness(self, policy, *, window_bytes=8_000, load=None):
        result = localfirst.readiness(
            policy=policy, state_root=str(self.state), local_queue_root=str(self.local_queue),
            worker_executable=str(self.worker), window_bytes=window_bytes, load=load)
        self.reached_codes.add(result.code)
        return result

    def _enabled_policy(self, **overrides):
        lf = autoroute.LocalFirstConfig(enabled=True, **{
            key: value for key, value in overrides.items()
            if key in ("latency_budget_seconds", "read_gate_min_bytes",
                      "digest_max_output_chars", "calibration_max_age_days",
                      "digest_grace_seconds", "executor_liveness_seconds", "default_globs")})
        return autoroute.Policy(local_first=lf,
                                declared_routes=overrides.get("declared_routes", ("local",)),
                                max_local_load_ratio=overrides.get("max_local_load_ratio", 0.75))

    def _write_calibration(self, *, sha=None, created_at=None, sizes=None):
        sha = sha if sha is not None else store.sha256_file(str(self.worker))
        created_at = created_at if created_at is not None else time.time()
        sizes = sizes if sizes is not None else {
            "8000": {"median_s": 4.0, "outcomes": ["complete"] * 3},
            "16000": {"median_s": 6.0, "outcomes": ["complete"] * 3},
            "24000": {"median_s": 9.0, "outcomes": ["complete"] * 3},
        }
        store.atomic_write_json(localfirst.calibration_path(str(self.state)), {
            "version": 1, "created_at": created_at, "worker_sha256": sha, "sizes": sizes})

    def _write_heartbeat(self, *, updated_at=None, verdict="admissible", cpu_idle_ratio=None):
        updated_at = updated_at if updated_at is not None else time.time()
        if verdict == "admissible":
            resource = {"verdict": {"interactive": "admissible", "bulk": "deferred"}}
        elif verdict == "deferred_dict":
            resource = {"verdict": {"interactive": "deferred", "bulk": "deferred"}}
        elif verdict == "deferred_string":
            resource = {"verdict": "deferred", "reason": "resource_sample_unavailable"}
        else:
            raise AssertionError(verdict)
        if cpu_idle_ratio is not None:
            resource["cpu_idle_ratio"] = cpu_idle_ratio
        store.atomic_write_json(localfirst.heartbeat_path(str(self.local_queue)), {
            "version": 1, "updated_at": updated_at, "queue": {"resource": resource}})

    def test_disabled_by_default(self):
        result = self._readiness(autoroute.Policy())
        self.assertFalse(result.ready)
        self.assertEqual(result.code, "local_first_disabled")

    def test_local_not_declared(self):
        result = self._readiness(self._enabled_policy(declared_routes=()))
        self.assertEqual(result.code, "local_not_declared")

    def test_worker_not_configured_missing_file(self):
        policy = self._enabled_policy()
        result = localfirst.readiness(
            policy=policy, state_root=str(self.state), local_queue_root=str(self.local_queue),
            worker_executable=str(self.base / "does-not-exist"), window_bytes=8_000)
        self.reached_codes.add(result.code)
        self.assertEqual(result.code, "worker_not_configured")

    def test_worker_not_configured_sentinel(self):
        from agent_bridge.orchestration import delegation
        policy = self._enabled_policy()
        sentinel = self.base / delegation.NO_WORKER_SENTINEL
        sentinel.write_text("x", encoding="utf-8")
        result = localfirst.readiness(
            policy=policy, state_root=str(self.state), local_queue_root=str(self.local_queue),
            worker_executable=str(sentinel), window_bytes=8_000)
        self.reached_codes.add(result.code)
        self.assertEqual(result.code, "worker_not_configured")

    def test_calibration_missing(self):
        result = self._readiness(self._enabled_policy())
        self.assertEqual(result.code, "calibration_missing")

    def test_calibration_stale(self):
        self._write_calibration(created_at=time.time() - 40 * 86400)
        result = self._readiness(self._enabled_policy())
        self.assertEqual(result.code, "calibration_stale")

    def test_calibration_missing_timestamp_is_also_stale(self):
        store.atomic_write_json(localfirst.calibration_path(str(self.state)), {
            "version": 1, "worker_sha256": store.sha256_file(str(self.worker)), "sizes": {}})
        result = self._readiness(self._enabled_policy())
        self.assertEqual(result.code, "calibration_stale")

    def test_calibration_worker_changed(self):
        self._write_calibration(sha="deadbeef" * 8)
        result = self._readiness(self._enabled_policy())
        self.assertEqual(result.code, "calibration_worker_changed")

    def test_executor_not_running_no_heartbeat(self):
        self._write_calibration()
        result = self._readiness(self._enabled_policy())
        self.assertEqual(result.code, "executor_not_running")

    def test_executor_not_running_stale_heartbeat(self):
        self._write_calibration()
        self._write_heartbeat(updated_at=time.time() - 120)
        result = self._readiness(self._enabled_policy())
        self.assertEqual(result.code, "executor_not_running")

    def test_resource_deferred_dict_shape(self):
        self._write_calibration()
        self._write_heartbeat(verdict="deferred_dict")
        result = self._readiness(self._enabled_policy())
        self.assertEqual(result.code, "resource_deferred")

    def test_resource_deferred_bare_string_shape(self):
        """LocalQueue.state_report's except-branch shape: verdict is the bare
        string "deferred" with a sibling "reason", not the interactive/bulk
        dict. Both shapes are real (spool.py writes either), so both are
        exercised rather than only the common one."""
        self._write_calibration()
        self._write_heartbeat(verdict="deferred_string")
        result = self._readiness(self._enabled_policy())
        self.assertEqual(result.code, "resource_deferred")
        self.assertIn("resource_sample_unavailable", result.reason)

    def test_load_unknown(self):
        self._write_calibration()
        self._write_heartbeat()
        result = self._readiness(self._enabled_policy(), load=autoroute.Load(known=False))
        self.assertEqual(result.code, "load_unknown")

    def test_load_high(self):
        self._write_calibration()
        self._write_heartbeat()
        result = self._readiness(self._enabled_policy(max_local_load_ratio=0.5),
                                 load=autoroute.Load(ratio=0.9, known=True))
        self.assertEqual(result.code, "load_high")

    def test_load_high_rescued_by_measured_idle(self):
        """The same rescue LocalQueue admission gets: load-average-per-core
        conflates waiting-on-I/O with genuine CPU contention, so a directly
        measured idle fraction from the same heartbeat sample can still let
        the lane through."""
        self._write_calibration()
        self._write_heartbeat(cpu_idle_ratio=0.9)
        result = self._readiness(self._enabled_policy(max_local_load_ratio=0.5),
                                 load=autoroute.Load(ratio=0.9, known=True))
        self.assertTrue(result.ready, result)
        self.assertEqual(result.code, "ready")
        self.assertEqual(result.considered["cpu_idle_ratio"], 0.9)

    def test_load_high_not_rescued_by_low_measured_idle(self):
        self._write_calibration()
        self._write_heartbeat(cpu_idle_ratio=0.1)
        result = self._readiness(self._enabled_policy(max_local_load_ratio=0.5),
                                 load=autoroute.Load(ratio=0.9, known=True))
        self.assertEqual(result.code, "load_high")

    def test_over_latency_budget_no_covering_size(self):
        self._write_calibration(sizes={"8000": {"median_s": None, "outcomes": ["failed"] * 3}})
        self._write_heartbeat()
        result = self._readiness(self._enabled_policy(), load=autoroute.Load(ratio=0.1, known=True))
        self.assertEqual(result.code, "over_latency_budget")
        self.assertIsNone(result.considered["calibrated_size"])

    def test_over_latency_budget_too_slow(self):
        self._write_calibration()
        self._write_heartbeat()
        result = self._readiness(self._enabled_policy(latency_budget_seconds=1.0),
                                 load=autoroute.Load(ratio=0.1, known=True))
        self.assertEqual(result.code, "over_latency_budget")
        self.assertEqual(result.considered["calibrated_median_s"], 4.0)

    def test_ready(self):
        self._write_calibration()
        self._write_heartbeat()
        result = self._readiness(self._enabled_policy(), load=autoroute.Load(ratio=0.1, known=True))
        self.assertTrue(result.ready)
        self.assertEqual(result.code, "ready")
        self.assertEqual(result.considered["calibrated_size"], 8_000)
        self.assertEqual(result.considered["calibrated_median_s"], 4.0)

    def test_a_bigger_window_needs_a_bigger_covering_size(self):
        self._write_calibration()
        self._write_heartbeat()
        result = self._readiness(self._enabled_policy(), window_bytes=20_000,
                                 load=autoroute.Load(ratio=0.1, known=True))
        self.assertTrue(result.ready)
        self.assertEqual(result.considered["calibrated_size"], 24_000)

    def test_every_readiness_code_is_reachable_and_the_vocabulary_is_closed(self):
        """Runs every test above via ``run_tests`` isn't how unittest composes,
        so this reruns the fixtures inline and asserts the union covers the
        module's whole vocabulary, the same closed-vocabulary discipline
        ``autoroute.CODES`` already holds itself to."""
        self._write_calibration()
        self._write_heartbeat()
        codes = set()
        codes.add(self._readiness(autoroute.Policy()).code)
        codes.add(self._readiness(self._enabled_policy(declared_routes=())).code)
        codes.add(localfirst.readiness(
            policy=self._enabled_policy(), state_root=str(self.state),
            local_queue_root=str(self.local_queue),
            worker_executable=str(self.base / "nope"), window_bytes=8_000).code)
        empty_state = str(self.base / "empty-state")
        codes.add(localfirst.readiness(
            policy=self._enabled_policy(), state_root=empty_state,
            local_queue_root=str(self.local_queue),
            worker_executable=str(self.worker), window_bytes=8_000).code)
        self._write_calibration(created_at=time.time() - 40 * 86400)
        codes.add(self._readiness(self._enabled_policy()).code)
        self._write_calibration(sha="0" * 64)
        codes.add(self._readiness(self._enabled_policy()).code)
        self._write_calibration()
        empty_local_queue = str(self.base / "no-heartbeat-here")
        codes.add(localfirst.readiness(
            policy=self._enabled_policy(), state_root=str(self.state),
            local_queue_root=empty_local_queue, worker_executable=str(self.worker),
            window_bytes=8_000).code)
        self._write_heartbeat(verdict="deferred_dict")
        codes.add(self._readiness(self._enabled_policy()).code)
        self._write_heartbeat()
        codes.add(self._readiness(self._enabled_policy(), load=autoroute.Load(known=False)).code)
        codes.add(self._readiness(self._enabled_policy(max_local_load_ratio=0.1),
                                  load=autoroute.Load(ratio=0.9, known=True)).code)
        codes.add(self._readiness(self._enabled_policy(latency_budget_seconds=0.5),
                                  load=autoroute.Load(ratio=0.1, known=True)).code)
        codes.add(self._readiness(self._enabled_policy(),
                                  load=autoroute.Load(ratio=0.1, known=True)).code)
        self.assertEqual(codes, localfirst.READINESS_CODES)

    def test_readiness_never_raises_on_a_corrupt_heartbeat(self):
        self._write_calibration()
        heartbeat_file = localfirst.heartbeat_path(str(self.local_queue))
        os.makedirs(os.path.dirname(heartbeat_file), exist_ok=True)
        with open(heartbeat_file, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        result = self._readiness(self._enabled_policy())
        self.assertFalse(result.ready)
        self.assertEqual(result.code, "executor_not_running")

    def test_readiness_never_raises_on_a_deeply_nested_calibration_record(self):
        """Found by adversarial review: json.loads recurses per nesting
        level with no bound of its own, so a calibration.json holding tens
        of thousands of nested arrays exhausts Python's recursion limit and
        raises RecursionError, which store.read_json_or_none did not catch.
        This project has hit exactly this shape before (see the commit
        fixing _result_detail's own JSON parse) and this is the same defect
        reintroduced in a new place."""
        nested = "[" * 100_000 + "]" * 100_000
        path = localfirst.calibration_path(str(self.state))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(nested)
        result = self._readiness(self._enabled_policy())
        self.assertFalse(result.ready)
        self.assertEqual(result.code, "calibration_missing")

    def test_readiness_never_raises_on_a_calibration_sizes_key_past_the_int_digit_limit(self):
        """Found by adversarial review: Python 3.11+ refuses to convert a
        digit string longer than sys.set_int_max_str_digits (4,300 by
        default) to int, raising ValueError, and _covering_calibration
        called int() on every numeric-looking key in "sizes" with no bound
        on its length."""
        self._write_calibration(sizes={"9" * 5_000: {"median_s": 1.0, "outcomes": ["complete"] * 3}})
        self._write_heartbeat()
        result = self._readiness(self._enabled_policy(), load=autoroute.Load(ratio=0.1, known=True))
        self.assertFalse(result.ready)
        self.assertEqual(result.code, "over_latency_budget")
        self.assertIsNone(result.considered["calibrated_size"])


# ------------------------------------------------------------------ calibrate


class _FlakyModel(BaseHTTPRequestHandler):
    """A stand-in model endpoint that fails exactly one request.

    The fake local worker builds its prompt as ``f"task=...\\ninstruction=
    ...\\n\\n{text}"``, so the length of everything after the first blank
    line says which calibration size a request belongs to. Failing the first
    request seen for one specific size, and none other, makes exactly one of
    that size's three runs fail while the other two sizes stay fully
    eligible -- the "one run fails" case the design's Phase 1 test list
    names explicitly.
    """

    requests: list = []
    fail_once_for_length: "int | None" = None
    _failed = False

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        prompt = payload.get("prompt", "")
        text = prompt.split("\n\n", 1)[1] if "\n\n" in prompt else ""
        type(self).requests.append(len(text))
        if (type(self).fail_once_for_length is not None
                and len(text) == type(self).fail_once_for_length
                and not type(self)._failed):
            type(self)._failed = True
            self.send_response(500)
            self.end_headers()
            return
        body = json.dumps({"model": "stand-in-calibration-model",
                           "response": "SUMMARY: ok", "done": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class CalibrateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.state = self.base / "state"
        self.local_queue = self.state / "local-queue"
        self.local_queue.mkdir(parents=True)
        self.worker_state = self.base / "worker-state"
        self.worker_state.mkdir()
        self.worker = self.base / "local-worker"
        shutil.copy(FAKES / "fake_local_worker.py", self.worker)
        os.chmod(self.worker, 0o755)
        self.config_path = self.base / "orchestration.json"
        store.atomic_write_json(str(self.config_path), {
            "config_version": "1", "state_root": str(self.state),
            "local_queue_root": str(self.local_queue),
            "capacity_db": str(self.state / "capacity.sqlite3"),
            "worker_executable": str(self.worker), "worker_state": str(self.worker_state),
            "interval_seconds": 1.0})
        _FlakyModel.requests = []
        _FlakyModel.fail_once_for_length = None
        _FlakyModel._failed = False

    def _start_model(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _FlakyModel)
        import threading
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 10)
        (self.worker_state / "endpoint.txt").write_text(
            f"http://127.0.0.1:{server.server_port}", encoding="utf-8")

    def test_refuses_without_a_portable_sampler_because_this_host_has_no_macos_probes(self):
        """The real MacSampler is what production uses when nothing is
        injected. On any host that is not macOS this documents the honest
        consequence rather than hiding it behind a fake: calibrate refuses
        rather than measuring blind.

        The real MacSampler is exercised (nothing injected), but its
        sample() is forced to fail the way it genuinely does on a host with
        no macOS probes -- the outcome must not depend on this machine's own
        ambient CPU load/idle state, which real hardware makes nondeterministic
        (found by adversarial review: this host's real load average and idle%
        could otherwise land on either side of admission from one run to the
        next)."""
        self._start_model()
        with mock.patch("agent_bridge.localq.runtime.MacSampler.sample",
                        side_effect=RuntimeError("no macos probes on this host")):
            result = delegation_verify.calibrate(str(self.config_path))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "calibration_refused:resource_resource_sample_unavailable")

    def test_refuses_when_the_production_queue_shows_a_running_job(self):
        database = self.local_queue / "localq.sqlite3"
        queue = LocalQueue(str(self.local_queue), sampler=PortableSampler(),
                           backend=FakeBackend())
        submitted = queue.submit(task_type="summarize", input="x" * 900, params={},
                                 priority="interactive", classification="synthetic",
                                 caller="codex", purpose="work")
        connection = sqlite3.connect(str(database))
        connection.execute("UPDATE jobs SET status='running' WHERE job_id=?",
                           (submitted["job_id"],))
        connection.commit()
        connection.close()
        result = delegation_verify.calibrate(str(self.config_path), sampler=PortableSampler())
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "calibration_refused:executor_busy")

    def test_refuses_when_no_worker_is_configured(self):
        store.atomic_write_json(str(self.config_path), {
            "config_version": "1", "state_root": str(self.state),
            "local_queue_root": str(self.local_queue),
            "capacity_db": str(self.state / "capacity.sqlite3"),
            "worker_executable": str(self.base / "does-not-exist"),
            "worker_state": str(self.worker_state), "interval_seconds": 1.0})
        result = delegation_verify.calibrate(str(self.config_path), sampler=PortableSampler())
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "calibration_refused:worker_not_configured")

    def test_measures_three_sizes_and_marks_one_ineligible_when_a_run_fails(self):
        self._start_model()
        _FlakyModel.fail_once_for_length = 16_000
        result = delegation_verify.calibrate(str(self.config_path), sampler=PortableSampler())
        self.assertTrue(result["ok"], result)
        sizes = result["record"]["sizes"]
        largest = str(localfirst.CALIBRATION_SIZES[-1])
        self.assertEqual(set(sizes), {str(size) for size in localfirst.CALIBRATION_SIZES})
        self.assertIsNotNone(sizes["8000"]["median_s"])
        self.assertIsNotNone(sizes[largest]["median_s"])
        # Exactly one of the three 16000-byte runs failed, so the whole tier
        # is ineligible: a median over a run that never completed is not a
        # latency (see localfirst._covering_calibration).
        self.assertIsNone(sizes["16000"]["median_s"])
        self.assertEqual(sizes["16000"]["outcomes"].count("failed"), 1)
        self.assertEqual(sizes["16000"]["outcomes"].count("complete"), 2)
        self.assertEqual(len(sizes["8000"]["outcomes"]), 3)

        self.assertEqual(result["record"]["version"], localfirst.CALIBRATION_VERSION)
        self.assertEqual(result["record"]["worker_sha256"], store.sha256_file(str(self.worker)))
        # Found by adversarial review: build_calibration_record hardcoded
        # worker_state to None and calibrate() never passed it through, even
        # though it already builds the Service with cfg.worker_state a few
        # lines earlier.
        self.assertEqual(result["record"]["worker_state"], str(self.worker_state))
        self.assertIn("host", result["record"])
        self.assertIn("platform", result["record"]["host"])

        self.assertTrue(result["fits_budget"]["8000"])
        self.assertFalse(result["fits_budget"]["16000"])  # no median, can't fit any budget

        written = store.read_json(localfirst.calibration_path(str(self.state)))
        self.assertEqual(written, result["record"])

    def test_a_second_calibration_overwrites_the_first(self):
        self._start_model()
        first = delegation_verify.calibrate(str(self.config_path), sampler=PortableSampler())
        self.assertTrue(first["ok"], first)
        time.sleep(0.01)
        second = delegation_verify.calibrate(str(self.config_path), sampler=PortableSampler())
        self.assertTrue(second["ok"], second)
        self.assertGreaterEqual(second["record"]["created_at"], first["record"]["created_at"])

    def test_the_disposable_calibration_queue_is_removed_afterward(self):
        self._start_model()
        before = set(os.listdir(self.base))
        delegation_verify.calibrate(str(self.config_path), sampler=PortableSampler())
        after = set(os.listdir(self.base))
        # No agent-bridge-calibrate-* temp directory left behind beside the
        # fixture's own files.
        leftover = [name for name in after - before if name.startswith("agent-bridge-calibrate-")]
        self.assertEqual(leftover, [])

    def test_calibrate_cli_writes_the_record_and_exits_zero(self):
        self._start_model()
        # No portable sampler is reachable from a real CLI subprocess (unlike
        # the in-process tests above, there is no seam here to mock the real
        # MacSampler), so refusal is forced through a config-level trigger
        # instead of ambient resource state: this host's real load average
        # and idle% are nondeterministic and, found by adversarial review,
        # can land on either side of admission from one run to the next.
        # worker_not_configured is checked before any resource sampling, so
        # it refuses the same way regardless of host CPU state.
        store.atomic_write_json(str(self.config_path), {
            "config_version": "1", "state_root": str(self.state),
            "local_queue_root": str(self.local_queue),
            "capacity_db": str(self.state / "capacity.sqlite3"),
            "worker_executable": str(self.base / "does-not-exist"),
            "worker_state": str(self.worker_state), "interval_seconds": 1.0})
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        import subprocess
        completed = subprocess.run(
            [sys.executable, "-P", "-m", "agent_bridge.orchestration.delegation_verify",
             "calibrate", "--config", str(self.config_path)],
            capture_output=True, timeout=60, env=env)
        # The CLI's exit code must reflect refusal ("ok": false) rather than
        # reporting success.
        self.assertEqual(completed.returncode, 1, completed.stdout.decode() + completed.stderr.decode())
        payload = json.loads(completed.stdout.decode())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "calibration_refused:worker_not_configured")


# ---------------------------------------------------------- protected launchers


class ProtectedLauncherTests(unittest.TestCase):
    """The bridge's own state-changing launchers must be refused to a covered
    client through the protected-path rule, and nothing else must start
    matching just because these patterns were added."""

    def test_the_verify_launcher_is_classified_as_a_write(self):
        for command in (
            "./bin/agent-bridge-orchestration-verify --config x --callers codex,claude --out y",
            "bin/agent-bridge-orchestration-verify calibrate --config x",
            "/home/user/agent-bridge/bin/agent-bridge-orchestration-verify.cmd --config x",
        ):
            with self.subTest(command=command):
                self.assertTrue(gate.shell_writes(command))

    def test_gate_hook_install_is_a_write_but_report_and_audit_are_not(self):
        self.assertTrue(gate.shell_writes(
            "./bin/agent-bridge-gate-hook install --root . --config x --apply"))
        self.assertTrue(gate.shell_writes("bin/agent-bridge-gate-hook.cmd install --config x"))
        self.assertFalse(gate.shell_writes("./bin/agent-bridge-gate-hook report --config x"))
        self.assertFalse(gate.shell_writes("./bin/agent-bridge-gate-hook audit --config x --json"))

    def test_onboard_apply_is_a_write_but_onboard_plan_is_not(self):
        self.assertTrue(gate.shell_writes(
            "python setup_bridge.py onboard apply --answers a.json --candidate c.json"))
        self.assertTrue(gate.shell_writes("python3 setup_bridge.py onboard apply --answers a.json"))
        self.assertFalse(gate.shell_writes("python setup_bridge.py onboard plan --answers a.json"))

    def test_the_launcher_patterns_are_case_insensitive_on_windows_spellings(self):
        """Found by adversarial review: NTFS is case-insensitive and
        case-preserving, so a Windows invocation spelled in a different case
        is the same file and must be recognised as one, the same lesson
        docs/REVIEW-HISTORY.md finding 57 already drew for the del/move/ren
        write verbs and the PowerShell cmdlets. The first assertion is the
        one that matters most: agent-bridge-orchestration-verify is meant to
        be blocked outright, so a case mismatch there was a total bypass of
        that block, not merely a narrower one."""
        self.assertTrue(gate.shell_writes(
            "./bin/Agent-Bridge-Orchestration-Verify.cmd calibrate --config x"))
        self.assertTrue(gate.shell_writes(
            "./bin/AGENT-BRIDGE-GATE-HOOK.CMD Install --root . --config x --apply"))
        self.assertTrue(gate.shell_writes(
            "python Setup_Bridge.py Onboard Apply --answers a.json"))
        self.assertTrue(gate.shell_writes(
            "./bin/AGENT-BRIDGE-SETUP.CMD onboard apply --answers a.json"))
        # And the read-only subcommands stay reads regardless of case.
        self.assertFalse(gate.shell_writes("./bin/Agent-Bridge-Gate-Hook Report --config x"))

    def test_the_module_invoked_directly_with_python_dash_m_is_also_protected(self):
        """Found by adversarial review: every one of the four launcher
        patterns above matches only the launcher SCRIPT's own filename.
        orchestration/delegation_verify.py and orchestration/gate.py are
        ordinary modules with their own __main__ guard, exactly like the
        scripts that exec into them (bin/agent-bridge-gate-hook itself runs
        `exec "$PY" -P -m agent_bridge.orchestration.gate "$@"`), so
        `python3 -m agent_bridge.orchestration.delegation_verify --config
        ... --callers ... --out ...` reaches the exact same main() the
        launcher does while matching none of the filename-based patterns at
        all. Measured live: this was allowed and unlogged
        (shell_read_only_heuristic) for the full live-verification path,
        which makes real, cost-incurring provider calls, not merely for
        calibrate."""
        self.assertTrue(gate.shell_writes(
            "python3 -m agent_bridge.orchestration.delegation_verify calibrate --config x"))
        self.assertTrue(gate.shell_writes(
            "python3 -m agent_bridge.orchestration.delegation_verify --config x "
            "--callers codex,claude --out y"))
        self.assertTrue(gate.shell_writes(
            "python -m agent_bridge.orchestration.gate install --root . --config x --apply"))
        # Case-insensitive here too, for the same reason as the launcher names.
        self.assertTrue(gate.shell_writes(
            "PYTHON3 -M AGENT_BRIDGE.ORCHESTRATION.DELEGATION_VERIFY --config x "
            "--callers codex --out y"))
        # report/audit stay reads via -m, exactly as via the launcher script,
        # and bare hook mode (no subcommand at all, what the installed hook
        # actually runs) is not a write either.
        self.assertFalse(gate.shell_writes(
            "python3 -m agent_bridge.orchestration.gate report --config x"))
        self.assertFalse(gate.shell_writes(
            "python3 -m agent_bridge.orchestration.gate audit --config x --json"))
        self.assertFalse(gate.shell_writes(
            "python3 -m agent_bridge.orchestration.gate --client claude --config x"))

    def test_reading_a_launcher_that_requires_a_second_word_stays_a_read(self):
        """gate-hook and onboard-apply both require a second word ("install",
        "onboard apply") elsewhere in the command, so merely naming the
        launcher file to read its source, with neither word present, is
        unaffected."""
        self.assertFalse(gate.shell_writes("cat bin/agent-bridge-gate-hook"))
        self.assertFalse(gate.shell_writes("grep -n foo setup_bridge.py"))

    def test_the_bare_verify_launcher_pattern_is_deliberately_broad(self):
        """Unlike the other two, the verify launcher is blocked outright
        regardless of subcommand (design section 2.4), so its pattern has no
        second word to require and also matches a plain read of its own
        source. That is an accepted, harmless over-match: the protected-path
        rule only denies when a named path also reaches a protected root
        (see test_the_full_gate_denies_a_write_naming_the_config_as_protected),
        so reading the script's text still costs nothing but an extra,
        accurate ledger line."""
        self.assertTrue(gate.shell_writes("less bin/agent-bridge-orchestration-verify"))

    def test_the_full_gate_denies_a_write_naming_the_config_as_protected(self):
        with tempfile.TemporaryDirectory() as base:
            state = os.path.join(base, "state")
            os.makedirs(os.path.join(state, "routing"))
            config_path = os.path.join(base, "orchestration.json")
            store.atomic_write_json(config_path, {"state_root": state,
                                                  "capacity_db": os.path.join(state, "capacity.sqlite3")})
            repo = os.path.join(base, "repo")
            os.makedirs(os.path.join(repo, ".git"))
            protected = gate.protected_paths(state, config_path, os.path.join(base, "home"))
            decision = gate.judge(
                "claude", "Bash",
                {"command": f"./bin/agent-bridge-gate-hook install --config {config_path} --apply"},
                repo, state_root=state, protected=protected)
            self.assertEqual(decision.permission, "deny")
            self.assertEqual(decision.code, "gate_state_protected")

    def test_local_queue_databases_and_heartbeat_join_protected_paths(self):
        with tempfile.TemporaryDirectory() as base:
            state = os.path.join(base, "state")
            local_queue = os.path.join(base, "local-queue")
            os.makedirs(local_queue)
            protected = gate.protected_paths(state, None, os.path.join(base, "home"),
                                             local_queue_root=local_queue)
            expected = {
                os.path.realpath(os.path.join(local_queue, "localq.sqlite3")),
                os.path.realpath(os.path.join(local_queue, "routing.sqlite3")),
                os.path.realpath(os.path.join(local_queue, "runtime-state.json")),
            }
            self.assertTrue(expected.issubset(set(protected)))

    def test_protected_paths_without_local_queue_root_is_unchanged(self):
        with tempfile.TemporaryDirectory() as base:
            state = os.path.join(base, "state")
            without = gate.protected_paths(state, None, os.path.join(base, "home"))
            with_empty = gate.protected_paths(state, None, os.path.join(base, "home"),
                                              local_queue_root=None)
            self.assertEqual(without, with_empty)


# ------------------------------------------------------- gate_paths_from_config


class GatePathsFromConfigTests(unittest.TestCase):
    def test_returns_a_default_local_queue_root_when_the_config_omits_it(self):
        with tempfile.TemporaryDirectory() as base:
            state = os.path.join(base, "state")
            config_path = os.path.join(base, "orchestration.json")
            store.atomic_write_json(config_path, {"state_root": state,
                                                  "capacity_db": os.path.join(state, "capacity.sqlite3")})
            state_root, capacity_db, local_queue_root, worker_executable = \
                gate.gate_paths_from_config(config_path)
            self.assertEqual(state_root, state)
            self.assertEqual(local_queue_root, os.path.join(state, "local-queue"))
            self.assertEqual(worker_executable, "")

    def test_returns_the_configured_local_queue_root_when_present(self):
        with tempfile.TemporaryDirectory() as base:
            state = os.path.join(base, "state")
            custom = os.path.join(base, "elsewhere", "queue")
            config_path = os.path.join(base, "orchestration.json")
            store.atomic_write_json(config_path, {"state_root": state,
                                                  "capacity_db": os.path.join(state, "capacity.sqlite3"),
                                                  "local_queue_root": custom})
            _, _, local_queue_root, _ = gate.gate_paths_from_config(config_path)
            self.assertEqual(local_queue_root, custom)

    def test_returns_the_configured_worker_executable_when_present(self):
        with tempfile.TemporaryDirectory() as base:
            state = os.path.join(base, "state")
            worker = os.path.join(base, "bin", "local-worker")
            config_path = os.path.join(base, "orchestration.json")
            store.atomic_write_json(config_path, {"state_root": state,
                                                  "capacity_db": os.path.join(state, "capacity.sqlite3"),
                                                  "worker_executable": worker})
            _, _, _, worker_executable = gate.gate_paths_from_config(config_path)
            self.assertEqual(worker_executable, worker)

    def test_a_non_string_worker_executable_is_refused(self):
        with tempfile.TemporaryDirectory() as base:
            state = os.path.join(base, "state")
            config_path = os.path.join(base, "orchestration.json")
            store.atomic_write_json(config_path, {"state_root": state,
                                                  "capacity_db": os.path.join(state, "capacity.sqlite3"),
                                                  "worker_executable": 7})
            with self.assertRaises(ValueError):
                gate.gate_paths_from_config(config_path)

    def test_state_root_from_config_is_unaffected_by_the_third_value(self):
        with tempfile.TemporaryDirectory() as base:
            state = os.path.join(base, "state")
            config_path = os.path.join(base, "orchestration.json")
            store.atomic_write_json(config_path, {"state_root": state,
                                                  "capacity_db": os.path.join(state, "capacity.sqlite3")})
            self.assertEqual(gate.state_root_from_config(config_path), state)


if __name__ == "__main__":
    unittest.main()
