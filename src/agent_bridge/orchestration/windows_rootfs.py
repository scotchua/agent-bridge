"""Reproducible pinned rootfs build plan and manifest workflow.

The Windows runtime imports a caller-supplied rootfs tarball and refuses to
run unless its SHA-256 matches a pinned manifest. This module is where that
tarball and that manifest come from. It is deliberately split so that almost
all of it is pure and testable on any platform:

* the **recipe** (base image digest, package versions, file layout) is data;
* the **build plan** (the exact container argv and Dockerfile text) is a pure
  function of the recipe;
* the **normalisation** that makes the exported tar reproducible is a pure
  transformation over tar members;
* only the thin CLI in ``tools/build_windows_rootfs.py`` actually runs a
  container engine or writes bytes.

No image is committed to git. A rootfs tarball is hundreds of megabytes and
would be a new copy in history on every rebuild; what is committed is the
recipe and the expected hashes, which is what anybody verifying the image
actually needs.

Architecture is explicit everywhere. Windows on ARM needs an ``arm64`` guest
and x64 Windows needs ``amd64``; an image built for the wrong one imports
successfully and then fails to execute anything, so the architecture is part
of the recipe, part of the output filename, and part of the manifest sidecar.

Reproducibility is claimed only as far as it actually holds: see
:data:`REPRODUCIBILITY_LIMITATION`. Nothing here has been run against a live
Windows host or validated as producing a working WSL2 image.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import tarfile
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Mapping

from . import signing
from . import windows_wsl as ww

SCHEMA_VERSION = 1

#: The two architectures a Windows host can need. Not a free-form string:
#: an unrecognised value would produce an image that imports and then cannot
#: execute a single byte of what it contains.
ARCHITECTURES = ("amd64", "arm64")

#: Guest paths the recipe writes. Fixed, because the guest runner and the
#: host canaries both depend on them being exactly these.
GUEST_RUNNER_PATH = ww.GUEST_RUNNER_PATH
WSL_CONF_PATH = "/etc/wsl.conf"
VERSIONS_PATH = "/etc/agent-bridge/versions.json"
WORKSPACE_PATHS = ("/workspace", "/workspace/canary", "/workspace/job")

#: A fixed timestamp for every archive member. Build time is the single
#: largest source of difference between two otherwise identical images.
SOURCE_DATE_EPOCH = 0

#: Everything in the image is owned by root and nothing is setuid. Normalising
#: this is not cosmetic: a stray setuid bit in a guest is a privilege boundary.
NORMALISED_UID = 0
NORMALISED_GID = 0

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def is_sha256_hex(value: object) -> bool:
    """One place that decides what a digest looks like."""

    return isinstance(value, str) and bool(_SHA256_RE.match(value))
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
#: A Debian package name followed by an exact version. Not a loose "contains
#: an equals sign": "curl>=1.0" contains one and pins nothing.
_APT_PIN_RE = re.compile(r"^[a-z0-9][a-z0-9+.\-]*=[^\s=]+$")


class RootfsRecipeError(ValueError):
    """The recipe is malformed or not fully pinned."""


@dataclass(frozen=True)
class ToolPin:
    """One tool, pinned by version and by the hash of what gets installed."""

    version: str
    path: str
    sha256: str


@dataclass(frozen=True)
class RootfsRecipe:
    """Everything needed to build one architecture's image, fully pinned.

    Every field is a pin. There is deliberately no "latest", no floating tag,
    and no package name without a version: an image whose contents depend on
    when it was built cannot be described by a manifest, and a manifest that
    does not describe the image is worse than none.
    """

    architecture: str
    base_image: str
    base_digest: str
    distro_release: str
    node_version: str
    claude_version: str
    codex_version: str
    #: sha256 of the official Node tarball for this exact version and
    #: architecture, taken from a release the operator verified out of band.
    #: Not the SHASUMS256.txt served alongside the download: a checksum file
    #: fetched from the same origin over the same connection is learned after
    #: the download and proves only that the two agree.
    node_tarball_sha256: str = ""
    #: npm integrity strings, "sha512-<base64>", for the exact provider
    #: package tarballs. The registry publishes these; they are recorded here
    #: so the build verifies the bytes it fetched against a value that was in
    #: the repository beforehand.
    claude_integrity: str = ""
    codex_integrity: str = ""
    apt_packages: tuple[str, ...] = ()


def validate_recipe(recipe: RootfsRecipe) -> RootfsRecipe:
    if recipe.architecture not in ARCHITECTURES:
        raise RootfsRecipeError("architecture must be one of " + ", ".join(ARCHITECTURES))
    if not isinstance(recipe.base_image, str) or not recipe.base_image:
        raise RootfsRecipeError("base_image must be a non-empty string")
    if ":" in recipe.base_image or "@" in recipe.base_image:
        # The tag is not the pin; the digest is. Allowing a tag here would let
        # two builds of the same recipe start from different bytes.
        raise RootfsRecipeError("base_image must not carry a tag or digest")
    if not _DIGEST_RE.match(recipe.base_digest or ""):
        raise RootfsRecipeError("base_digest must be sha256:<64 lowercase hex>")
    for name in ("distro_release", "node_version", "claude_version", "codex_version"):
        value = getattr(recipe, name)
        try:
            ww._reject_unpinned_version(name, value)
        except ww.ManifestError as exc:
            raise RootfsRecipeError(str(exc)) from exc
    for package in recipe.apt_packages:
        if not isinstance(package, str) or _APT_PIN_RE.match(package) is None:
            raise RootfsRecipeError(
                "every apt package must be pinned as name=version: " + repr(package))
    _require_download_integrity(recipe)
    _reject_placeholders(recipe)
    _require_runtime_packages(recipe)
    return recipe


#: npm's integrity format. Anchored, because a value that merely contains this
#: shape somewhere is a value nobody checked.
_INTEGRITY_RE = re.compile(r"^sha512-[A-Za-z0-9+/]{86}==$")


def _require_download_integrity(recipe: RootfsRecipe) -> None:
    """Every download must be checked against a value that was here first.

    Versions pin *what* to fetch. These pin *which bytes*, which is the part
    that matters when the thing being fetched is executed as root during the
    build. Without them the build trusts whatever the network returned, and a
    checksum served from the same origin is not independent of it.
    """

    if not _SHA256_RE.match(recipe.node_tarball_sha256 or ""):
        raise RootfsRecipeError(
            "node_tarball_sha256 must be the 64-hex sha256 of the official "
            "Node tarball for this version and architecture")
    for name in ("claude_integrity", "codex_integrity"):
        value = getattr(recipe, name)
        if not _INTEGRITY_RE.match(value or ""):
            raise RootfsRecipeError(
                f"{name} must be the npm integrity string, sha512-<base64>, "
                "for the exact pinned package tarball")


#: A digest of all zeros is not a digest. It is a slot someone left for a real
#: one, and a recipe carrying it must not look buildable: a build started from
#: it would either fail at the registry or, worse, be "fixed" by dropping the
#: digest and pulling a tag.
PLACEHOLDER_DIGEST = "sha256:" + "0" * 64

#: Versions that are obviously stand-ins rather than observed releases.
PLACEHOLDER_VERSIONS = {
    "claude_version": ("1.0.0",),
    "codex_version": ("0.1.0",),
}

#: Package names the guest cannot work without, whatever else a recipe adds.
#: ``nftables`` provides /usr/sbin/nft, which the network-egress canary
#: applies and reads back; ``git`` provides /usr/bin/git, which the guest uses
#: to take a baseline commit and produce the returned diff. An image missing
#: either would import fine and then fail a canary inside the guest, which is
#: a much worse place to discover it.
REQUIRED_APT_PACKAGES = ("nftables", "git", "ca-certificates")


def _reject_placeholders(recipe: RootfsRecipe) -> None:
    if recipe.base_digest == PLACEHOLDER_DIGEST:
        raise RootfsRecipeError(
            "base_digest is the all-zero placeholder; supply the digest of the "
            "base image you actually intend to build from")
    for field, placeholders in PLACEHOLDER_VERSIONS.items():
        if getattr(recipe, field) in placeholders:
            raise RootfsRecipeError(
                f"{field} is a placeholder, not an observed release version")


def _require_runtime_packages(recipe: RootfsRecipe) -> None:
    present = {package.split("=", 1)[0] for package in recipe.apt_packages}
    missing = [name for name in REQUIRED_APT_PACKAGES if name not in present]
    if missing:
        raise RootfsRecipeError(
            "recipe omits packages the guest depends on: " + ", ".join(missing))


def base_reference(recipe: RootfsRecipe) -> str:
    """The digest-pinned reference a container engine should pull."""

    validate_recipe(recipe)
    return f"{recipe.base_image}@{recipe.base_digest}"


# ---------------------------------------------------------------------------
# Build plan
# ---------------------------------------------------------------------------


def dockerfile(recipe: RootfsRecipe, *, guest_runner_sha256: str) -> str:
    """The exact build recipe, as text, derived only from the pins.

    Written out rather than assembled at build time so that a reviewer can
    read the whole thing, and so the tests can assert the security-relevant
    lines are present. The guest runner's hash is checked inside the build:
    an image that shipped a different runner would fail the host's canary
    much later, with far less to go on.
    """

    validate_recipe(recipe)
    if not _SHA256_RE.match(guest_runner_sha256 or ""):
        raise RootfsRecipeError("guest_runner_sha256 must be 64 lowercase hex")

    # Not conditional: validate_recipe refuses a recipe without the packages
    # the guest depends on, so there is no such thing as an image with no apt
    # step, and pretending otherwise left a branch nothing could reach.
    packages = " ".join(recipe.apt_packages)
    install_packages = (
        f"RUN apt-get update \\\n"
        f" && apt-get install -y --no-install-recommends {packages} \\\n"
        f" && rm -rf /var/lib/apt/lists/*\n")

    return f"""# syntax=docker/dockerfile:1
