"""Private, pinned Git sources for POSIX execution jobs.

This module deliberately has no callers in the execution lanes yet.  It
copies the object closure of one resolved commit into a bare repository owned
by the job, so later worktrees need not read or write the operator checkout.
"""
from __future__ import annotations

import hashlib
import json
import os
import select
import shutil
import signal
import stat
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from .. import runner
from . import hostenv


MAX_STREAM_BYTES = 16 * 1024 * 1024
MAX_SNAPSHOT_FILES = 2048
MAX_SNAPSHOT_BYTES = 256 * 1024 * 1024
MAX_SNAPSHOT_FILE_BYTES = 64 * 1024 * 1024
GIT_TIMEOUT_SECONDS = 30
PACK_TIMEOUT_SECONDS = 120
JOB_SOURCE_DIRNAME = "job-source.git"
BASE_REF = "refs/bridge/base"


class JobSourceError(RuntimeError):
    """A closed, stable refusal from the job-source boundary."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class JobSourceLimits:
    """Bounds charged before a private source is accepted."""

    max_pack_bytes: int = MAX_SNAPSHOT_BYTES
    max_files: int = MAX_SNAPSHOT_FILES
    max_file_bytes: int = MAX_SNAPSHOT_FILE_BYTES


DEFAULT_LIMITS = JobSourceLimits()
_GIT_BIN: Path | None = None


def _git_bin() -> Path:
    global _GIT_BIN
    if _GIT_BIN is None:
        try:
            _GIT_BIN = hostenv.resolve_git()
        except hostenv.HostCapabilityError as exc:
            raise JobSourceError("job_source_git_unavailable") from exc
    return _GIT_BIN


def _git_argv(*args: str) -> list[str]:
    """The same non-interactive Git invocation shape used by both lanes."""
    return [
        str(_git_bin()), "--no-optional-locks", "-c", "core.hooksPath=/dev/null",
        "-c", "core.fsmonitor=false", "-c", "diff.external=",
        "-c", "core.attributesFile=/dev/null", *args,
    ]


def _env() -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(Path.home()),
        "LANG": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
    }
    for key in ("USER", "LOGNAME"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def _run_git(cwd: Path, *args: str, timeout: int = GIT_TIMEOUT_SECONDS) -> bytes:
    """Run a bounded, no-shell Git command whose output is textual."""
    result = runner.run(
        _git_argv(*args), cwd=str(cwd), env=_env(), stdin_data="", timeout=timeout,
        grace=2, stdout_cap=MAX_STREAM_BYTES, stderr_cap=MAX_STREAM_BYTES,
    )
    if result.spawn_failed or result.timed_out or result.cap_exceeded \
            or result.descendant_held_pipes or result.returncode:
        raise JobSourceError("job_source_git_failed")
    return result.stdout


def _run_git_quiet(cwd: Path, args: list[str], *, stdin: Any = None,
                   timeout: int = GIT_TIMEOUT_SECONDS) -> None:
    """Run Git with no captured output, including a byte-stream stdin.

    ``runner.run`` intentionally accepts text stdin.  Pack files are arbitrary
    bytes, so the two pack-transfer commands use this small equivalent with
    output sent to ``DEVNULL`` (a zero-byte bounded capture), an explicit
    ``shell=False``, a new process group, and a deadline.
    """
    try:
        proc = subprocess.Popen(
            _git_argv(*args), cwd=str(cwd), env=_env(), stdin=stdin,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False,
            start_new_session=True,
        )
    except OSError as exc:
        raise JobSourceError("job_source_git_failed") from exc
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _stop_process(proc)
        raise JobSourceError("job_source_git_timeout") from exc
    if code:
        raise JobSourceError("job_source_git_failed")


def _stop_process(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def _coerce_limits(limits: JobSourceLimits | Mapping[str, int] | None) -> JobSourceLimits:
    if limits is None:
        return DEFAULT_LIMITS
    if isinstance(limits, JobSourceLimits):
        active = limits
    elif isinstance(limits, Mapping):
        aliases = {
            "pack_bytes": "max_pack_bytes", "file_count": "max_files",
            "file_bytes": "max_file_bytes",
        }
        values = asdict(DEFAULT_LIMITS)
        for key, value in limits.items():
            key = aliases.get(key, key)
            if key not in values:
                raise JobSourceError("job_source_limits_invalid")
            values[key] = value
        active = JobSourceLimits(**values)
    else:
        raise JobSourceError("job_source_limits_invalid")
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0
           for value in asdict(active).values()):
        raise JobSourceError("job_source_limits_invalid")
    return active


def _require_posix() -> None:
    if os.name == "nt":
        raise JobSourceError("job_source_platform_unsupported")


def _owner_only_directory(path: Path, *, create: bool) -> Path:
    """Return a lexical job root only when it is a private real directory."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if not create:
            raise JobSourceError("job_source_root_missing") from None
        try:
            path.mkdir(mode=0o700, parents=False)
        except OSError as exc:
            raise JobSourceError("job_source_root_invalid") from exc
        try:
            info = os.lstat(path)
        except OSError as exc:
            raise JobSourceError("job_source_root_invalid") from exc
    except OSError as exc:
        raise JobSourceError("job_source_root_invalid") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise JobSourceError("job_source_root_invalid")
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise JobSourceError("job_source_root_not_owner_only")
    return path


