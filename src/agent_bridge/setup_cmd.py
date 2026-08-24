"""`agent-bridge-setup`: find the peer CLIs, check them, and pin them.

Written for someone who has not read the rest of this repository. It answers the
three questions that actually stop a first run: where are the two CLIs, are they
signed in, and what versions am I pinning.

It writes config/local.json, which is gitignored, so nobody has to edit a
tracked file to run this on their own machine.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from typing import Any

from . import config, preflight, store

#: Places a Claude or Codex CLI commonly lands, beyond whatever is on PATH.
EXTRA_LOOKUP = {
    "claude": [
        "~/.local/bin/claude",
        "/opt/homebrew/bin/claude",
        "/usr/local/bin/claude",
    ],
    "codex": [
        "~/.local/bin/codex",
        "/opt/homebrew/bin/codex",
        "/usr/local/bin/codex",
    ],
}


def find_all(peer: str) -> list[str]:
    """Every candidate binary for a peer, PATH first.

    Keeps the path as found rather than its realpath, and de-duplicates on the
    realpath. Resolving would pin a version-numbered directory or an internal
    shim, both of which break on the next CLI update; the conventional path is
    the stable name and the one a person recognises.
    """
    found: list[str] = []
    seen: set[str] = set()

    def add(candidate: str) -> None:
        if not (os.path.isfile(candidate) and os.access(candidate, os.X_OK)):
            return
        real = os.path.realpath(candidate)
        if real in seen:
            return
        seen.add(real)
        found.append(candidate)

    on_path = shutil.which(preflight.DEFAULT_EXECUTABLE_NAMES[peer])
    if on_path:
        add(on_path)
    for candidate in EXTRA_LOOKUP.get(peer, []):
        add(os.path.expanduser(candidate))
    # nvm installs are easy to miss and are often where a working CLI lives.
    nvm = os.path.expanduser("~/.nvm/versions/node")
    if os.path.isdir(nvm):
        for version in sorted(os.listdir(nvm)):
            add(os.path.join(nvm, version, "bin",
                             preflight.DEFAULT_EXECUTABLE_NAMES[peer]))
    return found


def version_of(path: str) -> str:
    try:
        proc = subprocess.run([path, "--version"], capture_output=True,
                              timeout=30, check=False, shell=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    text = (proc.stdout or b"").decode("utf-8", "replace").strip()
    return text.splitlines()[0].strip() if text else ""


def claude_signed_in(path: str) -> bool | None:
    """True, False, or None when the CLI does not report it."""
    try:
        proc = subprocess.run([path, "auth", "status"], capture_output=True,
                              timeout=30, check=False, shell=False)
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        return bool(json.loads(proc.stdout.decode("utf-8", "replace")).get("loggedIn"))
    except (ValueError, AttributeError):
        return None


def codex_signed_in(path: str, codex_home: str) -> bool | None:
    """Whether the ISOLATED codex home has credentials. Not the default one."""
    auth = os.path.join(os.path.expanduser(codex_home), "auth.json")
    if os.path.isfile(auth):
        return True
    try:
        proc = subprocess.run([path, "login", "status"], capture_output=True,
                              timeout=30, check=False,
                              env={**os.environ, "CODEX_HOME":
                                   os.path.expanduser(codex_home)}, shell=False)
    except (OSError, subprocess.SubprocessError):
        return None
    text = (proc.stdout + proc.stderr).decode("utf-8", "replace").lower()
    if "not logged in" in text or "no credentials" in text:
        return False
    return True if proc.returncode == 0 else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent-bridge-setup",
        description="Find the Claude and Codex CLIs, check them, and pin them.")
    parser.add_argument("--write", action="store_true",
                        help="Write config/local.json. Without this, only report.")
    parser.add_argument("--claude", help="Force a specific claude binary.")
    parser.add_argument("--codex", help="Force a specific codex binary.")
    args = parser.parse_args(argv)
    store.set_umask()

    defaults = store.read_json(config.DEFAULT_CONFIG_PATH)
    codex_home = defaults["peers"]["codex"]["codex_home"]
    chosen: dict[str, dict[str, Any]] = {}
    problems: list[str] = []

    print("agent-bridge setup\n" + "=" * 60)
    for peer in ("claude", "codex"):
        forced = getattr(args, peer)
        candidates = [os.path.expanduser(forced)] if forced else find_all(peer)
        print(f"\n{peer}:")
        if not candidates:
            print(f"  NOT FOUND. Install the {peer} CLI, then re-run this.")
            problems.append(f"{peer} CLI not found")
            continue

        rows = []
        for path in candidates:
            version = version_of(path)
            if peer == "claude":
                signed = claude_signed_in(path)
            else:
                signed = codex_signed_in(path, codex_home)
            rows.append((path, version, signed))
            flag = {True: "signed in", False: "NOT signed in",
                    None: "sign-in unknown"}[signed]
            print(f"  {path}")
            print(f"      version: {version or 'unknown'}   ({flag})")

        # Prefer a signed-in install. Discovery alone can pick a second copy
        # that has never been logged in, which then fails on every call.
        preferred = next((r for r in rows if r[2] is True), None)
        if preferred is None:
            preferred = next((r for r in rows if r[2] is None), rows[0])
            problems.append(
                f"{peer}: no install is confirmed signed in "
                f"(picked {preferred[0]})")
        if len(rows) > 1:
            print(f"  -> {len(rows)} installs found; pinning {preferred[0]}")
        chosen[peer] = {"executable": preferred[0],
                        "allowed_versions": [preferred[1]] if preferred[1] else []}

    overlay: dict[str, Any] = {"peers": chosen}
    print("\n" + "=" * 60)
    print("config/local.json would contain:\n")
    print(json.dumps(overlay, indent=2, sort_keys=True))

    if problems:
        print("\nBefore this will work:")
        for item in problems:
            print(f"  - {item}")
        print("\n  claude:  <path> auth login")
        print(f"  codex:   CODEX_HOME={codex_home} <path> login")

    if args.write:
        store.atomic_write_json(config.local_config_path(), overlay)
        print(f"\nWrote {config.local_config_path()}")
        print("This file is gitignored. Re-run setup after a CLI update, "
              "because the pinned version will no longer match.")
    else:
        print("\nNothing written. Re-run with --write to save it.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
