#!/usr/bin/env python3
"""Build one architecture's pinned Agent Bridge rootfs and its manifest.

Thin on purpose. Every decision lives in
``agent_bridge.orchestration.windows_rootfs``, which is pure and tested; this
file assembles a build context, runs a container engine, normalises the
exported tarball and writes the manifest beside it.

    python3 tools/build_windows_rootfs.py --recipe tools/rootfs/recipes/arm64.json \\
        --out-dir build/rootfs

Nothing it writes belongs in git. The output directory is gitignored, and the
tarball is expected to be distributed out of band and checked against the
manifest hash on the machine that uses it.

This has not been run against a live Windows host from this worktree, so a
successful build here is not evidence that the resulting guest boots.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agent_bridge.orchestration import windows_rootfs as wrf  # noqa: E402
from agent_bridge.orchestration import windows_wsl as ww  # noqa: E402

GUEST_RUNNER_SOURCE = REPO / "src/agent_bridge/orchestration/guest_runner.py"
INSTALLER_SOURCE = REPO / "tools/rootfs/install-pinned-tools"

RECIPE_KEYS = {"node_tarball_sha256", "claude_integrity", "codex_integrity",
               "claude_native_integrity", "codex_native_integrity",
               "architecture", "base_image", "base_digest", "distro_release",
               "node_version", "claude_version", "codex_version", "apt_packages"}

#: A free-text note the shipped stub recipes carry. Ignored when building, so
#: a real recipe may keep or drop it, but never silently accepted as a pin.
NOTE_KEY = "_stub"


def load_recipe(path: Path) -> wrf.RootfsRecipe:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = {key: value for key, value in raw.items() if key != NOTE_KEY}
    if not isinstance(raw, dict) or set(raw) != RECIPE_KEYS:
        raise SystemExit(f"recipe keys must be exactly {sorted(RECIPE_KEYS)}")
    recipe = wrf.RootfsRecipe(
        architecture=raw["architecture"],
        base_image=raw["base_image"],
        base_digest=raw["base_digest"],
        distro_release=raw["distro_release"],
        node_version=raw["node_version"],
        claude_version=raw["claude_version"],
        codex_version=raw["codex_version"],
        node_tarball_sha256=raw["node_tarball_sha256"],
        claude_integrity=raw["claude_integrity"],
        codex_integrity=raw["codex_integrity"],
        claude_native_integrity=raw["claude_native_integrity"],
        codex_native_integrity=raw["codex_native_integrity"],
        apt_packages=tuple(raw["apt_packages"]),
    )
    return wrf.validate_recipe(recipe)


def assemble_context(recipe: wrf.RootfsRecipe, context: Path) -> str:
    """Write the build context and return the guest runner's SHA-256."""

    runner_target = context / "agent-bridge-guest-runner"
    shutil.copyfile(GUEST_RUNNER_SOURCE, runner_target)
    runner_sha256 = wrf.sha256_path(str(runner_target))

    shutil.copyfile(INSTALLER_SOURCE, context / "install-pinned-tools")
    (context / "wsl.conf").write_text(ww.WSL_CONF_CONTENTS, encoding="utf-8")
    (context / "Dockerfile").write_text(
        wrf.dockerfile(recipe, guest_runner_sha256=runner_sha256), encoding="utf-8")
    return runner_sha256


def run(argv: list[str], *, stdout=None) -> None:
    print("+ " + " ".join(argv), file=sys.stderr)
    completed = subprocess.run(argv, stdout=stdout, shell=False, check=False)
    if completed.returncode != 0:
        raise SystemExit(f"command failed with exit {completed.returncode}: {argv[0]}")


def tool_hashes_from(tar_path: str) -> dict[str, str]:
    """Read back the inventory the image generated for itself.

    Taken from the built tarball rather than from the recipe: the recipe says
    which versions to install, and only the image can say what installing them
    actually produced.
    """

    member_name = wrf.VERSIONS_PATH.lstrip("/")
    with tarfile.open(tar_path, "r:*") as archive:
        try:
            handle = archive.extractfile(member_name)
        except KeyError:
            handle = None
        if handle is None:
            raise SystemExit(f"built image has no {wrf.VERSIONS_PATH}")
        inventory = json.loads(handle.read().decode("utf-8"))
    tools = inventory.get("tools", {})
    return {name: entry["sha256"] for name, entry in sorted(tools.items())}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recipe", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--engine", default="docker", choices=("docker", "podman"))
    parser.add_argument("--keep-context", action="store_true",
                        help="leave the build context on disk for inspection")
    args = parser.parse_args()

    recipe = load_recipe(args.recipe)
    names = wrf.output_names(recipe)
    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(out_dir, 0o700)

    image_tag = f"agent-bridge-rootfs:{recipe.architecture}"
    context = Path(tempfile.mkdtemp(prefix="agent-bridge-rootfs-context-"))
    try:
        runner_sha256 = assemble_context(recipe, context)
        run(wrf.build_argv(recipe, context_dir=str(context), image_tag=image_tag,
                           engine=args.engine))

        raw_tar = out_dir / (names["rootfs"] + ".raw")
        create, export, remove = wrf.export_argv(image_tag, engine=args.engine)
        run(create)
        try:
            with open(raw_tar, "wb") as handle:
                run(export, stdout=handle)
        finally:
            # The throwaway container is removed whether or not the export
            # worked; a leftover container holds the whole image on disk.
            run(remove)

        rootfs_path = out_dir / names["rootfs"]
        rootfs_sha256 = wrf.normalise_tar(str(raw_tar), str(rootfs_path))
        raw_tar.unlink()

        manifest = wrf.build_manifest(recipe, rootfs_sha256=rootfs_sha256)
        sidecar = wrf.build_sidecar(
            recipe, rootfs_sha256=rootfs_sha256, guest_runner_sha256=runner_sha256,
            built_at=datetime.now(timezone.utc).isoformat(),
            tool_hashes=tool_hashes_from(str(rootfs_path)), manifest=manifest)

        (out_dir / names["manifest"]).write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (out_dir / names["sidecar"]).write_text(
            json.dumps(sidecar, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    finally:
        if args.keep_context:
            print(f"build context kept at {context}", file=sys.stderr)
        else:
            shutil.rmtree(context, ignore_errors=True)

    print(f"rootfs   {rootfs_path}")
    print(f"sha256   {rootfs_sha256}")
    print(f"manifest {out_dir / names['manifest']}")
    print("\nThis build has not been imported into WSL2 on a live Windows host.",
          file=sys.stderr)
    blockers = wrf.release_blockers({recipe.architecture: recipe})
    if blockers:
        # An artifact nobody has anchored trust to is not a release artifact,
        # whatever the filename says. State it where the person who just built
        # it will read it, not only in a document.
        print("NOT RELEASABLE: " + wrf.ARTIFACT_WORKFLOW_BLOCKER, file=sys.stderr)
        for blocker in blockers:
            print(f"  blocker: {blocker}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
