"""Offline regression tests for the local queue service boundary."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.localq import service


class ServiceMutationTests(unittest.TestCase):
    def test_atomic_state_creates_parent_and_writes_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "nested" / "state.json"
            service.atomic_state(path, {"company": "Cedar Finch LLC", "amount": 41})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")),
                             {"company": "Cedar Finch LLC", "amount": 41})

    def test_atomic_state_requests_private_parent_permissions(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "nested" / "state.json"
            calls = []
            real_mkdir = Path.mkdir

            def observe_mkdir(directory, mode=0o777, parents=False, exist_ok=False):
                calls.append((directory, mode, parents, exist_ok))
                return real_mkdir(directory, mode=mode, parents=parents, exist_ok=exist_ok)

            with mock.patch.object(Path, "mkdir", observe_mkdir):
                service.atomic_state(path, {"account": "Birch Ledger"})
            self.assertIn((path.parent, 0o700, True, True), calls)

    def test_atomic_state_flushes_before_fsync(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            sizes = []
            real_fsync = os.fsync

            def observe_fsync(fd):
                sizes.append(os.fstat(fd).st_size)
                return real_fsync(fd)

            with mock.patch.object(service.os, "fsync", side_effect=observe_fsync):
                service.atomic_state(path, {"account": "Elm Reserve", "amount": 17})
            self.assertEqual(len(sizes), 1)
            self.assertGreater(sizes[0], 0)

    def test_atomic_state_cleans_temporary_file_after_replace_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            with mock.patch.object(service.os, "replace", side_effect=OSError("synthetic failure")):
                with self.assertRaisesRegex(OSError, "synthetic failure"):
                    service.atomic_state(path, {"date": "2031-06-04"})
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_service_init_rejects_truthy_nonstring_backend_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(service, "LocalQueue") as queue:
                with self.assertRaisesRegex(ValueError, "backend_id_invalid"):
                    service.Service(temporary, "worker", "state", backend_id=42)
            queue.assert_not_called()

    def test_main_once_requires_options_and_returns_integer_zero(self):
        args = ["--root", "queue", "--worker", "worker", "--worker-state", "state", "--once"]
        with mock.patch.object(service, "Service") as service_class, \
                mock.patch.object(service.time, "sleep", side_effect=KeyboardInterrupt):
            result = service.main(args)
        self.assertIs(type(result), int)
        self.assertEqual(result, 0)
        service_class.assert_called_once_with("queue", "worker", "state")
        service_class.return_value.once.assert_called_once_with()

    def test_main_rejects_zero_and_negative_intervals(self):
        for interval in ("0", "-1"):
            with self.subTest(interval=interval), mock.patch.object(service, "Service") as service_class:
                args = ["--root", "queue", "--worker", "worker", "--worker-state", "state",
                        "--once", "--interval", interval]
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        service.main(args)
                self.assertEqual(error.exception.code, 2)
                service_class.assert_not_called()

    def test_main_accepts_positive_fractional_interval_and_sleeps(self):
        args = ["--root", "queue", "--worker", "worker", "--worker-state", "state",
                "--interval", "0.5"]
        with mock.patch.object(service, "Service") as service_class, \
                mock.patch.object(service.time, "sleep", side_effect=KeyboardInterrupt) as sleep:
            service_class.return_value.once.side_effect = [None, KeyboardInterrupt]
            with self.assertRaises(KeyboardInterrupt):
                service.main(args)
        service_class.return_value.once.assert_called_once_with()
        sleep.assert_called_once_with(0.5)

    def test_main_default_interval_runs_between_service_calls(self):
        args = ["--root", "queue", "--worker", "worker", "--worker-state", "state"]
        with mock.patch.object(service, "Service") as service_class, \
                mock.patch.object(service.time, "sleep", side_effect=[None, KeyboardInterrupt]) as sleep:
            service_class.return_value.once.side_effect = [None, KeyboardInterrupt]
            with self.assertRaises(KeyboardInterrupt):
                service.main(args)
        self.assertEqual(service_class.return_value.once.call_count, 2)
        sleep.assert_called_once_with(5.0)


if __name__ == "__main__":
    unittest.main()
