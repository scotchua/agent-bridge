"""A deterministic validation pass over a provisioned Windows host.

What this is for
----------------

Everything in the Windows lane is testable on a development machine except the
one question that matters: does it work on Windows. Answering that needs a
person at a real machine, and the thing that makes their answer worth anything
is that it is *reproducible* and *machine-readable* rather than a screenshot
and a sentence in a chat window.

So this module is one command that runs a fixed, ordered list of named checks
and prints one JSON object. Run it twice on an unchanged machine and the
verdicts are identical. Run it on a machine where something regressed and
exactly one check changes, by name.

What makes it deterministic
---------------------------

* **Fixed order, no parallelism.** The checks are a tuple, run front to back.
  A check that depends on an earlier one says so, and is reported ``blocked``
  rather than run, so a cascade produces one failure and a list of things that
  were never attempted.
* **No wall-clock in the verdict.** Timings and timestamps live in a separate
  ``envelope`` key that the verdict is not computed from, so two runs of the
  same machine produce byte-identical ``checks``.
* **No ambient input.** Every command goes through an injected runner, and
  nothing reads the process environment.
* **A closed vocabulary.** :data:`STATUSES` is the whole set. A check function
  that returns anything else is a ``fail`` naming the check, not a default.

What it deliberately does not do
--------------------------------

It does not record boundary verification. :mod:`windows_evidence` owns that,
it refuses to run off Windows, and it binds its record to the machine and the
artefacts. A validation report is an input a person reads; an evidence record
is a durable claim the planner trusts. Keeping them apart is what stops a
green report from silently enabling delegation.

It also does not spend a provider session. The authenticated probe costs real
allowance and is a separate, separately consented step.

Nothing in this module has been run on a live Windows host.
"""

from __future__ import annotations

import json
import platform as platform_module
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from . import windows_privacy as wpv
from . import windows_rootfs as wrf
from . import windows_wsl as ww
from . import windows_wsl_provision as wp

SCHEMA_VERSION = 1

#: The report's top-level keys. Fixed, so a reader that finds an unexpected
#: one is reading something else.
REPORT_KEYS = frozenset({"schema_version", "verdict", "checks", "envelope",
                         "not_proven"})

# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------

#: The check ran and observed what it set out to observe.
PASS = "pass"
#: The check ran and observed the opposite.
FAIL = "fail"
#: The check did not run because something it depends on failed. Never a pass.
BLOCKED = "blocked"
#: The check does not apply to this machine, for a reason it names. Never a
#: pass either: a skipped check contributes nothing to the verdict.
SKIPPED = "skipped"

STATUSES = frozenset({PASS, FAIL, BLOCKED, SKIPPED})

#: The overall verdict. ``ready`` requires every check to have passed, which is
#: why ``blocked`` and ``skipped`` are not passes: a report full of skips would
#: otherwise be indistinguishable from a machine that worked.
VERDICT_READY = "ready"
VERDICT_NOT_READY = "not_ready"
VERDICTS = frozenset({VERDICT_READY, VERDICT_NOT_READY})


class ValidationError(ValueError):
    """A report that cannot be trusted to mean what it says."""


# ---------------------------------------------------------------------------
# One check
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    """One named observation, and an honest statement of its limits.

    ``proves`` and ``does_not_prove`` are not documentation. They are carried
    into the report, so the person reading a green run sees, next to every
    pass, the thing that pass does not establish.
    """

    #: Stable identifier. Never renamed: it is how two runs are compared.
    id: str
    title: str
    proves: str
    does_not_prove: str
    #: Check ids that must have passed first. A missing one is a programming
    #: error and is refused when the suite is built, not at run time.
    requires: tuple[str, ...] = ()


@dataclass(frozen=True)
class Observation:
    """What a check function returns. Detail is redacted before it is used."""

    status: str
    #: A short fixed token, never free text from a command. Reason codes go
    #: into durable records and into other people's terminals.
    reason: str
    #: Small, bounded, machine-readable facts. Values are numbers, booleans
    #: or short tokens; never a path, a username or command output.
    facts: Mapping[str, Any] = field(default_factory=dict)


