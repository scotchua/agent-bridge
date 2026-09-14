"""Offline regression tests for health-check executable binding."""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from harness import Sandbox
from agent_bridge import health
from agent_bridge.errors import BrokerError, ErrorCategory


class HealthPinningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox()

    def tearDown(self) -> None:
        self.sandbox.cleanup()

    def test_path_alternative_is_reported_but_never_executed(self) -> None:
        validated = {
            "executable": "/configured/claude",
            "realpath": "/validated/claude",
            "observed_version": "approved",
            "allowed_versions": ["approved"],
            "version_pinned": True,
        }
        with (mock.patch.object(health.preflight, "discover_executable",
                                return_value="/path/alternative-claude"),
              mock.patch.object(health.preflight, "check_peer", return_value=validated),
              mock.patch.object(health.preflight, "observed_version") as version,
              mock.patch.object(health, "auth_status", return_value={
                  "state": "unknown", "network_verified": False,
              })):
            report = health.inspect_peer(self.sandbox.cfg, "claude")

        version.assert_not_called()
        self.assertTrue(report["path_differs"])
        self.assertEqual(report["path_executable"], "/path/alternative-claude")
        self.assertNotIn("path_version", report)

    def test_auth_uses_the_preflight_validated_realpath(self) -> None:
        validated = {
            "executable": "/configured/claude",
            "realpath": "/validated/claude",
            "observed_version": "approved",
            "allowed_versions": ["approved"],
            "version_pinned": True,
        }
        with (mock.patch.object(health.preflight, "discover_executable", return_value=None),
              mock.patch.object(health.preflight, "check_peer", return_value=validated),
              mock.patch.object(health, "auth_status", return_value={
                  "state": "signed_in", "network_verified": False,
              }) as auth):
            report = health.inspect_peer(self.sandbox.cfg, "claude")

        auth.assert_called_once_with("claude", "/validated/claude", mock.ANY)
        self.assertEqual(report["executable"], "/configured/claude")
        self.assertEqual(report["realpath"], "/validated/claude")
        self.assertEqual(report["operator_login_argv"][0], "/validated/claude")

    def test_preflight_failure_never_probes_auth(self) -> None:
        with (mock.patch.object(health.preflight, "discover_executable", return_value=None),
              mock.patch.object(health.preflight, "check_peer", side_effect=BrokerError(
                  ErrorCategory.PREFLIGHT_VERSION_MISMATCH,
              )),
              mock.patch.object(health, "auth_status") as auth):
            report = health.inspect_peer(self.sandbox.cfg, "claude")

        auth.assert_not_called()
        self.assertEqual(report["preflight"], ErrorCategory.PREFLIGHT_VERSION_MISMATCH.value)
        self.assertEqual(report["auth"]["state"], "not_checked")
        self.assertIsNone(report["operator_login_argv"])


if __name__ == "__main__":
    unittest.main()