# Generated from a pinned RootfsRecipe. Do not edit by hand: rebuild instead.
FROM {base_reference(recipe)}

# Fail the build, not a later canary, if the runner is not the pinned file.
COPY agent-bridge-guest-runner {GUEST_RUNNER_PATH}
RUN test "$(sha256sum {GUEST_RUNNER_PATH} | cut -d' ' -f1)" = "{guest_runner_sha256}" \\
 && chmod 0755 {GUEST_RUNNER_PATH}

# The guest security boundary. The host hashes this file and refuses to run a
# job if it differs by a single byte, so it is written here and never edited.
COPY wsl.conf {WSL_CONF_PATH}
RUN chmod 0644 {WSL_CONF_PATH}

{install_packages}\
# Pinned toolchain. Versions are fixed by the recipe, never resolved at build
# time, so two builds of one recipe install the same programs. The installer
# script comes from the build context and is removed again, so it is not part
# of the shipped image and cannot change its hash later.
COPY install-pinned-tools /tmp/install-pinned-tools
RUN chmod 0755 /tmp/install-pinned-tools \\
 && /tmp/install-pinned-tools \\
      --node {recipe.node_version} \\
      --node-sha256 {recipe.node_tarball_sha256} \\
      --claude {recipe.claude_version} \\
      --claude-integrity {recipe.claude_integrity} \\
      --codex {recipe.codex_version} \\
      --codex-integrity {recipe.codex_integrity} \\
      --arch {recipe.architecture} \\
 && rm -f /tmp/install-pinned-tools

