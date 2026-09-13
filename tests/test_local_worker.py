"""Synthetic localhost tests for the optional Ollama MCP worker."""
import io
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from agent_bridge.local_worker import (MAX_MCP_LINE_BYTES, MAX_OUTPUT_CHARS,
                                       LocalWorkerServer, validate_endpoint)


class _Ollama(BaseHTTPRequestHandler):
    received = None
    show_response = {"details": {"format": "gguf", "family": "synthetic"}}
    generate_response = {"model": "actual-model", "response": "local draft", "done": True}
    redirect_show = False

    def do_POST(self):
        size = int(self.headers["Content-Length"])
        request = json.loads(self.rfile.read(size))
        if self.path == "/api/show" and type(self).redirect_show:
            self.send_response(302)
            self.send_header("Location", "/redirected")
            self.end_headers()
            return
        if self.path == "/api/show":
            body = json.dumps(type(self).show_response).encode()
        else:
            type(self).received = (self.path, request)
            body = json.dumps(type(self).generate_response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class LocalWorkerTests(unittest.TestCase):
    def setUp(self):
        _Ollama.received = None
        _Ollama.show_response = {"details": {"format": "gguf", "family": "synthetic"}}
        _Ollama.generate_response = {"model": "actual-model", "response": "local draft", "done": True}
        _Ollama.redirect_show = False
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), _Ollama)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = "http://127.0.0.1:%s" % self.http.server_port

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()

    def call(self, server, name, arguments):
        return server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": name, "arguments": arguments}})["result"]

    def test_endpoint_refuses_remote_and_decorated_urls(self):
        for value in ("https://127.0.0.1:11434", "http://example.com:11434",
                      "http://127.0.0.1:11434/path", "http://a@127.0.0.1:11434",
                      "http://127.0.0.1:11434?x=1"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_endpoint(value)
        self.assertEqual(validate_endpoint("http://localhost:11434"), "http://127.0.0.1:11434")

    def test_info_checks_local_metadata_without_sending_source_text(self):
        result = self.call(LocalWorkerServer(self.endpoint, "chosen"), "local_worker_info", {})
        self.assertTrue(result["structuredContent"]["ok"])
        self.assertEqual(result["structuredContent"]["model_admission"], "metadata_checked")
        self.assertIsNone(_Ollama.received)

    def test_redirect_and_unknown_model_metadata_are_refused(self):
        _Ollama.redirect_show = True
        result = self.call(LocalWorkerServer(self.endpoint, "chosen"), "local_worker_info", {})
        self.assertEqual(result["structuredContent"]["model_admission"], "refused")
        _Ollama.redirect_show = False
        args = {"task": "summary", "text": "public text", "instructions": "brief",
                "source_classification": "public", "purpose": "test", "caller": "codex"}
        for metadata in ({}, {"details": {"format": "safetensors"}}):
            _Ollama.show_response = metadata
            result = self.call(LocalWorkerServer(self.endpoint, "chosen"), "local_worker_process", args)
            self.assertEqual(result["structuredContent"]["error"], "local_model_refused")
            self.assertIsNone(_Ollama.received)

    def test_process_posts_only_bounded_ollama_request(self):
        server = LocalWorkerServer(self.endpoint, "chosen")
        args = {"task": "summary", "text": "synthetic source", "instructions": "One sentence.",
                "source_classification": "synthetic", "purpose": "test", "caller": "codex"}
        result = self.call(server, "local_worker_process", args)["structuredContent"]
        self.assertTrue(result["ok"])
        self.assertEqual(result["output"], "local draft")
        self.assertEqual(result["metadata"]["actual_model"], "actual-model")
        self.assertEqual(_Ollama.received[0], "/api/generate")
        self.assertEqual(_Ollama.received[1]["model"], "chosen")
        self.assertFalse(_Ollama.received[1]["stream"])
        _Ollama.generate_response = {"response": "local draft", "done": True}
        result = self.call(server, "local_worker_process", args)["structuredContent"]
        self.assertEqual(result["metadata"]["actual_model"], "unknown")

    def test_proxy_environment_cannot_reroute_loopback_request(self):
        args = {"task": "summary", "text": "public text", "instructions": "brief",
                "source_classification": "public", "purpose": "test", "caller": "codex"}
        with patch.dict(os.environ, {"HTTP_PROXY": "http://example.invalid:9",
                                     "http_proxy": "http://example.invalid:9"}):
            result = self.call(LocalWorkerServer(self.endpoint, "chosen"), "local_worker_process", args)
        self.assertTrue(result["structuredContent"]["ok"])
        self.assertEqual(_Ollama.received[0], "/api/generate")

    def test_output_is_capped_and_surrogates_are_json_safe(self):
        args = {"task": "summary", "text": "public text", "instructions": "brief",
                "source_classification": "public", "purpose": "test", "caller": "codex"}
        _Ollama.generate_response = {"model": "actual-model", "response": "x" * (MAX_OUTPUT_CHARS + 1), "done": True}
        result = self.call(LocalWorkerServer(self.endpoint, "chosen"), "local_worker_process", args)
        payload = result["structuredContent"]
        self.assertEqual(len(payload["output"]), MAX_OUTPUT_CHARS)
        self.assertTrue(payload["metadata"]["output_truncated"])
        self.assertEqual(payload["metadata"]["output_char_count"], MAX_OUTPUT_CHARS + 1)
        _Ollama.generate_response = {"model": "actual-model", "response": "draft\ud800", "done": True}
        result = self.call(LocalWorkerServer(self.endpoint, "chosen"), "local_worker_process", args)
        self.assertIn("\\ud800", result["content"][0]["text"])
        self.assertEqual(json.loads(result["content"][0]["text"])["output"], "draft\ud800")

    def test_surrogate_and_astral_output_cross_real_utf8_stdio(self):
        args = {"task": "summary", "text": "public text", "instructions": "brief",
                "source_classification": "public", "purpose": "test", "caller": "codex"}
        request = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                              "params": {"name": "local_worker_process", "arguments": args}}) + "\n"
        for output in ("draft\ud800", "\U0001f680" * (MAX_OUTPUT_CHARS + 1)):
            _Ollama.generate_response = {"model": "actual-model", "response": output, "done": True}
            buffer = io.BytesIO()
            with io.TextIOWrapper(buffer, encoding="utf-8", errors="strict", newline="\n") as stdout:
                server = LocalWorkerServer(self.endpoint, "chosen")
                self.assertEqual(server.serve(io.StringIO(request), stdout), 0)
                stdout.flush()
                wire = buffer.getvalue()
            self.assertLess(len(wire), MAX_MCP_LINE_BYTES)
            decoded = json.loads(wire.decode("utf-8"))
            self.assertEqual(decoded["result"]["structuredContent"]["output"], output[:MAX_OUTPUT_CHARS])

    def test_deep_metadata_is_refused_before_source_dispatch(self):
        nested = "leaf"
        for _ in range(100):
            nested = [nested]
        _Ollama.show_response = {"details": {"format": "gguf"}, "nested": nested}
        args = {"task": "summary", "text": "public text", "instructions": "brief",
                "source_classification": "public", "purpose": "test", "caller": "codex"}
        result = self.call(LocalWorkerServer(self.endpoint, "chosen"), "local_worker_process", args)
        self.assertEqual(result["structuredContent"]["error"], "local_model_refused")
        self.assertIsNone(_Ollama.received)

    def test_classification_and_size_are_refused_before_http(self):
        base = {"task": "extract", "text": "x", "instructions": "fields",
                "source_classification": "internal", "purpose": "work", "caller": "claude"}
        result = self.call(LocalWorkerServer(self.endpoint, "chosen"), "local_worker_process", base)
        self.assertEqual(result["structuredContent"]["error"], "classification_refused")
        base["source_classification"] = "public"; base["text"] = "x" * 24001
        result = self.call(LocalWorkerServer(self.endpoint, "chosen"), "local_worker_process", base)
        self.assertEqual(result["structuredContent"]["error"], "input_too_large")
        self.assertIsNone(_Ollama.received)

    def test_cloud_model_is_refused_before_prompt_generation(self):
        args = {"task": "summary", "text": "must not leave this process", "instructions": "brief",
                "source_classification": "public", "purpose": "test", "caller": "codex"}
        result = self.call(LocalWorkerServer(self.endpoint, "gpt-oss:120b-cloud"),
                           "local_worker_process", args)["structuredContent"]
        self.assertEqual(result["error"], "local_model_refused")
        self.assertIsNone(_Ollama.received)

    def test_cloud_words_in_template_are_allowed_but_remote_metadata_refuses(self):
        _Ollama.show_response = {"details": {"format": "gguf"}, "template": "cloudy day"}
        args = {"task": "summary", "text": "public text", "instructions": "brief",
                "source_classification": "public", "purpose": "test", "caller": "codex"}
        result = self.call(LocalWorkerServer(self.endpoint, "chosen"), "local_worker_process", args)
        self.assertTrue(result["structuredContent"]["ok"])
        _Ollama.received = None
        _Ollama.show_response = {"details": {"format": "gguf"}, "remote_host": "example.invalid"}
        result = self.call(LocalWorkerServer(self.endpoint, "chosen"), "local_worker_process", args)
        self.assertEqual(result["structuredContent"]["error"], "local_model_refused")
        self.assertIsNone(_Ollama.received)

    def test_incomplete_response_and_invalid_utf8_are_refused(self):
        args = {"task": "summary", "text": "bad\ud800", "instructions": "brief",
                "source_classification": "public", "purpose": "test", "caller": "codex"}
        result = self.call(LocalWorkerServer(self.endpoint, "chosen"), "local_worker_process", args)
        self.assertEqual(result["structuredContent"]["error"], "input_invalid")
        self.assertIsNone(_Ollama.received)
        args["text"] = "public text"
        _Ollama.generate_response = {"model": "actual-model", "response": "partial", "done": False}
        result = self.call(LocalWorkerServer(self.endpoint, "chosen"), "local_worker_process", args)
        self.assertEqual(result["structuredContent"]["error"], "local_model_error")

    def test_mcp_notifications_are_silent_and_stdio_is_json_only(self):
        server = LocalWorkerServer(self.endpoint, "chosen")
        self.assertIsNone(server.handle({"jsonrpc": "2.0", "method": "ping"}))
        stdin = io.StringIO('{"jsonrpc":"2.0","id":1,"method":"ping"}\n')
        stdout = io.StringIO()
        self.assertEqual(server.serve(stdin, stdout), 0)
        self.assertEqual(json.loads(stdout.getvalue())["id"], 1)
        invalid = server.handle({"jsonrpc": "2.0", "id": 2})
        self.assertEqual(invalid["error"]["code"], -32600)

    def test_oversized_nonnewline_request_is_drained_once(self):
        server = LocalWorkerServer(self.endpoint, "chosen")
        stdin = io.StringIO("x" * (MAX_MCP_LINE_BYTES + 1) + "\n" +
                            '{"jsonrpc":"2.0","id":3,"method":"ping"}\n')
        stdout = io.StringIO()
        server.serve(stdin, stdout)
        lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["error"]["code"], -32600)
        self.assertEqual(lines[1]["id"], 3)


if __name__ == "__main__":
    unittest.main()
