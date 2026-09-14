"""The end-to-end provisioning driver: stock Windows to an activated worker.

:mod:`windows_wsl_provision` is the ladder, :mod:`windows_wsl_runtime` runs a
sandbox, :mod:`windows_evidence` records what was proven, and
:mod:`windows_activation` starts the background worker. This is the thing that
walks all of them in order, which is the part a person actually experiences.

The order is fixed and each step is a gate on the next:

1. **Observe.** Collect preflight evidence and Windows feature states.
2. **Consent, then elevate.** Turning on Windows features and installing the
   WSL kernel are machine-wide. The driver asks once, per surprise, and never
   proceeds on an assumed yes.
3. **Restart, and resume.** A reboot is written down before it happens, so
   setup continues at the stage it stopped at instead of starting over or
   appearing finished.
4. **WSL update or install**, still elevated, still consented.
5. **Acquire the guest image, provenance-verified.** The tarball is checked
   against the manifest *and* against a release trust anchor before it is
   installed. There is no anchor yet, so this step refuses today; that refusal
   is the honest state of the artifact workflow, not an oversight.
6. **Verify the boundary live.** Start a throwaway sandbox, run every canary,
   destroy it. Nothing is recorded unless all of them pass on this machine.
7. **Record machine-bound evidence.** Written by
   :func:`windows_evidence.record_verification`, which refuses off native
   Windows, so no amount of test wiring can mark a machine ready.
8. **Open the provider lane, separately.** A verified boundary says the
   sandbox is sealed. It says nothing about whether a subscription session
   works inside it, so the lane needs its own two live observations.
9. **Activate the per-user worker**, with its own consent, as a logon task in
   the user's own namespace.

Everything that touches the OS arrives through a collaborator the caller
supplies, so the whole ladder is exercisable off Windows while the steps that
must not be fakeable (recording evidence, running a sandbox) remain refusals
anywhere but a real host.

Nothing here has been validated on a live Windows host.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import windows_activation as wact
from . import windows_auth as wa
from . import windows_delegation as wd
from . import windows_evidence as wev
from . import windows_privacy as wpv
from . import windows_rootfs as wrf
from . import windows_wsl_provision as wp
from . import windows_wsl_runtime as wr

#: Step outcomes. A small closed vocabulary, because an installer UI branches
#: on these and a free-text status would eventually be branched on by prefix.
STEP_OK = "ok"
STEP_BLOCKED = "blocked"
STEP_CONSENT_REQUIRED = "consent_required"
STEP_FAILED = "failed"
STEP_REBOOT_SCHEDULED = "reboot_scheduled"
STEP_STATUSES = frozenset({STEP_OK, STEP_BLOCKED, STEP_CONSENT_REQUIRED,
                           STEP_FAILED, STEP_REBOOT_SCHEDULED})

#: The tool job used to prove the boundary. Deliberately the most boring
#: command available, and a non-provider one so no session is involved: the
#: point is the canaries around it, not its output.
BOUNDARY_TOOL = "node"
BOUNDARY_ARGS = ["--version"]

#: The capsule used for the refresh observation. Not a credential and never
#: was one: it is the right shape and deliberately worthless, so the guest
#: reaches the provider with a session the provider will not honour, which is
#: what a mid-job expiry looks like from inside. Withholding the capsule
#: entirely would not do: the host refuses to build that request at all, so
#: nothing would ever be observed in the guest.
SYNTHETIC_EXPIRED_TOKEN = "agent-bridge-synthetic-expired-session-" + "0" * 24


class DriverError(RuntimeError):
    """A driver step failed for a reason worth naming. Never carries data."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _step(name: str, status: str, reason: str = "", **extra: Any) -> dict[str, Any]:
    if status not in STEP_STATUSES:  # pragma: no cover - programming error
        raise ValueError("unknown step status")
    step = {"step": name, "status": status, "reason": reason}
    step.update(extra)
    return step


# ---------------------------------------------------------------------------
# Provenance-verified image acquisition
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImageProvenance:
    """What is actually known about the image sitting on disk."""

    installed: bool
    trusted: bool
    reason: str
    rootfs_sha256: str = ""
    release_blockers: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"installed": self.installed, "trusted": self.trusted,
                "reason": self.reason, "rootfs_sha256": self.rootfs_sha256,
                "release_blockers": list(self.release_blockers)}


