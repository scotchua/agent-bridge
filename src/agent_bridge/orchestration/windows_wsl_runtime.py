"""Bounded, fail-closed ephemeral WSL2 lifecycle for Windows delegation.

This module is the runtime that drives one delegated job through one
throwaway WSL2 instance created from a **caller-supplied, already-pinned**
rootfs. It does not download anything, does not select or discover an image,
does not touch provider credentials, does not configure networking, and does
not onboard anything. It runs only on native Windows; on any other platform
it returns an aborted result without spawning a process or touching the
filesystem.

Everything security-relevant is reused rather than reimplemented:

* :mod:`agent_bridge.orchestration.windows_wsl` supplies the pinned manifest
  type, safe distro naming, the guest security boundary (``wsl.conf``, the
  environment allowlist), strict job-spec validation, the pure ``wsl.exe``
  argv builders, the strict absolute-Windows-path validator, and the pure
  stale-age selection.
* :mod:`agent_bridge.orchestration.windows_preflight` supplies the native
  Windows platform gate.
* :mod:`agent_bridge.runner` supplies the bounded, argv-only, shell-free
  process runner, which on Windows spawns into a Job Object via
  :mod:`agent_bridge.platform.windows` and terminates the whole tree.
* :mod:`agent_bridge.platform` supplies the owner-only ACL verification
  (read back and proven, never assumed) and process liveness.
* :mod:`agent_bridge.store` supplies the per-user exclusive file lock,
  owner-only directory creation, and durable atomic JSON writes.

The invariants this module itself owns:

* One job at a time per user, serialised by the existing per-user lock.
* **Every** directory this runtime relies on is private and proven private:
  the runtime root (which also holds the lock file), the jobs root, the
  owner-record directory, the receipt directory, and the per-job directory.
  The owner record is a deletion authority, so the directory holding it is
  protected and verified exactly like the job directory itself.
* The per-job directory path is a strict absolute local Windows path, its
  ancestors contain no reparse points, and it must not already exist.
* The rootfs is copied into that private directory through a single source
  handle, hashed while streaming, written to an exclusively created
  destination, bounded by a hard size cap, and checked for source identity
  drift before and after. An incomplete copy is removed.
* Reparse and identity checks are repeated immediately before the import, on
  the job directory, the install directory, and the private rootfs copy, so
  a namespace swap between verification and use is narrowed to the interval
  described under "Known open windows" below. Only the verified private copy
  is ever imported, always as WSL2.
* A durable owner record is written **before** the import, so a crashed
  process still leaves provable ownership for a later reaper.
* Fixed canaries must pass before any caller payload runs.
* Caller input and job output move only through pipes to the fixed guest
  runner. Nothing is staged on disk.
* Terminate, unregister, and filesystem cleanup are attempted independently
  in a true ``finally`` path after every exception. Each is judged on the
  full runner verdict, not just an exit code. Incomplete containment is the
  terminal reason whatever else went wrong, and is never reported as
  success nor obscured by an earlier failure.
* The owner record is retired only once containment is proven, so a failed
  teardown leaves the evidence a later reaper needs.
* A reaper acts only on a positive proof of the owner's death and a
  re-verified on-disk binding. Liveness is tri-state: anything short of
  proven-dead, including an access-denied probe, a failed wait, or an
  unreadable process identity, blocks every destructive step.
* Persisted output carries fixed, path-free reason codes only. Operational
  exception text is never written to a receipt.
* The timing/status receipt is written outside the deleted job directory and
  never contains input, output, commands, secrets, or the source path.

Known open windows
------------------

These are stated rather than papered over. Both are same-user threats: a
hostile process already running as this user. Nothing here defends against
an attacker who is already the user in a different privilege sense.

* **Private-copy pathname race.** Between the final ``lstat`` of the private
  rootfs copy and ``wsl.exe`` reopening that pathname, a same-user process
  could replace the file. The revalidation narrows the window; it does not
  close it. The only mechanism that would close it on Windows is holding a
  ``CreateFileW`` handle with a share mode that denies write and delete
  across the import, which requires ``wsl.exe`` to request a share mode
  compatible with that handle. That is undocumented, and an incompatible
  share mode would make every import fail with a sharing violation. It is
  therefore not implemented here: a mechanism that cannot be tested without
  a live Windows host and that can break the whole path is a fragile
  workaround, not a fix. See :data:`PRIVATE_COPY_RACE_LIMITATION`.
* **WSL registration identity.** ``wsl.exe`` exposes no stable per-registration
  identifier or install path, so a distro name alone cannot prove that the
  registration about to be unregistered is the one this runtime created.
  Nothing is ever unregistered on the strength of a name. A live run proves
  creation positively: the name is listed as absent immediately before the
  import, and only a successful import, or the name being listed as present
  afterwards, authorises ``--terminate``/``--unregister``. A failed or
  ambiguous import whose outcome cannot be read back leaves the registration
  untouched and retains the job directory, install directory, and owner
  record so a later reaper can retry. The reaper in turn refuses to
  unregister any instance whose on-disk binding it cannot re-verify. See
  :data:`WSL_REGISTRATION_IDENTITY_LIMITATION`.
* **Registration proof race.** That proof is two separate ``wsl.exe --list``
  calls around the import rather than an atomic create-if-absent, which WSL
  does not offer. A same-user process registering the same name inside that
  window could be mistaken for this run's instance. The window is narrowed
  by taking the per-user lock around the whole lifecycle, and the name
  carries a caller-chosen token, but it is not closed. See
  :data:`REGISTRATION_PROOF_LIMITATION`.

Nothing here has been validated on a live Windows host. Reading this module,
or its tests passing, is not evidence that WSL delegation works.
"""

from __future__ import annotations

import contextlib
import hashlib
import ntpath
import os
import re
import stat as stat_module
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Mapping, Sequence

from .. import runner, store
from ..platform import platform as host_platform
from . import windows_wsl as ww
from .windows_preflight import is_windows

# ---------------------------------------------------------------------------
# Status vocabulary
# ---------------------------------------------------------------------------

STATUS_COMPLETED = "completed"
STATUS_ABORTED = "aborted"

REASON_OK = "ok"
REASON_UNSUPPORTED_PLATFORM = "unsupported_platform"
REASON_CLEANUP_FAILED = "cleanup_failed"
REASON_RECEIPT_WRITE_FAILED = "receipt_write_failed"

SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Hard ceilings. A caller may ask for less, never for more.
# ---------------------------------------------------------------------------

MAX_ROOTFS_BYTES = 8 * 1024 * 1024 * 1024
MAX_INPUT_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_TIMEOUT_SECONDS = 3600.0
MAX_LOCK_TIMEOUT_SECONDS = 600.0
COPY_CHUNK_BYTES = 1024 * 1024

#: Windows reparse-point attribute bit, as reported by ``st_file_attributes``.
FILE_ATTRIBUTE_REPARSE_POINT = 0x400

#: Name of the throwaway file used to prove a directory's ACL by read-back.
ACL_PROBE_NAME = "acl-probe"

# ---------------------------------------------------------------------------
# Fixed canaries. These are the security proof, so they are constants, never
# caller-supplied, and every one must match exactly.
# ---------------------------------------------------------------------------

CANARY_WORKDIR = "/workspace/canary"

CANARY_HOST_MOUNT = "host-mount-absent"
CANARY_INTEROP = "wsl-interop-absent"
CANARY_WSL_CONF = "wsl-conf-sha256"
CANARY_GUEST_RUNNER = "guest-runner-sha256"
CANARY_VERSIONS = "pinned-versions"

CANARY_ORDER = (
    CANARY_HOST_MOUNT,
    CANARY_INTEROP,
    CANARY_WSL_CONF,
    CANARY_GUEST_RUNNER,
    CANARY_VERSIONS,
)

WSL_CONF_SHA256 = hashlib.sha256(ww.WSL_CONF_CONTENTS.encode("utf-8")).hexdigest()


PRIVATE_COPY_RACE_LIMITATION = (
    "same-user pathname race between the final private-copy verification and "
    "wsl.exe reopening that path is narrowed, not closed; no handle-bound "
    "staging is used because wsl.exe's share mode is undocumented and an "
    "incompatible one would break every import"
)

WSL_REGISTRATION_IDENTITY_LIMITATION = (
    "wsl.exe exposes no stable per-registration identity or install path, so "
    "a distro name alone cannot prove a registration is ours; a live run "
    "proves creation by listing the name as absent immediately before the "
    "import and present immediately after, and reaping refuses to terminate "
    "or unregister any instance whose on-disk binding cannot be re-verified"
)

#: The pre/post listing proof is two separate wsl.exe calls, not an atomic
#: create-if-absent, so it narrows rather than closes the window in which a
#: same-user process could register the same name between the two.
REGISTRATION_PROOF_LIMITATION = (
    "the absent-before/present-after registration proof is two separate "
    "wsl.exe --list calls around the import, not an atomic create-if-absent; "
    "a same-user process that registers the same name inside that window "
    "could be mistaken for this run's instance"
)

# ---------------------------------------------------------------------------
# Path-free detail codes
# ---------------------------------------------------------------------------

DETAIL_SUPPRESSED = "detail_suppressed"

