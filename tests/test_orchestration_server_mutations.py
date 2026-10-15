"""Offline regression tests for orchestration server protocol and worker edges."""

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.orchestration import server as module


def make_server(*, dependency_check=None):
    queue = SimpleNamespace(hold_dependency=mock.Mock())
    service = SimpleNamespace(queue=queue, once=mock.Mock())
    tools = {"sample": {"description": "Synthetic sample", "inputSchema": {},
                        "handler": mock.Mock(return_value={"ok": True, "amount": 42})}}
    with mock.patch.object(module, "AutomaticIntake", return_value=object()), \
            mock.patch.object(module, "build_tools", return_value=tools):
        instance = module.Server("codex", service, object(), interval=0.01,
                                 dependency_check=dependency_check)
    return instance


def request(method, request_id=7, **extra):
    return {"jsonrpc": "2.0", "id": request_id, "method": method, **extra}


class ConstructorMutationTests(unittest.TestCase):
    def test_init_rejects_zero_interval(self):
        with self.assertRaisesRegex(ValueError, "interval_invalid"):
            make_server_with_interval(0)


def make_server_with_interval(interval):
    service = SimpleNamespace(queue=object())
    with mock.patch.object(module, "AutomaticIntake", return_value=object()), \
            mock.patch.object(module, "build_tools", return_value={}):
        return module.Server("codex", service, object(), interval=interval)


class WorkerMutationTests(unittest.TestCase):
    def test_start_clears_previous_stop(self):
        server = make_server()
        server._stop.set()
        with mock.patch.object(server, "_executor"):
            server.start()
            try:
                self.assertFalse(server._stop.is_set())
            finally:
                server.stop()

    def test_executor_holds_dependency_before_work(self):
        server = make_server(dependency_check=lambda: "gemma_validator")
        server._stop = mock.Mock()
        server._stop.is_set.side_effect = [False, True]
        server._executor()
        server.service.queue.hold_dependency.assert_called_once_with("gemma_validator")
        server.service.once.assert_not_called()
        server._stop.wait.assert_called_once_with(server.interval)

    def test_stop_sets_event_without_worker(self):
        server = make_server()
        server.stop()
        self.assertTrue(server._stop.is_set())