# install-pinned-tools writes {VERSIONS_PATH} itself, hashing each
# binary it just installed. Copying a hand-written inventory in would let the
# image claim contents it does not have; generating it from what was installed
# means the versions canary proves the binaries rather than reciting a string.

RUN mkdir -p {" ".join(WORKSPACE_PATHS)} && chmod 0755 {" ".join(WORKSPACE_PATHS)}

# No network, no credentials, no host paths are baked in, and nothing here
# authenticates a provider CLI. See guest_runner.AUTHENTICATION_BLOCKER.
"""


def build_argv(recipe: RootfsRecipe, *, context_dir: str, image_tag: str,
               engine: str = "docker") -> list[str]:
    """Container build argv. Fixed shape, no shell, explicit platform."""

    validate_recipe(recipe)
    if engine not in ("docker", "podman"):
        raise RootfsRecipeError("engine must be docker or podman")
    if not image_tag or any(character.isspace() for character in image_tag):
        raise RootfsRecipeError("image_tag must be a non-empty whitespace-free string")
    return [
        engine, "build",
        "--platform", f"linux/{recipe.architecture}",
        # Reproducibility: no cache reuse across recipes, and a fixed build
        # timestamp so two builds of one recipe differ by as little as possible.
        "--build-arg", f"SOURCE_DATE_EPOCH={SOURCE_DATE_EPOCH}",
        "--pull",
        "--file", posixpath.join(context_dir, "Dockerfile"),
        "--tag", image_tag,
        context_dir,
    ]


def export_argv(image_tag: str, *, engine: str = "docker",
                container_name: str = "agent-bridge-rootfs-export") -> list[list[str]]:
    """Create, export and remove a throwaway container, in that order.

    ``export`` rather than ``save``: WSL imports a filesystem tarball, not an
    OCI layer archive, and handing ``wsl --import`` a layer archive produces a
    distro whose root directory is a set of layer blobs.
    """

    if engine not in ("docker", "podman"):
        raise RootfsRecipeError("engine must be docker or podman")
    return [
        [engine, "create", "--name", container_name, image_tag],
        [engine, "export", container_name],
        [engine, "rm", "--force", container_name],
    ]


def output_names(recipe: RootfsRecipe) -> dict[str, str]:
    """Architecture is in every filename, so two images cannot be confused."""

    validate_recipe(recipe)
    return {
        "rootfs": f"agent-bridge-rootfs-{recipe.architecture}.tar",
        "manifest": f"agent-bridge-manifest-{recipe.architecture}.json",
        "sidecar": f"agent-bridge-rootfs-{recipe.architecture}.tar.sha256",
    }


# ---------------------------------------------------------------------------
# Tar normalisation
# ---------------------------------------------------------------------------


def normalise_member(member: tarfile.TarInfo) -> tarfile.TarInfo:
    """Strip everything that varies between two identical builds.

    Timestamps, uid/gid and owner names are build-machine facts, not image
    facts. Clearing them is what makes two builds of one recipe hash the same.
    The setuid/setgid bits are cleared for a different reason: nothing in this
    image needs them, and a guest that ships one is a guest with a privilege
    boundary nobody reviewed.
    """

    member.mtime = SOURCE_DATE_EPOCH
    member.uid = NORMALISED_UID
    member.gid = NORMALISED_GID
    member.uname = ""
    member.gname = ""
    member.pax_headers = {}
    member.mode &= 0o7777 & ~0o6000
    return member


def normalised_members(members: Iterable[tarfile.TarInfo]) -> list[tarfile.TarInfo]:
    """Normalised and sorted by name, so member order cannot vary."""

    return sorted((normalise_member(member) for member in members),
                  key=lambda member: member.name)


def normalise_tar(source_path: str, destination_path: str) -> str:
    """Rewrite a tarball in normalised form and return its SHA-256.

    The hash is computed from the file that was actually written, not from the
    bytes on the way past: the manifest pins what is on disk, and a hash taken
    from a stream would still match if the write were short.
    """

    # Seekable, not streaming: members are written in sorted order, and
    # sorting means reading a member's payload after its header has gone past.
    with tarfile.open(source_path, "r:*") as source:
        members = sorted(source.getmembers(), key=lambda member: member.name)
        with tarfile.open(destination_path, "w", format=tarfile.GNU_FORMAT) as out:
            for member in members:
                handle = source.extractfile(member) if member.isreg() else None
                out.addfile(normalise_member(member), handle)
    return sha256_path(destination_path)


def sha256_path(path: str, chunk_bytes: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_bytes)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Manifest and its sidecar
# ---------------------------------------------------------------------------


def build_manifest(recipe: RootfsRecipe, *, rootfs_sha256: str) -> dict[str, Any]:
    """The manifest the Windows runtime consumes.

    Deliberately exactly the keys :func:`windows_wsl.parse_manifest` accepts
    and no more: that parser rejects unknown keys, so anything extra here
    would produce a manifest this project's own runtime refuses.
    """

    validate_recipe(recipe)
    if not _SHA256_RE.match(rootfs_sha256 or ""):
        raise RootfsRecipeError("rootfs_sha256 must be 64 lowercase hex")
    manifest = {
        "schema_version": 1,
        "distro_release": recipe.distro_release,
        "rootfs_sha256": rootfs_sha256,
        "node_version": recipe.node_version,
        "claude_version": recipe.claude_version,
        "codex_version": recipe.codex_version,
    }
    # Parse it here so a manifest this project cannot read is never written.
    ww.parse_manifest(manifest)
    return manifest


def build_sidecar(recipe: RootfsRecipe, *, rootfs_sha256: str,
                  guest_runner_sha256: str, built_at: str,
                  tool_hashes: Mapping[str, str],
                  manifest: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Provenance the manifest has no room for.

    The manifest's shape is fixed by the runtime's strict parser, which is
    right: it is a security input. Everything a human needs in order to
    rebuild and compare goes here instead, beside it, where a loose schema
    costs nothing.
    """

    validate_recipe(recipe)
    if not _SHA256_RE.match(guest_runner_sha256 or ""):
        raise RootfsRecipeError("guest_runner_sha256 must be 64 lowercase hex")
    for name, digest in tool_hashes.items():
        if not _SHA256_RE.match(digest or ""):
            raise RootfsRecipeError(f"tool hash for {name} must be 64 lowercase hex")
    return {
        "schema_version": SCHEMA_VERSION,
        "architecture": recipe.architecture,
        "base_image": recipe.base_image,
        "base_digest": recipe.base_digest,
        "apt_packages": list(recipe.apt_packages),
        "rootfs_sha256": rootfs_sha256,
        "guest_runner_sha256": guest_runner_sha256,
        "wsl_conf_sha256": hashlib.sha256(
            ww.WSL_CONF_CONTENTS.encode("utf-8")).hexdigest(),
        "tool_sha256": dict(sorted(tool_hashes.items())),
        "built_at": built_at,
        "source_date_epoch": SOURCE_DATE_EPOCH,
        # The digest a release would anchor trust to. Recorded by the build so
        # the value a maintainer signs is the one the build actually produced,
        # not one re-derived later from a manifest somebody re-serialised.
        "manifest_sha256": manifest_digest(manifest) if manifest is not None else "",
        "release_blockers": list(release_blockers({recipe.architecture: recipe})),
    }


