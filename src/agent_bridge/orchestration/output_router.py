"""Pre-context local routing for large, read-only command output.

This module deliberately has no provider client or network path.  The gate
rewrites an eligible Claude Bash input to this module *before* the command
runs, so the original output is captured locally instead of first entering
the assistant context.  Any refusal, deferral, timeout, or internal failure
falls back to the original bytes with one visible waiver line.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .. import store
from ..localq.intake import AutomaticIntake
from ..localq.spool import AdmissionError
from . import autoroute

SMALL_OUTPUT_BYTES = 4 * 1024
WAIT_SECONDS = 45.0
RETENTION_SECONDS = 30 * 24 * 3600
OUTPUT_DIR = "inline-output"
LEDGER = "inline-output-routing.jsonl"
FAILURE_LINE = re.compile(r"FAILED|Error|Traceback|assert", re.IGNORECASE)

DIGEST_PROMPT = (
    "Summarize this command output for the calling engineer. Preserve the "
    "command result, failures, and actionable diagnostics; do not invent facts."
)


def is_wrapped_command(command: str) -> bool:
    """Whether ``command`` is already this module's run wrapper."""
    return bool(re.search(r"(?:^|\s)-m\s+agent_bridge\.orchestration\.output_router\s+run(?:\s|$)", command))


def _git_routable(words: list[str]) -> bool:
    try:
        index = words.index("git")
    except ValueError:
        return False
    rest = words[index + 1:]
    while rest and (rest[0].startswith("-") or rest[0] in {"-C", "-c"}):
        if rest[0] in {"-C", "-c"} and len(rest) > 1:
            rest = rest[2:]
        else:
            rest = rest[1:]
    if not rest or rest[0] not in {"log", "diff", "show"}:
        return False
    subcommand, arguments = rest[0], rest[1:]
    if subcommand == "log":
        return True
    if "--stat" in arguments or "--" in arguments:
        return False
    # A diff/show target without ``--`` can be a revision or a path.  Only
    # known revision syntax is accepted; ambiguity routes nowhere.
    return not any(not value.startswith("-") and not re.fullmatch(r"[0-9A-Fa-f]{4,}|HEAD(?:[~^][0-9]*)*", value)
                   for value in arguments)


def is_routable_command(command: str) -> bool:
    """Narrow, side-effect-free command classes worth pre-context routing."""
    if not isinstance(command, str) or not command.strip() or is_wrapped_command(command):
        return False
    if re.search(r"\|[^|]|<<|(?<![<>])>{1,2}(?!&)|(?:^|[\s;])tee(?:\s|$)", command):
        return False
    try:
        import shlex
        words = shlex.split(command)
    except ValueError:
        return False
    if not words or any(token in {"watch", "sleep", "-f", "--watch", "--watchAll"} for token in words):
        return False
    lowered = [word.lower() for word in words]
    if _git_routable(words):
        return True
    joined = " ".join(lowered)
    if re.search(r"(?:^|\s)pytest(?:\s|$)", joined) or re.search(r"(?:^|\s)python(?:3)?\s+-m\s+(?:pytest|unittest)(?:\s|$)", joined):
        return True
    if re.search(r"(?:^|\s)(?:npm|pnpm|yarn)\s+test(?:\s|$)", joined):
        return True
    if re.search(r"(?:^|\s)go\s+test(?:\s|$)|(?:^|\s)cargo\s+test(?:\s|$)", joined):
        return True
    if re.search(r"(?:^|\s)(?:npm|pnpm|yarn)\s+(?:run\s+)?(?:build|lint)(?:\s|$)|(?:^|\s)(?:make|cargo|go)\s+(?:build|lint|clippy|vet)(?:\s|$)|(?:^|\s)(?:ruff|mypy|eslint|tsc|gradle|mvn)(?:\s|$)", joined):
        return True
    program = os.path.basename(words[0]).lower()
    if program in {"grep", "rg"}:
        recursive = program == "rg" or any(word in {"-r", "-R", "--recursive"} for word in words[1:])
        bounded = any(word == "-n" or word.startswith("-n") or word in {"-m", "--max-count"}
                      or word.startswith("--max-count=") for word in words[1:])
        return recursive and not bounded
    return False


def output_directory(state_root: str) -> Path:
    return Path(state_root) / "routing" / OUTPUT_DIR


