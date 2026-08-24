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
import tempfile
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


#: Directories whose contents are periodically cleaned by the OS, or created
#: fresh per session by tooling. A CLI found here works right now and may not
#: exist tomorrow.
def _ephemeral_roots() -> list[str]:
    roots = {tempfile.gettempdir(), "/tmp", "/var/tmp", "/private/tmp",
             "/var/folders", "/private/var/folders"}
    return [os.path.realpath(r) for r in roots if r]


def is_durable(path: str) -> bool:
    """Whether a discovered CLI is somewhere that will still exist tomorrow.

    Wrapper shims commonly live in the per-user temp directory, and they are
    first on PATH precisely because they are meant to intercept. Pinning one
    creates two problems: the path is cleaned periodically, so every job later
    fails with a missing executable; and a shim is not the binary whose
    behaviour this project measured, so the facts in
    docs/verified-cli-behaviour.md may simply not hold for it, while the version
    string still matches.
    """
    real = os.path.realpath(path)
    return not any(real == root or real.startswith(root + os.sep)
                   for root in _ephemeral_roots())


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
    codex_home = os.path.expanduser(defaults["peers"]["codex"]["codex_home"])
    # Codex refuses to start when CODEX_HOME names a directory that does not
    # exist, and it will not create one. Until this ran here, the only thing
    # that created it was the first consultation, which needs a signed-in home,
    # which needs the login, which needs the directory. A clean machine could
    # not complete the documented order at all.
    #
    # secure_mkdir rather than a plain mkdir: auth.json is about to land here.
    store.secure_mkdir(codex_home)
    chosen: dict[str, dict[str, Any]] = {}
    problems: list[str] = []

    print("agent-bridge setup\n" + "=" * 60)

    # Catch a state root that cannot keep permissions before anything is
    # written to it. On WSL this is the /mnt/c mistake.
    from .errors import BrokerError, hint as error_hint
    try:
        perms = preflight.assert_state_root_secure(config.Config(defaults, "setup"))
        print(f"\nstate root: {perms['state_root']}  "
              f"(dir {perms['directory_mode']}, files {perms['file_mode']})")
    except BrokerError as exc:
        print(f"\nSTATE ROOT PROBLEM\n  {error_hint(exc.category)}")
        problems.append("state root does not keep owner-only permissions")
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
            durability = "" if is_durable(path) else "  [TEMPORARY PATH]"
            print(f"  {path}{durability}")
            print(f"      version: {version or 'unknown'}   ({flag})")

        # Prefer a signed-in install. Discovery alone can pick a second copy
        # that has never been logged in, which then fails on every call.
        # Prefer signed in, then durable. Discovery alone can pick a second
        # copy that has never been logged in, or a temp-directory shim that
        # will be cleaned out from under the pin.
        def rank(row: tuple[str, str, bool | None]) -> tuple[int, int]:
            signed_rank = {True: 0, None: 1, False: 2}[row[2]]
            return (signed_rank, 0 if is_durable(row[0]) else 1)

        ordered = sorted(rows, key=rank)
        preferred = ordered[0]
        if preferred[2] is not True:
            problems.append(
                f"{peer}: no install is confirmed signed in "
                f"(picked {preferred[0]})")
        if len(rows) > 1:
            print(f"  -> {len(rows)} installs found; pinning {preferred[0]}")
        if not is_durable(preferred[0]):
            durable_alternatives = [r[0] for r in rows if is_durable(r[0])]
            print(f"  -> WARNING: {preferred[0]}")
            print("     is inside a temporary directory. It will not survive a")
            print("     cleanup, and every job will then fail with a missing")
            print("     executable. Re-running setup would pin another")
            print("     temporary path and appear to fix it.")
            print("     A wrapper shim there is also not the binary this")
            print("     project measured, so documented CLI behaviour may not")
            print("     hold for it even though --version matches.")
            if durable_alternatives:
                print(f"     Prefer: --{peer} {durable_alternatives[0]}")
            problems.append(f"{peer}: pinned path is in a temporary directory")
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
