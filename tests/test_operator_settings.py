"""The operator settings file is the one switch for client-derived cloud routes.

Synthetic data only. Every test points the runner state home at a temporary
directory and never touches the real HOME.
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from argparse import Namespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import operator_isolation  # noqa: E402,F401

from agent_bridge import admin, config, operator_settings  # noqa: E402
from agent_bridge.chat.policy import RoomPolicy  # noqa: E402
from agent_bridge.execution import claude_task, codex_task  # noqa: E402
from agent_bridge.orchestration import autoroute  # noqa: E402
from agent_bridge.orchestration import config as orchestration_config  # noqa: E402
from agent_bridge.orchestration import mcp as orchestration_mcp  # noqa: E402
from agent_bridge.orchestration.execution_queue import ExecutionQueue, reserve_nothing  # noqa: E402

POSIX = os.name == "posix"


class StateCase(unittest.TestCase):
    """A temporary state home wired in through the runner's environment variable."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        os.chmod(self.state, 0o700)
        patcher = mock.patch.dict(os.environ, {operator_settings.STATE_ENV: str(self.state)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = self.state / operator_settings.FILE_NAME

    def write(self, document, mode=0o600):
        text = document if isinstance(document, str) else json.dumps(document)
        self.path.write_text(text, encoding="utf-8")
        os.chmod(self.path, mode)

    def routes(self, legacy=None):
        return operator_settings.resolve(legacy=legacy)


class AuthoritativeFileTests(StateCase):
    def test_valid_file_decides(self):
        for listed in ([], ["peer"], ["execution"], ["peer", "execution"]):
            with self.subTest(listed=listed):
                self.write({"version": 1, "client_derived_routes": listed})
                result = self.routes()
                self.assertEqual(result.routes, frozenset(listed))
                self.assertEqual(result.source, "operator_settings")
                self.assertIsNone(result.reason)
                self.assertFalse(result.fail_closed)

    def test_valid_file_ignores_wider_legacy_values(self):
        self.write({"version": 1, "client_derived_routes": ["peer"]})
        result = self.routes(legacy=[{"peer", "execution"}, {"peer", "execution"}])
        self.assertEqual(result.routes, frozenset({"peer"}))
        self.assertEqual(result.source, "operator_settings")

    def test_valid_file_can_turn_legacy_routes_off(self):
        self.write({"version": 1, "client_derived_routes": []})
        self.assertEqual(self.routes(legacy=[{"peer", "execution"}]).routes, frozenset())

    def test_duplicates_collapse(self):
        self.write({"version": 1, "client_derived_routes": ["peer", "peer"]})
        self.assertEqual(self.routes().routes, frozenset({"peer"}))

    def test_runner_key_alone_never_widens(self):
        self.write({"allow_client_derived": True})
        result = self.routes()
        self.assertEqual(result.routes, frozenset())
        self.assertEqual(result.source, "default")
        self.assertEqual(result.reason, "no_client_derived_routes_key")

    def test_runner_key_is_ignored_beside_a_valid_list(self):
        self.write({"version": 1, "client_derived_routes": [], "allow_client_derived": True})
        self.assertEqual(self.routes().routes, frozenset())


class FailClosedTests(StateCase):
    def assertClosed(self, reason, legacy=({"peer", "execution"},)):
        result = self.routes(legacy=list(legacy))
        self.assertEqual(result.routes, frozenset(), result)
        self.assertTrue(result.fail_closed)
        self.assertEqual(result.source, "operator_settings")
        self.assertEqual(result.reason, reason)
        # The call every reader makes never raises either.
        self.assertEqual(operator_settings.client_derived_routes(legacy=list(legacy)), frozenset())

    def test_bad_json(self):
        self.write("{not json")
        self.assertClosed("bad_json")

    def test_invalid_utf8(self):
        self.path.write_bytes(b"\xff\xfe\x00")
        os.chmod(self.path, 0o600)
        self.assertClosed("bad_json" if not POSIX else "unreadable")

    def test_not_an_object(self):
        self.write("[]")
        self.assertClosed("not_an_object")

    def test_unknown_route(self):
        self.write({"version": 1, "client_derived_routes": ["peer", "local"]})
        self.assertClosed("unknown_route")

    def test_wrong_types(self):
        for value in ("peer", {"peer": True}, [1], [None], [["peer"]], True, 1):
            with self.subTest(value=value):
                self.write({"version": 1, "client_derived_routes": value})
                self.assertClosed("wrong_type")

    def test_version_must_be_exactly_one(self):
        for version in (None, 2, "1", True, 1.5, 1.0):
            with self.subTest(version=version):
                document = {"client_derived_routes": ["peer"]}
                if version is not None:
                    document["version"] = version
                self.write(document)
                self.assertClosed("bad_version")

    @unittest.skipUnless(POSIX, "mode bits and symlinks")
    def test_symlink_file(self):
        target = self.root / "elsewhere.json"
        target.write_text(json.dumps({"version": 1, "client_derived_routes": ["peer"]}))
        os.chmod(target, 0o600)
        os.symlink(target, self.path)
        self.assertClosed("unreadable")

    @unittest.skipUnless(POSIX, "mode bits and symlinks")
    def test_dangling_symlink_file_is_not_treated_as_absent(self):
        os.symlink(self.root / "missing.json", self.path)
        self.assertClosed("unreadable")

    def test_unsupported_version_without_the_routes_key_does_not_fall_back(self):
        for version in (999, 1.0, "1", True):
            with self.subTest(version=version):
                self.write({"version": version})
                self.assertClosed("bad_version")

    @unittest.skipUnless(POSIX and hasattr(os, "geteuid") and os.geteuid() != 0, "mode bits")
    def test_unsearchable_state_directory_is_not_treated_as_absent(self):
        self.write({"version": 1, "client_derived_routes": []})
        os.chmod(self.state, 0o000)
        self.addCleanup(os.chmod, self.state, 0o700)
        self.assertClosed("unreadable")

    @unittest.skipUnless(POSIX, "symlinks")
    def test_symlinked_state_directory_is_refused(self):
        real = self.root / "real-state"
        real.mkdir(mode=0o700)
        target = real / operator_settings.FILE_NAME
        target.write_text(json.dumps({"version": 1, "client_derived_routes": ["peer"]}))
        os.chmod(target, 0o600)
        linked = self.root / "linked-state"
        os.symlink(real, linked)
        with mock.patch.dict(os.environ, {operator_settings.STATE_ENV: str(linked)}):
            self.assertClosed("unreadable")

    def test_descriptor_errors_fail_closed(self):
        self.write({"version": 1, "client_derived_routes": ["peer"]})
        for name in ("fstat", "read"):
            with self.subTest(name=name), \
                    mock.patch.object(operator_settings.os, name, side_effect=OSError("synthetic")):
                self.assertClosed("unreadable")

    @unittest.skipUnless(operator_settings._HAS_SAFE_OPEN, "descriptor-relative open")
    def test_close_errors_fail_closed_and_release_both_descriptors(self):
        self.write({"version": 1, "client_derived_routes": ["peer"]})
        real_close = os.close
        for failing in (0, 1):
            closed = []

            def close(fd, failing=failing):
                closed.append(fd)
                real_close(fd)
                if (len(closed) - 1) % 2 == failing:
                    raise OSError("synthetic")

            with self.subTest(failing=failing), \
                    mock.patch.object(operator_settings.os, "close", side_effect=close):
                self.assertClosed("unreadable")
            # Each read opens the folder and the file; every descriptor was
            # still closed although one close reported an error.
            self.assertEqual(len(closed) % 2, 0)

    @unittest.skipUnless(operator_settings._HAS_SAFE_OPEN, "descriptor-relative open")
    def test_close_error_after_absent_file_fails_closed(self):
        with mock.patch.object(operator_settings.os, "close", side_effect=OSError("synthetic")):
            self.assertClosed("unreadable")

    @unittest.skipUnless(POSIX, "mode bits")
    def test_close_error_keeps_the_first_refusal(self):
        self.write({"version": 1, "client_derived_routes": ["peer"]}, mode=0o620)
        real_close = os.close

        def close(fd):
            real_close(fd)
            raise OSError("synthetic")

        with mock.patch.object(operator_settings.os, "close", side_effect=close):
            self.assertClosed("untrusted")

    @unittest.skipUnless(POSIX, "mode bits")
    def test_group_writable_file(self):
        self.write({"version": 1, "client_derived_routes": ["peer"]}, mode=0o620)
        self.assertClosed("untrusted")

    @unittest.skipUnless(POSIX, "mode bits")
    def test_other_writable_file(self):
        self.write({"version": 1, "client_derived_routes": ["peer"]}, mode=0o602)
        self.assertClosed("untrusted")

    @unittest.skipUnless(POSIX, "mode bits")
    def test_group_writable_directory(self):
        self.write({"version": 1, "client_derived_routes": ["peer"]})
        os.chmod(self.state, 0o770)
        self.addCleanup(os.chmod, self.state, 0o700)
        self.assertClosed("untrusted")

    @unittest.skipUnless(POSIX, "mode bits")
    def test_other_writable_directory(self):
        self.write({"version": 1, "client_derived_routes": ["peer"]})
        os.chmod(self.state, 0o707)
        self.addCleanup(os.chmod, self.state, 0o700)
        self.assertClosed("untrusted")

    @unittest.skipUnless(POSIX, "mode bits")
    def test_group_readable_file_is_still_trusted(self):
        self.write({"version": 1, "client_derived_routes": ["peer"]}, mode=0o640)
        self.assertEqual(self.routes().routes, frozenset({"peer"}))

    @unittest.skipUnless(POSIX, "uid")
    def test_foreign_owner_file_and_directory(self):
        self.write({"version": 1, "client_derived_routes": ["peer"]})
        with mock.patch.object(os, "getuid", return_value=os.getuid() + 1):
            self.assertClosed("untrusted")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "fifo")
    def test_fifo_does_not_hang_and_is_refused(self):
        os.mkfifo(self.path, 0o600)
        self.assertClosed("untrusted")

    @unittest.skipUnless(POSIX, "directories")
    def test_directory_in_place_of_file(self):
        os.mkdir(self.path, 0o700)
        self.assertClosed("untrusted")

    def test_platform_without_safe_open_refuses_a_present_file(self):
        self.write({"version": 1, "client_derived_routes": ["peer"]})
        with mock.patch.object(operator_settings, "_HAS_SAFE_OPEN", False):
            self.assertClosed("unsupported_platform")

    def test_platform_without_safe_open_still_allows_legacy_when_absent(self):
        with mock.patch.object(operator_settings, "_HAS_SAFE_OPEN", False):
            result = self.routes(legacy=[{"peer"}])
        self.assertEqual(result.routes, frozenset({"peer"}))
        self.assertEqual(result.source, "legacy")

    def test_bad_file_beats_a_legacy_opt_in_in_every_reader(self):
        self.write("{")
        cfg = config.Config(_raw_with_routes(["peer", "execution"]), config.DEFAULT_CONFIG_PATH)
        self.assertEqual(cfg.client_derived_routes, frozenset())
        policy = autoroute.parse_policy(
            {"version": autoroute.POLICY_VERSION, "client_derived_routes": ["execution"]})
        self.assertEqual(policy.client_derived_routes, frozenset())
        self.assertNotIn("client_derived", policy.peer_classifications)

    def test_deciding_file_outranks_a_malformed_legacy_key(self):
        self.write({"version": 1, "client_derived_routes": ["peer"]})
        cfg = config.Config(_raw_with_routes(["bogus"]), config.DEFAULT_CONFIG_PATH)
        self.assertEqual(cfg.client_derived_routes, frozenset({"peer"}))
        policy = autoroute.parse_policy(
            {"version": autoroute.POLICY_VERSION, "client_derived_routes": "bogus"})
        self.assertEqual(policy.client_derived_routes, frozenset({"peer"}))

    def test_malformed_legacy_key_still_raises_on_the_fallback(self):
        with self.assertRaises(ValueError):
            config.Config(_raw_with_routes(["bogus"]), config.DEFAULT_CONFIG_PATH)
        with self.assertRaises(autoroute.PolicyError):
            autoroute.parse_policy(
                {"version": autoroute.POLICY_VERSION, "client_derived_routes": "bogus"})


class LegacyTransitionTests(StateCase):
    def test_absent_file_uses_the_intersection(self):
        result = self.routes(legacy=[{"peer", "execution"}, {"execution"}, {"execution", "peer"}])
        self.assertEqual(result.routes, frozenset({"execution"}))
        self.assertEqual(result.source, "legacy")
        self.assertEqual(result.reason, "absent")

    def test_disjoint_legacy_values_give_nothing_not_the_union(self):
        result = self.routes(legacy=[{"peer"}, {"execution"}])
        self.assertEqual(result.routes, frozenset())
        self.assertEqual(result.source, "legacy")

    def test_one_holder_with_nothing_turns_the_route_off(self):
        self.assertEqual(self.routes(legacy=[{"peer", "execution"}, set()]).routes, frozenset())

    def test_single_holder_is_its_own_intersection(self):
        self.assertEqual(self.routes(legacy=[["peer"]]).routes, frozenset({"peer"}))

    def test_file_without_the_key_falls_back(self):
        self.write({"version": 1})
        result = self.routes(legacy=[{"peer"}, {"peer", "execution"}])
        self.assertEqual(result.routes, frozenset({"peer"}))
        self.assertEqual(result.source, "legacy")
        self.assertEqual(result.reason, "no_client_derived_routes_key")

    def test_nothing_passed_is_the_default(self):
        for legacy in (None, []):
            result = self.routes(legacy=legacy)
            self.assertEqual(result.routes, frozenset())
            self.assertEqual(result.source, "default")

    def test_runner_key_does_not_widen_legacy(self):
        self.write({"allow_client_derived": True})
        self.assertEqual(self.routes(legacy=[set()]).routes, frozenset())
        self.assertEqual(self.routes(legacy=[{"peer"}]).routes, frozenset({"peer"}))


class StateHomeTests(unittest.TestCase):
    def test_environment_override_wins(self):
        with tempfile.TemporaryDirectory() as home:
            os.mkdir(os.path.join(home, ".codex-bridge"))
            found = operator_settings.state_home(home, {operator_settings.STATE_ENV: "/synthetic/state"})
            self.assertEqual(found, "/synthetic/state")

    def test_new_name_unless_only_the_legacy_one_exists(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(operator_settings.state_home(home, {}),
                             os.path.join(home, ".codex-job-runner"))
            os.mkdir(os.path.join(home, ".codex-bridge"))
            self.assertEqual(operator_settings.state_home(home, {}),
                             os.path.join(home, ".codex-bridge"))
            os.mkdir(os.path.join(home, ".codex-job-runner"))
            self.assertEqual(operator_settings.state_home(home, {}),
                             os.path.join(home, ".codex-job-runner"))

    @unittest.skipUnless(POSIX, "symlink")
    def test_migrated_state_home_symlink_is_followed(self):
        with tempfile.TemporaryDirectory() as home:
            real = os.path.join(home, ".codex-job-runner")
            os.mkdir(real, 0o700)
            os.symlink(real, os.path.join(home, ".codex-bridge"))
            path = operator_settings.settings_path(home, {})
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "client_derived_routes": ["execution"]}, handle)
            os.chmod(path, 0o600)
            self.assertEqual(operator_settings.client_derived_routes(home, {}),
                             frozenset({"execution"}))

    def test_default_home_is_used_only_when_asked(self):
        # An explicit home never reads the real one.
        with tempfile.TemporaryDirectory() as home:
            result = operator_settings.resolve(home, {})
            self.assertTrue(result.path.startswith(home))
            self.assertEqual(result.reason, "absent")