def ledger_path(state_root: str) -> str:
    return str(Path(state_root) / "routing" / LEDGER)


def prune_output_files(state_root: str, *, now: float | None = None) -> None:
    """Remove only old captured output files, never ledgers or queue state."""
    directory = output_directory(state_root)
    if not directory.is_dir():
        return
    cutoff = (time.time() if now is None else now) - RETENTION_SECONDS
    for path in directory.glob("*.output"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            # Retention is best effort; a capture must never fail because an
            # older file could not be pruned.
            pass


def capture_output(state_root: str, output: bytes, *, now: float | None = None) -> str:
    """Write one owner-only full-output file and return its absolute path."""
    prune_output_files(state_root, now=now)
    path = output_directory(state_root) / (uuid.uuid4().hex + ".output")
    store.atomic_write_bytes(str(path), output)
    return str(path)


def _raw(output: bytes) -> None:
    stream = getattr(sys.stdout, "buffer", None)
    # Text written before (a waiver or header line) must reach the stream
    # first; the byte buffer bypasses the text layer's pending writes.
    sys.stdout.flush()
    if stream is not None:
        stream.write(output)
        stream.flush()
    else:  # pragma: no cover - StringIO replacement in embedders
        sys.stdout.write(output.decode("utf-8", "replace"))
        sys.stdout.flush()


def _fit_for_queue(output: bytes, max_input_bytes: int) -> tuple[bytes, bool]:
    """Head and tail of large output, sized under the queue's input cap.

    The queue measures its normalized (JSON-escaped) input, which grows with
    escaped characters, so the raw budget keeps generous headroom. The
    failure tail and the full-output file always come from the whole output.
    """
    budget = max(1024, int(max_input_bytes * 0.6))
    if len(output) <= budget:
        return output, False
    half = budget // 2
    marker = b"\n...[middle omitted for local digest]...\n"
    return output[:half] + marker + output[-half:], True


def _failure_tail(output: bytes) -> str:
    matches = [line for line in output.decode("utf-8", "replace").splitlines()
               if FAILURE_LINE.search(line)]
    return "\n".join(matches[-40:])


def _digest_text(result: dict[str, Any]) -> str | None:
    payload = result.get("result")
    if not isinstance(payload, dict):
        return None
    output = payload.get("output")
    if isinstance(output, dict) and isinstance(output.get("text"), str):
        return output["text"]
    if isinstance(output, str):
        return output
    return None


def _record(state_root: str, *, bytes_count: int, repo: str, classification: str,
            routed: bool, waiver_reason: str | None, job_id: str | None,
            duration: float) -> None:
    # Content-free by construction: no command, output, hash, prompt, or
    # digest is permitted in this ledger.
    try:
        store.append_ledger(ledger_path(state_root), {
            "bytes": bytes_count, "repo": repo, "classification": classification,
            "routed": routed, "waiver_reason": waiver_reason, "job_id": job_id,
            "duration": duration,
        })
    except OSError:
        # The output path still visibly waives; a broken state root cannot
        # turn a completed original command into a wrapper exception.
        pass


def route_output(output: bytes, *, state_root: str, repo: str, classification: str,
                 intake: AutomaticIntake, executor: Callable[[], Any] | None = None,
                 exit_code: int = 0, wait_seconds: float = WAIT_SECONDS,
                 clock: Callable[[], float] = time.monotonic) -> int:
    """Print the context-safe response for a captured command and return 0.

    The caller owns the original command's exit status.  This function never
    raises an output-routing error through that status: it emits a waiver and
    the exact original output instead.
    """
    started = clock()
    job_id: str | None = None
    waiver: str | None = None
    routed = False
    try:
        full_path = capture_output(state_root, output)
        if len(output) < SMALL_OUTPUT_BYTES:
            _raw(output)
            _record(state_root, bytes_count=len(output), repo=repo, classification=classification,
                    routed=False, waiver_reason="below_threshold", job_id=None,
                    duration=clock() - started)
            return 0
        # Public admission probe before a checkpoint or submission. The queue
        # repeats admission at execution, which remains authoritative.
        intake.queue.admit("interactive")
        local_bytes, clipped = _fit_for_queue(output, intake.queue.caps.max_input_bytes)
        text = local_bytes.decode("utf-8", "replace")
        # Identity is the whole output: two outputs sharing a head and tail
        # must never collapse into one job.
        task_id = "inline-output:" + hashlib.sha256(output).hexdigest()
        checkpoint = intake.checkpoint(
            task_id=task_id, task_type="summarize", classification=classification,
            caller="claude", input_bytes=len(local_bytes),
            nonblank_lines=sum(1 for line in text.splitlines() if line.strip()),
            risk_flags=[], idempotency_key=task_id)
        params = ({} if intake.queue.backend_id == "gemma_certified"
                  else {"instruction": DIGEST_PROMPT})
        receipt = intake.route(
            task_type="summarize", input=text, params=params,
            priority="interactive", classification=classification, caller="claude",
            purpose="work", checkpoint_id=checkpoint["checkpoint_id"])
        job_id = receipt.get("job_id") if isinstance(receipt.get("job_id"), str) else None
        if receipt.get("decision") != "local" or not job_id:
            waiver = "intake_refused:" + str(receipt.get("reason", "unknown"))
            raise RuntimeError(waiver)
        deadline = started + wait_seconds
        while clock() < deadline:
            result = intake.queue.result(job_id)
            if (result.get("status") == "queued"
                    and str(result.get("error") or "").startswith("deferred:")):
                waiver = str(result["error"])
                break
            if result.get("ready"):
                digest = _digest_text(result)
                if result.get("status") == "complete" and digest is not None:
                    routed = True
                    scope = "; digest covers head and tail only" if clipped else ""
                    header = f"[local digest of {len(output)} bytes{scope}; full output: {full_path}; exit {exit_code}]\n"
                    sys.stdout.write(header)
                    sys.stdout.write(digest)
                    if not digest.endswith("\n"):
                        sys.stdout.write("\n")
                    tail = _failure_tail(output)
                    if tail:
                        sys.stdout.write(tail + "\n")
                    _record(state_root, bytes_count=len(output), repo=repo, classification=classification,
                            routed=True, waiver_reason=None, job_id=job_id,
                            duration=clock() - started)
                    return 0
                waiver = "local_" + str(result.get("status", "failed"))
                break
            if executor is not None:
                executor()
            else:
                time.sleep(min(0.05, max(0.0, deadline - clock())))
        if waiver is None:
            waiver = "timeout"
    except AdmissionError as exc:
        waiver = "admission_deferred:" + str(exc)
    except Exception as exc:  # noqa: BLE001 - wrapper must preserve child exit
        waiver = waiver or "routing_failure:" + type(exc).__name__
    sys.stdout.write(f"[local output routing waived: {waiver}]\n")
    _raw(output)
    _record(state_root, bytes_count=len(output), repo=repo, classification=classification,
            routed=routed, waiver_reason=waiver, job_id=job_id, duration=clock() - started)
    return 0


def run_command(command: list[str], *, config_path: str) -> int:
    """Execute original argv, then route its captured output without changing its code."""
    try:
        # One combined pipe preserves command output ordering, unlike two
        # independent pipes concatenated after completion.
        completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   check=False)
    except OSError as exc:
        sys.stdout.write(f"[local output routing waived: command_failure:{type(exc).__name__}]\n")
        return 127
    output = completed.stdout
    try:
        from .config import load
        from ..localq.service import Service

        cfg = load(config_path)
        state_root = str(cfg.state_root)
        repo = autoroute.repo_key(os.getcwd())
        policy = autoroute.load_policy(state_root)
        repo_policy = policy.for_repo(repo or os.getcwd())
        service = Service.for_config(cfg)
        route_output(output, state_root=state_root, repo=repo or os.getcwd(),
                     classification=repo_policy.classification,
                     # The local queue service owns execution.  Running it
                     # here could let one 60-second queue job overrun this
                     # wrapper's 45-second wait bound.
                     intake=AutomaticIntake(service.queue), executor=None,
                     exit_code=int(completed.returncode))
        return int(completed.returncode)
    except Exception as exc:  # noqa: BLE001 - wrapper itself never masks child execution
        sys.stdout.write(f"[local output routing waived: setup_failure:{type(exc).__name__}]\n")
        _raw(output)
        return int(completed.returncode)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-bridge-output-router")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--config", required=True)
    run.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = list(args.argv)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("run requires an original command after --")
    return run_command(command, config_path=args.config)


if __name__ == "__main__":
    raise SystemExit(main())