#: Anything that could carry a filesystem path or a quoted caller value. A
#: bare ``letter:`` only counts when it starts a word, so "failure: x" is not
#: mistaken for a drive.
_PATHY_RE = re.compile(r"""[\\/'"]|(?:^|[^A-Za-z0-9_])[A-Za-z]:""")
_TOKEN_CLEAN_RE = re.compile(r"[^a-z0-9]+")


def _token(value: Any) -> str:
    """Reduce a label to a lowercase, path-free token."""

    reduced = _TOKEN_CLEAN_RE.sub("_", str(value).lower()).strip("_")
    return reduced or "unknown"


def sanitize_detail(detail: Any) -> str:
    """Return a fixed, path-free code, or suppress the detail entirely.

    Receipts are persistent. An operational ``OSError`` message names the
    file it failed on, so letting exception text reach ``detail`` writes the
    inspected path, and therefore a confidential source location, into a
    durable record. Every detail is tokenised, and anything that still looks
    like it carries a path or a quoted caller value is dropped rather than
    tokenised, so a future edit that reintroduces ``{exc}`` leaks a code
    instead of a pathname.
    """

    if detail is None or detail == "":
        return ""
    text = str(detail)
    if _PATHY_RE.search(text):
        return DETAIL_SUPPRESSED
    token = _token(text)
    return token[:120]


# ---------------------------------------------------------------------------
# Tri-state liveness
# ---------------------------------------------------------------------------

LIVENESS_DEAD = "dead"
LIVENESS_ALIVE = "alive"
LIVENESS_INDETERMINATE = "indeterminate"

#: Only this OpenProcess error proves a pid is genuinely gone.
WINDOWS_ERROR_INVALID_PARAMETER = 87
#: ERROR_ACCESS_DENIED means the process EXISTS and we may not ask about it.
WINDOWS_ERROR_ACCESS_DENIED = 5
#: WaitForSingleObject: the handle was signalled, so the process has exited.
WINDOWS_WAIT_OBJECT_0 = 0


def classify_liveness(evidence: Any) -> str:
    """Tri-state verdict from a platform ``process_liveness`` report.

    ``process_alive`` is a bool and a bool cannot express "the probe failed".
    On Windows it returns False whenever ``OpenProcess`` fails, which folds
    ERROR_ACCESS_DENIED, a process that provably exists and that we are not
    allowed to query, into the same answer as a pid that is genuinely gone.
    A reaper acting on that bool would terminate and unregister a live
    instance belonging to a session it cannot see.

    So only a positive proof of death is ``dead``. Access denied, a failed or
    unexpected wait, an unexplained error code, and a malformed report are
    all ``indeterminate``, and no destructive step may run on them.
    """

    if not isinstance(evidence, Mapping):
        return LIVENESS_INDETERMINATE
    if evidence.get("liveness_indeterminate") or evidence.get("unexpected_wait_result"):
        return LIVENESS_INDETERMINATE
    if "wait_error" in evidence:
        return LIVENESS_INDETERMINATE

    error = evidence.get("open_process_error")
    if error is not None:
        if error == WINDOWS_ERROR_INVALID_PARAMETER:
            return LIVENESS_DEAD
        # ERROR_ACCESS_DENIED and everything else: the process may well exist.
        return LIVENESS_INDETERMINATE

    alive = evidence.get("alive")
    if alive is True:
        return LIVENESS_ALIVE
    if alive is not False:
        return LIVENESS_INDETERMINATE

    wait_result = evidence.get("wait_result")
    if wait_result is not None:
        return LIVENESS_DEAD if wait_result == WINDOWS_WAIT_OBJECT_0 else LIVENESS_INDETERMINATE
    reason = evidence.get("reason")
    if reason is not None and reason != "no such process":
        return LIVENESS_INDETERMINATE
    return LIVENESS_DEAD


