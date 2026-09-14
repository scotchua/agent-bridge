"""Durable, machine-bound evidence that a Windows host was actually verified.

Everything else in the Windows delegation stack is portable and testable on a
development machine, which is exactly why this module exists. A portable test
can construct any :class:`~.windows_wsl_provision.ProvisionState` it likes,
including a fully ready one, and a planner cannot tell the difference. This
module is the one place that decides whether a *real* machine was verified,
and it is deliberately hard to satisfy by accident:

* **Only a native Windows process may write one.** :func:`record_verification`
  refuses on anything else. A record produced on macOS is not evidence about a
  Windows host.
* **A record is bound to the machine that produced it.** It carries a host
  fingerprint, and :func:`load_verified_state` refuses a record whose
  fingerprint does not match the machine reading it. Copying a verified record
  from a working machine onto a fresh one does not enable delegation there.
* **A record is bound to the artefacts it verified.** The rootfs hash and the
  guest runner hash are part of the record. Replacing the image after
  verification invalidates it, because what was verified is no longer what
  would run.
* **The provider lane is a second, separate gate.** Proving the boundary holds
  says nothing about whether a subscription session survives being handed into
  the guest. That needs its own recorded observation, including the two things
  that cannot be proven from a development machine: portability and refresh.

Nothing here has been produced on a live Windows host from this worktree, so
no verified record exists anywhere in this repository, and the fail-closed
paths are the only ones the tests can reach.
"""

from __future__ import annotations

import json
import os
import platform as platform_module
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from . import windows_privacy as wpv
from . import windows_wsl_provision as wp

SCHEMA_VERSION = 1

EVIDENCE_NAME = "provision-evidence.json"

EVIDENCE_KEYS = frozenset({
    "schema_version", "recorded_at", "host_fingerprint", "platform",
    "wsl_version", "rootfs_sha256", "guest_runner_sha256", "canaries",
    "boundary_verified", "provider_lane",
})

PROVIDER_KEYS = frozenset({
    "verified", "providers", "portability_observed",
    "refresh_behaviour_observed", "detail",
})

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[0-9:.+\-Z]{5,32}$")


class EvidenceError(ValueError):
    """The record is missing, malformed, or not about this machine."""


def evidence_path(runtime_root: str) -> str:
    """Joined with ``os.path``, not ``ntpath``.

    Unlike the configured rootfs and manifest paths, which are Windows paths
    validated off Windows, this file is only ever opened by the process that
    lives beside it. Joining with the running platform's rules is what makes
    the loader exercisable on a development machine at all.
    """

    return os.path.join(runtime_root, EVIDENCE_NAME)


# ---------------------------------------------------------------------------
# Host identity
# ---------------------------------------------------------------------------


def host_fingerprint(*, machine_guid: str | None = None,
                     node: str | None = None) -> str:
    """A stable, non-secret identifier for this machine.

    Built from the Windows MachineGuid where it can be read, and the node name
    otherwise. It is not a security boundary and is not treated as one: it
    exists so that a verified record copied to a different machine is rejected
    as being about somewhere else, which is a mistake far likelier than an
    attack.
    """

    import hashlib

    guid = machine_guid if machine_guid is not None else _read_machine_guid()
    name = node if node is not None else platform_module.node()
    material = f"{guid or 'no-guid'}|{name or 'no-node'}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _read_machine_guid() -> str | None:
    if os.name != "nt":
        return None
    try:
        import winreg  # noqa: PLC0415 - Windows only, imported where used
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Cryptography", 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
    except OSError:
        return None
    return value if isinstance(value, str) else None


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderLane:
    """What a live provider run actually demonstrated."""

    verified: bool = False
    providers: tuple[str, ...] = ()
    portability_observed: bool = False
    refresh_behaviour_observed: bool = False
    detail: str = ""

    @property
    def observed(self) -> bool:
        """Both live observations were made, for at least one provider.

        Kept separate from :meth:`enabled_for` because they answer different
        questions. This one gates the provisioning rung: is the lane proven at
        all on this machine. That one gates a single job: is it proven for the
        provider that job actually wants.
        """

        return (self.verified and self.portability_observed
                and self.refresh_behaviour_observed and bool(self.providers))

    def enabled_for(self, provider: str) -> bool:
        return self.observed and provider in self.providers

    def as_dict(self) -> dict[str, Any]:
        return {"verified": self.verified, "providers": list(self.providers),
                "portability_observed": self.portability_observed,
                "refresh_behaviour_observed": self.refresh_behaviour_observed,
                "detail": self.detail}


