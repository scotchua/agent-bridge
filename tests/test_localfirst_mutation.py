"""Offline regression cases found by the local-first mutation sweep."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge import store  # noqa: E402
from agent_bridge.orchestration import autoroute, localfirst  # noqa: E402


class LocalFirstPathMutationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_routing_dir_joins_the_state_root(self):
        self.assertEqual(localfirst.routing_dir(str(self.root)),
                         str(self.root / "routing"))

    def test_relative_posix_path_converts_windows_separators(self):
        with mock.patch.object(localfirst.os, "sep", "\\"), \
             mock.patch.object(localfirst.os.path, "relpath", return_value="logs\\run.log"):
            self.assertEqual(localfirst.relative_posix_path("ignored", "ignored"), "logs/run.log")

    def test_matches_any_accepts_a_file_inside_the_repo(self):
        path = self.root / "logs" / "run.log"
        path.parent.mkdir()
        path.write_text("invented event\n", encoding="utf-8")
        self.assertTrue(localfirst.matches_any(str(path), str(self.root), ("**/*.log",)))

    def test_file_task_for_path_requires_pilot_authorization(self):
        path = self.root / "run.log"
        path.write_text("invented event\n", encoding="utf-8")
        repo = autoroute.RepoPolicy(extract_globs=("**/*.log",))
        config = autoroute.LocalFirstConfig(default_globs=("**/*.log",))
        self.assertEqual(localfirst.file_task_for_path(str(path), str(self.root), repo, config),
                         ("summarize", "work_digest_file", ("**/*.log",)))

    def test_digest_key_has_a_stable_32_character_identity(self):
        path = str(self.root / "invented.log")
        self.assertEqual(len(localfirst.digest_key(path)), 32)
        self.assertEqual(localfirst.digest_key(path), localfirst.digest_key(path))

    def test_digest_intent_path_names_a_json_record(self):
        path = str(self.root / "invented.log")
        self.assertEqual(localfirst.digest_intent_path(str(self.root), path),
                         str(self.root / "routing" / "digest-intents" /
                             (localfirst.digest_key(path) + ".json")))

    def test_digest_receipt_path_names_a_json_record(self):
        path = str(self.root / "invented.log")
        self.assertEqual(localfirst.digest_receipt_path(str(self.root), path),
                         str(self.root / "routing" / "digests" /
                             (localfirst.digest_key(path) + ".json")))

    def test_read_digest_intent_returns_the_saved_record(self):
        path = str(self.root / "invented.log")
        store.atomic_write_json(localfirst.digest_intent_path(str(self.root), path),
                                {"path": path, "size": 17})
        self.assertEqual(localfirst.read_digest_intent(str(self.root), path),
                         {"path": path, "size": 17})

    def test_read_digest_receipt_returns_the_saved_record(self):
        path = str(self.root / "invented.log")
        store.atomic_write_json(localfirst.digest_receipt_path(str(self.root), path),
                                {"path": path, "job_id": "invented-job"})
        self.assertEqual(localfirst.read_digest_receipt(str(self.root), path),
                         {"path": path, "job_id": "invented-job"})

    def test_write_digest_intent_returns_and_saves_the_same_record(self):
        path = str(self.root / "invented.log")
        intent = localfirst.write_digest_intent(
            str(self.root), path=path, repo=str(self.root), size=17, mtime_ns=19,
            matched_glob="**/*.log", classification="synthetic", client="codex",
            readiness_info={"ready": True}, digest_grace_seconds=30, clock=lambda: 100.0)
        self.assertEqual(intent["expires_at"], 130.0)
        self.assertEqual(localfirst.read_digest_intent(str(self.root), path), intent)

    def test_retire_digest_intent_requires_the_complete_binding(self):
        path = str(self.root / "invented.log")
        localfirst.write_digest_intent(
            str(self.root), path=path, repo=str(self.root), size=17, mtime_ns=19,
            matched_glob="**/*.log", classification="synthetic", client="codex",
            readiness_info={}, digest_grace_seconds=30, clock=lambda: 100.0)
        binding = {"path": os.path.realpath(path), "size": 17, "mtime_ns": 19}
        self.assertTrue(localfirst.retire_digest_intent(str(self.root), path,
                                                        binding=binding, clock=lambda: 101.0))
        self.assertIsNone(localfirst.read_digest_intent(str(self.root), path))

    def test_write_digest_receipt_returns_and_saves_the_same_record(self):
        path = str(self.root / "invented.log")
        receipt = localfirst.write_digest_receipt(
            str(self.root), path=path, repo=str(self.root), size=17, mtime_ns=19,
            offset=0, window_bytes=17, window_sha256="a" * 64,
            decode_replacements=0, task_type="summarize", classification="synthetic",
            caller="codex", job_id="invented-job", intake_receipt_id="invented-intake",
            clock=lambda: 100.0)
        self.assertEqual(receipt["job_id"], "invented-job")
        self.assertEqual(localfirst.read_digest_receipt(str(self.root), path), receipt)

    def test_digest_job_index_path_names_a_json_record(self):
        self.assertEqual(localfirst.digest_job_index_path(str(self.root), "invented-job"),
                         str(self.root / "routing" / "digest-jobs" / "invented-job.json"))

    def test_record_digest_job_saves_the_output_limit(self):
        localfirst.record_digest_job(str(self.root), "invented-job", max_output_chars=137)
        self.assertEqual(store.read_json(localfirst.digest_job_index_path(
            str(self.root), "invented-job")), {"max_output_chars": 137})

    def test_digest_job_max_output_chars_accepts_positive_only(self):
        localfirst.record_digest_job(str(self.root), "invented-job", max_output_chars=137)
        self.assertEqual(localfirst.digest_job_max_output_chars(str(self.root), "invented-job"),
                         137)
        localfirst.record_digest_job(str(self.root), "invented-job", max_output_chars=0)
        self.assertIsNone(localfirst.digest_job_max_output_chars(str(self.root), "invented-job"))


class LocalFirstValueMutationTests(unittest.TestCase):
    def test_digest_window_accepts_a_zero_byte_file(self):
        self.assertEqual(localfirst.digest_window(0), (0, 0))

    def test_covering_calibration_accepts_an_eighteen_digit_size_key(self):
        key = "100000000000000000"
        calibration = {"sizes": {key: {"outcomes": ["complete"] * 3, "median_s": 2.5}}}
        self.assertEqual(localfirst._covering_calibration(calibration, 1), (int(key), 2.5))

    def test_build_calibration_record_rejects_a_non_string_backend(self):
        with mock.patch.object(localfirst.store, "sha256_file", return_value="a" * 64):
            with self.assertRaisesRegex(ValueError, "backend_id_invalid"):
                localfirst.build_calibration_record(
                    worker_executable="invented-worker", worker_state="invented-state",
                    sizes={}, sampler_snapshot={}, host={}, backend_id=7)

    def test_write_calibration_record_persists_the_record(self):
        with tempfile.TemporaryDirectory() as root:
            record = {"version": 1, "created_at": 100.0}
            localfirst.write_calibration_record(root, record)
            self.assertEqual(store.read_json(localfirst.calibration_path(root)), record)

    def test_load_calibration_record_returns_the_record(self):
        with tempfile.TemporaryDirectory() as root:
            record = {"version": 1, "created_at": 100.0}
            store.atomic_write_json(localfirst.calibration_path(root), record)
            self.assertEqual(localfirst.load_calibration_record(root), record)

    def test_reparse_tag_preserves_a_nonzero_tag(self):
        self.assertEqual(localfirst._reparse_tag(SimpleNamespace(st_reparse_tag=42)), 42)

    def test_render_instruction_accepts_a_supported_task(self):
        instruction = localfirst.render_instruction("summarize", max_chars=137)
        self.assertIn("137", instruction)
        self.assertIn("Summarize", instruction)


if __name__ == "__main__":
    unittest.main()
