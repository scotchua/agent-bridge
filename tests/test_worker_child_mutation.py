"""Offline boundary tests for the private worker child adapter."""

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.localq import worker_child


class WorkerChildMutationTests(unittest.TestCase):
    def _response(self, *, content=None):
        if content is None:
            value = {"status": "draft", "output": {"note": "draft"},
                     "provider": "apple", "job_id": "job-204"}
            content = [{"type": "text", "text": json.dumps(value)}]
        wire = json.dumps({"result": {}}) + "\n" + json.dumps(
            {"result": {"isError": False, "content": content}}) + "\n"
        return SimpleNamespace(returncode=0, stdout=wire)

    def _invoke(self, payload=None, *, response=None):
        if payload is None:
            payload = {"job_id": "job-204", "task_type": "summarize",
                       "input": "Invented source", "params": {}}
        if response is None:
            response = self._response()
        with patch.object(worker_child, "_paths", return_value=(Path("worker.py"), Path("state"))), \
                patch.object(worker_child, "_provenance", return_value=("synthetic", "codex", "test")), \
                patch.object(worker_child.subprocess, "run", return_value=response) as run:
            result = worker_child.invoke(payload, worker="worker.py", state="state",
                                         queue_root="queue")
        return result, run

    def test_paths_reject_missing_state_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            worker = Path(temporary) / "worker.py"
            worker.write_text("# invented worker", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "worker state must be"):
                worker_child._paths(str(worker), str(Path(temporary) / "absent"))

    def test_provenance_closes_connection(self):
        with tempfile.TemporaryDirectory() as temporary:
            (Path(temporary) / "localq.sqlite3").touch()
            connection = Mock()
            connection.execute.return_value.fetchone.return_value = ("synthetic", "codex", "test")
            with patch.object(worker_child.sqlite3, "connect", return_value=connection):
                self.assertEqual(worker_child._provenance(temporary, "job-204"),
                                 ("synthetic", "codex", "test"))
            connection.close.assert_called_once_with()

    def test_provenance_rejects_unapproved_classification(self):
        with tempfile.TemporaryDirectory() as temporary:
            (Path(temporary) / "localq.sqlite3").touch()
            connection = Mock()
            connection.execute.return_value.fetchone.return_value = ("private", "codex", "test")
            with patch.object(worker_child.sqlite3, "connect", return_value=connection):
                with self.assertRaisesRegex(ValueError, "queue provenance unavailable"):
                    worker_child._provenance(temporary, "job-204")

    def test_invoke_rejects_unsupported_task_before_worker_call(self):
        payload = {"job_id": "job-204", "task_type": "archive", "input": "Invented source", "params": {}}
        with patch.object(worker_child, "_paths", return_value=(Path("worker.py"), Path("state"))), \
                patch.object(worker_child.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "unsupported queue payload"):
                worker_child.invoke(payload, worker="worker.py", state="state",
                                    queue_root="queue")
            run.assert_not_called()

    def test_invoke_uses_default_timeout(self):
        _, run = self._invoke()
        self.assertEqual(run.call_args.kwargs["timeout"], 65.0)

    def test_invoke_returns_worker_job_id(self):
        result, _ = self._invoke()
        self.assertEqual(result["worker_job_id"], "job-204")

    def test_invoke_rejects_non_list_content(self):
        with self.assertRaisesRegex(RuntimeError, "private worker response invalid"):
            self._invoke(response=self._response(content=42))

    def test_main_success_prints_result_and_returns_zero(self):
        stdout = io.StringIO()
        with patch.object(worker_child.sys, "stdin", io.StringIO('{"task_type":"summarize"}')), \
                patch.object(worker_child.sys, "stdout", stdout), \
                patch.object(worker_child, "invoke", return_value={"output": {"note": "draft"}}):
            status = worker_child.main(["--worker", "worker.py", "--state", "state",
                                        "--queue-root", "queue"])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(stdout.getvalue()), {"output": {"note": "draft"}})

    def test_main_failure_returns_one_and_preserves_fixed_reason(self):
        stdout = io.StringIO()
        with patch.object(worker_child.sys, "stdin", io.StringIO("{}")), \
                patch.object(worker_child.sys, "stdout", stdout), \
                patch.object(worker_child, "invoke", side_effect=ValueError("unsupported queue payload")):
            status = worker_child.main(["--worker", "worker.py", "--state", "state",
                                        "--queue-root", "queue"])
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(stdout.getvalue()),
                         {"ok": False, "error": "ValueError", "error_detail": "unsupported queue payload"})

    def test_main_invalid_json_reports_fixed_reason(self):
        stdout = io.StringIO()
        with patch.object(worker_child.sys, "stdin", io.StringIO("not json")), \
                patch.object(worker_child.sys, "stdout", stdout):
            status = worker_child.main(["--worker", "worker.py", "--state", "state",
                                        "--queue-root", "queue"])
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(stdout.getvalue()),
                         {"ok": False, "error": "ValueError", "error_detail": "payload is not JSON"})


if __name__ == "__main__":
    unittest.main()
