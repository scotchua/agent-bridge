"""Offline contracts for synthetic delegation verification."""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge.orchestration import delegation_verify as dv


class EmptyRowTests(unittest.TestCase):
    def test_unattempted_direction_is_explicit(self):
        self.assertIs(dv._empty_row("offline")["attempted"], False)


class RunDirectionTests(unittest.TestCase):
    def test_submitted_direction_is_marked_attempted(self):
        class Queue:
            def __init__(self, *args, **kwargs):
                pass

            def submit(self, **kwargs):
                return {"job_id": "invented-job"}

            def status(self, job_id):
                return {"state": "complete"}

            def result(self, job_id):
                return {"state": "complete", "classification": "synthetic",
                        "harness": {"returncode": 0, "harness_ok": True}}

        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "invented-repo"
            repo.mkdir()
            with mock.patch.object(dv, "_disposable_repo", return_value=repo), \
                 mock.patch.object(dv, "ExecutionQueue", Queue):
                row = dv._run_direction(object(), None, caller="codex", provider="claude")
        self.assertIs(row["attempted"], True)


class LocalModelParamsTests(unittest.TestCase):
    def test_private_worker_receives_automatic_provider(self):
        params = dv._local_model_params(SimpleNamespace(local_backend="private_worker"))
        self.assertEqual(params["provider"], "auto")


class LocalModelCheckTests(unittest.TestCase):
    def test_certified_backend_does_not_require_private_worker(self):
        class Service:
            @classmethod
            def for_config(cls, cfg, *, root):
                return cls()

        cfg = SimpleNamespace(local_backend="gemma_certified")
        with mock.patch("agent_bridge.localq.service.Service", Service), \
             mock.patch.object(dv, "_local_model_check_in", return_value={"status": "complete"}):
            self.assertEqual(dv._local_model_check(cfg)["status"], "complete")


class LocalModelCheckInTests(unittest.TestCase):
    def test_completed_job_reports_synthetic_source(self):
        class Queue:
            def submit(self, **kwargs):
                return {"job_id": "invented-job"}

            def status(self, job_id):
                return {"status": "complete"}

            def result(self, job_id):
                return {"status": "complete"}

        service = SimpleNamespace(queue=Queue())
        result = dv._local_model_check_in(SimpleNamespace(local_backend="private_worker"), service)
        self.assertEqual(result, {"status": "complete", "source_classification": "synthetic"})


class SyntheticCalibrationInputTests(unittest.TestCase):
    def test_final_run_index_is_accepted(self):
        self.assertIn("run 99", dv._synthetic_calibration_input(8000, 99))

    def test_requested_byte_length_is_exact(self):
        self.assertEqual(len(dv._synthetic_calibration_input(8000, 1).encode("ascii")), 8000)

    def test_negative_run_index_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "calibration_input_invalid"):
            dv._synthetic_calibration_input(8000, -1)


class RunTests(unittest.TestCase):
    def test_report_affirms_no_client_data(self):
        with mock.patch.object(dv, "load_orchestration_config", return_value=SimpleNamespace()), \
             mock.patch.object(dv.store, "read_json", return_value={}), \
             mock.patch.object(dv.store, "utc_now", return_value="2024-03-01T00:00:00Z"), \
             mock.patch.object(dv, "select_executor", return_value=(None, "offline")), \
             mock.patch.object(dv, "_local_model_check", return_value={"status": "not_configured"}):
            result = dv.run("invented-config.json", callers=("codex",))
        self.assertIs(result["no_client_data"], True)


class MainTests(unittest.TestCase):
    def test_partial_arguments_are_rejected_before_running(self):
        with mock.patch.object(dv.store, "set_umask"), \
             mock.patch.object(dv, "run") as run, \
             mock.patch.object(dv.store, "atomic_write_json"), \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                dv.main(["--config", "invented-config.json"])
        self.assertEqual(raised.exception.code, 2)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
