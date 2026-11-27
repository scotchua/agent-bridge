"""The bridge's one-year Claude subscription token.

This module is deliberately the sole reader of the Keychain item.  Callers
receive either a narrowly-scoped child environment or non-secret status; the
token itself is never persisted, logged, or put in this process's environment.
"""
from __future__ import annotations

import datetime as dt
import getpass
import json
import os
import subprocess
import sys
from pathlib import Path

SERVICE = "agent-bridge-claude-oauth-token"
SECURITY = "/usr/bin/security"
_NOT_FOUND = 44                 # errSecItemNotFound
_INTERACTION_NOT_ALLOWED = 36   # errSecInteractionNotAllowed
_MAX_TOKEN_LENGTH = 16_384
DISABLE_ENV = "AGENT_BRIDGE_DISABLE_CLAUDE_TOKEN"
_last_issue: str | None = None


def disable_child_env() -> dict[str, str]:
    """Return the test-only Keychain-reader disable switch for bridge children.

    This is deliberately a one-way, one-variable pass-through.  A parent
    value other than the explicit test value never reaches a child.
    """
    return {DISABLE_ENV: "1"} if os.environ.get(DISABLE_ENV) == "1" else {}


def _account() -> str:
    try:
        return getpass.getuser()
    except (OSError, KeyError):
        return os.environ.get("USER", "")


def _valid(value: str) -> bool:
    return (bool(value) and len(value) <= _MAX_TOKEN_LENGTH and "\n" not in value
            and "\r" not in value and not any(c.isspace() or not (32 <= ord(c) <= 126)
                                            for c in value))


def _read_keychain() -> str | None:
    """Read and validate the item, retaining only a secret-free diagnostic."""
    global _last_issue
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            [SECURITY, "find-generic-password", "-s", SERVICE, "-a", _account(), "-w"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=3, check=False, shell=False,
            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "USER": _account()},
        )
    except (OSError, subprocess.SubprocessError):
        _last_issue = "keychain_access_failed"
        return None
    if result.returncode:
        # Never surface stderr: security may include an item label or value.
        _last_issue = ("keychain_locked" if result.returncode == _INTERACTION_NOT_ALLOWED
                       else "keychain_access_denied" if result.returncode != _NOT_FOUND else None)
        return None
    try:
        raw = result.stdout.decode("ascii")
    except UnicodeDecodeError:
        _last_issue = "token_malformed"
        return None
    value = raw.strip()
    # ``security -w`` may terminate its value with a newline. Other leading
    # or trailing whitespace is part of a malformed item, not normalization.
    if raw.rstrip("\r\n") != value or not _valid(value):
        _last_issue = "token_malformed"
        return None
    return value


# Tests replace this narrow seam before exercising launch sites.  Keeping the
# reader as a module attribute avoids a test-mode environment switch that a
# production child could accidentally inherit.
_reader = _read_keychain


def load() -> str | None:
    """Return the Keychain value or ``None``. This function never raises."""
    global _last_issue
    # Reset at the seam, so a test reader that reports an absent item cannot
    # accidentally retain a previous Keychain diagnostic.
    _last_issue = None
    # This is deliberately disable-only: test children inherit it so they
    # cannot read the operator's Keychain item in a fresh interpreter.
    if os.environ.get(DISABLE_ENV) == "1":
        return None
    return _reader()


def child_auth_env() -> dict[str, str]:
    token = load()
    return {"CLAUDE_CODE_OAUTH_TOKEN": token} if token is not None else {}


def source() -> str:
    """Non-secret credential source label for health and job records."""
    return "setup_token" if load() is not None else "login"


def diagnostic() -> str | None:
    """A secret-free reason after a load/source call, if Keychain access failed."""
    return _last_issue


def _bridge_home() -> Path:
    return Path.home() / ".agent-bridge"


def _date_path() -> Path:
    return _bridge_home() / "claude-token.json"


def set_on() -> str | None:
    try:
        value = json.loads(_date_path().read_text(encoding="utf-8"))
        stamp = value.get("set_on") if isinstance(value, dict) else None
        dt.date.fromisoformat(stamp)
        return stamp if isinstance(stamp, str) else None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def mark_set(minted_on: str | None = None) -> str:
    """Record the mint date, but only after proving the item can be read."""
    if load() is None:
        raise ValueError("Claude setup token is not available in the login Keychain")
    stamp = minted_on or dt.date.today().isoformat()
    try:
        dt.date.fromisoformat(stamp)
    except (TypeError, ValueError) as exc:
        raise ValueError("minted-on must be YYYY-MM-DD") from exc
    directory = _bridge_home()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = _date_path()
    temporary = path.with_suffix(".tmp")
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            os.chmod(temporary, 0o600)
            json.dump({"set_on": stamp}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass
    return stamp


def clear_date() -> None:
    try:
        _date_path().unlink()
    except FileNotFoundError:
        pass


def days_left(today: dt.date | None = None) -> int | None:
    stamp = set_on()
    if stamp is None:
        return None
    return ((dt.date.fromisoformat(stamp) + dt.timedelta(days=365)) - (today or dt.date.today())).days


def attention_line() -> str | None:
    """One short, secret-free status line for expiration or login fallback."""
    if source() == "login":
        return "Claude bridge is using refreshable login; set up a one-year token (see docs/bridge-health.md)."
    remaining = days_left()
    if remaining is None:
        return None
    if remaining <= 0:
        return "Claude bridge setup token has expired; run `claude setup-token` and rotate it (see docs/bridge-health.md)."
    if remaining <= 30:
        return f"Claude bridge setup token expires in {remaining} days; rotate it soon (see docs/bridge-health.md)."
    return None


def attention_once_today() -> str | None:
    """Return an attention line at most once per local day across status runs."""
    if sys.platform != "darwin":
        return None
    line = attention_line()
    if line is None:
        return None
    path = _bridge_home() / "claude-token-attention.json"
    today = dt.date.today().isoformat()
    try:
        prior = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(prior, dict) and prior.get("day") == today and prior.get("line") == line:
            return None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    try:
        _bridge_home().mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        with open(temporary, "w", encoding="utf-8") as handle:
            os.chmod(temporary, 0o600)
            json.dump({"day": today, "line": line}, handle)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except OSError:
        # A status check must remain useful in a restricted service account.
        return line
    return line
