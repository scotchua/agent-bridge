"""Binds each client-derived cloud send to its exact bytes before it leaves.

Every client-derived payload bound for a cloud provider, on the "peer"
(consultation) route or the "execution" route, passes through here
immediately before the process that carries it to the provider starts. The
same rules apply whichever teammate (Claude or Codex) is on the other end.

    binding = bind(body, route="peer", provider="codex",
                   classification="client_derived", log_path=...)
    release(binding, body, route="peer", provider="codex")
    ...hand exactly ``body`` to the provider...

``bind`` builds an immutable record of the outbound body: its SHA-256 and
length, the route and provider, the classification, the identifier classes
found in it, the operator settings revision, any identifier exception that
covers it, and an expiry. It refuses when:

* the operator settings do not enable this route for client-derived content;
* the body holds an SSN, EIN, account or card number, email address or
  phone number, unless Scott granted an exception for this exact payload.

A clean payload on an enabled route binds automatically (D1). The decision is
logged with the hash, length, route and identifier CLASSES, never the content
or any identifier value. Raw previews are not persisted.

``bind`` also issues a single-use capability: an HMAC over the whole record
under a key that lives only in this process. ``release`` checks the
capability, the expiry, the exact body hash, the destination and the current
settings revision, then spends the capability. A second release, a changed
byte, a different destination or a settings change in between all refuse.

Identifier exceptions (D3) cover one payload only. Each is a JSON file Scott
writes under ``<state home>/identifier-exceptions/<id>.json``:

    {"exception_id": "...", "approval_ref": "...", "body_sha256": "...",
     "route": "peer", "provider": "codex", "classes": ["email"],
     "expires_at": 1790000000}

It must be trusted the same way as the operator settings file, must name this
exact body hash and destination, must cover every class found, and is spent
on first use. Nothing in this module writes one.

Detection cannot prove a payload is clean. It errs toward finding too much:
a nine-digit run counts as a possible SSN. Other classifications (public,
synthetic, internal) are outside this gate and pass unchanged; their own
refusals live in the broker and the execution lanes.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import stat
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Mapping

from . import operator_settings
from .platform import platform

SCHEMA_VERSION = 1
DEFAULT_TTL_SECONDS = 300
CLIENT_DERIVED = frozenset({"client_derived", "client-derived"})
PROVIDERS = frozenset({"claude", "codex"})
EXCEPTIONS_DIR = "identifier-exceptions"

# Fixed refusal reasons, safe to log.
REFUSED_ROUTE_DISABLED = "route_not_enabled"
REFUSED_SETTINGS = "settings_fail_closed"
REFUSED_IDENTIFIERS = "identifiers_need_exception"
REFUSED_EXCEPTION = "exception_invalid"
REFUSED_DESTINATION = "destination_invalid"
REFUSED_CAPABILITY = "capability_invalid"
REFUSED_REPLAY = "capability_spent"
REFUSED_EXPIRED = "binding_expired"
REFUSED_BODY = "body_changed"
REFUSED_POLICY = "policy_changed"
REFUSED_LOG = "log_untrusted"


class SendRefused(Exception):
    """The send must not happen. ``reason`` is one of the fixed strings above."""

    def __init__(self, reason: str, classes: tuple[str, ...] = ()):
        super().__init__(reason)
        self.reason = reason
        self.classes = classes


# ------------------------------------------------------------------ detection
_SEP = r"[ .-]?"
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")),
    # Any separator, or none: 123-45-6789, 123 45 6789, 123.45.6789, 123456789.
    ("ssn", re.compile(r"(?<!\d)\d{3}" + _SEP + r"\d{2}" + _SEP + r"\d{4}(?!\d)")),
    ("ein", re.compile(r"(?<!\d)\d{2}" + _SEP + r"\d{7}(?!\d)")),
    ("phone", re.compile(
        r"(?<![\w-])(?:\+\d{1,3}" + _SEP + r")?(?:\(\d{3}\)\s?|\d{3}" + _SEP + r")\d{3}"
        + _SEP + r"\d{4}(?![\w-])"
        r"|(?<![\w-])\+\d(?:" + _SEP + r"\d){6,13}(?![\w-])")),
    ("account", re.compile(r"(?<!\d)(?:\d[ -]?){7,18}\d(?!\d)")),
)

# A real calendar date (2026-10-27) is not an account number unless a label
# near it says it is: "Account number: 2026-10-27" still fails closed (F9).
_ISO_DATE = re.compile(r"(19|20)\d\d-(\d\d)-(\d\d)")
# Whole words only, so "postcard" or "accounting" alone do not block a date.
_ACCOUNT_LABEL = re.compile(
    r"(?i)\b(?:acc(?:oun)?t?s?|accts?|cards?|pan|routing|iban|numbers?|nbr|num|no)\b"
    r"|(?<!\w)a[/.]c\b|#")
_LABEL_WINDOW = 40


def _is_plain_date(text: str, match: re.Match[str]) -> bool:
    if not _ISO_DATE.fullmatch(match.group()):
        return False
    try:
        datetime.date.fromisoformat(match.group())
    except ValueError:
        return False
    return not _ACCOUNT_LABEL.search(text[max(0, match.start() - _LABEL_WINDOW):match.start()])


def _found(name: str, pattern: re.Pattern[str], text: str) -> bool:
    if name != "account":
        return pattern.search(text) is not None
    return any(not _is_plain_date(text, m) for m in pattern.finditer(text))


def identifier_classes(text: str) -> tuple[str, ...]:
    """The identifier classes found in ``text``, sorted. Never the values."""
    return tuple(sorted(name for name, pattern in _PATTERNS if _found(name, pattern, text)))


# -------------------------------------------------------------------- binding
@dataclass(frozen=True)
class Binding:
    schema_version: int
    record_id: str
    body_sha256: str
    body_length: int
    route: str
    provider: str
    classification: str
    identifier_classes: tuple[str, ...]
    policy_revision: str
    exception_id: str | None
    approval_ref: str | None
    created_at: float
    expires_at: float
    capability: str = field(repr=False, compare=False)

    def record(self) -> dict:
        """The signed record: everything except the capability."""
        value = asdict(self)
        value.pop("capability")
        value["identifier_classes"] = list(self.identifier_classes)
        return value

    def log_entry(self) -> dict:
        """The record as logged: the approval reference only as a hash."""
        value = self.record()
        ref = value.pop("approval_ref")
        value["approval_ref_sha256"] = (hashlib.sha256(ref.encode("utf-8")).hexdigest()
                                        if ref else None)
        return value


_KEY = secrets.token_bytes(32)
_SPENT: set[str] = set()
_LOCK = threading.Lock()


def _digest(body: str | bytes) -> tuple[bytes, str]:
    data = body.encode("utf-8") if isinstance(body, str) else bytes(body)
    return data, hashlib.sha256(data).hexdigest()


def _sign(record: Mapping) -> str:
    message = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(_KEY, message, hashlib.sha256).hexdigest()


def _policy(home, environ, legacy) -> tuple[frozenset[str], str, bool]:
    """Enabled routes, the settings revision, and whether the read failed closed.

    The revision covers the file's exact text and the routes it resolved to,
    so a change to either (including the legacy values the caller holds when
    the file is absent) refuses a release bound under the old policy.
    """
    setting = operator_settings.resolve(home, environ, legacy=legacy)
    text = operator_settings.read_text(home, environ)
    material = json.dumps([text, sorted(setting.routes), setting.source])
    revision = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return setting.routes, revision, setting.fail_closed


def _check_destination(route: str, provider: str) -> None:
    if route not in operator_settings.ROUTES or provider not in PROVIDERS:
        raise SendRefused(REFUSED_DESTINATION)


def _owned(st: os.stat_result, mask: int) -> bool:
    getuid = getattr(os, "getuid", None)
    return (getuid is None or st.st_uid == getuid()) and not st.st_mode & mask


def _log_intact(fd: int, size: int) -> bool:
    """True when every line of the log is one complete JSON object."""
    os.lseek(fd, 0, os.SEEK_SET)
    chunks, remaining = [], size
    while remaining:
        chunk = os.read(fd, min(remaining, 1 << 16))
        if not chunk:
            return False
        chunks.append(chunk)
        remaining -= len(chunk)
    raw = b"".join(chunks)
    if not raw.endswith(b"\n"):
        return False
    try:
        return all(isinstance(json.loads(line), dict) for line in raw[:-1].split(b"\n"))
    except ValueError:
        return False


def _log(log_path: str | None, event: str, entry: Mapping) -> None:
    """Append one decision. A log that cannot be trusted refuses the send."""
    if not log_path:
        return
    line = json.dumps({"event": event, "logged_at": time.time(), **entry},
                      sort_keys=True) + "\n"
    try:
        parent = os.stat(os.path.dirname(os.path.abspath(log_path)))
        fd = os.open(log_path, os.O_RDWR | os.O_CREAT | os.O_APPEND
                     | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError:
        raise SendRefused(REFUSED_LOG) from None
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or not _owned(st, 0o077)
                or not stat.S_ISDIR(parent.st_mode) or not _owned(parent, 0o022)):
            raise SendRefused(REFUSED_LOG)
        # One writer at a time, and only onto an intact log (F11): a fragment
        # left by a failed append is cut back, and a log holding any damaged
        # record refuses every later send until it is repaired.
        with platform.lock_exclusive(fd, log_path, 10.0):
            start = os.fstat(fd).st_size
            if start and not _log_intact(fd, start):
                raise SendRefused(REFUSED_LOG)
            data = memoryview(line.encode("utf-8"))
            try:
                while data:
                    written = os.write(fd, data)
                    if written <= 0:
                        raise SendRefused(REFUSED_LOG)
                    data = data[written:]
            except BaseException:
                with contextlib.suppress(OSError):
                    os.ftruncate(fd, start)
                raise
    except OSError:
        raise SendRefused(REFUSED_LOG) from None
    finally:
        os.close(fd)


EXCEPTION_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def valid_exception_id(value) -> bool:
    """True for an id the exception store can name; callers check at intake."""
    return isinstance(value, str) and EXCEPTION_ID.fullmatch(value) is not None


def _exception_path(exception_id: str, home, environ) -> str:
    if not valid_exception_id(exception_id):
        raise SendRefused(REFUSED_EXCEPTION)
    return os.path.join(operator_settings.state_home(home, environ), EXCEPTIONS_DIR,
                        exception_id + ".json")


def _load_exception(exception_id: str, *, body_sha256: str, route: str, provider: str,
                    classes: tuple[str, ...], now: float, home, environ) -> dict:
    try:
        text = operator_settings._read_trusted(_exception_path(exception_id, home, environ))
    except operator_settings._Refused:
        text = None
    try:
        document = json.loads(text) if text else None
    except ValueError:
        document = None
    if (not isinstance(document, dict)
            or document.get("exception_id") != exception_id
            or not isinstance(document.get("approval_ref"), str) or not document["approval_ref"]
            or document.get("body_sha256") != body_sha256
            or document.get("route") != route
            or document.get("provider") != provider
            or not isinstance(document.get("classes"), list)
            or not set(classes) <= set(document["classes"])
            or type(document.get("expires_at")) not in (int, float)
            or not math.isfinite(document["expires_at"])
            or document["expires_at"] <= now):
        raise SendRefused(REFUSED_EXCEPTION, classes)
    return document


def _spend_exception(exception_id: str, classes: tuple[str, ...], home, environ) -> None:
    """Spend an exception for good: a marker file that survives this process."""
    folder = os.path.join(operator_settings.state_home(home, environ), EXCEPTIONS_DIR, "spent")
    try:
        try:
            os.mkdir(folder, 0o700)
        except FileExistsError:
            pass
        st = os.lstat(folder)
        if not stat.S_ISDIR(st.st_mode) or not _owned(st, 0o022):
            raise SendRefused(REFUSED_EXCEPTION, classes)
        fd = os.open(os.path.join(folder, exception_id),
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                     0o600)
        os.close(fd)
    except OSError:
        raise SendRefused(REFUSED_EXCEPTION, classes) from None


def bind(body: str | bytes, *, route: str, provider: str, classification: str,
         exception_id: str | None = None, log_path: str | None = None,
         ttl_seconds: float = DEFAULT_TTL_SECONDS, now: float | None = None,
         home=None, environ: Mapping[str, str] | None = None,
         legacy=None) -> Binding | None:
    """Bind a client-derived cloud send, or refuse it. None outside this gate."""
    if classification.strip().lower() not in CLIENT_DERIVED:
        return None
    _check_destination(route, provider)
    now = time.time() if now is None else now
    data, body_sha256 = _digest(body)
    base = {"body_sha256": body_sha256, "body_length": len(data),
            "route": route, "provider": provider}
    try:
        if (isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float))
                or not math.isfinite(ttl_seconds) or ttl_seconds <= 0
                or not math.isfinite(now)):
            raise SendRefused(REFUSED_EXPIRED)
        routes, revision, fail_closed = _policy(home, environ, legacy)
        if fail_closed:
            raise SendRefused(REFUSED_SETTINGS)
        if route not in routes:
            raise SendRefused(REFUSED_ROUTE_DISABLED)
        classes = identifier_classes(data.decode("utf-8", errors="replace"))
        exception = None
        if classes:
            if exception_id is None:
                raise SendRefused(REFUSED_IDENTIFIERS, classes)
            exception = _load_exception(exception_id, body_sha256=body_sha256, route=route,
                                        provider=provider, classes=classes, now=now,
                                        home=home, environ=environ)
        record = {
            "schema_version": SCHEMA_VERSION,
            "record_id": secrets.token_hex(16),
            "body_sha256": body_sha256,
            "body_length": len(data),
            "route": route,
            "provider": provider,
            "classification": "client_derived",
            "identifier_classes": classes,
            "policy_revision": revision,
            "exception_id": exception_id if exception else None,
            "approval_ref": exception["approval_ref"] if exception else None,
            "created_at": now,
            # Never outlive the exception that allowed it.
            "expires_at": (min(now + ttl_seconds, exception["expires_at"]) if exception
                           else now + ttl_seconds),
        }
        if exception is not None:
            # D3: one payload only. Spend the exception before anything is sent,
            # durably, so a restart or another process cannot spend it again.
            _spend_exception(exception_id, classes, home, environ)
        binding = Binding(**record, capability=_sign({**record,
                                                      "identifier_classes": list(classes)}))
    except SendRefused as exc:
        _log(log_path, "refused", {**base, "reason": exc.reason,
                                    "identifier_classes": list(exc.classes)})
        raise
    _log(log_path, "bound", binding.log_entry())
    return binding


def _spend(token: str, reason: str, classes: tuple[str, ...] = ()) -> None:
    with _LOCK:
        if token in _SPENT:
            raise SendRefused(reason, classes)
        _SPENT.add(token)


def release(binding: Binding | None, body: str | bytes, *, route: str, provider: str,
            classification: str, log_path: str | None = None, now: float | None = None,
            home=None, environ: Mapping[str, str] | None = None,
         legacy=None) -> None:
    """Check and spend the capability immediately before ``body`` is sent."""
    client_derived = classification.strip().lower() in CLIENT_DERIVED
    if binding is None and not client_derived:
        return
    data, body_sha256 = _digest(body)
    base = {"body_sha256": body_sha256, "route": route, "provider": provider}
    try:
        # A binding offered under another classification is a relabel, not a pass.
        if not client_derived or not isinstance(binding, Binding):
            raise SendRefused(REFUSED_CAPABILITY)
        record = binding.record()
        if not hmac.compare_digest(binding.capability, _sign(record)):
            raise SendRefused(REFUSED_CAPABILITY)
        now = time.time() if now is None else now
        if (not math.isfinite(now) or not math.isfinite(binding.expires_at)
                or now >= binding.expires_at):
            raise SendRefused(REFUSED_EXPIRED)
        if binding.route != route or binding.provider != provider:
            raise SendRefused(REFUSED_DESTINATION)
        if binding.body_sha256 != body_sha256 or binding.body_length != len(data):
            raise SendRefused(REFUSED_BODY)
        routes, revision, fail_closed = _policy(home, environ, legacy)
        if fail_closed or route not in routes or revision != binding.policy_revision:
            raise SendRefused(REFUSED_POLICY)
        _spend("record:" + binding.record_id, REFUSED_REPLAY)
    except SendRefused as exc:
        _log(log_path, "release_refused", {**base, "reason": exc.reason,
                                            "record_id": getattr(binding, "record_id", None)})
        raise
    _log(log_path, "released", {**base, "record_id": binding.record_id})


def guard(body: str | bytes, *, route: str, provider: str, classification: str,
          exception_id: str | None = None, log_path: str | None = None,
          home=None, environ: Mapping[str, str] | None = None,
         legacy=None) -> None:
    """Bind and release in one step, for callers that send right after."""
    binding = bind(body, route=route, provider=provider, classification=classification,
                   exception_id=exception_id, log_path=log_path, home=home, environ=environ,
                   legacy=legacy)
    release(binding, body, route=route, provider=provider, classification=classification,
            log_path=log_path, home=home, environ=environ, legacy=legacy)