@dataclass(frozen=True)
class Evidence:
    recorded_at: str
    host_fingerprint: str
    wsl_version: str
    rootfs_sha256: str
    guest_runner_sha256: str
    canaries: tuple[str, ...]
    boundary_verified: bool
    provider_lane: ProviderLane

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "recorded_at": self.recorded_at,
            "host_fingerprint": self.host_fingerprint,
            "platform": "windows",
            "wsl_version": self.wsl_version,
            "rootfs_sha256": self.rootfs_sha256,
            "guest_runner_sha256": self.guest_runner_sha256,
            "canaries": list(self.canaries),
            "boundary_verified": self.boundary_verified,
            "provider_lane": self.provider_lane.as_dict(),
        }


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise EvidenceError(code)


def parse_evidence(raw: Any) -> Evidence:
    """Strictly parse a record. Every failure is a fixed, path-free code."""

    _require(isinstance(raw, Mapping), "evidence_not_an_object")
    _require(set(raw) == EVIDENCE_KEYS, "evidence_keys_invalid")
    _require(raw["schema_version"] == SCHEMA_VERSION, "evidence_schema_unsupported")
    _require(raw["platform"] == "windows", "evidence_platform_invalid")

    recorded_at = raw["recorded_at"]
    _require(isinstance(recorded_at, str) and bool(_TIMESTAMP_RE.match(recorded_at)),
             "evidence_timestamp_invalid")

    fingerprint = raw["host_fingerprint"]
    _require(isinstance(fingerprint, str) and bool(_SHA256_RE.match(fingerprint)),
             "evidence_fingerprint_invalid")

    for field in ("rootfs_sha256", "guest_runner_sha256"):
        value = raw[field]
        _require(isinstance(value, str) and bool(_SHA256_RE.match(value)),
                 f"evidence_{field}_invalid")

    wsl_version = raw["wsl_version"]
    _require(isinstance(wsl_version, str) and 0 < len(wsl_version) <= 64,
             "evidence_wsl_version_invalid")

    canaries = raw["canaries"]
    _require(isinstance(canaries, list)
             and all(isinstance(name, str) for name in canaries),
             "evidence_canaries_invalid")

    _require(isinstance(raw["boundary_verified"], bool),
             "evidence_boundary_invalid")

    lane_raw = raw["provider_lane"]
    _require(isinstance(lane_raw, Mapping) and set(lane_raw) == PROVIDER_KEYS,
             "evidence_provider_lane_invalid")
    providers = lane_raw["providers"]
    _require(isinstance(providers, list)
             and all(isinstance(name, str) for name in providers),
             "evidence_provider_lane_invalid")
    for flag in ("verified", "portability_observed", "refresh_behaviour_observed"):
        _require(isinstance(lane_raw[flag], bool), "evidence_provider_lane_invalid")
    detail = lane_raw["detail"]
    _require(isinstance(detail, str) and len(detail) <= 512,
             "evidence_provider_lane_invalid")

    return Evidence(
        recorded_at=recorded_at,
        host_fingerprint=fingerprint,
        wsl_version=wsl_version,
        rootfs_sha256=raw["rootfs_sha256"],
        guest_runner_sha256=raw["guest_runner_sha256"],
        canaries=tuple(canaries),
        boundary_verified=bool(raw["boundary_verified"]),
        provider_lane=ProviderLane(
            verified=bool(lane_raw["verified"]),
            providers=tuple(providers),
            portability_observed=bool(lane_raw["portability_observed"]),
            refresh_behaviour_observed=bool(lane_raw["refresh_behaviour_observed"]),
            detail=detail),
    )


