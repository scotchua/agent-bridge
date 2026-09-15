"""Wire the ephemeral WSL2 runtime into execution dispatch.

:class:`WindowsWslExecutor` has the same shape as the POSIX
``SubprocessHarnessExecutor``: it is called with ``(request, job_dir)`` and
returns an outcome mapping. Everything underneath is the already-hardened
lifecycle in :mod:`windows_wsl_runtime`, so this module is a translator and a
gate rather than a second implementation of anything.

What it translates:

* a queue request into a :class:`~.windows_wsl_runtime.JobRequest` whose job
  spec invokes the pinned guest runner and carries nothing variable on the
  argv. The request the queue actually stores names a provider, a repository,
  a brief file, a base and a list of verification commands, so that is what is
  translated: the admitted worktree is packed into the bounded archive the
  guest protocol defines, the brief travels in the request body, and the
  verification commands are re-validated against the guest's own allowlist;
* a :class:`~.windows_wsl_runtime.RuntimeResult` back into the outcome shape
  the queue records, with output hashed rather than stored.

What it gates, fail closed and before anything is spawned:

* not native Windows;
* provisioning not complete and the boundary not verified on this machine;
* a manifest or rootfs that does not match its pinned hash;
* an image built for the wrong architecture;
* a request whose classification has not been approved for delegation;
* every provider job, until a live verification record on this machine says
  the session handoff was actually observed working. See
  :data:`PROVIDER_EXECUTION_BLOCKER`.

Nothing here has been run against a live Windows host.
"""

from __future__ import annotations

import base64
import contextlib
import errno
import hashlib
import io
import json
import os
import stat
import tarfile
import platform as platform_module
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from . import guest_runner, windows_auth as wa, windows_evidence as wev
from . import windows_rootfs as wrf
from . import windows_wsl as ww
from . import windows_wsl_provision as wp
from . import windows_wsl_runtime as wr

SCHEMA_VERSION = 1

#: Classifications this runtime will accept without a separate, explicit
#: approval. The defaults the rest of the bridge uses: public and synthetic
#: material only. Anything client-derived needs an approval recorded against
#: the request, never a default.
#: These are the queue's own labels, not a second vocabulary. ``internal`` was
#: previously used here and does not exist anywhere else in the bridge; the
#: queue admits ``internal_nonclient``, and inventing a near-miss synonym is
#: how a label ends up meaning one thing at admission and another at dispatch.
DEFAULT_ALLOWED_CLASSIFICATIONS = frozenset({"public", "synthetic",
                                             "internal_nonclient"})

#: Classifications that may only run with an explicit, recorded approval.
APPROVAL_REQUIRED_CLASSIFICATIONS = frozenset({"client", "client_derived",
                                               "confidential"})

#: Every tool the guest can run. Provider tools are on this list because the
#: runner can now execute them with a session capsule, but reaching them still
#: requires a recorded live verification: see
#: :mod:`agent_bridge.orchestration.windows_evidence`.
SUPPORTED_TOOLS = frozenset(guest_runner.TOOL_NAMES)

#: Tools that need a provider session, and the queue provider each serves.
PROVIDER_TOOLS = frozenset(guest_runner.PROVIDER_TOOLS)

GUEST_JOB_WORKDIR = "/workspace/job"


