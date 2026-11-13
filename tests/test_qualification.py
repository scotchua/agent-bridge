"""Offline contracts for automatic requalification state and scheduling."""
from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge import drift, qualification, store  # noqa: E402


def inventory(version: str = "1") -> dict:
    return {"executables": {"claude": {"path": "/tmp/claude", "version": version, "sha256": version},
                            "codex": {"path": "/tmp/codex", "version": version, "sha256": version}},
            "ollama": {"version": version, "endpoint": "http://127.0.0.1:11434"},
            "gemma": {"delegate": {"path": "/tmp/delegate", "sha256": version},
                      "validator": {"path": "/tmp/validator", "sha256": version}}}


class QualificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = self.temp.name
        self.config = os.path.join(self.home, "orchestration.json")
        Path(self.config).write_text("{}", encoding="utf-8")
        store.atomic_write_json(os.path.join(self.home, ".agent-bridge", "onboarding",
                                             "gate-installation.json"), {"config": self.config})

    def settings(self, **values: object) -> None:
        store.atomic_write_json(os.path.join(self.home, ".agent-bridge", "drift", "settings.json"), values)

    @contextlib.contextmanager
    def automatic_requalification_enabled(self):
        """Locally undo the suite-wide kill switch and restore it afterwards."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(drift.NO_AUTO_REQUALIFY_ENV, None)
            yield

    def test_identity_is_stable_and_uses_full_component_identity(self) -> None:
        first = inventory("1")
        self.assertEqual(qualification.key("claude", first), qualification.key("claude", first))
        second = inventory("2")
        self.assertNotEqual(qualification.key("claude", first), qualification.key("claude", second))
        self.assertNotEqual(qualification.key("ollama", first), qualification.key("ollama", second))

    def test_two_attempts_then_structured_refusal(self) -> None:
        value = inventory()
        first = qualification.reserve(self.home, ["claude"], value, now=1000)
        self.assertTrue(first["started"])
        qualification.finish(self.home, set(), now=1000)
        second = qualification.reserve(self.home, ["claude"], value,
                                       now=1000 + qualification.BACKOFF_SECONDS)
        self.assertTrue(second["started"])
        qualification.finish(self.home, set(), now=1000 + qualification.BACKOFF_SECONDS)
        third = qualification.reserve(self.home, ["claude"], value,
                                      now=1000 + 2 * qualification.BACKOFF_SECONDS)
        self.assertEqual(third["reason"], "attempts_exhausted")

    def test_reserve_requalifies_an_eligible_component_when_another_is_exhausted(self) -> None:
        self.settings(auto_requalify_max_attempts=1)
        value = inventory()
        self.assertTrue(qualification.reserve(self.home, ["claude"], value, now=1000)["started"])
        qualification.finish(self.home, set(), now=1000)
        result = qualification.reserve(self.home, ["claude", "ollama"], value,
                                       now=1000 + qualification.BACKOFF_SECONDS)
        self.assertTrue(result["started"])
        self.assertEqual(result["components"], ["ollama"])
        self.assertEqual(result["skipped"], [{"component": "claude", "key": qualification.key("claude", value),
                                                "reason": "attempts_exhausted"}])

    def test_fourth_provider_run_in_one_day_is_refused(self) -> None:
        for number in range(3):
            value = inventory(str(number))
            started = qualification.reserve(self.home, ["ollama"], value, now=1000 + number)
            self.assertTrue(started["started"])
            qualification.finish(self.home, set(), now=1000 + number)
        fourth = qualification.reserve(self.home, ["ollama"], inventory("four"), now=1010)
        self.assertEqual(fourth["reason"], "daily_budget_exhausted")

    def test_racing_callers_reserve_one_flight(self) -> None:
        outcomes: list[dict] = []
        barrier = threading.Barrier(2)
        def reserve() -> None:
            barrier.wait()
            outcomes.append(qualification.reserve(self.home, ["codex"], inventory(), now=1000))
        threads = [threading.Thread(target=reserve) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(sum(bool(item.get("started")) for item in outcomes), 1)
        self.assertEqual(next(item for item in outcomes if not item.get("started"))["reason"], "single_flight")

    def test_disabled_detect_creates_no_state_or_child(self) -> None:
        status = {"changed": ["claude"], "inventory": inventory()}
        spawn = mock.Mock()
        with self.automatic_requalification_enabled():
            result = drift.requalify_on_detect(self.config, status, home=self.home, spawner=spawn)
        self.assertEqual(result["reason"], "not_enabled")
        spawn.assert_not_called()
        self.assertFalse(os.path.exists(qualification.paths(self.home)["root"]))

    def test_enabled_detect_starts_injected_detached_child_once(self) -> None:
        self.settings(auto_requalify=True)
        status = {"changed": ["claude"], "inventory": inventory()}
        child = type("Child", (), {"pid": os.getpid()})()
        spawn = mock.Mock(return_value=child)
        with self.automatic_requalification_enabled():
            result = drift.requalify_on_detect(self.config, status, home=self.home, spawner=spawn)
            again = drift.requalify_on_detect(self.config, status, home=self.home, spawner=spawn)
        self.assertTrue(result["started"])
        self.assertIn("--requalify-only", spawn.call_args.args[0])
        self.assertEqual(spawn.call_args.kwargs["env"][drift.AUTO_REQUALIFY_ENV], "1")
        self.assertEqual(again["reason"], "single_flight")
        spawn.assert_called_once()

    def test_final_failure_is_durable_quick_status_not_a_separate_alert(self) -> None:
        self.settings(auto_requalify=True, auto_requalify_max_attempts=1)
        status = {"changed": ["claude"], "inventory": inventory()}
        child = type("Child", (), {"pid": os.getpid()})()
        with self.automatic_requalification_enabled(), \
             mock.patch.object(drift, "doctor", return_value={"requalified": [], "inventory": inventory(), "ok": False,
                                                                "changed": ["claude"], "needs_you": []}), \
             mock.patch.object(drift, "report_attention") as attention:
            drift.requalify_on_detect(self.config, status, home=self.home, spawner=lambda *_a, **_k: child)
            drift.automatic_requalify_child(self.config, home=self.home)
            refusal = drift.requalify_on_detect(self.config, status, home=self.home, spawner=lambda *_a, **_k: child)
        self.assertEqual(refusal["reason"], "attempts_exhausted")
        attention.assert_not_called()
        items = qualification.attention(self.home, ["claude"], inventory())
        self.assertEqual(items[0]["reason"], "attempts_exhausted")

    def test_child_checks_only_the_components_reserved_in_its_flight(self) -> None:
        self.settings(auto_requalify=True, auto_requalify_max_attempts=1)
        self.assertTrue(qualification.reserve(self.home, ["claude"], inventory(), now=1000)["started"])
        qualification.finish(self.home, set(), now=1000)
        status = {"changed": ["claude", "codex"], "inventory": inventory()}
        child = type("Child", (), {"pid": os.getpid()})()
        with self.automatic_requalification_enabled():
            drift.requalify_on_detect(self.config, status, home=self.home, spawner=lambda *_a, **_k: child)
        report = {"requalified": ["codex"], "inventory": inventory(), "ok": True,
                  "changed": ["codex"], "needs_you": []}
        with mock.patch.object(drift, "doctor", return_value=report) as doctor:
            drift.automatic_requalify_child("/tmp/config", home=self.home)
        self.assertEqual(doctor.call_args.kwargs["live_components"], {"codex"})

    def test_automatic_child_suppresses_the_generic_quiet_notification(self) -> None:
        report = {"ok": False, "changed": ["claude"], "needs_you": ["unrelated"]}
        with mock.patch.dict(os.environ, {drift.AUTO_REQUALIFY_ENV: "1"}), \
             mock.patch.object(drift, "automatic_requalify_child", return_value=report), \
             mock.patch.object(drift, "_notify_attention") as notify:
            self.assertEqual(drift.main(["--requalify-only", "--quiet"]), 1)
        notify.assert_not_called()

    def test_passing_child_audits_and_finishes_the_flight(self) -> None:
        self.settings(auto_requalify=True)
        status = {"changed": ["claude"], "inventory": inventory()}
        child = type("Child", (), {"pid": os.getpid()})()
        with self.automatic_requalification_enabled():
            drift.requalify_on_detect(self.config, status, home=self.home, spawner=lambda *_a, **_k: child)
        report = {"requalified": ["claude"], "inventory": inventory(), "ok": True,
                  "changed": ["claude"], "needs_you": []}
        with mock.patch.object(drift, "doctor", return_value=report), \
             mock.patch("agent_bridge.orchestration.config.load", return_value=type("Cfg", (), {"execution_queue_root": "/tmp/queue"})()), \
             mock.patch("agent_bridge.orchestration.execution_worker._request_drift_refresh") as refresh:
            result = drift.automatic_requalify_child("/tmp/config", home=self.home)
        self.assertEqual(result["automatic_passed"], ["claude"])
        self.assertIsNone(qualification.flight(self.home))
        self.assertIn('"action": "automatic_requalification"', Path(drift.paths(self.home)["audit"]).read_text())
        refresh.assert_called_once_with("/tmp/queue")

    def test_quick_check_keeps_final_failure_until_that_identity_passes(self) -> None:
        value = {**inventory("2"), "paths": {}}
        baseline = {**inventory("1"), "paths": {}}
        qualification.report_refusal(self.home, "claude", value, "attempts_exhausted")
        drift._quick_cache.clear()
        with mock.patch.object(drift, "discover_config", return_value="/tmp/config"), \
             mock.patch.object(drift, "inventory", return_value=value), \
             mock.patch.object(drift, "_read_baseline", return_value=baseline):
            failed = drift.quick_check("/tmp/config", home=self.home)
            state = qualification._read_state(self.home, qualification.key("claude", value))
            state["last_result"] = "passed"
            store.atomic_write_json(qualification._state_path(self.home, qualification.key("claude", value)), state)
            passed = drift.quick_check("/tmp/config", home=self.home, force_refresh=True)
        self.assertIn("Automatic requalification for claude", " ".join(failed["attention"]))
        self.assertNotIn("Automatic requalification for claude", " ".join(passed["attention"]))

    def test_foreign_config_refuses_before_state_or_child(self) -> None:
        self.settings(auto_requalify=True)
        foreign = os.path.join(self.home, "foreign.json")
        Path(foreign).write_text("{}", encoding="utf-8")
        spawn = mock.Mock()
        with self.automatic_requalification_enabled():
            result = drift.requalify_on_detect(foreign, {"changed": ["claude"], "inventory": inventory()},
                                               home=self.home, spawner=spawn)
        self.assertEqual(result, {"started": False, "reason": "foreign_config"})
        spawn.assert_not_called()
        self.assertFalse(os.path.exists(qualification.paths(self.home)["root"]))

    def test_environment_switch_refuses_matching_config_before_state_or_child(self) -> None:
        self.settings(auto_requalify=True)
        spawn = mock.Mock()
        with mock.patch.dict(os.environ, {drift.NO_AUTO_REQUALIFY_ENV: "1"}):
            result = drift.requalify_on_detect(self.config, {"changed": ["claude"], "inventory": inventory()},
                                               home=self.home, spawner=spawn)
        self.assertEqual(result, {"started": False, "reason": "disabled_by_environment"})
        spawn.assert_not_called()
        self.assertFalse(os.path.exists(qualification.paths(self.home)["root"]))

    def test_explicit_consent_setting_overrides_schedule_only_when_present(self) -> None:
        schedule = drift.schedule_path(self.home)
        Path(schedule).parent.mkdir(parents=True)
        Path(schedule).write_text("scheduled", encoding="utf-8")
        self.settings(auto_requalify=False)
        self.assertFalse(qualification.enabled(self.home, schedule))
        self.settings(auto_requalify=True)
        self.assertTrue(qualification.enabled(self.home, schedule))
        self.settings()
        self.assertTrue(qualification.enabled(self.home, schedule))

    def test_later_refusal_reason_updates_quick_status_without_a_second_alert(self) -> None:
        value = inventory()
        qualification.report_refusal(self.home, "claude", value, "attempts_exhausted")
        self.assertIsNone(qualification.report_refusal(self.home, "claude", value, "daily_budget_exhausted"))
        state = qualification._read_state(self.home, qualification.key("claude", value))
        self.assertEqual(state["last_result"], "daily_budget_exhausted")
        self.assertTrue(state["attention_reported"])