def verify_installed_image(config: wd.DelegationConfig, *,
                           machine: str | None = None,
                           release: str | None = None,
                           anchors: Any = None,
                           verify_signature: Any = None) -> ImageProvenance:
    """Two questions, asked in the order that matters.

    First: are these the bytes the manifest describes, and is the image for
    this machine's architecture. Second, and separately: who says that manifest
    is the right one. The first is answered by hashing; the second cannot be,
    because a manifest vouching for itself is not evidence.
    """

    try:
        manifest = wd.load_manifest(config.manifest_path)
        sidecar = wd.load_sidecar(config.sidecar_path)
    except wd.DelegationRefused as exc:
        return ImageProvenance(False, False, exc.reason)

    rootfs = Path(config.rootfs_path)
    if not rootfs.is_file():
        return ImageProvenance(False, False, "rootfs_absent")
    try:
        observed = wrf.sha256_path(str(rootfs))
    except OSError:
        return ImageProvenance(False, False, "rootfs_unreadable")

    check = wrf.verify_image(manifest=_manifest_dict(manifest),
                             observed_sha256=observed, sidecar=sidecar,
                             host_architecture=wd.host_architecture(machine))
    if not check.passed:
        return ImageProvenance(True, False, check.reason, rootfs_sha256=observed)

    architecture = (sidecar or {}).get("architecture", "")
    trust = wrf.verify_manifest_trust(
        _manifest_dict(manifest), architecture=str(architecture),
        release=release, anchors=anchors, verify_signature=verify_signature)
    return ImageProvenance(True, trust.passed, "" if trust.passed else trust.reason,
                           rootfs_sha256=observed,
                           release_blockers=wrf.release_blockers(anchors=anchors))


def _manifest_dict(manifest: Any) -> dict[str, Any]:
    """The manifest as the keys the runtime's own parser accepts."""

    return {"schema_version": 1,
            "distro_release": manifest.distro_release,
            "rootfs_sha256": manifest.rootfs_sha256,
            "node_version": manifest.node_version,
            "claude_version": manifest.claude_version,
            "codex_version": manifest.codex_version}


def acquire_guest_image(source_path: str, config: wd.DelegationConfig, *,
                        consent: bool, platform: Any = None,
                        **provenance: Any) -> ImageProvenance:
    """Install a supplied tarball into the runtime root, verified both ways.

    Nothing is fetched here. A downloader inside the provisioner would be one
    more thing deciding what to trust; the operator supplies a file, and this
    decides whether it may be used. The copy is written owner-only and
    atomically, and the provenance check runs against the installed copy, not
    against the source, so what was checked is what will be imported.
    """

    if consent is not True:
        raise DriverError("image_install_consent_required")
    source = Path(source_path)
    try:
        payload = source.read_bytes()
    except OSError:
        raise DriverError("image_source_unreadable") from None

    target = Path(config.rootfs_path)
    try:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        wpv.atomic_private_write(target, payload,
                                 secure=wpv.platform_secure_writer(platform),
                                 root=target.parent)
    except wpv.PrivacyError as exc:
        raise DriverError(f"image_{exc.reason}") from None
    except OSError:
        raise DriverError("image_unwritable") from None
    finally:
        del payload
    return verify_installed_image(config, **provenance)


# ---------------------------------------------------------------------------
# Live verification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BoundaryOutcome:
    """The result of one live boundary run, before anything is recorded."""

    verified: bool
    reason: str
    canaries: tuple[str, ...] = ()
    cleanup_complete: bool = False


def verify_boundary_live(config: wd.DelegationConfig, *,
                         run_job: Any = None,
                         distro_token: str | None = None) -> BoundaryOutcome:
    """Start a sandbox, let every canary run, and destroy it.

    ``canaries_passed`` is the whole point. A job that completed with a canary
    unrun proves nothing about the boundary, and a canary that failed is the
    one outcome that must never be recoverable into a "mostly fine".
    """

    runner = run_job if run_job is not None else wr.run_job
    try:
        manifest = wd.load_manifest(config.manifest_path)
        request = wd.build_job_request(config=config, manifest=manifest,
                                       tool=BOUNDARY_TOOL, args=list(BOUNDARY_ARGS),
                                       stdin_data="", distro_token=distro_token)
    except wd.DelegationRefused as exc:
        return BoundaryOutcome(False, exc.reason)

    result = runner(request)
    cleanup = getattr(result, "cleanup", None)
    complete = bool(getattr(cleanup, "complete", False))
    if not getattr(result, "canaries_passed", False):
        return BoundaryOutcome(False, "canaries_failed", cleanup_complete=complete)
    if getattr(result, "status", "") != wr.STATUS_COMPLETED:
        return BoundaryOutcome(False, wr._token(getattr(result, "reason", "")),
                               cleanup_complete=complete)
    if not complete:
        # A sandbox that could not be proven gone is not a clean result. The
        # canaries described a boundary; a leftover distro outlives it.
        return BoundaryOutcome(False, "cleanup_incomplete")
    return BoundaryOutcome(True, "", canaries=tuple(wr.CANARY_ORDER),
                           cleanup_complete=True)