def build_versions_file(recipe: RootfsRecipe,
                        tools: Mapping[str, ToolPin]) -> dict[str, Any]:
    """The shape of the inventory the image writes to :data:`VERSIONS_PATH`.

    The real file is generated inside the build by ``install-pinned-tools``,
    which hashes each binary it just installed. This function is the canonical
    definition of that shape, shared by the tests that hold the builder and
    the guest runner to the same contract.
    """

    validate_recipe(recipe)
    expected = {"node", "claude", "codex"}
    if set(tools) != expected:
        raise RootfsRecipeError("tools must be exactly " + ", ".join(sorted(expected)))
    entries = {}
    for name, pin in tools.items():
        if not _SHA256_RE.match(pin.sha256 or ""):
            raise RootfsRecipeError(f"{name} sha256 must be 64 lowercase hex")
        if not pin.path.startswith("/"):
            raise RootfsRecipeError(f"{name} path must be absolute")
        entries[name] = {"version": pin.version, "path": pin.path,
                         "sha256": pin.sha256}
    return {"schema_version": 1, "tools": entries}


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImageCheck:
    passed: bool
    reason: str


def verify_image(*, manifest: Mapping[str, Any], observed_sha256: str,
                 sidecar: Mapping[str, Any] | None = None,
                 host_architecture: str | None = None) -> ImageCheck:
    """Check an installed image against its manifest, fail closed.

    Three separate questions, because they fail differently: is the manifest
    readable at all, is the tarball the one it describes, and is it for this
    machine's architecture. An image that passes the first two and fails the
    third imports fine and then cannot run a single program in the guest.
    """

    try:
        parsed = ww.parse_manifest(manifest)
    except ww.ManifestError as exc:
        return ImageCheck(False, f"manifest_invalid:{type(exc).__name__}")
    if not _SHA256_RE.match(observed_sha256 or ""):
        return ImageCheck(False, "observed_hash_invalid")
    if parsed.rootfs_sha256 != observed_sha256:
        return ImageCheck(False, "rootfs_hash_mismatch")
    if sidecar is not None:
        architecture = sidecar.get("architecture")
        if architecture not in ARCHITECTURES:
            return ImageCheck(False, "sidecar_architecture_invalid")
        if sidecar.get("rootfs_sha256") != observed_sha256:
            return ImageCheck(False, "sidecar_hash_mismatch")
        if host_architecture is not None and architecture != host_architecture:
            return ImageCheck(False, "architecture_mismatch")
    elif host_architecture is not None:
        # No sidecar means no architecture statement, and guessing is how an
        # arm64 image ends up installed on an x64 machine.
        return ImageCheck(False, "architecture_unknown")
    return ImageCheck(True, "ok")