#: What a fact value may be. Anything else is refused rather than serialised,
#: because "just this once" is how a host path ends up in a shared report.
_FACT_SCALARS = (bool, int, float, str)
MAX_FACT_CHARS = 64
_FACT_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:+-]{0,64}$")
_REASON_RE = re.compile(r"^[a-z0-9_]{1,64}$")


def validate_observation(check_id: str, observation: Any) -> Observation:
    """Refuse anything that is not an observation, by name.

    A check that returns a string, a truthy object or an unknown status is a
    bug in that check. Treating it as a pass would be the single worst failure
    mode this module has, so every departure from the contract is a ``fail``
    that names the check.
    """

    if not isinstance(observation, Observation):
        raise ValidationError(f"{check_id}: not an observation")
    if observation.status not in STATUSES:
        raise ValidationError(f"{check_id}: unknown status")
    if not _REASON_RE.match(observation.reason):
        raise ValidationError(f"{check_id}: reason is not a fixed token")
    for key, value in observation.facts.items():
        if not isinstance(key, str) or not _FACT_TOKEN_RE.match(key):
            raise ValidationError(f"{check_id}: fact key is not a token")
        if isinstance(value, bool) or isinstance(value, (int, float)):
            continue
        if isinstance(value, str) and _FACT_TOKEN_RE.match(value):
            continue
        raise ValidationError(f"{check_id}: fact {key} is not a bounded scalar")
    return observation


# ---------------------------------------------------------------------------
# The suite
# ---------------------------------------------------------------------------

#: The ordered checks. Cheapest and most fundamental first, so a machine that
#: is not Windows produces one line rather than a cascade of timeouts.
CHECKS: tuple[Check, ...] = (
    Check(
        id="platform",
        title="This is a native Windows process",
        proves="the report describes a Windows host rather than an emulation",
        does_not_prove="anything about WSL, the image, or a provider session",
    ),
    Check(
        id="windows_build",
        title="The Windows build supports WSL2 with systemd",
        proves="the host is new enough for the features the lane relies on",
        does_not_prove="that those features are enabled",
        requires=("platform",),
    ),
    Check(
        id="wsl_version",
        title="wsl.exe reports a supported version",
        proves="a WSL2 runtime is installed and answers",
        does_not_prove="that a distribution is registered or healthy",
        requires=("windows_build",),
    ),
    Check(
        id="features",
        title="The required optional features are enabled",
        proves="the platform features the guest needs are on",
        does_not_prove="that a reboot is not still pending for them",
        requires=("platform",),
    ),
    Check(
        id="reboot_clear",
        title="No restart is pending",
        proves="what the machine reports now is what it will report later",
        does_not_prove="that no later step will require one",
        requires=("platform",),
    ),
    Check(
        id="runtime_root",
        title="The runtime root is a private directory this user owns",
        proves="records written here are not readable by other users",
        does_not_prove="that an administrator cannot read them",
        requires=("platform",),
    ),
    Check(
        id="image_pinned",
        title="The installed rootfs matches its pinned manifest hash",
        proves="the image on disk is the image that was reviewed",
        does_not_prove="that the image's contents are themselves trustworthy",
        requires=("platform",),
    ),
    Check(
        id="distro_registered",
        title="The pinned distribution is registered and is WSL2",
        proves="a guest exists to run jobs in",
        does_not_prove="that the guest is contained",
        requires=("wsl_version", "image_pinned"),
    ),
    Check(
        id="guest_runner",
        title="The guest runner inside the image matches its pinned hash",
        proves="the program that will execute jobs is the reviewed one",
        does_not_prove="that it is reachable or that it runs",
        requires=("distro_registered",),
    ),
    Check(
        id="canaries",
        title="Every containment canary passes inside the guest",
        proves="no host mount, no interop, and the pinned tool versions",
        does_not_prove="that the guest cannot reach the network",
        requires=("guest_runner",),
    ),
    Check(
        id="egress_policy",
        title="The verification egress policy loads and reads back",
        proves="the deny ruleset is in the kernel during verification",
        does_not_prove="that every destination is unreachable; it is sampled",
        requires=("canaries",),
    ),
    Check(
        id="round_trip",
        title="A synthetic job runs in the guest and returns a patch",
        proves="the whole lane carries work in and evidence out",
        does_not_prove="that a provider session survives the boundary",
        requires=("egress_policy",),
    ),
)

