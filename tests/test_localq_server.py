"""Protocol tests for the standalone local queue MCP transport."""

import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.localq import server as server_module
from agent_bridge.localq.server import INSTRUCTIONS, MAX_FRAME_CHARS, Server
from agent_bridge.localq.spool import FakeBackend, LocalQueue, ResourceSnapshot


class Sampler:
    def sample(self):
        return ResourceSnapshot(100.0, "normal", "normal", True, 120.0)


class Service:
    def __init__(self, root):
        self.queue = LocalQueue(root, sampler=Sampler(), backend=FakeBackend(), clock=lambda: 100.0)
        self.calls = 0

    def once(self):
        self.calls += 1
        return self.queue.run_once("test-server")


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.service = Service(self.temporary.name)
        self.server = Server(self.service, interval=0.01)

    def tearDown(self):
        self.server.stop()
        # stop() waits only interval + 1 seconds. On a slow Windows runner the
        # executor can still be inside a queue call holding localq.sqlite3, and
        # Windows refuses to delete an open file. Wait for it, then retry the
        # cleanup briefly rather than failing on a sharing violation.
        for thread in threading.enumerate():
            if thread.name == "localq-executor":
                thread.join(timeout=30)
        for attempt in range(50):
            try:
                self.temporary.cleanup()
                return
            except PermissionError:
                if attempt == 49:
                    raise
                time.sleep(0.1)

    def request(self, method, params=None, request_id=1):
        value = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            value["params"] = params
        return self.server.handle(value)

    def test_initialize_and_tools_are_local_only(self):
        init = self.request("initialize")["result"]
        self.assertIn("Local mechanical", init["instructions"])
        self.assertEqual(init["instructions"], INSTRUCTIONS)
        tools = self.request("tools/list")["result"]["tools"]
        self.assertEqual({tool["name"] for tool in tools}, {"local_submit", "local_status", "local_result", "local_cancel", "local_feedback", "local_route", "local_route_receipt"})

    def test_submit_status_and_stdio_lifecycle(self):
        args = {"task_type": "summarize", "input": "fixture", "params": {}, "priority": "interactive",
                "classification": "synthetic", "caller": "codex", "purpose": "test"}
        submitted = self.request("tools/call", {"name": "local_submit", "arguments": args})["result"]["structuredContent"]
        self.assertTrue(submitted["ok"])
        status = self.request("tools/call", {"name": "local_status", "arguments": {"job_id": submitted["job_id"]}})
        self.assertEqual(status["result"]["structuredContent"]["status"], "queued")
        stdin = io.StringIO('{"jsonrpc":"2.0","id":7,"method":"ping"}\n')
        stdout = io.StringIO()
        self.assertEqual(self.server.serve(stdin, stdout), 0)
        self.assertEqual(json.loads(stdout.getvalue())["id"], 7)
        self.assertIsNone(self.server._thread)
        self.assertGreaterEqual(self.service.calls, 1)

    def test_bad_requests_do_not_crash_server(self):
        self.assertEqual(self.server.handle([])["error"]["code"], -32600)
        self.assertEqual(self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "nope"})["error"]["code"], -32601)
        wire = io.StringIO('not json\n')
        out = io.StringIO()
        self.server.serve(wire, out)
        self.assertEqual(json.loads(out.getvalue())["error"]["code"], -32700)

    def test_oversized_frame_is_refused_before_intake_and_closes_transport(self):
        output = io.StringIO()
        status = self.server.serve(io.StringIO("x" * (MAX_FRAME_CHARS + 1)), output)
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(output.getvalue()), {
            "jsonrpc": "2.0", "id": None,
            "error": {"code": -32600, "message": "request exceeds frame limit"},
        })

    def test_throwing_handler_returns_fixed_error_and_transport_serves_next_request(self):
        self.server.tools["synthetic_throw"] = {
            "description": "synthetic throwing handler",
            "inputSchema": {"type": "object"},
            "handler": mock.Mock(side_effect=RuntimeError("must not be exposed")),
        }
        wire = io.StringIO(
            '{"jsonrpc":"2.0","id":1,"method":"tools/call",'
            '"params":{"name":"synthetic_throw","arguments":{}}}\n'
            '{"jsonrpc":"2.0","id":2,"method":"ping"}\n')
        output = io.StringIO()
        with mock.patch.object(server_module._LOG, "error") as error_log:
            self.assertEqual(self.server.serve(wire, output), 0)
        failed, ping = (json.loads(line) for line in output.getvalue().splitlines())
        self.assertEqual(failed["id"], 1)
        self.assertEqual(failed["result"]["structuredContent"],
                         {"ok": False, "error": "internal_error"})
        self.assertTrue(failed["result"]["isError"])
        self.assertNotIn("must not be exposed", output.getvalue())
        self.assertEqual(ping, {"jsonrpc": "2.0", "id": 2, "result": {}})
        error_log.assert_called_once_with("localq_transport_handler_error")

    def test_init_rejects_nonpositive_interval_and_keeps_default(self):
        self.assertEqual(Server(self.service).interval, 5.0)
        for interval in (0, -0.25):
            with self.subTest(interval=interval), self.assertRaisesRegex(ValueError, "interval must be positive"):
                Server(self.service, interval=interval)

    def test_executor_waits_after_each_poll(self):
        server = object.__new__(Server)
        server.service = mock.Mock()
        server.interval = 0.25
        server._stop = mock.Mock()
        server._stop.is_set.side_effect = (False, True)
        server._executor()
        server.service.once.assert_called_once_with()
        server._stop.wait.assert_called_once_with(0.25)

    def test_stop_signals_and_joins_with_interval_grace(self):
        server = object.__new__(Server)
        server.interval = 0.25
        server._stop = mock.Mock()
        thread = mock.Mock()
        server._thread = thread
        server.stop()
        server._stop.set.assert_called_once_with()
        thread.join.assert_called_once_with(timeout=1.25)
        self.assertIsNone(server._thread)

    def test_valid_id_rejects_boolean_and_nonfinite_numbers(self):
        for value in (True, False, float("nan"), float("inf"), -float("inf"), [], {}):
            with self.subTest(value=value):
                self.assertFalse(Server._valid_id(value))
        for value in ("synthetic-request", 0, -2, 1.5):
            with self.subTest(value=value):
                self.assertTrue(Server._valid_id(value))

    def test_handle_preserves_ids_and_distinguishes_invalid_tool_inputs(self):
        for value in (True, float("nan"), []):
            reply = self.server.handle({"jsonrpc": "2.0", "id": value, "method": "ping"})
            self.assertEqual(reply, {"jsonrpc": "2.0", "id": None,
                                     "error": {"code": -32600, "message": "invalid request"}})
        cases = ((None, "invalid params"), ({}, "invalid params"),
                 ({"name": "missing_tool"}, "unknown tool"),
                 ({"name": "local_status", "arguments": []}, "invalid params"))
        for params, message in cases:
            with self.subTest(params=params):
                reply = self.server.handle({"jsonrpc": "2.0", "id": "synthetic-request",
                                            "method": "tools/call", "params": params})
                self.assertEqual(reply, {"jsonrpc": "2.0", "id": "synthetic-request",
                                         "error": {"code": -32602, "message": message}})

    def test_serve_uses_default_streams_and_flushes_each_reply(self):
        class Output(io.StringIO):
            def __init__(self):
                super().__init__()
                self.flushes = 0

            def flush(self):
                self.flushes += 1
                super().flush()

        output = Output()
        source = io.StringIO('{"jsonrpc":"2.0","id":7,"method":"ping"}\n')
        with mock.patch.object(server_module.sys, "stdin", source), \
                mock.patch.object(server_module.sys, "stdout", output), \
                mock.patch.object(self.server, "start"), mock.patch.object(self.server, "stop"):
            result = self.server.serve()
        self.assertIs(type(result), int)
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["id"], 7)
        self.assertEqual(output.flushes, 1)

    def test_main_passes_absolute_paths_and_interval_to_service(self):
        root = os.path.join(self.temporary.name, "queue")
        worker = os.path.join(self.temporary.name, "synthetic-worker")
        state = os.path.join(self.temporary.name, "synthetic-state")
        argv = ["--root", root, "--worker", worker, "--worker-state", state]
        with mock.patch.object(server_module, "Service") as service, \
                mock.patch.object(server_module, "Server") as server:
            server.return_value.serve.return_value = 17
            self.assertEqual(server_module.main(argv), 17)
            service.assert_called_once_with(root, worker, state)
            server.assert_called_once_with(service.return_value, interval=5.0)
            server.reset_mock()
            self.assertEqual(server_module.main(argv + ["--interval", "0.75"]), 17)
            server.assert_called_once_with(service.return_value, interval=0.75)

    def test_main_requires_each_path_argument(self):
        root = os.path.join(self.temporary.name, "queue")
        worker = os.path.join(self.temporary.name, "synthetic-worker")
        state = os.path.join(self.temporary.name, "synthetic-state")
        parts = (("--root", root), ("--worker", worker), ("--worker-state", state))
        with mock.patch.object(server_module, "Service"), mock.patch.object(server_module, "Server"):
            for omitted in range(len(parts)):
                argv = [item for index, pair in enumerate(parts) if index != omitted for item in pair]
                with self.subTest(omitted=parts[omitted][0]), \
                        mock.patch.object(server_module.sys, "stderr", io.StringIO()), \
                        self.assertRaises(SystemExit):
                    server_module.main(argv)

    def test_main_rejects_relative_paths_before_service_creation(self):
        root = os.path.join(self.temporary.name, "queue")
        worker = os.path.join(self.temporary.name, "synthetic-worker")
        state = os.path.join(self.temporary.name, "synthetic-state")
        with mock.patch.object(server_module, "Service") as service, \
                mock.patch.object(server_module, "Server"), \
                mock.patch.object(server_module.sys, "stderr", io.StringIO()):
            for argv in (["--root", "relative-queue", "--worker", worker, "--worker-state", state],
                         ["--root", root, "--worker", "relative-worker", "--worker-state", state],
                         ["--root", root, "--worker", worker, "--worker-state", "relative-state"]):
                with self.subTest(argv=argv), self.assertRaises(SystemExit):
                    server_module.main(argv)
            service.assert_not_called()


if __name__ == "__main__":
    unittest.main()