def record_boundary(config: wd.DelegationConfig, outcome: BoundaryOutcome, *,
                    wsl_version: str, recorded_at: str,
                    fingerprint: str | None = None,
                    provider_lane: wev.ProviderLane | None = None,
                    os_name: str | None = None,
                    platform: Any = None) -> wev.Evidence:
    """Write the machine-bound record. Refuses anywhere but a live Windows host.

    The lane defaults to closed, and that is not an oversight left over from
    when nothing could open it. Re-verifying the boundary happens because
    something about the guest changed, and a session observation made against
    the previous image is not evidence about this one. The lane is opened
    afterwards, by :func:`record_provider_lane`, from its own live run.
    """

    if not outcome.verified:
        raise DriverError("boundary_not_verified")
    try:
        manifest = wd.load_manifest(config.manifest_path)
    except wd.DelegationRefused as exc:
        raise DriverError(exc.reason) from None
    evidence = wev.Evidence(
        recorded_at=recorded_at,
        host_fingerprint=fingerprint or wev.host_fingerprint(),
        wsl_version=wsl_version,
        rootfs_sha256=manifest.rootfs_sha256,
        guest_runner_sha256=wd.guest_runner_sha256(),
        canaries=tuple(outcome.canaries),
        boundary_verified=True,
        provider_lane=provider_lane or wev.ProviderLane())
    try:
        wev.record_verification(wev.evidence_path(config.runtime_root), evidence,
                                os_name=os_name, platform=platform)
    except wev.EvidenceError as exc:
        raise DriverError(str(exc)) from None
    return evidence


# ---------------------------------------------------------------------------
# The provider lane, which a sealed boundary does not grant
# ---------------------------------------------------------------------------


def wa_kind(provider: str) -> str:
    """The capsule kind the guest will accept for this provider."""

    from . import guest_runner

    return guest_runner.AUTH_KINDS[provider]


#: The probe verdict that means a real authenticated turn happened. Imported
#: by name rather than compared against a literal, so the guest's vocabulary
#: and the host's agreement about it cannot drift apart silently.
def _probe_verdicts() -> Any:
    from . import guest_runner

    return guest_runner


@dataclass(frozen=True)
class ProbeOutcome:
    """One probe run, reduced to a verdict. Never carries provider output."""

    verdict: str
    completed: bool
    cleanup_complete: bool

    @property
    def authenticated(self) -> bool:
        return self.verdict == _probe_verdicts().PROBE_AUTHENTICATED and self.completed

    @property
    def rejected(self) -> bool:
        return self.verdict == _probe_verdicts().PROBE_REJECTED


def run_auth_probe(config: wd.DelegationConfig, provider: str, *,
                   capsule: Mapping[str, str], run_job: Any = None,
                   distro_token: str | None = None) -> ProbeOutcome:
    """Run one minimal authenticated provider operation inside a fresh guest.

    The guest decides the verdict, because the guest is the only side that
    sees the provider's answer. What comes back here is one token from a fixed
    vocabulary plus whether the sandbox was destroyed; the answer itself, and
    anything the provider printed, stays inside the sandbox that is deleted.
    """

    runner = run_job if run_job is not None else wr.run_job
    guest = _probe_verdicts()
    try:
        manifest = wd.load_manifest(config.manifest_path)
        request = wd.build_auth_probe_request(
            config=config, manifest=manifest, provider=provider,
            auth=dict(capsule), distro_token=distro_token)
    except wd.DelegationRefused as exc:
        return ProbeOutcome(f"probe_unbuildable:{exc.reason}", False, False)

    result = runner(request)
    cleanup = bool(getattr(getattr(result, "cleanup", None), "complete", False))
    if not getattr(result, "canaries_passed", False):
        # A probe run whose boundary checks did not pass proves nothing about
        # the session, and must never be read as a provider outcome.
        return ProbeOutcome("probe_canaries_failed", False, cleanup)
    verdict = wr._token(getattr(result, "reason", ""))
    completed = getattr(result, "status", "") == wr.STATUS_COMPLETED
    if verdict not in guest.PROBE_VERDICTS:
        return ProbeOutcome("probe_verdict_unknown", False, cleanup)
    return ProbeOutcome(verdict, completed, cleanup)


