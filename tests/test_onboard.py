"""Offline tests for portable onboarding; all home paths are throwaways."""
from __future__ import annotations

import json
import os
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from agent_bridge import config, onboard, setup_cmd, store  # noqa: E402


def answers(**changes):
    base = {"version": 1, "directions": "both",
            "targets": {"codex": True, "claude_code": True, "claude_desktop": False},
            "privacy": {"mode": "baseline", "peers": {}}, "local_ollama": {"enabled": False}}
    base.update(changes)
    return base


def active_in(directory):
    raw = store.read_json(config.DEFAULT_CONFIG_PATH)
    raw["state_root"] = os.path.join(directory, "state")
    raw["peers"]["codex"]["codex_home"] = os.path.join(directory, "isolated-codex")
    return config.Config(raw, config.DEFAULT_CONFIG_PATH)


class OnboardingTests(unittest.TestCase):
    def test_privacy_never_widens_blocked_labels(self):
        bad = answers(privacy={"mode": "custom", "peers": {"claude": ["public", "secret"], "codex": ["synthetic"]}})
        with self.assertRaisesRegex(ValueError, "client and secret"):
            onboard.validate_answers(bad)
        strict = onboard.validate_answers(answers(privacy={"mode": "strict", "peers": {}}))
        self.assertEqual(onboard.privacy_overlay(strict)["allowed_source_classifications"], ["public", "synthetic"])

    def test_stage_uses_discovered_logged_in_pins_and_writes_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            candidate = os.path.join(tmp, "candidate.json")
            active = active_in(tmp)
            signed = {"claude": True, "codex": True}
            with mock.patch.object(config, "load", return_value=active), \
                 mock.patch.object(setup_cmd, "find_all", side_effect=lambda peer: [f"/{peer}"]), \
                 mock.patch.object(setup_cmd, "is_durable", return_value=True), \
                 mock.patch.object(setup_cmd, "version_of", side_effect=lambda path: path + " version"), \
                 mock.patch.object(setup_cmd, "claude_signed_in", return_value=signed["claude"]), \
                 mock.patch.object(setup_cmd, "codex_signed_in", return_value=signed["codex"]):
                record = onboard.stage(onboard.validate_answers(answers()), candidate)
            staged = config.load_effective(candidate)
            self.assertEqual(staged.peer("claude")["allowed_versions"], ["/claude version"])
            self.assertEqual(store.read_json(candidate + ".onboarding-plan.json"), record)

    def test_stage_refuses_missing_login_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(setup_cmd, "find_all", return_value=["/claude"]), \
             mock.patch.object(setup_cmd, "is_durable", return_value=True), \
             mock.patch.object(setup_cmd, "version_of", return_value="v"), \
             mock.patch.object(setup_cmd, "claude_signed_in", return_value=False):
            candidate = os.path.join(tmp, "candidate.json")
            with mock.patch.object(config, "load", return_value=active_in(tmp)):
                with self.assertRaisesRegex(ValueError, "confirmed login"):
                    onboard.stage(onboard.validate_answers(answers()), candidate)
            self.assertFalse(os.path.exists(candidate))

    def test_apply_merges_named_configs_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, candidate, results = os.path.join(tmp, "home"), os.path.join(tmp, "candidate.json"), os.path.join(tmp, "results.json")
            os.makedirs(os.path.join(home, ".codex"), exist_ok=True)
            Path(os.path.join(home, ".codex", "config.toml")).write_text('[projects."x"]\ntrust_level = "trusted"\n')
            Path(os.path.join(home, ".claude.json")).write_text(json.dumps({"mcpServers": {"keep": {"command": "else"}}, "other": 1}))
            active = active_in(tmp)
            staged = setup_cmd.write_candidate(candidate, onboard.privacy_overlay(onboard.validate_answers(answers())), active)
            plan = {"answers_sha256": onboard._sha(onboard.validate_answers(answers())), "effective_config_sha256": config.effective_config_sha256(staged.raw), "directions": "both", "targets": answers()["targets"], "pins": {peer: {key: staged.peer(peer).get(key) for key in ("executable", "allowed_versions")} for peer in ("claude", "codex")}}
            store.atomic_write_json(candidate + ".onboarding-plan.json", plan)
            results_doc = {"verdict": "PASS", "effective_config_sha256": config.effective_config_sha256(staged.raw), "configured_versions": {"claude": [], "codex": []}, "observed_versions": {"claude": [], "codex": []}, "controls_requested": {"timeout_canaries": 1}, "controls_executed": {"timeout_canaries": 1}}
            store.atomic_write_json(results, results_doc)
            # The promotion validator insists on pins; patch it and promotion so this remains offline.
            with mock.patch.object(setup_cmd, "validate_promotion"), mock.patch.object(setup_cmd, "promote_candidate", return_value={}), mock.patch.object(setup_cmd, "is_durable", return_value=True), mock.patch.object(config, "local_config_path", return_value=os.path.join(tmp,"local.json")):
                onboard.apply(onboard.validate_answers(answers()), candidate, results, str(ROOT), home=home)
                onboard.apply(onboard.validate_answers(answers()), candidate, results, str(ROOT), home=home)
                shared = Path(onboard._paths(home)["shared"])
                edited = shared.read_bytes() + b"User-authored rule\n"
                shared.write_bytes(edited)
                with self.assertRaisesRegex(ValueError, "shared instructions were edited"):
                    onboard.apply(onboard.validate_answers(answers()), candidate, results, str(ROOT), home=home)
                self.assertEqual(shared.read_bytes(), edited)
            self.assertIn('mcp_servers."claude-peer"', Path(os.path.join(home, ".codex", "config.toml")).read_text())
            self.assertEqual(store.read_json(os.path.join(home, ".claude.json"))["mcpServers"]["keep"]["command"], "else")
            self.assertTrue(list(Path(home).glob(".claude.json.agent-bridge.*.bak")))

    def test_apply_refuses_unmanaged_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.toml")
            Path(path).write_text('[mcp_servers."claude-peer"]\ncommand = "other"\n')
            with self.assertRaisesRegex(ValueError, "unmanaged"):
                onboard._toml_registration_update(path, "claude-peer", {"command": "python", "args": []})

    def test_uninstall_preserves_unmanaged_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "claude.json")
            store.atomic_write_json(path, {"mcpServers": {"codex-peer": {"command": "someone-else"}}})
            self.assertIsNone(onboard._remove_json_registration(path, "codex-peer", {"command": "python", "args": []}))

    def test_stale_managed_registration_requires_explicit_uninstall(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = onboard._paths(tmp)
            os.makedirs(os.path.dirname(paths["codex_toml"]), exist_ok=True)
            Path(paths["codex_toml"]).write_text(onboard._toml_block("claude-peer", {"command": "python", "args": []}))
            with self.assertRaisesRegex(ValueError, "uninstall"):
                onboard._refuse_stale_registrations(paths, {"codex_toml": set(), "claude_json": set(), "desktop_json": set()})

    def test_local_route_is_explicit_loopback_only(self):
        data = answers(local_ollama={"enabled": True, "endpoint": "http://127.0.0.1:11434", "model": "llama3", "allow_internal": True})
        output = onboard.plan(onboard.validate_answers(data), str(ROOT))
        self.assertEqual(output["local_worker"][-1], "--allow-internal")
        data["local_ollama"]["endpoint"] = "http://example.com:11434"
        with self.assertRaisesRegex(ValueError, "loopback"):
            onboard.validate_answers(data)


class HardeningTests(unittest.TestCase):
    def test_unknown_or_bad_privacy_is_not_guessed(self):
        for changes in ({"directions":[]}, {"privacy":{"mode":[]}}, {"typo_restriction":True}):
            with self.assertRaises(ValueError):onboard.validate_answers(answers(**changes))

    def test_toml_unicode_and_dotted_collision(self):
        import tomllib
        with tempfile.TemporaryDirectory() as tmp:
            path=os.path.join(tmp,"config.toml")
            command={"command":"C:\\People\\Zoë 🐈\\python.exe","args":["path with spaces/setup_bridge.py"]}
            update=onboard._toml_registration_update(path,"claude-peer",command)
            self.assertEqual(tomllib.loads(update.decode())["mcp_servers"]["claude-peer"],command)
            Path(path).write_text('mcp_servers.claude-peer = { command = "other" }\n')
            with self.assertRaises(ValueError):onboard._toml_registration_update(path,"claude-peer",command)

    def test_failed_second_write_rolls_back_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            first,second=[os.path.join(tmp,n) for n in ("first","second")]
            Path(first).write_bytes(b"old");Path(second).write_bytes(b"other")
            original_writer=store.atomic_write_bytes
            def writer(path,data):
                if path==second:raise OSError("synthetic failure")
                return original_writer(path,data)
            with mock.patch.object(store,"atomic_write_bytes",side_effect=writer):
                with self.assertRaises(OSError):onboard._commit_updates({first:b"new",second:b"next"},{first:b"old",second:b"other"})
            self.assertEqual(Path(first).read_bytes(),b"old")
            self.assertEqual(Path(second).read_bytes(),b"other")

    def test_concurrent_edit_refused_before_any_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=os.path.join(tmp,"file");Path(path).write_bytes(b"new by host")
            with self.assertRaises(ValueError):onboard._commit_updates({path:b"installer"},{path:b"old"})
            self.assertEqual(Path(path).read_bytes(),b"new by host")

    def test_uninstall_composes_peer_and_local_and_preserves_user_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            choices=onboard.validate_answers(answers(local_ollama={"enabled":True,"endpoint":"http://127.0.0.1:11434","model":"example:local","allow_internal":False}))
            paths=onboard._paths(tmp)
            peer=onboard._command("claude",str(ROOT));local=onboard._local_command(choices["local_ollama"],str(ROOT))
            store.atomic_write_json(paths["claude_json"],{"keep":1,"mcpServers":{"codex-peer":peer,"local-peer":local,"other":{"command":"keep"}}})
            store.atomic_write_bytes(paths["codex_toml"],(onboard._toml_block("claude-peer",onboard._command("codex",str(ROOT)))+onboard._toml_block("local-peer",local)).encode())
            originals={path:Path(path).read_bytes() for path in (paths["claude_json"],paths["codex_toml"])}
            onboard.uninstall(choices,str(ROOT),home=tmp)
            self.assertEqual({path:Path(path).read_bytes() for path in originals},originals)
            onboard.uninstall(choices,str(ROOT),home=tmp,apply_changes=True)
            self.assertEqual(store.read_json(paths["claude_json"])["mcpServers"],{"other":{"command":"keep"}})
            self.assertNotIn("mcp_servers",Path(paths["codex_toml"]).read_text())

    def test_edited_managed_pointer_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=os.path.join(tmp,"AGENTS.md")
            store.atomic_write_bytes(path,onboard._managed_text_update(path,"instructions","original"))
            Path(path).write_text(Path(path).read_text().replace("original","my changes"))
            self.assertIsNone(onboard._remove_managed_text(path,"instructions","original"))
            with self.assertRaises(ValueError):onboard._managed_text_update(path,"instructions","original")

    def test_invalid_canary_never_reaches_writes(self):
        cfg=active_in(tempfile.gettempdir())
        with self.assertRaisesRegex(ValueError,"not PASS"):
            setup_cmd.validate_promotion(cfg,{"verdict":"FAIL"})

    def test_home_override_never_uses_real_appdata(self):
        with mock.patch.dict(os.environ,{"APPDATA":"do-not-use"}), mock.patch.object(onboard.os,"name","nt"):
            paths=onboard._paths("synthetic-home")
            self.assertNotIn("do-not-use",paths["desktop_json"])


class ReviewRegressionTests(unittest.TestCase):
    def test_failing_gate_preserves_every_configuration_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            choices = onboard.validate_answers(answers())
            active = active_in(tmp)
            candidate = os.path.join(tmp, "candidate.json")
            results = os.path.join(tmp, "results.json")
            staged = setup_cmd.write_candidate(candidate, onboard.privacy_overlay(choices), active)
            store.atomic_write_json(candidate + ".onboarding-plan.json", {
                "answers_sha256": onboard._sha(choices),
                "effective_config_sha256": config.effective_config_sha256(staged.raw),
                "pins": {peer: {key: staged.peer(peer).get(key) for key in ("executable", "allowed_versions")}
                         for peer in ("claude", "codex")}})
            store.atomic_write_json(results, {"verdict": "FAIL"})
            home = os.path.join(tmp, "home")
            paths = onboard._paths(home)
            paths["active"] = os.path.join(tmp, "local.json")
            originals = {}
            for path in paths.values():
                store.atomic_write_bytes(path, b"existing user content\n")
                originals[path] = Path(path).read_bytes()
            with mock.patch.object(config, "load", return_value=active), \
                 mock.patch.object(config, "local_config_path", return_value=paths["active"]), \
                 mock.patch.object(setup_cmd, "promote_candidate") as promote:
                with self.assertRaisesRegex(ValueError, "not PASS"):
                    onboard.apply(choices, candidate, results, str(ROOT), home=home)
                promote.assert_not_called()
            self.assertEqual({path: Path(path).read_bytes() for path in originals}, originals)

    def test_custom_privacy_refuses_before_peer_dispatch(self):
        from agent_bridge import broker
        from agent_bridge.errors import BrokerError, ErrorCategory
        with tempfile.TemporaryDirectory() as tmp:
            choices = onboard.validate_answers(answers(privacy={"mode": "custom", "peers": {
                "claude": ["public", "synthetic"], "codex": ["internal", "synthetic"]}}))
            cfg = config.Config(config.merge(active_in(tmp).raw, onboard.privacy_overlay(choices)), config.DEFAULT_CONFIG_PATH)
            self.assertIn("internal", cfg.allowed_classifications)
            for caller, label in (("codex", "internal"), ("claude", "public")):
                with mock.patch.object(broker.preflight, "check_peer") as preflight:
                    with self.assertRaises(BrokerError) as error:
                        broker.start(cfg, caller, {"prompt": "invented test", "source_classification": label})
                    self.assertEqual(error.exception.category, ErrorCategory.SOURCE_CLASSIFICATION_REFUSED)
                    preflight.assert_not_called()

    def test_known_generated_pointer_upgrades_but_user_edit_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "AGENTS.md")
            store.atomic_write_bytes(path, onboard._managed_text_update(path, "instructions", "version one"))
            replacement = onboard._managed_text_update(path, "instructions", "version two", previous_body="version one")
            self.assertIn(b"version two", replacement)
            with self.assertRaises(ValueError):
                onboard._managed_text_update(path, "instructions", "version two", previous_body="different content")

    def test_uninstall_reports_edited_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = onboard._paths(tmp)
            store.atomic_write_json(paths["claude_json"], {"mcpServers": {"codex-peer": {"command": "user-edited"}}})
            before = Path(paths["claude_json"]).read_bytes()
            report = onboard.uninstall(onboard.validate_answers(answers()), str(ROOT), home=tmp, apply_changes=True)
            self.assertTrue(report["preserved_conflicts"])
            self.assertEqual(Path(paths["claude_json"]).read_bytes(), before)
            self.assertIn(paths["shared"], report["retained_files"])

    def test_choice_retries_without_guessing(self):
        replies = iter(["typo", "yes"])
        self.assertEqual(onboard._choice("Use?", {"yes", "no"}, lambda _: next(replies)), "yes")
        with self.assertRaises(ValueError):
            onboard._choice("Use?", {"yes", "no"}, lambda _: "")


