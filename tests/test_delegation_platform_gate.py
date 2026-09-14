"""The gate that keeps automatic execution off unverified platforms.

Public main refuses automatic delegation anywhere but macOS. This branch is
the Windows lane, so the obvious thing to do while building it was to relax
that refusal, and this file exists to make sure nobody does.

The refusal is kept and computed. ``delegation_platform_blocker`` asks whether
*this machine* carries a boundary-verification record, which is a fact rather
than a platform name, so the gate opens when the lane is actually proven and
not when somebody edits a string. No machine can satisfy it today.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge import onboard


ANSWERS = {"version": 1, "directions": "both",
           "targets": {"codex": True, "claude_code": True, "claude_desktop": False},
           "privacy": {"mode": "strict", "peers": {}},
           "local_ollama": {"enabled": False},
           "automatic_delegation": {"enabled": True}}


class PlatformBlockerTests(unittest.TestCase):
    def test_macos_is_the_only_platform_that_passes(self):
        self.assertEqual(onboard.delegation_platform_blocker(platform_name="darwin"), "")

    def test_every_other_platform_is_refused(self):
        for name in ("win32", "cygwin", "linux", "freebsd", "aix"):
            with self.subTest(name):
                self.assertTrue(onboard.delegation_platform_blocker({}, platform_name=name))

    def test_the_refusal_is_one_string_rather_than_three_wordings(self):
        for name in ("win32", "linux"):
            self.assertEqual(onboard.delegation_platform_blocker({}, platform_name=name),
                             onboard.DELEGATION_PLATFORM_REFUSAL)

    def test_the_refusal_names_macos_so_a_caller_can_say_why(self):
        self.assertIn("only on macOS", onboard.DELEGATION_PLATFORM_REFUSAL)

    def test_an_unconfigured_windows_machine_is_refused_without_observing(self):
        """The gate must not run a preflight sweep to decide whether to ask."""

        with mock.patch.object(onboard.windows_preflight, "run_preflight") as sweep:
            self.assertTrue(onboard.delegation_platform_blocker({}, platform_name="win32"))
        sweep.assert_not_called()

    def test_a_windows_machine_with_no_record_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            choice = {"windows_wsl_runtime_root": str(root),
                      "windows_wsl_manifest_path": str(root / "manifest.json")}
            self.assertTrue(onboard.delegation_platform_blocker(choice,
                                                                platform_name="win32"))

    def test_the_gate_reads_the_record_the_worker_reads(self):
        """Not a second opinion. The same helper dispatch uses."""

        with mock.patch.object(onboard, "_windows_delegation_state",
                               return_value={"state": "ready"}) as state:
            self.assertEqual(onboard.delegation_platform_blocker({}, platform_name="win32"), "")
        state.assert_called_once()


class CallSiteTests(unittest.TestCase):
    """Three call sites, one rule, each checked on its own."""

    def test_plan_refuses_off_macos(self):
        with mock.patch.object(onboard.sys, "platform", "win32"):
            with self.assertRaisesRegex(ValueError, "only on macOS"):
                onboard.plan(ANSWERS, "/repo")

    def test_plan_refuses_on_linux_too(self):
        with mock.patch.object(onboard.sys, "platform", "linux"):
            with self.assertRaisesRegex(ValueError, "only on macOS"):
                onboard.plan(ANSWERS, "/repo")

    def test_plan_allows_it_when_delegation_is_off(self):
        answers = dict(ANSWERS, automatic_delegation={"enabled": False})
        with mock.patch.object(onboard.sys, "platform", "win32"):
            report = onboard.plan(answers, str(ROOT))
        self.assertIsNone(report.get("automatic_delegation"))

    def test_apply_re_checks_rather_than_trusting_the_plan(self):
        """A candidate written on a Mac can be applied on a Windows box."""

        source = Path(onboard.__file__).read_text(encoding="utf-8")
        body = source[source.index("def _apply("):]
        self.assertIn("delegation_platform_blocker", body)

    def test_the_questionnaire_does_not_ask_what_it_would_refuse(self):
        asked: list[str] = []

        def ask(prompt: str) -> str:
            asked.append(prompt)
            if "Enable automatic delegation" in prompt:
                return "yes"
            if "[yes/no]" in prompt:
                return "no"
            if "direction" in prompt.lower():
                return "both"
            return ""

        with mock.patch.object(onboard.sys, "platform", "win32"):
            try:
                answers = onboard.questionnaire(ask)
            except (ValueError, KeyError):  # an unrelated prompt path
                answers = None
        self.assertFalse(any("Enable automatic delegation" in prompt
                             for prompt in asked))
        if answers is not None:
            self.assertFalse(answers["automatic_delegation"]["enabled"])


class DocumentedBoundaryTests(unittest.TestCase):
    """The words a reader sees have to agree with the gate."""

    def test_the_readme_states_the_windows_boundary(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("Windows support boundary", text)
        self.assertIn("macOS-only", text)

    def test_the_agent_setup_guide_tells_an_agent_not_to_offer_it(self):
        text = (ROOT / "docs" / "SETUP-WITH-AN-AGENT.md").read_text(encoding="utf-8")
        self.assertIn("Platform gate", text)
        self.assertIn("not partial", text)

    def test_install_does_not_present_windows_delegation_as_available(self):
        text = (ROOT / "INSTALL.md").read_text(encoding="utf-8")
        self.assertIn("Automatic delegation is not available on Windows", text)

    def test_no_document_claims_live_windows_validation(self):
        """Affirmative claims only. The denials are the point of these files.

        A plain substring search fails here for the right reason: every one of
        these documents says the phrase, preceded by "has not been" or
        "none of it has been". So the pattern requires the affirmative form,
        with no negation in the fifty characters before it.
        """

        import re

        claims = re.compile(
            r"(?<!not )(?<!never )\b(?:has been|was|is) (?:live-?)?"
            r"(?:validated|verified|tested) on (?:a )?(?:live )?windows",
            re.IGNORECASE)
        negated = re.compile(
            r"\b(?:not|never|no|none|nothing|neither|without|unverified)\b",
            re.IGNORECASE)
        for name in ("README.md", "INSTALL.md", "docs/WINDOWS-DELEGATION.md",
                     "docs/WINDOWS-ROOTFS.md", "docs/SETUP-WITH-AN-AGENT.md",
                     "docs/WINDOWS-VALIDATION-RUNBOOK.md",
                     "docs/RELEASE-SIGNING.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            for match in claims.finditer(text):
                window = text[max(0, match.start() - 60):match.start()]
                self.assertTrue(negated.search(window),
                                f"{name}: {text[match.start():match.end() + 20]!r}")


if __name__ == "__main__":
    unittest.main()
