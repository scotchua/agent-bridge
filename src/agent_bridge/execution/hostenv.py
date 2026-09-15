"""Host capability resolution for the two bounded implementation lanes.

Both lanes used to hard-code one macOS layout: ``git`` at the standalone
Command Line Tools path, and ``/usr/bin/sandbox-exec`` as the only possible
verification confinement. Two consequences followed, and both were observed
rather than theorised:

1. On any host that is not macOS, ``run_task`` refused the whole lane before
   doing anything, even though only the *verification* step needs a sandbox.
   Generation, patch capture and independent re-application need git, not
   confinement.
2. On a Mac that passes the platform assert but has no standalone Command
   Line Tools (git supplied by Xcode.app or a package manager instead), the
   first git call spawned a path that does not exist and the lane reported
   ``TaskError: command spawn failed``. No program, no path, no cause. That
   string is not diagnostic evidence, and an operator reading a receipt
   containing it cannot tell it from a failed login or a killed process.

This module replaces both constants with a probe that names what it looked
for and what it found, and it makes the verification confinement a selected
backend rather than one hard-wired binary.

**A backend proves itself before it is offered.** An earlier version of this
file shipped a Linux backend that denied network access and nothing else,
restricted it to invented material, and documented that writes were bounded
only by the disposable worktree and the harness's own post-run snapshot of
the source repository. An adversarial review was right to reject that: the
snapshot covers the source repository, so a write to ``$HOME``, to another
checkout, or to the bridge's own state root passed entirely unnoticed. And
because reads were unconfined while the worktree becomes the returned patch,
a secret read from the home directory could be copied into the patch. That is
an exfiltration channel with the network already closed. Documenting a hole
is not mitigating it.

So the Linux backend now confines writes with a mount namespace, and
``_selftest_argv`` runs the real confinement against canary paths on every
probe: the backend is offered only if a write outside the worktree actually
fails on this host, right now. Reads remain unconfined, which is why it still
carries invented material only.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

#: The confinement backend that has been independently verified.
MACOS_SANDBOX_EXEC = "macos-sandbox-exec"
#: Linux: a private mount namespace in which the filesystem is read-only
#: except the worktree and scratch, plus a private network namespace. Confines
#: writes and denies network; confines no reads, so it carries synthetic
#: material only. Not independently reviewed.
LINUX_USERNS = "linux-userns-write-confined"

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
UNSHARE = "/usr/bin/unshare"

#: Looked at in order, then ``PATH``. The Command Line Tools path stays first
#: on macOS because it is the layout the lane's behaviour was measured
#: against; it is no longer the only one accepted.
GIT_CANDIDATES: dict[str, tuple[str, ...]] = {
    "Darwin": ("/Library/Developer/CommandLineTools/usr/bin/git",
               "/Applications/Xcode.app/Contents/Developer/usr/bin/git",
               "/opt/homebrew/bin/git", "/usr/local/bin/git", "/usr/bin/git"),
    "Linux": ("/usr/bin/git", "/usr/local/bin/git", "/bin/git"),
    "Windows": (),
}

#: Exit status the confinement helper uses when its own self-check fails. A
#: distinct value so a failed boundary is never mistaken for a failed command.
CONFINEMENT_SELFTEST_EXIT = 121


class HostCapabilityError(RuntimeError):
    """A host requirement is absent, named, and not worked around.

    ``code`` is a fixed vocabulary member so a receipt can be read back to a
    rule. ``detail`` is assembled only from this module's own text plus
    filesystem paths and platform names, never from a subprocess's output, so
    it is safe to persist in an operator-visible receipt.
    """

    def __init__(self, code: str, detail: str):
        super().__init__(f"{detail} [{code}]")
        self.code = code
        self.detail = detail


def resolve_git(env: dict[str, str] | None = None,
                *, system: str | None = None) -> Path:
    """The git this host actually has, or a refusal naming every path tried.

    Resolution is explicit rather than a bare ``which``: a lane that shells
    out to whatever is first on an inherited ``PATH`` is a lane whose
    behaviour depends on the caller's shell. The fixed candidates come first
    and ``PATH`` is the fallback, so a normal installation works and an
    unusual one is still found.
    """
    name = system or platform.system()
    tried: list[str] = []
    for candidate in GIT_CANDIDATES.get(name, ()):
        tried.append(candidate)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return Path(candidate)
    search_path = (env or {}).get("PATH") or os.environ.get("PATH", "")
    found = shutil.which("git", path=search_path)
    if found and os.path.isfile(found):
        return Path(found)
    tried.append(f"PATH={search_path or '(empty)'}")
    raise HostCapabilityError(
        "git_unavailable",
        "no usable git executable on this host; tried " + ", ".join(tried))


#: The program that establishes the Linux write boundary and then proves it.
#:
#: Run as mapped root inside a fresh user, mount and network namespace. It
#: takes every mount point from ``/proc/self/mountinfo`` and remounts it
#: read-only, deepest first, then re-opens exactly the worktree and the
#: scratch directory for writing. Enumerating mountinfo rather than remounting
#: ``/`` alone is deliberate: a remount of ``/`` does not touch a separately
#: mounted ``/tmp``, ``/dev/shm``, ``/home`` or ``/run``, and on a host where
#: any of those is its own mount the boundary would have had a hole in it.
#:
#: Then it checks its own work. Every canary path handed to it must be
#: unwritable and the worktree must be writable, or it exits
#: ``CONFINEMENT_SELFTEST_EXIT`` without running the command at all. A
#: boundary that is asserted and not measured is the defect this replaces, so
#: this one refuses to run work it cannot first show is confined.
_HELPER = r'''
import ctypes, os, struct, sys

MS_RDONLY, MS_REMOUNT, MS_BIND, MS_REC, MS_PRIVATE = 1, 32, 4096, 16384, 1 << 18
libc = ctypes.CDLL(None, use_errno=True)
libc.mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
                       ctypes.c_ulong, ctypes.c_void_p]


def mount(source, target, flags):
    encoded = None if source is None else source.encode("utf-8", "surrogateescape")
    if libc.mount(encoded, target.encode("utf-8", "surrogateescape"), None,
                  flags, None) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno), target)


def mount_points():
    """Every mount point, deepest first, from mountinfo (field 5, octal-escaped)."""
    points = []
    with open("/proc/self/mountinfo", "rb") as handle:
        for line in handle:
            fields = line.split(b" ")
            if len(fields) < 5:
                continue
            raw = fields[4].decode("utf-8", "surrogateescape")
            for escape, literal in (("\\040", " "), ("\\011", "\t"),
                                    ("\\012", "\n"), ("\\134", "\\")):
                raw = raw.replace(escape, literal)
            points.append(raw)
    points.sort(key=lambda path: path.count(os.sep), reverse=True)
    return points


def writable(path):
    probe = os.path.join(path, ".agent-bridge-confinement-probe")
    try:
        descriptor = os.open(probe, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
    except OSError:
        return False
    os.close(descriptor)
    os.unlink(probe)
    return True


separator = sys.argv.index("--", 1)
writable_roots = sys.argv[1:separator]
rest = sys.argv[separator + 1:]
separator = rest.index("--")
canaries, command = rest[:separator], rest[separator + 1:]

mount(None, "/", MS_REC | MS_PRIVATE)

# Order matters, and getting it wrong fails closed rather than open, which is
# how it was caught: the kernel refuses to remount a bind read-write when its
# source is already read-only. Making the filesystem read-only first and then
# trying to re-open the worktree therefore left the worktree unwritable and
# every verification command failing. So bind the two writable trees FIRST,
# while everything is still writable, and only then seal the rest. Each bind
# is its own mount entry, so sealing its parent afterwards does not reach it.
for root in writable_roots:
    if not os.path.isdir(root):
        # Named, not a traceback. An absent worktree or scratch directory is
        # a caller mistake, and "FileNotFoundError" from inside a mount
        # helper is exactly the opaque diagnostic this work exists to remove.
        sys.stderr.write("confinement setup failed: %s is not a directory\n" % root)
        raise SystemExit(SELFTEST_EXIT)
    mount(root, root, MS_BIND | MS_REC)

sealed = {os.path.realpath(root) for root in writable_roots}
for point in mount_points():
    if os.path.realpath(point) in sealed:
        continue
    try:
        mount(point, point, MS_REMOUNT | MS_BIND | MS_RDONLY)
    except OSError:
        # A pseudo-filesystem that refuses a read-only remount is not a hole
        # the canary check will miss: it is checked below like everything else.
        pass

# The command inherits its working directory from before the unshare, which
# is a handle on the OLD mount. The path now resolves to the writable bind,
# but that stale handle still points at the sealed one, so a command using a
# relative path got EROFS while an absolute path to the same file worked.
# Re-enter the worktree by path so both agree. The first writable root is the
# working directory by contract.
os.chdir(writable_roots[0])

# Drop every capability before running the payload. Without this the boundary
# is decorative: --map-root-user makes the payload root in this namespace with
# CAP_SYS_ADMIN over it, so it can simply remount / read-write and write
# anywhere. Measured, not assumed: a plain ctypes mount() remount from inside
# returned 0 until these three steps were added, and the remount check below
# is what keeps that honest.
libc.prctl(38, 1, 0, 0, 0)                      # PR_SET_NO_NEW_PRIVS
for capability in range(64):
    libc.prctl(24, capability, 0, 0, 0)         # PR_CAPBSET_DROP, best effort
header = ctypes.create_string_buffer(struct.pack("Ii", 0x20080522, 0))
cleared = ctypes.create_string_buffer(struct.pack("6I", 0, 0, 0, 0, 0, 0))
libc.capset(header, cleared)

# Now prove all three properties of the boundary, and run nothing if any of
# them does not hold. A boundary that is asserted rather than measured is the
# defect this file exists to replace.
if libc.mount(b"/", b"/", None, MS_REMOUNT | MS_BIND, None) == 0:
    sys.stderr.write("confinement self-check failed: / could be remounted read-write\n")
    raise SystemExit(SELFTEST_EXIT)
for canary in canaries:
    if writable(canary):
        sys.stderr.write("confinement self-check failed: %s is writable\n" % canary)
        raise SystemExit(SELFTEST_EXIT)
for root in writable_roots:
    if not writable(root):
        sys.stderr.write("confinement self-check failed: %s is not writable\n" % root)
        raise SystemExit(SELFTEST_EXIT)

# execvp, not execv: a verification command is an allowlisted bare program
# name ("pytest", "python3"), so it has to be resolved on PATH. execv treated
# the name as a path and the command never ran at all.
os.execvp(command[0], command)
'''


def helper_source() -> str:
    """The helper with its exit status compiled in rather than interpolated."""
    return _HELPER.replace("SELFTEST_EXIT", str(CONFINEMENT_SELFTEST_EXIT))


def confined_argv(command: list[str], *, tree: Path, scratch: Path,
                  canaries: list[str], python: str | None = None) -> list[str]:
    """``command`` wrapped so it has no network and can write only two trees.

    No shell anywhere: the paths travel as argv to a Python helper, so a
    worktree path containing a space, a quote or a newline is just an
    argument. An earlier sketch of this used ``sh -c`` with the paths
    interpolated, which would have been an injection hole on exactly the
    repositories most likely to expose it.

    ``tree`` is the command's working directory: the helper chdirs into it
    after mounting, because a working directory inherited from before the
    namespace is a handle on the sealed mount rather than the writable bind.
    """
    interpreter = python or os.path.realpath(sys.executable)
    return [UNSHARE, "--map-root-user", "--mount", "--net",
            "--propagation", "private", "--",
            interpreter, "-c", helper_source(),
            str(tree), str(scratch), "--", *canaries, "--", *command]


def _selftest_argv(temporary: str, python: str | None = None) -> list[str]:
    """The probe: build the real confinement and let it check itself."""
    tree = os.path.join(temporary, "tree")
    scratch = os.path.join(temporary, "scratch")
    canary = os.path.join(temporary, "canary")
    for path in (tree, scratch, canary):
        os.makedirs(path, exist_ok=True)
    # The canary set is deliberately broader than the temporary directory.
    # A boundary that holds for one temp path and not for the home directory
    # is not a boundary, and this is where that gets caught.
    canaries = [canary, str(Path.home()), tempfile.gettempdir()]
    for extra in ("/dev/shm", "/run", "/var/tmp"):
        if os.path.isdir(extra):
            canaries.append(extra)
    return confined_argv(["/bin/true"], tree=Path(tree), scratch=Path(scratch),
                         canaries=canaries, python=python)


def _userns_usable(python: str | None = None) -> bool:
    """Whether this host can actually confine a command, measured not inferred.

    Runs the real helper. It returns True only when the namespace was entered,
    every mount succeeded, and the helper's own canary checks passed, so a
    host where unprivileged user namespaces are disabled, or where the mount
    flags do not take, reports no backend rather than a weak one.
    """
    if not (os.path.isfile(UNSHARE) and os.access(UNSHARE, os.X_OK)):
        return False
    try:
        with tempfile.TemporaryDirectory() as temporary:
            probe = subprocess.run(_selftest_argv(temporary, python),
                                   capture_output=True, timeout=30,
                                   check=False, shell=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


@dataclass(frozen=True)
class Confinement:
    """The verification confinement this host will actually apply.

    ``name`` goes into the receipt beside every verification command, so
    evidence never leaves it ambiguous which boundary ran. The three boolean
    fields are recorded rather than assumed: the Linux backend confines writes
    and denies network but confines no reads, and a report that implied
    otherwise would be the overclaim this module exists to prevent.
    """

    name: str
    denies_network: bool
    confines_reads: bool
    confines_writes: bool
    #: Classifications this backend may be used for. The Linux backend is
    #: restricted to invented material because it is not a read boundary.
    classifications: frozenset[str]
    #: Whether an independent review has examined the boundary itself.
    independently_verified: bool = False

    def permits(self, classification: str) -> bool:
        return classification in self.classifications


MACOS_CONFINEMENT = Confinement(
    name=MACOS_SANDBOX_EXEC, denies_network=True, confines_reads=True,
    confines_writes=True, independently_verified=True,
    classifications=frozenset({"synthetic", "public", "internal_nonclient"}))

LINUX_CONFINEMENT = Confinement(
    name=LINUX_USERNS, denies_network=True, confines_reads=False,
    confines_writes=True, independently_verified=False,
    classifications=frozenset({"synthetic"}))


def confinement(classification: str, *, system: str | None = None) -> Confinement:
    """The backend for ``classification`` on this host, or a named refusal.

    macOS with ``sandbox-exec`` is the verified backend and carries every
    allowed classification. Linux carries ``synthetic`` only, and only when
    the mount-namespace boundary proves itself on this host: it denies network
    access and confines writes to the worktree and scratch, but it confines no
    reads. Because the worktree becomes the returned patch, unconfined reads
    are an exfiltration channel for anything the account can read, so declared
    non-client internal material and public material refuse there rather than
    running under a weaker boundary than the one they were promised.

    Windows is not served here. Its lane runs verification inside the WSL
    guest (``orchestration/guest_runner.py``), which is a different mechanism
    with its own evidence.
    """
    name = system or platform.system()
    if name == "Darwin" and os.path.isfile(SANDBOX_EXEC):
        backend = MACOS_CONFINEMENT
    elif name == "Linux" and _userns_usable():
        backend = LINUX_CONFINEMENT
    else:
        missing = {
            "Darwin": f"{SANDBOX_EXEC} is absent",
            "Linux": (f"{UNSHARE} cannot establish a write-confined mount and "
                      f"network namespace here, or the boundary failed its own "
                      f"canary check"),
        }.get(name, f"no verification confinement backend exists for {name}")
        raise HostCapabilityError(
            "verification_confinement_unavailable",
            f"this host cannot confine verification commands: {missing}")
    if not backend.permits(classification):
        raise HostCapabilityError(
            "verification_confinement_insufficient",
            f"the {backend.name} backend confines no reads, and the worktree "
            f"becomes the returned patch, so it carries only synthetic "
            f"material; {classification!r} needs a host with full confinement")
    return backend


def describe(classification: str, *, system: str | None = None) -> dict[str, object]:
    """What this host can and cannot do, for a preflight or a status report.

    Never raises: a caller asking what is supported wants the answer, not an
    exception. The refusal codes appear in the result instead.
    """
    name = system or platform.system()
    report: dict[str, object] = {"platform": name, "classification": classification}
    try:
        report["git"] = str(resolve_git(system=name))
    except HostCapabilityError as exc:
        report["git"] = None
        report["git_refusal"] = exc.code
    try:
        backend = confinement(classification, system=name)
    except HostCapabilityError as exc:
        report["confinement"] = None
        report["confinement_refusal"] = exc.code
        report["confinement_detail"] = exc.detail
    else:
        report["confinement"] = backend.name
        report["denies_network"] = backend.denies_network
        report["confines_reads"] = backend.confines_reads
        report["confines_writes"] = backend.confines_writes
        report["independently_verified"] = backend.independently_verified
    return report
