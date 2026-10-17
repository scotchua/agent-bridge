"""Proofs for the bounded Windows SQLite fixture teardown helper."""

import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import sqlite_teardown


def sharing_violation():
    error = PermissionError(32, "sharing violation")
    error.winerror = 32
    return error


class Temporary:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = 0

    def cleanup(self):
        self.calls += 1
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException):
            raise outcome


class SqliteTeardownTests(unittest.TestCase):
    def test_windows_sharing_violation_retries_within_the_fixed_bound(self):
        temporary = Temporary([sharing_violation(), None])
        with mock.patch.object(sqlite_teardown.os, "name", "nt"), \
                mock.patch.object(sqlite_teardown.time, "sleep") as sleep:
            sqlite_teardown.cleanup_temporary_directory(temporary)
        self.assertEqual(temporary.calls, 2)
        sleep.assert_called_once_with(sqlite_teardown._RETRY_DELAY_SECONDS)

    def test_held_open_handle_fails_after_the_bound_with_original_error(self):
        held_open = sharing_violation()
        temporary = Temporary([held_open] * sqlite_teardown._RETRY_COUNT)
        with tempfile.TemporaryDirectory() as directory:
            connection = sqlite3.connect(os.path.join(directory, "held.sqlite3"))
            try:
                with mock.patch.object(sqlite_teardown.os, "name", "nt"), \
                        mock.patch.object(sqlite_teardown.time, "sleep") as sleep, \
                        self.assertRaises(PermissionError) as caught:
                    sqlite_teardown.cleanup_temporary_directory(temporary)
            finally:
                connection.close()
        self.assertIs(caught.exception, held_open)
        self.assertEqual(temporary.calls, sqlite_teardown._RETRY_COUNT)
        self.assertEqual(sleep.call_count, sqlite_teardown._RETRY_COUNT - 1)
        self.assertIn("sqlite_teardown owners:", "\n".join(caught.exception.__notes__))

    def test_other_cleanup_errors_fail_without_retry(self):
        denied = PermissionError(5, "access denied")
        denied.winerror = 5
        temporary = Temporary([denied])
        with mock.patch.object(sqlite_teardown.os, "name", "nt"), \
                mock.patch.object(sqlite_teardown.time, "sleep") as sleep, \
                self.assertRaises(PermissionError) as caught:
            sqlite_teardown.cleanup_temporary_directory(temporary)
        self.assertIs(caught.exception, denied)
        self.assertEqual(temporary.calls, 1)
        sleep.assert_not_called()