class WindowsWslRuntimeError(RuntimeError):
    """An operational failure inside the ephemeral WSL2 lifecycle.

    Every one of these is caught at the top of :func:`run_job` and turned
    into a safe aborted result. It never escapes to the caller.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        # Sanitised here, at the single choke point, so no raise site
        # anywhere can put a pathname into a durable receipt.
        safe = sanitize_detail(detail)
        super().__init__(reason if not safe else f"{reason}: {safe}")
        self.reason = reason
        self.detail = safe


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeLimits:
    """Caller-chosen bounds, all validated against the hard ceilings above."""

    rootfs_max_bytes: int
    input_max_bytes: int
    output_max_bytes: int
    job_timeout_seconds: float
    canary_timeout_seconds: float
    import_timeout_seconds: float
    cleanup_timeout_seconds: float
    grace_seconds: float
    lock_timeout_seconds: float


def _positive_int(name: str, value: Any, ceiling: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WindowsWslRuntimeError("limits_invalid", f"{name} must be an int")
    if value <= 0:
        raise WindowsWslRuntimeError("limits_invalid", f"{name} must be positive")
    if value > ceiling:
        raise WindowsWslRuntimeError("limits_invalid", f"{name} exceeds ceiling {ceiling}")
    return value


def _positive_seconds(name: str, value: Any, ceiling: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WindowsWslRuntimeError("limits_invalid", f"{name} must be a number")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise WindowsWslRuntimeError("limits_invalid", f"{name} must be finite")
    if number <= 0:
        raise WindowsWslRuntimeError("limits_invalid", f"{name} must be positive")
    if number > ceiling:
        raise WindowsWslRuntimeError("limits_invalid", f"{name} exceeds ceiling {ceiling}")
    return number


def validate_limits(limits: Any) -> RuntimeLimits:
    """Validate every numeric bound. Fails closed on anything unusable."""

    if not isinstance(limits, RuntimeLimits):
        raise WindowsWslRuntimeError("limits_invalid", "limits must be RuntimeLimits")
    return RuntimeLimits(
        rootfs_max_bytes=_positive_int(
            "rootfs_max_bytes", limits.rootfs_max_bytes, MAX_ROOTFS_BYTES),
        input_max_bytes=_positive_int(
            "input_max_bytes", limits.input_max_bytes, MAX_INPUT_BYTES),
        output_max_bytes=_positive_int(
            "output_max_bytes", limits.output_max_bytes, MAX_OUTPUT_BYTES),
        job_timeout_seconds=_positive_seconds(
            "job_timeout_seconds", limits.job_timeout_seconds, MAX_TIMEOUT_SECONDS),
        canary_timeout_seconds=_positive_seconds(
            "canary_timeout_seconds", limits.canary_timeout_seconds, MAX_TIMEOUT_SECONDS),
        import_timeout_seconds=_positive_seconds(
            "import_timeout_seconds", limits.import_timeout_seconds, MAX_TIMEOUT_SECONDS),
        cleanup_timeout_seconds=_positive_seconds(
            "cleanup_timeout_seconds", limits.cleanup_timeout_seconds, MAX_TIMEOUT_SECONDS),
        grace_seconds=_positive_seconds(
            "grace_seconds", limits.grace_seconds, MAX_TIMEOUT_SECONDS),
        lock_timeout_seconds=_positive_seconds(
            "lock_timeout_seconds", limits.lock_timeout_seconds, MAX_LOCK_TIMEOUT_SECONDS),
    )


DEFAULT_LIMITS = RuntimeLimits(
    rootfs_max_bytes=4 * 1024 * 1024 * 1024,
    input_max_bytes=1024 * 1024,
    output_max_bytes=4 * 1024 * 1024,
    job_timeout_seconds=900.0,
    canary_timeout_seconds=60.0,
    import_timeout_seconds=600.0,
    cleanup_timeout_seconds=120.0,
    grace_seconds=5.0,
    lock_timeout_seconds=60.0,
)


# ---------------------------------------------------------------------------
# Request and result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JobRequest:
    """Everything one ephemeral WSL2 job needs. All of it caller-supplied."""

    runtime_root: str
    rootfs_source_path: str
    manifest: ww.PinnedBaseImageManifest
    guest_runner_sha256: str
    distro_token: str
    job_spec: Mapping[str, Any]
    stdin_data: str = ""
    limits: RuntimeLimits = DEFAULT_LIMITS


@dataclass(frozen=True)
class CleanupReport:
    """What each independent cleanup step actually did.

    ``complete`` is the only thing callers should branch on, and it is false
    unless every step either succeeded or was provably never needed.
    """

    terminate: str = "not_attempted"
    unregister: str = "not_attempted"
    filesystem: str = "not_attempted"
    owner_record: str = "not_attempted"

    @property
    def complete(self) -> bool:
        return all(
            value in ("ok", "not_attempted")
            for value in (self.terminate, self.unregister,
                          self.filesystem, self.owner_record)
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "terminate": self.terminate,
            "unregister": self.unregister,
            "filesystem": self.filesystem,
            "owner_record": self.owner_record,
            "complete": self.complete,
        }


@dataclass(frozen=True)
class RuntimeResult:
    """The outcome of one ephemeral WSL2 job.

    ``stdout``/``stderr`` are returned to the caller in memory only. They are
    never written to the receipt, never written into the job directory, and
    never logged by this module.
    """

    status: str
    reason: str
    distro_name: str | None = None
    exit_code: int | None = None
    stdout: bytes = b""
    stderr: bytes = b""
    canaries_passed: bool = False
    spawned: bool = False
    timings: Mapping[str, float] = field(default_factory=dict)
    cleanup: CleanupReport = field(default_factory=CleanupReport)
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == STATUS_COMPLETED


# ---------------------------------------------------------------------------
# Host operations seam
# ---------------------------------------------------------------------------


class HostOps:
    """The only place this module touches the OS.

    Every method delegates to already-hardened code. It exists as a class so
    tests can substitute faults (cleanup raising, identity drift, a racing
    reaper) without a live Windows host, and so that no filesystem or process
    primitive is reimplemented here.
    """

    # -- clocks -------------------------------------------------------------

    def monotonic(self) -> float:
        import time

        return time.monotonic()

    def now_utc(self) -> datetime:
        return datetime.now(timezone.utc)

    # -- locking ------------------------------------------------------------

    @contextlib.contextmanager
    def lock(self, lock_path: str, timeout: float) -> Iterator[None]:
        with store.file_lock(lock_path, timeout):
            yield

    # -- filesystem inspection ---------------------------------------------

    def lstat(self, path: str) -> os.stat_result:
        return os.lstat(path)

    def listdir(self, path: str) -> list[str]:
        return os.listdir(path)

    # -- filesystem mutation ------------------------------------------------

    def secure_mkdir(self, path: str) -> None:
        store.secure_mkdir(path)

    def verify_owner_only(self, directory: str, probe_file: str) -> tuple[bool, dict[str, Any]]:
        return host_platform.verify_owner_only_path(directory, probe_file)

    def open_read(self, path: str) -> int:
        return os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))

    def open_exclusive_write(self, path: str) -> int:
        return os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            store.FILE_MODE,
        )

    def fstat(self, fd: int) -> os.stat_result:
        return os.fstat(fd)

    def read(self, fd: int, size: int) -> bytes:
        return os.read(fd, size)

    def write(self, fd: int, data: bytes) -> int:
        return os.write(fd, data)

    def fsync(self, fd: int) -> None:
        os.fsync(fd)

    def close(self, fd: int) -> None:
        os.close(fd)

    def unlink(self, path: str) -> None:
        os.unlink(path)

    def remove_tree(self, path: str) -> None:
        import shutil

        shutil.rmtree(path)

    def exists(self, path: str) -> bool:
        return os.path.lexists(path)

    # -- durable records ----------------------------------------------------

    def write_json(self, path: str, payload: Mapping[str, Any]) -> None:
        store.atomic_write_json(path, dict(payload))

    def read_json(self, path: str) -> Any:
        return store.read_json_atomic(path)

    # -- processes ----------------------------------------------------------

    def run_host(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        timeout: float,
        grace: float,
        stdout_cap: int,
        stderr_cap: int,
        stdin_data: str = "",
    ) -> runner.RunResult:
        return runner.run(
            list(argv),
            cwd=cwd,
            env=runner.scrubbed_env(),
            stdin_data=stdin_data,
            timeout=timeout,
            grace=grace,
            stdout_cap=stdout_cap,
            stderr_cap=stderr_cap,
        )

    def host_cwd(self) -> str:
        """A fixed, absolute Windows directory for host-side ``wsl.exe`` runs.

        Never the job directory: an open working directory keeps a handle on
        it and blocks the very cleanup this runtime has to guarantee.
        """
        return os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT") or "C:\\Windows"

    def process_liveness(self, pid: int) -> Mapping[str, Any]:
        """Evidence, not a boolean.

        ``process_alive`` collapses "the OS says it exited" and "we were not
        allowed to ask" into the same False, which is precisely the
        distinction a reaper must not get wrong.
        """
        return host_platform.process_liveness(pid)

    def process_identity(self, pid: int) -> str:
        return host_platform.process_identity(pid)

    def current_pid(self) -> int:
        return os.getpid()


# ---------------------------------------------------------------------------
# Runner verdicts
# ---------------------------------------------------------------------------


REGISTRATION_NONE = "none"
REGISTRATION_CREATED = "created"
REGISTRATION_UNPROVEN = "unproven"

WSL_LIST_ARGV = ["wsl.exe", "--list", "--quiet"]

LISTING_ABSENT = "absent"
LISTING_PRESENT = "present"
LISTING_UNUSABLE = "unusable"


def _decode_wsl_listing(raw: Any) -> list[str] | None:
    """Decode ``wsl.exe --list --quiet`` output, or None if it cannot be.

    wsl.exe writes UTF-16LE. A listing that will not decode strictly is not
    a listing showing no match: it is no answer at all, and the caller must
    treat it as such rather than reading absence into it.
    """

    if not isinstance(raw, bytes):
        return None
    try:
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = raw.decode("utf-16", errors="strict")
        elif b"\x00" in raw:
            text = raw.decode("utf-16-le", errors="strict")
        else:
            text = raw.decode("utf-8-sig", errors="strict")
    except (UnicodeDecodeError, ValueError):
        return None
    return [line.strip().strip("\x00") for line in text.splitlines() if line.strip()]


def registration_listing(ops: "HostOps", distro_name: str, limits: "RuntimeLimits") -> str:
    """Is this exact distro name registered right now?

    Returns ``absent``, ``present``, or ``unusable``. ``unusable`` covers
    every way the question can fail to be answered (spawn failure, timeout,
    nonzero exit, capped or undecodable output, an exception) and is never
    collapsed into ``absent``: the whole point of asking is to avoid acting
    on a name we cannot account for.
    """

    try:
        result = ops.run_host(
            list(WSL_LIST_ARGV),
            cwd=ops.host_cwd(),
            timeout=limits.cleanup_timeout_seconds,
            grace=limits.grace_seconds,
            stdout_cap=limits.output_max_bytes,
            stderr_cap=limits.output_max_bytes,
        )
    except BaseException:  # noqa: BLE001 - an unanswerable probe is not an answer
        return LISTING_UNUSABLE
    if command_failure(result):
        return LISTING_UNUSABLE
    names = _decode_wsl_listing(getattr(result, "stdout", None))
    if names is None:
        return LISTING_UNUSABLE
    return LISTING_PRESENT if distro_name in names else LISTING_ABSENT


def registration_after_failed_import(
    ops: "HostOps", distro_name: str, limits: "RuntimeLimits"
) -> str:
    """What to believe about a name after an import that did not clearly work.

    The name was proven free immediately before the import, so "present now"
    means this run put it there and it may be destroyed. "Absent" means
    nothing was created. Anything else is unproven, and an unproven name is
    never unregistered: it could already belong to somebody else.
    """

    state = registration_listing(ops, distro_name, limits)
    if state == LISTING_PRESENT:
        return REGISTRATION_CREATED
    if state == LISTING_ABSENT:
        return REGISTRATION_NONE
    return REGISTRATION_UNPROVEN


def command_failure(result: Any) -> str:
    """Why a host command is not a clean success, or "" when it is.

    Judging a ``wsl.exe`` run by its exit code alone is how a timed-out or
    output-flooded teardown gets recorded as a successful one. Every signal
    the bounded runner reports is checked, and an unknown exit code counts
    as a failure rather than as a pass.
    """

    if getattr(result, "spawn_failed", False):
        return "spawn_failed"
    if getattr(result, "timed_out", False):
        return "timed_out"
    if getattr(result, "cap_exceeded", False):
        return "cap_exceeded"
    if getattr(result, "descendant_held_pipes", False):
        return "descendant_held_pipes"
    code = getattr(result, "returncode", None)
    if code is None:
        return "unknown_exit"
    if code != 0:
        return f"exit_{code}"
    return ""


# ---------------------------------------------------------------------------
# Path policy
# ---------------------------------------------------------------------------


def _validate_local_windows_path(name: str, value: Any) -> str:
    """Strict absolute local Windows path, reusing the existing validator."""

    if not isinstance(value, str):
        raise WindowsWslRuntimeError("path_invalid", f"{name} must be a string")
    try:
        return ww._validate_windows_host_path(name, value)
    except ww.WindowsWslContractError as exc:
        # Deliberately not str(exc): the contract error quotes the offending
        # path, which is exactly what must not reach a persistent receipt.
        raise WindowsWslRuntimeError(
            "path_invalid", f"{_token(name)}_contract_rejected") from exc


def _ancestors(path: str) -> list[str]:
    """Every path component from the drive root down to, and including, path."""

    drive, tail = ntpath.splitdrive(path)
    parts = [part for part in tail.replace("/", "\\").split("\\") if part]
    current = drive + "\\"
    chain = [current]
    for part in parts:
        current = ntpath.join(current, part)
        chain.append(current)
    return chain


def _is_reparse_point(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    if attributes & FILE_ATTRIBUTE_REPARSE_POINT:
        return True
    return stat_module.S_ISLNK(info.st_mode)


def _reject_reparse_ancestors(ops: HostOps, name: str, path: str) -> None:
    """Refuse any path reached through a reparse point.

    A junction or symlink anywhere above the target means the bytes we
    believe we created privately can land somewhere else entirely, so the ACL
    we verified proves nothing about where they went.
    """

    saw_gap = False
    for component in _ancestors(path):
        try:
            info = ops.lstat(component)
        except FileNotFoundError:
            # Everything below this point is ours to create. Keep walking
            # rather than returning: a component that exists below a missing
            # parent is impossible on a sane volume, so seeing one means the
            # namespace is not what it appears to be and nothing here may be
            # trusted.
            saw_gap = True
            continue
        except OSError as exc:
            raise WindowsWslRuntimeError(
                "path_unreadable",
                f"{_token(name)}_ancestor_{_token(type(exc).__name__)}") from exc
        if saw_gap:
            raise WindowsWslRuntimeError(
                "path_invalid", f"{name} has a component below a missing parent")
        if _is_reparse_point(info):
            raise WindowsWslRuntimeError(
                "reparse_point_rejected", f"{name} ancestor is a reparse point")


def _reject_existing(ops: HostOps, name: str, path: str) -> None:
    """The job directory must be ours alone, so it must not already exist."""

    if ops.exists(path):
        raise WindowsWslRuntimeError("path_exists", f"{name} already exists")


def _require_regular_file(ops: HostOps, name: str, path: str) -> os.stat_result:
    try:
        info = ops.lstat(path)
    except FileNotFoundError as exc:
        raise WindowsWslRuntimeError(
            "source_missing", f"{_token(name)}_missing") from exc
    except OSError as exc:
        raise WindowsWslRuntimeError(
            "path_unreadable", f"{_token(name)}_{_token(type(exc).__name__)}") from exc
    if _is_reparse_point(info):
        raise WindowsWslRuntimeError("reparse_point_rejected", f"{name} is a reparse point")
    if not stat_module.S_ISREG(info.st_mode):
        raise WindowsWslRuntimeError("source_not_regular_file", f"{name} is not a regular file")
    return info


def _identity_of(info: os.stat_result) -> tuple[int, int, int, int]:
    """The parts of a stat result that must not change under us."""

    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _directory_identity(ops: HostOps, path: str) -> str:
    try:
        info = ops.lstat(path)
    except FileNotFoundError as exc:
        raise WindowsWslRuntimeError(
            "job_dir_missing", "job directory does not exist") from exc
    except OSError as exc:
        raise WindowsWslRuntimeError(
            "path_unreadable", f"job_dir_identity_{_token(type(exc).__name__)}") from exc
    if _is_reparse_point(info):
        raise WindowsWslRuntimeError(
            "reparse_point_rejected", "job directory is a reparse point")
    if not stat_module.S_ISDIR(info.st_mode):
        raise WindowsWslRuntimeError("path_invalid", "job directory is not a directory")
    return f"{info.st_dev}:{info.st_ino}"


# ---------------------------------------------------------------------------
# Private, ACL-verified directories
# ---------------------------------------------------------------------------


def _verify_private_directory(ops: HostOps, name: str, path: str) -> None:
    """Create the directory owner-only and prove it by reading the ACL back.

    Applied to every directory this runtime depends on, not only the per-job
    one. The owner-record directory in particular is a deletion authority: a
    record another principal can write is a record that can point a reaper at
    somebody else's instance, so an unverified ACL there is fatal.
    """

    _reject_reparse_ancestors(ops, name, path)
    ops.secure_mkdir(path)

    probe = ntpath.join(path, ACL_PROBE_NAME)
    # A probe left behind by a crashed run is not evidence of anything; remove
    # it so the create below is still an exclusive create.
    with contextlib.suppress(OSError):
        ops.unlink(probe)
    try:
        handle = ops.open_exclusive_write(probe)
    except OSError as exc:
        raise WindowsWslRuntimeError(
            "acl_not_verified", f"{name} probe could not be created: {type(exc).__name__}") from exc
    ops.close(handle)
    try:
        verified, _report = ops.verify_owner_only(path, probe)
    finally:
        with contextlib.suppress(OSError):
            ops.unlink(probe)
    if not verified:
        raise WindowsWslRuntimeError(
            "acl_not_verified", f"{name} ACL could not be proven owner-only")


def _prepare_private_tree(ops: HostOps, runtime_root: str) -> None:
    """Secure and verify every directory the runtime relies on.

    The runtime root is included because it holds the per-user lock file: a
    lock in a directory anyone can write is not a lock.
    """

    for name, path in (
        ("runtime_root", runtime_root),
        ("jobs_root", jobs_root(runtime_root)),
        ("owner_records_root", owner_records_root(runtime_root)),
        ("receipts_root", receipts_root(runtime_root)),
    ):
        _verify_private_directory(ops, name, path)


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


def jobs_root(runtime_root: str) -> str:
    return ntpath.join(runtime_root, "jobs")


def owner_records_root(runtime_root: str) -> str:
    return ntpath.join(runtime_root, "owner-records")


def receipts_root(runtime_root: str) -> str:
    return ntpath.join(runtime_root, "receipts")


def lock_path(runtime_root: str) -> str:
    """The existing per-user lock file that serialises every job and reap."""

    return ntpath.join(runtime_root, "wsl-runtime.lock")


def job_directory(runtime_root: str, distro_name: str) -> str:
    return ntpath.join(jobs_root(runtime_root), distro_name)


def owner_record_path(runtime_root: str, distro_name: str) -> str:
    return ntpath.join(owner_records_root(runtime_root), distro_name + ".json")


def receipt_path(runtime_root: str, distro_name: str) -> str:
    return ntpath.join(receipts_root(runtime_root), distro_name + ".json")


# ---------------------------------------------------------------------------
# Rootfs copy
# ---------------------------------------------------------------------------


def _copy_rootfs(
    ops: HostOps,
    source_path: str,
    destination_path: str,
    *,
    max_bytes: int,
    expected_sha256: str,
) -> int:
    """Copy through one source handle, hashing while streaming.

    The handle is opened once and every check is made against that same
    handle, so a source swapped underneath us after the first stat cannot
    smuggle different bytes in. The destination is created exclusively, the
    size cap is hard, and an incomplete copy is removed before raising.
    """

    pre = _require_regular_file(ops, "rootfs_source_path", source_path)
    if pre.st_size > max_bytes:
        raise WindowsWslRuntimeError("rootfs_too_large", "source exceeds the rootfs size cap")

    source_fd = ops.open_read(source_path)
    destination_fd: int | None = None
    wrote_any = False
    try:
        opened = ops.fstat(source_fd)
        if not stat_module.S_ISREG(opened.st_mode):
            raise WindowsWslRuntimeError(
                "source_not_regular_file", "opened source is not a regular file")
        if _identity_of(opened) != _identity_of(pre):
            raise WindowsWslRuntimeError(
                "source_identity_drift", "source changed between stat and open")

        try:
            destination_fd = ops.open_exclusive_write(destination_path)
        except FileExistsError as exc:
            # Something already occupies the destination inside a directory we
            # just created privately. Refuse rather than write into it.
            raise WindowsWslRuntimeError(
                "destination_exists", "rootfs destination already exists") from exc

        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = ops.read(source_fd, COPY_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise WindowsWslRuntimeError(
                    "rootfs_too_large", "source exceeded the rootfs size cap while copying")
            digest.update(chunk)
            wrote_any = True
            offset = 0
            while offset < len(chunk):
                offset += ops.write(destination_fd, chunk[offset:])
        ops.fsync(destination_fd)

        post = ops.fstat(source_fd)
        if _identity_of(post) != _identity_of(opened):
            raise WindowsWslRuntimeError(
                "source_identity_drift", "source changed while it was being copied")
        if total != post.st_size:
            raise WindowsWslRuntimeError(
                "source_identity_drift", "copied byte count does not match the source size")
        if digest.hexdigest() != expected_sha256:
            raise WindowsWslRuntimeError(
                "rootfs_hash_mismatch", "copied rootfs does not match the pinned hash")
        return total
    except BaseException:
        # An incomplete or unverified copy must never be left where a later
        # import could pick it up.
        if destination_fd is not None or wrote_any:
            with contextlib.suppress(OSError):
                ops.unlink(destination_path)
        raise
    finally:
        if destination_fd is not None:
            with contextlib.suppress(OSError):
                ops.close(destination_fd)
        with contextlib.suppress(OSError):
            ops.close(source_fd)


def _revalidate_before_import(
    ops: HostOps,
    *,
    job_dir: str,
    install_dir: str,
    rootfs_copy: str,
    job_dir_identity: str,
    copy_identity: tuple[int, int, int, int],
) -> None:
    """Re-prove the private tree immediately before handing it to ``wsl.exe``.

    Everything checked at creation time was checked earlier; between then and
    the import the ACL write, the copy and the owner-record write all
    happened. Re-running the reparse and identity checks here is what closes
    the window in which a junction or a swapped file could redirect the
    import away from the copy we actually verified.
    """

    _reject_reparse_ancestors(ops, "job_dir", job_dir)
    _reject_reparse_ancestors(ops, "install_dir", install_dir)
    _reject_reparse_ancestors(ops, "rootfs_copy", rootfs_copy)

    if _directory_identity(ops, job_dir) != job_dir_identity:
        raise WindowsWslRuntimeError(
            "job_dir_identity_drift", "job directory changed before the import")

    info = _require_regular_file(ops, "rootfs_copy", rootfs_copy)
    if _identity_of(info) != copy_identity:
        raise WindowsWslRuntimeError(
            "rootfs_copy_identity_drift", "private rootfs copy changed before the import")


# ---------------------------------------------------------------------------
# Canaries
# ---------------------------------------------------------------------------


def canary_expectations(
    manifest: ww.PinnedBaseImageManifest, guest_runner_sha256: str
) -> dict[str, str]:
    """The exact stdout each fixed canary must produce. Nothing is inferred."""

    return {
        CANARY_HOST_MOUNT: f"{CANARY_HOST_MOUNT}:ok",
        CANARY_INTEROP: f"{CANARY_INTEROP}:ok",
        CANARY_WSL_CONF: f"{CANARY_WSL_CONF}:{WSL_CONF_SHA256}",
        CANARY_GUEST_RUNNER: f"{CANARY_GUEST_RUNNER}:{guest_runner_sha256}",
        CANARY_VERSIONS: (
            f"{CANARY_VERSIONS}:node={manifest.node_version}"
            f" claude={manifest.claude_version}"
            f" codex={manifest.codex_version}"
        ),
    }


def canary_job_spec(name: str, distro_name: str) -> dict[str, Any]:
    """A fixed canary job spec, validated by the existing job-spec rules."""

    return {
        "command": [ww.GUEST_RUNNER_PATH, "--canary", name],
        "workdir": CANARY_WORKDIR,
        "env": {"HOME": "/root", "AGENT_BRIDGE_JOB_ID": distro_name},
    }


def _matches_exactly(raw: bytes, expected: str) -> bool:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return text in (expected, expected + "\n", expected + "\r\n")


def _run_canaries(
    ops: HostOps,
    distro_name: str,
    manifest: ww.PinnedBaseImageManifest,
    guest_runner_sha256: str,
    limits: RuntimeLimits,
) -> None:
    expectations = canary_expectations(manifest, guest_runner_sha256)
    host_cwd = ops.host_cwd()
    for name in CANARY_ORDER:
        job = ww.validate_job_spec(canary_job_spec(name, distro_name))
        argv = ww.build_guest_exec_argv(distro_name, job)
        result = ops.run_host(
            argv,
            cwd=host_cwd,
            timeout=limits.canary_timeout_seconds,
            grace=limits.grace_seconds,
            stdout_cap=limits.output_max_bytes,
            stderr_cap=limits.output_max_bytes,
            stdin_data="",
        )
        failure = command_failure(result)
        if failure:
            raise WindowsWslRuntimeError(
                "canary_failed", f"{_token(name)}_{_token(failure)}")
        if not _matches_exactly(result.stdout, expectations[name]):
            raise WindowsWslRuntimeError(
                "canary_failed", f"{_token(name)}_unexpected_output")


# ---------------------------------------------------------------------------
# Owner record
# ---------------------------------------------------------------------------

_OWNER_RECORD_KEYS = frozenset({
    "schema_version", "distro_name", "install_dir", "job_dir",
    "created_at", "owner_pid", "owner_identity", "job_dir_identity",
    "install_dir_identity",
})


def build_owner_record(
    ops: HostOps,
    distro_name: str,
    job_dir: str,
    install_dir: str,
    job_dir_identity: str,
    install_dir_identity: str,
) -> dict[str, Any]:
    """The durable proof of ownership, written before the import.

    It carries no input, no output, no command, no secret, and not the rootfs
    source path: only what a later reaper needs to prove this instance is
    ours and safe to remove.
    """

    pid = ops.current_pid()
    return {
        "schema_version": SCHEMA_VERSION,
        "distro_name": distro_name,
        "job_dir": job_dir,
        "install_dir": install_dir,
        "job_dir_identity": job_dir_identity,
        "install_dir_identity": install_dir_identity,
        "created_at": ops.now_utc().isoformat(),
        "owner_pid": pid,
        "owner_identity": ops.process_identity(pid),
    }


def parse_owner_record(raw: Any, *, runtime_root: str) -> dict[str, Any]:
    """Strictly parse an owner record. Anything odd means "not ours"."""

    if not isinstance(raw, Mapping):
        raise WindowsWslRuntimeError("owner_record_invalid", "record is not a mapping")
    if set(raw) != _OWNER_RECORD_KEYS:
        raise WindowsWslRuntimeError("owner_record_invalid", "record key set is wrong")
    if raw["schema_version"] != SCHEMA_VERSION:
        raise WindowsWslRuntimeError("owner_record_invalid", "unsupported schema version")

    distro_name = raw["distro_name"]
    try:
        ww.validate_distro_name(distro_name)
    except ww.DistroNameError as exc:
        raise WindowsWslRuntimeError(
            "owner_record_invalid", "distro_name_rejected") from exc

    job_dir = _validate_local_windows_path("job_dir", raw["job_dir"])
    install_dir = _validate_local_windows_path("install_dir", raw["install_dir"])
    expected_job_dir = job_directory(runtime_root, distro_name)
    if job_dir.casefold() != expected_job_dir.casefold():
        raise WindowsWslRuntimeError(
            "owner_record_unowned", "record does not name this runtime's job directory")
    if not install_dir.casefold().startswith(job_dir.casefold() + "\\"):
        raise WindowsWslRuntimeError(
            "owner_record_unowned", "install dir is outside the job directory")

    identity = raw["job_dir_identity"]
    if not isinstance(identity, str) or ":" not in identity:
        raise WindowsWslRuntimeError("owner_record_invalid", "job_dir_identity is malformed")

    install_identity = raw["install_dir_identity"]
    if not isinstance(install_identity, str) or ":" not in install_identity:
        raise WindowsWslRuntimeError(
            "owner_record_invalid", "install_dir_identity is malformed")

    owner_pid = raw["owner_pid"]
    if isinstance(owner_pid, bool) or not isinstance(owner_pid, int) or owner_pid <= 0:
        raise WindowsWslRuntimeError("owner_record_invalid", "owner_pid is malformed")
    if not isinstance(raw["owner_identity"], str):
        raise WindowsWslRuntimeError("owner_record_invalid", "owner_identity is malformed")

    created_at = raw["created_at"]
    if not isinstance(created_at, str):
        raise WindowsWslRuntimeError("owner_record_invalid", "created_at is malformed")
    try:
        parsed = datetime.fromisoformat(created_at)
    except ValueError as exc:
        raise WindowsWslRuntimeError("owner_record_invalid", "created_at is unparseable") from exc
    if parsed.tzinfo is None:
        raise WindowsWslRuntimeError("owner_record_invalid", "created_at is not timezone-aware")

    return {
        "schema_version": SCHEMA_VERSION,
        "distro_name": distro_name,
        "job_dir": job_dir,
        "install_dir": install_dir,
        "job_dir_identity": identity,
        "install_dir_identity": install_identity,
        "created_at": parsed,
        "owner_pid": owner_pid,
        "owner_identity": raw["owner_identity"],
    }


def verify_registration_binding(ops: HostOps, parsed: Mapping[str, Any]) -> str:
    """Re-prove that a recorded instance is still the one we created.

    ``wsl.exe`` offers no stable per-registration identifier, so a distro
    name is not proof of anything: the name ``agent-bridge-<token>`` could
    have been re-registered by anything able to run ``wsl.exe --import``, and
    unregistering on the strength of a matching name would destroy a
    stranger's instance.

    The strongest binding available without WSL's cooperation is the on-disk
    one this runtime created and recorded: the job directory and the install
    directory, each still present, still a real directory, still not a
    reparse point, and still carrying the recorded ``(dev, ino)``. When that
    cannot be re-proven, the answer is a refusal code, never an assumption.

    Returns "" when the binding holds, otherwise a fixed refusal code.
    """

    for label, path, expected in (
        ("job_dir", parsed["job_dir"], parsed["job_dir_identity"]),
        ("install_dir", parsed["install_dir"], parsed["install_dir_identity"]),
    ):
        try:
            _reject_reparse_ancestors(ops, label, path)
            observed = _directory_identity(ops, path)
        except WindowsWslRuntimeError as exc:
            if exc.reason == "job_dir_missing":
                # The directory is gone. The registration may or may not
                # still exist, and nothing left on disk can tell us whether
                # the name still refers to our instance, so we stop.
                return "registration_unverifiable"
            return f"{_token(label)}_{_token(exc.reason)}"
        if observed != expected:
            return f"{_token(label)}_identity_drift"
    return ""


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def _cleanup(
    ops: HostOps,
    *,
    distro_name: str,
    job_dir: str,
    record_path: str,
    limits: RuntimeLimits,
    registration: str,
    job_dir_created: bool,
    record_written: bool,
    precheck: Any = None,
) -> CleanupReport:
    """Attempt every teardown step independently.

    A failure in one step must never skip the others: leaving a registered
    distro behind because a directory removal raised is exactly the leak this
    runtime exists to prevent. Each step reports its own outcome, judged on
    the full runner verdict rather than an exit code alone, and the aggregate
    decides whether success may be claimed at all.

    The owner record is retired last and only once containment is proven. A
    record deleted after a failed terminate would destroy the only evidence a
    later reaper has that the instance exists.

    ``registration`` says what is known about the WSL registration, and only
    ``created`` authorises ``--terminate``/``--unregister``. A distro name is
    not a capability: after a failed or ambiguous import the name may belong
    to something this run never made, so ``unproven`` refuses to touch the
    registration and refuses to delete the evidence a later reaper needs.

    ``precheck``, when supplied, is re-run immediately before the first
    destructive command. It is how the reaper re-proves, at the last possible
    moment, that the instance it is about to unregister is still its own. A
    refusal stops every step, including the deletions: if ownership cannot be
    proven, neither the registration nor the directory nor the record may be
    touched.
    """

    if registration not in (REGISTRATION_NONE, REGISTRATION_CREATED,
                            REGISTRATION_UNPROVEN):
        raise ValueError("registration must be a known registration state")

    terminate = "not_attempted"
    unregister = "not_attempted"
    filesystem = "not_attempted"
    record = "not_attempted"

    if precheck is not None:
        try:
            refusal = precheck()
        except BaseException as exc:  # noqa: BLE001 - a failed proof is a refusal
            refusal = f"precheck_{_token(type(exc).__name__)}"
        if refusal:
            refused = f"refused+{_token(refusal)}"
            attempted = registration != REGISTRATION_NONE
            return CleanupReport(
                terminate=refused if attempted else "not_attempted",
                unregister=refused if attempted else "not_attempted",
                filesystem=refused if job_dir_created else "not_attempted",
                owner_record=refused if record_written else "not_attempted",
            )

    if registration == REGISTRATION_UNPROVEN:
        # The name may be a stranger's. Refuse the commands AND refuse the
        # deletions: the job and install directories are the only evidence a
        # later reaper has, so removing them would make a leak permanent.
        terminate = unregister = "skipped+registration_unproven"
    elif registration == REGISTRATION_CREATED:
        try:
            result = ops.run_host(
                ww.build_terminate_argv(distro_name),
                cwd=ops.host_cwd(),
                timeout=limits.cleanup_timeout_seconds,
                grace=limits.grace_seconds,
                stdout_cap=limits.output_max_bytes,
                stderr_cap=limits.output_max_bytes,
            )
            failure = command_failure(result)
            terminate = "ok" if not failure else f"failed+{_token(failure)}"
        except BaseException as exc:  # noqa: BLE001 - cleanup must not propagate
            terminate = f"failed+{_token(type(exc).__name__)}"

        try:
            result = ops.run_host(
                ww.build_unregister_argv(distro_name),
                cwd=ops.host_cwd(),
                timeout=limits.cleanup_timeout_seconds,
                grace=limits.grace_seconds,
                stdout_cap=limits.output_max_bytes,
                stderr_cap=limits.output_max_bytes,
            )
            failure = command_failure(result)
            unregister = "ok" if not failure else f"failed+{_token(failure)}"
        except BaseException as exc:  # noqa: BLE001 - cleanup must not propagate
            unregister = f"failed+{_token(type(exc).__name__)}"

        if unregister == "ok":
            # A zero exit is not proof the name is gone, and the decision to
            # delete this instance's only evidence rests on it. Read it back.
            state = registration_listing(ops, distro_name, limits)
            if state == LISTING_PRESENT:
                unregister = "failed+still_registered"
            elif state == LISTING_UNUSABLE:
                unregister = "failed+unverified"

    # The job directory holds the install directory, and the recorded
    # identities of both are what the reaper re-proves ownership with. If the
    # registration was not provably destroyed, deleting them would strand a
    # registered distro that nothing can ever safely unregister again. Keep
    # them and let the reaper retry.
    # Keyed on the unregister verdict alone, and that verdict is now a
    # read-back rather than an exit code. A failed terminate still makes the
    # run incomplete below, but it does not by itself mean a registration
    # survived, and retaining the tree forever for it would leak disk with
    # nothing for the reaper to reap.
    registration_proven = unregister in ("ok", "not_attempted")
    if job_dir_created:
        if not registration_proven:
            filesystem = "retained+registration_not_proven"
        else:
            try:
                ops.remove_tree(job_dir)
                # Removal that raised nothing is not proof. Read it back.
                filesystem = "failed+still_present" if ops.exists(job_dir) else "ok"
            except FileNotFoundError:
                filesystem = "ok"
            except BaseException as exc:  # noqa: BLE001 - cleanup must not propagate
                filesystem = f"failed+{_token(type(exc).__name__)}"

    containment_proven = all(
        value in ("ok", "not_attempted")
        for value in (terminate, unregister, filesystem)
    )
    if record_written:
        if not containment_proven:
            record = "retained+containment_not_proven"
        else:
            try:
                ops.unlink(record_path)
                record = "failed+still_present" if ops.exists(record_path) else "ok"
            except FileNotFoundError:
                record = "ok"
            except BaseException as exc:  # noqa: BLE001 - cleanup must not propagate
                record = f"failed+{_token(type(exc).__name__)}"

    return CleanupReport(
        terminate=terminate, unregister=unregister,
        filesystem=filesystem, owner_record=record,
    )


# ---------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------


def build_receipt(
    *,
    distro_name: str,
    status: str,
    reason: str,
    detail: str,
    exit_code: int | None,
    canaries_passed: bool,
    spawned: bool,
    timings: Mapping[str, float],
    cleanup: CleanupReport,
    finished_at: str,
    registration: str = REGISTRATION_NONE,
) -> dict[str, Any]:
    """Timing and status only.

    Deliberately excludes the caller's input, the job's output, every argv,
    the rootfs source path, and anything credential-shaped. ``detail`` is a
    fixed internal reason string produced by this module, never peer text.
    """

    return {
        "schema_version": SCHEMA_VERSION,
        "distro_name": distro_name,
        "status": status,
        "reason": reason,
        # Sanitised again at the boundary. The exception choke point already
        # does this, but the receipt is the durable artefact and is the one
        # place worth checking twice.
        "detail": sanitize_detail(detail),
        "exit_code": exit_code,
        "canaries_passed": canaries_passed,
        "spawned": spawned,
        # Fixed vocabulary, never a path or a name from the host.
        "registration": registration,
        "finished_at": finished_at,
        "timings": {key: round(float(value), 6) for key, value in sorted(timings.items())},
        "cleanup": cleanup.as_dict(),
    }


# ---------------------------------------------------------------------------
# The lifecycle
# ---------------------------------------------------------------------------


def run_job(
    request: JobRequest,
    *,
    ops: HostOps | None = None,
    platform_name: str | None = None,
) -> RuntimeResult:
    """Run one job in one ephemeral WSL2 instance, then destroy it.

    Never raises: every operational failure becomes an aborted
    :class:`RuntimeResult`. A result is ``completed`` only when the canaries
    passed, the job exited zero, containment was proven, and the receipt
    landed.
    """

    ops = ops or HostOps()

    if not is_windows(platform_name):
        # No lock, no directory, no process. Nothing at all happens off
        # native Windows, so there is no partial state to clean up.
        return RuntimeResult(
            status=STATUS_ABORTED,
            reason=REASON_UNSUPPORTED_PLATFORM,
            detail="ephemeral WSL2 delegation only runs on native Windows",
        )

    try:
        return _run_job_on_windows(request, ops)
    except WindowsWslRuntimeError as exc:
        return RuntimeResult(status=STATUS_ABORTED, reason=exc.reason, detail=exc.detail)
    except Exception as exc:  # noqa: BLE001 - operational failures become results
        return RuntimeResult(
            status=STATUS_ABORTED, reason="internal_error", detail=type(exc).__name__)


def _run_job_on_windows(request: JobRequest, ops: HostOps) -> RuntimeResult:
    if not isinstance(request, JobRequest):
        raise WindowsWslRuntimeError("request_invalid", "request must be a JobRequest")

    limits = validate_limits(request.limits)

    if not isinstance(request.manifest, ww.PinnedBaseImageManifest):
        raise WindowsWslRuntimeError("manifest_invalid", "manifest must be a parsed manifest")
    guest_runner_sha256 = request.guest_runner_sha256
    if (not isinstance(guest_runner_sha256, str)
            or ww._ROOTFS_SHA256_RE.match(guest_runner_sha256) is None):
        raise WindowsWslRuntimeError(
            "guest_runner_hash_invalid", "guest runner hash must be 64 lowercase hex")

    try:
        distro_name = ww.build_distro_name(request.distro_token)
    except ww.DistroNameError as exc:
        raise WindowsWslRuntimeError(
            "distro_name_invalid", "token_rejected") from exc

    runtime_root = _validate_local_windows_path("runtime_root", request.runtime_root)
    source_path = _validate_local_windows_path(
        "rootfs_source_path", request.rootfs_source_path)

    try:
        job = ww.validate_job_spec(request.job_spec)
    except ww.WindowsWslContractError as exc:
        # The contract error quotes the rejected workdir or argument; only
        # the fact of rejection may be recorded.
        raise WindowsWslRuntimeError("job_spec_invalid", "contract_rejected") from exc

    if not isinstance(request.stdin_data, str):
        raise WindowsWslRuntimeError("input_invalid", "stdin_data must be a string")
    if len(request.stdin_data.encode("utf-8")) > limits.input_max_bytes:
        raise WindowsWslRuntimeError("input_too_large", "stdin payload exceeds the input cap")

    job_dir = job_directory(runtime_root, distro_name)
    install_dir = ntpath.join(job_dir, "distro")
    rootfs_copy = ntpath.join(job_dir, "rootfs.tar")
    record_path = owner_record_path(runtime_root, distro_name)
    _validate_local_windows_path("job_dir", job_dir)
    _validate_local_windows_path("install_dir", install_dir)
    _validate_local_windows_path("rootfs_copy", rootfs_copy)

    started = ops.monotonic()
    timings: dict[str, float] = {}

    # Prove every shared directory private before taking a lock inside one of
    # them or trusting a record stored in another.
    _prepare_private_tree(ops, runtime_root)

    registration = REGISTRATION_NONE
    job_dir_created = False
    record_written = False
    canaries_passed = False
    spawned = False
    exit_code: int | None = None
    stdout = b""
    stderr = b""
    status = STATUS_ABORTED
    reason = "not_run"
    detail = ""
    receipt_error = ""
    cleanup = CleanupReport()

    try:
        with ops.lock(lock_path(runtime_root), limits.lock_timeout_seconds):
            try:
                # --- private job directory --------------------------------
                _reject_reparse_ancestors(ops, "rootfs_source_path", source_path)
                _reject_reparse_ancestors(ops, "job_dir", job_dir)
                _reject_existing(ops, "job_dir", job_dir)

                ops.secure_mkdir(job_dir)
                job_dir_created = True
                _verify_private_directory(ops, "job_dir", job_dir)
                _verify_private_directory(ops, "install_dir", install_dir)

                job_dir_identity = _directory_identity(ops, job_dir)
                install_dir_identity = _directory_identity(ops, install_dir)

                # --- verified private rootfs copy -------------------------
                copy_started = ops.monotonic()
                _copy_rootfs(
                    ops, source_path, rootfs_copy,
                    max_bytes=limits.rootfs_max_bytes,
                    expected_sha256=request.manifest.rootfs_sha256,
                )
                timings["copy_seconds"] = ops.monotonic() - copy_started
                copy_identity = _identity_of(
                    _require_regular_file(ops, "rootfs_copy", rootfs_copy))

                # --- durable ownership, written BEFORE the import ---------
                # A failure here happens with the job directory already on
                # disk, so it raises into the same finally path as everything
                # else rather than leaving the tree behind.
                record = build_owner_record(
                    ops, distro_name, job_dir, install_dir,
                    job_dir_identity, install_dir_identity)
                try:
                    ops.write_json(record_path, record)
                except Exception as exc:  # noqa: BLE001
                    raise WindowsWslRuntimeError(
                        "owner_record_write_failed", type(exc).__name__) from exc
                record_written = True

                # --- re-prove the tree, then import the private copy ------
                _revalidate_before_import(
                    ops,
                    job_dir=job_dir,
                    install_dir=install_dir,
                    rootfs_copy=rootfs_copy,
                    job_dir_identity=job_dir_identity,
                    copy_identity=copy_identity,
                )
                import_argv = ww.build_import_argv(
                    distro_name, install_dir, rootfs_copy, wsl_version=2)

                # Half of the ownership proof. Unregistering by name is only
                # safe if this run is what put the name there, so establish
                # that the name is free BEFORE the import. An answer that is
                # not a clear "absent" stops the run: no import, no commands
                # against that name, nothing to clean up.
                before = registration_listing(ops, distro_name, limits)
                if before != LISTING_ABSENT:
                    raise WindowsWslRuntimeError(
                        "registration_precondition_failed",
                        "name_already_registered" if before == LISTING_PRESENT
                        else "registration_listing_unusable")

                import_started = ops.monotonic()
                spawned = True
                try:
                    import_result = ops.run_host(
                        import_argv,
                        cwd=ops.host_cwd(),
                        timeout=limits.import_timeout_seconds,
                        grace=limits.grace_seconds,
                        stdout_cap=limits.output_max_bytes,
                        stderr_cap=limits.output_max_bytes,
                    )
                except BaseException as exc:  # noqa: BLE001
                    # The call raised. That is not proof nothing registered,
                    # so ask, and let the answer decide what may be touched.
                    timings["import_seconds"] = ops.monotonic() - import_started
                    registration = registration_after_failed_import(
                        ops, distro_name, limits)
                    raise WindowsWslRuntimeError(
                        "import_failed", f"spawn_{_token(type(exc).__name__)}") from exc
                timings["import_seconds"] = ops.monotonic() - import_started
                import_failure = command_failure(import_result)
                if not import_failure:
                    registration = REGISTRATION_CREATED
                else:
                    # Failed or ambiguous: a timeout can still have registered
                    # something. The name was provably free a moment ago, so
                    # the second half of the proof decides. Never unregister
                    # on the strength of a matching name alone.
                    registration = registration_after_failed_import(
                        ops, distro_name, limits)
                    raise WindowsWslRuntimeError("import_failed", import_failure)

                # --- fixed canaries before any caller payload -------------
                canary_started = ops.monotonic()
                _run_canaries(
                    ops, distro_name, request.manifest, guest_runner_sha256, limits)
                timings["canary_seconds"] = ops.monotonic() - canary_started
                canaries_passed = True

                # --- the job itself, pipes only ---------------------------
                job_started = ops.monotonic()
                job_result = ops.run_host(
                    ww.build_guest_exec_argv(distro_name, job),
                    cwd=ops.host_cwd(),
                    timeout=limits.job_timeout_seconds,
                    grace=limits.grace_seconds,
                    stdout_cap=limits.output_max_bytes,
                    stderr_cap=limits.output_max_bytes,
                    stdin_data=request.stdin_data,
                )
                timings["job_seconds"] = ops.monotonic() - job_started
                exit_code = job_result.returncode
                job_failure = command_failure(job_result)
                if job_failure == "timed_out":
                    raise WindowsWslRuntimeError("job_timed_out", "job exceeded its timeout")
                if job_failure == "cap_exceeded":
                    raise WindowsWslRuntimeError("output_too_large", "job output exceeded the cap")
                if job_failure:
                    # A nonzero or unknown exit is a failed delegation, not a
                    # completed one, whatever the peer printed.
                    raise WindowsWslRuntimeError("job_failed", job_failure)
                stdout = job_result.stdout
                stderr = job_result.stderr
                status = STATUS_COMPLETED
                reason = REASON_OK
            except WindowsWslRuntimeError as exc:
                status, reason, detail = STATUS_ABORTED, exc.reason, exc.detail
            except Exception as exc:  # noqa: BLE001 - operational failure, not a crash
                status, reason, detail = STATUS_ABORTED, "internal_error", type(exc).__name__
            finally:
                # A true finally: reached after every exception above, and
                # every step inside it is attempted independently.
                cleanup_started = ops.monotonic()
                cleanup = _cleanup(
                    ops,
                    distro_name=distro_name,
                    job_dir=job_dir,
                    record_path=record_path,
                    limits=limits,
                    registration=registration,
                    job_dir_created=job_dir_created,
                    record_written=record_written,
                )
                timings["cleanup_seconds"] = ops.monotonic() - cleanup_started

            # Still inside the lock, deliberately. The receipt is keyed by
            # the distro name, so a receipt written after release could be
            # raced or overwritten by the next job using the same token.
            # The lock that serialises the instance also serialises its
            # record of what happened.
            timings["total_seconds"] = ops.monotonic() - started

            # Incomplete containment is the terminal verdict, whatever else
            # happened. An earlier failure does not excuse a leaked instance,
            # and must not stand in for it in the reported reason.
            if not cleanup.complete:
                prior = ("" if reason in (REASON_OK, "not_run")
                         else f" (prior failure: {reason})")
                status = STATUS_ABORTED
                reason = REASON_CLEANUP_FAILED
                detail = f"containment incomplete{prior}"
                stdout, stderr = b"", b""

            receipt = build_receipt(
                distro_name=distro_name,
                status=status,
                reason=reason,
                detail=detail,
                exit_code=exit_code,
                canaries_passed=canaries_passed,
                spawned=spawned,
                registration=registration,
                timings=timings,
                cleanup=cleanup,
                finished_at=ops.now_utc().isoformat(),
            )
            try:
                ops.write_json(receipt_path(runtime_root, distro_name), receipt)
            except Exception as exc:  # noqa: BLE001 - unrecorded is not success
                receipt_error = type(exc).__name__
    except TimeoutError as exc:
        raise WindowsWslRuntimeError("lock_unavailable", type(exc).__name__) from exc
    except OSError as exc:
        raise WindowsWslRuntimeError("lock_unavailable", type(exc).__name__) from exc

    if receipt_error:
        if reason == REASON_CLEANUP_FAILED:
            # Never let a receipt problem displace incomplete containment as
            # the reported reason.
            detail = f"{detail}; receipt write failed: {receipt_error}"
        else:
            reason = REASON_RECEIPT_WRITE_FAILED
            detail = receipt_error
        return RuntimeResult(
            status=STATUS_ABORTED,
            reason=reason,
            distro_name=distro_name,
            exit_code=exit_code,
            canaries_passed=canaries_passed,
            spawned=spawned,
            timings=timings,
            cleanup=cleanup,
            detail=detail,
        )

    return RuntimeResult(
        status=status,
        reason=reason,
        distro_name=distro_name,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        canaries_passed=canaries_passed,
        spawned=spawned,
        timings=timings,
        cleanup=cleanup,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Stale reaping
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReapReport:
    """What a reap pass actually did, per instance. Never a bare count."""

    status: str
    outcomes: Mapping[str, str] = field(default_factory=dict)
    detail: str = ""

    @property
    def reaped(self) -> list[str]:
        return sorted(name for name, outcome in self.outcomes.items() if outcome == "reaped")


def reap_stale_instances(
    *,
    runtime_root: str,
    max_age: timedelta,
    ops: HostOps | None = None,
    platform_name: str | None = None,
    active_distros: Sequence[str] = (),
    now: datetime | None = None,
) -> ReapReport:
    """Remove only instances this runtime provably owns and that are idle.

    Runs under the same per-user lock as :func:`run_job`, so a reap can never
    race a live job into removing its instance. Every candidate's name, owner
    record and directory identity are revalidated immediately before anything
    is terminated, unregistered or deleted; anything unowned, changed, or
    still active is left completely untouched.
    """

    ops = ops or HostOps()

    if not is_windows(platform_name):
        return ReapReport(
            status=REASON_UNSUPPORTED_PLATFORM,
            detail="stale reaping only runs on native Windows",
        )

    try:
        return _reap_on_windows(
            runtime_root=runtime_root, max_age=max_age, ops=ops,
            active_distros=active_distros, now=now,
        )
    except WindowsWslRuntimeError as exc:
        return ReapReport(
            status=STATUS_ABORTED, detail=f"{_token(exc.reason)}+{exc.detail}")
    except Exception as exc:  # noqa: BLE001
        return ReapReport(status=STATUS_ABORTED, detail=type(exc).__name__)


def _reap_on_windows(
    *,
    runtime_root: str,
    max_age: timedelta,
    ops: HostOps,
    active_distros: Sequence[str],
    now: datetime | None,
) -> ReapReport:
    root = _validate_local_windows_path("runtime_root", runtime_root)
    if not isinstance(max_age, timedelta) or max_age <= timedelta(0):
        raise WindowsWslRuntimeError("max_age_invalid", "max_age must be a positive timedelta")
    moment = now or ops.now_utc()
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise WindowsWslRuntimeError("now_invalid", "now must be timezone-aware")
    active = {name for name in active_distros if isinstance(name, str)}

    if not ops.exists(root):
        return ReapReport(status="ok", outcomes={})
    # The lock lives in the runtime root, and the records are the deletion
    # authority, so both directories must be proven private before either is
    # trusted.
    _verify_private_directory(ops, "runtime_root", root)

    outcomes: dict[str, str] = {}
    records_dir = owner_records_root(root)

    with ops.lock(lock_path(root), MAX_LOCK_TIMEOUT_SECONDS):
        if not ops.exists(records_dir):
            return ReapReport(status="ok", outcomes={})
        _verify_private_directory(ops, "owner_records_root", records_dir)

        try:
            entries = sorted(ops.listdir(records_dir))
        except FileNotFoundError:
            return ReapReport(status="ok", outcomes={})
        except OSError as exc:
            raise WindowsWslRuntimeError("records_unreadable", type(exc).__name__) from exc

        candidates: list[dict[str, Any]] = []
        for entry in entries:
            if entry == ACL_PROBE_NAME:
                continue
            if not entry.endswith(".json"):
                outcomes[entry] = "skipped+not_an_owner_record"
                continue
            name = entry[: -len(".json")]
            try:
                ww.validate_distro_name(name)
            except ww.DistroNameError:
                outcomes[entry] = "skipped+name_is_not_ours"
                continue
            if name in active:
                outcomes[name] = "skipped+active_in_this_process"
                continue
            path = owner_record_path(root, name)
            try:
                parsed = parse_owner_record(ops.read_json(path), runtime_root=root)
            except WindowsWslRuntimeError as exc:
                outcomes[name] = f"skipped+{_token(exc.reason)}"
                continue
            except (OSError, ValueError):
                outcomes[name] = "skipped+owner_record_unreadable"
                continue
            if parsed["distro_name"] != name:
                outcomes[name] = "skipped+owner_record_unowned"
                continue
            candidates.append(parsed)

        stale = set(ww.select_stale_instances(
            [ww.InstanceRecord(name=item["distro_name"], created_at=item["created_at"])
             for item in candidates],
            moment,
            max_age,
        ))

        for parsed in candidates:
            name = parsed["distro_name"]
            if name not in stale:
                outcomes[name] = "skipped+not_stale"
                continue
            verdict = owner_liveness(ops, parsed)
            if verdict != LIVENESS_DEAD:
                outcomes[name] = f"skipped+owner_{_token(verdict)}"
                continue

            # Revalidate immediately before acting. Everything above was read
            # earlier in this pass; between then and now the owner may have
            # rewritten the record or the directory may have been replaced,
            # and acting on the stale reading would delete someone else's
            # instance.
            path = owner_record_path(root, name)
            try:
                fresh = parse_owner_record(ops.read_json(path), runtime_root=root)
            except WindowsWslRuntimeError as exc:
                outcomes[name] = f"skipped+{_token(exc.reason)}"
                continue
            except (OSError, ValueError):
                outcomes[name] = "skipped+owner_record_vanished"
                continue
            if fresh != parsed:
                outcomes[name] = "skipped+owner_record_changed"
                continue
            verdict = owner_liveness(ops, fresh)
            if verdict != LIVENESS_DEAD:
                outcomes[name] = f"skipped+owner_{_token(verdict)}"
                continue

            # The on-disk binding is the only evidence that the name still
            # refers to our registration. A missing or drifted binding is a
            # refusal, never a licence to unregister by name.
            refusal = verify_registration_binding(ops, fresh)
            if refusal:
                outcomes[name] = f"skipped+{_token(refusal)}"
                continue

            outcomes[name] = _reap_one(ops, fresh, path)

    failed = [name for name, outcome in outcomes.items() if outcome.startswith("failed")]
    return ReapReport(
        status="ok" if not failed else "incomplete",
        outcomes=outcomes,
        detail="" if not failed else f"{len(failed)} instance(s) could not be fully reaped",
    )


def owner_liveness(ops: HostOps, parsed: Mapping[str, Any]) -> str:
    """Tri-state verdict for a record's owner. Only ``dead`` permits action.

    Two separate uncertainties have to clear before an instance may be
    touched: the OS must positively prove the pid has exited, and the pid
    must be identifiable well enough to tell "our owner exited" from "a probe
    we could not complete" or "a recycled pid we cannot compare". Either one
    unresolved leaves the instance alone.
    """

    try:
        evidence = ops.process_liveness(parsed["owner_pid"])
    except BaseException:  # noqa: BLE001 - a failed probe proves nothing
        return LIVENESS_INDETERMINATE

    verdict = classify_liveness(evidence)
    if verdict == LIVENESS_INDETERMINATE:
        return LIVENESS_INDETERMINATE

    try:
        identity = ops.process_identity(parsed["owner_pid"])
    except BaseException:  # noqa: BLE001 - unreadable identity is uncertainty
        return LIVENESS_INDETERMINATE

    if verdict == LIVENESS_ALIVE:
        if not identity:
            # Something is running under that pid and we cannot tell whether
            # it is the owner. Not proof of anything, so leave it alone.
            return LIVENESS_INDETERMINATE
        return LIVENESS_ALIVE if identity == parsed["owner_identity"] else LIVENESS_DEAD

    if identity and identity == parsed["owner_identity"]:
        # The probe says exited but something still answers with the owner's
        # identity. Contradictory evidence is not proof of death.
        return LIVENESS_INDETERMINATE
    return LIVENESS_DEAD


def _reap_one(ops: HostOps, parsed: Mapping[str, Any], path: str) -> str:
    """Terminate, unregister, remove the directory, retire the record.

    Reuses the same independent-step cleanup as the live path, so a reap and
    a normal teardown cannot drift apart, including the rule that the record
    survives until containment is proven. The binding is re-proven one last
    time inside ``_cleanup``, immediately before the first destructive
    command, because everything checked above this line was checked before
    the ``wsl.exe`` process existed.
    """

    report = _cleanup(
        ops,
        distro_name=parsed["distro_name"],
        job_dir=parsed["job_dir"],
        record_path=path,
        limits=DEFAULT_LIMITS,
        # The reaper's ownership proof is the precheck below, re-run
        # immediately before the first destructive command, not a name match.
        registration=REGISTRATION_CREATED,
        job_dir_created=True,
        record_written=True,
        precheck=lambda: verify_registration_binding(ops, parsed),
    )
    if report.complete:
        return "reaped"
    outcome = report.as_dict()
    if any(str(value).startswith("refused") for key, value in outcome.items()
           if key != "complete"):
        return "skipped+registration_unverifiable"
    broken = [
        f"{_token(step)}_{_token(value)}"
        for step, value in outcome.items()
        if step != "complete" and not str(value).startswith(("ok", "not_attempted"))
    ]
    return "failed+" + "+".join(broken)