def observe_provider_lane(config: wd.DelegationConfig, provider: str, *,
                          capsule: Mapping[str, str],
                          run_job: Any = None,
                          distro_token: str | None = None) -> wev.ProviderLane:
    """Two live observations, both required, neither assumed.

    *Portability*: a session minted on this host is actually accepted by the
    provider running inside the guest. This is established by a real
    authenticated operation that returns a fixed sentinel, not by asking the
    CLI for its version. A version string is printed identically with a valid
    session, a worthless session, and no session at all, so it cannot
    distinguish the three things this observation exists to distinguish.

    *Refresh behaviour*: when the session the guest carries is not one the
    provider honours, the job ends in a bounded, named refusal instead of
    hanging on a login prompt or silently proceeding unauthenticated. That is
    what a mid-job expiry looks like from inside, and it is observable now with
    a deliberately worthless capsule rather than by waiting for a real session
    to expire.

    The second observation is what rules out the false positive the first one
    cannot rule out alone. If a worthless session also produces a successful
    authenticated turn, then whatever authorised that turn was not the session
    this lane is about, and the lane stays shut.
    """

    if provider not in wd.PROVIDER_TOOLS:
        raise DriverError("provider_not_supported")

    live = run_auth_probe(config, provider, capsule=capsule, run_job=run_job,
                          distro_token=distro_token)
    if not live.authenticated:
        return wev.ProviderLane(detail=f"portability_not_observed:{live.verdict}")
    if not live.cleanup_complete:
        return wev.ProviderLane(detail="portability_probe_left_a_sandbox")

    stale = run_auth_probe(
        config, provider,
        capsule={"kind": wa_kind(provider), "token": SYNTHETIC_EXPIRED_TOKEN},
        run_job=run_job, distro_token=distro_token)
    if stale.authenticated:
        # A capsule that is not a credential produced an authenticated turn.
        # Either the run was authorised by something other than the session,
        # or nothing checked the session at all.
        return wev.ProviderLane(portability_observed=True,
                                detail="refresh_behaviour_not_observed:accepted")
    if not stale.rejected:
        # It failed, but not identifiably because the session was refused. A
        # failure for an unrelated reason is not evidence about refresh.
        return wev.ProviderLane(portability_observed=True,
                                detail=f"refresh_behaviour_inconclusive:{stale.verdict}")
    if not stale.cleanup_complete:
        return wev.ProviderLane(portability_observed=True,
                                detail="refresh_probe_left_a_sandbox")
    return wev.ProviderLane(verified=True, providers=(provider,),
                            portability_observed=True,
                            refresh_behaviour_observed=True,
                            detail="observed_live")


def record_provider_lane(config: wd.DelegationConfig, lane: wev.ProviderLane, *,
                         os_name: str | None = None,
                         platform: Any = None) -> wev.Evidence:
    """Amend the machine-bound record with what the lane observation showed.

    Read, amend, write, and then read back. The read-back is the part that
    matters: everything downstream asks the *record* whether the lane is open,
    so a lane that was observed but did not durably land must not enable
    anything. An in-memory result that agrees with itself is not evidence.

    The amendment is pinned to the identity of the record it was computed
    from, so it cannot discard a boundary record written in between.
    """

    path = wev.evidence_path(config.runtime_root)
    try:
        existing, identity = wev.read_evidence_with_identity(
            path, runtime_root=config.runtime_root, platform=platform)
    except wev.EvidenceError as exc:
        raise DriverError(str(exc)) from None
    if not existing.boundary_verified:
        raise DriverError("boundary_not_verified")

    amended = wev.Evidence(
        recorded_at=existing.recorded_at,
        host_fingerprint=existing.host_fingerprint,
        wsl_version=existing.wsl_version,
        rootfs_sha256=existing.rootfs_sha256,
        guest_runner_sha256=existing.guest_runner_sha256,
        canaries=existing.canaries,
        boundary_verified=True,
        provider_lane=lane)
    try:
        wev.record_verification(path, amended, os_name=os_name,
                                platform=platform, expect_identity=identity)
    except wev.EvidenceError as exc:
        raise DriverError(str(exc)) from None

    reloaded = _reload_lane(config, platform=platform)
    if reloaded is None:
        raise DriverError("provider_lane_not_durable")
    if reloaded.as_dict() != lane.as_dict():
        raise DriverError("provider_lane_readback_mismatch")
    return amended


def _reload_lane(config: wd.DelegationConfig, *, platform: Any = None
                 ) -> wev.ProviderLane | None:
    """The lane exactly as the durable record now states it, or None."""

    try:
        rootfs_sha256 = wd.load_manifest(config.manifest_path).rootfs_sha256
    except wd.DelegationRefused:
        return None
    _state, evidence, _reason = wev.load_verified_state(
        wev.evidence_path(config.runtime_root),
        expected_rootfs_sha256=rootfs_sha256,
        expected_runner_sha256=wd.guest_runner_sha256(),
        required_canaries=wr.CANARY_ORDER,
        runtime_root=config.runtime_root, platform=platform)
    return None if evidence is None else evidence.provider_lane


# ---------------------------------------------------------------------------
# The driver
# ---------------------------------------------------------------------------


@dataclass
class DriverContext:
    """Everything the driver needs from the outside world.

    Supplied rather than discovered, for the same reason the runtime takes a
    ``HostOps``: a driver that reached for the OS itself could not be walked
    end to end anywhere except the machine it is provisioning.
    """

    config: wd.DelegationConfig
    run: Callable[[Sequence[str]], Any]
    preflight: Any = None
    feature_states: Mapping[str, str | None] = field(default_factory=dict)
    reboot_pending: bool = False
    platform_name: str | None = None
    machine: str | None = None
    os_name: str | None = None
    wsl_version: str = ""
    clock: Callable[[], str] | None = None
    run_job: Any = None
    image_source_path: str | None = None
    #: Trust material for the image. Defaults to what the project ships, which
    #: is nothing, so the image step refuses unless a caller supplies real
    #: anchors and a verifier.
    release: str | None = None
    anchors: Any = None
    verify_signature: Any = None
    #: The ``os``-like module the privacy and protection layers consult. A
    #: seam for tests only; production leaves it None and gets the real one.
    platform_module: Any = None
    #: The argv the per-user logon task runs after a restart. Required before
    #: any rebooting stage can be taken: a reboot with no way back is how
    #: setup used to strand a machine halfway through.
    resume_command: Sequence[str] | None = None