def check_evidence(evidence: Evidence, *, expected_rootfs_sha256: str,
                   expected_runner_sha256: str,
                   required_canaries: tuple[str, ...],
                   fingerprint: str | None = None) -> None:
    """Refuse a record that is not about this machine, or not about this image."""

    current = fingerprint if fingerprint is not None else host_fingerprint()
    _require(evidence.host_fingerprint == current, "evidence_other_machine")
    _require(evidence.rootfs_sha256 == expected_rootfs_sha256,
             "evidence_image_changed")
    _require(evidence.guest_runner_sha256 == expected_runner_sha256,
             "evidence_runner_changed")
    missing = [name for name in required_canaries if name not in evidence.canaries]
    _require(not missing, "evidence_canaries_incomplete")
    _require(evidence.boundary_verified, "evidence_boundary_not_verified")


# ---------------------------------------------------------------------------
# Reading and writing
# ---------------------------------------------------------------------------


#: Privacy refusals, mapped into this module's vocabulary. The evidence gate
#: is itself a security authority: whatever it says is trusted decides whether
#: the provider lane and the whole delegation path open. A record anyone could
#: have written is not evidence, so a protection failure is a refusal with its
#: own code rather than a warning beside a record that is used anyway.
_PRIVACY_REASONS = {
    "directory_absent": "evidence_root_absent",
    "acl_unenforceable": "evidence_acl_unenforceable",
    "acl_unverified": "evidence_acl_unverified",
    "directory_not_owner_only": "evidence_root_not_owner_only",
    "file_not_owner_only": "evidence_not_owner_only",
    "file_not_regular": "evidence_not_a_regular_file",
    "path_traverses_a_link": "evidence_path_traverses_a_link",
    "path_unreadable": "evidence_unreadable",
    "file_absent": "evidence_absent",
    "file_unreadable": "evidence_unreadable",
    "secure_writer_required": "evidence_acl_unenforceable",
    "replace_failed": "evidence_unwritable",
    "replace_identity_changed": "evidence_changed_while_writing",
    "file_changed_while_reading": "evidence_changed_while_reading",
    "file_too_large": "evidence_malformed",
}


def _as_evidence_error(exc: wpv.PrivacyError) -> EvidenceError:
    return EvidenceError(_PRIVACY_REASONS.get(exc.reason, "evidence_unreadable"))


def read_evidence(path: str, *, runtime_root: str | None = None,
                  platform: Any = None) -> Evidence:
    """Read a record only after proving it is one this owner alone controls.

    Order matters. The ACL on the runtime root and on the record itself, the
    regular-file check, and the ancestry check all happen before the content
    is parsed, and the file's identity is re-checked afterwards. A record that
    was replaced between the check and the read is refused rather than
    believed, because the whole point of this file is that something else
    trusts what it says.
    """

    target = Path(path)
    root = Path(runtime_root) if runtime_root is not None else target.parent
    try:
        wpv.require_private_directory(root, root=root, platform=platform)
        payload, _identity = wpv.read_private_file(target, root=root,
                                                   platform=platform)
    except wpv.PrivacyError as exc:
        raise _as_evidence_error(exc) from exc
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise EvidenceError("evidence_malformed") from exc
    return parse_evidence(raw)


def read_evidence_with_identity(path: str, *, runtime_root: str | None = None,
                                platform: Any = None
                                ) -> tuple[Evidence, wpv.FileIdentity]:
    """The record and the identity it was read from, for a read-modify-write.

    The provider lane is recorded by amending an existing boundary record, and
    an amendment written from a record somebody else has since replaced would
    discard their write. The identity travels to
    :func:`wpv.atomic_private_write`, which refuses the replace if the file on
    disk is no longer the one that was read.
    """

    target = Path(path)
    root = Path(runtime_root) if runtime_root is not None else target.parent
    try:
        wpv.require_private_directory(root, root=root, platform=platform)
        payload, identity = wpv.read_private_file(target, root=root,
                                                  platform=platform)
    except wpv.PrivacyError as exc:
        raise _as_evidence_error(exc) from exc
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise EvidenceError("evidence_malformed") from exc
    return parse_evidence(raw), identity