def _source_dir(job_root: Path) -> Path:
    # This is intentionally a fixed direct child, never input supplied as a
    # path component.  The parent has already been lstat-checked, not resolved.
    return job_root / JOB_SOURCE_DIRNAME


def _require_source_directory(job_root: Path) -> Path:
    source = _source_dir(job_root)
    try:
        info = os.lstat(source)
    except FileNotFoundError:
        raise JobSourceError("job_source_missing") from None
    except OSError as exc:
        raise JobSourceError("job_source_missing") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise JobSourceError("job_source_invalid")
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise JobSourceError("job_source_invalid")
    return source


def _write_config(source: Path) -> None:
    hooks = source / "hooks"
    try:
        hooks.mkdir(mode=0o700)
        os.chmod(hooks, 0o700)
        config = source / "config"
        config.write_text(
            "[core]\n"
            "\trepositoryformatversion = 0\n"
            "\tbare = true\n"
            "\tlogallrefupdates = false\n"
            f"\thooksPath = {hooks}\n"
            "\tfsmonitor = false\n",
            encoding="utf-8",
        )
        os.chmod(config, 0o600)
    except OSError as exc:
        raise JobSourceError("job_source_config_failed") from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        raise JobSourceError("job_source_config_failed") from exc
    return digest.hexdigest()


def _resolve_commit(operator_repo: Path, base_spelling: str) -> str:
    if not isinstance(base_spelling, str) or not base_spelling or "\x00" in base_spelling:
        raise JobSourceError("base_commit_unresolved")
    try:
        output = _run_git(
            operator_repo, "rev-parse", "--verify", "--end-of-options",
            f"{base_spelling}^{{commit}}",
        )
    except JobSourceError as exc:
        if exc.reason == "job_source_git_failed":
            raise JobSourceError("base_commit_unresolved") from None
        raise
    try:
        value = output.strip().decode("ascii", "strict")
    except UnicodeError as exc:
        raise JobSourceError("base_commit_unresolved") from exc
    if not value or any(ch not in "0123456789abcdef" for ch in value.lower()):
        raise JobSourceError("base_commit_unresolved")
    return value


def _object_format(operator_repo: Path) -> str:
    try:
        value = _run_git(operator_repo, "rev-parse", "--show-object-format=storage").strip()
        name = value.decode("ascii", "strict")
    except (JobSourceError, UnicodeError) as exc:
        raise JobSourceError("job_source_object_format_invalid") from exc
    if name not in {"sha1", "sha256"}:
        raise JobSourceError("job_source_object_format_invalid")
    return name


