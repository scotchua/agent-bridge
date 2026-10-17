"""A real one-commit Git repository for tests that admit execution jobs.

On POSIX, execution admission pins each job to a private copy of a resolved
commit, so a directory holding only an empty ``.git`` no longer admits.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


def git_env() -> dict[str, str]:
    """Git with no system or user configuration and a fixed identity."""
    return {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull, "GIT_AUTHOR_NAME": "Queue Test",
            "GIT_AUTHOR_EMAIL": "queue@example.invalid", "GIT_COMMITTER_NAME": "Queue Test",
            "GIT_COMMITTER_EMAIL": "queue@example.invalid"}


def make_git_repo(path: Path) -> Path:
    """Create (or fill an existing empty) ``path`` with one committed file."""
    path.mkdir(exist_ok=True)
    env = git_env()
    (path / "tracked.txt").write_text("base\n", encoding="utf-8")
    for argv in (("git", "init", "--quiet"), ("git", "add", "tracked.txt"),
                 ("git", "commit", "--quiet", "-m", "base")):
        subprocess.run(argv, cwd=path, check=True, env=env, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, shell=False)
    return path