def _raw_with_routes(routes):
    raw = copy.deepcopy(config.load().raw)
    raw["client_derived_routes"] = list(routes)
    return raw


class ReaderAgreementTests(StateCase):
    """Every former reader gives the same answer from one call path."""

    def setUp(self):
        super().setUp()
        self.state_root = self.root / "orch-state"

    def orchestration(self, legacy):
        path = self.root / "orchestration.json"
        document = {
            "config_version": "1", "state_root": str(self.state_root),
            "local_queue_root": str(self.state_root / "local"),
            "capacity_db": str(self.state_root / "capacity.sqlite3"),
            "worker_executable": str(self.root / "worker"),
            "worker_state": str(self.state_root / "worker-state")}
        if legacy is not None:
            document["client_derived_routes"] = legacy
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def readers(self, broker_legacy=(), orchestration_legacy=None, policy_legacy=None):
        """What each reader concludes, as {name: frozenset of routes}."""
        cfg = config.Config(_raw_with_routes(broker_legacy), config.DEFAULT_CONFIG_PATH)
        orchestration = orchestration_config.load(self.orchestration(orchestration_legacy))
        document = {"version": autoroute.POLICY_VERSION}
        if policy_legacy is not None:
            document["client_derived_routes"] = policy_legacy
        policy = autoroute.parse_policy(document)
        queue = ExecutionQueue(self.root / "queue", None, model_reserved=reserve_nothing,
                               client_derived_routes=orchestration.client_derived_routes)
        found = {
            "broker_config": cfg.client_derived_routes,
            "orchestration_config": orchestration.client_derived_routes,
            "routing_policy": policy.client_derived_routes,
            "execution_queue": queue.client_derived_routes,
        }
        # Derived behavior, not just the stored set.
        found["peer_classifications"] = frozenset(
            {"peer"} if "client_derived" in cfg.peer_allowed_classifications("codex") else set())
        found["room_policy"] = frozenset({"peer"} if RoomPolicy(cfg).allow_client else set())
        found["policy_classifications"] = frozenset(
            {"execution"} if "client_derived" in policy.peer_classifications else set())
        return found, policy, queue

    def test_authoritative_file_gives_every_reader_the_same_answer(self):
        for listed in ([], ["peer"], ["execution"], ["peer", "execution"]):
            with self.subTest(listed=listed):
                self.write({"version": 1, "client_derived_routes": listed})
                # Legacy values are deliberately wide and disagree.
                found, policy, queue = self.readers(
                    broker_legacy=["peer", "execution"], orchestration_legacy=["peer"],
                    policy_legacy=["execution"])
                expected = frozenset(listed)
                for name in ("broker_config", "orchestration_config", "routing_policy",
                             "execution_queue"):
                    self.assertEqual(found[name], expected, name)
                self.assertEqual(found["peer_classifications"], expected & {"peer"})
                self.assertEqual(found["room_policy"], expected & {"peer"})
                self.assertEqual(found["policy_classifications"], expected & {"execution"})
                # The old mismatch refusal cannot fire when the file decides.
                allowed = orchestration_mcp._execution_route_classifications(policy, queue)
                self.assertEqual("client_derived" in allowed, "execution" in expected)

    def test_legacy_readers_agree_on_the_intersection_when_nothing_is_written(self):
        found, policy, queue = self.readers(
            broker_legacy=["peer", "execution"], orchestration_legacy=["peer", "execution"],
            policy_legacy=["execution", "peer"])
        for name in ("broker_config", "orchestration_config", "routing_policy"):
            self.assertEqual(found[name], frozenset({"peer", "execution"}), name)
        self.assertIn("client_derived",
                      orchestration_mcp._execution_route_classifications(policy, queue))

    def test_lone_legacy_execution_opt_in_still_refuses_loudly(self):
        # The mismatch guard is kept for the legacy fallback only.
        _, policy, queue = self.readers(
            broker_legacy=[], orchestration_legacy=["execution"], policy_legacy=[])
        with self.assertRaisesRegex(Exception, "client_derived_routes_mismatch"):
            orchestration_mcp._execution_route_classifications(policy, queue)

    def test_default_is_closed_everywhere(self):
        found, policy, queue = self.readers()
        for value in found.values():
            self.assertEqual(value, frozenset())
        self.assertNotIn("client_derived",
                         orchestration_mcp._execution_route_classifications(policy, queue))

    def _harness_reaches_path_checks(self, routes_argument):
        """True when the client-derived admission check passed in both harnesses."""
        outcomes = []
        for runner in (
                lambda: claude_task.run_task(
                    brief=Path("brief"), repo=Path("repo"), task_root=Path("tasks"),
                    claude_bin=Path("claude"), claude_config_dir=Path("config"),
                    classification="client_derived", model="fixture", effort="low",
                    client_derived_routes=routes_argument, verify_argv=[]),
                lambda: codex_task._run_admitted(
                    brief=Path("brief"), repo=Path("repo"), task_root=Path("tasks"),
                    codex_bin=Path("codex"), admission=None, codex_home=Path("home"),
                    classification="client_derived", model="fixture", reasoning_effort="low",
                    client_derived_routes=routes_argument, verify_argv=[])):
            with self.assertRaises(Exception) as caught:
                runner()
            outcomes.append("refuses client-derived" not in str(caught.exception))
        return outcomes

    def test_task_harnesses_read_the_file_not_the_argument(self):
        self.write({"version": 1, "client_derived_routes": ["execution"]})
        self.assertEqual(self._harness_reaches_path_checks(()), [True, True])
        self.write({"version": 1, "client_derived_routes": ["peer"]})
        self.assertEqual(self._harness_reaches_path_checks(("execution",)), [False, False])
        self.write("{")
        self.assertEqual(self._harness_reaches_path_checks(("execution",)), [False, False])

    def test_task_harnesses_keep_the_argument_as_the_legacy_fallback(self):
        self.assertEqual(self._harness_reaches_path_checks(("execution",)), [True, True])
        self.assertEqual(self._harness_reaches_path_checks(()), [False, False])