def observe(context: DriverContext) -> tuple[wp.ProvisionState, wp.ProvisionEvidence,
                                             ImageProvenance]:
    """One observation feeding both the ladder and the image gate."""

    provenance = verify_installed_image(
        context.config, machine=context.machine, release=context.release,
        anchors=context.anchors, verify_signature=context.verify_signature)
    boundary, lane = _recorded_state(context)
    state, evidence = wp.observe(
        platform_name=context.platform_name, preflight=context.preflight,
        feature_states=context.feature_states,
        reboot_pending=context.reboot_pending,
        guest_image_installed=provenance.installed and provenance.trusted,
        boundary_verified=boundary,
        provider_lane_verified=lane)
    return state, evidence, provenance


def _recorded_state(context: DriverContext) -> tuple[bool, bool | None]:
    """What the durable record says: boundary verified, and lane observed.

    Both come from the same read, and neither is inferred from anything that
    happened in this process. A driver that remembered its own successful run
    would report a machine as provisioned after a record failed to land.
    """

    config = context.config
    try:
        rootfs_sha256 = wd.load_manifest(config.manifest_path).rootfs_sha256
    except wd.DelegationRefused:
        return False, None
    _state, evidence, _reason = wev.load_verified_state(
        wev.evidence_path(config.runtime_root),
        expected_rootfs_sha256=rootfs_sha256,
        expected_runner_sha256=wd.guest_runner_sha256(),
        required_canaries=wr.CANARY_ORDER,
        runtime_root=config.runtime_root)
    if evidence is None:
        return False, None
    return True, (True if evidence.provider_lane.observed else None)


def step(context: DriverContext, *, admin_consent: bool = False,
         reboot_consent: bool = False, image_consent: bool = False,
         provider_consent: bool = False, elevate: bool = True) -> dict[str, Any]:
    """Do the single next thing, and no more.

    One step per call on purpose. Each of these can require a decision, take
    minutes, or end the user's session, and a loop that ran them back to back
    would make the consent boundary depend on how fast the previous command
    finished.
    """

    state, _evidence, provenance = observe(context)
    stage = wp.next_stage(state)
    name = stage.stage

    if name == wp.STAGE_READY:
        return _step(name, STEP_OK, "", stage_title=stage.title)

    if stage.actor == wp.ACTOR_USER:
        return _step(name, STEP_BLOCKED, "requires_a_person",
                     stage_title=stage.title, detail=stage.detail)

    if name == wp.STAGE_GUEST_IMAGE:
        return _image_step(context, provenance, consent=image_consent)

    if name == wp.STAGE_BOUNDARY_VERIFICATION:
        return _boundary_step(context)

    if name == wp.STAGE_PROVIDER_ENROLMENT:
        return _provider_step(context, consent=provider_consent)

    try:
        wp.require_consent(stage, admin_consent=admin_consent,
                           reboot_consent=reboot_consent)
    except wp.ConsentRequired as exc:
        return _step(name, STEP_CONSENT_REQUIRED, str(exc),
                     stage_title=stage.title, detail=stage.detail,
                     requires_admin=stage.requires_admin, reboots=stage.reboots)

    if stage.reboots:
        # Record *and* task, before the machine goes down. This used to write
        # the record only, which left a machine that rebooted with a note
        # describing where it was and nothing that would ever read the note.
        scheduled = schedule_resume(context, stage,
                                    command=context.resume_command or ())
        if scheduled["status"] != STEP_OK:
            # No way back means no reboot. Refusing here leaves the machine
            # exactly where it is, which is recoverable; rebooting anyway is
            # not.
            return scheduled

    outcome = wp.advance(stage, run=context.run, admin_consent=admin_consent,
                         reboot_consent=reboot_consent, elevate=elevate)
    if outcome["status"] != "ok":
        return _step(name, STEP_FAILED, outcome.get("reason", outcome["status"]),
                     stage_title=stage.title, commands_run=outcome["commands_run"])
    if stage.reboots:
        return _step(name, STEP_REBOOT_SCHEDULED, "",
                     stage_title=stage.title,
                     commands_run=outcome["commands_run"])
    return _step(name, STEP_OK, "", stage_title=stage.title,
                 commands_run=outcome["commands_run"])


