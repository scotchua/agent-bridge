"""The night-shift helper reports only, keeps the review reserve, and fails closed."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.orchestration import night_shift
from test_capacity_feed import NOW, claude_bucket, codex_bucket, snapshot


class NightShiftTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, source, data):
        text = data if isinstance(data, str) else json.dumps(data)
        (self.dir / f"{source}.json").write_text(text, encoding="utf-8")

    def plan(self, provider="codex", reserve=20, **kwargs):
        kwargs.setdefault("max_units", 5)
        return night_shift.plan_batch(self.dir, provider, reserve, now=NOW, **kwargs)

    def test_no_written_limit_asks_scott(self):
        self.write("codex", snapshot("codex", [codex_bucket(10)]))
        report = self.plan(max_units=None)
        self.assertEqual((report["decision"], report["reason"]), ("ask_scott", "no_written_limit"))

    def test_headroom_under_the_reserve_line(self):
        self.write("codex", snapshot("codex", [codex_bucket(50), codex_bucket(30, scope="secondary",
                                                                           resets_in=7200)]))
        report = self.plan()
        self.assertEqual(report["decision"], "headroom_observed")
        self.assertEqual(report["effective_ceiling_percent"], 80)
        self.assertEqual(report["headroom_percent"], 30)
        self.assertEqual(report["next_reset"], NOW + 3600)
        self.assertEqual(report["account_binding"], "unverified")
        self.assertEqual(report["authority"], "report_only")

    def test_reserve_line_reached_waits_for_the_latest_binding_reset(self):
        self.write("codex", snapshot("codex", [codex_bucket(85), codex_bucket(
            90, scope="secondary", resets_in=7200)]))
        report = self.plan()
        self.assertEqual(report["decision"], "wait_for_reset")
        self.assertEqual(report["not_before"], NOW + 7200)

    def test_batch_ceiling_lower_than_reserve_line_wins(self):
        self.write("claude", snapshot("claude", [claude_bucket(60), claude_bucket(
            10, window="seven_day", resets_in=86400)]))
        report = self.plan("claude", reserve=10, stop_at_percent=50)
        self.assertEqual(report["effective_ceiling_percent"], 50)
        self.assertEqual(report["decision"], "wait_for_reset")
        self.assertEqual(report["not_before"], NOW + 3600)

    def test_time_limit_before_reset_stops(self):
        self.write("codex", snapshot("codex", [codex_bucket(95, resets_in=7200)]))
        report = self.plan(max_units=None, until=NOW + 3600)
        self.assertEqual((report["decision"], report["reason"]),
                         ("stop", "limit_ends_before_reset"))

    def test_time_limit_already_passed_stops(self):
        report = self.plan(until=NOW - 1)
        self.assertEqual((report["decision"], report["reason"]), ("stop", "time_limit_passed"))

    def test_unknown_capacity_holds(self):
        cases = {
            "missing": None,
            "stale": snapshot("codex", [codex_bucket(10)], received_at=NOW - 3600),
            "unbound": snapshot("codex", [codex_bucket(10)], account_binding="verified"),
            "malformed": "{not json",
        }
        for name, data in cases.items():
            with self.subTest(name):
                for path in self.dir.iterdir():
                    path.unlink()
                if data is not None:
                    self.write("codex", data)
                report = self.plan()
                self.assertEqual(report["decision"], "hold")
                self.assertNotIn("headroom_percent", report)

    def test_a_skipped_window_is_never_called_headroom(self):
        for bucket in (codex_bucket(10, scope="secondary", status="unknown (partial)"),
                       codex_bucket(10, scope="secondary", age=3600),
                       codex_bucket(10, scope="secondary", resets_in=-1)):
            with self.subTest(bucket=bucket):
                self.write("codex", snapshot("codex", [codex_bucket(10), bucket]))
                report = self.plan()
                self.assertEqual((report["decision"], report["reason"]),
                                 ("hold", "incomplete_windows"))

    def test_an_omitted_window_is_never_called_headroom(self):
        # NS-F1: a window left out of the snapshot entirely could be the one
        # at the ceiling, and duplicates of one window do not cover another.
        cases = {
            "claude seven_day omitted": ("claude", [claude_bucket(10)]),
            "claude duplicate five_hour": ("claude", [claude_bucket(10), claude_bucket(20)]),
            "codex secondary omitted": ("codex", [codex_bucket(10)]),
            "codex second limit half there": ("codex", [
                codex_bucket(10), codex_bucket(10, scope="secondary"),
                codex_bucket(10, limit="bucket_1")]),
        }
        for name, (provider, buckets) in cases.items():
            with self.subTest(name):
                self.write(provider, snapshot(provider, buckets))
                report = self.plan(provider)
                self.assertEqual((report["decision"], report["reason"]),
                                 ("hold", "incomplete_windows"))
                self.assertNotIn("headroom_percent", report)

    def test_a_skipped_window_does_not_hide_a_binding_one(self):
        self.write("codex", snapshot("codex", [codex_bucket(95), codex_bucket(
            10, scope="secondary", status="unknown (absent)")]))
        report = self.plan()
        self.assertEqual(report["decision"], "wait_for_reset")
        self.assertTrue(report["not_before_is_lower_bound"])

    def test_a_complete_snapshot_gives_an_exact_reset(self):
        self.write("claude", snapshot("claude", [claude_bucket(90), claude_bucket(
            10, window="seven_day", resets_in=86400)]))
        report = self.plan("claude")
        self.assertEqual(report["decision"], "wait_for_reset")
        self.assertFalse(report["not_before_is_lower_bound"])

    def test_bad_inputs_raise(self):
        for kwargs in ({"reserve": 100}, {"reserve": -1}, {"reserve": float("nan")},
                       {"reserve": True}, {"provider": "local"}, {"max_units": 0},
                       {"max_units": True}, {"max_units": 1.5}, {"until": float("inf")},
                       {"stop_at_percent": 0}, {"stop_at_percent": 101}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    self.plan(**kwargs)

    def test_bad_now_raises(self):
        # NS-F2: the clock is an input too.
        for now in (float("inf"), float("nan"), True, -1, "1"):
            with self.subTest(now=now):
                with self.assertRaises(ValueError):
                    night_shift.plan_batch(self.dir, "claude", 20, until=1, now=now)

    def test_writes_nothing(self):
        self.write("codex", snapshot("codex", [codex_bucket(95)]))
        before = {p.name: p.read_bytes() for p in self.dir.iterdir()}
        self.plan()
        self.assertEqual({p.name: p.read_bytes() for p in self.dir.iterdir()}, before)

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(night_shift.time, "time", return_value=NOW):
            try:
                code = night_shift.main(list(argv))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_main_requires_a_reserve(self):
        code, _, err = self.run_main("--snapshot-dir", str(self.dir), "--provider", "codex",
                                     "--max-units", "3")
        self.assertEqual(code, 2)
        self.assertIn("--reserve-percent", err)

    def test_main_reports_json(self):
        self.write("codex", snapshot("codex", [codex_bucket(10), codex_bucket(10, scope="secondary")]))
        code, out, _ = self.run_main("--snapshot-dir", str(self.dir), "--provider", "codex",
                                     "--reserve-percent", "20", "--max-units", "3")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["decision"], "headroom_observed")

    def test_main_rejects_bad_numbers(self):
        for flag, value in (("--reserve-percent", "100"), ("--reserve-percent", "nan"),
                            ("--max-units", "0"), ("--until", "inf"),
                            ("--stop-at-percent", "0")):
            with self.subTest(flag=flag, value=value):
                argv = ["--snapshot-dir", str(self.dir), "--provider", "codex",
                        "--reserve-percent", "20", "--max-units", "3", flag, value]
                self.assertEqual(self.run_main(*argv)[0], 2)


if __name__ == "__main__":
    unittest.main()
