"""Live synthetic verification for the automatic-delegation opt-in.

Exercises exactly three synthetic, non-client directions: Codex-to-Claude
bounded execution, Claude-to-Codex bounded execution, and eligible work to
the configured local model. It never applies a patch, commits, pushes,
merges, downloads a model, enables paid API fallback, or admits
client-derived/confidential input. It requires this machine's own signed-in
provider CLIs and a supported platform for the execution harnesses, so it
cannot be exercised from an offline test; ``delegation.validate_evidence``
is what an offline test checks against a fixed, synthetic result document.

The ``calibrate`` subcommand is a separate live check for the local-first
read gate (``docs/LOCAL-FIRST-DESIGN.md``): it measures the configured local
model's own latency at three window sizes on this machine and writes the
record ``readiness()`` reads. It shares this module's isolation discipline
(a disposable queue root, never the operator's real one) but nothing else:
it makes no provider calls, and it refuses outright rather than measuring a
machine that is busy or throttled, because a number measured under load is
not a number the gate should trust.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sqlite3
import statistics
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from .. import setup_cmd, store
from . import autoroute, delegation, localfirst
from .config import load as load_orchestration_config
from .execution_queue import (
    ExecutionAdmissionError,
    ExecutionQueue,
    Harnesses,
    SubprocessHarnessExecutor,
    reserve_nothing,
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
CALIBRATION_RESOURCE_WAIT_ATTEMPTS = 120
CALIBRATION_RESOURCE_POLL_SECONDS = 5.0


def _empty_row(reason: str) -> dict[str, Any]:
    return {"attempted": False, "reason": reason, "state": None, "returncode": None,
            "harness_ok": None, "harness_status": None, "harness_verdict": None,
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


def _verification_queue_root() -> Path:
    """A queue root that exists only for this one synthetic job.

    Never the configured production queue. ``run_once`` selects the oldest
    queued job in whatever root it was given, not the job that was just
    submitted, so draining the production queue here would execute the user's
    real queued work, send it to a provider, and consume its receipt as a side
    effect of an opt-in check. An isolated root makes that structurally
    impossible rather than merely unlikely.
    """

    return Path(tempfile.mkdtemp(prefix="agent-bridge-verify-queue-"))


def _remove_tree(path: Path, *, ignore_errors: bool = False) -> bool:
    """Remove one verifier-owned tree, including read-only Git files on Windows."""

    def make_writable_and_retry(function: Any, blocked_path: str,
                                _error: Any) -> None:
        os.chmod(blocked_path, stat.S_IWRITE)
        function(blocked_path)

    try:
        shutil.rmtree(path, onerror=make_writable_and_retry)
    except OSError:
        if not ignore_errors:
            raise
    return not path.exists()


def select_executor(cfg: Any, *, platform_name: str | None = None) -> tuple[Any, str]:
    """The executor this machine actually dispatches through.

    On Windows that is :class:`~.windows_delegation.WindowsWslExecutor`, built
    from the verification record on this host. Verification has to use the
    same executor production does, or it verifies something nobody runs: a
    POSIX subprocess harness on a Windows machine would pass here and then be
    replaced at dispatch time by an executor with entirely different gates.

    The Windows executor is built even when nothing is verified. It then
    refuses every job by name, which is a far more useful answer than an
    unavailable harness, and it is the only caller positioned to record live
    evidence once a real run succeeds.
    """

    from .windows_preflight import is_windows

    if is_windows(platform_name):
        from . import windows_delegation

        missing = [name for name in ("windows_wsl_runtime_root",
                                     "windows_wsl_rootfs_path",
                                     "windows_wsl_manifest_path")
                   if getattr(cfg, name, None) is None]
        if missing:
            return None, "windows_wsl_configuration_missing"
        config = windows_delegation.DelegationConfig(
            runtime_root=cfg.windows_wsl_runtime_root,
            rootfs_path=cfg.windows_wsl_rootfs_path,
            manifest_path=cfg.windows_wsl_manifest_path,
            sidecar_path=getattr(cfg, "windows_wsl_sidecar_path", None))
        return windows_delegation.verified_executor(
            config, platform_name=platform_name), ""

    if cfg.execution_queue_root is None:
        return None, "execution_configuration_missing"
    try:
        return SubprocessHarnessExecutor(Harnesses(
            codex=cfg.codex_task_executable, claude=cfg.claude_task_executable,
            python=cfg.python_executable,
            claude_config_dir=cfg.claude_config_dir)), ""
    except ExecutionAdmissionError as exc:
        return None, str(exc) or type(exc).__name__


def _run_direction(executor: Any, _unused_queue_root: Any,
                   *, caller: str, provider: str) -> dict[str, Any]:
    """Submit and drain exactly one synthetic job for one direction.

    Never applies, commits, pushes, or merges: the harness only returns an
    unapplied receipt, and this function does nothing to the disposable
    repository beyond removing it afterward. The queue it drains is created
    here and destroyed here; the configured queue root is deliberately not
    used, and is accepted only so callers do not have to change.
    """
    repo = _disposable_repo()
    brief = repo / ".agent-bridge-verify-brief.txt"
    brief.write_text(SYNTHETIC_BRIEF, encoding="utf-8")
    verify_argv = [["git", "status"]]
    queue_root = _verification_queue_root()
    try:
        # reserve_nothing, deliberately: this queue is created and destroyed
        # here, drains exactly one fixed synthetic job with model="default",
        # and is not a surface anything can steer a model choice through. It
        # also has no cfg to read a policy from.
        queue = ExecutionQueue(queue_root, executor, recover_interrupted=False,
                               model_reserved=reserve_nothing)
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
        _remove_tree(repo, ignore_errors=True)
        _remove_tree(queue_root, ignore_errors=True)
        return _empty_row(str(exc) or type(exc).__name__)
    finally:
        # The receipt is already read; the queue was only ever scaffolding.
        _remove_tree(queue_root, ignore_errors=True)
    removed = _remove_tree(repo, ignore_errors=True)
    harness = receipt.get("harness") or {}
    return {
        "attempted": True, "reason": None, "state": receipt.get("state"),
        "returncode": harness.get("returncode"),
        # The harness's own verdict, carried through rather than re-derived.
        # A zero exit code is a fact about a process; these are the fact about
        # the task, and the evidence gate reads them through the same
        # ``outcome_is_success`` the queue used to set ``state``.
        "harness_ok": harness.get("harness_ok"),
        "harness_status": harness.get("harness_status"),
        "harness_verdict": harness.get("harness_verdict"),
        "source_classification": receipt.get("classification"),
        "worktree_removed": removed,
        "permission_to_apply": receipt.get("permission_to_apply", False),
        "permission_to_commit": receipt.get("permission_to_commit", False),
        "permission_to_push": receipt.get("permission_to_push", False),
        "permission_to_merge": receipt.get("permission_to_merge", False),
    }


#: The one fixed instruction every local-model verification/calibration run
#: sends. It matches the certified delegate's own certified summarize
#: prompt (``gemma_child.CERTIFIED_SUMMARIZE_INSTRUCTION``) exactly, so a
#: run against ``gemma_certified`` sends the certified prompt rather than a
#: second, separately-invented one.
_LOCAL_MODEL_INSTRUCTION = "Summarize in one sentence."


def _local_model_params(cfg: Any) -> dict[str, Any]:
    """The params object this check/calibration actually sends, matching
    whichever backend ``cfg`` configures rather than a hard-coded provider.

    ``gemma_certified`` accepts no "provider" key at all -- nothing here
    selects Qwen, Apple, or any other route for it -- so it is simply
    omitted for that backend; the private worker keeps sending the explicit
    "auto" it always effectively meant, exercising its own automatic
    Apple/Qwen selection precisely as before.
    """
    params: dict[str, Any] = {"instruction": _LOCAL_MODEL_INSTRUCTION}
    if getattr(cfg, "local_backend", "private_worker") != "gemma_certified":
        params["provider"] = "auto"
    return params


def _local_model_check(cfg: Any) -> dict[str, Any]:
    backend = getattr(cfg, "local_backend", "private_worker")
    if backend != "gemma_certified":
        worker = Path(cfg.worker_executable)
        if worker.name == delegation.NO_WORKER_SENTINEL or not worker.is_file():
            return {"status": "not_configured"}
    from ..localq.service import Service  # deferred: only needed on this path
    # Same isolation as the provider directions, for the same reason:
    # ``service.once()`` runs whatever is queued, and the user's own local
    # queue is not this check's to consume.
    queue_root = Path(tempfile.mkdtemp(prefix="agent-bridge-verify-localq-"))
    try:
        # The private-worker branch keeps the exact three-positional-argument
        # construction this always used, unchanged: existing callers (and
        # tests that substitute a fake ``Service``) keep working without
        # having to know about ``for_config``. Only "gemma_certified" needs
        # that classmethod, to build the certified delegate's own backend.
        service = (Service.for_config(cfg, root=str(queue_root)) if backend == "gemma_certified"
                  else Service(str(queue_root), str(cfg.worker_executable), str(cfg.worker_state)))
        return _local_model_check_in(cfg, service)
    finally:
        shutil.rmtree(queue_root, ignore_errors=True)


def _local_model_check_in(cfg: Any, service: Any) -> dict[str, Any]:
    submitted = service.queue.submit(
        task_type="summarize", input=LOCAL_MODEL_INPUT,
        params=_local_model_params(cfg),
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


#: A fixed, deterministic ASCII line so the synthetic input is exactly the
#: requested byte length: every character is one UTF-8 byte, so slicing the
#: ASCII-only sentence stems keep byte and character lengths identical. Sixteen
#: long, distinct sentences exercise the real summarize selector without the
#: hundreds of near-duplicate sentence IDs created by the original repeated
#: line. Sixteen also keeps every sentence below the delegate's ten-percent
#: summary-output budget at all calibrated sizes. No real or invented user
#: narrative is present.
_CALIBRATION_STEMS = tuple(
    f"Synthetic workload record {index} measures an invented queue with no client data and notes that "
    for index in range(1, 17)
)


def _synthetic_calibration_input(byte_length: int, run_index: int = 0) -> str:
    if byte_length < 1024 or run_index < 0 or run_index > 99:
        raise ValueError("calibration_input_invalid")
    sentence_bytes = byte_length - (len(_CALIBRATION_STEMS) - 1)
    base, extra = divmod(sentence_bytes, len(_CALIBRATION_STEMS))
    sentences = []
    for index, stem in enumerate(_CALIBRATION_STEMS):
        target = base + (1 if index < extra else 0)
        prefix = f"{stem}run {run_index:02d} position {index + 1} contains "
        filler_length = target - len(prefix) - 1
        if filler_length < 1:
            raise ValueError("calibration_input_invalid")
        filler_unit = f"neutral measured detail {index + 1} remains stable "
        filler = (filler_unit * (filler_length // len(filler_unit) + 1))[:filler_length]
        sentences.append(prefix + filler + ".")
    result = " ".join(sentences)
    if len(result.encode("ascii")) != byte_length:
        raise AssertionError("calibration_input_size_mismatch")
    return result


def _production_queue_busy(local_queue_root: Any) -> bool:
    """Whether the operator's real local queue currently shows a running job.

    Read-only, and never creates the database: a queue that has never run a
    job is not busy. Calibration must not compete with a real job for the
    same physical model, so this is checked against the *configured*
    ``local_queue_root`` before anything is submitted to the disposable
    queue calibration actually measures against.
    """
    database = os.path.join(str(local_queue_root), "localq.sqlite3")
    if not os.path.isfile(database):
        return False
    uri = ("file:" + os.path.realpath(database).replace("%", "%25")
          .replace("?", "%3F").replace("#", "%23") + "?mode=ro")
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.Error:
        return False
    try:
        row = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE status='running'").fetchone()
        return bool(row and row[0])
    except sqlite3.Error:
        return False
    finally:
        connection.close()


def calibrate(config_path: str, *, clock: Any = time.time,
              sampler: Any = None, monotonic: Any = time.monotonic,
              sleeper: Any = time.sleep) -> dict[str, Any]:
    """Measure the configured local model's latency at three window sizes.

    Refuses rather than measuring, in order: the production queue showing a
    job in flight right now (``calibration_refused:executor_busy``); no
    usable worker configured (``calibration_refused:worker_not_configured``);
    and this machine's own resource sample not admitting interactive work
    (``calibration_refused:resource_<reason>``). Only past all three does it
    submit anything, and everything it submits goes to a queue root created
    and destroyed inside this call, never the configured one.

    ``sampler`` is the same seam ``localq.service.Service`` already exposes,
    threaded through rather than re-invented: omitted (the CLI never passes
    one), this measures with the real ``MacSampler``, exactly what
    production does. A test supplies a portable stand-in so the happy path
    is exercised on every CI platform, not only macOS, the same reasoning
    ``tests/test_automatic_delegation_e2e.py`` already applies to the local
    lane's own end-to-end coverage.
    """
    cfg = load_orchestration_config(config_path)
    if _production_queue_busy(cfg.local_queue_root):
        return {"ok": False, "error": "calibration_refused:executor_busy"}

    gemma_backend = getattr(cfg, "local_backend", "private_worker") == "gemma_certified"
    if not gemma_backend:
        worker = Path(cfg.worker_executable)
        if worker.name == delegation.NO_WORKER_SENTINEL or not worker.is_file():
            return {"ok": False, "error": "calibration_refused:worker_not_configured"}
    # The executable this calibration's own record identifies and hashes:
    # the configured productive backend's own binary, never a hard-coded
    # one. For "private_worker" this is unchanged (``cfg.worker_executable``);
    # for "gemma_certified" it is the certified delegate itself, which
    # ``orchestration.config.load`` already required to exist.
    calibration_target = (cfg.gemma_delegate_executable if gemma_backend
                          else cfg.worker_executable)

    from ..localq.service import Service  # deferred: only needed on this path

    queue_root = Path(tempfile.mkdtemp(prefix="agent-bridge-calibrate-"))
    try:
        service = Service.for_config(cfg, root=str(queue_root), sampler=sampler)
        # Wait out a transient deferral (a warm or busy moment, or a one-off
        # sampler failure) with the same budget each sample run gets, rather
        # than refusing on one reading. Total: at most 120 probes and 119
        # sleeps (about ten minutes) before the per-sample waits begin.
        for wait_index in range(CALIBRATION_RESOURCE_WAIT_ATTEMPTS):
            resource = service.queue.state_report().get("resource", {})
            verdict = resource.get("verdict") if isinstance(resource, dict) else None
            if isinstance(verdict, dict):
                admissible = verdict.get("interactive") == "admissible"
                detail = "interactive_not_admissible"
            else:
                admissible = False
                detail = resource.get("reason", "unavailable") if isinstance(resource, dict) else "unavailable"
            if admissible:
                break
            if wait_index + 1 < CALIBRATION_RESOURCE_WAIT_ATTEMPTS:
                sleeper(CALIBRATION_RESOURCE_POLL_SECONDS)
        if not admissible:
            return {"ok": False, "error": f"calibration_refused:resource_{detail}"}

        sizes: dict[str, Any] = {}
        for size in localfirst.CALIBRATION_SIZES:
            runs_s: list[float] = []
            resource_waits_s: list[float] = []
            outcomes: list[str] = []
            for run_index in range(localfirst.CALIBRATION_RUNS_PER_SIZE):
                # Back-to-back model calls can legitimately make the normal
                # resource guard defer the next job while the Mac cools. Wait
                # for that guard *before* starting the latency clock instead
                # of hammering run_once until a fixed attempt count expires
                # and misreporting a never-started job as a model failure.
                wait_started = monotonic()
                admitted = False
                for wait_index in range(CALIBRATION_RESOURCE_WAIT_ATTEMPTS):
                    current_resource = service.queue.state_report().get("resource", {})
                    current_verdict = (current_resource.get("verdict", {})
                                       if isinstance(current_resource, dict) else {})
                    if (isinstance(current_verdict, dict)
                            and current_verdict.get("interactive") == "admissible"):
                        admitted = True
                        break
                    if wait_index + 1 < CALIBRATION_RESOURCE_WAIT_ATTEMPTS:
                        sleeper(CALIBRATION_RESOURCE_POLL_SECONDS)
                resource_waits_s.append(monotonic() - wait_started)
                if not admitted:
                    outcomes.append("failed")
                    runs_s.append(0.0)
                    continue

                text = _synthetic_calibration_input(size, run_index)
                submitted = service.queue.submit(
                    task_type="summarize", input=text,
                    params=_local_model_params(cfg),
                    priority="interactive", classification="synthetic", caller="codex",
                    purpose="test",
                    idempotency_key=f"calibrate-{size}-{run_index}-{int(clock() * 1000)}")
                job_id = submitted["job_id"]
                started = clock()
                drain_deadline = monotonic() + float(service.queue.caps.timeout_seconds) + 60.0
                while monotonic() < drain_deadline:
                    if service.queue.status(job_id)["status"] in ("complete", "failed"):
                        break
                    service.queue.run_once("localq-calibration")
                    if service.queue.status(job_id)["status"] == "queued":
                        sleeper(CALIBRATION_RESOURCE_POLL_SECONDS)
                elapsed = clock() - started
                outcome = service.queue.result(job_id)
                status = outcome.get("status", "failed")
                outcomes.append("complete" if status == "complete" else "failed")
                runs_s.append(elapsed)
            eligible = [seconds for seconds, outcome in zip(runs_s, outcomes)
                       if outcome == "complete"]
            # A run that never reached ``complete`` makes the whole tier
            # ineligible: a median over a failure is not a latency, and
            # ``readiness`` refuses to trust a size unless every one of its
            # measured runs actually finished (see ``_covering_calibration``).
            sizes[str(size)] = {
                "runs_s": runs_s, "outcomes": outcomes,
                "resource_waits_s": resource_waits_s,
                "median_s": statistics.median(eligible) if len(eligible) == len(runs_s) else None,
                "max_s": max(eligible) if len(eligible) == len(runs_s) else None,
            }

        host = {"platform": platform.system(), "cpu_count": os.cpu_count(),
               "python_version": platform.python_version()}
        record = localfirst.build_calibration_record(
            worker_executable=str(calibration_target), worker_state=str(cfg.worker_state),
            sizes=sizes, sampler_snapshot=resource, host=host,
            backend_id=str(getattr(cfg, "local_backend", "private_worker")), clock=clock)
        localfirst.write_calibration_record(str(cfg.state_root), record)
    finally:
        shutil.rmtree(queue_root, ignore_errors=True)

    try:
        budget = autoroute.load_policy(str(cfg.state_root)).local_first.latency_budget_seconds
    except autoroute.PolicyError:
        budget = autoroute.LocalFirstConfig().latency_budget_seconds
    fits_budget = {size: (entry["median_s"] is not None and entry["median_s"] <= budget)
                  for size, entry in sizes.items()}
    ready = bool(fits_budget) and all(fits_budget.values())
    result = {"ok": ready, "record": record, "latency_budget_seconds": budget,
              "fits_budget": fits_budget}
    if not ready:
        # The durable record remains useful diagnostic evidence, but a CLI
        # caller (especially the activation launcher) must not confuse
        # "measurement finished" with "the configured route is ready".
        result["error"] = "calibration_failed:incomplete_or_over_budget"
    return result


def run(config_path: str, *, callers: tuple[str, ...]) -> dict[str, Any]:
    cfg = load_orchestration_config(config_path)
    cfg_doc = store.read_json(config_path)
    required = delegation.required_directions_for(callers)
    directions: dict[str, Any] = {d: _empty_row("not_requested") for d in delegation.DIRECTIONS}

    executor, executor_error = select_executor(cfg)

    for direction in required:
        caller, provider = direction.split("->")
        if executor is None:
            directions[direction] = _empty_row(executor_error or "execution_harness_unavailable")
            continue
        directions[direction] = _run_direction(
            executor, None, caller=caller, provider=provider)

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
    # Kept directly on the main parser, and not required here, so the
    # documented invocation (INSTALL.md: no subcommand, --config/--callers/
    # --out) keeps working unchanged. The default branch below enforces that
    # all three are present exactly as ``required=True`` used to.
    parser.add_argument("--config", help="Private orchestration config path.")
    parser.add_argument("--callers",
                        help="Comma-separated subset of codex,claude naming which side is verified.")
    parser.add_argument("--out", help="Durable path for the result artifact.")
    sub = parser.add_subparsers(dest="command")
    calibrate_parser = sub.add_parser(
        "calibrate",
        help="Measure the configured local model's latency for the local-first read gate. "
             "Writes <state_root>/local/calibration.json. Makes no provider calls.")
    calibrate_parser.add_argument("--config", required=True,
                                  help="Private orchestration config path.")
    args = parser.parse_args(argv)
    store.set_umask()

    if args.command == "calibrate":
        try:
            result = calibrate(args.config)
        except Exception as exc:  # noqa: BLE001 - always leave a durable, honest record
            print(f"calibration failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
        return 0 if result.get("ok") else 1

    if not args.config or not args.callers or not args.out:
        parser.error("--config, --callers and --out are required")
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
    if cfg_doc.get("local_backend") == "gemma_certified":
        # ``orchestration.config.load`` already required every gemma_*
        # path to exist before this config could load at all, so the local
        # lane is unconditionally required and there is no
        # "not_configured" state for it to have.
        local_required = True
    else:
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
