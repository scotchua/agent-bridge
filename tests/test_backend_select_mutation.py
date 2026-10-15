"""Offline boundary tests for local queue backend selection."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.localq import backend_select, worker_child
from agent_bridge.localq.spool import MECHANICAL_TASKS, QueueCaps, SubprocessBackend


def gemma_config(**changes):
    fields = {
        "local_backend": "gemma_certified",
        "gemma_delegate_executable": "delegate-synthetic.py",
        "gemma_python_executable": "python-synthetic",
        "gemma_receipt_root": "receipts-synthetic",
        "gemma_receipt_validator_executable": "validator-synthetic.py",
        "gemma_delegate_sha256": "a" * 64,
        "gemma_receipt_validator_sha256": "b" * 64,
        "gemma_model_digest": "c" * 64,
        "gemma_pilot_tasks": (),
    }
    fields.update(changes)
    return SimpleNamespace(**fields)


class GemmaQueueCapsTests(unittest.TestCase):
    def test_certified_defaults_and_explicit_timeout_keep_exact_queue_headroom(self):
        defaults = backend_select.gemma_queue_caps_for(SimpleNamespace())
        self.assertEqual(defaults.max_input_bytes, 400_000)
        self.assertEqual(defaults.timeout_seconds, 660.0)
        self.assertEqual(defaults.lease_seconds, 720.0)

        configured = backend_select.gemma_queue_caps_for(
            SimpleNamespace(gemma_timeout_seconds=725.5))
        self.assertEqual(configured.max_input_bytes, 400_000)
        self.assertEqual(configured.timeout_seconds, 785.5)
        self.assertEqual(configured.lease_seconds, 845.5)


class BuildBackendAndCapsTests(unittest.TestCase):
    def test_private_worker_defaults_to_exact_child_command_and_caps(self):
        cfg = SimpleNamespace(worker_executable="worker-synthetic.py",
                              worker_state="state-synthetic")
        with tempfile.TemporaryDirectory() as root:
            queue_root = str(Path(root) / "queue")
            backend, caps, allowed = backend_select.build_backend_and_caps(cfg, queue_root)
        self.assertIsInstance(backend, SubprocessBackend)
        self.assertEqual(backend.command,
                         (sys.executable, str(Path(worker_child.__file__).resolve()),
                          "--worker", "worker-synthetic.py", "--state", "state-synthetic",
                          "--queue-root", queue_root))
        self.assertEqual(caps, QueueCaps())
        self.assertEqual(allowed, MECHANICAL_TASKS)

    def test_unknown_backend_reports_unsupported(self):
        with self.assertRaisesRegex(backend_select.BackendSelectionError,
                                    "local_backend_unsupported"):
            backend_select.build_backend_and_caps(
                SimpleNamespace(local_backend="synthetic_unknown"), "queue-synthetic")

    def test_gemma_refuses_non_posix_platform_before_building_command(self):
        with mock.patch.object(backend_select, "os", SimpleNamespace(name="nt")):
            with self.assertRaisesRegex(backend_select.BackendSelectionError,
                                        "gemma_certified_platform_unsupported"):
                backend_select.build_backend_and_caps(gemma_config(), "queue-synthetic")

    @unittest.skipUnless(os.name == "posix", "certified Gemma backend requires POSIX")
    def test_gemma_requires_every_field_on_hand_built_config(self):
        required = ("gemma_delegate_executable", "gemma_python_executable",
                    "gemma_receipt_root", "gemma_receipt_validator_executable",
                    "gemma_delegate_sha256", "gemma_receipt_validator_sha256",
                    "gemma_model_digest")
        for field in required:
            with self.subTest(field=field):
                cfg = gemma_config(**{field: None})
                with self.assertRaisesRegex(backend_select.BackendSelectionError,
                                            "gemma_certified_configuration_incomplete"):
                    backend_select.build_backend_and_caps(cfg, "queue-synthetic")

    @unittest.skipUnless(os.name == "posix", "certified Gemma backend requires POSIX")
    def test_gemma_rejects_unreviewed_pilot_task_on_hand_built_config(self):
        cfg = gemma_config(gemma_pilot_tasks=("synthetic_unreviewed",))
        with self.assertRaisesRegex(backend_select.BackendSelectionError,
                                    "gemma_pilot_tasks_invalid"):
            backend_select.build_backend_and_caps(cfg, "queue-synthetic")
