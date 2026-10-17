"""The verification-only capability skip is narrow and pre-body."""

from __future__ import annotations

import errno
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import verification_capabilities as capabilities
from agent_bridge.execution import verification_diagnostics as diagnostics


class CapabilitySkipTests(unittest.TestCase):
    def _run_body(self) -> unittest.TestResult:
        class BodyFailure(unittest.TestCase):
            def runTest(self):
                capabilities.requires_nested_confinement(self)
                self.fail("test body ran")

        result = unittest.TestResult()
        BodyFailure().run(result)
        return result

    def test_no_marker_runs_the_body_even_when_the_probe_would_deny(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(capabilities, "_nested_confinement_denied",
                               return_value=True) as probe:
            result = self._run_body()
        self.assertEqual(len(result.skipped), 0)
        self.assertEqual(len(result.failures), 1)
        probe.assert_not_called()

    def test_marker_with_an_available_capability_runs_a_real_assertion_failure(self):
        with mock.patch.dict(os.environ, {diagnostics.JOB_VERIFICATION_MARKER: "1"}, clear=True), \
             mock.patch.object(capabilities, "_nested_confinement_denied", return_value=False):
            result = self._run_body()
        self.assertEqual(len(result.skipped), 0)
        self.assertEqual(len(result.failures), 1)

    def test_marker_and_denied_probe_skip_before_the_body_and_record_fixed_count(self):
        with tempfile.TemporaryDirectory() as temp:
            record = Path(temp) / diagnostics.SKIP_RECORD_NAME
            env = {diagnostics.JOB_VERIFICATION_MARKER: "1",
                   diagnostics.SKIP_RECORD_PATH: str(record)}
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(capabilities, "_nested_confinement_denied", return_value=True):
                result = self._run_body()
            self.assertEqual(result.skipped, [(result.skipped[0][0], diagnostics.NESTED_CONFINEMENT_UNAVAILABLE)])
            self.assertEqual(len(result.failures), 0)
            self.assertEqual(diagnostics.skip_counts(record), {
                diagnostics.NESTED_CONFINEMENT_UNAVAILABLE: 1})

    def test_only_sandbox_denials_make_the_loopback_probe_a_skip_condition(self):
        # sandbox-exec refuses with EPERM or EACCES; Linux unshare --net
        # leaves lo down, so the bind fails with EADDRNOTAVAIL.
        probe = mock.MagicMock()
        for code in (errno.EPERM, errno.EACCES, errno.EADDRNOTAVAIL):
            probe.__enter__.return_value.bind.side_effect = OSError(code, "denied")
            with mock.patch.object(capabilities.socket, "socket", return_value=probe):
                self.assertTrue(capabilities._loopback_denied(), code)
        probe.__enter__.return_value.bind.side_effect = OSError(errno.EADDRINUSE, "in use")
        with mock.patch.object(capabilities.socket, "socket", return_value=probe):
            self.assertFalse(capabilities._loopback_denied())



class SkipRecordReaderTests(unittest.TestCase):
    """The record lives in a scratch directory the verify command can write."""

    def _counts_within(self, path: Path, seconds: float = 5.0) -> dict:
        answer: list[dict] = []
        reader = threading.Thread(target=lambda: answer.append(diagnostics.skip_counts(path)),
                                  daemon=True)
        reader.start()
        reader.join(seconds)
        self.assertFalse(reader.is_alive(), "skip_counts blocked")
        return answer[0]

    def test_regular_record_is_counted(self):
        with tempfile.TemporaryDirectory() as temp:
            record = Path(temp) / diagnostics.SKIP_RECORD_NAME
            record.write_bytes(b"loopback_unavailable\nunknown\nloopback_unavailable\n")
            self.assertEqual(self._counts_within(record),
                             {diagnostics.LOOPBACK_UNAVAILABLE: 2})

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFOs are POSIX only")
    def test_fifo_with_no_writer_is_ignored_without_blocking(self):
        with tempfile.TemporaryDirectory() as temp:
            record = Path(temp) / diagnostics.SKIP_RECORD_NAME
            os.mkfifo(record)
            self.assertEqual(self._counts_within(record), {})

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFOs are POSIX only")
    def test_symlink_to_fifo_or_file_is_not_followed(self):
        with tempfile.TemporaryDirectory() as temp:
            fifo = Path(temp) / "fifo"
            os.mkfifo(fifo)
            regular = Path(temp) / "regular"
            regular.write_bytes(b"loopback_unavailable\n")
            record = Path(temp) / diagnostics.SKIP_RECORD_NAME
            for target in (fifo, regular):
                record.symlink_to(target)
                self.assertEqual(self._counts_within(record), {})
                record.unlink()


if __name__ == "__main__":
    unittest.main()
