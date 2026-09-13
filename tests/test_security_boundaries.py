"""Focused regressions for caller-supplied state identifiers and peer exits.

These tests create only temporary broker state and fake peer processes.  They
never invoke a real provider or use local credentials.
"""
from __future__ import annotations

import json
import io
import os
from pathlib import Path
import shutil
import tempfile
import unittest
import uuid
from unittest import mock
from contextlib import redirect_stdout

from harness import Sandbox

from agent_bridge import admin, broker, registry, store
from agent_bridge.backends import claude_backend
from agent_bridge.errors import BrokerError, ErrorCategory


class IdentifierBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.external_root = tempfile.mkdtemp(prefix="agent-bridge-external-")

    def tearDown(self) -> None:
        self.sandbox.cleanup()
        shutil.rmtree(self.external_root, ignore_errors=True)

    def assert_schema_invalid(self, call) -> None:
        with self.assertRaises(BrokerError) as caught:
            call()
        self.assertEqual(caught.exception.category, ErrorCategory.INPUT_SCHEMA_INVALID)

    def test_job_id_rejected_before_registry_or_external_read(self) -> None:
        external_job = os.path.join(self.external_root, "job")
        os.makedirs(external_job)
        status_path = os.path.join(external_job, "status.json")
        result_path = os.path.join(external_job, "result.json")
        with open(status_path, "w", encoding="utf-8") as handle:
            json.dump({"status": "complete", "caller": "codex"}, handle)
        with open(result_path, "w", encoding="utf-8") as handle:
            json.dump({"response": "EXTERNAL_SENTINEL"}, handle)
        before = (Path(status_path).read_bytes(), Path(result_path).read_bytes())

        with mock.patch.object(registry, "reconcile", side_effect=AssertionError("registry read")):
            self.assert_schema_invalid(lambda: broker.poll(
                self.sandbox.cfg, "codex", {"job_id": external_job}))
            self.assert_schema_invalid(lambda: broker.read(
                self.sandbox.cfg, "codex", {"job_id": external_job}))

        self.assertEqual(before, (Path(status_path).read_bytes(), Path(result_path).read_bytes()))

    def test_conversation_id_rejected_before_lookup_preflight_or_external_write(self) -> None:
        external_conversation = os.path.join(self.external_root, "conversation")
        path = external_conversation + ".json"
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"caller": "codex", "closed": False, "sentinel": "outside"}, handle)
        before = Path(path).read_bytes()

        with (mock.patch.object(registry, "load_conversation", side_effect=AssertionError("lookup")),
              mock.patch("agent_bridge.broker.preflight.check_peer", side_effect=AssertionError("preflight"))):
            self.assert_schema_invalid(lambda: broker.close(
                self.sandbox.cfg, "codex", {"conversation_id": external_conversation}))
            self.assert_schema_invalid(lambda: broker.continue_(self.sandbox.cfg, "codex", {
                "conversation_id": external_conversation,
                "prompt": "follow up",
                "source_classification": "internal",
            }))

        self.assertEqual(before, Path(path).read_bytes())

    def test_malformed_and_traversal_ids_return_structured_input_errors(self) -> None:
        malformed = [None, 7, [], {}, "../escape", "/tmp/escape", "not-a-uuid"]
        for identifier in malformed:
            with self.subTest(identifier=repr(identifier), operation="poll"):
                self.assert_schema_invalid(lambda: broker.poll(
                    self.sandbox.cfg, "codex", {"job_id": identifier}))
            with self.subTest(identifier=repr(identifier), operation="read"):
                self.assert_schema_invalid(lambda: broker.read(
                    self.sandbox.cfg, "codex", {"job_id": identifier}))
            with self.subTest(identifier=repr(identifier), operation="close"):
                self.assert_schema_invalid(lambda: broker.close(
                    self.sandbox.cfg, "codex", {"conversation_id": identifier}))

    def test_derived_paths_also_reject_noncanonical_ids(self) -> None:
        for identifier in ("../escape", "/tmp/escape", str(uuid.uuid4()).upper(), 4):
            with self.subTest(identifier=repr(identifier), kind="job"):
                self.assert_schema_invalid(lambda: self.sandbox.cfg.job_dir(identifier))
            with self.subTest(identifier=repr(identifier), kind="conversation"):
                self.assert_schema_invalid(lambda: self.sandbox.cfg.conversation_path(identifier))

    def test_symlinked_identifier_roots_cannot_escape_state(self) -> None:
        identifier = str(uuid.uuid4())
        for directory, derive in (
            ("jobs", self.sandbox.cfg.job_dir),
            ("conversations", self.sandbox.cfg.conversation_path),
        ):
            with self.subTest(directory=directory):
                try:
                    os.symlink(
                        self.external_root,
                        os.path.join(self.sandbox.state, directory),
                        target_is_directory=True,
                    )
                except OSError as exc:
                    self.skipTest(f"symlink creation unavailable: {exc}")
                with self.assertRaises(BrokerError) as caught:
                    derive(identifier)
                self.assertEqual(caught.exception.category, ErrorCategory.STATE_ROOT_INSECURE)
                self.assertFalse(os.path.exists(os.path.join(self.external_root, identifier)))

    def test_admin_resolve_returns_a_structured_error_for_an_invalid_id(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            code = admin.main([
                "--config", self.sandbox.config_path,
                "resolve", os.path.join(self.external_root, "conversation"),
            ])
        self.assertEqual(code, 2)
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_category"], ErrorCategory.INPUT_SCHEMA_INVALID.value)

    def test_valid_ids_still_use_state_root_and_enforce_caller_identity(self) -> None:
        job_id = str(uuid.uuid4())
        conversation_id = str(uuid.uuid4())
        self.assertEqual(
            self.sandbox.cfg.job_dir(job_id),
            os.path.join(self.sandbox.state, "jobs", job_id),
        )
        self.assertEqual(
            self.sandbox.cfg.conversation_path(conversation_id),
            os.path.join(self.sandbox.state, "conversations", f"{conversation_id}.json"),
        )
        registry.write_status(
            self.sandbox.cfg, job_id, "complete", caller="claude",
            conversation_id=conversation_id, peer="codex", attempts=1,
        )
        store.atomic_write_json(os.path.join(self.sandbox.cfg.job_dir(job_id), "request.json"), {
            "job_id": job_id,
            "caller": "claude",
        })
        with mock.patch.object(registry, "reconcile", side_effect=AssertionError("foreign reconcile")):
            for operation in (broker.poll, broker.read):
                with self.subTest(operation=operation.__name__):
                    with self.assertRaises(BrokerError) as caught:
                        operation(self.sandbox.cfg, "codex", {"job_id": job_id})
                    self.assertEqual(caught.exception.category, ErrorCategory.JOB_NOT_FOUND)

        own_job_id = str(uuid.uuid4())
        store.atomic_write_json(
            os.path.join(store.secure_mkdir(self.sandbox.cfg.job_dir(own_job_id)), "request.json"),
            {"job_id": own_job_id, "caller": "codex"},
        )
        registry.write_status(
            self.sandbox.cfg, own_job_id, "complete", caller="codex",
            conversation_id=conversation_id, peer="claude", attempts=1,
        )
        self.assertEqual(
            broker.poll(self.sandbox.cfg, "codex", {"job_id": own_job_id})["status"],
            "complete",
        )


class ClaudeExitBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox()
        self.sandbox.env(FAKE_CLAUDE_MODE="success_envelope_nonzero")

    def tearDown(self) -> None:
        self.sandbox.cleanup()

    def test_success_shaped_nonzero_envelope_is_not_accepted(self) -> None:
        session_id = str(uuid.uuid4())
        workspace = store.secure_mkdir(self.sandbox.cfg.workspace("claude", session_id))
        schema, _ = self.sandbox.cfg.load_schema()
        outcome = claude_backend.run_consultation(
            self.sandbox.cfg,
            prompt="Return the response contract.",
            schema=schema,
            session_id=session_id,
            resume=False,
            workspace=workspace,
        )
        self.assertEqual(outcome.returncode, 7)
        self.assertEqual(outcome.category, ErrorCategory.PEER_NONZERO_EXIT)
        self.assertIsNone(outcome.payload)

    def test_worker_does_not_publish_a_nonzero_envelope_as_a_response(self) -> None:
        started, status, response = self.sandbox.run_to_completion("codex")
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["error_category"], ErrorCategory.PEER_NONZERO_EXIT.value)
        self.assertFalse(response["ok"])
        self.assertNotIn("peer_response", response)
        self.assertEqual(response["job_id"], started["job_id"])


if __name__ == "__main__":
    unittest.main()
