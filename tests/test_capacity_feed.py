"""The capacity feeder writes trusted refusals only, and fails closed."""

import contextlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.capacity_router import (
    DECLARED_SOURCE, MAX_FRESHNESS_SECONDS, PRESENCE_SOURCE, REPLAYED_SOURCES,
    CapacityObservation, StageRouter)
from agent_bridge.orchestration import autodecide, capacity_feed

NOW = 1_800_000_000.0


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


def codex_bucket(used=95, *, scope="primary", resets_in=3600, age=60, **extra):
    bucket = {"limit": "codex", "scope": scope, "status": "available", "used_percent": used,
              "window_minutes": 300, "resets_at": NOW + resets_in, "freshness_at": NOW - age}
    bucket.update(extra)
    return bucket


def claude_bucket(used=95, *, window="five_hour", resets_in=3600, age=60, **extra):
    bucket = {"window": window, "status": "available", "used_percent": used,
              "resets_at": NOW + resets_in, "freshness_at": NOW - age}
    bucket.update(extra)
    return bucket


def snapshot(source, buckets, **extra):
    data = {"schema": 1, "source": source, "status": "available", "received_at": NOW - 60,
            "freshness_at": NOW - 60, "source_measurement_at": None,
            "account_binding": "unverified", "buckets": buckets, "seen": {}}
    data.update(extra)
    return data


class CapacityFeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name) / "snapshots"
        self.dir.mkdir()
        self.clock = Clock()
        self.router = StageRouter(Path(self.temp.name) / "router.sqlite3", clock=self.clock)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, source, data):
        text = data if isinstance(data, str) else json.dumps(data)
        (self.dir / f"{source}.json").write_text(text, encoding="utf-8")

    def run_feed(self, ceiling=90, **kwargs):
        return {r["route"]: r for r in capacity_feed.feed(self.router, self.dir, ceiling, **kwargs)}

    def status(self, route):
        return self.router.report()["capacity"].get(route, {}).get("status")

    def row(self, route):
        return next((r for r in self.router.capacity_rows() if r["route"] == route), None)

    def test_over_ceiling_writes_a_trusted_refusal(self):
        self.write("codex", snapshot("codex", [codex_bucket(95)]))
        result = self.run_feed()
        self.assertEqual(result["codex"]["action"], "refused")
        self.assertEqual(self.status("codex"), "unavailable")
        row = self.row("codex")
        self.assertEqual((row["source"], row["trusted"], row["available"]),
                         (capacity_feed.FEED_SOURCE, 1, 0))
        self.assertEqual(row["fresh_until"], NOW + 3600)

    def test_exactly_at_ceiling_refuses(self):
        self.write("claude", snapshot("claude", [claude_bucket(90)]))
        self.assertEqual(self.run_feed()["claude"]["action"], "refused")

    def test_under_ceiling_writes_nothing(self):
        self.write("codex", snapshot("codex", [codex_bucket(40), codex_bucket(89.9, scope="secondary")]))
        result = self.run_feed()
        self.assertEqual((result["codex"]["action"], result["codex"]["reason"]),
                         ("none", "under_ceiling"))
        self.assertEqual(self.router.capacity_rows(), [])

    def test_never_writes_available(self):
        self.write("codex", snapshot("codex", [codex_bucket(1)]))
        self.write("claude", snapshot("claude", [claude_bucket(1)]))
        self.run_feed()
        self.assertEqual(self.router.capacity_rows(), [])

    def test_missing_files_write_nothing(self):
        result = self.run_feed()
        self.assertEqual({r["reason"] for r in result.values()}, {"snapshot_unknown"})
        self.assertEqual(self.router.capacity_rows(), [])

    def test_rejected_snapshots_write_nothing(self):
        bad = {
            "garbage": "{not json",
            "list": "[]",
            "schema": snapshot("codex", [codex_bucket()], schema=2),
            "source": snapshot("claude", [codex_bucket()]),
            "status": snapshot("codex", [codex_bucket()], status="unknown"),
            "verified": snapshot("codex", [codex_bucket()], account_binding="verified"),
            "future": snapshot("codex", [codex_bucket()], received_at=NOW + 301),
            "received_bool": snapshot("codex", [codex_bucket()], received_at=True),
            "buckets_dict": snapshot("codex", {"a": codex_bucket()}),
            "bucket_not_dict": snapshot("codex", [codex_bucket(), "x"]),
            "bad_scope": snapshot("codex", [codex_bucket(scope="tertiary")]),
            "bad_limit": snapshot("codex", [codex_bucket(limit="acct-12345")]),
        }
        for name, data in bad.items():
            with self.subTest(name=name):
                self.write("codex", data)
                self.assertEqual(self.run_feed()["codex"]["action"], "none")
                self.assertEqual(self.router.capacity_rows(), [])

    def test_claude_window_must_be_known(self):
        self.write("claude", snapshot("claude", [claude_bucket(window="monthly")]))
        self.assertEqual(self.run_feed()["claude"]["reason"], "snapshot_malformed")

    def test_unusable_buckets_are_skipped(self):
        bad = {
            "stale": codex_bucket(age=901),
            "future_fresh": codex_bucket(age=-301),
            "reset_passed": codex_bucket(resets_in=0),
            "bool_used": codex_bucket(used=True),
            "nan_used": codex_bucket(used=float("nan")),
            "over_100": codex_bucket(used=101),
            "string_used": codex_bucket(used="95"),
            "no_window": codex_bucket(window_minutes=None),
            "partial": codex_bucket(status="unknown (partial)"),
            "inf_reset": codex_bucket(resets_at=float("inf")),
        }
        for name, bucket in bad.items():
            with self.subTest(name=name):
                # NaN and infinity are not JSON; write them the way Python would.
                self.write("codex", json.dumps(snapshot("codex", [bucket])))
                result = self.run_feed()["codex"]
                self.assertEqual((result["action"], result["reason"]), ("none", "no_usable_window"))
                self.assertEqual(self.router.capacity_rows(), [])

    def test_one_bad_bucket_does_not_hide_a_good_one(self):
        self.write("codex", snapshot("codex", [codex_bucket(99, age=901),
                                               codex_bucket(95, scope="secondary")]))
        self.assertEqual(self.run_feed()["codex"]["action"], "refused")

    def test_freshness_is_capped_at_the_ledger_limit(self):
        self.write("claude", snapshot("claude", [claude_bucket(95, window="seven_day",
                                                               resets_in=5 * 86400)]))
        self.run_feed()
        self.assertEqual(self.row("claude")["fresh_until"], NOW + MAX_FRESHNESS_SECONDS)

    def test_refusal_lasts_until_the_last_over_window_resets(self):
        self.write("claude", snapshot("claude", [claude_bucket(95, resets_in=600),
                                                 claude_bucket(91, window="seven_day", resets_in=7200),
                                                 claude_bucket(10, window="seven_day", resets_in=9000)]))
        self.run_feed()
        self.assertEqual(self.row("claude")["fresh_until"], NOW + 7200)

    def test_dry_run_writes_nothing(self):
        self.write("codex", snapshot("codex", [codex_bucket(99)]))
        self.assertEqual(self.run_feed(dry_run=True)["codex"]["action"], "would_refuse")
        self.assertEqual(self.router.capacity_rows(), [])

    def test_overwrites_presence_and_declaration(self):
        for source in sorted(REPLAYED_SOURCES):
            with self.subTest(source=source):
                self.router.observe_capacity(CapacityObservation(
                    "codex", NOW, NOW + 60, True, source), trusted=True)
                self.write("codex", snapshot("codex", [codex_bucket(95)]))
                self.run_feed()
                self.assertEqual(self.status("codex"), "unavailable")

    def test_never_shortens_another_writers_refusal(self):
        self.router.observe_capacity(CapacityObservation(
            "codex", NOW, NOW + 3 * 3600, False, "quota:exhausted"), trusted=True)
        self.write("codex", snapshot("codex", [codex_bucket(95, resets_in=600)]))
        self.assertEqual(self.run_feed()["codex"]["action"], "kept_existing_refusal")
        self.assertEqual(self.row("codex")["source"], "quota:exhausted")
        self.assertEqual(self.row("codex")["fresh_until"], NOW + 3 * 3600)

    def test_extends_its_own_refusal(self):
        self.write("codex", snapshot("codex", [codex_bucket(95, resets_in=600)]))
        self.run_feed()
        self.write("codex", snapshot("codex", [codex_bucket(95, resets_in=1800)]))
        self.assertEqual(self.run_feed()["codex"]["action"], "refused")
        self.assertEqual(self.row("codex")["fresh_until"], NOW + 1800)

    def test_ceiling_must_be_set_and_sane(self):
        for value in (0, -1, 101, float("nan"), True, None, "90"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                capacity_feed.feed(self.router, self.dir, value)


class ReplayCannotClearRefusalTests(unittest.TestCase):
    """The ledger keeps one row per route, so this guard is what keeps a
    feeder refusal from being erased by the next hook call."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.router = StageRouter(Path(self.temp.name) / "router.sqlite3", clock=self.clock)
        self.router.observe_capacity(CapacityObservation(
            "codex", NOW, NOW + 600, False, capacity_feed.FEED_SOURCE), trusted=True)

    def tearDown(self):
        self.temp.cleanup()

    def status(self):
        return self.router.report()["capacity"]["codex"]

    def test_declared_replay_keeps_the_refusal(self):
        policy = types.SimpleNamespace(declared_routes=frozenset({"codex"}))
        autodecide._observe_declared_routes(self.router, policy, client="claude")
        self.assertEqual((self.status()["status"], self.status()["source"]),
                         ("unavailable", capacity_feed.FEED_SOURCE))

    def test_presence_keeps_the_refusal(self):
        self.router.observe_capacity(CapacityObservation(
            "codex", NOW, NOW + 60, True, PRESENCE_SOURCE), trusted=True)
        self.assertEqual(self.status()["status"], "unavailable")

    def test_withdrawn_declaration_does_not_retract_the_refusal(self):
        policy = types.SimpleNamespace(declared_routes=frozenset())
        autodecide._observe_declared_routes(self.router, policy, client="claude")
        self.assertEqual(self.status()["source"], capacity_feed.FEED_SOURCE)

    def test_declaration_resumes_after_the_refusal_expires(self):
        self.clock.now = NOW + 601
        policy = types.SimpleNamespace(declared_routes=frozenset({"codex"}))
        autodecide._observe_declared_routes(self.router, policy, client="claude")
        self.assertEqual((self.status()["status"], self.status()["source"]),
                         ("available", DECLARED_SOURCE))

    def test_feed_source_is_not_a_replayed_source(self):
        self.assertNotIn(capacity_feed.FEED_SOURCE, REPLAYED_SOURCES)


class MainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "router.sqlite3"
        self.dir = Path(self.temp.name) / "snapshots"
        self.dir.mkdir()
        config = types.SimpleNamespace(capacity_db=self.db)
        patcher = mock.patch.object(capacity_feed, "load", return_value=config)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.temp.cleanup()

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = capacity_feed.main(list(argv))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_ceiling_is_required(self):
        code, _, err = self.main("--config", "c.json", "--snapshot-dir", str(self.dir))
        self.assertEqual(code, 2)
        self.assertIn("--ceiling-percent", err)

    def test_bad_ceiling_is_refused(self):
        for value in ("0", "101", "nan", "inf", "ninety"):
            with self.subTest(value=value):
                code, _, _ = self.main("--config", "c.json", "--snapshot-dir", str(self.dir),
                                       "--ceiling-percent", value)
                self.assertEqual(code, 2)

    def test_prints_a_json_summary(self):
        code, out, _ = self.main("--config", "c.json", "--snapshot-dir", str(self.dir),
                                 "--ceiling-percent", "90", "--dry-run")
        self.assertEqual(code, 0)
        summary = json.loads(out)
        self.assertEqual((summary["dry_run"], summary["ceiling_percent"]), (True, 90.0))
        self.assertEqual({r["route"] for r in summary["routes"]}, {"codex", "claude"})

    def test_unreadable_config_fails_closed(self):
        with mock.patch.object(capacity_feed, "load", side_effect=OSError("nope")):
            code, out, err = self.main("--config", "c.json", "--snapshot-dir", str(self.dir),
                                       "--ceiling-percent", "90")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("config unreadable", err)


if __name__ == "__main__":
    unittest.main()