class MigrationTests(StateCase):
    def setUp(self):
        super().setUp()
        self.orch_state = self.root / "orch-state"
        (self.orch_state / "routing").mkdir(parents=True)
        self.orch_path = self.root / "orchestration.json"
        patcher = mock.patch.object(admin.drift, "discover_config",
                                    side_effect=lambda *a, **k: str(self.orch_path)
                                    if self.orch_path.exists() else None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def legacy(self, broker, orchestration=None, policy=None):
        cfg = config.Config(_raw_with_routes(broker), config.DEFAULT_CONFIG_PATH)
        if orchestration is not None:
            document = {"config_version": "1", "state_root": str(self.orch_state),
                        "client_derived_routes": orchestration}
            self.orch_path.write_text(json.dumps(document), encoding="utf-8")
        if policy is not None:
            (self.orch_state / "routing" / autoroute.POLICY_FILE).write_text(
                json.dumps({"version": autoroute.POLICY_VERSION,
                            "client_derived_routes": policy}), encoding="utf-8")
        return cfg

    def run_cli(self, cfg, *flags):
        args = admin.argparse.ArgumentParser()
        args.add_argument("--dry-run", action="store_true")
        args.add_argument("--write", action="store_true")
        namespace = args.parse_args(list(flags))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = admin.cmd_operator_settings(cfg, namespace)
        return code, json.loads(out.getvalue())

    def test_dry_run_is_the_default_and_writes_nothing(self):
        cfg = self.legacy(["peer", "execution"], ["peer", "execution"], ["execution"])
        code, plan = self.run_cli(cfg)
        self.assertEqual(code, 0)
        self.assertEqual(plan["mode"], "dry-run")
        self.assertEqual(plan["routes"], ["execution"])
        self.assertFalse(plan["written"])
        self.assertFalse(self.path.exists())
        self.assertEqual(set(plan["legacy"]),
                         {"broker_config", "orchestration_config", "routing_policy"})

    def test_explicit_dry_run_flag(self):
        cfg = self.legacy(["peer"])
        code, plan = self.run_cli(cfg, "--dry-run")
        self.assertEqual((code, plan["mode"], plan["written"]), (0, "dry-run", False))
        self.assertFalse(self.path.exists())

    def test_intersection_never_union(self):
        cfg = self.legacy(["peer"], ["execution"], ["peer", "execution"])
        _, plan = self.run_cli(cfg)
        self.assertEqual(plan["routes"], [])

    def test_absent_sources_hold_no_opinion(self):
        cfg = self.legacy(["peer", "execution"])
        _, plan = self.run_cli(cfg)
        self.assertEqual(plan["routes"], ["execution", "peer"])
        self.assertEqual(set(plan["legacy"]), {"broker_config"})

    def test_malformed_legacy_value_counts_as_empty(self):
        cfg = self.legacy(["peer", "execution"], None, None)
        self.orch_path.write_text(json.dumps({
            "config_version": "1", "state_root": str(self.orch_state),
            "client_derived_routes": ["bogus"]}), encoding="utf-8")
        _, plan = self.run_cli(cfg)
        self.assertEqual(plan["routes"], [])
        self.assertIn("note", plan["legacy"]["orchestration_config"])

    def test_present_bad_policy_file_counts_as_empty(self):
        cfg = self.legacy(["peer", "execution"], ["peer", "execution"])
        (self.orch_state / "routing" / autoroute.POLICY_FILE).write_text("{", encoding="utf-8")
        code, plan = self.run_cli(cfg)
        self.assertEqual(code, 0)
        self.assertEqual(plan["routes"], [])
        self.assertIn("note", plan["legacy"]["routing_policy"])

    @unittest.skipUnless(POSIX and hasattr(os, "geteuid") and os.geteuid() != 0, "mode bits")
    def test_unreadable_orchestration_config_blocks(self):
        cfg = self.legacy(["peer", "execution"], ["peer", "execution"])
        os.chmod(self.orch_path, 0o000)
        self.addCleanup(os.chmod, self.orch_path, 0o600)
        code, plan = self.run_cli(cfg, "--write")
        self.assertEqual(code, 2)
        self.assertFalse(plan["written"])
        self.assertEqual(plan["blocked_by"], "orchestration_config_unreadable")
        self.assertFalse(self.path.exists())

    def test_blocked_dry_run_exits_nonzero(self):
        self.write({"version": 1, "client_derived_routes": []})
        code, plan = self.run_cli(self.legacy(["peer"]))
        self.assertEqual((code, plan["mode"], plan["writable"]), (2, "dry-run", False))

    def test_file_that_appears_during_migration_is_not_overwritten(self):
        # The file appears after the plan and immediately before the publish,
        # with no further check in between: only the no-replace link stops it.
        cfg = self.legacy(["peer"])
        real_link = os.link

        def link(source, destination):
            self.write({"version": 1, "client_derived_routes": []})
            return real_link(source, destination)

        with mock.patch.object(operator_settings.os, "link", side_effect=link):
            code, plan = self.run_cli(cfg, "--write")
        self.assertEqual(code, 2)
        self.assertFalse(plan["written"])
        self.assertEqual(plan["blocked_by"], "changed_during_migration")
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")),
                         {"version": 1, "client_derived_routes": []})
        leftovers = [name for name in os.listdir(self.state) if name.startswith(".tmp-")]
        self.assertEqual(leftovers, [])

    def test_runner_file_that_appears_during_migration_is_not_overwritten(self):
        cfg = self.legacy(["peer"])
        real_link = os.link

        def link(source, destination):
            self.write({"allow_client_derived": False})
            return real_link(source, destination)

        with mock.patch.object(operator_settings.os, "link", side_effect=link):
            code, plan = self.run_cli(cfg, "--write")
        self.assertEqual((code, plan["blocked_by"]), (2, "changed_during_migration"))
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")),
                         {"allow_client_derived": False})

    def test_create_never_replaces(self):
        self.write({"allow_client_derived": True})
        before = self.path.read_bytes()
        with self.assertRaises(FileExistsError):
            operator_settings.create(["peer"])
        self.assertEqual(self.path.read_bytes(), before)
        leftovers = [name for name in os.listdir(self.state) if name.startswith(".tmp-")]
        self.assertEqual(leftovers, [])

    @unittest.skipUnless(POSIX, "mode bits")
    def test_write_creates_a_private_file_the_loader_trusts(self):
        cfg = self.legacy(["peer", "execution"], ["execution"], ["execution", "peer"])
        code, plan = self.run_cli(cfg, "--write")
        self.assertEqual((code, plan["written"]), (0, True))
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        document = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(document, {"version": 1, "client_derived_routes": ["execution"]})
        self.assertEqual(operator_settings.resolve().source, "operator_settings")
        self.assertEqual(operator_settings.client_derived_routes(), frozenset({"execution"}))
        leftovers = [name for name in os.listdir(self.state) if name.startswith(".tmp-")]
        self.assertEqual(leftovers, [])

    def test_dry_run_plans_an_upgrade_of_the_runner_file(self):
        self.write({"allow_client_derived": True})
        before = self.path.read_bytes()
        code, plan = self.run_cli(self.legacy(["peer"]))
        self.assertEqual((code, plan["writable"], plan["action"]), (0, True, "upgrade"))
        self.assertIs(plan["preserved_allow_client_derived"], True)
        self.assertNotIn("_expected", plan)
        self.assertEqual(self.path.read_bytes(), before)

    @unittest.skipUnless(POSIX, "mode bits")
    def test_runner_file_is_upgraded_keeping_its_value(self):
        for value in (True, False):
            with self.subTest(value=value):
                self.write({"allow_client_derived": value})
                code, plan = self.run_cli(self.legacy(["peer"]), "--write")
                self.assertEqual((code, plan["written"], plan["action"]), (0, True, "upgrade"))
                self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")),
                                 {"version": 1, "client_derived_routes": ["peer"],
                                  "allow_client_derived": value})
                self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
                self.assertEqual(operator_settings.client_derived_routes(), frozenset({"peer"}))
                leftovers = [n for n in os.listdir(self.state) if n.startswith(".tmp-")]
                self.assertEqual(leftovers, [])

    def test_runner_file_with_other_keys_needs_a_manual_merge(self):
        self.write({"allow_client_derived": True, "other": 1})
        before = self.path.read_bytes()
        for flags in ((), ("--write",)):
            with self.subTest(flags=flags):
                code, plan = self.run_cli(self.legacy(["peer"]), *flags)
                self.assertEqual((code, plan["written"]), (2, False))
                self.assertEqual(plan["blocked_by"], "existing_file_needs_manual_merge")
                self.assertEqual(self.path.read_bytes(), before)

    def test_runner_file_changed_before_the_replace_is_left_alone(self):
        # The change lands after the first comparison and before the second.
        self.write({"allow_client_derived": True})
        real_fsync = os.fsync
        calls = []

        def fsync(fd):
            if not calls:
                calls.append(fd)
                self.write({"allow_client_derived": False})
            return real_fsync(fd)

        with mock.patch.object(operator_settings.os, "fsync", side_effect=fsync):
            code, plan = self.run_cli(self.legacy(["peer"]), "--write")
        self.assertEqual((code, plan["blocked_by"]), (2, "changed_during_migration"))
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")),
                         {"allow_client_derived": False})
        leftovers = [n for n in os.listdir(self.state) if n.startswith(".tmp-")]
        self.assertEqual(leftovers, [])

    def test_upgrade_refuses_a_file_that_is_not_the_runners(self):
        with self.assertRaises(ValueError):
            operator_settings.upgrade(["peer"], '{"version": 1, "client_derived_routes": []}')

    def test_write_refuses_to_overwrite_a_deciding_file(self):
        self.write({"version": 1, "client_derived_routes": []})
        before = self.path.read_bytes()
        cfg = self.legacy(["peer", "execution"])
        code, plan = self.run_cli(cfg, "--write")
        self.assertEqual(code, 2)
        self.assertFalse(plan["written"])
        self.assertEqual(plan["blocked_by"], "already_migrated")
        self.assertEqual(self.path.read_bytes(), before)

    def test_write_refuses_to_overwrite_a_malformed_file(self):
        self.write("{")
        before = self.path.read_bytes()
        cfg = self.legacy(["peer", "execution"])
        code, plan = self.run_cli(cfg, "--write")
        self.assertEqual(code, 2)
        self.assertFalse(plan["written"])
        self.assertEqual(plan["blocked_by"], "existing_file_bad_json")
        self.assertEqual(self.path.read_bytes(), before)

    def test_main_parses_the_subcommand_and_defaults_to_dry_run(self):
        cfg = self.legacy(["peer"])
        out = io.StringIO()
        with mock.patch.object(admin, "load_config", return_value=cfg), \
                contextlib.redirect_stdout(out):
            self.assertEqual(admin.main(["operator-settings", "migrate"]), 0)
        self.assertEqual(json.loads(out.getvalue())["mode"], "dry-run")
        self.assertFalse(self.path.exists())
        with mock.patch.object(admin, "load_config", return_value=cfg), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                admin.main(["operator-settings", "migrate", "--dry-run", "--write"])

    def test_render_and_write_reject_unknown_routes(self):
        with self.assertRaises(ValueError):
            operator_settings.create(["peer", "local"])
        self.assertFalse(self.path.exists())