def _now(context: DriverContext) -> str:
    if context.clock is not None:
        return context.clock()
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _image_step(context: DriverContext, provenance: ImageProvenance, *,
                consent: bool) -> dict[str, Any]:
    if provenance.installed and provenance.trusted:
        return _step(wp.STAGE_GUEST_IMAGE, STEP_OK, "",
                     provenance=provenance.as_dict())
    if context.image_source_path is None:
        return _step(wp.STAGE_GUEST_IMAGE, STEP_BLOCKED,
                     provenance.reason or "image_source_missing",
                     provenance=provenance.as_dict())
    try:
        installed = acquire_guest_image(
            context.image_source_path, context.config, consent=consent,
            machine=context.machine, release=context.release,
            anchors=context.anchors, verify_signature=context.verify_signature)
    except DriverError as exc:
        status = (STEP_CONSENT_REQUIRED
                  if exc.reason == "image_install_consent_required" else STEP_FAILED)
        return _step(wp.STAGE_GUEST_IMAGE, status, exc.reason,
                     provenance=provenance.as_dict())
    if not installed.trusted:
        return _step(wp.STAGE_GUEST_IMAGE, STEP_BLOCKED, installed.reason,
                     provenance=installed.as_dict())
    return _step(wp.STAGE_GUEST_IMAGE, STEP_OK, "",
                 provenance=installed.as_dict())


def _boundary_step(context: DriverContext) -> dict[str, Any]:
    outcome = verify_boundary_live(context.config, run_job=context.run_job)
    if not outcome.verified:
        return _step(wp.STAGE_BOUNDARY_VERIFICATION, STEP_FAILED, outcome.reason,
                     cleanup_complete=outcome.cleanup_complete)
    try:
        evidence = record_boundary(context.config, outcome,
                                   wsl_version=context.wsl_version,
                                   recorded_at=_now(context),
                                   os_name=context.os_name)
    except DriverError as exc:
        return _step(wp.STAGE_BOUNDARY_VERIFICATION, STEP_FAILED, exc.reason)
    return _step(wp.STAGE_BOUNDARY_VERIFICATION, STEP_OK, "",
                 recorded_at=evidence.recorded_at,
                 canaries=list(evidence.canaries))


def _provider_step(context: DriverContext, *, consent: bool) -> dict[str, Any]:
    """Observe the provider lane live, then record it, then read it back.

    This is the step that was missing. The observation existed and had no
    caller, so the lane it could have opened was never written anywhere and
    :func:`record_boundary` stored a closed lane unconditionally. Delegation
    therefore refused forever, and the executor could not bootstrap itself:
    it would not hand over a session until the lane was open, and the lane
    could only be opened by handing over a session.

    The bootstrap is resolved here rather than in the executor, and
    deliberately so. The driver holds the one context in which handing a
    session into the guest is justified before the lane is open: a boundary
    that was verified on this machine moments ago, a person present who
    consented to exactly this, and a single fixed probe that does no work.
    """

    if consent is not True:
        return _step(wp.STAGE_PROVIDER_ENROLMENT, STEP_CONSENT_REQUIRED,
                     "provider_probe_consent_required",
                     detail=PROVIDER_PROBE_CONSENT)

    states = wa.enrolment_states(context.config.runtime_root,
                                 platform=context.platform_module)
    enrolled = [state for state in states
                if state.get("status") in ("enrolled", "enrolled_lane_closed")]
    if not enrolled:
        return _step(wp.STAGE_PROVIDER_ENROLMENT, STEP_BLOCKED, "not_enrolled",
                     provider_sessions=states)

    failures: list[str] = []
    for state in enrolled:
        provider = str(state["provider"])
        try:
            capsule = wa.load_capsule(context.config.runtime_root, provider,
                                      platform=context.platform_module)
        except wa.AuthError as exc:
            failures.append(f"{provider}:{exc.reason}")
            continue
        try:
            lane = observe_provider_lane(context.config, provider,
                                         capsule=capsule,
                                         run_job=context.run_job)
        finally:
            # The plaintext leaves scope here. It is not erased; see
            # windows_auth.MEMORY_LIFETIME_NOTE for what that does and does
            # not mean.
            capsule = None
        if not lane.observed:
            failures.append(f"{provider}:{lane.detail}")
            continue
        try:
            recorded = record_provider_lane(context.config, lane,
                                            os_name=context.os_name,
                                            platform=context.platform_module)
        except DriverError as exc:
            return _step(wp.STAGE_PROVIDER_ENROLMENT, STEP_FAILED, exc.reason)
        return _step(wp.STAGE_PROVIDER_ENROLMENT, STEP_OK, "",
                     providers=list(recorded.provider_lane.providers),
                     detail=recorded.provider_lane.detail)
    return _step(wp.STAGE_PROVIDER_ENROLMENT, STEP_BLOCKED,
                 "provider_lane_not_observed", failures=failures)


