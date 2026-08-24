"""Implementation provenance.

The existing bridge's gap was representing an uncommitted source file by the
previous Git HEAD.  This records commit, dirty state, AND an independent hash
tree over the actual on-disk implementation, so a dirty run is self-evident
and a clean run is verifiable.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from typing import Any

_HASHED_SUFFIXES = (".py", ".json", ".sh", ".md", ".toml")
_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "runtime", "node_modules"}


def _git(repo_root: str, *args: str) -> str | None:
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, shell explicitly off
            ["git", *args], cwd=repo_root, capture_output=True,
            timeout=15, check=False, shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.decode("utf-8", "replace").strip()


def source_hash(repo_root: str) -> tuple[str, int]:
    """SHA-256 over the sorted (relpath, filehash) list of implementation files.

    Independent of Git entirely, so it is meaningful on a dirty tree.
    """
    entries: list[str] = []
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for name in sorted(filenames):
            if not name.endswith(_HASHED_SUFFIXES):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, repo_root)
            digest = hashlib.sha256()
            try:
                with open(full, "rb") as handle:
                    for chunk in iter(lambda: handle.read(65536), b""):
                        digest.update(chunk)
            except OSError:
                continue
            entries.append(f"{rel}\0{digest.hexdigest()}")
    joined = "\n".join(entries).encode("utf-8")
    return hashlib.sha256(joined).hexdigest(), len(entries)


def collect(repo_root: str) -> dict[str, Any]:
    """Full implementation provenance block, recorded on every job."""
    commit = _git(repo_root, "rev-parse", "HEAD")
    porcelain = _git(repo_root, "status", "--porcelain")
    tree_hash, file_count = source_hash(repo_root)
    dirty = None if porcelain is None else bool(porcelain.strip())
    return {
        "repo_root": repo_root,
        "git_commit": commit,
        "git_available": commit is not None,
        "dirty_tree": dirty,
        "dirty_file_count": (
            None if porcelain is None else len([l for l in porcelain.splitlines() if l.strip()])
        ),
        "source_hash_sha256": tree_hash,
        "source_file_count": file_count,
    }