# ---------------------------------------------------------------------------
# Release trust
# ---------------------------------------------------------------------------

#: The canonical byte form a manifest is trusted *as*. A manifest is a small
#: JSON object, and "the same manifest" has to mean the same bytes, not the
#: same dict with different spacing.
def canonical_manifest_bytes(manifest: Mapping[str, Any]) -> bytes:
    """Serialise a manifest the one way every trust decision uses."""

    return json.dumps(dict(manifest), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")


def manifest_digest(manifest: Mapping[str, Any]) -> str:
    """sha256 of the canonical manifest bytes."""

    return hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest()


@dataclass(frozen=True)
class TrustAnchor:
    """One release's signed statement about one architecture's manifest.

    ``manifest_sha256`` is what makes this release-bound: it names the exact
    manifest that release shipped, so a manifest that arrived by any other
    route is recognisably not that one.

    ``signature`` and ``public_key`` are not optional in practice, whatever
    their defaults say. The defaults exist so a malformed anchor can be
    *constructed* and then refused by name, which is how the regressions here
    prove that an unsigned anchor cannot be trusted. An anchor missing either
    field never returns trusted from :func:`verify_manifest_trust`.

    The digest alone is deliberately not enough. A digest says "this is the
    manifest somebody pinned"; whoever could edit the pinned digest could edit
    the manifest to match it. The signature is what makes the statement come
    from the holder of the release key rather than from whoever last wrote to
    this file, and :mod:`agent_bridge.orchestration.signing` actually checks
    it rather than carrying it as decoration.
    """

    release: str
    architecture: str
    manifest_sha256: str
    signature: str = ""
    public_key: str = ""

    @property
    def signed(self) -> bool:
        """Both halves present. Neither is any use without the other."""

        return bool(self.signature.strip()) and bool(self.public_key.strip())


#: Empty on purpose. No release of this project has published a signed
#: manifest, so there is nothing here to anchor trust to, and
#: :func:`verify_manifest_trust` refuses every manifest by name. Adding an
#: entry is a release action, not a code change made to get a test to pass.
RELEASE_TRUST_ANCHORS: tuple[TrustAnchor, ...] = ()


def verify_manifest_trust(manifest: Mapping[str, Any], *, architecture: str,
                          release: str | None = None,
                          anchors: Iterable[TrustAnchor] | None = None,
                          verify_signature: Any = None) -> ImageCheck:
    """Is this manifest the one a named release actually published, and signed?

    A hash pinned in a manifest answers "are these the bytes the manifest
    describes". This answers the prior question, "and who said that manifest
    was right", which a hash the manifest carries about itself cannot.

    Four things must all hold, and each failure has its own name so an
    operator is told which one: an anchor exists for this architecture and
    release, its digest matches these exact canonical bytes, it carries both
    a signature and the public key that signature must verify under, and the
    signature actually verifies. ``verify_signature`` defaults to the Ed25519
    check in :mod:`agent_bridge.orchestration.signing`; it is a parameter so
    a release using a different scheme can supply its own, not so a caller
    can supply one that says yes.
    """

    candidates = tuple(RELEASE_TRUST_ANCHORS if anchors is None else anchors)
    if not candidates:
        return ImageCheck(False, "manifest_untrusted:no_release_anchor")
    if architecture not in ARCHITECTURES:
        return ImageCheck(False, "manifest_untrusted:architecture_invalid")
    matching = [anchor for anchor in candidates
                if anchor.architecture == architecture
                and (release is None or anchor.release == release)]
    if not matching:
        return ImageCheck(False, "manifest_untrusted:no_anchor_for_release")
    observed = manifest_digest(manifest)
    anchor = next((one for one in matching
                   if one.manifest_sha256 == observed), None)
    if anchor is None:
        return ImageCheck(False, "manifest_untrusted:digest_mismatch")

    # A digest match is not trust. Whoever could rewrite the pinned digest
    # could rewrite the manifest to match it, so the signature is the only
    # part of this that an attacker with write access here cannot forge.
    if not anchor.signature.strip():
        return ImageCheck(False, "manifest_untrusted:signature_missing")
    if not anchor.public_key.strip():
        return ImageCheck(False, "manifest_untrusted:public_key_missing")

    verifier = verify_signature if verify_signature is not None else signing.verify_hex
    try:
        accepted = bool(verifier(canonical_manifest_bytes(manifest),
                                 anchor.signature, anchor.public_key))
    except Exception:  # a verifier that raises has not accepted anything
        return ImageCheck(False, "manifest_untrusted:signature_error")
    if not accepted:
        return ImageCheck(False, "manifest_untrusted:signature_invalid")
    return ImageCheck(True, "ok")


#: The one sentence to quote when somebody asks whether the image workflow is
#: shippable. Kept next to the check that proves it, so the two cannot drift.
ARTIFACT_WORKFLOW_BLOCKER = (
    "the rootfs artifact workflow is unreleasable: no release has published a "
    "signed, release-bound manifest to anchor trust to, and the shipped "
    "recipes are unfilled stubs rather than observed pins")


def release_blockers(recipes: Mapping[str, RootfsRecipe] | None = None,
                     *, anchors: Iterable[TrustAnchor] | None = None
                     ) -> tuple[str, ...]:
    """Everything that stands between this workflow and a real release.

    Machine-checkable on purpose. A prose caveat in a document is something a
    reader may or may not reach; this is something a test asserts and a build
    can refuse on, and it returns empty only when there is genuinely nothing
    left to state.
    """

    blockers: list[str] = []
    candidates = tuple(RELEASE_TRUST_ANCHORS if anchors is None else anchors)
    if not candidates:
        blockers.append("manifest_trust_anchor_missing")
    else:
        covered = {anchor.architecture for anchor in candidates}
        for architecture in ARCHITECTURES:
            if architecture not in covered:
                blockers.append(f"manifest_trust_anchor_missing:{architecture}")
        for anchor in candidates:
            if not anchor.signature.strip():
                blockers.append(f"manifest_signature_missing:{anchor.release}")
            elif not anchor.public_key.strip():
                blockers.append(f"manifest_public_key_missing:{anchor.release}")
    supplied = dict(recipes or {})
    for architecture in ARCHITECTURES:
        recipe = supplied.get(architecture)
        if recipe is None:
            blockers.append(f"recipe_missing:{architecture}")
            continue
        try:
            validate_recipe(recipe)
        except RootfsRecipeError as exc:
            blockers.append(f"recipe_unbuildable:{architecture}:{exc}")
    return tuple(blockers)


def host_architecture_for(machine: str) -> str | None:
    """Map a platform machine string onto a recipe architecture, or None."""

    normalised = (machine or "").strip().lower()
    if normalised in ("amd64", "x86_64", "x64"):
        return "amd64"
    if normalised in ("arm64", "aarch64"):
        return "arm64"
    return None


# ---------------------------------------------------------------------------
# Limitations, stated rather than papered over
# ---------------------------------------------------------------------------

REPRODUCIBILITY_LIMITATION = (
    "the build is fully pinned (base image by digest, every tool and apt "
    "package by version) and the exported tar is normalised (sorted members, "
    "fixed mtime, root ownership, no setuid), which makes two builds of one "
    "recipe comparable; it is not a bit-for-bit reproducible build, because "
    "apt and npm can still emit machine-specific content inside files, so the "
    "published rootfs hash is the pin and a rebuild is a comparison, not a proof"
)

IMAGE_DISTRIBUTION_LIMITATION = (
    "no rootfs tarball is committed to git; the recipe and the expected hashes "
    "are what is versioned, and an operator either builds the image locally or "
    "obtains it out of band and checks it against the manifest before use"
)

NO_LIVE_VALIDATION = (
    "no image produced by this recipe has been imported into WSL2 on a live "
    "Windows host from this worktree, so nothing here is evidence that the "
    "resulting guest boots or that its canaries pass"
)
