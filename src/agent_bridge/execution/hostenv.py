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
backend rather than one hard-wired binary. Nothing here relaxes a boundary:
a host with no supported confinement for the requested material is refused
by name, never run unconfined.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: The single confinement backend that has been independently verified.
MACOS_SANDBOX_EXEC = "macos-sandbox-exec"
#: Network denial through a private network namespace, and nothing more. Not
#: equivalent to the macOS backend and never selected for material that is
#: not synthetic; see ``confinement``.
LINUX_NETNS_SYNTHETIC = "linux-netns-synthetic-only"

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


def _netns_usable() -> bool:
    """Whether an unprivileged private network namespace can be entered here.

    Probed, not inferred from a kernel version or a sysctl name: user
    namespaces are disabled on some hosts and restricted on others, and the
    only answer that matters is whether the command this lane would run
    actually starts.
    """
    if not (os.path.isfile(UNSHARE) and os.access(UNSHARE, os.X_OK)):
        return False
    try:
        probe = subprocess.run(
            [UNSHARE, "--net", "--map-root-user", "/bin/true"],
            capture_output=True, timeout=15, check=False, shell=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


@dataclass(frozen=True)
class Confinement:
    """The verification confinement this host will actually apply.

    ``name`` goes into the receipt beside every verification command, so
    evidence never leaves it ambiguous which boundary ran. ``confines_reads``
    and ``confines_writes`` are recorded rather than assumed: the Linux
    backend denies network and nothing else, and a report that implied
    otherwise would be the overclaim this module exists to prevent.
    """

    name: str
    denies_network: bool
    confines_reads: bool
    confines_writes: bool
    #: Classifications this backend may be used for. The Linux backend is
    #: restricted to invented material because it is not a read boundary.
    classifications: frozenset[str]

    def permits(self, classification: str) -> bool:
        return classification in self.classifications


MACOS_CONFINEMENT = Confinement(
    name=MACOS_SANDBOX_EXEC, denies_network=True, confines_reads=True,
    confines_writes=True,
    classifications=frozenset({"synthetic", "public", "internal_nonclient"}))

LINUX_CONFINEMENT = Confinement(
    name=LINUX_NETNS_SYNTHETIC, denies_network=True, confines_reads=False,
    confines_writes=False, classifications=frozenset({"synthetic"}))


def confinement(classification: str, *, system: str | None = None) -> Confinement:
    """The backend for ``classification`` on this host, or a named refusal.

    macOS with ``sandbox-exec`` is the verified backend and carries every
    allowed classification. Linux with a usable private network namespace
    carries ``synthetic`` only: it denies network access, and the disposable
    worktree plus the harness's own post-run content snapshot of the source
    repository is what bounds writes, but it confines no reads. Declared
    non-client internal material and public material therefore still refuse
    there rather than running under a weaker boundary than the one they were
    promised.

    Windows is not served here. Its lane runs verification inside the WSL
    guest (``orchestration/guest_runner.py``), which is a different
    mechanism with its own evidence.
    """
    name = system or platform.system()
    if name == "Darwin" and os.path.isfile(SANDBOX_EXEC):
        backend = MACOS_CONFINEMENT
    elif name == "Linux" and _netns_usable():
        backend = LINUX_CONFINEMENT
    else:
        missing = {"Darwin": f"{SANDBOX_EXEC} is absent",
                   "Linux": f"{UNSHARE} cannot enter a private network namespace here"}.get(
                       name, f"no verification confinement backend exists for {name}")
        raise HostCapabilityError(
            "verification_confinement_unavailable",
            f"this host cannot confine verification commands: {missing}")
    if not backend.permits(classification):
        raise HostCapabilityError(
            "verification_confinement_insufficient",
            f"the {backend.name} backend confines no reads, so it carries only "
            f"synthetic material; {classification!r} needs a host with full "
            f"confinement")
    return backend


def netns_argv(command: list[str]) -> list[str]:
    """Wrap ``command`` so it runs with no network. Nothing else is changed."""
    return [UNSHARE, "--net", "--map-root-user", "--", *command]


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
    return report
