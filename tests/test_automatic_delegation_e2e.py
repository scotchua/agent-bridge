"""The complete automatic-delegation workflow, driven end to end.

Every other test module here checks one component. This one runs the workflow
a user actually gets, in a temporary home, through the real parts:

* the gate hook **installed** into an isolated ``~/.claude/settings.json`` and
  ``$CODEX_HOME/hooks.json`` by the real installer, then invoked through the
  installed launcher as a subprocess;
* the real orchestration MCP server as a **subprocess over stdio**, which is
  the surface an assistant actually talks to;
* the real ``ExecutionQueue`` and the real ``SubprocessHarnessExecutor``
  running the real ``claude_task.py`` and ``codex_task.py``, which create real
  git worktrees, capture a real patch, re-apply it to a second real worktree,
  run verification under the host's real confinement backend, and snapshot the
  source repository for integrity;
* the real local lane: automatic intake, the durable queue, the service loop,
  the worker child adapter, and a real HTTP round trip to a model service;
* the real audit report over the records all of that left behind.

**What is standing in, stated exactly.** The provider CLIs and the local model
are stand-in executables (``tests/fakes/fake_exec_*.py``,
``tests/fakes/fake_local_worker.py``). No Claude, Codex or Ollama account is
contacted and no allowance is spent. So this proves the dispatch machinery,
the enforcement, the receipts and the recovery; it does not prove any
provider's own login or model behaviour. That needs the same commands run
against signed-in CLIs on a real host, which is a separate, authorised step.

Everything else here is the production code path.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAKES = Path(__file__).resolve().parent / "fakes"
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge.capacity_router import CapacityObservation, StageRouter  # noqa: E402
from agent_bridge.execution import hostenv  # noqa: E402
from agent_bridge.localq.spool import ResourceSnapshot  # noqa: E402
from agent_bridge.orchestration import audit, autodecide, autoroute, gate  # noqa: E402
from agent_bridge.orchestration.execution_queue import (  # noqa: E402
    ExecutionQueue, Harnesses, SubprocessHarnessExecutor)

def _verify_python() -> str | None:
    """The interpreter name to verify with, or None if this host has neither.

    ``sys.executable`` cannot be used: ``verify_policy`` requires a program
    named without a path, on purpose. So the name has to be bare, and which
    bare name exists is a property of the host. This was hardcoded to
    ``python``, which cost a reviewer three failures on macOS, where a bare
    ``python`` has not existed since the system Python 2 was removed and the
    setup documentation says ``python3`` for that reason. Some Windows
    installs are the other way round. Probing is the only honest answer, and
    a host with neither skips rather than reporting a failure that is about
    the machine instead of the code.
    """
    for name in ("python3", "python"):
        if shutil.which(name):
            return name
    return None


VERIFY_PYTHON = _verify_python()
#: A verification command the allowlist accepts and that needs no third-party
#: package, so the synthetic repository can actually be verified anywhere.
VERIFY = [VERIFY_PYTHON or "python3", "-m", "unittest", "discover", "-s", ".",
          "-p", "test_calc.py"]
#: Applied to every test whose assertion depends on verification running.
needs_python = unittest.skipUnless(
    VERIFY_PYTHON, "no bare python3 or python on PATH; verification cannot run")

BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
TEST = ("import unittest\nimport calc\n\n\n"
        "class AddTest(unittest.TestCase):\n"
        "    def test_add(self):\n"
        "        self.assertEqual(calc.add(2, 2), 4)\n")


class PortableSampler:
    """A resource reading that works on every host the suite runs on.

    ``localq.runtime.MacSampler`` shells out to macOS-only probes. The queue
    takes a sampler through a seam precisely so the lane can be exercised
    elsewhere; this reports a machine with capacity so admission is about the
    routing policy under test rather than about this host's load.
    """

    def __init__(self, clock=time.time):
        self.clock = clock
        self.last_details = {"source": "portable-test-sampler"}

    def sample(self):
        return ResourceSnapshot(self.clock(), "normal", "normal", True, 600.0, 0.0)


class ModelService(BaseHTTPRequestHandler):
    """A stand-in model endpoint. Real HTTP, real request and response bodies."""

    requests: list = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        type(self).requests.append((self.path, payload))
        body = json.dumps({
            "model": "stand-in-local-model",
            "response": "SUMMARY: three synthetic log lines describe one "
                        "timeout and two retries.",
            "done": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, timeout=120,
                          capture_output=True)


class Workflow(unittest.TestCase):
    """One temporary home holding everything a real installation would."""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        try:
            cls.backend = hostenv.confinement("synthetic")
        except hostenv.HostCapabilityError as exc:
            raise unittest.SkipTest(
                "this host cannot confine verification commands, so the "
                f"execution lanes refuse by design: {exc.code}") from None

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()

        # An isolated home. Nothing here touches the real user's settings.
        self.home = self.base / "home"
        self.codex_home = self.home / ".codex"
        (self.home / ".claude").mkdir(parents=True)
        self.codex_home.mkdir(parents=True)
        self.lane_claude_store = self.home / ".agent-bridge" / "claude-home"
        self.lane_claude_store.mkdir(parents=True)
        self.lane_codex_home = self.home / ".agent-bridge" / "codex-home"
        self.lane_codex_home.mkdir(parents=True)
        for path in (self.home / ".agent-bridge", self.lane_claude_store,
                     self.lane_codex_home):
            os.chmod(path, 0o700)

        # Stand-in provider CLIs and local worker.
        self.bin = self.base / "bin"
        self.bin.mkdir()
        for name, source in (("claude", "fake_exec_claude.py"),
                             ("codex", "fake_exec_codex.py"),
                             ("local-worker", "fake_local_worker.py")):
            target = self.bin / name
            shutil.copy(FAKES / source, target)
            os.chmod(target, 0o755)
        self.control = {"mode": "ok", "write": {"calc.py": FIXED}}
        self.write_control()

        # The synthetic repository. Invented material, a deliberate bug.
        self.repo = self.base / "repo"
        self.repo.mkdir()
        git("init", "-q", ".", cwd=self.repo)
        git("config", "user.email", "synthetic@example.invalid", cwd=self.repo)
        git("config", "user.name", "Synthetic", cwd=self.repo)
        (self.repo / "calc.py").write_text(BUGGY, encoding="utf-8")
        (self.repo / "test_calc.py").write_text(TEST, encoding="utf-8")
        git("add", "-A", cwd=self.repo)
        git("commit", "-qm", "synthetic seed with a deliberate bug", cwd=self.repo)
        self.head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(self.repo),
                                   capture_output=True, text=True, timeout=60,
                                   check=True).stdout.strip()
        self.brief = self.base / "brief.txt"
        self.brief.write_text("Fix calc.add so that it returns the sum. "
                              "Change only calc.py.\n", encoding="utf-8")

        # State, queues and the one configuration both the gate and the
        # worker read.
        self.state = self.base / "state"
        (self.state / "routing").mkdir(parents=True)
        os.chmod(self.state, 0o700)
        self.local_root = self.state / "local-queue"
        self.exec_root = self.state / "execution-queue"
        self.worker_state = self.state / "local-worker-state"
        self.worker_state.mkdir()
        os.chmod(self.worker_state, 0o700)
        self.db = self.state / "capacity.sqlite3"
        self.config = self.base / "orchestration.json"
        self.config.write_text(json.dumps({
            "config_version": "1",
            "state_root": str(self.state),
            "local_queue_root": str(self.local_root),
            "capacity_db": str(self.db),
            "worker_executable": str(self.bin / "local-worker"),
            "worker_state": str(self.worker_state),
            "execution_queue_root": str(self.exec_root),
            "codex_task_executable": str(ROOT / "src/agent_bridge/execution/codex_task.py"),
            "claude_task_executable": str(ROOT / "src/agent_bridge/execution/claude_task.py"),
            "python_executable": os.path.realpath(sys.executable),
            "claude_config_dir": str(self.lane_claude_store),
            "interval_seconds": 1.0,
        }, indent=2), encoding="utf-8")
        os.chmod(self.config, 0o600)

        # The model service, for the local lane.
        ModelService.requests = []
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), ModelService)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_http)
        (self.worker_state / "endpoint.txt").write_text(
            f"http://127.0.0.1:{self.http.server_port}", encoding="utf-8")

    def _stop_http(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=10)

    # ------------------------------------------------------------- fixtures

    def write_control(self):
        (self.home / "fake-exec.json").write_text(json.dumps(self.control),
                                                  encoding="utf-8")

    def write_policy(self, **entry):
        document = {"version": 1, "prefer": ["codex", "claude", "local"],
                    "repos": {str(self.repo): entry}}
        path = Path(autoroute.policy_path(str(self.state)))
        path.write_text(json.dumps(document), encoding="utf-8")
        os.chmod(path, 0o600)

    def observe(self, route, seconds=3600):
        router = StageRouter(str(self.db))
        now = time.time()
        router.observe_capacity(CapacityObservation(
            route=route, observed_at=now, fresh_until=now + seconds,
            available=True, source="e2e-operator-observation"), trusted=True)

    def env(self):
        return {**os.environ, "HOME": str(self.home),
                "CODEX_HOME": str(self.codex_home),
                "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
                "PYTHONPATH": str(ROOT / "src")}

    # --------------------------------------------------------------- drivers

    def install_gate(self):
        """The real installer, into the isolated home."""
        return gate.install(str(self.home), str(ROOT), str(self.config),
                            ("claude", "codex"), apply=True)

    def run_installed_hook(self, client, payload):
        """Through the launcher the installer wrote, as the host would."""
        command = gate.hook_command(str(ROOT), client, str(self.config))
        completed = subprocess.run(command, shell=True, input=json.dumps(payload).encode(),
                                   capture_output=True, timeout=180, env=self.env())
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def edit_attempt(self, client, relative="calc.py"):
        return self.run_installed_hook(client, {
            "hook_event_name": "PreToolUse", "tool_name": "Edit",
            "tool_input": {"file_path": str(self.repo / relative)},
            "cwd": str(self.repo)})

    def mcp(self, caller, calls):
        """The orchestration MCP server as a subprocess over stdio."""
        frames = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}]
        for index, (name, arguments) in enumerate(calls, start=2):
            frames.append({"jsonrpc": "2.0", "id": index, "method": "tools/call",
                           "params": {"name": name, "arguments": arguments}})
        completed = subprocess.run(
            [sys.executable, "-P", "-m", "agent_bridge.orchestration.server",
             "--caller", caller, "--config", str(self.config)],
            input=("\n".join(json.dumps(frame) for frame in frames) + "\n").encode(),
            capture_output=True, timeout=300, env=self.env())
        self.assertEqual(completed.returncode, 0, completed.stderr)
        replies = {}
        for line in completed.stdout.decode("utf-8").splitlines():
            if not line.strip():
                continue
            message = json.loads(line)
            replies[message.get("id")] = message
        return replies

    def tool_result(self, replies, request_id):
        return replies[request_id]["result"]["structuredContent"]

    def execution_queue(self):
        """The real queue with the real harness executor."""
        return ExecutionQueue(str(self.exec_root), SubprocessHarnessExecutor(Harnesses(
            codex=ROOT / "src/agent_bridge/execution/codex_task.py",
            claude=ROOT / "src/agent_bridge/execution/claude_task.py",
            python=Path(os.path.realpath(sys.executable)),
            claude_config_dir=self.lane_claude_store)),
            recover_interrupted=False)

    def drain_execution(self, expected=1):
        """Run the worker's own loop body against the real harnesses.

        ``HOME`` and ``PATH`` must be the isolated ones while this runs: the
        harnesses read ``Path.home()`` for the lane's store and resolve the
        provider executable on ``PATH``.
        """
        previous = {key: os.environ.get(key) for key in ("HOME", "PATH", "CODEX_HOME")}
        os.environ.update({key: value for key, value in self.env().items()
                           if key in ("HOME", "PATH", "CODEX_HOME")})
        try:
            queue = self.execution_queue()
            results = []
            for _ in range(expected):
                result = queue.run_once(f"e2e-worker-{len(results)}")
                if result is None:
                    break
                results.append(result)
            return results
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


class TheUserWorkflow(Workflow):
    def test_install_decide_dispatch_verify_and_audit(self):
        """One pass through everything, in the order a user meets it."""

        # 1. Install. The real installer writes both hook entries.
        report = self.install_gate()
        self.assertTrue(report["applied"])
        settings = json.loads((self.home / ".claude" / "settings.json").read_text())
        self.assertTrue(any(gate._is_ours(item)
                            for item in settings["hooks"]["PreToolUse"]))
        hooks = json.loads((self.codex_home / "hooks.json").read_text())
        self.assertTrue(any(gate._is_ours(item)
                            for item in hooks["hooks"]["PreToolUse"]))

        # 2. The operator classifies the repository and reports peer capacity.
        self.write_policy(classification="synthetic",
                          allowed_routes=["claude", "codex"])
        self.observe("codex")

        # 3. Claude Code tries to edit. Nobody said "send this to Codex".
        result = self.edit_attempt("claude")
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertTrue(reason.endswith("[routed_elsewhere]"), reason)
        self.assertIn("routed to codex", reason)

        receipt = gate.read_receipt(str(self.state), str(self.repo))
        self.assertTrue(receipt["automatic"])
        self.assertEqual(receipt["owner_route"], "codex")
        self.assertEqual(receipt["code"], "routed_peer_implementation")

        intent = autodecide.read_intent(str(self.state), str(self.repo))
        self.assertEqual(intent["next_call"], "execution_dispatch")

        # 4. The assistant makes the one call the gate named, through the
        #    real MCP server. No stage_register, no stage_claim.
        replies = self.mcp("claude", [("execution_dispatch", {
            "provider": "codex", "repo": str(self.repo), "brief": str(self.brief),
            "base": self.head, "classification": "synthetic", "model": "default",
            "effort": "medium", "verify_argv": [VERIFY],
            "item_id": intent["item_id"], "stage": intent["stage"],
            "owner_id": intent["owner_id"],
            "stage_revision": intent["stage_revision"]})])
        dispatched = self.tool_result(replies, 2)
        self.assertTrue(dispatched["ok"], dispatched)
        self.assertEqual(dispatched["state"], "queued")
        job_id = dispatched["job_id"]

        # The intent is met, so it leaves the audit's bypass column.
        self.assertIsNone(autodecide.read_intent(str(self.state), str(self.repo)))

        # 5. The worker runs the real Codex harness.
        states = self.drain_execution()
        self.assertEqual([row["state"] for row in states], ["complete"], states)

        # 6. The assistant reads the receipt back through the MCP server.
        replies = self.mcp("claude", [("execution_result", {"job_id": job_id})])
        outcome = self.tool_result(replies, 2)
        self.assertTrue(outcome["ok"], outcome)
        self.assertEqual(outcome["state"], "complete")
        for permission in ("permission_to_apply", "permission_to_commit",
                           "permission_to_push", "permission_to_merge"):
            self.assertFalse(outcome[permission])
        harness = outcome["harness"]
        self.assertTrue(harness["harness_ok"])
        self.assertEqual(harness["harness_status"], "complete")
        self.assertEqual(harness["harness_verdict"], "read")

        # 7. The patch exists, is unapplied, and the source is untouched.
        job_dir = self.exec_root / job_id
        patches = list((job_dir / "harness").glob("*/changes.patch"))
        self.assertEqual(len(patches), 1, list((job_dir / "harness").iterdir()))
        patch = patches[0].read_text(encoding="utf-8")
        self.assertIn("-    return a - b", patch)
        self.assertIn("+    return a + b", patch)
        self.assertEqual((self.repo / "calc.py").read_text(encoding="utf-8"), BUGGY)
        self.assertEqual(subprocess.run(
            ["git", "status", "--porcelain=v2", "--untracked-files=all"],
            cwd=str(self.repo), capture_output=True, text=True, timeout=60,
            check=True).stdout, "")

        # 8. The verification really ran, under this host's real confinement.
        lane = json.loads((patches[0].parent / "receipt.json").read_text())
        self.assertEqual(lane["status"], "complete")
        self.assertEqual(lane["verification_confinement"], self.backend.name)
        self.assertTrue(lane["confinement_denies_network"])
        self.assertEqual([row["returncode"] for row in lane["verification"]], [0])
        self.assertEqual(lane["verification"][0]["argv"], VERIFY)
        self.assertTrue(lane["source_integrity_match"])
        self.assertTrue(lane["fresh_worktree_patch_match"])
        self.assertFalse(lane["permission_to_land"])
        # Neither worktree is left behind.
        self.assertTrue(lane["cleanup"]["generation_removed"])
        self.assertTrue(lane["cleanup"]["verification_removed"])

        # 9. Codex, which owns the stage, may edit the repository.
        self.assertEqual(self.edit_attempt("codex"), {})

        # 10. The audit accounts for all of it.
        document = audit.report(str(self.state), home=str(self.home),
                                config_path=str(self.config), since_hours=0.0)
        self.assertEqual(document["routed"]["count"], 1)
        self.assertEqual(document["routed"]["by_route"], {"codex": 1})
        self.assertEqual(document["eligible"]["count"], 1)
        # One decision, not one per tool call: Codex's allowed edit above
        # was judged against the receipt that already existed.
        self.assertEqual(document["automatic_share"]["decisions"], 1)
        self.assertEqual(document["automatic_share"]["made_automatically"], 1)
        self.assertEqual(
            document["automatic_share"]["made_by_an_agent_calling_routing_decide"], 0)
        self.assertEqual(
            document["bypasses"]["observed"]["routed_but_never_dispatched"]["count"], 0)
        self.assertEqual(document["bypasses"]["observed"]["clients_without_the_hook"], [])
        self.assertEqual(document["failures"]["gate"]["count"], 0)
        self.assertEqual(document["failures"]["execution_queue"]["states"],
                         {"complete": 1})
        self.assertTrue(document["hook_coverage"]["claude"]["installed"])
        self.assertTrue(document["hook_coverage"]["codex"]["installed"])
        self.assertTrue(document["hook_coverage"]["claude"]["automatic_routing"])
        # And it still says what it cannot see.
        self.assertTrue(document["bypasses"]["not_countable"])


class BothDirections(Workflow):
    """Codex to Claude and Claude to Codex, through the same queue."""

    def dispatch(self, caller, provider, item_suffix):
        router = StageRouter(str(self.db))
        now = time.time()
        router.observe_capacity(CapacityObservation(
            route=provider, observed_at=now, fresh_until=now + 3600,
            available=True, source="e2e-operator-observation"), trusted=True)
        item = f"e2e-{item_suffix}"
        router.register(item, "implement", allowed_routes=[provider])
        owned = router.assign(item, "implement", owner_id=f"{caller}-session",
                              lease_seconds=1800, expected_revision=0)
        replies = self.mcp(caller, [("execution_dispatch", {
            "provider": provider, "repo": str(self.repo), "brief": str(self.brief),
            "base": self.head, "classification": "synthetic", "model": "default"
            if provider == "codex" else "sonnet", "effort": "medium",
            "verify_argv": [VERIFY], "item_id": item, "stage": "implement",
            "owner_id": owned["owner_id"], "stage_revision": owned["revision"]})])
        result = self.tool_result(replies, 2)
        self.assertTrue(result["ok"], result)
        return result["job_id"]

    def test_codex_to_claude_and_claude_to_codex_both_produce_verified_patches(self):
        codex_to_claude = self.dispatch("codex", "claude", "c2c")
        claude_to_codex = self.dispatch("claude", "codex", "c2x")

        states = self.drain_execution(expected=2)
        self.assertEqual(sorted(row["state"] for row in states),
                         ["complete", "complete"], states)

        for job_id, provider in ((codex_to_claude, "claude"),
                                 (claude_to_codex, "codex")):
            receipt = json.loads(
                (self.exec_root / job_id / "receipt.json").read_text())
            self.assertEqual(receipt["state"], "complete", receipt)
            self.assertEqual(receipt["provider"], provider)
            lane = json.loads(next(
                (self.exec_root / job_id / "harness").glob("*/receipt.json")
            ).read_text())
            self.assertEqual(lane["status"], "complete")
            self.assertEqual(lane["route"], f"{provider}-subscription-cli")
            self.assertTrue(lane["source_integrity_match"])
            self.assertEqual([row["returncode"] for row in lane["verification"]], [0])
            # The repaired git resolution and confinement selection, recorded.
            self.assertEqual(lane["git_executable"], str(hostenv.resolve_git()))
            self.assertEqual(lane["verification_confinement"], self.backend.name)

        # One source repository, two independent unapplied patches, untouched.
        self.assertEqual((self.repo / "calc.py").read_text(encoding="utf-8"), BUGGY)

    def test_a_caller_cannot_dispatch_to_its_own_provider(self):
        router = StageRouter(str(self.db))
        now = time.time()
        router.observe_capacity(CapacityObservation(
            route="claude", observed_at=now, fresh_until=now + 3600,
            available=True, source="e2e-operator-observation"), trusted=True)
        router.register("self-dispatch", "implement", allowed_routes=["claude"])
        owned = router.assign("self-dispatch", "implement", owner_id="claude-session",
                              lease_seconds=1800, expected_revision=0)
        replies = self.mcp("claude", [("execution_dispatch", {
            "provider": "claude", "repo": str(self.repo), "brief": str(self.brief),
            "base": self.head, "classification": "synthetic", "model": "sonnet",
            "effort": "medium", "verify_argv": [VERIFY], "item_id": "self-dispatch",
            "stage": "implement", "owner_id": owned["owner_id"],
            "stage_revision": owned["revision"]})])
        result = self.tool_result(replies, 2)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "provider_not_eligible_for_caller")

    def test_paid_fallback_is_refused_at_the_queue(self):
        router = StageRouter(str(self.db))
        now = time.time()
        router.observe_capacity(CapacityObservation(
            route="codex", observed_at=now, fresh_until=now + 3600,
            available=True, source="e2e-operator-observation"), trusted=True)
        router.register("paid", "implement", allowed_routes=["codex"])
        owned = router.assign("paid", "implement", owner_id="claude-session",
                              lease_seconds=1800, expected_revision=0)
        replies = self.mcp("claude", [("execution_dispatch", {
            "provider": "codex", "repo": str(self.repo), "brief": str(self.brief),
            "base": self.head, "classification": "synthetic", "model": "default",
            "effort": "medium", "verify_argv": [VERIFY], "item_id": "paid",
            "stage": "implement", "owner_id": owned["owner_id"],
            "stage_revision": owned["revision"], "paid_fallback": True})])
        result = self.tool_result(replies, 2)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "paid_fallback_forbidden")


class LocalLane(Workflow):
    """Either assistant, to a local model, without anyone asking for it."""

    def route_local(self, caller, text, task_type="log_triage",
                    classification="internal_nonclient"):
        replies = self.mcp(caller, [("work_route_local", {
            "task_type": task_type, "input": text, "priority": "interactive",
            "classification": classification, "purpose": "work"})])
        return self.tool_result(replies, 2)

    def drain_local(self):
        from agent_bridge.localq.service import Service

        service = Service(str(self.local_root), str(self.bin / "local-worker"),
                          str(self.worker_state), sampler=PortableSampler())
        return service.once()

    def test_eligible_mechanical_text_is_admitted_and_drafted_automatically(self):
        text = "\n".join(f"2026-09-15T10:{minute:02d}:00 WARN retry {minute}"
                         for minute in range(40))
        decision = self.route_local("codex", text)
        # The intake decided and submitted. Nobody said "use the local model".
        self.assertTrue(decision["ok"], decision)
        self.assertEqual(decision["decision"], "local")
        self.assertEqual(decision["reason"], "eligible_mechanical_work")
        self.assertEqual(decision["fallback"], "none")
        self.assertTrue(decision["receipt_id"])
        job_id = decision["job_id"]

        self.drain_local()

        replies = self.mcp("codex", [("work_result", {"job_id": job_id})])
        result = self.tool_result(replies, 2)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "complete")
        self.assertTrue(result["ready"])
        self.assertEqual(result["disposition"], {"executor": "child",
                                                 "outcome": "complete"})
        # The draft came back from the model service, and stays a draft.
        self.assertEqual(result["result"]["output"]["task"], "log_triage")
        self.assertIn("SUMMARY", result["result"]["output"]["text"])
        # A real HTTP round trip to the model service happened.
        self.assertEqual(len(ModelService.requests), 1)
        self.assertEqual(ModelService.requests[0][0], "/api/generate")

    def test_both_assistants_can_reach_the_local_lane(self):
        text = "\n".join(f"line {index} of synthetic material" for index in range(60))
        for caller in ("claude", "codex"):
            decision = self.route_local(caller, text + f"\ncaller {caller}")
            self.assertEqual(decision["decision"], "local", decision)
            self.assertEqual(decision["caller"], caller)

    def test_client_derived_material_is_refused_with_no_cloud_fallback(self):
        text = "\n".join(f"line {index}" for index in range(60))
        decision = self.route_local("claude", text, classification="client_derived")
        self.assertEqual(decision["decision"], "refused")
        self.assertEqual(decision["reason"], "classification_refused")
        self.assertEqual(decision["fallback"], "none")
        self.assertIsNone(decision["job_id"])

    def test_work_needing_judgment_is_refused_rather_than_sent_local(self):
        text = "\n".join(f"line {index}" for index in range(60))
        decision = self.route_local("claude", text, task_type="design_review")
        self.assertEqual(decision["decision"], "refused")
        self.assertEqual(decision["reason"], "task_requires_cloud_or_human_judgment")

    def test_the_routing_receipt_is_immutable(self):
        text = "\n".join(f"line {index}" for index in range(60))
        first = self.route_local("claude", text)
        again = self.route_local("claude", text)
        self.assertEqual(first["receipt_id"], again["receipt_id"])
        self.assertTrue(again["deduplicated"])


class RestartRecovery(Workflow):
    """A worker that dies mid-job must never silently repeat a provider send."""

    def queued_job(self):
        router = StageRouter(str(self.db))
        now = time.time()
        router.observe_capacity(CapacityObservation(
            route="codex", observed_at=now, fresh_until=now + 3600,
            available=True, source="e2e-operator-observation"), trusted=True)
        router.register("restart", "implement", allowed_routes=["codex"])
        owned = router.assign("restart", "implement", owner_id="claude-session",
                              lease_seconds=1800, expected_revision=0)
        replies = self.mcp("claude", [("execution_dispatch", {
            "provider": "codex", "repo": str(self.repo), "brief": str(self.brief),
            "base": self.head, "classification": "synthetic", "model": "default",
            "effort": "medium", "verify_argv": [VERIFY], "item_id": "restart",
            "stage": "implement", "owner_id": owned["owner_id"],
            "stage_revision": owned["revision"]})])
        result = self.tool_result(replies, 2)
        self.assertTrue(result["ok"], result)
        return result["job_id"]

    def test_a_job_interrupted_while_running_is_blocked_for_reconciliation(self):
        job_id = self.queued_job()
        directory = self.exec_root / job_id
        # Exactly the on-disk state a worker killed mid-send leaves: a claim
        # taken and the receipt marked running.
        (directory / "claim.lock").write_bytes(b"")
        receipt = json.loads((directory / "receipt.json").read_text())
        receipt.update(state="running", worker_id="killed-worker",
                       started_at=time.time())
        (directory / "receipt.json").write_text(json.dumps(receipt))

        # Restart: a fresh queue with recovery on, which is what the worker
        # binary constructs after it takes the global lock.
        recovered = ExecutionQueue(str(self.exec_root), None, recover_interrupted=True)
        status = recovered.status(job_id)
        self.assertEqual(status["state"], "blocked")
        final = recovered.result(job_id)
        self.assertEqual(final["error"], "interrupted_requires_reconciliation")

        # And it is not picked up again by a working worker.
        self.assertEqual(self.drain_execution(expected=1), [])

    def test_a_claim_without_a_running_receipt_is_also_uncertain(self):
        """The window between taking the claim and publishing running."""
        job_id = self.queued_job()
        (self.exec_root / job_id / "claim.lock").write_bytes(b"")
        recovered = ExecutionQueue(str(self.exec_root), None, recover_interrupted=True)
        self.assertEqual(recovered.status(job_id)["state"], "blocked")

    def test_a_queued_job_survives_a_restart_and_still_runs(self):
        """Recovery must not blanket-block work that was never started."""
        job_id = self.queued_job()
        ExecutionQueue(str(self.exec_root), None, recover_interrupted=True)
        self.assertEqual(
            json.loads((self.exec_root / job_id / "receipt.json").read_text())["state"],
            "queued")
        states = self.drain_execution()
        self.assertEqual([row["state"] for row in states], ["complete"], states)

    def test_the_routing_decision_survives_a_restart(self):
        """Receipts are files, so a new hook process reads the same decision."""
        self.install_gate()
        self.write_policy(classification="synthetic",
                          allowed_routes=["claude", "codex"])
        self.observe("codex")
        self.assertEqual(
            self.edit_attempt("claude")["hookSpecificOutput"]["permissionDecision"],
            "deny")
        first = gate.read_receipt(str(self.state), str(self.repo))
        # A completely separate process, as every tool call is.
        self.edit_attempt("claude")
        second = gate.read_receipt(str(self.state), str(self.repo))
        self.assertEqual(first["item_id"], second["item_id"])
        self.assertEqual(first["stage"], second["stage"])
        self.assertEqual(first["owner_route"], "codex")
        self.assertEqual(second["owner_route"], "codex")

    def test_a_failure_is_recorded_with_its_detail_not_just_a_class_name(self):
        """The spec's rule: a bare TaskError is not diagnostic evidence."""
        self.control = {"mode": "auth_failure"}
        self.write_control()
        job_id = self.queued_job()
        states = self.drain_execution()
        self.assertEqual([row["state"] for row in states], ["failed"], states)
        receipt = json.loads((self.exec_root / job_id / "receipt.json").read_text())
        harness = receipt["harness"]
        self.assertFalse(harness["harness_ok"])
        self.assertEqual(harness["harness_status"], "failed")
        self.assertEqual(harness["harness_verdict"], "read_failure")
        self.assertIn("error_detail", harness)
        self.assertTrue(harness["error_detail"])
        # It names the actual problem, not the exception class.
        self.assertIn("authenticated", harness["error_detail"].lower())


if __name__ == "__main__":
    unittest.main()