CHECK_IDS = tuple(check.id for check in CHECKS)

#: Stated in the report itself, not only here. A green run means the boundary
#: behaved; it does not mean delegation is proven end to end.
NOT_PROVEN = (
    "a provider subscription session surviving the boundary, which costs real "
    "allowance and is a separately consented step",
    "that the guest cannot reach the network; the egress check samples "
    "destinations and reads the ruleset back, which is not the same claim",
    "that the image's contents are trustworthy; the hash proves only that the "
    "image is the one that was reviewed",
)


def _validate_suite(checks: Sequence[Check]) -> None:
    """A dependency on a check that does not exist is refused at build time."""

    seen: set[str] = set()
    for check in checks:
        if check.id in seen:
            raise ValidationError(f"duplicate check id {check.id}")
        for required in check.requires:
            if required not in seen:
                raise ValidationError(
                    f"{check.id} requires {required}, which does not run first")
        seen.add(check.id)


_validate_suite(CHECKS)


# ---------------------------------------------------------------------------
# Running one
# ---------------------------------------------------------------------------


def run_checks(runners: Mapping[str, Callable[[], Any]], *,
               checks: Sequence[Check] = CHECKS) -> list[dict[str, Any]]:
    """Run the suite in order and return one row per check.

    ``runners`` maps a check id to a zero-argument callable returning an
    :class:`Observation`. A check with no runner is ``blocked``: a suite that
    quietly skipped an unimplemented check would report a machine as ready on
    the strength of the checks somebody remembered to write.
    """

    rows: list[dict[str, Any]] = []
    passed: set[str] = set()
    for check in checks:
        missing = [name for name in check.requires if name not in passed]
        if missing:
            rows.append(_row(check, Observation(
                BLOCKED, "requirement_not_met",
                {"blocked_by": missing[0]})))
            continue
        runner = runners.get(check.id)
        if runner is None:
            rows.append(_row(check, Observation(BLOCKED, "check_not_implemented")))
            continue
        try:
            observation = validate_observation(check.id, runner())
        except ValidationError:
            # The check misbehaved. That is a failure of the check, and a
            # failure is the only safe reading of it.
            rows.append(_row(check, Observation(FAIL, "check_contract_violated")))
            continue
        except Exception:  # noqa: BLE001 - any raise is a failed observation
            # Deliberately broad, and deliberately discarding the exception:
            # the text would carry paths and command output into a report
            # people paste into issues.
            rows.append(_row(check, Observation(FAIL, "check_raised")))
            continue
        rows.append(_row(check, observation))
        if observation.status == PASS:
            passed.add(check.id)
    return rows


def _row(check: Check, observation: Observation) -> dict[str, Any]:
    return {
        "id": check.id,
        "title": check.title,
        "status": observation.status,
        "reason": observation.reason,
        "facts": dict(observation.facts),
        "proves": check.proves if observation.status == PASS else "",
        "does_not_prove": check.does_not_prove,
    }


def verdict(rows: Sequence[Mapping[str, Any]], *,
            checks: Sequence[Check] = CHECKS) -> str:
    """Ready only when every check in the suite passed.

    Computed from the suite rather than from the rows, so a report that is
    missing a row entirely is not ready either.
    """

    by_id = {row["id"]: row for row in rows}
    for check in checks:
        row = by_id.get(check.id)
        if row is None or row.get("status") != PASS:
            return VERDICT_NOT_READY
    return VERDICT_READY


