"""The verification-only capability skip is narrow and pre-body."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests import verification_capabilities as capabilities
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

    def test_only_permission_errors_make_the_loopback_probe_a_skip_condition(self):
        denied = OSError(1, "Operation not permitted")
        unavailable = OSError(99, "Cannot assign requested address")
        probe = mock.MagicMock()
        probe.__enter__.return_value.bind.side_effect = denied
        with mock.patch.object(capabilities.socket, "socket", return_value=probe):
            self.assertTrue(capabilities._loopback_denied())
        probe.__enter__.return_value.bind.side_effect = unavailable
        with mock.patch.object(capabilities.socket, "socket", return_value=probe):
            self.assertFalse(capabilities._loopback_denied())


if __name__ == "__main__":
    unittest.main()
