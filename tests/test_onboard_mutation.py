"""Focused offline contracts exposed by the onboard mutation sweep."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import operator_isolation  # noqa: E402,F401
from agent_bridge import config, onboard, setup_cmd, store  # noqa: E402
from test_onboard import active_in, answers  # noqa: E402


class AnswersMutationTests(unittest.TestCase):
    def test_default_answers_path_uses_supplied_home(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(onboard.default_answers_path(home),
                             os.path.join(home, ".agent-bridge", "onboarding", "answers.json"))

    def test_sha_is_canonical_sha256(self):
        value = {"company": "Invented Cedar LLC", "amount": 17}
        expected = hashlib.sha256(b'{"amount":17,"company":"Invented Cedar LLC"}').hexdigest()
        self.assertEqual(onboard._sha(value), expected)

    def test_validate_answers_rejects_wrong_and_missing_version(self):
        for value in (None, 2):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "version 1"):
                onboard.validate_answers(answers(version=value))

    def test_questionnaire_returns_validated_choices(self):
        replies = iter(("both", "baseline", "yes", "yes", "no", "n", "no"))
        with mock.patch.object(onboard, "delegation_platform_blocker", return_value="unavailable"):
            with contextlib.redirect_stdout(io.StringIO()):
                result = onboard.questionnaire(lambda _: next(replies))
        self.assertEqual(result["directions"], "both")
        self.assertEqual(result["targets"], answers()["targets"])

    def test_load_answers_returns_validated_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "answers.json")
            store.atomic_write_json(path, answers())
            self.assertEqual(onboard.load_answers(path), onboard.validate_answers(answers()))


class CommitMutationTests(unittest.TestCase):
    def test_backup_returns_a_copy_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "invented-ledger.txt")
            Path(path).write_bytes(b"Invented Cedar LLC: 17\n")
            backup = onboard._backup(path)
            self.assertTrue(backup.startswith(path + ".agent-bridge."))
            self.assertEqual(Path(backup).read_bytes(), Path(path).read_bytes())

    def test_commit_returns_backup_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "invented-ledger.txt")
            Path(path).write_bytes(b"old")
            backups = onboard._commit_updates({path: b"new"}, {path: b"old"})
            self.assertEqual(len(backups), 1)
            self.assertEqual(Path(backups[0]).read_bytes(), b"old")

    def test_concurrent_edit_creates_no_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "invented-ledger.txt")
            Path(path).write_bytes(b"host edit")
            with self.assertRaisesRegex(ValueError, "changed during planning"):
                onboard._commit_updates({path: b"installer"}, {path: b"old"})
            self.assertEqual(list(Path(tmp).glob("*.bak")), [])


class ManagedMarkerMutationTests(unittest.TestCase):
    def test_toml_rejects_orphan_end_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.toml")
            command = {"command": "python", "args": []}
            text = onboard.END.format(name="claude-peer") + "\n"
            with self.assertRaisesRegex(ValueError, "damaged"):
                onboard._toml_registration_update(path, "claude-peer", command, text)

    def test_toml_rejects_duplicate_marker_pairs_as_damage(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.toml")
            command = {"command": "python", "args": []}
            pair = (onboard.BEGIN.format(name="claude-peer") + "\n"
                    + onboard.END.format(name="claude-peer") + "\n")
            with self.assertRaisesRegex(ValueError, "damaged"):
                onboard._toml_registration_update(path, "claude-peer", command, pair * 2)

    def test_managed_text_rejects_orphan_end_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "AGENTS.md")
            text = "<!-- " + onboard.END.format(name="instructions") + " -->\n"
            Path(path).write_text(text)
            with self.assertRaisesRegex(ValueError, "damaged"):
                onboard._managed_text_update(path, "instructions", "Read invented rules.")

    def test_managed_text_rejects_duplicate_marker_pairs_as_damage(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "AGENTS.md")
            pair = ("<!-- " + onboard.BEGIN.format(name="instructions") + " -->\n"
                    "<!-- " + onboard.END.format(name="instructions") + " -->\n")
            Path(path).write_text(pair * 2)
            with self.assertRaisesRegex(ValueError, "damaged"):
                onboard._managed_text_update(path, "instructions", "Read invented rules.")

    def test_remove_managed_text_returns_new_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "AGENTS.md")
            body = "Read invented rules.\n"
            Path(path).write_bytes(onboard._managed_text_update(path, "instructions", body))
            self.assertEqual(onboard._remove_managed_text(path, "instructions", body), b"\n")


class PathAndApplyMutationTests(unittest.TestCase):
    def test_same_directory_returns_boolean_for_equal_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIs(onboard._same_directory(tmp, os.path.join(tmp, ".")), True)

    def test_unmanaged_command_returns_false(self):
        self.assertIs(onboard._looks_managed_command("invented command"), False)

    def test_apply_rejects_other_checkout_before_reading_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(onboard.store, "read_json") as read:
                with self.assertRaisesRegex(ValueError, "checkout"):
                    onboard._apply(answers(), "candidate", "results", tmp, home=tmp)
                read.assert_not_called()

    def test_apply_rejects_mismatched_answers_before_candidate_inspection(self):
        with mock.patch.object(onboard.store, "read_json", return_value={"answers_sha256": "wrong"}), \
             mock.patch.object(onboard.config, "load_effective", return_value=None):
            with self.assertRaisesRegex(ValueError, "answers differ"):
                onboard._apply(answers(), "candidate", "results", str(ROOT))

    def test_apply_canonicalizes_checkout_root_for_registrations(self):
        with tempfile.TemporaryDirectory() as tmp:
            choices = onboard.validate_answers(answers())
            candidate = os.path.join(tmp, "candidate.json")
            results = os.path.join(tmp, "results.json")
            active = active_in(tmp)
            staged = setup_cmd.write_candidate(candidate, onboard.privacy_overlay(choices), active)
            store.atomic_write_json(candidate + ".onboarding-plan.json", {
                "answers_sha256": onboard._sha(choices),
                "effective_config_sha256": config.effective_config_sha256(staged.raw),
                "pins": {peer: {key: staged.peer(peer).get(key)
                                for key in ("executable", "allowed_versions")}
                         for peer in ("claude", "codex")}})
            store.atomic_write_json(results, {"verdict": "PASS"})
            home = os.path.join(tmp, "home")
            spelling = str(ROOT) + os.sep + "."
            with mock.patch.object(setup_cmd, "validate_promotion"), \
                 mock.patch.object(setup_cmd, "promote_candidate", return_value={}), \
                 mock.patch.object(setup_cmd, "is_durable", return_value=True), \
                 mock.patch.object(config, "local_config_path", return_value=os.path.join(tmp, "local.json")), \
                 contextlib.redirect_stdout(io.StringIO()):
                onboard.apply(choices, candidate, results, spelling, home=home)
            command = store.read_json(os.path.join(home, ".claude.json"))["mcpServers"]["codex-peer"]
            self.assertEqual(command["args"][0], str(ROOT / "setup_bridge.py"))

    def test_apply_lock_uses_explicit_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(onboard.store, "file_lock") as lock, \
                 mock.patch.object(onboard, "_apply"):
                onboard.apply(answers(), "candidate", "results", str(ROOT), home=tmp)
            self.assertEqual(lock.call_args.args[0],
                             os.path.join(tmp, ".agent-bridge", "onboarding", "install.lock"))


class GateAndUninstallMutationTests(unittest.TestCase):
    def test_install_gate_returns_failure_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(onboard.delegation_gate, "install", side_effect=ValueError("invented failure")):
                report = onboard._install_gate(tmp, str(ROOT), os.path.join(tmp, "config.json"), None)
            self.assertEqual(report["install_error"], "ValueError: invented failure")

    def test_install_gate_handles_non_object_existing_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = onboard.autoroute.policy_path(tmp)
            store.atomic_write_json(policy, [])
            cfg = {"state_root": tmp}
            with mock.patch.object(onboard.delegation_gate, "install", return_value={}):
                report = onboard._install_gate(tmp, str(ROOT), os.path.join(tmp, "config.json"), cfg)
            # The operator settings file carries the route choice now, so the
            # installer no longer asks the operator to edit the policy by hand
            # and never rewrites an existing policy document.
            self.assertNotIn("client_derived_policy_step", report)
            self.assertIs(report["policy_created"], False)
            self.assertEqual(store.read_json(policy), [])

    def test_uninstall_defaults_to_isolated_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_expanduser = os.path.expanduser
            def expanduser(path):
                return tmp if path == "~" else original_expanduser(path)
            with mock.patch.object(onboard.os.path, "expanduser", side_effect=expanduser), \
                 mock.patch.object(config, "local_config_path", return_value=os.path.join(tmp, "local.json")):
                report = onboard.uninstall(onboard.validate_answers(answers()), str(ROOT))
            self.assertIn(os.path.join(tmp, ".agent-bridge", "onboarding", "installation.json"),
                          report["retained_files"])

    def test_main_reports_missing_answers_as_failure(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(io.StringIO()):
            result = onboard.main(["plan", "--answers", os.path.join(tmp, "absent.json")])
        self.assertEqual(result, 1)