def build_report(rows: Sequence[Mapping[str, Any]], *,
                 recorded_at: str | None = None,
                 checks: Sequence[Check] = CHECKS) -> dict[str, Any]:
    """The whole object. ``checks`` is deterministic; ``envelope`` is not.

    The split is the point. Two runs on an unchanged machine differ only in
    ``envelope``, so a diff of two reports is a diff of what changed about the
    machine.
    """

    return {
        "schema_version": SCHEMA_VERSION,
        "verdict": verdict(rows, checks=checks),
        "checks": [dict(row) for row in rows],
        "not_proven": list(NOT_PROVEN),
        "envelope": {
            "recorded_at": recorded_at,
            "suite": list(check.id for check in checks),
        },
    }


def parse_report(raw: Any) -> dict[str, Any]:
    """Read a report back with the same strictness it was written with."""

    if not isinstance(raw, Mapping):
        raise ValidationError("report is not an object")
    if set(raw) != REPORT_KEYS:
        raise ValidationError("report keys are not the agreed set")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise ValidationError("report schema version is not supported")
    if raw.get("verdict") not in VERDICTS:
        raise ValidationError("report verdict is not a known verdict")
    rows = raw.get("checks")
    if not isinstance(rows, list):
        raise ValidationError("report checks is not a list")
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValidationError("report check is not an object")
        if row.get("status") not in STATUSES:
            raise ValidationError("report check status is not a known status")
    # The verdict is recomputed rather than believed. A report is a file, and
    # a file can be edited between the machine that wrote it and the person
    # reading it.
    if verdict(rows) != raw["verdict"]:
        raise ValidationError("report verdict does not match its own checks")
    return dict(raw)


def render(report: Mapping[str, Any]) -> str:
    """One object, sorted keys, one trailing newline. Diffable."""

    return json.dumps(report, indent=2, sort_keys=True) + "\n"


# ---------------------------------------------------------------------------
# The real runners
# ---------------------------------------------------------------------------


def platform_runner(*, platform_name: str | None = None
                    ) -> Callable[[], Observation]:
    """The one check that cannot be satisfied anywhere but Windows."""

    name = platform_name if platform_name is not None else platform_module.system()

    def check() -> Observation:
        # ``default_runners`` normally receives ``platform.system()``'s
        # ``Windows``, while the provisioning context carries
        # ``sys.platform``'s ``win32``.  Both identify the same native host.
        if name.lower() not in {"windows", "win32"}:
            return Observation(FAIL, "not_a_windows_host")
        return Observation(PASS, "native_windows")

    return check


def windows_build_runner(run: Callable[[Sequence[str]], Any]
                         ) -> Callable[[], Observation]:
    def check() -> Observation:
        completed = run(["C:\\Windows\\System32\\cmd.exe", "/c", "ver"])
        if getattr(completed, "returncode", 1) != 0:
            return Observation(FAIL, "version_unreadable")
        text = _text(completed)
        result = ww.parse_windows_build(text)
        build = _build_number(result.value)
        if not result.passed:
            return Observation(FAIL, "build_too_old",
                               {"minimum": ww.WINDOWS_MIN_BUILD,
                                "build": build})
        return Observation(PASS, "build_supported", {"build": build})

    return check


def wsl_version_runner(run: Callable[[Sequence[str]], Any]
                       ) -> Callable[[], Observation]:
    def check() -> Observation:
        completed = run(["wsl.exe", "--version"])
        if getattr(completed, "returncode", 1) != 0:
            return Observation(FAIL, "wsl_unavailable")
        result = ww.parse_wsl_version(_text(completed))
        if not result.passed:
            return Observation(FAIL, "wsl_too_old")
        # The version string is a fixed-shape token, so it is safe to carry.
        return Observation(PASS, "wsl_supported",
                           {"version": _short(result.value or "")})

    return check


def _build_number(value: Any) -> int:
    """``10.0.22631`` to ``22631``, and anything unparseable to zero."""

    parts = str(value or "").split(".")
    return int(parts[-1]) if parts and parts[-1].isdigit() else 0


def _text(completed: Any) -> str:
    """Decode command output without letting an encoding surprise raise.

    ``wsl.exe`` writes UTF-16LE on some builds and UTF-8 on others, and the
    difference is not something a validation run should fail on.
    """

    raw = getattr(completed, "stdout", b"") or b""
    if isinstance(raw, str):
        return raw
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", "replace")
    if b"\x00" in raw[:64]:
        return raw.decode("utf-16-le", "replace")
    return raw.decode("utf-8", "replace")


