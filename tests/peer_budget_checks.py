"""Reviewed K09/M01 checks, kept separate from origin/main's existing tests."""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time

from harness import REPO, Sandbox, check
from agent_bridge import admin, broker, preflight, registry, runner, schema_validate, store, worker
from agent_bridge.errors import BrokerError, ErrorCategory, hint as error_hint
from agent_bridge.mcp_server import build_tools
from agent_bridge.platform import platform as active_platform

from test_suite import PROCESS_GROUP_ENUMERATION_AVAILABLE, group_survivors, skip

BOTH = (("codex", "claude"), ("claude", "codex"))


def test_tool_exposure() -> None:
    print("\n[tool exposure]")
    sb = Sandbox()
    try:
        for caller, peer in BOTH:
            out = sb.mcp(caller, [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            ])
            names = {t["name"] for t in out[-1]["result"]["tools"]}
            check(f"--caller {caller} exposes all five {peer}_* tools",
                  names == {f"{peer}_{v}" for v in ("start", "continue", "poll", "read", "close")},
                  str(sorted(names)))
            check(f"--caller {caller} exposes no {caller}_* self-consultation tool",
                  not any(n.startswith(f"{caller}_") for n in names), str(sorted(names)))
            advertised = {t["name"]: t["inputSchema"] for t in out[-1]["result"]["tools"]}
            for verb in ("start", "continue"):
                schema = advertised[f"{peer}_{verb}"]
                required = (["conversation_id"] if verb == "continue" else []) + [
                    "prompt", "source_classification"]
                check(f"{peer}_{verb}: handoff fields are optional and required fields unchanged",
                      schema["required"] == required and schema["additionalProperties"] is False
                      and all(schema["properties"].get(field, {}).get("type") == "string"
                              for field in ("preparation_dir", "clearance_sha256")))
                for field, companion in (("preparation_dir", "clearance_sha256"),
                                         ("clearance_sha256", "preparation_dir")):
                    description = schema["properties"].get(field, {}).get("description", "")
                    check(f"{peer}_{verb}: {field} describes the pair and label exclusion",
                          "together" in description and companion in description
                          and "cannot be used with label" in description)
                args = {"prompt": "Synthetic question", "source_classification": "synthetic"}
                if verb == "continue":
                    args["conversation_id"] = "synthetic-conversation"
                check(f"{peer}_{verb}: advertised schema still accepts legacy labels",
                      not schema_validate.validate({**args, "label": "Synthetic label"}, schema))
                check(f"{peer}_{verb}: advertised schema accepts the handoff pair",
                      not schema_validate.validate({**args, "preparation_dir": sb.root,
                                                    "clearance_sha256": "0" * 64}, schema))
        out = sb.mcp("codex", [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": "codex_start", "arguments": {}}}])
        wrong_mode = out[0]["result"]
        check("calling the peer's own name in the wrong mode is refused",
              wrong_mode["isError"] is True
              and wrong_mode["structuredContent"]["error_category"]
              == ErrorCategory.INPUT_SCHEMA_INVALID.value
              and "No such tool in this caller mode"
              in wrong_mode["structuredContent"]["error_hint"])
        # Use the platform's own launcher. The POSIX one is a /bin/sh script,
        # which Windows cannot execute at all, so testing it there proves
        # nothing about the tool and everything about shebangs.
        launcher = [os.path.join(REPO, "bin", "agent-bridge-mcp.cmd")] \
            if os.name == "nt" else [os.path.join(REPO, "bin", "agent-bridge-mcp")]
        proc = subprocess.run(launcher, capture_output=True, timeout=60)
        check("--caller is required",
              proc.returncode != 0 and b"--caller" in proc.stderr, proc.stderr.decode())
        proc = subprocess.run(launcher + ["--caller", "gpt"],
                              capture_output=True, timeout=60)
        check("--caller rejects an unknown value",
              proc.returncode != 0 and b"invalid choice" in proc.stderr
              and b"gpt" in proc.stderr, proc.stderr.decode())
        check("a launcher exists for this platform",
              os.path.isfile(launcher[0]), launcher[0])
    finally:
        sb.cleanup()


def test_timeout_and_process_group_cleanup() -> None:
    print("\n[timeout and process-group cleanup]")
    if os.name == "nt":
        # A job whose members have all exited can no longer be opened, exactly
        # like a job that cannot be queried for some other reason. Reporting
        # "alive" for that ambiguity marks every clean shutdown as a
        # containment failure, which is a real regression this pins down.
        finished = active_platform.spawn_isolated(
            [sys.executable, "-c", "pass"], cwd=tempfile.gettempdir(),
            env=runner.scrubbed_env())
        finished.wait(timeout=30)
        for stream in (finished.stdin, finished.stdout, finished.stderr):
            if stream is not None:
                stream.close()
        time.sleep(0.2)
        check("a finished tree reports gone, not survived",
              active_platform.process_tree_alive(finished.pid) is False)
        report = active_platform.terminate_process_tree(finished.pid, 0.0)
        check("and terminating it is not reported as an escape",
              not report.get("group_survived_termination"), json.dumps(report))
    for caller, peer in BOTH:
        sb = Sandbox(**{f"{peer}.timeout_seconds": 2, f"{peer}.grace_seconds": 1})
        try:
            sb.env(**{f"FAKE_{peer.upper()}_MODE": "hang"})
            requested_at = time.monotonic()
            started = sb.consult(caller)
            status = sb.wait(started["job_id"], timeout=60)
            elapsed = time.monotonic() - requested_at
            result = broker.read(sb.cfg, caller, {"job_id": started["job_id"]})
            check(f"{caller}->{peer}: hang lands in timed_out",
                  status["status"] == "timed_out"
                  and status["error_category"] == ErrorCategory.PEER_TIMEOUT.value
                  and result["error_hint"] == error_hint(ErrorCategory.PEER_TIMEOUT),
                  json.dumps(status))
            prov = sb.provenance(started["job_id"])
            kills = [a["group_kill"] for a in prov["attempts"] if a["group_kill"]]
            if os.name == "nt":
                reported_states = [sum(bool(k.get(key)) for key in (
                    "job_terminated", "tree_already_gone", "termination_failed"))
                    for k in kills]
                check(f"{caller}->{peer}: tree termination has honest provenance",
                      bool(kills) and all(state == 1 for state in reported_states),
                      json.dumps(kills))
                check(f"{caller}->{peer}: the timed-out tree is gone",
                      all(not k.get("group_survived_termination") for k in kills),
                      json.dumps(kills))
            else:
                check(f"{caller}->{peer}: the whole process group was signalled",
                      bool(kills) and all(k.get("sigterm") or k.get("sigkill")
                                          for k in kills),
                      json.dumps(kills))
            pgids = [k["pgid"] for k in kills if k.get("pgid")]
            if not PROCESS_GROUP_ENUMERATION_AVAILABLE:
                skip(f"{caller}->{peer}: no orphan processes survive in the killed groups",
                     "this platform cannot enumerate isolated process groups")
            else:
                time.sleep(0.5)
                survivors = [(p, group_survivors(p)) for p in pgids]
                survivors = [(p, s) for p, s in survivors if s]
                check(f"{caller}->{peer}: no orphan processes survive in the killed groups",
                      not survivors, str(survivors))
            check(f"{caller}->{peer}: timeout is not retried into a second hang",
                  prov["attempt_count"] == 1, str(prov["attempt_count"]))
            check(f"{caller}->{peer}: timeout is not retryable by the caller",
                  result["retryable_by_caller"] is False)
            check(f"{caller}->{peer}: external timeout includes grace",
                  prov["attempts"][0]["duration_seconds"] <= 2.25,
                  str(prov["attempts"][0]["duration_seconds"]))
            check(f"{caller}->{peer}: slow consultation terminates without a retry delay",
                  elapsed < 3.5, str(elapsed))
        finally:
            sb.cleanup()


def test_external_termination_deadline() -> None:
    print("\n[external termination deadline]")
    with tempfile.TemporaryDirectory(prefix="agent-bridge-deadline-") as root:
        marker = os.path.join(root, "pids")
        record_pid = (
            f"with open({marker!r}, 'a', encoding='utf-8') as handle:\n"
            "    handle.write(str(os.getpid()) + '\\n')\n"
        )
        child_script = (
            "import os,signal,time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            + record_pid + "time.sleep(120)\n"
        )
        for closed_pipes in (False, True):
            if os.path.isfile(marker):
                os.unlink(marker)
            script = (
                "import os,signal,subprocess,sys,time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                + record_pid
                + ("os.close(1)\nos.close(2)\n" if closed_pipes else "")
                + f"subprocess.Popen([sys.executable, '-c', {child_script!r}], "
                "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
                "stderr=subprocess.DEVNULL)\n"
                "time.sleep(120)\n"
            )
            result = runner.run(
                [sys.executable, "-c", script], cwd=root,
                env=runner.scrubbed_env(), stdin_data="", timeout=2, grace=0.8,
                stdout_cap=1000, stderr_cap=1000)
            label = "closed pipes" if closed_pipes else "open pipes"
            try:
                check(f"external: {label} timeout includes forced termination and grace",
                      result.timed_out and result.duration_seconds <= 2.25,
                      str(result.duration_seconds))
                check(f"external: {label} leader is reaped before returning",
                      result.returncode is not None, str(result.returncode))
                if os.name != "nt":
                    check(f"external: {label} ignores SIGTERM and requires SIGKILL",
                          result.group_kill.get("sigkill") is True,
                          json.dumps(result.group_kill))
                pids = [int(pid) for pid in open(marker).read().splitlines()]
                check(f"external: {label} stand-in launched its descendant", len(pids) == 2)
                # Allow the OS to reap the orphan; a zombie is already dead.
                deadline = time.monotonic() + 0.5
                while any(active_platform.process_alive(pid) for pid in pids) \
                        and time.monotonic() < deadline:
                    time.sleep(0.01)
                check(f"external: {label} leaves neither leader nor descendant alive",
                      not any(active_platform.process_alive(pid) for pid in pids), str(pids))
            finally:
                if result.pgid is not None and active_platform.process_tree_alive(result.pgid):
                    active_platform.terminate_process_tree(result.pgid, 0.0)

        refused = runner.run(
            [sys.executable, "-c", f"open({os.path.join(root, 'launched')!r}, 'w').close()"],
            cwd=root, env=runner.scrubbed_env(), stdin_data="", timeout=0, grace=1,
            stdout_cap=1000, stderr_cap=1000)
        check("external: an exhausted time budget never launches the stand-in",
              refused.timed_out and refused.pgid is None
              and not os.path.exists(os.path.join(root, "launched")))


def test_whole_request_budget_and_receipts() -> None:
    print("\n[whole-request budgets and receipts]")
    for caller, peer in BOTH:
        prefix = f"FAKE_{peer.upper()}"
        for mode in ("stubborn_hang", "slow_transient", "slow_corrective"):
            sb = Sandbox(**{f"{peer}.timeout_seconds": 3, f"{peer}.grace_seconds": 0.8})
            pids = []
            try:
                sb.env(**{f"{prefix}_MODE": mode,
                          f"{prefix}_STATE": sb.marker("retry"),
                          f"{prefix}_PID_LOG": sb.marker("pids")})
                began = time.monotonic()
                started, status, result = sb.run_to_completion(caller)
                elapsed = time.monotonic() - began
                receipt = result["receipt"]
                expected_attempts = 1 if mode == "stubborn_hang" else 2
                check(f"budget {peer} {mode}: timeout receipt includes hashes and next action",
                      receipt["outcome"] == "timeout" and receipt["next_action"] == "shorten"
                      and receipt["prompt_sha256"] == store.sha256_text("Is this design sound?")
                      and receipt["config_snapshot_sha256"]
                      == sb.provenance(started["job_id"])["config_snapshot_sha256"]
                      and receipt["unresolved_questions"] == ["Is this design sound?"])
                check(f"budget {peer} {mode}: all retries and grace share one deadline",
                      status["status"] == "timed_out" and elapsed <= 3.4
                      and receipt["elapsed_seconds"] <= 3.3,
                      f"elapsed={elapsed} receipt={receipt['elapsed_seconds']}")
                check(f"budget {peer} {mode}: timeout stops further expensive attempts",
                      receipt["attempt_count"] == expected_attempts
                      and not result["retryable_by_caller"], json.dumps(receipt))
                hashes = [a["prompt_sha256"] for a in receipt["attempts"]]
                check(f"budget {peer} {mode}: retry prompt identity is recorded",
                      len(set(hashes)) == (2 if mode == "slow_corrective" else 1))
                stages = receipt["elapsed_stages"]
                check(f"budget {peer} {mode}: termination grace is measured in the receipt",
                      any(s["stage"] == "termination" and s["elapsed_seconds"] >= (0 if os.name == "nt" else 0.1)
                          for s in stages), json.dumps(stages))
                pids = [int(pid) for pid in open(sb.marker("pids")).read().splitlines()]
                check(f"budget {peer} {mode}: stand-in launched a descendant", len(pids) == 2)
                reap_deadline = time.monotonic() + 0.5
                while any(active_platform.process_alive(pid) for pid in pids) \
                        and time.monotonic() < reap_deadline:
                    time.sleep(0.01)
                check(f"budget {peer} {mode}: leader and descendant are dead",
                      not any(active_platform.process_alive(pid) for pid in pids), str(pids))
                check(f"budget {peer} {mode}: no timed-out result is published",
                      not os.path.isfile(os.path.join(sb.cfg.job_dir(started["job_id"]),
                                                     "result.json")))
            finally:
                for pid in pids:
                    if active_platform.process_alive(pid):
                        os.kill(pid, signal.SIGTERM if os.name == "nt" else signal.SIGKILL)
                sb.cleanup()

        for mode, outcome, action, attempts in (
            ("auth_failure", "auth_failure", "route_to_code_task", 1),
            ("malformed_payload", "structured_output_exhausted", "decompose", 2),
            ("schema_invalid", "structured_output_exhausted", "decompose", 2),
        ):
            sb = Sandbox()
            try:
                sb.env(**{f"{prefix}_MODE": mode})
                question = "  Why this design?\r\nWhat about λ and edge cases?\n\t"
                started, status, result = sb.run_to_completion(caller, prompt=question)
                receipt = result["receipt"]
                prov = sb.provenance(started["job_id"])
                check(f"receipt {peer} {mode}: distinct non-success with allowed next action",
                      not result["ok"] and receipt["outcome"] == outcome
                      and receipt["next_action"] == action
                      and action in receipt["allowed_next_actions"]
                      and receipt["attempt_count"] == attempts, json.dumps(receipt))
                check(f"receipt {peer} {mode}: hashes match the admitted artifacts",
                      receipt["prompt_sha256"] == store.sha256_text(question)
                      and receipt["config_snapshot_sha256"] == prov["config_snapshot_sha256"])
                check(f"receipt {peer} {mode}: all unresolved caller text is verbatim",
                      receipt["unresolved_questions"] == [question])
                check(f"receipt {peer} {mode}: attempts and elapsed stages are present",
                      len(receipt["attempts"]) == attempts
                      and {s["stage"] for s in receipt["elapsed_stages"]} >= {
                          "admission_and_queue", "preparation", "spawn", "execution",
                          "termination", "validation", "finalization"})
                saved = store.read_json(os.path.join(sb.cfg.job_dir(started["job_id"]),
                                                    "receipt.json"))
                poll = broker.poll(sb.cfg, caller, {"job_id": started["job_id"]})
                check(f"receipt {peer} {mode}: poll, read, file and ledger agree",
                      receipt == saved == poll["receipt"] == prov["receipt"]
                      == sb.ledger()[-1]["receipt"])
                response = sb.mcp(caller, [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": f"{peer}_read", "arguments": {"job_id": started["job_id"]}}}])
                check(f"receipt {peer} {mode}: MCP error contract remains intact",
                      response[0]["result"]["isError"]
                      and response[0]["result"]["structuredContent"] == result
                      and json.loads(response[0]["result"]["content"][0]["text"]) == result)

                # Shortening alone is not evidence of a valid answer.
                followup = {"conversation_id": started["conversation_id"], "prompt": "Why?",
                            "source_classification": "internal"}
                again = broker.continue_(sb.cfg, caller, followup)
                check(f"receipt {peer} {mode}: shorter invalid follow-up still fails",
                      sb.wait(again["job_id"])["status"] == "failed")
                sb.env(**{f"{prefix}_MODE": "ok"})
                recovered = broker.continue_(sb.cfg, caller, followup)
                sb.wait(recovered["job_id"])
                answer = broker.read(sb.cfg, caller, {"job_id": recovered["job_id"]})
                check(f"receipt {peer} {mode}: shorter follow-up succeeds on validated output",
                      answer["ok"] and not schema_validate.validate(
                          answer["peer_response"], sb.cfg.load_schema()[0]))
            finally:
                sb.cleanup()


def test_prompt_budgets_and_expired_queue() -> None:
    print("\n[prompt budgets and expired queue]")
    from unittest import mock
    for caller, peer in BOTH:
        sb = Sandbox()
        try:
            tools = build_tools(caller, sb.cfg)
            for operation in ("start", "continue"):
                budget = sb.cfg.prompt_budget(operation)
                args = {"prompt": "x" * (budget + 1), "source_classification": "internal"}
                if operation == "continue":
                    args["conversation_id"] = "unused"
                with mock.patch.object(preflight, "check_peer") as probe, \
                        mock.patch.object(broker, "_spawn_worker") as spawn:
                    try:
                        tools[f"{peer}_{operation}"]["handler"](sb.cfg, caller, args)
                        refused = False
                    except BrokerError as exc:
                        refused = exc.category is ErrorCategory.INPUT_TOO_LARGE
                check(f"prompt {peer} {operation}: oversize refuses before any launch",
                      refused and not probe.called and not spawn.called)
                check(f"prompt {peer} {operation}: advertised budget is below the hard cap",
                      tools[f"{peer}_{operation}"]["inputSchema"]["properties"]["prompt"]["maxLength"]
                      == budget < sb.cfg.limit("prompt_max_chars"))

            with mock.patch.object(broker, "_spawn_worker", return_value=os.getpid()):
                started = sb.consult(caller)
            job_dir = sb.cfg.job_dir(started["job_id"])
            request_path = os.path.join(job_dir, "request.json")
            request = store.read_json(request_path)
            request["deadline_monotonic"] = time.monotonic() - 1
            store.atomic_write_json(request_path, request)
            with mock.patch.object(active_platform, "spawn_isolated") as spawn:
                worker.execute(job_dir)
            result = broker.read(sb.cfg, caller, {"job_id": started["job_id"]})
            check(f"budget {peer}: expired queued request launches neither preflight nor peer",
                  not spawn.called and result["receipt"]["outcome"] == "timeout"
                  and result["receipt"]["attempt_count"] == 0)

            # An optional nested field does not alter the closed peer schema.
            check(f"receipt {peer}: peer contract and successful MCP envelope are unchanged",
                  sb.cfg.contract_version == "2"
                  and sb.cfg.load_schema()[0]["additionalProperties"] is False
                  and "receipt" not in sb.cfg.load_schema()[0]["properties"]
                  and "outputSchema" not in tools[f"{peer}_read"])
        finally:
            sb.cleanup()

        sb = Sandbox(**{f"{peer}.timeout_seconds": 0.5})
        try:
            sb.env(**{f"FAKE_{peer.upper()}_VERSION_DELAY": "10"})
            began = time.monotonic()
            result = sb.consult(caller)
            check(f"budget {peer}: slow admission preflight also uses external termination",
                  time.monotonic() - began <= 0.8 and not result["ok"]
                  and result["receipt"]["outcome"] == "timeout"
                  and result["receipt"]["attempt_count"] == 0)
        finally:
            sb.cleanup()

        sb = Sandbox(**{f"{peer}.timeout_seconds": 0.5})
        try:
            with registry.admission_gate(sb.cfg):
                began = time.monotonic()
                result = sb.consult(caller)
            check(f"budget {peer}: admission lock waits cannot reset the request deadline",
                  time.monotonic() - began <= 0.8 and not result["ok"]
                  and result["receipt"]["outcome"] == "timeout"
                  and result["receipt"]["attempt_count"] == 0)
        finally:
            sb.cleanup()

        sb = Sandbox(limits={"prompt_corrective_max_chars": 1})
        try:
            sb.env(**{f"FAKE_{peer.upper()}_MODE": "schema_invalid"})
            _, status, result = sb.run_to_completion(caller)
            check(f"prompt {peer}: oversize corrective prompt is never launched",
                  status["retries_exhausted"] and result["receipt"]["attempt_count"] == 1
                  and result["receipt"]["outcome"] == "structured_output_exhausted")
        finally:
            sb.cleanup()

    import contextlib
    import io
    sb = Sandbox()
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            admin.cmd_status(sb.cfg, argparse.Namespace())
        health = json.loads(buf.getvalue())
        check("health visibility validates neither a session nor approval",
              health["session_validation"] == "not_checked" and not health["approval_granted"])
    finally:
        sb.cleanup()
