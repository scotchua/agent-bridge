"""Live synthetic verification for the automatic-delegation opt-in.

Exercises exactly three synthetic, non-client directions: Codex-to-Claude
bounded execution, Claude-to-Codex bounded execution, and eligible work to
the configured local model. It never applies a patch, commits, pushes,
merges, downloads a model, enables paid API fallback, or admits
client-derived/confidential input. It requires this machine's own signed-in
provider CLIs and a supported platform for the execution harnesses, so it
cannot be exercised from an offline test; ``delegation.validate_evidence``
is what an offline test checks against a fixed, synthetic result document.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from .. import setup_cmd, store
from . import delegation
from .config import load as load_orchestration_config
from .execution_queue import (
    ExecutionAdmissionError,
    ExecutionQueue,
    Harnesses,
    SubprocessHarnessExecutor,
)

SYNTHETIC_BRIEF = (
    "This is a synthetic, non-client verification brief for agent-bridge automatic "
    "delegation. Append exactly one line, the text 'agent-bridge synthetic "
    "verification ok', to a file named VERIFICATION.md in this disposable worktree, "
    "creating the file if it does not exist. Do not read, modify, or reference any "
    "other file. Then reply that the bounded harness executed successfully, and stop.\n"
)
LOCAL_MODEL_INPUT = "\n".join(
    f"Synthetic verification line {i}: agent-bridge automatic delegation local-model check."
    for i in range(1, 16)
) + "\n"
TERMINAL_STATES = frozenset({"complete", "failed", "blocked"})
MAX_DRAIN_ATTEMPTS = 50


def _empty_row(reason: str) -> dict[str, Any]:
    return {"attempted": False, "reason": reason, "state": None, "returncode": None,
            "source_classification": None, "worktree_removed": None,
            "permission_to_apply": None, "permission_to_commit": None,
            "permission_to_push": None, "permission_to_merge": None}


def _disposable_repo() -> Path:
    directory = Path(tempfile.mkdtemp(prefix="agent-bridge-delegation-verify-"))
    subprocess.run(["git", "init", "--quiet", str(directory)], check=True, shell=False)
    (directory / "README.md").write_text("synthetic verification fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(directory), "add", "README.md"], check=True, shell=False)
    subprocess.run(["git", "-C", str(directory), "-c", "user.email=verify@example.invalid",
                    "-c", "user.name=agent-bridge-verify", "commit", "--quiet", "-m", "synthetic"],
                   check=True, shell=False)
    return directory


def _run_direction(executor: SubprocessHarnessExecutor, queue_root: Path, *,
                    caller: str, provider: str) -> dict[str, Any]:
    """Submit and drain exactly one synthetic job for one direction.

    Never applies, commits, pushes, or merges: the harness only returns an
    unapplied receipt, and this function does nothing to the disposable
    repository beyond removing it afterward.
    """
    repo = _disposable_repo()
    brief = repo / ".agent-bridge-verify-brief.txt"
    brief.write_text(SYNTHETIC_BRIEF, encoding="utf-8")
    verify_argv = [["git", "status"]]
    try:
        queue = ExecutionQueue(queue_root, executor, recover_interrupted=False)
        submitted = queue.submit(
            caller=caller, provider=provider, repo=str(repo), brief=str(brief),
            base="HEAD", classification="synthetic", model="default", effort="low",
            item_id="delegation-verify", stage="synthetic-check", owner_id="delegation-verify",
            stage_revision=0, verify_argv=verify_argv, timeout_seconds=300,
            idempotency_key=f"delegation-verify-{caller}-{int(time.time())}")
        job_id = submitted["job_id"]
        for _ in range(MAX_DRAIN_ATTEMPTS):
            if queue.status(job_id)["state"] in TERMINAL_STATES:
                break
            queue.run_once(f"delegation-verify-{caller}")
        receipt = queue.result(job_id)
    except ExecutionAdmissionError as exc:
        shutil.rmtree(repo, ignore_errors=True)
        return _empty_row(str(exc) or type(exc).__name__)
    removed = True
    try:
        shutil.rmtree(repo)
    except OSError:
        removed = False
    harness = receipt.get("harness") or {}
    return {
        "attempted": True, "reason": None, "state": receipt.get("state"),
        "returncode": harness.get("returncode"),
        "source_classification": receipt.get("classification"),
        "worktree_removed": removed,
        "permission_to_apply": receipt.get("permission_to_apply", False),
        "permission_to_commit": receipt.get("permission_to_commit", False),
        "permission_to_push": receipt.get("permission_to_push", False),
        "permission_to_merge": receipt.get("permission_to_merge", False),
    }


def _local_model_check(cfg: Any) -> dict[str, Any]:
    worker = Path(cfg.worker_executable)
    if worker.name == delegation.NO_WORKER_SENTINEL or not worker.is_file():
        return {"status": "not_configured"}
    from ..localq.service import Service  # deferred: only needed on this path
    service = Service(str(cfg.local_queue_root), str(cfg.worker_executable), str(cfg.worker_state))
    submitted = service.queue.submit(
        task_type="summarize", input=LOCAL_MODEL_INPUT,
        params={"instruction": "Summarize in one sentence.", "provider": "qwen"},
        priority="interactive", classification="synthetic", caller="codex", purpose="test",
        idempotency_key=f"delegation-verify-local-{int(time.time())}")
    job_id = submitted["job_id"]
    for _ in range(MAX_DRAIN_ATTEMPTS):
        if service.queue.status(job_id)["status"] in ("complete", "failed"):
            break
        service.once()
    outcome = service.queue.result(job_id)
    if outcome.get("status") != "complete":
        return {"status": outcome.get("status", "failed"), "detail": outcome.get("error")}
    return {"status": "complete", "source_classification": "synthetic"}


def run(config_path: str, *, callers: tuple[str, ...]) -> dict[str, Any]:
    cfg = load_orchestration_config(config_path)
    cfg_doc = store.read_json(config_path)
    required = delegation.required_directions_for(callers)
    directions: dict[str, Any] = {d: _empty_row("not_requested") for d in delegation.DIRECTIONS}

    executor: SubprocessHarnessExecutor | None = None
    executor_error: str | None = None
    if cfg.execution_queue_root is None:
        executor_error = "execution_configuration_missing"
    else:
        try:
            executor = SubprocessHarnessExecutor(Harnesses(
                codex=cfg.codex_task_executable, claude=cfg.claude_task_executable,
                python=cfg.python_executable))
        except ExecutionAdmissionError as exc:
            executor_error = str(exc) or type(exc).__name__

    for direction in required:
        caller, provider = direction.split("->")
        if executor is None:
            directions[direction] = _empty_row(executor_error or "execution_harness_unavailable")
            continue
        directions[direction] = _run_direction(
            executor, cfg.execution_queue_root, caller=caller, provider=provider)

    local_model = _local_model_check(cfg)

    return {
        "verification_profile": delegation.VERIFICATION_PROFILE,
        "effective_config_sha256": delegation.config_sha256(cfg_doc),
        "created_at": store.utc_now(),
        "directions": directions,
        "local_model": local_model,
        "no_patches_applied": True,
        "no_commits": True,
        "no_pushes": True,
        "no_merges": True,
        "no_paid_fallback": True,
        "no_client_data": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Live synthetic verification for the automatic-delegation opt-in. "
                    "Never applies, commits, pushes, or merges.")
    parser.add_argument("--config", required=True, help="Private orchestration config path.")
    parser.add_argument("--callers", required=True,
                        help="Comma-separated subset of codex,claude naming which side is verified.")
    parser.add_argument("--out", required=True, help="Durable path for the result artifact.")
    args = parser.parse_args(argv)
    store.set_umask()
    callers = tuple(sorted({part.strip() for part in args.callers.split(",") if part.strip()}))
    if not callers or any(c not in ("codex", "claude") for c in callers):
        parser.error("--callers must name codex, claude, or both")
    if not setup_cmd.is_durable(args.out):
        parser.error("--out must be outside temporary directories")
    try:
        results = run(args.config, callers=callers)
    except Exception as exc:  # noqa: BLE001 - always leave a durable, honest record
        store.atomic_write_json(args.out, {
            "verification_profile": delegation.VERIFICATION_PROFILE,
            "created_at": store.utc_now(), "error": f"{type(exc).__name__}: {exc}",
        })
        print(f"delegation verification failed: {exc}", file=sys.stderr)
        return 1
    store.atomic_write_json(args.out, results)
    print(f"wrote {args.out}")
    cfg_doc = store.read_json(args.config)
    worker_path = cfg_doc.get("worker_executable", "")
    local_required = (isinstance(worker_path, str)
                      and Path(worker_path).is_file()
                      and Path(worker_path).name != delegation.NO_WORKER_SENTINEL)
    try:
        delegation.validate_evidence(
            results, cfg_doc,
            required_directions=delegation.required_directions_for(callers),
            local_worker_required=local_required)
    except delegation.DelegationVerificationError as exc:
        print(f"delegation verification incomplete: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