def features_runner(states: Callable[[], Mapping[str, Any]]
                    ) -> Callable[[], Observation]:
    """The two optional features, as Windows itself reports them.

    ``None`` for a feature is "could not be read", which is not "disabled"
    and is certainly not "enabled". Both are failures here; only an explicit
    Enabled is a pass.
    """

    def check() -> Observation:
        collected = states()
        missing = [name for name in wp.REQUIRED_FEATURES
                   if str(collected.get(name) or "").lower() != "enabled"]
        if missing:
            return Observation(FAIL, "feature_not_enabled",
                               {"first_missing": _short(missing[0])})
        return Observation(PASS, "features_enabled")

    return check


def reboot_runner(pending: Callable[[], bool]) -> Callable[[], Observation]:
    """A pending restart means this machine's answers are provisional."""

    def check() -> Observation:
        if pending():
            return Observation(FAIL, "restart_pending")
        return Observation(PASS, "no_restart_pending")

    return check


def runtime_root_runner(runtime_root: str) -> Callable[[], Observation]:
    """The directory records land in, checked the way records check it.

    Goes through :func:`windows_privacy.require_private_directory` rather
    than a mode comparison, so this check and the write path agree by
    construction: if one of them would refuse, so does the other.
    """

    def check() -> Observation:
        try:
            wpv.require_private_directory(runtime_root)
        except wpv.PrivacyError as exc:
            return Observation(FAIL, "runtime_root_not_private",
                               {"code": _short(str(exc))})
        except OSError:
            return Observation(FAIL, "runtime_root_unreadable")
        return Observation(PASS, "runtime_root_private")

    return check


def image_runner(rootfs_path: str, manifest: Any) -> Callable[[], Observation]:
    """The bytes on disk against the hash in the manifest.

    Hashing the whole file rather than trusting a recorded value: a recorded
    hash proves what was true when it was recorded, which is the claim this
    check exists to stop the lane from making about a file that has since
    changed.
    """

    def check() -> Observation:
        try:
            observed = wrf.sha256_path(rootfs_path)
        except OSError:
            return Observation(FAIL, "rootfs_unreadable")
        expected = getattr(manifest, "rootfs_sha256", None)
        if not isinstance(expected, str) or not wrf.is_sha256_hex(expected):
            return Observation(FAIL, "manifest_hash_missing")
        if observed != expected:
            return Observation(FAIL, "rootfs_hash_mismatch")
        return Observation(PASS, "rootfs_matches_manifest")

    return check


def parse_distro_list(text: str) -> dict[str, tuple[str, str]]:
    """``wsl.exe --list --verbose`` into ``{name: (state, version)}``.

    Written here rather than reused, because nothing else in the project
    needed it, and because the format is loose enough to deserve a parser
    with a test rather than a regex at a call site. The default marker is
    dropped: which distribution is default says nothing about this one.
    """

    parsed: dict[str, tuple[str, str]] = {}
    for line in text.splitlines():
        stripped = line.lstrip("*").strip()
        if not stripped:
            continue
        fields = stripped.split()
        if len(fields) < 3:
            continue
        name, state, version = fields[0], fields[1], fields[2]
        if name.upper() == "NAME":
            continue
        parsed[name] = (state, version)
    return parsed


def distro_runner(run: Callable[[Sequence[str]], Any], distro_name: str
                  ) -> Callable[[], Observation]:
    def check() -> Observation:
        completed = run(["wsl.exe", "--list", "--verbose"])
        if getattr(completed, "returncode", 1) != 0:
            return Observation(FAIL, "distro_list_unavailable")
        entry = parse_distro_list(_text(completed)).get(distro_name)
        if entry is None:
            return Observation(FAIL, "distro_not_registered")
        _state, version = entry
        if version.strip() != "2":
            # WSL1 shares the host filesystem and has no kernel boundary, so
            # a WSL1 registration is not a smaller version of this lane.
            return Observation(FAIL, "distro_not_wsl2")
        return Observation(PASS, "distro_registered_wsl2")

    return check


