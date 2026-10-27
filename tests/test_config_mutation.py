"""Offline regression checks for the broker configuration helpers."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import operator_isolation  # noqa: E402,F401
from agent_bridge import config  # noqa: E402
from agent_bridge.errors import BrokerError, ErrorCategory  # noqa: E402


def synthetic_raw(root: str) -> dict:
    return {
        "config_version": "1", "contract_version": "7",
        "state_root": root, "schema_path": "schema/synthetic.json",
        "limits": {"prompt_max_chars": 100, "prompt_start_max_chars": 60},
        "retention": {"jobs_days": 17},
        "peers": {"claude": {}, "codex": {}},
    }


class ConfigMutationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="config-mutation-")
        self.addCleanup(self.tmp.cleanup)
        self.raw = synthetic_raw(self.tmp.name)
        self.cfg = config.Config(self.raw, os.path.join(self.tmp.name, "synthetic.json"))

    def write_json(self, name, value):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(value, handle)
        return path

    def test_canonical_uuid_rejects_noncanonical_identifiers(self):
        identifier = "123e4567-e89b-42d3-a456-426614174000"
        self.assertEqual(config.canonical_uuid(identifier), identifier)
        for invalid in (None, 42, identifier.upper(), "../synthetic-account"):
            with self.subTest(invalid=invalid), self.assertRaises(BrokerError) as caught:
                config.canonical_uuid(invalid)
            self.assertEqual(caught.exception.category, ErrorCategory.INPUT_SCHEMA_INVALID)

    def test_deep_merge_preserves_inputs_and_nested_defaults(self):
        base = {"account": {"name": "Juniper Works", "cap": 12}, "enabled": True}
        overlay = {"account": {"cap": 13}, "enabled": False}
        self.assertEqual(config.merge(base, overlay),
                         {"account": {"name": "Juniper Works", "cap": 13},
                          "enabled": False})
        self.assertEqual(base["account"]["cap"], 12)
        self.assertEqual(overlay["account"], {"cap": 13})
        self.assertEqual(config.merge({"account": {"name": "Juniper Works"}},
                                      {"account": 2}), {"account": 2})
        self.assertEqual(config.merge({"account": 2},
                                      {"account": {"name": "Juniper Works"}}),
                         {"account": {"name": "Juniper Works"}})

    def test_effective_config_sha256_uses_canonical_json(self):
        raw = {"company": "Juniper Works", "amount": 17}
        payload = '{\n  "amount": 17,\n  "company": "Juniper Works"\n}\n'
        self.assertEqual(config.effective_config_sha256(raw),
                         hashlib.sha256(payload.encode("utf-8")).hexdigest())

    def test_load_effective_requires_a_complete_document(self):
        complete = self.write_json("complete.json", self.raw)
        loaded = config.load_effective(complete)
        self.assertEqual(loaded.raw, self.raw)
        self.assertEqual(loaded.path, complete)
        fragment = self.write_json("fragment.json", {"state_root": self.tmp.name})
        with self.assertRaisesRegex(ValueError, "not complete"):
            config.load_effective(fragment)

    def test_init_rejects_missing_keys_and_peers(self):
        missing = dict(self.raw)
        del missing["limits"]
        with self.assertRaisesRegex(ValueError, "limits"):
            config.Config(missing, "synthetic.json")
        missing = dict(self.raw, peers={"claude": {}})
        with self.assertRaisesRegex(ValueError, "codex"):
            config.Config(missing, "synthetic.json")

    def test_contract_version_returns_configured_value(self):
        self.assertEqual(self.cfg.contract_version, "7")

    def test_state_root_expands_home(self):
        self.raw["state_root"] = "~/synthetic-state"
        expanded = os.path.join(self.tmp.name, "synthetic-state")
        with mock.patch.object(config.os.path, "expanduser", return_value=expanded) as expanduser:
            self.assertEqual(self.cfg.state_root,
                             expanded)
        expanduser.assert_called_once_with("~/synthetic-state")

    def test_schema_path_resolves_relative_and_absolute_paths(self):
        self.assertEqual(self.cfg.schema_path,
                         os.path.join(config.REPO_ROOT, "schema/synthetic.json"))
        absolute = os.path.join(self.tmp.name, "schema.json")
        self.raw["schema_path"] = absolute
        self.assertEqual(self.cfg.schema_path, absolute)

    def test_limit_rejects_missing_or_noninteger_values(self):
        self.assertEqual(self.cfg.limit("prompt_max_chars"), 100)
        for name in ("absent", "broken"):
            if name == "broken":
                self.raw["limits"][name] = "many"
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "limits"):
                self.cfg.limit(name)

    def test_retention_days_returns_configured_days(self):
        self.assertEqual(self.cfg.retention_days("jobs_days"), 17)

    def test_prompt_budget_respects_configured_cap(self):
        self.assertEqual(self.cfg.prompt_budget("start"), 60)
        self.raw["limits"]["prompt_start_max_chars"] = 120
        self.assertEqual(self.cfg.prompt_budget("start"), 99)
        self.raw["limits"]["prompt_start_max_chars"] = 0
        with self.assertRaisesRegex(ValueError, "positive integer"):
            self.cfg.prompt_budget("start")

    def test_request_timeout_checks_finite_value_and_floor(self):
        self.assertEqual(self.cfg.request_timeout("claude"), 300.0)
        self.raw["peers"]["claude"]["timeout_seconds"] = -3
        self.assertEqual(self.cfg.request_timeout("claude"), 0.0)
        self.raw["peers"]["claude"]["timeout_seconds"] = "inf"
        with self.assertRaisesRegex(ValueError, "finite"):
            self.cfg.request_timeout("claude")

    def test_allowed_classifications_returns_tuple(self):
        self.raw["allowed_source_classifications"] = ["synthetic", "public"]
        self.assertEqual(self.cfg.allowed_classifications, ("synthetic", "public"))

    def test_client_derived_routes_rejects_bad_opt_in(self):
        self.raw["client_derived_routes"] = ["peer"]
        self.assertEqual(self.cfg.client_derived_routes, frozenset({"peer"}))
        self.raw["client_derived_routes"] = ["unknown"]
        with self.assertRaisesRegex(ValueError, "client_derived_routes"):
            self.cfg.client_derived_routes

    def test_peer_reasoning_effort_validates_level(self):
        self.assertIsNone(self.cfg.peer_reasoning_effort("claude"))
        self.raw["peers"]["claude"]["reasoning_effort"] = "high"
        self.assertEqual(self.cfg.peer_reasoning_effort("claude"), "high")
        self.raw["peers"]["claude"]["reasoning_effort"] = "extreme"
        with self.assertRaisesRegex(ValueError, "reasoning_effort"):
            self.cfg.peer_reasoning_effort("claude")

    def test_peer_allowed_classifications_respects_override(self):
        self.raw["allowed_source_classifications"] = ["synthetic", "public"]
        self.assertEqual(self.cfg.peer_allowed_classifications("claude"),
                         ("synthetic", "public"))
        self.raw["peers"]["claude"]["allowed_source_classifications"] = ["public"]
        self.assertEqual(self.cfg.peer_allowed_classifications("claude"), ("public",))
        self.raw["client_derived_routes"] = ["peer"]
        self.assertEqual(self.cfg.peer_allowed_classifications("claude"),
                         ("public", "client_derived"))

    def test_refused_classifications_returns_tuple(self):
        self.raw["refused_source_classifications"] = ["secret"]
        self.assertEqual(self.cfg.refused_classifications, ("secret",))

    def test_local_first_object_is_validated(self):
        self.assertFalse(self.cfg.local_first_enabled())
        self.raw["local_first"] = "enabled"
        with self.assertRaisesRegex(ValueError, "local_first must be an object"):
            self.cfg.local_first_enabled()

    def test_local_first_enabled_requires_boolean(self):
        self.raw["local_first"] = {"enabled": True}
        self.assertTrue(self.cfg.local_first_enabled())
        self.raw["local_first"]["enabled"] = "yes"
        with self.assertRaisesRegex(ValueError, "boolean"):
            self.cfg.local_first_enabled()

    def test_local_first_min_bytes_requires_nonnegative_integer(self):
        self.raw["local_first"] = {"read_gate_min_bytes": 5}
        self.assertEqual(self.cfg.local_first_min_bytes(), 5)
        for invalid in (-1, True, "5"):
            self.raw["local_first"]["read_gate_min_bytes"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.cfg.local_first_min_bytes()

    def test_local_first_queue_root_requires_absolute_path(self):
        self.raw["local_first"] = {"local_queue_root": self.tmp.name}
        self.assertEqual(self.cfg.local_first_queue_root(), self.tmp.name)
        self.raw["local_first"]["local_queue_root"] = "relative/queue"
        with self.assertRaisesRegex(ValueError, "absolute path"):
            self.cfg.local_first_queue_root()

    def test_local_first_receipt_age_requires_nonnegative_integer(self):
        self.raw["local_first"] = {"receipt_max_age_seconds": 45}
        self.assertEqual(self.cfg.local_first_receipt_max_age_seconds(), 45)
        for invalid in (-1, True, "45"):
            self.raw["local_first"]["receipt_max_age_seconds"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.cfg.local_first_receipt_max_age_seconds()

    def test_peer_extra_env_rejects_credentials(self):
        self.raw["peers"]["codex"]["extra_env"] = {"SYNTHETIC_MODE": "on"}
        self.assertEqual(self.cfg.peer_extra_env("codex"), {"SYNTHETIC_MODE": "on"})
        self.raw["peers"]["codex"]["extra_env"] = {"OPENAI_API_KEY": "invented"}
        with self.assertRaisesRegex(ValueError, "must not carry"):
            self.cfg.peer_extra_env("codex")
        self.raw["peers"]["codex"]["extra_env"] = ["wrong"]
        with self.assertRaisesRegex(ValueError, "must be an object"):
            self.cfg.peer_extra_env("codex")

    def test_poll_interval_returns_configured_seconds(self):
        self.raw["worker"] = {"poll_interval_seconds": 0.75}
        self.assertEqual(self.cfg.poll_interval, 0.75)

    def test_python_executable_uses_configured_path(self):
        self.raw["worker"] = {"python_executable": os.path.join(self.tmp.name, "python")}
        self.assertEqual(self.cfg.python_executable, self.raw["worker"]["python_executable"])
        self.raw["worker"] = {}
        self.assertEqual(self.cfg.python_executable, sys.executable)

    def test_state_joins_root_and_components(self):
        self.assertEqual(self.cfg.state("ledger", "synthetic.jsonl"),
                         os.path.join(self.tmp.name, "ledger", "synthetic.jsonl"))

    def test_state_identifier_path_rejects_bad_id(self):
        identifier = str(uuid.uuid4())
        self.assertEqual(self.cfg._state_identifier_path("jobs", identifier),
                         os.path.join(self.tmp.name, "jobs", identifier))
        with self.assertRaises(BrokerError):
            self.cfg._state_identifier_path("jobs", "../account")

    def test_ledger_path_uses_state_root(self):
        self.assertEqual(self.cfg.ledger_path,
                         os.path.join(self.tmp.name, "ledger", "exchanges.jsonl"))

    def test_job_dir_uses_canonical_identifier(self):
        identifier = str(uuid.uuid4())
        self.assertEqual(self.cfg.job_dir(identifier),
                         os.path.join(self.tmp.name, "jobs", identifier))

    def test_conversation_path_uses_json_suffix(self):
        identifier = str(uuid.uuid4())
        self.assertEqual(self.cfg.conversation_path(identifier),
                         os.path.join(self.tmp.name, "conversations", identifier + ".json"))

    def test_workspace_joins_peer_and_conversation(self):
        self.assertEqual(self.cfg.workspace("codex", "synthetic-conversation"),
                         os.path.join(self.tmp.name, "workspaces", "codex",
                                      "synthetic-conversation"))

    def test_load_schema_reads_blob_and_reports_hash(self):
        path = self.write_json("schema.json", {"title": "Synthetic schema"})
        self.raw["schema_path"] = path
        with mock.patch("agent_bridge.schema_validate.assert_supported") as supported:
            schema, digest = self.cfg.load_schema()
        self.assertEqual(schema, {"title": "Synthetic schema"})
        self.assertEqual(digest, hashlib.sha256(Path(path).read_bytes()).hexdigest())
        supported.assert_called_once_with(schema)
        os.unlink(path)
        with self.assertRaises(BrokerError) as caught:
            self.cfg.load_schema()
        self.assertEqual(caught.exception.category, ErrorCategory.PREFLIGHT_SCHEMA_MISSING)


if __name__ == "__main__":
    unittest.main()