def _object_ids(operator_repo: Path, commit: str) -> bytes:
    try:
        output = _run_git(
            operator_repo, "rev-list", "--objects", "--no-object-names", commit,
            timeout=PACK_TIMEOUT_SECONDS,
        )
    except JobSourceError as exc:
        if exc.reason == "job_source_git_failed":
            raise JobSourceError("source_object_missing") from None
        raise
    if not output.strip():
        raise JobSourceError("source_object_missing")
    return output


def _write_pack(operator_repo: Path, object_ids: bytes, destination: Path,
                max_bytes: int) -> None:
    """Stream pack-objects into a bounded temporary pack in the job source."""
    try:
        proc = subprocess.Popen(
            _git_argv("pack-objects", "--stdout", "--revs"), cwd=str(operator_repo),
            env=_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, shell=False, start_new_session=True,
        )
    except OSError as exc:
        raise JobSourceError("source_pack_failed") from exc
    assert proc.stdin is not None and proc.stdout is not None
    try:
        total = 0
        deadline = time.monotonic() + PACK_TIMEOUT_SECONDS
        with destination.open("xb") as handle:
            # pack-objects consumes revisions before it can produce much pack
            # output.  Feeding a large revision list in one blocking write can
            # therefore deadlock on its stdin pipe.  Multiplex both ends.
            os.set_blocking(proc.stdin.fileno(), False)
            os.set_blocking(proc.stdout.fileno(), False)
            input_offset = 0
            stdout_closed = False
            while not stdout_closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _stop_process(proc)
                    raise JobSourceError("source_pack_timeout")
                readers = [] if stdout_closed else [proc.stdout]
                writers = [proc.stdin] if input_offset < len(object_ids) else []
                ready, writable, _ = select.select(readers, writers, [], remaining)
                if not ready and not writable:
                    _stop_process(proc)
                    raise JobSourceError("source_pack_timeout")
                if proc.stdin in writable:
                    written = os.write(proc.stdin.fileno(), object_ids[input_offset:input_offset + 65536])
                    input_offset += written
                    if input_offset == len(object_ids):
                        proc.stdin.close()
                if proc.stdout in ready:
                    try:
                        chunk = os.read(proc.stdout.fileno(), 1024 * 1024)
                    except BlockingIOError:
                        chunk = None
                    if chunk == b"":
                        stdout_closed = True
                    elif chunk:
                        total += len(chunk)
                        if total > max_bytes:
                            _stop_process(proc)
                            raise JobSourceError("source_pack_exceeds_byte_limit")
                        handle.write(chunk)
            if proc.wait(timeout=max(0.0, deadline - time.monotonic())):
                raise JobSourceError("source_object_missing")
    except BrokenPipeError as exc:
        _stop_process(proc)
        raise JobSourceError("source_object_missing") from exc
    except subprocess.TimeoutExpired as exc:
        _stop_process(proc)
        raise JobSourceError("source_pack_timeout") from exc
    except OSError as exc:
        _stop_process(proc)
        raise JobSourceError("source_pack_failed") from exc
    finally:
        try:
            proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            proc.stdout.close()
        except OSError:
            pass


def _install_pack(source: Path, incoming: Path) -> None:
    try:
        with incoming.open("rb") as handle:
            _run_git_quiet(
                source, ["--git-dir", str(source), "index-pack", "--stdin", "--fix-thin",
                         "--keep=bridge"], stdin=handle, timeout=PACK_TIMEOUT_SECONDS,
            )
    except JobSourceError as exc:
        if exc.reason in {"job_source_git_failed", "job_source_git_timeout"}:
            raise JobSourceError("source_pack_incomplete") from None
        raise
    except OSError as exc:
        raise JobSourceError("source_pack_incomplete") from exc