class DelegationRefused(RuntimeError):
    """A fixed, path-free refusal code. Never carries an OS message."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = wr.sanitize_detail(detail)


@dataclass(frozen=True)
class DelegationConfig:
    """Per-user, absolute, pinned paths. All supplied, none discovered."""

    runtime_root: str
    rootfs_path: str
    manifest_path: str
    sidecar_path: str | None = None
    allowed_classifications: frozenset[str] = DEFAULT_ALLOWED_CLASSIFICATIONS
    limits: wr.RuntimeLimits = wr.DEFAULT_LIMITS


def load_manifest(path: str) -> ww.PinnedBaseImageManifest:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise DelegationRefused("manifest_unreadable", type(exc).__name__) from exc
    except ValueError as exc:
        raise DelegationRefused("manifest_malformed", type(exc).__name__) from exc
    try:
        return ww.parse_manifest(raw)
    except ww.ManifestError as exc:
        # The contract error quotes the offending value; only the fact of
        # rejection may be recorded.
        raise DelegationRefused("manifest_invalid", "contract_rejected") from exc


def load_sidecar(path: str | None) -> Mapping[str, Any] | None:
    if not path:
        return None
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise DelegationRefused("sidecar_unreadable", type(exc).__name__) from exc
    except ValueError as exc:
        raise DelegationRefused("sidecar_malformed", type(exc).__name__) from exc
    if not isinstance(raw, Mapping):
        raise DelegationRefused("sidecar_malformed", "not_an_object")
    return raw


def host_architecture(machine: str | None = None) -> str | None:
    return wrf.host_architecture_for(
        machine if machine is not None else platform_module.machine())


def guest_runner_sha256(source: str | os.PathLike[str] | None = None) -> str:
    """Hash the runner this repository ships.

    The host pins the runner's hash and a canary proves the guest's copy
    matches. Taking the pin from the file in this checkout means the two can
    only disagree if the image was built from different source, which is
    exactly what the canary exists to catch.
    """

    path = Path(source) if source is not None else Path(guest_runner.__file__)
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def classification_allowed(classification: Any, config: DelegationConfig, *,
                           approvals: Mapping[str, Any] | None = None) -> str:
    """Which gate a classification passes through, or a refusal code.

    Returns "" when the request may proceed. Client-derived material never
    passes on a default: it needs an approval recorded against this request,
    naming this classification.
    """

    if not isinstance(classification, str) or not classification:
        return "classification_missing"
    if classification in APPROVAL_REQUIRED_CLASSIFICATIONS:
        approved = (approvals or {}).get("classification")
        if approved != classification:
            return "classification_requires_explicit_approval"
        return ""
    if classification not in config.allowed_classifications:
        return "classification_not_allowed"
    return ""


def build_job_request(*, config: DelegationConfig, manifest: ww.PinnedBaseImageManifest,
                      tool: str, args: list[str], stdin_data: str,
                      distro_token: str | None = None,
                      auth: Mapping[str, str] | None = None,
                      workspace_tar_b64: str | None = None) -> wr.JobRequest:
    """One bare tool invocation, expressed as a runtime job.

    Not the production dispatch path: the queue submits provider work, which
    :func:`build_provider_job_request` translates. This shape exists for live
    boundary verification, where the point is to run a fixed, trivial command
    inside the guest and observe the canaries, with no provider and no
    session involved.

    The guest job spec is fixed: it runs the pinned guest runner with
    ``--run`` and nothing else. Everything variable travels inside the JSON
    that goes down the pipe, where both sides validate it, rather than on an
    argv where a single unescaped value would be a command.
    """

    if tool not in SUPPORTED_TOOLS:
        raise DelegationRefused("tool_not_supported", wr._token(tool))
    payload = {
        "schema_version": guest_runner.SCHEMA_VERSION,
        "mode": guest_runner.MODE_TOOL,
        "tool": tool,
        "args": list(args),
        "workdir": GUEST_JOB_WORKDIR,
        "timeout_seconds": config.limits.job_timeout_seconds,
        "env": {"HOME": guest_runner.JOB_HOME},
        "stdin": stdin_data,
        "auth": dict(auth) if auth is not None else None,
        "workspace_tar_b64": workspace_tar_b64,
    }
    # Validate with the guest's own validator before it is ever sent. A
    # request the guest would reject should fail here, where the refusal can
    # name a reason, rather than inside a sandbox that is about to be deleted.
    try:
        guest_runner.validate_request(payload)
    except guest_runner.GuestRunnerError as exc:
        raise DelegationRefused("guest_request_invalid", exc.code) from exc

    return wr.JobRequest(
        runtime_root=config.runtime_root,
        rootfs_source_path=config.rootfs_path,
        manifest=manifest,
        guest_runner_sha256=guest_runner_sha256(),
        distro_token=distro_token or secrets.token_hex(8),
        job_spec={
            "command": [ww.GUEST_RUNNER_PATH, "--run"],
            "workdir": GUEST_JOB_WORKDIR,
            "env": {"HOME": "/root"},
        },
        stdin_data=json.dumps(payload, separators=(",", ":"), sort_keys=True),
        limits=config.limits,
    )


def build_auth_probe_request(*, config: DelegationConfig,
                             manifest: ww.PinnedBaseImageManifest,
                             provider: str,
                             auth: Mapping[str, str],
                             distro_token: str | None = None,
                             timeout_seconds: float | None = None,
                             ) -> wr.JobRequest:
    """One minimal authenticated provider operation, for the lane observation.

    Everything variable about a job is absent here. There is no brief, no
    workspace, no verification and no model choice, because the probe is not
    doing work: it is establishing whether a session minted on this host is
    honoured by this provider from inside this guest. The prompt and the
    expected answer live in the guest runner as constants, so nothing on the
    host can arrange the result.

    The capsule is required. A probe without one would answer a question
    nobody asked.
    """

    if provider not in PROVIDER_TOOLS:
        raise DelegationRefused("provider_not_supported", wr._token(provider))
    if not auth:
        raise DelegationRefused("auth_required", wr._token(provider))
    timeout = min(float(timeout_seconds if timeout_seconds is not None
                        else AUTH_PROBE_TIMEOUT_SECONDS),
                  float(config.limits.job_timeout_seconds))
    payload = {
        "schema_version": guest_runner.SCHEMA_VERSION,
        "mode": guest_runner.MODE_AUTH_PROBE,
        "provider": provider,
        "workdir": GUEST_JOB_WORKDIR,
        "timeout_seconds": timeout,
        "env": {"HOME": guest_runner.JOB_HOME},
        "auth": dict(auth),
    }
    try:
        guest_runner.validate_request(payload)
    except guest_runner.GuestRunnerError as exc:
        raise DelegationRefused("guest_request_invalid", exc.code) from exc

    return wr.JobRequest(
        runtime_root=config.runtime_root,
        rootfs_source_path=config.rootfs_path,
        manifest=manifest,
        guest_runner_sha256=guest_runner_sha256(),
        distro_token=distro_token or secrets.token_hex(8),
        job_spec={
            "command": [ww.GUEST_RUNNER_PATH, "--run"],
            "workdir": GUEST_JOB_WORKDIR,
            "env": {"HOME": "/root"},
        },
        stdin_data=json.dumps(payload, separators=(",", ":"), sort_keys=True),
        limits=config.limits,
    )


#: One model turn answering with a fixed string. Generous enough for a cold
#: start and a slow network, short enough that a hung provider is a failed
#: observation rather than a stalled installer.
AUTH_PROBE_TIMEOUT_SECONDS = 180.0


#: Bounds on the tree that is packed for one job. Checked while the archive is
#: being built, not after, so a repository that is far too large is refused in
#: bounded memory rather than assembled and then rejected.
#:
#: Taken from the guest rather than chosen again here. They used to be
#: separate numbers and they disagreed: this side would happily build an
#: archive the far side would refuse, so an oversized repository failed with
#: a reason naming the transport rather than the repository.
MAX_WORKSPACE_FILES = 4096
MAX_WORKSPACE_CONTENT_BYTES = guest_runner.MAX_WORKSPACE_CONTENT_BYTES
MAX_WORKSPACE_FILE_BYTES = guest_runner.MAX_WORKSPACE_FILE_BYTES

#: Never packed. ``.git`` in particular: the guest builds its own baseline
#: commit in :func:`guest_runner.unpack_workspace`, so sending the host's
#: history would ship reflogs, remotes, and credentials helpers into a guest
#: that has no use for any of them, and would make the returned patch describe
#: the repository's past rather than the job's changes.
WORKSPACE_EXCLUDED_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".idea", ".vscode",
})


def _reparse_tag(info: os.stat_result) -> int:
    """Non-zero for a Windows reparse point of any kind.

    ``entry.is_symlink()`` is not this check. A directory junction and a
    volume mount point are reparse points that are not symlinks, and on
    Windows a junction reports as an ordinary directory to every test except
    this one. Packing through a junction is how a tree outside the repository
    ends up inside the archive under an in-repository name, which is the whole
    attack this function is written against.
    """

    return int(getattr(info, "st_reparse_tag", 0) or 0)


def _is_link_like(entry: os.DirEntry, info: os.stat_result) -> bool:
    return entry.is_symlink() or bool(_reparse_tag(info))


class _PackRoot:
    """The one directory the archive is allowed to contain bytes from.

    Resolved once, at the start, and then every packed file is proven to live
    under it. The structural argument (no reparse point is ever traversed, so
    nothing outside can be reached) is the real defence; this is the check
    that makes the structural argument falsifiable instead of asserted.
    """

    def __init__(self, root: Path) -> None:
        self.path = root
        self.real = os.path.realpath(root)
        self.prefix = self.real.rstrip(os.sep) + os.sep

    def contains(self, candidate: str) -> bool:
        resolved = os.path.realpath(candidate)
        return resolved.startswith(self.prefix) and resolved != self.real


def pack_workspace(repo: str | os.PathLike[str]) -> tuple[str, dict[str, int]]:
    """Pack the admitted worktree into the guest's bounded archive.

    Only regular files, and only ones proven to live inside the repository.
    Three separate things could put foreign bytes in this archive, and each
    has its own defence:

    * A **symlink** is not followed and not packed. The guest refuses symlink
      members on extraction anyway, and following one here would mean the host
      reading a file outside the repository and handing it across the boundary
      under an in-repository name.
    * A **junction, mount point or other reparse point** is refused by tag,
      not by ``is_symlink()``. On Windows a junction is a directory that
      answers yes to ``is_dir`` and no to ``is_symlink``; traversing one walks
      into whatever it points at. ``O_NOFOLLOW`` does not help here because
      Windows does not have it, so the tag is checked directly on every entry
      before it is opened or descended into.
    * A **swap between the check and the read** is caught by identity. Each
      file's ``(device, inode)`` is taken from the ``lstat`` that approved it
      and compared against the ``fstat`` of the descriptor actually read. A
      file replaced in that window, by a symlink or by anything else, has a
      different identity and is refused rather than packed.

    Sockets, FIFOs and device nodes are skipped rather than opened, because
    opening one is how a pack step blocks forever on a tree somebody else
    controls.

    The archive is deterministic: sorted entries, fixed ownership, fixed
    timestamps, fixed modes. Two packs of the same tree produce the same bytes,
    which is what makes the recorded digest mean anything.
    """

    root = Path(repo)
    if not root.is_dir():
        raise DelegationRefused("workspace_unreadable", "not_a_directory")
    try:
        info = os.lstat(root)
    except OSError as exc:
        raise DelegationRefused("workspace_unreadable", type(exc).__name__) from exc
    if stat.S_ISLNK(info.st_mode) or _reparse_tag(info):
        # The repository root itself being a link or junction is the
        # operator's own layout decision, but it is not one this can pack
        # safely: everything below it is reached through the link, so the
        # containment check would be measuring the wrong root.
        raise DelegationRefused("workspace_unreadable", "root_is_a_link")

    boundary = _PackRoot(root)
    buffer = io.BytesIO()
    counts = {"files": 0, "bytes": 0, "skipped": 0, "links_refused": 0}
    try:
        with tarfile.open(fileobj=buffer, mode="w:gz", compresslevel=6) as archive:
            _pack_directory(archive, boundary, root, counts)
    except OSError as exc:
        raise DelegationRefused("workspace_unreadable", type(exc).__name__) from exc
    payload = buffer.getvalue()
    if len(payload) > guest_runner.MAX_WORKSPACE_BYTES:
        raise DelegationRefused("workspace_too_large", "archive")
    encoded = base64.b64encode(payload).decode("ascii")
    if len(encoded) > guest_runner.MAX_WORKSPACE_B64_CHARS:
        raise DelegationRefused("workspace_too_large", "encoded")
    counts["archive_bytes"] = len(payload)
    return encoded, counts


def _pack_directory(archive: tarfile.TarFile, boundary: _PackRoot,
                    directory: Path, counts: dict[str, int]) -> None:
    try:
        entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
    except OSError as exc:
        raise DelegationRefused("workspace_unreadable", type(exc).__name__) from exc
    for entry in entries:
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            # Vanished between the scan and the stat. Nothing to pack, and
            # nothing that needs saying about it.
            counts["skipped"] += 1
            continue
        if _is_link_like(entry, info):
            counts["links_refused"] += 1
            counts["skipped"] += 1
            continue
        if stat.S_ISDIR(info.st_mode):
            if entry.name in WORKSPACE_EXCLUDED_DIRS:
                continue
            child = Path(entry.path)
            if not boundary.contains(str(child)):
                # Belt to the reparse check's braces. A directory that is not
                # a reparse point and still resolves outside the repository is
                # something this does not understand, so it does not pack it.
                counts["links_refused"] += 1
                counts["skipped"] += 1
                continue
            _pack_directory(archive, boundary, child, counts)
            continue
        if not stat.S_ISREG(info.st_mode):
            counts["skipped"] += 1
            continue
        _pack_file(archive, boundary, Path(entry.path), info, counts)


def _pack_file(archive: tarfile.TarFile, boundary: _PackRoot, path: Path,
               approved: os.stat_result, counts: dict[str, int]) -> None:
    if counts["files"] >= MAX_WORKSPACE_FILES:
        raise DelegationRefused("workspace_too_many_files")
    name = path.relative_to(boundary.path).as_posix()
    if not boundary.contains(str(path)):
        raise DelegationRefused("workspace_outside_repository")
    # O_NOFOLLOW where it exists; the identity comparison below is what does
    # the work on Windows, where it does not.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise DelegationRefused("workspace_unreadable", type(exc).__name__) from exc
    try:
        status = os.fstat(descriptor)
        # The object actually opened must be the object that was approved.
        # This is the check that survives a swap on a platform with no
        # O_NOFOLLOW: a replacement file has its own identity.
        if (status.st_dev, status.st_ino) != (approved.st_dev, approved.st_ino):
            raise DelegationRefused("workspace_file_changed")
        if not stat.S_ISREG(status.st_mode) or _reparse_tag(status):
            counts["skipped"] += 1
            return
        if status.st_size > MAX_WORKSPACE_FILE_BYTES:
            raise DelegationRefused("workspace_file_too_large")
        if counts["bytes"] + status.st_size > MAX_WORKSPACE_CONTENT_BYTES:
            raise DelegationRefused("workspace_too_large", "content")
        with open(descriptor, "rb", closefd=False) as handle:
            payload = handle.read(status.st_size + 1)
    finally:
        os.close(descriptor)
    if len(payload) > status.st_size:
        # The file grew between the fstat and the read. Whatever is being
        # packed is not the thing that was measured.
        raise DelegationRefused("workspace_file_changed")
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mode = 0o600
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.type = tarfile.REGTYPE
    archive.addfile(info, io.BytesIO(payload))
    counts["files"] += 1
    counts["bytes"] += len(payload)


def read_brief(path: str | os.PathLike[str], *,
               expected_sha256: str | None = None) -> str:
    """Read the admitted brief through one descriptor, and prove it is the one.

    The queue hashes the brief at admission and checks that hash again before
    it dispatches. It then throws the bytes away and hands over a pathname,
    which is where the gap was: this function used to open that name a second
    time, so the file that was hashed and the file that reached the provider
    were only the same file if nothing moved in between. A brief is the
    instruction a model acts on, so substituting one is substituting the job.

    Closed by doing both through one descriptor. The name is opened once, the
    handle is checked for type, size and identity, the bytes are read from
    that handle, and the digest is compared immediately. There is no second
    read of the pathname anywhere in this path.

    ``expected_sha256`` is not optional in production: :meth:`translate`
    always passes what the queue admitted. It defaults to None so a caller
    that genuinely has no admitted digest, such as a first-time enrolment
    preview, fails loudly at the call site rather than silently here.
    """

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if isinstance(exc, (IsADirectoryError, FileNotFoundError)):
            raise DelegationRefused("brief_invalid",
                                    "not_a_regular_file") from exc
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            # O_NOFOLLOW refused a symlink. Reported as what it is rather than
            # as a generic read failure: the two need different fixes.
            raise DelegationRefused("brief_invalid",
                                    "not_a_regular_file") from exc
        raise DelegationRefused("brief_unreadable", type(exc).__name__) from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise DelegationRefused("brief_invalid", "not_a_regular_file")
        if info.st_size > guest_runner.MAX_BRIEF_BYTES:
            raise DelegationRefused("brief_too_large")
        payload = _read_descriptor(descriptor, guest_runner.MAX_BRIEF_BYTES)
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino):
            raise DelegationRefused("brief_invalid", "changed_while_reading")
    except OSError as exc:
        raise DelegationRefused("brief_unreadable", type(exc).__name__) from exc
    finally:
        with contextlib.suppress(OSError):
            os.close(descriptor)

    if len(payload) > guest_runner.MAX_BRIEF_BYTES:
        raise DelegationRefused("brief_too_large")
    if expected_sha256 is not None:
        digest = hashlib.sha256(payload).hexdigest()
        if not _constant_time_equal(digest, str(expected_sha256)):
            # The brief that reaches the provider is not the brief the queue
            # admitted. There is no safe way to continue from here.
            raise DelegationRefused("brief_changed_after_admission")
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DelegationRefused("brief_invalid", "not_utf8") from exc


def _read_descriptor(descriptor: int, limit: int) -> bytes:
    payload = b""
    while len(payload) <= limit:
        chunk = os.read(descriptor, 65536)
        if not chunk:
            break
        payload += chunk
    return payload


def _constant_time_equal(first: str, second: str) -> bool:
    import hmac
    return hmac.compare_digest(first, second)


# ---------------------------------------------------------------------------
# The base the job is measured against
# ---------------------------------------------------------------------------

#: Where git is looked up on the host. A bare name would resolve through PATH,
#: which a repository can influence the moment it can write a file.
_GIT_CANDIDATES = ("git",)

BASE_SEMANTICS = (
    "The queue admits a base and the POSIX lane honours it: it resolves the "
    "base to a commit and builds a detached worktree at exactly that commit. "
    "This lane packs the working tree, so the only honest reading of a base "
    "is 'that commit, plus whatever is dirty on top of it'. That is only true "
    "when HEAD is the admitted base, so HEAD is resolved and compared, and a "
    "repository sitting on a different commit is refused by name rather than "
    "delegated with a base nobody checked"
)


def resolve_base(repo: str | os.PathLike[str], base: object,
                 *, run: Any = None) -> str:
    """The commit the admitted base names, proven to be what is packed.

    Returns the resolved 40-character SHA. Raises rather than guessing: an
    unresolvable base and a base that is not what the worktree is sitting on
    are different refusals, because they need different fixes.
    """

    if not isinstance(base, str) or not base.strip():
        raise DelegationRefused("base_missing")
    text = base.strip()
    if len(text) > 256 or any(character.isspace() for character in text):
        raise DelegationRefused("base_invalid")
    caller = run if run is not None else _run_git
    base_sha = caller(repo, ["rev-parse", "--verify", f"{text}^{{commit}}"])
    if base_sha is None:
        raise DelegationRefused("base_unresolvable")
    head_sha = caller(repo, ["rev-parse", "--verify", "HEAD^{commit}"])
    if head_sha is None:
        raise DelegationRefused("base_unresolvable", "head")
    if head_sha != base_sha:
        # Packing the working tree while claiming a different base would put
        # a patch in the receipt that does not apply to the commit the
        # receipt names.
        raise DelegationRefused("base_mismatch")
    return base_sha


def _run_git(repo: str | os.PathLike[str], args: list[str]) -> str | None:
    """One bounded git query. None when git could not answer."""

    import shutil
    import subprocess

    for candidate in _GIT_CANDIDATES:
        program = shutil.which(candidate)
        if program:
            break
    else:
        return None
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, shell off
            [program, "-C", str(repo)] + list(args), shell=False,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    value = completed.stdout.decode("ascii", "replace").strip()
    if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
        return None
    return value


def build_provider_job_request(*, config: DelegationConfig,
                               manifest: ww.PinnedBaseImageManifest,
                               provider: str, model: str, effort: str,
                               brief: str, verify_argv: list[list[str]],
                               workspace_tar_b64: str,
                               auth: Mapping[str, str] | None,
                               distro_token: str | None = None,
                               verify_timeout_seconds: float | None = None,
                               ) -> wr.JobRequest:
    """One real queued provider job, expressed as a runtime job.

    This is the production translation. Nothing bespoke: every field comes
    from the request the queue admitted and stored, and the whole payload is
    put through the guest's own validator before it is sent, so a request the
    guest would refuse fails here where the refusal can be named.
    """

    if provider not in PROVIDER_TOOLS:
        raise DelegationRefused("provider_not_supported", wr._token(provider))
    timeout = float(config.limits.job_timeout_seconds)
    payload = {
        "schema_version": guest_runner.SCHEMA_VERSION,
        "mode": guest_runner.MODE_PROVIDER_JOB,
        "provider": provider,
        "model": model,
        "effort": effort,
        "brief": brief,
        "verify_argv": [list(command) for command in verify_argv],
        "workdir": GUEST_JOB_WORKDIR,
        "timeout_seconds": timeout,
        "verify_timeout_seconds": min(
            float(verify_timeout_seconds
                  if verify_timeout_seconds is not None
                  else guest_runner.DEFAULT_VERIFY_TIMEOUT_SECONDS),
            timeout),
        "env": {"HOME": guest_runner.JOB_HOME},
        "auth": dict(auth) if auth is not None else None,
        "workspace_tar_b64": workspace_tar_b64,
    }
    try:
        guest_runner.validate_request(payload)
    except guest_runner.GuestRunnerError as exc:
        raise DelegationRefused("guest_request_invalid", exc.code) from exc

    return wr.JobRequest(
        runtime_root=config.runtime_root,
        rootfs_source_path=config.rootfs_path,
        manifest=manifest,
        guest_runner_sha256=guest_runner_sha256(),
        distro_token=distro_token or secrets.token_hex(8),
        job_spec={
            "command": [ww.GUEST_RUNNER_PATH, "--run"],
            "workdir": GUEST_JOB_WORKDIR,
            "env": {"HOME": "/root"},
        },
        stdin_data=json.dumps(payload, separators=(",", ":"), sort_keys=True),
        limits=config.limits,
    )


#: Exactly the fields the guest sends. A response with more is a guest that is
#: not the pinned guest; a response with fewer is one that failed halfway.
RESPONSE_KEYS = frozenset(guest_runner.RESPONSE_KEYS)

RESPONSE_STATUSES = ("completed", "aborted")

#: Refusal reasons are fixed tokens on both sides. The guest's reason is
#: recorded, so it is held to the same shape as everything else that crosses
#: the boundary rather than pasted into a receipt as free text.
_REASON_MAX = 64


def _decode_field(parsed: Mapping[str, Any], key: str, limit: int) -> bytes:
    value = parsed.get(key)
    if not isinstance(value, str):
        raise DelegationRefused("guest_response_malformed", key)
    if len(value) > ((limit + 2) // 3) * 4 + 4:
        raise DelegationRefused("guest_response_too_large", key)
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise DelegationRefused("guest_response_not_base64", key) from exc
    if len(decoded) > limit:
        raise DelegationRefused("guest_response_too_large", key)
    return decoded


def parse_guest_response(raw: bytes) -> dict[str, Any]:
    """Read the guest's single response object, fail closed.

    The guest is inside the boundary and its output is untrusted input on this
    side of it. Every field is checked for presence, type and bound, and the
    base64 payloads are decoded here rather than carried around encoded, so a
    malformed payload is refused at the edge instead of at whatever later
    point first tried to use it.
    """

    if len(raw) > guest_runner.MAX_TOTAL_REQUEST_BYTES:
        raise DelegationRefused("guest_response_too_large", "envelope")
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise DelegationRefused("guest_response_malformed", type(exc).__name__) from exc
    if not isinstance(parsed, Mapping):
        raise DelegationRefused("guest_response_malformed", "not_an_object")
    if set(parsed) != RESPONSE_KEYS:
        raise DelegationRefused("guest_response_keys_invalid")
    if parsed.get("schema_version") != guest_runner.SCHEMA_VERSION:
        raise DelegationRefused("guest_response_schema_unsupported")

    status = parsed.get("status")
    if status not in RESPONSE_STATUSES:
        raise DelegationRefused("guest_response_status_unknown")

    reason = parsed.get("reason")
    if not isinstance(reason, str) or not reason or len(reason) > _REASON_MAX:
        raise DelegationRefused("guest_response_reason_invalid")
    if not all(character.isalnum() or character == "_" for character in reason):
        raise DelegationRefused("guest_response_reason_invalid")

    exit_code = parsed.get("exit_code")
    if exit_code is not None:
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            raise DelegationRefused("guest_response_exit_code_invalid")
        if not -256 <= exit_code <= 256:
            raise DelegationRefused("guest_response_exit_code_invalid")

    truncated = parsed.get("truncated")
    if not isinstance(truncated, bool):
        raise DelegationRefused("guest_response_malformed", "truncated")

    duration = parsed.get("duration_seconds")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        raise DelegationRefused("guest_response_malformed", "duration")
    if not 0 <= float(duration) <= guest_runner.MAX_TIMEOUT_SECONDS * 2:
        raise DelegationRefused("guest_response_malformed", "duration")

    harness_status = parsed.get("harness_status")
    if harness_status not in guest_runner.HARNESS_STATUSES:
        raise DelegationRefused("guest_response_harness_status_invalid")

    verification = _parse_verification(parsed.get("verification"))

    stdout = _decode_field(parsed, "stdout_b64", guest_runner.MAX_OUTPUT_BYTES)
    stderr = _decode_field(parsed, "stderr_b64", guest_runner.MAX_OUTPUT_BYTES)
    diff = _decode_field(parsed, "diff_b64", guest_runner.MAX_DIFF_BYTES)

    # A completed job that reports truncation is a contradiction: the runner
    # aborts on truncation precisely so a prefix is never mistaken for output.
    if status == "completed" and truncated:
        raise DelegationRefused("guest_response_inconsistent", "truncated_completed")
    if status == "completed" and exit_code != 0:
        raise DelegationRefused("guest_response_inconsistent", "completed_nonzero")

    # The two status fields answer different questions and must agree about
    # the one they share. "completed" means the guest finished its own work;
    # the harness status says whether that work passed. A guest that reports a
    # completion with a status that is not a completion or a verification
    # failure is not the pinned guest.
    if status == "completed" and harness_status not in (
            guest_runner.HARNESS_COMPLETE, guest_runner.HARNESS_VERIFICATION_FAILED):
        raise DelegationRefused("guest_response_inconsistent", "completed_status")
    if status == "aborted" and harness_status in (
            guest_runner.HARNESS_COMPLETE, guest_runner.HARNESS_VERIFICATION_FAILED):
        raise DelegationRefused("guest_response_inconsistent", "aborted_status")
    if harness_status == guest_runner.HARNESS_COMPLETE and any(
            item["returncode"] != 0 for item in verification):
        raise DelegationRefused("guest_response_inconsistent", "complete_with_failure")
    if harness_status == guest_runner.HARNESS_VERIFICATION_FAILED and not any(
            item["returncode"] != 0 for item in verification):
        raise DelegationRefused("guest_response_inconsistent", "failure_without_evidence")

    return {
        "status": status,
        "harness_status": harness_status,
        "reason": reason,
        "exit_code": exit_code,
        "truncated": truncated,
        "duration_seconds": float(duration),
        "verification": verification,
        "stdout": stdout,
        "stderr": stderr,
        "diff": diff,
    }


def _parse_verification(raw: object) -> list[dict[str, Any]]:
    """The guest's verification evidence, checked field by field.

    This is the record of what was actually run against the patch, so it is
    held to the same standard as everything else crossing the boundary: fixed
    keys, bounded values, and a program name that is one of the ones the guest
    was allowed to resolve.
    """

    if not isinstance(raw, list):
        raise DelegationRefused("guest_response_verification_invalid", "not_a_list")
    if len(raw) > guest_runner.MAX_VERIFY_COMMANDS:
        raise DelegationRefused("guest_response_verification_invalid", "too_many")
    evidence: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping) or set(item) != guest_runner.VERIFICATION_KEYS:
            raise DelegationRefused("guest_response_verification_invalid", "keys")
        program = item["program"]
        if program not in guest_runner.ALLOWED_VERIFY_PROGRAMS:
            raise DelegationRefused("guest_response_verification_invalid", "program")
        code = item["returncode"]
        if isinstance(code, bool) or not isinstance(code, int) or not -256 <= code <= 256:
            raise DelegationRefused("guest_response_verification_invalid", "returncode")
        for key in ("stdout_sha256", "stderr_sha256"):
            digest = item[key]
            if not isinstance(digest, str) or not wrf.is_sha256_hex(digest):
                raise DelegationRefused("guest_response_verification_invalid", "digest")
        duration = item["duration_seconds"]
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            raise DelegationRefused("guest_response_verification_invalid", "duration")
        evidence.append({"program": program, "returncode": code,
                         "stdout_sha256": item["stdout_sha256"],
                         "stderr_sha256": item["stderr_sha256"],
                         "duration_seconds": round(float(duration), 6)})
    return evidence


def write_job_outputs(job_dir: Any, guest: Mapping[str, Any] | None) -> dict[str, str]:
    """Persist the delegated result beside the queue receipt, owner-only.

    The POSIX harness writes ``harness.stdout``/``harness.stderr`` into the job
    directory and the receipt records their hashes. This is the same contract
    for the same reason: the caller needs the actual answer, and a receipt full
    of hashes with the content thrown away is a record of work nobody can use.
    """

    written: dict[str, str] = {}
    if job_dir is None or guest is None:
        return written
    for name, key in (("guest.stdout", "stdout"), ("guest.stderr", "stderr"),
                      ("guest.diff", "diff")):
        payload = guest.get(key) or b""
        if not payload:
            continue
        target = Path(job_dir) / name
        target.write_bytes(payload)
        try:
            os.chmod(target, 0o600)
        except OSError:
            # A platform without POSIX modes still gets the file; the queue
            # root's own ACL is what protects it there.
            pass
        written[name] = str(target)
    return written


def build_outcome(result: wr.RuntimeResult,
                  guest: Mapping[str, Any] | None,
                  *, job_dir: Any = None) -> dict[str, Any]:
    """The mapping the queue records, with the real result in it.

    Hashes and byte counts are kept because they are what a later reader uses
    to tell whether a stored file still matches the run. They are no longer
    the *only* thing kept: the decoded output and patch are written next to the
    receipt and named here, so a delegated job returns an answer rather than a
    fingerprint of one.
    """

    outcome: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "runtime": "windows_wsl2",
        "returncode": result.exit_code if result.ok else 1,
        # The queue's outcome contract, spoken exactly. See
        # :func:`agent_bridge.orchestration.execution_queue.validate_outcome`.
        "harness_ok": False,
        "harness_status": guest_runner.HARNESS_ABORTED,
        "harness_verdict": "runtime_aborted",
        "status": result.status,
        "reason": result.reason,
        "detail": wr.sanitize_detail(result.detail),
        "canaries_passed": result.canaries_passed,
        "spawned": result.spawned,
        "cleanup": result.cleanup.as_dict(),
        "timings": {key: round(float(value), 6)
                    for key, value in sorted(result.timings.items())},
    }
    if guest is None:
        outcome.update(
            stdout_sha256=hashlib.sha256(result.stdout).hexdigest(),
            stderr_sha256=hashlib.sha256(result.stderr).hexdigest(),
            stdout_bytes=len(result.stdout),
            stderr_bytes=len(result.stderr))
        return outcome

    stdout, stderr = guest["stdout"], guest["stderr"]
    diff = guest["diff"]
    outcome.update(
        stdout_sha256=hashlib.sha256(stdout).hexdigest(),
        stderr_sha256=hashlib.sha256(stderr).hexdigest(),
        diff_sha256=hashlib.sha256(diff).hexdigest(),
        stdout_bytes=len(stdout), stderr_bytes=len(stderr),
        diff_bytes=len(diff),
        guest={
            "status": guest["status"],
            "harness_status": guest["harness_status"],
            "reason": guest["reason"],
            "exit_code": guest["exit_code"],
            "truncated": guest["truncated"],
            "duration_seconds": guest["duration_seconds"],
            "verification": guest["verification"],
        },
        files=write_job_outputs(job_dir, guest),
    )
    # One semantic schema, not two. The guest already speaks the queue's
    # harness vocabulary, so the translation is an assignment rather than a
    # mapping from one notion of success to another. A zero exit is not a
    # verified task on either side of the boundary.
    harness_status = guest["harness_status"]
    outcome["harness_status"] = harness_status
    outcome["harness_ok"] = harness_status == guest_runner.HARNESS_COMPLETE
    outcome["harness_verdict"] = "guest_receipt"
    outcome["returncode"] = 0 if outcome["harness_ok"] else 1
    return outcome


def _refusal_outcome(reason: str, detail: str, *,
                     verdict: str = "refused") -> dict[str, Any]:
    """An outcome for a job that never ran. Still the full contract."""

    return {
        "schema_version": SCHEMA_VERSION, "runtime": "windows_wsl2",
        "returncode": 1, "status": wr.STATUS_ABORTED,
        "harness_ok": False, "harness_status": guest_runner.HARNESS_ABORTED,
        "harness_verdict": verdict,
        "reason": reason, "detail": detail,
        "spawned": False, "canaries_passed": False,
    }


def _no_auth_source(provider: str) -> Mapping[str, str] | None:
    """The default: there is no session, and that is stated rather than guessed.

    An executor constructed without an explicit source must refuse by name.
    Returning ``None`` instead would turn a host that was never configured
    into a guest-side ``auth_required``, which reads like a provider problem.
    """

    raise DelegationRefused("provider_session_unavailable", wr._token(provider))


def enrolled_auth_source(runtime_root: Any, *, lane: wev.ProviderLane | None = None,
                         protector: Any = None, platform: Any = None) -> Any:
    """A session source backed by this user's OS-protected enrolment.

    The lane check is repeated here even though :meth:`WindowsWslExecutor.refusal`
    already made it. This is the function that decrypts a credential, and the
    condition under which it may do so should be checked by the code doing it,
    not inherited from a caller that might change.
    """

    def source(provider: str) -> Mapping[str, str] | None:
        name = wr._token(provider)
        if lane is not None and not lane.enabled_for(str(provider)):
            raise DelegationRefused("provider_lane_unverified", name)
        try:
            return wa.load_capsule(runtime_root, str(provider),
                                   protector=protector, platform=platform)
        except wa.AuthError as exc:
            # The reason is one of this module's fixed tokens, so it can be
            # recorded without carrying anything about the credential.
            raise DelegationRefused("provider_session_unavailable",
                                    f"{name}:{exc.reason}") from None

    return source


class WindowsWslExecutor:
    """The dispatch seam. Refuses before it spawns, never after.

    ``provision_state`` is supplied by the caller rather than collected here:
    the installer already observes it, and re-deriving it at dispatch time
    would mean a second, differently-shaped answer to the one question that
    decides whether delegation is on.
    """

    def __init__(self, config: DelegationConfig, *,
                 provision_state: wp.ProvisionState | None = None,
                 provider_lane: wev.ProviderLane | None = None,
                 evidence_reason: str = "",
                 platform_name: str | None = None,
                 machine: str | None = None,
                 auth_source: Any = None,
                 run_job: Any = None):
        self.config = config
        self.provision_state = provision_state
        # No lane unless a live record says otherwise. Constructing this class
        # without one must never be the permissive case.
        self.provider_lane = (provider_lane if provider_lane is not None
                              else wev.ProviderLane())
        self.evidence_reason = evidence_reason
        self.platform_name = platform_name
        self.machine = machine
        # Where a provider session comes from. There is no default that
        # produces one: host-side subscription sourcing is not built, and a
        # default that silently returned None would turn a missing capsule
        # into a guest-side "auth_required" instead of a named host refusal.
        self._auth_source = auth_source if auth_source is not None else _no_auth_source
        self._run_job = run_job if run_job is not None else wr.run_job

    # -- gates --------------------------------------------------------------

    def refusal(self, request: Mapping[str, Any]) -> str:
        """The first reason this request may not run, or "".

        Ordered cheapest and most fundamental first, so a machine that is not
        Windows never reads a manifest and a machine with no verified boundary
        never hashes a rootfs.
        """

        from .windows_preflight import is_windows

        if not is_windows(self.platform_name):
            return "unsupported_platform"
        if self.provision_state is None:
            # Named, not generic: the caller loaded a verification record and
            # it was absent, malformed, about another machine, or about an
            # image that has since changed.
            return "provisioning_unverified:" + (self.evidence_reason
                                                 or "evidence_absent")
        if not wp.delegation_may_be_enabled(self.provision_state):
            return "provisioning_incomplete:" + wp.current_stage(self.provision_state)

        classification_refusal = classification_allowed(
            request.get("classification"), self.config,
            approvals=request.get("approvals") if isinstance(
                request.get("approvals"), Mapping) else None)
        if classification_refusal:
            return classification_refusal

        provider = request.get("provider")
        if provider not in PROVIDER_TOOLS:
            # The queue submits provider work. There is no tool-shaped
            # production request, and accepting one here would be a second,
            # test-only entry point into the same boundary.
            return "provider_not_supported"
        if not self.provider_lane.enabled_for(str(provider)):
            # Not "impossible": unproven. The handoff mechanism exists and is
            # implemented; what has never been observed is whether a
            # host-minted session is accepted inside the guest and how a
            # mid-job expiry behaves. Until a live record says both were seen,
            # the lane stays off.
            return "provider_lane_unverified"
        return ""

    def verify_image(self) -> wrf.ImageCheck:
        """Re-check the pinned image against its manifest at dispatch time.

        Checked here as well as at install time because the file can change in
        between, and this is the last point before it is copied and imported.
        """

        manifest = load_manifest(self.config.manifest_path)
        sidecar = load_sidecar(self.config.sidecar_path)
        try:
            observed = wrf.sha256_path(self.config.rootfs_path)
        except OSError as exc:
            raise DelegationRefused("rootfs_unreadable", type(exc).__name__) from exc
        return wrf.verify_image(
            manifest={
                "schema_version": manifest.schema_version,
                "distro_release": manifest.distro_release,
                "rootfs_sha256": manifest.rootfs_sha256,
                "node_version": manifest.node_version,
                "claude_version": manifest.claude_version,
                "codex_version": manifest.codex_version,
            },
            observed_sha256=observed,
            sidecar=sidecar,
            host_architecture=host_architecture(self.machine))

    # -- dispatch -----------------------------------------------------------

    def translate(self, request: Mapping[str, Any]
                  ) -> tuple[wr.JobRequest, str]:
        """Turn one admitted queue request into one runtime job and its base.

        Returns the job and the resolved base commit. The base is returned
        rather than stored on the executor because one executor serves many
        jobs, and instance state would let one job's base reach another job's
        receipt.

        Every value comes from the stored request: the provider, the model and
        effort the submitter chose, the brief file the queue hashed at
        admission, the verification commands it recorded, and the repository
        it checked. Nothing is read from a field that exists only for a test.
        """

        check = self.verify_image()
        if not check.passed:
            raise DelegationRefused("image_unverified", check.reason)
        manifest = load_manifest(self.config.manifest_path)
        provider = str(request["provider"])

        # The repository has to be there before anything is asked about it,
        # so a missing one says so rather than surfacing as an unresolvable
        # base.
        repo = str(request["repo"])
        if not Path(repo).is_dir():
            raise DelegationRefused("workspace_unreadable", "repo_absent")

        # The base next, because it decides whether packing the working tree
        # means anything at all. See BASE_SEMANTICS.
        base_sha = resolve_base(repo, request.get("base"))

        # The digest the queue admitted, not an optional extra. A request that
        # reached here without one did not come through admission, and this
        # lane will not read a brief it cannot bind to one.
        expected = request.get("brief_sha256")
        if not isinstance(expected, str) or len(expected) != 64:
            raise DelegationRefused("brief_unverified")

        workspace, _summary = pack_workspace(str(request["repo"]))
        return build_provider_job_request(
            config=self.config, manifest=manifest, provider=provider,
            model=str(request.get("model") or "default"),
            effort=str(request.get("effort") or "low"),
            brief=read_brief(str(request["brief"]), expected_sha256=expected),
            verify_argv=[list(command)
                         for command in request.get("verify_argv") or []],
            workspace_tar_b64=workspace,
            auth=self._auth_source(provider)), base_sha

    def __call__(self, request: Mapping[str, Any],
                 job_dir: Any = None) -> dict[str, Any]:
        refusal = self.refusal(request)
        if refusal:
            return _refusal_outcome("refused", refusal)

        try:
            job_request, base_sha = self.translate(request)
        except DelegationRefused as exc:
            return _refusal_outcome(exc.reason, exc.detail,
                                    verdict="translation_refused")
        except (KeyError, TypeError, ValueError) as exc:
            # A request missing a field the queue is supposed to guarantee.
            return _refusal_outcome("request_malformed", type(exc).__name__,
                                    verdict="translation_refused")

        result = self._run_job(job_request, platform_name=self.platform_name)
        guest: Mapping[str, Any] | None = None
        if result.ok:
            try:
                guest = parse_guest_response(result.stdout)
            except DelegationRefused as exc:
                outcome = build_outcome(result, None)
                outcome.update(returncode=1, status=wr.STATUS_ABORTED,
                               reason=exc.reason, detail=exc.detail,
                               harness_verdict="guest_response_rejected",
                               base_sha=base_sha)
                return outcome
        outcome = build_outcome(result, guest, job_dir=job_dir)
        # The commit the patch is relative to, on the receipt. Without it the
        # patch names files and nothing names the tree they came from.
        outcome["base_sha"] = base_sha
        return outcome


def verified_executor(config: DelegationConfig, *,
                     fingerprint: str | None = None,
                     platform_name: str | None = None,
                     machine: str | None = None,
                     auth_source: Any = None,
                     run_job: Any = None) -> WindowsWslExecutor:
    """Build the executor from what this machine can actually prove.

    This is the production constructor. It reads the verification record in
    the configured runtime root and binds it to the artefacts that are on disk
    right now: the manifest's pinned rootfs hash and the hash of the guest
    runner this process would ship. If any of that fails the executor is still
    built, because an executor that refuses by name is far more useful to an
    operator than an import error, but it is built with no provisioning state
    and no provider lane, so it refuses everything.
    """

    reason = ""
    try:
        rootfs_sha256 = load_manifest(config.manifest_path).rootfs_sha256
    except DelegationRefused as exc:
        rootfs_sha256, reason = "", exc.reason
    state: wp.ProvisionState | None = None
    lane: wev.ProviderLane | None = None
    if rootfs_sha256:
        state, evidence, reason = wev.load_verified_state(
            wev.evidence_path(config.runtime_root),
            expected_rootfs_sha256=rootfs_sha256,
            expected_runner_sha256=guest_runner_sha256(),
            required_canaries=wr.CANARY_ORDER,
            fingerprint=fingerprint, runtime_root=config.runtime_root)
        if evidence is None:
            state = None
        else:
            lane = evidence.provider_lane
    if auth_source is None:
        # The production source: this user's own enrolled session, decrypted
        # per job and never written down. Bound to the lane that was just
        # loaded, so a closed lane cannot reach a decryption call at all.
        auth_source = enrolled_auth_source(config.runtime_root, lane=lane)
    return WindowsWslExecutor(config, provision_state=state, provider_lane=lane,
                              evidence_reason=reason, platform_name=platform_name,
                              machine=machine, auth_source=auth_source,
                              run_job=run_job)


# ---------------------------------------------------------------------------
# The blocker, stated exactly
# ---------------------------------------------------------------------------

PROVIDER_EXECUTION_BLOCKER = (
    "Provider execution is implemented but not enabled, and the distinction "
    "matters. Claude Code and Codex CLI authenticate from a subscription "
    "session, and both accept one without an API key and without a persistent "
    "credential store in the image: Claude Code reads CLAUDE_CODE_OAUTH_TOKEN "
    "(minted on the host by `claude setup-token`) alongside CLAUDE_CONFIG_DIR, "
    "and Codex CLI accepts `codex login --with-access-token` on stdin with "
    "CODEX_HOME pointing at per-job storage. The guest therefore receives a "
    "bounded, memory-only auth capsule on a private tmpfs, unmounted in a "
    "finally block and redacted out of every output, never an API key and "
    "never a copy of the host credential store. Two things have not been "
    "observed on a live host: portability, that a session minted on the host "
    "is actually accepted from inside an ephemeral guest, and refresh, how a "
    "session that expires mid-job behaves when the capsule is discarded and "
    "nothing is written back. Until a verification record on the machine "
    "itself reports both, provider jobs are refused as "
    "provider_lane_unverified."
)

NO_LIVE_VALIDATION = (
    "no job has been dispatched through this executor on a live Windows host "
    "from this worktree; the tests are mocked and run on the development "
    "machine, so nothing here is evidence that delegation works end to end"
)
