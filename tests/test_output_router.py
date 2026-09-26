"""Pre-context Bash output routing: narrow rewrite and safe local fallback."""

from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from agent_bridge.localq.intake import AutomaticIntake, IntakePolicy
from agent_bridge.localq.spool import FakeBackend, LocalQueue, ResourceSnapshot
from agent_bridge.orchestration import autoroute, gate, output_router


class GoodSampler:
    def sample(self):
        return ResourceSnapshot(0.0, "normal", "normal", True, 10_000, cpu_load_ratio=0.1)


class BusySampler:
    def sample(self):
        return ResourceSnapshot(0.0, "high", "normal", True, 10_000, cpu_load_ratio=0.1)


class OutputRouterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.state = self.base / "state"
        self.repo = self.base / "repo"
        (self.repo / ".git").mkdir(parents=True)
        (self.state / "routing").mkdir(parents=True)
        self.policy_path = self.state / "routing" / "routing-policy.json"
        self.policy_path.write_text(json.dumps({
            "version": 1,
            "local_first": {"inline_output_router": True},
            "repos": {str(self.repo): {"classification": "internal_nonclient",
                                         "allowed_routes": ["local"], "mechanical_ok": True}},
        }), encoding="utf-8")

    def queue(self, sampler=GoodSampler(), backend=None):
        queue = LocalQueue(self.base / "queue", sampler=sampler,
                           backend=backend or FakeBackend(lambda _p: {"output": {"text": "digest"}}),
                           clock=lambda: 0.0)
        return queue, AutomaticIntake(queue, policy=IntakePolicy(min_input_chars=1, min_nonblank_lines=1),
                                      clock=lambda: 0.0)

    def rows(self):
        path = Path(output_router.ledger_path(str(self.state)))
        return [] if not path.exists() else [json.loads(line) for line in path.read_text().splitlines()]

    def test_routable_and_never_wrap_classes(self):
        for command in ("pytest -q", "python -m unittest", "npm test", "go test ./...",
                        "cargo test", "git log --oneline", "git diff", "git show HEAD",
                        "grep -r token .", "rg token .", "npm run build", "ruff check ."):
            self.assertTrue(output_router.is_routable_command(command), command)
        for command in ("sed -n '1,20p' a", "head a", "tail a", "cat a", "grep -n x a",
                        "git diff -- a", "git show -- a", "pytest -q | tee out", "pytest > out",
                        "cat <<'EOF'", "tail -f log"):
            self.assertFalse(output_router.is_routable_command(command), command)

    def test_update_requires_opt_in_admission_and_an_allowed_gate_decision(self):
        payload = {"tool_name": "Bash", "cwd": str(self.repo), "tool_input": {"command": "pytest -q", "timeout": 10}}
        allowed = gate.Decision("allow", "routing_receipt_valid", "ok")
        updated = gate.inline_output_update("claude", payload, allowed, state_root=str(self.state), config_path="/tmp/config")
        self.assertEqual(updated["timeout"], 10)
        self.assertIn("-m agent_bridge.orchestration.output_router run", updated["command"])
        self.assertIn("/bin/sh -c", updated["command"])
        wire = gate.hook_output(allowed, updated)
        self.assertEqual(wire["hookSpecificOutput"]["updatedInput"], updated)
        self.assertIsNone(gate.inline_output_update("claude", payload,
                                                     gate.Decision("deny", "routed_elsewhere", "no"),
                                                     state_root=str(self.state), config_path="/tmp/config"))
        self.policy_path.write_text(json.dumps({"version": 1, "repos": {str(self.repo): {
            "classification": "internal_nonclient", "allowed_routes": ["local"], "mechanical_ok": True}}}))
        self.assertIsNone(gate.inline_output_update("claude", payload, allowed,
                                                     state_root=str(self.state), config_path="/tmp/config"))

    def test_inline_router_flag_is_strict_boolean_and_defaults_off(self):
        self.assertFalse(autoroute.parse_policy({"version": 1, "repos": {}}).local_first.inline_output_router)
        with self.assertRaises(autoroute.PolicyError):
            autoroute.parse_policy({"version": 1, "repos": {},
                                    "local_first": {"inline_output_router": "yes"}})

    def test_rewrite_is_idempotent_and_worktree_inherits_main_entry(self):
        original = "pytest -q"
        wrapped = f"python -m agent_bridge.orchestration.output_router run --config c -- /bin/sh -c '{original}'"
        self.assertFalse(output_router.is_routable_command(wrapped))
        worktree = self.base / "review"
        admin = self.repo / ".git" / "worktrees" / "review"
        admin.mkdir(parents=True)
        worktree.mkdir()
        (worktree / ".git").write_text(f"gitdir: {admin}")
        (admin / "gitdir").write_text(str(worktree / ".git"))
        payload = {"tool_name": "Bash", "cwd": str(worktree), "tool_input": {"command": original}}
        self.assertIsNotNone(gate.inline_output_update("claude", payload, gate.Decision("allow", "x", "x"),
                                                        state_root=str(self.state), config_path="/tmp/c"))

    def test_small_output_is_exact_and_nonzero_command_status_is_caller_owned(self):
        queue, intake = self.queue()
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            output_router.route_output(b"small\n", state_root=str(self.state), repo=str(self.repo),
                                       classification="internal_nonclient", intake=intake, exit_code=7)
        self.assertEqual(captured.getvalue(), "small\n")
        self.assertEqual(self.rows()[-1]["waiver_reason"], "below_threshold")

    def test_wrapper_preserves_a_nonzero_original_exit_code(self):
        # A deliberately unusable config exercises the wrapper's own waiver
        # path after the child has run; its status must remain the child's.
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            code = output_router.run_command(["/bin/sh", "-c", "printf child; exit 7"],
                                             config_path=str(self.base / "missing.json"))
        self.assertEqual(code, 7)
        self.assertTrue(captured.getvalue().endswith("child"))

    def test_routed_output_has_header_digest_failure_tail_and_private_capture(self):
        queue, intake = self.queue()
        source = (b"ordinary line\n" * 400) + b"FAILED important assertion\n"
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            output_router.route_output(source, state_root=str(self.state), repo=str(self.repo),
                                       classification="internal_nonclient", intake=intake,
                                       executor=lambda: queue.run_once("test"), exit_code=3)
        text = captured.getvalue()
        self.assertIn("[local digest of", text)
        self.assertIn("exit 3]", text)
        self.assertIn("digest", text)
        self.assertIn("FAILED important assertion", text)
        path = next(output_router.output_directory(str(self.state)).glob("*.output"))
        self.assertEqual(path.read_bytes(), source)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertTrue(self.rows()[-1]["routed"])

    def test_deferral_and_timeout_waive_to_raw_output(self):
        source = b"x\n" * 3000
        _queue, intake = self.queue(BusySampler())
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            output_router.route_output(source, state_root=str(self.state), repo=str(self.repo),
                                       classification="internal_nonclient", intake=intake)
        self.assertTrue(captured.getvalue().endswith(source.decode()))
        self.assertIn("admission_deferred", captured.getvalue())
        self.assertIn("admission_deferred", self.rows()[-1]["waiver_reason"])
        queue, intake = self.queue()
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            output_router.route_output(source, state_root=str(self.state), repo=str(self.repo),
                                       classification="internal_nonclient", intake=intake, wait_seconds=0)
        self.assertTrue(captured.getvalue().endswith(source.decode()))
        self.assertEqual(self.rows()[-1]["waiver_reason"], "timeout")

    def test_emitted_command_runs_from_a_plain_shell_without_agent_bridge_on_its_path(self):
        import subprocess
        payload = {"tool_name": "Bash", "cwd": str(self.repo),
                   "tool_input": {"command": "pytest -q"}}
        updated = gate.inline_output_update("claude", payload, gate.Decision("allow", "x", "x"),
                                            state_root=str(self.state),
                                            config_path=str(self.base / "missing.json"))
        command = updated["command"].replace("/bin/sh -c 'pytest -q'",
                                             "/bin/sh -c 'printf child-output; exit 5'")
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        done = subprocess.run(["/bin/sh", "-c", command], cwd=str(self.base), env=env,
                              capture_output=True, text=True)
        self.assertNotIn("No module named", done.stderr)
        self.assertEqual(done.returncode, 5)
        self.assertTrue(done.stdout.endswith("child-output"), done.stdout)
        self.assertTrue(done.stdout.startswith("[local output routing waived: setup_failure:"), done.stdout)

    def test_output_over_the_queue_cap_is_clipped_not_refused(self):
        queue, intake = self.queue()
        source = (b"line of ordinary output\n" * 3000) + b"FAILED final assertion\n"
        self.assertGreater(len(source), queue.caps.max_input_bytes)
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            output_router.route_output(source, state_root=str(self.state), repo=str(self.repo),
                                       classification="internal_nonclient", intake=intake,
                                       executor=lambda: queue.run_once("test"))
        text = captured.getvalue()
        self.assertIn("digest covers head and tail only", text)
        self.assertIn("FAILED final assertion", text)
        self.assertTrue(self.rows()[-1]["routed"])
        path = next(output_router.output_directory(str(self.state)).glob("*.output"))
        self.assertEqual(path.read_bytes(), source)

    def test_clipped_outputs_with_different_middles_get_distinct_task_ids(self):
        queue, intake = self.queue()
        seen = []
        original = intake.checkpoint

        def recording(**kwargs):
            seen.append(kwargs["task_id"])
            return original(**kwargs)

        intake.checkpoint = recording
        head, tail = b"head line\n" * 2000, b"tail line\n" * 2000
        for middle in (b"alpha\n" * 500, b"bravo\n" * 500):
            with contextlib.redirect_stdout(io.StringIO()):
                output_router.route_output(head + middle + tail, state_root=str(self.state),
                                           repo=str(self.repo), classification="internal_nonclient",
                                           intake=intake, executor=lambda: queue.run_once("test"))
        self.assertEqual(len(seen), 2)
        self.assertNotEqual(seen[0], seen[1])

    def test_router_has_no_network_or_provider_import(self):
        tree = ast.parse(Path(output_router.__file__).read_text())
        modules = {node.module.split(".")[0] for node in ast.walk(tree)
                   if isinstance(node, ast.ImportFrom) and node.module}
        modules.update(alias.name.split(".")[0] for node in ast.walk(tree)
                       if isinstance(node, ast.Import) for alias in node.names)
        self.assertFalse({"requests", "http", "urllib", "socket", "provider"} & modules)


if __name__ == "__main__":
    unittest.main()