def _tree_entries(source: Path, commit: str, limits: JobSourceLimits) -> list[dict[str, str]]:
    try:
        raw = _run_git(
            source, "--git-dir", str(source), "ls-tree", "-r", "-z", commit,
            timeout=PACK_TIMEOUT_SECONDS,
        )
    except JobSourceError as exc:
        raise JobSourceError("source_pack_incomplete") from exc
    entries: list[dict[str, str]] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            prefix, path = record.split(b"\t", 1)
            mode, kind, object_id = prefix.split(b" ", 2)
            item = {
                "path": path.decode("utf-8", "surrogateescape"),
                "mode": mode.decode("ascii"), "type": kind.decode("ascii"),
                "object": object_id.decode("ascii"),
            }
        except (UnicodeError, ValueError) as exc:
            raise JobSourceError("manifest_invalid") from exc
        entries.append(item)
        if len(entries) > limits.max_files:
            raise JobSourceError("source_file_count_exceeded")
        if item["type"] == "blob":
            try:
                size = int(_run_git(
                    source, "--git-dir", str(source), "cat-file", "-s", item["object"],
                ).strip())
            except (ValueError, JobSourceError) as exc:
                raise JobSourceError("source_pack_incomplete") from exc
            if size > limits.max_file_bytes:
                raise JobSourceError("source_file_size_exceeded")
    return entries


def _manifest_payload(*, base_spelling: str, commit: str, tree: str,
                      object_format: str, entries: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "version": 1,
        "base_spelling": base_spelling,
        "commit": commit,
        "tree": tree,
        "object_format": object_format,
        "entries": entries,
    }


def canonical_manifest_bytes(manifest: Mapping[str, Any]) -> bytes:
    """Canonical bytes covered by ``manifest_sha256``.

    The config hash is deliberately a separate integrity value: it is not
    part of the Git object graph and must be compared independently.
    """
    payload = {key: manifest[key] for key in (
        "version", "base_spelling", "commit", "tree", "object_format", "entries"
    )}
    return json.dumps(payload, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":")).encode("ascii")


def _make_manifest(source: Path, *, base_spelling: str, commit: str,
                   object_format: str, limits: JobSourceLimits) -> dict[str, Any]:
    try:
        tree = _run_git(
            source, "--git-dir", str(source), "rev-parse", f"{commit}^{{tree}}",
        ).strip().decode("ascii", "strict")
    except (JobSourceError, UnicodeError) as exc:
        raise JobSourceError("source_pack_incomplete") from exc
    payload = _manifest_payload(
        base_spelling=base_spelling, commit=commit, tree=tree,
        object_format=object_format, entries=_tree_entries(source, commit, limits),
    )
    payload["manifest_sha256"] = hashlib.sha256(canonical_manifest_bytes(payload)).hexdigest()
    payload["config_sha256"] = _sha256_file(source / "config")
    return payload


def _connectivity_check(source: Path, commit: str) -> None:
    try:
        _run_git(
            source, "--git-dir", str(source), "fsck", "--connectivity-only",
            "--no-dangling", commit, timeout=PACK_TIMEOUT_SECONDS,
        )
    except JobSourceError as exc:
        raise JobSourceError("source_pack_incomplete") from exc


def prepare_job_source(operator_repo: Path | str, base_spelling: str,
                       job_root: Path | str, *,
                       limits: JobSourceLimits | Mapping[str, int] | None = None) -> dict[str, Any]:
    """Pin ``base_spelling`` into an owner-only bare repository below a job.

    No Git command in this function has an operator-repository write path.
    Preparation either returns a self-contained manifest or removes its own
    partial source and raises one fixed ``JobSourceError.reason``.
    """
    _require_posix()
    active_limits = _coerce_limits(limits)
    root = _owner_only_directory(Path(job_root), create=True)
    source = _source_dir(root)
    try:
        os.lstat(source)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise JobSourceError("job_source_invalid") from exc
    else:
        raise JobSourceError("job_source_exists")

    operator = Path(operator_repo)
    commit = _resolve_commit(operator, base_spelling)
    object_format = _object_format(operator)
    object_ids = _object_ids(operator, commit)
    created = False
    try:
        try:
            source.mkdir(mode=0o700)
            os.chmod(source, 0o700)
        except OSError as exc:
            raise JobSourceError("job_source_create_failed") from exc
        created = True
        _run_git(root, "init", "--bare", "--template=/dev/null",
                 f"--object-format={object_format}", str(source))
        _require_source_directory(root)
        _write_config(source)
        incoming = source / "objects" / "pack" / f".bridge-incoming-{uuid.uuid4().hex}.pack"
        _write_pack(operator, object_ids, incoming, active_limits.max_pack_bytes)
        try:
            _install_pack(source, incoming)
        finally:
            try:
                incoming.unlink()
            except FileNotFoundError:
                pass
        _connectivity_check(source, commit)
        _run_git(source, "--git-dir", str(source), "update-ref", BASE_REF, commit)
        manifest = _make_manifest(
            source, base_spelling=base_spelling, commit=commit,
            object_format=object_format, limits=active_limits,
        )
        return manifest
    except Exception:
        if created:
            _remove_source_directory(root, missing_ok=True)
        raise


