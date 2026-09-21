"""Offline tests for the Stage 1b local queue runtime adapters."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.localq.runtime import MacSampler, foundation_thermal_state
from agent_bridge.localq.service import Service
from agent_bridge.localq.spool import ResourceSnapshot
from agent_bridge.localq import worker_child


class RuntimeTests(unittest.TestCase):
    def test_foundation_thermal_probe_returns_supported_state(self):
        self.assertIn(foundation_thermal_state(), {"normal", "high", "unknown"})

    def test_mac_sampler_parses_safe_fixture_and_unknown_thermal_defers(self):
        fixtures = {
            "/usr/bin/memory_pressure": "System-wide memory free percentage: 42%\n",
            "/usr/bin/pmset": "Now drawing from 'AC Power'\n -InternalBattery-0 100%; charging;\n",
            "/usr/sbin/ioreg": '"HIDIdleTime" = 120000000000\n',
            "/usr/bin/uptime": " 10:00 up 1 day, 1 user, load averages: 0.42 0.31 0.22\n",
            "/usr/bin/top": "Processes: 400 total\nCPU usage: 12.5% user, 7.5% sys, 80.0% idle\n",
        }
        sampler = MacSampler(lambda argv: fixtures[argv[0]], thermal_probe=lambda: "normal",
                             cores_probe=lambda: 8, clock=lambda: 100.0)
        snap = sampler.sample()
        self.assertEqual(snap, ResourceSnapshot(100.0, "normal", "normal", True, 120.0, 0.0525, 0.80))
        self.assertEqual(sampler.last_details["load_1"], 0.42)
        self.assertEqual(sampler.last_details["cpu_cores"], 8)
        with self.assertRaises(ValueError):
            MacSampler(lambda _: "bad").sample()

    def test_mac_sampler_tolerates_unparseable_or_failing_top_probe(self):
        fixtures = {
            "/usr/bin/memory_pressure": "System-wide memory free percentage: 42%\n",
            "/usr/bin/pmset": "Now drawing from 'AC Power'\n -InternalBattery-0 100%; charging;\n",
            "/usr/sbin/ioreg": '"HIDIdleTime" = 120000000000\n',
            "/usr/bin/uptime": " 10:00 up 1 day, 1 user, load averages: 0.42 0.31 0.22\n",
            "/usr/bin/top": "unexpected output with no CPU usage line\n",
        }
        sampler = MacSampler(lambda argv: fixtures[argv[0]], thermal_probe=lambda: "normal",
                             cores_probe=lambda: 8, clock=lambda: 100.0)
        snap = sampler.sample()
        self.assertIsNone(snap.cpu_idle_ratio)

        def raising_runner(argv):
            if argv[0] == "/usr/bin/top":
                raise RuntimeError("top unavailable")
            return fixtures[argv[0]]

        sampler = MacSampler(raising_runner, thermal_probe=lambda: "normal",
                             cores_probe=lambda: 8, clock=lambda: 100.0)
        snap = sampler.sample()
        self.assertIsNone(snap.cpu_idle_ratio)

    def test_worker_child_uses_explicit_paths_auto_provider_and_queue_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worker, state = root / "worker.py", root / "state"
            worker.write_text("# fixture", encoding="utf-8")
            state.mkdir()
            import sqlite3
            db = sqlite3.connect(root / "localq.sqlite3")
            db.execute("CREATE TABLE jobs(job_id TEXT,classification TEXT,caller TEXT,purpose TEXT)")
            db.execute("INSERT INTO jobs VALUES('a1','public','codex','work')")
            db.commit(); db.close()
            value = {"status": "draft", "job_id": "b2", "provider": "apple", "output": {"answer": "draft"}}
            wire = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}) + "\n" + json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"isError": False, "content": [{"type": "text", "text": json.dumps(value)}]}}) + "\n"
            class Completed:
                returncode = 0
                stdout = wire
            with patch("agent_bridge.localq.worker_child.subprocess.run", return_value=Completed()) as run:
                output = worker_child.invoke({"job_id": "a1", "task_type": "summarize", "input": "source", "params": {}},
                                             worker=str(worker), state=str(state), queue_root=str(root))
            self.assertEqual(output["provider"], "apple")
            args = run.call_args.args[0]
            self.assertIn("--state", args)
            sent = run.call_args.kwargs["input"].splitlines()[1]
            tool_args = json.loads(sent)["params"]["arguments"]
            self.assertEqual(tool_args["provider"], "auto")
            self.assertEqual(tool_args["classification"], "public")
            self.assertNotIn("API_KEY", " ".join(run.call_args.kwargs["env"]))

    def test_worker_child_reports_its_own_fixed_reason_when_it_fails(self):
        """The exit status alone told the queue nothing; the child now prints
        the fixed diagnostic it raised, and only for the classes it raised."""
        import subprocess
        import sys
        env = {**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(__file__), "..", "src")}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "state").mkdir()
            argv = [sys.executable, "-P", "-m", "agent_bridge.localq.worker_child", "--worker", str(root / "absent.py"),
                    "--state", str(root / "state"), "--queue-root", str(root)]
            completed = subprocess.run(argv, input=b"not json", capture_output=True, env=env, timeout=60)
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(json.loads(completed.stdout),
                             {"ok": False, "error": "ValueError", "error_detail": "payload is not JSON"})
            completed = subprocess.run(argv, input=json.dumps({"job_id": "a1"}).encode(), capture_output=True, env=env, timeout=60)
            self.assertEqual(json.loads(completed.stdout),
                             {"ok": False, "error": "ValueError",
                              "error_detail": "worker path must be an existing absolute regular file"})

    def test_worker_child_forwards_an_explicit_supported_provider(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worker, state = root / "worker.py", root / "state"
            worker.write_text("# fixture", encoding="utf-8")
            state.mkdir()
            import sqlite3
            db = sqlite3.connect(root / "localq.sqlite3")
            db.execute("CREATE TABLE jobs(job_id TEXT,classification TEXT,caller TEXT,purpose TEXT)")
            db.execute("INSERT INTO jobs VALUES('a1','synthetic','codex','test')")
            db.commit(); db.close()
            value = {"status": "draft", "job_id": "b2", "provider": "qwen", "output": {"answer": "draft"}}
            wire = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}) + "\n" + json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"isError": False, "content": [{"type": "text", "text": json.dumps(value)}]}}) + "\n"
            class Completed:
                returncode = 0
                stdout = wire
            with patch("agent_bridge.localq.worker_child.subprocess.run", return_value=Completed()) as run:
                worker_child.invoke({"job_id": "a1", "task_type": "summarize", "input": "source",
                                     "params": {"provider": "qwen"}},
                                    worker=str(worker), state=str(state), queue_root=str(root))
            sent = run.call_args.kwargs["input"].splitlines()[1]
            self.assertEqual(json.loads(sent)["params"]["arguments"]["provider"], "qwen")

            with self.assertRaisesRegex(ValueError, "provider must be"):
                worker_child.invoke({"job_id": "a1", "task_type": "summarize", "input": "source",
                                     "params": {"provider": "remote"}},
                                    worker=str(worker), state=str(state), queue_root=str(root))

    def test_service_once_writes_atomic_restartable_metadata_without_inference(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "spool"
            worker, state = Path(temporary) / "worker.py", Path(temporary) / "worker-state"
            worker.write_text("# fixture", encoding="utf-8"); state.mkdir()
            class Sampler:
                last_details = {"fixture": True}
                def sample(self):
                    return ResourceSnapshot(100.0, "normal", "unknown", True, 100.0)
            service = Service(str(root), str(worker), str(state), sampler=Sampler())
            first = service.once()
            restarted = Service(str(root), str(worker), str(state), sampler=Sampler()).once()
            saved = json.loads((root / "runtime-state.json").read_text())
            self.assertEqual(first["version"], 1)
            self.assertEqual(first["backend_id"], "private_worker")
            self.assertEqual(restarted["queue"]["state"], "local_queue")
            self.assertEqual(saved["sampler"], {"fixture": True})


if __name__ == "__main__":
    unittest.main()
