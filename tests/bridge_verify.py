"""Run the broker's script suite through the existing unittest-only policy.

This module is explicitly selected by the repo verification defaults. Its name
does not match discovery, so the broker suite runs once per build dispatch.
"""
from pathlib import Path
import subprocess
import sys
import unittest


class BrokerSuite(unittest.TestCase):
    def test_broker_suite(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, str(root / "tests" / "test_suite.py")],
                                cwd=root, check=False)
        self.assertEqual(result.returncode, 0, "tests/test_suite.py failed; see verification log")