class LauncherTests(unittest.TestCase):
    def test_absolute_launcher_exposes_only_intended_tools_from_other_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = active_in(tmp)
            candidate = os.path.join(tmp, "isolated.json")
            store.atomic_write_json(candidate, cfg.raw)
            requests = "\n".join(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": {}})
                                  for i, method in ((1, "initialize"), (2, "tools/list"))) + "\n"
            for caller, peer in (("codex", "claude"), ("claude", "codex")):
                proc = subprocess.run([sys.executable, str(ROOT / "setup_bridge.py"), "serve-peer",
                                       "--caller", caller, "--config", candidate], input=requests,
                                      capture_output=True, text=True, encoding="utf-8", cwd=tmp, timeout=15)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                messages = [json.loads(line) for line in proc.stdout.splitlines()]
                names = {t["name"] for m in messages if m.get("id") == 2 for t in m["result"]["tools"]}
                self.assertEqual(names, {peer + "_" + verb for verb in ("start", "continue", "poll", "read", "close")})
            proc = subprocess.run([sys.executable, str(ROOT / "setup_bridge.py"), "serve-local",
                                   "--endpoint", "http://127.0.0.1:11434", "--model", "synthetic-fixture"],
                                  input=requests, capture_output=True, text=True, encoding="utf-8", cwd=tmp, timeout=15)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            messages = [json.loads(line) for line in proc.stdout.splitlines()]
            names = {t["name"] for m in messages if m.get("id") == 2 for t in m["result"]["tools"]}
            self.assertEqual(names, {"local_worker_info", "local_worker_process"})


if __name__ == "__main__":
    unittest.main()