def _manifest_is_well_formed(manifest: Mapping[str, Any]) -> bool:
    required = {
        "version", "base_spelling", "commit", "tree", "object_format", "entries",
        "manifest_sha256", "config_sha256",
    }
    return isinstance(manifest, Mapping) and required.issubset(manifest) and \
        isinstance(manifest.get("entries"), list)


def verify_job_source(job_root: Path | str, manifest: Mapping[str, Any]) -> list[str]:
    """Return stable integrity failures for a prepared source, or ``[]``."""
    _require_posix()
    if not _manifest_is_well_formed(manifest):
        return ["manifest_mismatch"]
    reasons: list[str] = []
    try:
        root = _owner_only_directory(Path(job_root), create=False)
        source = _require_source_directory(root)
    except JobSourceError:
        return ["job_source_missing"]

    try:
        ref = _run_git(source, "--git-dir", str(source), "rev-parse", "--verify",
                       "--end-of-options", BASE_REF).strip().decode("ascii", "strict")
    except (JobSourceError, UnicodeError):
        ref = ""
    if ref != manifest["commit"]:
        reasons.append("base_ref_changed")

    tree = ""
    if ref:
        try:
            tree = _run_git(source, "--git-dir", str(source), "rev-parse",
                            f"{ref}^{{tree}}").strip().decode("ascii", "strict")
        except (JobSourceError, UnicodeError):
            pass
    if tree != manifest["tree"]:
        reasons.append("tree_changed")

    try:
        if _sha256_file(source / "config") != manifest["config_sha256"]:
            reasons.append("config_changed")
    except JobSourceError:
        reasons.append("config_changed")

    try:
        expected_hash = hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest()
        if expected_hash != manifest["manifest_sha256"]:
            reasons.append("manifest_mismatch")
        elif ref and tree:
            current = _make_manifest(
                source, base_spelling=manifest["base_spelling"], commit=ref,
                object_format=manifest["object_format"], limits=DEFAULT_LIMITS,
            )
            if canonical_manifest_bytes(current) != canonical_manifest_bytes(manifest):
                reasons.append("manifest_mismatch")
        else:
            reasons.append("manifest_mismatch")
    except (JobSourceError, KeyError, TypeError, ValueError):
        if "manifest_mismatch" not in reasons:
            reasons.append("manifest_mismatch")
    return reasons


def _remove_source_directory(root: Path, *, missing_ok: bool) -> None:
    source = _source_dir(root)
    try:
        info = os.lstat(source)
    except FileNotFoundError:
        if missing_ok:
            return
        raise JobSourceError("job_source_missing") from None
    except OSError as exc:
        raise JobSourceError("job_source_remove_refused") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise JobSourceError("job_source_remove_refused")
    # ``rmtree`` unlinks symlinks it encounters instead of traversing them;
    # its top-level symlink behaviour is nevertheless guarded above.
    try:
        shutil.rmtree(source)
    except OSError as exc:
        raise JobSourceError("job_source_remove_failed") from exc


def remove_job_source(job_root: Path | str) -> None:
    """Remove this job's fixed private-source child, never a caller path."""
    _require_posix()
    root = _owner_only_directory(Path(job_root), create=False)
    _remove_source_directory(root, missing_ok=False)
