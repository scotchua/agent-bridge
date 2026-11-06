"""Pre-context Bash output routing: narrow rewrite and safe local fallback."""

from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import shlex
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_bridge.localq.intake import AutomaticIntake, IntakePolicy  # noqa: E402
from agent_bridge.localq.spool import FakeBackend, LocalQueue, ResourceSnapshot  # noqa: E402
from agent_bridge.orchestration import autoroute, gate, output_router  # noqa: E402


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
        for command in ("sed -n '1,20p' a", "head missing", "tail missing", "cat missing", "grep -n x a",
                        "git diff -- a", "git show -- a", "pytest -q | tee out", "pytest > out",
                        "cat <<'EOF'", "tail -f log"):
            self.assertFalse(output_router.is_routable_command(command), command)

    def test_new_read_only_classes_and_dangerous_variants(self):
        regular = self.repo / "output file.txt"
        regular.write_text("synthetic output\n")
        accepted = ("python3 -m pytest -q", "python3 -m unittest discover",
                    "python3 -m ruff check .", "python3 -m mypy src",
                    "git status", "git status --untracked-files=all", "git status -uall",
                    "find . -type f", "find . -name '*.py'", "ls -R", "ls -laR .",
                    "cat 'output file.txt'", "cat -n -- 'output file.txt'",
                    "head -n 20 'output file.txt'", "tail -c +10 'output file.txt'",
                    "tail --lines=20 'output file.txt'", "head " + shlex.quote(str(regular)))
        rejected = ("python3 -m arbitrary_module", "ls .", "ls -R | head",
                    "find . -exec rm {} +", "find . -execdir echo {} +", "find . -delete",
                    "find . -ok echo {} +", "find . -okdir echo {} +",
                    "find . -fprint listing", "find . -fprintf listing '%p'", "find . -fls listing",
                    "find .; touch marker", "find . && touch marker", "find . > listing",
                    "cat 'output file.txt' | head", "cat 'output file.txt' > copy",
                    "cat 'output file.txt' < input", "cat *.txt", "cat 'output*.txt'",
                    "cat output?.txt", "cat '[ab].txt'", "cat {a,b}.txt", "cat .", "cat missing",
                    "cat -", "cat", "cat 'output file.txt' 'output file.txt'",
                    "cat $(touch marker)", "cat `touch marker`", "cat 'output file.txt' &",
                    "head 'output file.txt'\ntouch marker", "tail -f 'output file.txt'",
                    "tail -F 'output file.txt'", "tail --follow=name 'output file.txt'",
                    "tail -nf 20 'output file.txt'", "watch ls -R", "sleep 1", "ls -R -f")
        for command in accepted:
            with self.subTest(command=command):
                self.assertTrue(output_router.is_routable_command(command, cwd=str(self.repo)))
        for command in rejected:
            with self.subTest(command=command):
                self.assertFalse(output_router.is_routable_command(command, cwd=str(self.repo)))
        if hasattr(os, "mkfifo"):
            os.mkfifo(self.repo / "fifo")
            self.assertFalse(output_router.is_routable_command("cat fifo", cwd=str(self.repo)))

    def test_codex_rewrite_wire_shape_and_caller(self):
        allowed = gate.Decision("allow", "shell_read_only_heuristic", "ok")
        for client in ("claude", "codex"):
            for key in ("command", "cmd"):
                with self.subTest(client=client, key=key):
                    payload = {"tool_name": "Bash", "cwd": str(self.repo),
                               "tool_input": {key: "git status -uall", "timeout": 10}}
                    updated = gate.inline_output_update(client, payload, allowed,
                                                        state_root=str(self.state), config_path="/tmp/config")
                    wire = gate.hook_output(allowed, updated)["hookSpecificOutput"]
                    self.assertEqual(wire["hookEventName"], "PreToolUse")
                    self.assertEqual(wire["permissionDecision"], "allow")
                    command_key = "command" if client == "codex" else key
                    command = wire["updatedInput"][command_key]
                    self.assertIsInstance(command, str)
                    self.assertEqual(wire["updatedInput"]["timeout"], 10)
                    words = shlex.split(command)
                    self.assertEqual(words[words.index("--caller") + 1], client)
                    self.assertEqual(words[-1], "git status -uall")
                    self.assertIsNone(gate.inline_output_update(client, payload,
                        gate.Decision("deny", "routed_elsewhere", "no"),
                        state_root=str(self.state), config_path="/tmp/config"))
                    payload["tool_input"] = updated
                    self.assertIsNone(gate.inline_output_update(client, payload, allowed,
                        state_root=str(self.state), config_path="/tmp/config"))

    def test_codex_rewrite_retains_policy_and_privacy_requirements(self):
        payload = {"tool_name": "Bash", "cwd": str(self.repo), "tool_input": {"command": "pytest -q"}}
        allowed = gate.Decision("allow", "shell_read_only_heuristic", "ok")
        for repo_policy in (None,
                            {"classification": "client_derived", "allowed_routes": ["codex"], "mechanical_ok": True},
                            {"classification": "internal_nonclient", "allowed_routes": ["codex"], "mechanical_ok": True},
                            {"classification": "internal_nonclient", "allowed_routes": ["local"], "mechanical_ok": False}):
            with self.subTest(policy=repo_policy):
                self.policy_path.write_text(json.dumps({"version": 1,
                    "local_first": {"inline_output_router": True},
                    "repos": {} if repo_policy is None else {str(self.repo): repo_policy}}))
                self.assertIsNone(gate.inline_output_update("codex", payload, allowed,
                    state_root=str(self.state), config_path="/tmp/config"))
        self.policy_path.write_text("unreadable policy")
        self.assertIsNone(gate.inline_output_update("codex", payload, allowed,
            state_root=str(self.state), config_path="/tmp/config"))

    def test_file_rewrite_uses_payload_cwd(self):
        (self.repo / "output.txt").write_text("synthetic output")
        payload = {"tool_name": "Bash", "cwd": str(self.repo), "tool_input": {"command": "cat output.txt"}}
        self.assertIsNotNone(gate.inline_output_update("codex", payload, gate.Decision("allow", "x", "x"),
            state_root=str(self.state), config_path="/tmp/config"))

    def test_gate_cli_emits_a_rewrite_for_both_callers(self):
        import subprocess

        payload = {"tool_name": "Bash", "cwd": str(self.repo),
                   "tool_input": {"command": "git status --untracked-files=all"}}
        env = dict(os.environ, PYTHONPATH=str(Path(gate.__file__).resolve().parents[2]))
        for caller in ("claude", "codex"):
            done = subprocess.run([sys.executable, "-m", "agent_bridge.orchestration.gate",
                "--client", caller, "--state-root", str(self.state), "--config", str(self.base / "config.json"),
                "--no-automatic-routing"], input=json.dumps(payload), capture_output=True, text=True, env=env)
            self.assertEqual(done.returncode, 0, done.stderr)
            wire = json.loads(done.stdout)["hookSpecificOutput"]
            self.assertEqual(wire["permissionDecision"], "allow")
            self.assertEqual(wire["hookEventName"], "PreToolUse")
            self.assertIn(f"--caller {caller}", wire["updatedInput"]["command"])

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

    def test_the_wrapper_reaches_routing_with_a_working_setup(self):
        """Every other run_command test uses an unusable config, so the setup
        itself was never exercised and a call to a function that does not
        exist shipped unseen. This one stubs only the backend."""
        from unittest import mock
        from agent_bridge.localq import service as service_module

        queue, _intake = self.queue()
        stub = mock.Mock(queue=queue)
        cfg = mock.Mock(state_root=self.state)
        calls = []
        real_route = output_router.route_output

        def spy(output, **kwargs):
            calls.append(kwargs)
            return real_route(output, **kwargs)

        previous = os.getcwd()
        os.chdir(self.repo)
        try:
            with mock.patch("agent_bridge.orchestration.config.load", return_value=cfg), \
                    mock.patch.object(service_module.Service, "for_config", return_value=stub), \
                    mock.patch.object(output_router, "route_output", side_effect=spy):
                captured = io.StringIO()
                with contextlib.redirect_stdout(captured):
                    code = output_router.run_command(["/bin/sh", "-c", "printf small; exit 4"],
                                                     config_path=str(self.base / "config.json"), caller="codex")
        finally:
            os.chdir(previous)
        self.assertEqual(code, 4)
        self.assertNotIn("setup_failure", captured.getvalue())
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["repo"], os.path.realpath(self.repo))
        self.assertEqual(calls[0]["classification"], "internal_nonclient")
        self.assertEqual(calls[0]["caller"], "codex")

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
        row = self.rows()[-1]
        self.assertIn(f"Job {row['job_id']}", text)
        self.assertIn("work_feedback: used, reworked or discarded", text)
        self.assertIsNone(queue.result(row["job_id"])["feedback"])
        self.assertEqual(set(row), {"at", "caller", "bytes", "repo", "classification", "routed",
                                    "waiver_reason", "job_id", "duration"})
        self.assertEqual(row["caller"], "claude")
        self.assertIsInstance(row["at"], float)

    def test_caller_reaches_checkpoint_route_and_ledger_without_duplicate_jobs(self):
        queue, intake = self.queue()
        source = b"synthetic output\n" * 400
        with mock.patch.object(intake, "checkpoint", wraps=intake.checkpoint) as checkpoint, \
                mock.patch.object(intake, "route", wraps=intake.route) as route:
            jobs = []
            for caller in ("claude", "codex", "codex"):
                with contextlib.redirect_stdout(io.StringIO()):
                    output_router.route_output(source, state_root=str(self.state), repo=str(self.repo),
                        classification="internal_nonclient", caller=caller, intake=intake,
                        executor=lambda: queue.run_once("test"))
                self.assertEqual(checkpoint.call_args.kwargs["caller"], caller)
                self.assertEqual(route.call_args.kwargs["caller"], caller)
                row = self.rows()[-1]
                self.assertTrue(row["routed"])
                self.assertEqual(row["caller"], caller)
                jobs.append(row["job_id"])
            self.assertNotEqual(jobs[0], jobs[1])
            self.assertEqual(jobs[1], jobs[2])

    def test_deferral_and_timeout_waive_to_raw_output(self):
        source = b"x\n" * 3000
        _queue, intake = self.queue(BusySampler())
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            output_router.route_output(source, state_root=str(self.state), repo=str(self.repo),
                                       classification="internal_nonclient", intake=intake)
        self.assertTrue(captured.getvalue().endswith(source.decode()))
        self.assertIn("admission_deferred", captured.getvalue())
        self.assertNotIn("work_feedback", captured.getvalue())
        self.assertIn("admission_deferred", self.rows()[-1]["waiver_reason"])
        queue, intake = self.queue()
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            output_router.route_output(source, state_root=str(self.state), repo=str(self.repo),
                                       classification="internal_nonclient", intake=intake, wait_seconds=0,
                                       caller="codex")
        self.assertTrue(captured.getvalue().endswith(source.decode()))
        self.assertEqual(self.rows()[-1]["waiver_reason"], "timeout")
        self.assertEqual(self.rows()[-1]["caller"], "codex")
        self.assertNotIn("work_feedback", captured.getvalue())

    def test_client_data_health_deferral_is_preserved_for_both_callers(self):
        queue = LocalQueue(self.base / "health-queue", sampler=GoodSampler(), backend=FakeBackend(),
                           backend_id="gemma_certified", clock=lambda: 0.0)
        intake = AutomaticIntake(queue, policy=IntakePolicy(min_input_chars=1, min_nonblank_lines=1),
                                 clock=lambda: 0.0)
        source = b"synthetic stand-in for client output\n" * 200
        for caller in ("claude", "codex"):
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                output_router.route_output(source, state_root=str(self.state), repo=str(self.repo),
                    classification="client_derived", caller=caller, intake=intake)
            self.assertIn("admission_deferred:client_data_health_unverified", captured.getvalue())
            self.assertTrue(captured.getvalue().endswith(source.decode()))
            self.assertNotIn("work_feedback", captured.getvalue())
            self.assertEqual(self.rows()[-1]["caller"], caller)
            self.assertFalse(self.rows()[-1]["routed"])
        self.assertEqual(queue.state_report()["counts"], {})

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
