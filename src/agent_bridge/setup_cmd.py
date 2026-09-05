"""`agent-bridge-setup`: find the peer CLIs, check them, and pin them.

Written for someone who has not read the rest of this repository. It answers the
three questions that actually stop a first run: where are the two CLIs, are they
signed in, and what versions am I pinning.

It stages a complete effective candidate without activating it, then promotes
only the machine-local overlay after version-bound canary evidence passes.
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


def write_candidate(path: str, proposed_overlay: dict[str, Any],
                    active: config.Config | None = None) -> config.Config:
    """Write one complete candidate without changing the active overlay."""
    target = os.path.realpath(os.path.abspath(path))
    protected = {
        os.path.realpath(config.DEFAULT_CONFIG_PATH),
        os.path.realpath(config.local_config_path()),
    }
    if target in protected:
        raise ValueError("candidate path must not be an active config path")
    current = active or config.load()
    raw = config.merge(current.raw, proposed_overlay)
    candidate = config.Config(raw, path)
    store.atomic_write_json(path, candidate.raw)
    return candidate


def _overlay_from_effective(base: dict[str, Any],
                            effective: dict[str, Any]) -> dict[str, Any]:
    """Return the deep-merge overlay that reproduces an effective config."""
    missing = [key for key in base if key not in effective]
    if missing:
        raise ValueError(
            "candidate cannot remove committed config keys: " + ", ".join(missing))
    overlay: dict[str, Any] = {}
    for key, value in effective.items():
        if key not in base:
            overlay[key] = value
        elif isinstance(value, dict) and isinstance(base[key], dict):
            child = _overlay_from_effective(base[key], value)
            if child:
                overlay[key] = child
        elif value != base[key]:
            overlay[key] = value
    return overlay


def _configured_versions(cfg: config.Config) -> dict[str, list[str]]:
    return {peer: list(cfg.peer(peer).get("allowed_versions") or [])
            for peer in config.PEERS}


def validate_promotion(candidate: config.Config, results: dict[str, Any]) -> None:
    """Refuse evidence that is not a complete, version-bound PASS."""
    if results.get("verdict") != "PASS":
        raise ValueError("canary results verdict is not PASS")
    expected_hash = config.effective_config_sha256(candidate.raw)
    if results.get("effective_config_sha256") != expected_hash:
        raise ValueError("canary results do not match the candidate config hash")

    configured = _configured_versions(candidate)
    if results.get("configured_versions") != configured:
        raise ValueError("canary results configured versions do not match the candidate")
    observed = results.get("observed_versions")
    if not isinstance(observed, dict):
        raise ValueError("canary results have no observed versions")
    for peer, allowed in configured.items():
        values = observed.get(peer)
        if len(allowed) != 1 or not isinstance(allowed[0], str):
            raise ValueError(f"candidate must pin exactly one {peer} version")
        if (not isinstance(values, list)
                or not all(isinstance(value, str) for value in values)
                or sorted(set(values)) != allowed):
            raise ValueError(f"canary results are not bound to the {peer} version")

    requested = results.get("controls_requested")
    executed = results.get("controls_executed")
    if not isinstance(requested, dict) or not isinstance(executed, dict):
        raise ValueError("canary results have no requested/executed control counts")
    if results.get("skipped_controls"):
        raise ValueError("canary results contain skipped controls")
    if requested.get("timeout_canaries", 0) < 1:
        raise ValueError("canary results did not request the timeout control")
    for name, count in requested.items():
        if not isinstance(count, int) or executed.get(name) != count:
            raise ValueError(f"canary control {name!r} was not fully executed")


def promote_candidate(candidate_path: str, results_path: str,
                      destination: str | None = None) -> dict[str, Any]:
    """Atomically activate only the overlay from a version-bound candidate."""
    if not is_durable(results_path):
        raise ValueError("canary results must be stored outside temporary directories")
    candidate = config.load_effective(candidate_path)
    results = store.read_json(results_path)
    if not isinstance(results, dict):
        raise ValueError("canary results must be an object")
    validate_promotion(candidate, results)

    base = store.read_json(config.DEFAULT_CONFIG_PATH)
    overlay = _overlay_from_effective(base, candidate.raw)
    if config.merge(base, overlay) != candidate.raw:
        raise ValueError("candidate cannot be represented by a local overlay")
    store.atomic_write_json(destination or config.local_config_path(), overlay)
    return overlay


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent-bridge-setup",
        description="Find the Claude and Codex CLIs, check them, and pin them.")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--write", action="store_true",
                         help="Deprecated direct activation mode (always refused).")
    actions.add_argument("--candidate", metavar="PATH",
                         help="Write a complete candidate config without activating it.")
    actions.add_argument("--promote", metavar="PATH",
                         help="Promote this candidate after a version-bound PASS.")
    parser.add_argument("--results",
                        help="Durable canary results required with --promote.")
    parser.add_argument("--claude", help="Force a specific claude binary.")
    parser.add_argument("--codex", help="Force a specific codex binary.")
    args = parser.parse_args(argv)
    store.set_umask()

    if args.write:
        print("Direct --write activation is disabled. Emit a --candidate, run "
              "the full canaries, then use --promote with --results.")
        return 2
    if args.promote:
        if not args.results:
            print("--promote requires --results from the full canary run.")
            return 2
        try:
            promote_candidate(args.promote, args.results)
        except (OSError, ValueError) as exc:
            print(f"Promotion refused: {exc}")
            return 1
        print(f"Promoted the candidate overlay to {config.local_config_path()}")
        return 0
    if args.results:
        print("--results is only valid with --promote.")
        return 2

    active = config.load()
    codex_home = os.path.expanduser(active.raw["peers"]["codex"]["codex_home"])
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
        perms = preflight.assert_state_root_secure(active)
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

    if args.candidate and not problems:
        try:
            candidate = write_candidate(args.candidate, overlay, active)
        except (OSError, ValueError) as exc:
            print(f"\nCandidate refused: {exc}")
            return 1
        print(f"\nWrote complete candidate config to {args.candidate}")
        print(f"Effective config sha256: "
              f"{config.effective_config_sha256(candidate.raw)}")
        print("The active config/local.json was not changed.")
    elif args.candidate:
        print(f"\nCandidate not written because setup found {len(problems)} problem(s).")
    else:
        print("\nNothing written. Re-run with --candidate PATH to stage it.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