#: Shown before the probe runs. It spends a small amount of the user's own
#: provider allowance, which they are entitled to know before it happens.
PROVIDER_PROBE_CONSENT = (
    "Setup will send one very small request to your provider using your own "
    "subscription, from inside the sandbox, and check that the expected "
    "answer comes back. It then repeats the check with a deliberately invalid "
    "sign-in to confirm that a stale session is refused rather than ignored. "
    "This uses a small amount of your provider allowance. Nothing you have "
    "written is sent, and no answer is recorded.")


# ---------------------------------------------------------------------------
# Reboot and resume, as one owned state machine
# ---------------------------------------------------------------------------
#
# Three things have to agree for a resume to be real: a record saying where
# setup got to, a logon task that will actually run, and a bounded count so a
# stage that fails identically every boot stops asking. Any one of them alone
# is worse than none: a record with no task never resumes, a task with no
# record restarts from the beginning and elevates again for work already done,
# and neither with a count is a reboot loop.


@dataclass(frozen=True)
class ResumeState:
    """Where a resumed setup believes it is, and whether it may continue."""

    stage: str
    attempts: int
    awaiting_reboot: bool
    exhausted: bool
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"stage": self.stage, "attempts": self.attempts,
                "awaiting_reboot": self.awaiting_reboot,
                "exhausted": self.exhausted, "reason": self.reason}


def schedule_resume(context: DriverContext, stage: wp.Stage, *,
                    command: Sequence[str]) -> dict[str, Any]:
    """Write the record and register the task, in that order, both proven.

    Order matters and is not arbitrary. The record is written first because a
    task that fires with no record is recoverable (setup re-observes and
    continues) while a record with no task is not (nothing ever runs again).
    If the task cannot be registered, the record is removed rather than left
    as a promise nothing will keep.

    The task name is only taken over when it is proven to be ours.
    ``schtasks /F`` replaces whatever holds a name, so registering without the
    ownership check is how setup would silently delete somebody else's task
    that happened to be called the same thing.
    """

    path = wp.resume_record_path(context.config.runtime_root)
    try:
        existing = wp.read_resume_record(path, platform=context.platform_module)
    except wp.ResumeError as exc:
        return _step("resume_schedule", STEP_FAILED, exc.reason)
    previous, identity = existing if existing is not None else ({}, None)
    attempts = wp.next_attempt(previous, stage.stage)
    if attempts > wp.MAX_STAGE_ATTEMPTS:
        return _step("resume_schedule", STEP_BLOCKED, "stage_attempts_exhausted",
                     stage=stage.stage, attempts=attempts - 1)

    argv = [str(item) for item in command]
    if not argv:
        # A rebooting stage with no way back. Named rather than raised: the
        # caller is a step, and a step reports.
        return _step("resume_schedule", STEP_FAILED, "resume_command_missing")
    try:
        register = wp.register_resume_argv(argv)
    except ValueError:
        return _step("resume_schedule", STEP_FAILED, "resume_command_unsafe")

    status = _resume_task_status(context)
    blocker = wp.resume_ownership_blocker(status, argv)
    if blocker:
        return _step("resume_schedule", STEP_BLOCKED, "resume_task_not_ours",
                     detail=blocker)

    record = wp.build_resume_record(
        stage=stage.stage, stage_completed=None, awaiting_reboot=True,
        updated_at=_now(context), attempts=attempts)
    try:
        wp.write_resume_record(path, record, platform=context.platform_module,
                               expect_identity=identity)
    except wp.ResumeError as exc:
        return _step("resume_schedule", STEP_FAILED, exc.reason)

    result = context.run(register)
    if getattr(result, "returncode", None) != 0:
        # No task means no resume. Do not leave a record claiming otherwise.
        try:
            wp.clear_resume_record(path)
        except wp.ResumeError:
            pass
        return _step("resume_schedule", STEP_FAILED, "resume_task_unregistered")
    return _step("resume_schedule", STEP_OK, "", stage=stage.stage,
                 attempts=attempts, task_name=wp.RESUME_TASK_NAME)


def _resume_task_status(context: DriverContext) -> wact.ActivationStatus:
    try:
        result = context.run(wp.resume_status_argv())
    except OSError:
        return wact.parse_status(1, b"")
    return wact.parse_status(getattr(result, "returncode", 1),
                             getattr(result, "stdout", b"") or b"")


def resume(context: DriverContext) -> ResumeState | None:
    """Read what the reboot left, validate it, and say whether to continue.

    Returns None when there is nothing to resume, which is the ordinary case
    on a machine that never needed a restart. A malformed or unprotected
    record is not treated as "nothing to resume": it is reported, because the
    difference between "setup never started" and "setup's state was tampered
    with" is exactly the difference this file exists to preserve.
    """

    path = wp.resume_record_path(context.config.runtime_root)
    try:
        existing = wp.read_resume_record(path, platform=context.platform_module)
    except wp.ResumeError as exc:
        return ResumeState(stage="", attempts=0, awaiting_reboot=False,
                           exhausted=True, reason=exc.reason)
    if existing is None:
        return None
    record, _identity = existing
    stage = str(record["stage"])
    attempts = int(record["attempts"])
    return ResumeState(stage=stage, attempts=attempts,
                       awaiting_reboot=bool(record["awaiting_reboot"]),
                       exhausted=wp.should_stop_retrying(record, stage))


