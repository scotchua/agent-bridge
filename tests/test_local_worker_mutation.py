"""Offline boundary checks for the local worker."""
import io
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import agent_bridge.local_worker as worker


class EndpointTests(unittest.TestCase):
    def test_validate_endpoint_accepts_highest_port(self):
        self.assertEqual(worker.validate_endpoint("http://127.0.0.1:65535"),
                         "http://127.0.0.1:65535")


class ToolSchemaTests(unittest.TestCase):
    def test_schema_refuses_extra_properties(self):
        self.assertIs(worker._schema({}, [])["additionalProperties"], False)

    def test_build_tools_admits_internal_only_when_enabled(self):
        def admitted(enabled):
            return worker.build_tools(enabled)["local_worker_process"]["inputSchema"][
                "properties"]["source_classification"]["enum"]

        self.assertNotIn("internal", admitted(False))
        self.assertIn("internal", admitted(True))


class ServerConstructionTests(unittest.TestCase):
    def test_init_accepts_model_at_byte_limit(self):
        server = worker.LocalWorkerServer("http://127.0.0.1:11434", "a" * worker.MAX_MODEL_BYTES)
        self.assertEqual(len(server.model), worker.MAX_MODEL_BYTES)


class ResponseTests(unittest.TestCase):
    def setUp(self):
        self.server = worker.LocalWorkerServer("http://127.0.0.1:11434", "fictional-model")

    def test_error_preserves_request_id(self):
        result = self.server.handle({"jsonrpc": "2.0", "id": "fictional-7",
                                     "method": "missing"})
        self.assertEqual(result["id"], "fictional-7")

    def test_failure_marks_tool_result_as_error(self):
        result = self.server.call_tool({"name": "missing"})
        self.assertIs(result["isError"], True)


class PostJsonTests(unittest.TestCase):
    def test_post_json_rejects_oversized_response(self):
        server = worker.LocalWorkerServer("http://127.0.0.1:11434", "fictional-model")
        body = json.dumps({"draft": "x" * 1_048_577}).encode()

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def geturl(self):
                return server.endpoint + "/api/show"

            def read(self, count):
                return body[:count]

        class Opener:
            def open(self, request, timeout):
                return Response()

        with patch.object(worker, "build_opener", return_value=Opener()):
            with self.assertRaisesRegex(ValueError, "local response exceeded 1 MiB"):
                server._post_json("/api/show", {"model": "fictional-model"})


class CloudMarkerTests(unittest.TestCase):
    def test_has_cloud_marker_refuses_at_depth_limit(self):
        nested = "ordinary metadata"
        for _ in range(64):
            nested = [nested]
        self.assertTrue(worker.LocalWorkerServer._has_cloud_marker(nested))


class ProcessTests(unittest.TestCase):
    def test_process_accepts_exact_input_byte_limit(self):
        server = worker.LocalWorkerServer("http://127.0.0.1:11434", "fictional-model")
        args = {"task": "summary", "text": "x" * worker.MAX_TEXT_BYTES,
                "instructions": "", "source_classification": "synthetic",
                "purpose": "test", "caller": "codex"}
        with patch.object(server, "_model_preflight", return_value=(True, "checked")):
            with patch.object(server, "_post_json", return_value={
                    "model": "fictional-model", "response": "draft", "done": True}):
                result = server._process(args)
        self.assertTrue(result["structuredContent"]["ok"])


class ServeTests(unittest.TestCase):
    def setUp(self):
        self.server = worker.LocalWorkerServer("http://127.0.0.1:11434", "fictional-model")

    def test_serve_accepts_exact_line_byte_limit(self):
        message = '{"jsonrpc":"2.0","id":21,"method":"ping"}\n'
        line = " " * (worker.MAX_MCP_LINE_BYTES - len(message)) + message
        stdout = io.StringIO()
        self.assertEqual(self.server.serve(io.StringIO(line), stdout), 0)
        self.assertEqual(json.loads(stdout.getvalue())["id"], 21)

    def test_serve_flushes_each_response(self):
        class FlushedOutput(io.StringIO):
            flushes = 0

            def flush(self):
                self.flushes += 1
                super().flush()

        stdout = FlushedOutput()
        self.server.serve(io.StringIO('{"jsonrpc":"2.0","id":21,"method":"ping"}\n'), stdout)
        self.assertEqual(stdout.flushes, 1)


class MainTests(unittest.TestCase):
    def test_main_returns_server_status(self):
        with patch.object(worker, "LocalWorkerServer") as server_class:
            server_class.return_value.serve.return_value = 7
            with patch.object(worker.sys, "stdin", io.StringIO()):
                with patch.object(worker.sys, "stdout", io.StringIO()):
                    self.assertEqual(worker.main(["--model", "fictional-model"]), 7)

    def test_main_reports_invalid_model_as_argument_error(self):
        with patch.object(worker.sys, "stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                worker.main(["--model", "invalid model name"])
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