class GuestRoundTrip:
    """One real ephemeral job, read by four checks.

    Running the guest four times would be four instances, four imports and
    four teardowns, and the four answers could disagree with each other. One
    job produces one result and the four checks are four readings of it, so
    the report is internally consistent by construction.
    """

    def __init__(self, run_job: Callable[[], Any]):
        self._run_job = run_job
        self._result: Any = None
        self._ran = False

    def result(self) -> Any:
        if not self._ran:
            self._ran = True
            self._result = self._run_job()
        return self._result

    def guest_runner_check(self) -> Observation:
        result = self.result()
        if getattr(result, "canaries_passed", False):
            return Observation(PASS, "guest_runner_hash_matches")
        return Observation(FAIL, _short(getattr(result, "reason", "")) or "canary_failed")

    def canaries_check(self) -> Observation:
        result = self.result()
        if not getattr(result, "canaries_passed", False):
            return Observation(FAIL, "canaries_failed")
        return Observation(PASS, "canaries_passed",
                           {"count": len(getattr(result, "timings", {}) or {})})

    def egress_check(self) -> Observation:
        # The egress policy is one of the canaries, and it is the last of
        # them, so a canary pass is what carries this claim. Named separately
        # because it is the claim people most often overstate.
        result = self.result()
        if not getattr(result, "canaries_passed", False):
            return Observation(FAIL, "egress_policy_unproven")
        return Observation(PASS, "egress_policy_loaded_and_read_back")

    def round_trip_check(self) -> Observation:
        result = self.result()
        if not getattr(result, "ok", False):
            return Observation(FAIL, _short(getattr(result, "reason", "")) or "job_aborted")
        cleanup = getattr(result, "cleanup", None)
        if not getattr(cleanup, "complete", False):
            # A job that ran and left an instance behind is not a pass: the
            # lane's whole containment story is that nothing survives it.
            return Observation(FAIL, "cleanup_incomplete")
        return Observation(PASS, "job_completed_and_instance_destroyed")


def _short(value: Any) -> str:
    """Reduce anything to a bounded token, or to nothing at all."""

    token = re.sub(r"[^A-Za-z0-9_.:+-]+", "_", str(value)).strip("_")
    return token[:MAX_FACT_CHARS]


def default_runners(*, run: Callable[[Sequence[str]], Any],
                    platform_name: str | None = None,
                    feature_states: Callable[[], Mapping[str, Any]] | None = None,
                    reboot_pending: Callable[[], bool] | None = None,
                    runtime_root: str | None = None,
                    rootfs_path: str | None = None,
                    manifest: Any = None,
                    distro_name: str | None = None,
                    run_job: Callable[[], Any] | None = None,
                    ) -> dict[str, Callable[[], Observation]]:
    """Assemble the runners the caller supplied the inputs for.

    A check whose inputs were not supplied gets no runner and is reported
    ``blocked`` with ``check_not_implemented``, which keeps the verdict at
    ``not_ready``. That is the honest reading: a suite that quietly dropped
    the checks it lacked inputs for would call a half-validated machine ready.
    """

    runners: dict[str, Callable[[], Observation]] = {
        "platform": platform_runner(platform_name=platform_name),
        "windows_build": windows_build_runner(run),
        "wsl_version": wsl_version_runner(run),
    }
    if feature_states is not None:
        runners["features"] = features_runner(feature_states)
    if reboot_pending is not None:
        runners["reboot_clear"] = reboot_runner(reboot_pending)
    if runtime_root is not None:
        runners["runtime_root"] = runtime_root_runner(runtime_root)
    if rootfs_path is not None and manifest is not None:
        runners["image_pinned"] = image_runner(rootfs_path, manifest)
    if distro_name is not None:
        runners["distro_registered"] = distro_runner(run, distro_name)
    if run_job is not None:
        trip = GuestRoundTrip(run_job)
        runners["guest_runner"] = trip.guest_runner_check
        runners["canaries"] = trip.canaries_check
        runners["egress_policy"] = trip.egress_check
        runners["round_trip"] = trip.round_trip_check
    return runners
