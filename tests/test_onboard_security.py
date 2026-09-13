"""Preview must disclose every local-model registration before installation."""
from pathlib import Path
import sys
import unittest
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from agent_bridge import onboard

class OnboardSecurityTests(unittest.TestCase):
    def test_local_model_targets_are_visible_even_for_one_peer_direction(self):
        answers = onboard.validate_answers({
            "version": 1, "directions": "codex_to_claude",
            "targets": {"codex": True, "claude_code": True, "claude_desktop": False},
            "privacy": {"mode": "strict"},
            "local_ollama": {"enabled": True, "model": "synthetic-model",
                             "endpoint": "http://127.0.0.1:11434", "allow_internal": False}})
        result = onboard.plan(answers, str(ROOT))
        pairs = {(r["target"], r["name"]) for r in result["registrations"]}
        self.assertEqual(pairs, {("Codex", "claude-peer"), ("Codex", "local-peer"),
                                 ("Claude Code", "local-peer")})
        for r in result["registrations"]:
            if r["name"] == "local-peer":
                self.assertIn("serve-local", r["args"])
                self.assertNotIn("--allow-internal", r["args"])

if __name__ == "__main__": unittest.main()
