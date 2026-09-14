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
from typing import Any, Callable

from agent_bridge.platform import platform as host_platform


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
        self.harnesses = harnesses

    def __call__(self, request: dict[str, Any], job_dir: Path) -> dict[str, Any]:
        if os.name != "posix":
            raise ExecutionAdmissionError("execution_worker_platform_unsupported")
        # Imported only at execution time so the queue/status MCP remains
        # importable on Windows, where the persistent worker is not supported.
        import pwd
        provider = request["provider"]
        harness = getattr(self.harnesses, provider)
        argv = [str(self.harnesses.python), "-P", str(harness), request["brief"],
                "--repo", request["repo"], "--base", request["base"],
                "--timeout", str(request["timeout_seconds"])]
        if provider == "codex":
            argv += ["--classification", request["classification"],
                     "--model", request["model"], "--reasoning-effort", request["effort"],
                     "--tasks-dir", str(job_dir / "harness")]
        else:
            argv += ["--classification", request["classification"],
                     "--model", request["model"], "--effort", request["effort"],
                     "--task-root", str(job_dir / "harness")]
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
        return {"returncode": process.returncode,
                "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
                "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
                "stdout_bytes": len(stdout), "stderr_bytes": len(stderr)}


class ExecutionQueue:
    """Filesystem queue whose request and terminal receipts survive restarts."""

    def __init__(self, root: str | Path,
                 executor: Callable[[dict[str, Any], Path], dict[str, Any]] | None,
                 *, clock: Callable[[], float] = time.time,
                 recover_interrupted: bool = True):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
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
            receipt.update(state="complete" if outcome.get("returncode") == 0 else "failed",
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
