"""Durable, caller-bound dispatch to the two bounded implementation harnesses.

This module is deliberately a scheduler, not a new execution sandbox.  It
invokes the existing provider-specific harness and returns only durable receipt
metadata.  It never applies a patch to the operator checkout, commits, pushes,
merges, or injects work into a desktop conversation.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from agent_bridge.platform import platform as host_platform

from ..execution import claude_config
from . import windows_privacy as wpv


ALLOWED_CLASSIFICATIONS = frozenset({"synthetic", "public", "internal_nonclient"})
PROVIDER_FOR_CALLER = {"claude": "codex", "codex": "claude"}
TERMINAL = frozenset({"complete", "failed", "blocked"})


class ExecutionAdmissionError(ValueError):
    """A dispatch request does not satisfy the execution-lane contract."""


@dataclass(frozen=True)
class Harnesses:
    codex: Path
    claude: Path
    python: Path
    # The configuration directory the Claude harness signs in against. It
    # selects a store, it is not a credential. Keeping the lane off the
    # default ~/.claude store stops a concurrent desktop refresh from signing
    # the lane out; claude_config decides which directory is allowed.
    claude_config_dir: Path | None = None


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        try:
            host_platform.enforce_owner_only_file(fd)
        except BaseException:
            # Windows cannot unlink an open file. Close before cleanup while
            # preserving the original ACL-enforcement failure.
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ExecutionAdmissionError("execution_receipt_unreadable") from exc
    if not isinstance(value, dict):
        raise ExecutionAdmissionError("execution_receipt_invalid")
    return value


class SubprocessHarnessExecutor:
    """Invoke only a configured bounded harness, with no shell or fallback."""

    def __init__(self, harnesses: Harnesses):
        # Construction stays portable so Windows MCP clients can submit and
        # inspect queue work. Actual execution is rejected in __call__ until a
        # Windows execution worker exists and has been tested.
        for name in ("codex", "claude", "python"):
            path = getattr(harnesses, name)
            if not path.is_absolute() or not path.is_file():
                raise ExecutionAdmissionError(f"{name}_harness_unavailable")
        if not os.access(harnesses.python, os.X_OK):
            raise ExecutionAdmissionError("python_harness_not_executable")
        directory = harnesses.claude_config_dir
        # Checked at construction as well as at dispatch. A wrong store found
        # here is a setup problem an operator can fix before any job is
        # claimed; found at dispatch it is a failed job.
        if directory is not None and not claude_config.is_ready(directory):
            raise ExecutionAdmissionError("claude_config_dir_unavailable")
        self.harnesses = harnesses

    def __call__(self, request: dict[str, Any], job_dir: Path) -> dict[str, Any]:
        provider = request["provider"]
        if provider == "claude" and not claude_config.is_ready(
                self.harnesses.claude_config_dir):
            # Checked before the platform check, on every platform. A missing
            # or wrong store is the operator's setup problem and is reported
            # as such wherever the dispatcher runs; the platform refusal below
            # is about this executor, not about the request.
            raise ExecutionAdmissionError("claude_config_dir_unavailable")
        if os.name != "posix":
            raise ExecutionAdmissionError("execution_worker_platform_unsupported")
        # Imported only at execution time so the queue/status MCP remains
        # importable on Windows, where the persistent worker is not supported.
        import pwd
        harness = getattr(self.harnesses, provider)
        argv = [str(self.harnesses.python), "-P", str(harness), request["brief"],
                "--repo", request["repo"], "--base", request["base"],
                "--timeout", str(request["timeout_seconds"])]
        if provider == "codex":
            argv += ["--classification", request["classification"],
                     "--model", request["model"], "--reasoning-effort", request["effort"],
                     "--tasks-dir", str(job_dir / "harness")]
        else:
            # Fail closed rather than let the harness fall back to the shared
            # default store; an unisolated lane is the recurring login-loss
            # bug, and a store that is not the lane's own is not a store this
            # dispatcher may point a subscription login at.
            directory = self.harnesses.claude_config_dir
            if not claude_config.is_ready(directory):
                raise ExecutionAdmissionError("claude_config_dir_unavailable")
            argv += ["--classification", request["classification"],
                     "--model", request["model"], "--effort", request["effort"],
                     "--task-root", str(job_dir / "harness"),
                     "--claude-config-dir", str(directory)]
        for command in request["verify_argv"]:
            argv += ["--verify-json", json.dumps(command, separators=(",", ":"))]
        account = pwd.getpwuid(os.getuid()).pw_name
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "HOME": str(Path.home()), "LANG": "C.UTF-8",
               "USER": account, "LOGNAME": account}
        process = subprocess.Popen(argv, cwd=request["repo"], env=env,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True,
                                   shell=False)
        try:
            stdout, stderr = process.communicate(timeout=request["timeout_seconds"] + 60)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            raise ExecutionAdmissionError("execution_harness_timeout")
        for name, payload in (("harness.stdout", stdout), ("harness.stderr", stderr)):
            target = job_dir / name
            target.write_bytes(payload)
            os.chmod(target, 0o600)
        summary = _harness_summary(stdout)
        return {"returncode": process.returncode,
                "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
                "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
                "stdout_bytes": len(stdout), "stderr_bytes": len(stderr),
                **summary}


#: The only harness status that means the work is done and verified. Every
#: other value, including "verification_failed", is a task that did not pass.
HARNESS_COMPLETE = "complete"

#: The complete outcome vocabulary, shared by every executor.
#:
#: There is exactly one semantic schema for an execution outcome, and every
#: executor speaks it: the POSIX ``SubprocessHarnessExecutor`` reading a
#: harness receipt, and the Windows ``WindowsWslExecutor`` translating a guest
#: response. Two executors with two different notions of "done" is how a
#: verification failure became a completed job once already; a single
#: vocabulary, enforced at the point the queue records a state, is what stops
#: it becoming one again.
HARNESS_VERIFICATION_FAILED = "verification_failed"
HARNESS_FAILED = "failed"
HARNESS_ABORTED = "aborted"
HARNESS_STATUSES = frozenset({HARNESS_COMPLETE, HARNESS_VERIFICATION_FAILED,
                              HARNESS_FAILED, HARNESS_ABORTED})

#: Fields every outcome must carry. ``harness_verdict`` says how the status was
#: learned, which is what distinguishes "the harness said it failed" from "the
#: harness said nothing we could read".
OUTCOME_REQUIRED_KEYS = ("returncode", "harness_ok", "harness_status",
                         "harness_verdict")


def validate_outcome(outcome: object) -> Mapping[str, Any]:
    """Hold an executor to the outcome contract before anything is recorded.

    Fail closed: an executor that returns a shape the queue does not
    understand is a bug that must surface as a failed job with a named
    reason, never as a job whose state was decided by whichever keys happened
    to be missing.
    """

    if not isinstance(outcome, Mapping):
        raise ExecutionAdmissionError("execution_outcome_not_a_mapping")
    for key in OUTCOME_REQUIRED_KEYS:
        if key not in outcome:
            raise ExecutionAdmissionError("execution_outcome_incomplete")
    returncode = outcome["returncode"]
    if isinstance(returncode, bool) or not isinstance(returncode, int):
        raise ExecutionAdmissionError("execution_outcome_returncode_invalid")
    if not isinstance(outcome["harness_ok"], bool):
        raise ExecutionAdmissionError("execution_outcome_harness_ok_invalid")
    if outcome["harness_status"] not in HARNESS_STATUSES:
        raise ExecutionAdmissionError("execution_outcome_harness_status_invalid")
    if not isinstance(outcome["harness_verdict"], str) or not outcome["harness_verdict"]:
        raise ExecutionAdmissionError("execution_outcome_harness_verdict_invalid")
    # A status and an ok flag that disagree is a contradiction, not a result.
    if outcome["harness_ok"] != (outcome["harness_status"] == HARNESS_COMPLETE):
        raise ExecutionAdmissionError("execution_outcome_inconsistent")
    return outcome


def _harness_summary(stdout: bytes) -> dict[str, Any]:
    """Read the harness's own verdict out of its final JSON line.

    A process that exits 0 is not a task that succeeded, and treating it as
    one is how a verification failure became a completed job. The harness
    states its verdict in the receipt it prints; that verdict is what counts.
    Unreadable output is a refusal, never an assumption of success.
    """

    for line in reversed(stdout.splitlines()):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return _unreadable("receipt_unparseable")
        if not isinstance(parsed, dict):
            return _unreadable("receipt_not_an_object")
        status = parsed.get("status")
        if status not in HARNESS_STATUSES:
            # A status outside the vocabulary is output nobody agreed on. It
            # is not a completion, and it is not silently renamed to one.
            return _unreadable("receipt_status_unknown")
        return {"harness_ok": parsed.get("ok") is True and status == HARNESS_COMPLETE,
                "harness_status": status, "harness_verdict": "read"}
    return _unreadable("receipt_absent")


def _unreadable(verdict: str) -> dict[str, Any]:
    """No usable verdict is an aborted job, never an assumed one."""

    return {"harness_ok": False, "harness_status": HARNESS_ABORTED,
            "harness_verdict": verdict}


def outcome_is_success(outcome: Mapping[str, Any]) -> bool:
    """Both gates: the process exited 0 *and* the harness said it completed.

    Either alone is insufficient. A zero exit with a "verification_failed"
    receipt is the case this exists for; a nonzero exit with a "complete"
    receipt would mean the harness contradicted itself, which is equally not
    a success.
    """

    if outcome.get("returncode") != 0:
        return False
    if not outcome.get("harness_ok"):
        return False
    return outcome.get("harness_status") == HARNESS_COMPLETE


def require_private_queue(path: Path, *, platform: Any = None,
                         root: Path | None = None) -> None:
    """Establish and read back an owner-only ACL, or refuse to use the path.

    ``mkdir(mode=0o700)`` and ``chmod`` are no-ops on Windows: the directory
    inherits whatever its parent grants and nothing says so. This queue holds
    brief paths, repository paths, model choices and job outcomes, so a
    directory whose privacy cannot be established is one nothing may be
    written into. It is called at creation and again for each job directory
    *before* the request is written, not afterwards by the worker: by the time
    a worker takes its lock the content is already on disk.
    """

    try:
        wpv.require_private_directory(path, root=root or path, platform=platform)
    except wpv.PrivacyError as exc:
        raise ExecutionAdmissionError(f"queue_{exc.reason}") from exc


class ExecutionQueue:
    """Filesystem queue whose request and terminal receipts survive restarts."""

    def __init__(self, root: str | Path,
                 executor: Callable[[dict[str, Any], Path], dict[str, Any]] | None,
                 *, clock: Callable[[], float] = time.time,
                 recover_interrupted: bool = True,
                 platform: Any = None):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            # Windows: st_mode is not the mechanism. The ACL check below is.
            pass
        self._platform = platform
        require_private_queue(self.root, platform=platform)
        self.executor, self.clock = executor, clock
        self._lock = threading.Lock()
        if recover_interrupted:
            self._recover_interrupted()

    def _recover_interrupted(self) -> None:
        """Never silently repeat a provider send after process interruption."""
        for directory in self.root.iterdir():
            receipt_path = directory / "receipt.json"
            if not receipt_path.is_file():
                continue
            receipt = _read_json(receipt_path)
            state = receipt.get("state")
            # A claim file is created before a request is marked running.  A
            # process death between those two durable writes is uncertain, so
            # it is reconciled rather than silently retried.
            if state == "running" or (state == "queued" and (directory / "claim.lock").exists()):
                receipt.update(state="blocked", error="interrupted_requires_reconciliation",
                               finished_at=self.clock())
                _atomic_json(receipt_path, receipt)

    @staticmethod
    def _clean_text(value: Any, label: str, maximum: int = 256) -> str:
        if (not isinstance(value, str) or not value or len(value) > maximum
                or any(c in value for c in "\0\r\n")):
            raise ExecutionAdmissionError(f"{label}_invalid")
        return value

    def submit(self, *, caller: str, provider: str, repo: str, brief: str,
               base: str, classification: str, model: str, effort: str,
               item_id: str, stage: str, owner_id: str, stage_revision: int,
               verify_argv: list[list[str]] | None = None,
               timeout_seconds: int = 900, paid_fallback: bool = False,
               idempotency_key: str | None = None) -> dict[str, Any]:
        if caller not in PROVIDER_FOR_CALLER or provider != PROVIDER_FOR_CALLER[caller]:
            raise ExecutionAdmissionError("provider_not_eligible_for_caller")
        if paid_fallback:
            raise ExecutionAdmissionError("paid_fallback_forbidden")
        if classification not in ALLOWED_CLASSIFICATIONS:
            raise ExecutionAdmissionError("classification_not_eligible")
        repo_path, brief_path = Path(repo), Path(brief)
        if not repo_path.is_absolute() or not brief_path.is_absolute():
            raise ExecutionAdmissionError("repo_and_brief_must_be_absolute")
        if not repo_path.is_dir() or not (repo_path / ".git").exists():
            raise ExecutionAdmissionError("repo_invalid")
        if not brief_path.is_file() or brief_path.is_symlink():
            raise ExecutionAdmissionError("brief_invalid")
        brief_bytes = brief_path.read_bytes()
        if not brief_bytes or len(brief_bytes) > 100_000:
            raise ExecutionAdmissionError("brief_size_invalid")
        try:
            brief_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ExecutionAdmissionError("brief_not_utf8") from exc
        base = self._clean_text(base, "base")
        model = self._clean_text(model, "model")
        effort = self._clean_text(effort, "effort")
        item_id = self._clean_text(item_id, "item_id")
        stage = self._clean_text(stage, "stage")
        owner_id = self._clean_text(owner_id, "owner_id")
        if isinstance(stage_revision, bool) or not isinstance(stage_revision, int) or stage_revision < 0:
            raise ExecutionAdmissionError("stage_revision_invalid")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 7200:
            raise ExecutionAdmissionError("timeout_invalid")
        checks = verify_argv or []
        if provider == "claude" and not checks:
            raise ExecutionAdmissionError("claude_verification_required")
        if (not isinstance(checks, list) or any(not isinstance(row, list) or not row
                or any(not isinstance(cell, str) or not cell for cell in row) for row in checks)):
            raise ExecutionAdmissionError("verify_argv_invalid")
        key = self._clean_text(idempotency_key, "idempotency_key") if idempotency_key else None
        identity = {"caller": caller, "provider": provider, "repo": str(repo_path),
                    "brief": str(brief_path), "brief_sha256": hashlib.sha256(brief_bytes).hexdigest(),
                    "base": base, "classification": classification, "model": model,
                    "effort": effort, "verify_argv": checks, "timeout_seconds": timeout_seconds,
                    "item_id": item_id, "stage": stage, "owner_id": owner_id,
                    "stage_revision": stage_revision}
        with self._lock:
            if key:
                for directory in self.root.iterdir():
                    request_path = directory / "request.json"
                    if request_path.is_file():
                        previous = _read_json(request_path)
                        if previous.get("caller") == caller and previous.get("idempotency_key") == key:
                            if any(previous.get(name) != value for name, value in identity.items()):
                                raise ExecutionAdmissionError("idempotency_conflict")
                            return self.status(directory.name)
            job_id = uuid.uuid4().hex
            directory = self.root / job_id
            directory.mkdir(mode=0o700)
            # Before the request, never after it. The request names the brief
            # and the repository; a job directory that turned out to be
            # readable by other accounts would have leaked them already.
            require_private_queue(directory, platform=self._platform,
                                  root=self.root)
            request = {"schema": 1, "job_id": job_id, **identity,
                       "paid_fallback": False, "idempotency_key": key,
                       "submitted_at": self.clock()}
            _atomic_json(directory / "request.json", request)
            _atomic_json(directory / "receipt.json", {
                "schema": 1, "job_id": job_id, "state": "queued", "caller": caller,
                "provider": provider, "classification": classification,
                "permission_to_apply": False, "permission_to_commit": False,
                "permission_to_push": False, "permission_to_merge": False,
                "submitted_at": request["submitted_at"]})
        return self.status(job_id)

    def _directory(self, job_id: str) -> Path:
        if (not isinstance(job_id, str) or len(job_id) != 32
                or any(c not in "0123456789abcdef" for c in job_id)):
            raise ExecutionAdmissionError("job_id_invalid")
        directory = self.root / job_id
        if not directory.is_dir():
            raise ExecutionAdmissionError("execution_job_not_found")
        return directory

    def status(self, job_id: str) -> dict[str, Any]:
        receipt = _read_json(self._directory(job_id) / "receipt.json")
        return {"job_id": job_id, "state": receipt["state"],
                "caller": receipt["caller"], "provider": receipt["provider"],
                "classification": receipt["classification"]}

    def result(self, job_id: str) -> dict[str, Any]:
        directory = self._directory(job_id)
        receipt = _read_json(directory / "receipt.json")
        if receipt["state"] not in TERMINAL:
            raise ExecutionAdmissionError("execution_job_not_terminal")
        return {**receipt, "receipt_path": str(directory / "receipt.json")}

    def run_once(self, worker_id: str) -> dict[str, Any] | None:
        self._clean_text(worker_id, "worker_id")
        if self.executor is None:
            raise ExecutionAdmissionError("execution_worker_not_configured")
        with self._lock:
            selected = None
            for directory in sorted(self.root.iterdir()):
                receipt_path = directory / "receipt.json"
                if receipt_path.is_file() and _read_json(receipt_path).get("state") == "queued":
                    selected = directory
                    break
            if selected is None:
                return None
            lock_path = selected / "claim.lock"
            try:
                descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                return None
            os.close(descriptor)
            request = _read_json(selected / "request.json")
            receipt = _read_json(selected / "receipt.json")
            receipt.update(state="running", worker_id=worker_id, started_at=self.clock())
            _atomic_json(selected / "receipt.json", receipt)
        try:
            current = Path(request["brief"]).read_bytes()
            if hashlib.sha256(current).hexdigest() != request["brief_sha256"]:
                raise ExecutionAdmissionError("brief_changed_after_admission")
            outcome = self.executor(request, selected)
            validate_outcome(outcome)
            receipt.update(
                state="complete" if outcome_is_success(outcome) else "failed",
                harness=outcome, finished_at=self.clock())
        except Exception as exc:
            receipt.update(state="failed", error=type(exc).__name__, finished_at=self.clock())
        _atomic_json(selected / "receipt.json", receipt)
        return self.status(selected.name)

    def state_report(self) -> dict[str, int]:
        states: dict[str, int] = {}
        for directory in self.root.iterdir():
            path = directory / "receipt.json"
            if path.is_file():
                state = _read_json(path).get("state", "unknown")
                states[state] = states.get(state, 0) + 1
        return states