def finish_resume(context: DriverContext) -> dict[str, Any]:
    """Remove the record and the task, once and only once it is safe to.

    Safe means one of two terminal conditions, not "the last command worked":
    provisioning has reached a state where delegation may be enabled, or it
    has stopped for a reason a further reboot cannot change. Removing the
    record at any other point is how a half-provisioned machine forgets what
    it was doing.
    """

    state, _evidence, _provenance = observe(context)
    resume_state = resume(context)
    if resume_state is None:
        return _step("resume_finish", STEP_OK, "", detail="nothing_to_clear")
    ready = wp.delegation_may_be_enabled(state)
    if not ready and not resume_state.exhausted:
        return _step("resume_finish", STEP_BLOCKED, "still_provisioning",
                     stage=wp.current_stage(state))

    argv = wp.unregister_resume_argv()
    result = context.run(argv)
    removed_task = getattr(result, "returncode", None) == 0
    try:
        wp.clear_resume_record(wp.resume_record_path(context.config.runtime_root))
    except wp.ResumeError as exc:
        return _step("resume_finish", STEP_FAILED, exc.reason,
                     task_removed=removed_task)
    if not removed_task:
        # The record is gone, so nothing will act on a stale stage, but a
        # logon task nobody removed is an orphan and is reported as one.
        return _step("resume_finish", STEP_FAILED, "resume_task_not_removed")
    return _step("resume_finish", STEP_OK, "",
                 terminal=("ready" if ready else "stopped"))


def activate_worker(context: DriverContext, *, command: Sequence[str],
                    consent: bool) -> dict[str, Any]:
    """Register the logon task, only once everything above actually holds.

    Activation last, deliberately. A worker registered before the boundary was
    proven would start, find delegation refused, and fail on a schedule.
    """

    if consent is not True:
        return _step("activation", STEP_CONSENT_REQUIRED, "activation_consent_required")
    state, _evidence, _provenance = observe(context)
    if not wp.delegation_may_be_enabled(state):
        return _step("activation", STEP_BLOCKED, "provisioning_incomplete",
                     stage=wp.current_stage(state))
    try:
        argv = wact.register_argv(command)
    except wact.ActivationError as exc:
        return _step("activation", STEP_FAILED, str(exc))
    result = context.run(argv)
    if getattr(result, "returncode", None) != 0:
        return _step("activation", STEP_FAILED, "command_failed")
    return _step("activation", STEP_OK, "", task_name=wact.TASK_NAME)


def plan(context: DriverContext) -> dict[str, Any]:
    """What is done, what is next, and what is still ahead. Reads only."""

    state, evidence, provenance = observe(context)
    stage = wp.next_stage(state)
    lane = _reload_lane(context.config, platform=context.platform_module)
    # The setup screen must distinguish "a session is saved" from "a session
    # is known to work here". They are different facts and only the second
    # one lets a job run.
    sessions = wa.enrolment_states(context.config.runtime_root, lane=lane,
                                   platform=context.platform_module)
    return {
        "stage": stage.stage,
        "title": stage.title,
        "detail": stage.detail,
        "actor": stage.actor,
        "requires_admin": stage.requires_admin,
        "reboots": stage.reboots,
        "remaining": wp.remaining_stages(state),
        "delegation_may_be_enabled": wp.delegation_may_be_enabled(state),
        "image": provenance.as_dict(),
        "provider_sessions": sessions,
        "provider_lane": (lane.as_dict() if lane is not None
                          else wev.ProviderLane().as_dict()),
        "provider_probe_consent": PROVIDER_PROBE_CONSENT,
        "checks": evidence.as_dict()["checks"],
        "release_blockers": list(wrf.release_blockers(anchors=context.anchors)),
    }


# ---------------------------------------------------------------------------
# Limitations, stated rather than papered over
# ---------------------------------------------------------------------------

DRIVER_LIMITATION = (
    "the driver walks every stage from a stock Windows machine to an activated "
    "worker, and every stage is now connected: the provider observation has a "
    "caller, what it observes is persisted machine-bound and read back before "
    "anything is enabled, and the reboot path writes a protected record and "
    "registers an ownership-proven logon task. Two steps still refuse rather "
    "than complete, and both refuse for the same reason, which is that "
    "something has not been produced rather than that something is unwired: "
    "the image step refuses because no release has published a signed manifest "
    "to anchor trust to, and the provider lane refuses until the authenticated "
    "probe has actually been run against a real subscription on a real Windows "
    "host. Neither refusal is a bug to route around"
)