def load_verified_state(path: str, *, expected_rootfs_sha256: str,
                        expected_runner_sha256: str,
                        required_canaries: tuple[str, ...],
                        fingerprint: str | None = None,
                        runtime_root: str | None = None,
                        platform: Any = None,
                        ) -> tuple[wp.ProvisionState, Evidence | None, str]:
    """The production path: a state that is ready only on recorded evidence.

    Returns ``(state, evidence, reason)``. On any failure the state is the
    empty, fail-closed one and ``reason`` says which check refused, so the
    caller can report why delegation is off instead of reporting that it is
    off for no stated reason.
    """

    try:
        evidence = read_evidence(path, runtime_root=runtime_root,
                                 platform=platform)
        check_evidence(evidence, expected_rootfs_sha256=expected_rootfs_sha256,
                       expected_runner_sha256=expected_runner_sha256,
                       required_canaries=required_canaries,
                       fingerprint=fingerprint)
    except EvidenceError as exc:
        return (wp.ProvisionState(is_windows=os.name == "nt"), None, str(exc))
    state = wp.ProvisionState(
        is_windows=True, windows_build_ok=True, firmware_virtualization=True,
        features_enabled={name: True for name in wp.REQUIRED_FEATURES},
        reboot_pending=False, wsl_present=True, wsl_version_ok=True,
        guest_image_installed=True, boundary_verified=True,
        # Read back off the record, not carried over from the run that made
        # the observation. A lane is open because the durable, machine-bound
        # record says so, or it is not open.
        provider_lane_verified=evidence.provider_lane.observed or None)
    return (state, evidence, "")


def record_verification(path: str, evidence: Evidence, *,
                        os_name: str | None = None,
                        secure: Any = None,
                        platform: Any = None,
                        expect_identity: wpv.FileIdentity | None = None) -> None:
    """Write a verification record. Native Windows only, owner-only on disk.

    The platform check is the gate that keeps a portable test from marking a
    machine ready: a test can build an :class:`Evidence` object, but it cannot
    make this function write one anywhere the production loader will read.

    Protection is mandatory, not optional. There is no argument that turns it
    off, because the caller most likely to want one is a test, and a test that
    can write an unprotected record can write a record the loader trusts.

    The write is atomic and never truncates what is already there. If the ACL
    cannot be established, or the bytes cannot be written, the previous record
    survives untouched: losing a good record to a failed write would take
    delegation offline and look exactly like tampering.
    """

    if (os_name if os_name is not None else os.name) != "nt":
        raise EvidenceError("evidence_requires_windows")
    target = Path(path)
    try:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise EvidenceError("evidence_unwritable") from exc
    writer = secure if secure is not None else wpv.platform_secure_writer(platform)
    payload = json.dumps(evidence.as_dict(), indent=2, sort_keys=True) + "\n"
    try:
        wpv.require_private_directory(target.parent, root=target.parent,
                                      platform=platform)
        wpv.atomic_private_write(target, payload.encode("utf-8"),
                                 secure=writer, root=target.parent,
                                 expect_identity=expect_identity)
    except wpv.PrivacyError as exc:
        raise _as_evidence_error(exc) from exc
    except OSError as exc:
        raise EvidenceError("evidence_unwritable") from exc


LIVE_EVIDENCE_LIMITATION = (
    "no verification record has been produced on a live Windows host from this "
    "worktree; every test here exercises the refusal paths, and the only way "
    "to reach the ready state in production is a record written by "
    "record_verification on the machine it describes"
)

PROVIDER_LANE_LIMITATION = (
    "the provider lane needs its own recorded observation on top of a verified "
    "boundary: that a subscription session minted on the host is accepted from "
    "inside the guest (portability) and that a session expiring mid-job "
    "behaves predictably when the capsule is discarded (refresh). Neither has "
    "been observed, so the lane is off"
)