class ProtocolMutationTests(unittest.TestCase):
    def setUp(self):
        self.server = make_server()

    def test_error_preserves_request_id_and_code(self):
        self.assertEqual(self.server._error("fictional-7", -32600, "invalid request"), {
            "jsonrpc": "2.0", "id": "fictional-7",
            "error": {"code": -32600, "message": "invalid request"}})

    def test_valid_id_rejects_nonfinite_numbers(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                self.assertFalse(self.server._valid_id(value))

    def test_content_marks_success_and_failure(self):
        self.assertFalse(self.server._content({"ok": True})["isError"])
        self.assertTrue(self.server._content({"ok": False})["isError"])

    def test_handle_rejects_non_object_message(self):
        self.assertEqual(self.server.handle([])["error"]["code"], -32600)

    def test_handle_initialize_includes_capability_and_instructions(self):
        result = self.server.handle(request("initialize"))["result"]
        self.assertEqual(result["capabilities"]["tools"]["listChanged"], False)
        self.assertEqual(result["instructions"], module.INSTRUCTIONS)

    def test_handle_ping_result_is_empty_object(self):
        self.assertEqual(self.server.handle(request("ping"))["result"], {})

    def test_handle_accepts_list_changed_request(self):
        listed = self.server.handle(request("tools/listChanged"))["result"]["tools"]
        self.assertEqual([item["name"] for item in listed], ["sample"])

    def test_handle_rejects_non_object_params(self):
        reply = self.server.handle(request("tools/call", params=[]))
        self.assertEqual(reply["error"]["code"], -32602)

    def test_handle_supplies_empty_arguments(self):
        reply = self.server.handle(request("tools/call", params={"name": "sample"}))
        self.server.tools["sample"]["handler"].assert_called_once_with({})
        self.assertEqual(reply["result"]["structuredContent"]["amount"], 42)

    def test_handle_unknown_method_uses_method_code(self):
        reply = self.server.handle(request("fictional/method"))
        self.assertEqual(reply["error"]["code"], -32601)

    def test_handle_converts_handler_exception_to_error_content(self):
        self.server.tools["sample"]["handler"].side_effect = RuntimeError("synthetic failure")
        reply = self.server.handle(request("tools/call", params={"name": "sample"}))
        self.assertEqual(reply["result"]["structuredContent"],
                         {"ok": False, "error": "internal_error"})
        self.assertTrue(reply["result"]["isError"])


class ServeMutationTests(unittest.TestCase):
    def setUp(self):
        self.server = make_server()
        self.start = mock.patch.object(self.server, "start")
        self.stop = mock.patch.object(self.server, "stop")
        self.start.start()
        self.stop.start()
        self.addCleanup(self.start.stop)
        self.addCleanup(self.stop.stop)

    def test_serve_accepts_exact_frame_limit(self):
        line = json.dumps(request("ping"))
        with mock.patch.object(module, "MAX_FRAME_CHARS", len(line) + 1):
            output = io.StringIO()
            self.assertEqual(self.server.serve(io.StringIO(line + "\n"), output), 0)
        self.assertEqual(json.loads(output.getvalue())["result"], {})

    def test_serve_flushes_each_reply(self):
        class TrackingOutput(io.StringIO):
            def __init__(self):
                super().__init__()
                self.flushes = 0

            def flush(self):
                self.flushes += 1
                super().flush()

        output = TrackingOutput()
        self.server.serve(io.StringIO(json.dumps(request("ping")) + "\n"), output)
        self.assertEqual(output.flushes, 1)

    def test_serve_returns_integer_success_status(self):
        result = self.server.serve(io.StringIO(""), io.StringIO())
        self.assertIs(type(result), int)
        self.assertEqual(result, 0)


class MainMutationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.cfg = SimpleNamespace(state_root=root, capacity_db=root / "capacity.sqlite3",
                                   local_queue_root=root / "local", execution_queue_root=root / "execution",
                                   interval_seconds=1, client_derived_routes={})

    def run_main(self):
        fake_server = mock.Mock()
        fake_server.serve.return_value = 13
        with mock.patch.object(module, "load", return_value=self.cfg), \
                mock.patch.object(module.Service, "for_config", return_value=object()), \
                mock.patch.object(module, "StageRouter", return_value=object()), \
                mock.patch.object(module, "ExecutionQueue", return_value=object()) as execution, \
                mock.patch.object(module.autoroute, "model_reserved_for", return_value={}), \
                mock.patch.object(module.gate, "protected_paths", return_value=()), \
                mock.patch.object(module, "Server", return_value=fake_server) as constructor:
            result = module.main(["--caller", "codex", "--config", str(Path(self.temp.name) / "config.json")])
        return result, execution, constructor, fake_server

    def test_main_constructs_execution_queue_when_configured(self):
        _, execution, _, _ = self.run_main()
        execution.assert_called_once()
        self.assertEqual(execution.call_args.args[0], self.cfg.execution_queue_root)

    def test_main_returns_server_status(self):
        result, _, constructor, fake_server = self.run_main()
        constructor.assert_called_once()
        fake_server.serve.assert_called_once_with()
        self.assertEqual(result, 13)

    def test_dependency_check_reports_changed_local_component(self):
        _, _, constructor, _ = self.run_main()
        check = constructor.call_args.kwargs["dependency_check"]
        from agent_bridge import drift
        with mock.patch.object(drift, "quick_check", return_value={"changed": ["gemma_validator"]}), \
                mock.patch.object(drift, "report_attention"):
            self.assertEqual(check(), "gemma_validator")


if __name__ == "__main__":
    unittest.main()