class OnboardWritesTheFileTests(StateCase):
    def test_new_installs_carry_no_legacy_keys(self):
        from agent_bridge import onboard
        answers = {"privacy": {"mode": "baseline", "peers": {}}, "client_derived_routes": ["peer"]}
        for mode in ("baseline", "strict"):
            answers["privacy"]["mode"] = mode
            self.assertNotIn("client_derived_routes", onboard.privacy_overlay(answers))
        answers["privacy"] = {"mode": "custom", "peers": {"claude": ["public"], "codex": ["public"]}}
        self.assertNotIn("client_derived_routes", onboard.privacy_overlay(answers))
        scaffold = json.loads(onboard._routing_policy_scaffold())
        self.assertNotIn("client_derived_routes", scaffold)

    def test_orchestration_config_builder_omits_the_legacy_key(self):
        from agent_bridge.orchestration import delegation
        with tempfile.TemporaryDirectory() as home:
            cfg = delegation.build_config(home, str(ROOT), local_worker_executable=None)
        self.assertNotIn("client_derived_routes", cfg)

    def test_paths_name_the_operator_file_in_the_state_home(self):
        from agent_bridge import onboard
        with tempfile.TemporaryDirectory() as home:
            paths = onboard._paths(home, None, str(ROOT))
            self.assertEqual(paths["operator_settings"],
                             os.path.join(home, ".codex-job-runner", "operator-settings.json"))

    @unittest.skipUnless(POSIX, "mode bits")
    def test_rendered_bytes_round_trip_through_the_trusted_reader(self):
        data = operator_settings.render(["execution", "peer"], True)
        self.write(data.decode("utf-8"))
        result = self.routes()
        self.assertEqual(result.routes, frozenset({"peer", "execution"}))
        self.assertEqual(json.loads(data)["allow_client_derived"], True)


if __name__ == "__main__":
    unittest.main()
