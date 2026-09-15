"""Host-side enrolment and handoff of a provider subscription session.

The guest runs a provider CLI, and a provider CLI cannot do anything useful
without a session. This module is the host half of getting one there, and it
is written around a single rule: **the host never holds a readable copy of a
token, and never touches the user's own credential store.**

What that means concretely:

* Nothing here reads ``~/.claude``, ``~/.codex`` or any other directory the
  user's own CLI keeps its session in. Copying a credential directory into a
  guest is the exact thing this design exists to avoid: it moves a long-lived
  session to a place the user did not put it and cannot see.
* The supported path is the provider's own non-interactive one. Claude Code
  mints a setup token with ``claude setup-token``; Codex CLI accepts an access
  token on stdin via ``codex login --with-access-token``. Both are things the
  subscriber deliberately produces, once, and can revoke.
* That one value is enrolled into OS-protected storage. On Windows that is
  DPAPI under the current user, so the ciphertext is useless to another
  account on the same machine and useless on any other machine. There is no
  fallback: a platform without OS protection cannot enrol, because the
  alternative is a plaintext token on disk wearing a strict file mode.
* At job time the value is unprotected into memory, handed to the bounded
  request, and dropped. It is never written to a file, never placed on an
  argv, and never given to a verification subprocess.
* **Dropping it is not erasing it.** See :data:`MEMORY_LIFETIME_NOTE`. Python
  gives no way to guarantee a plaintext token is gone from process memory, so
  what is controlled here is how long it exists and how few places hold it,
  not whether the bytes are wiped.
* Enrolment requires explicit consent and is reversible by the person who gave
  it. :func:`revoke` removes the local copy; the text it returns says plainly
  that revoking at the provider is a separate act, because deleting a local
  ciphertext does not invalidate a token.

Every error raised here is a fixed token. A message that quoted what it was
given would eventually quote a token.

Nothing here has been validated on a live Windows host.
"""

from __future__ import annotations

import ctypes
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from . import guest_runner
from . import windows_privacy as wpv

SCHEMA_VERSION = 1

#: The providers a subscription session can be enrolled for. Not free text:
#: the value names a directory entry and selects a capsule kind.
PROVIDERS = ("claude", "codex")

#: Subdirectory of the runtime root holding enrolment state.
AUTH_DIRNAME = "auth"

#: A protected blob is bounded like everything else. The plaintext inside is
#: a JSON object around one token, so anything much larger than the token
#: bound is not one of ours.
MAX_PROTECTED_BYTES = 64 * 1024

#: Plain-language description of each provider's one supported enrolment
#: input. Shown to the person enrolling, who is not expected to know what an
#: OAuth token is.
ENROLMENT_STEPS = {
    "claude": (
        "In a terminal where you are already signed in to Claude Code, run "
        "'claude setup-token'. It prints one line. Paste that line here. It "
        "is stored encrypted for your Windows account only, and you can "
        "remove it at any time."),
    "codex": (
        "In a terminal where you are already signed in to Codex CLI, obtain "
        "your access token as the Codex documentation describes, then paste "
        "the single line here. It is stored encrypted for your Windows "
        "account only, and you can remove it at any time."),
}

#: The sentence the person has to agree to. Recorded with the enrolment so
#: what was consented to is not a matter of recollection.
CONSENT_STATEMENT = (
    "I am storing one provider session token, encrypted for this Windows "
    "account, so background jobs can use my own subscription. It will be "
    "used only to run jobs I submit, never written anywhere else, and I can "
    "remove it here and revoke it with the provider at any time.")

#: What this module can and cannot promise about memory. Stated because the
#: alternative was a comment saying ``del`` "drops" the token, which reads as
#: a guarantee it is not.
#:
#: CPython strings are immutable and may be interned, copied by the garbage
#: collector, or paged to disk by the OS. ``del`` removes a name, and frees
#: the object only if nothing else refers to it; even then the freed memory is
#: returned to an allocator that does not clear it. There is no supported way
#: to zero a ``str`` in place.
#:
#: So the controls here are lifetime and blast radius, and those are real: the
#: plaintext exists only between :func:`load_capsule` and the request being
#: sent, it is never written to any file, never appears on an argv, never
#: reaches a verification subprocess, and is redacted out of anything returned
#: to the host. The ciphertext on disk is the only durable copy, and DPAPI
#: makes that useless to another account or another machine.
MEMORY_LIFETIME_NOTE = (
    "Python cannot guarantee that a plaintext token is erased from process "
    "memory. Nothing here claims to zeroize one: releasing a reference is not "
    "wiping, and a garbage collector or the OS pager may have copied the "
    "bytes already. What is bounded is lifetime and spread, which is why the "
    "plaintext exists only for the duration of one request, is never "
    "persisted, and is redacted from every outcome")

REVOCATION_NOTE = (
    "The stored copy is gone from this machine. That does not cancel the "
    "token itself: if you want it to stop working everywhere, revoke it with "
    "the provider as well.")


class AuthError(ValueError):
    """Enrolment or handoff failed. The reason is a fixed token, never data."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# OS-protected storage
# ---------------------------------------------------------------------------


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _entropy(provider: str) -> bytes:
    """Per-provider secondary entropy, so one blob cannot decrypt as another."""

    return b"agent-bridge/windows-auth/v1/" + provider.encode("ascii")


def dpapi_protector(provider: str, *, platform: Any = None) -> "Any":
    """A protector backed by the current user's DPAPI key, or nothing.

    Deliberately raises rather than degrading. The only thing a fallback could
    fall back *to* is a plaintext token behind a file mode, and a file mode is
    not encryption: it protects against another account reading the file, not
    against the file being carried off the machine.
    """

    module = platform if platform is not None else __import__("os")
    if getattr(module, "name", "") != "nt":
        raise AuthError("os_protection_unavailable")
    try:  # pragma: no cover - exercised only on Windows
        crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    except (AttributeError, OSError) as exc:  # pragma: no cover
        raise AuthError("os_protection_unavailable") from exc

    def _call(function, payload: bytes) -> bytes:  # pragma: no cover
        data = _Blob(len(payload), ctypes.cast(
            ctypes.create_string_buffer(payload, len(payload)),
            ctypes.POINTER(ctypes.c_char)))
        extra = _entropy(provider)
        salt = _Blob(len(extra), ctypes.cast(
            ctypes.create_string_buffer(extra, len(extra)),
            ctypes.POINTER(ctypes.c_char)))
        out = _Blob()
        # CRYPTPROTECT_UI_FORBIDDEN (0x1): a background worker must never
        # block on a dialog nobody is present to answer.
        ok = function(ctypes.byref(data), None, ctypes.byref(salt), None,
                      None, 0x1, ctypes.byref(out))
        if not ok:
            raise AuthError("os_protection_failed")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            kernel32.LocalFree(out.pbData)

    class _Dpapi:  # pragma: no cover - Windows only
        def protect(self, payload: bytes) -> bytes:
            return _call(crypt32.CryptProtectData, payload)

        def unprotect(self, payload: bytes) -> bytes:
            return _call(crypt32.CryptUnprotectData, payload)

    return _Dpapi()


def _resolve_protector(provider: str, protector: Any, platform: Any) -> Any:
    if protector is not None:
        return protector
    return dpapi_protector(provider, platform=platform)


# ---------------------------------------------------------------------------
# Enrolment state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Enrolment:
    """What is recorded about an enrolment. Never the token."""

    provider: str
    kind: str
    enrolled_at: float
    consent_statement: str

    def as_record(self) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, "provider": self.provider,
                "kind": self.kind, "enrolled_at": self.enrolled_at,
                "consent_statement": self.consent_statement}


def auth_root(runtime_root: Path | str) -> Path:
    return Path(runtime_root) / AUTH_DIRNAME


def record_path(runtime_root: Path | str, provider: str) -> Path:
    return auth_root(runtime_root) / f"{_provider(provider)}.enrolment.json"


def secret_path(runtime_root: Path | str, provider: str) -> Path:
    return auth_root(runtime_root) / f"{_provider(provider)}.secret"


def lock_path(runtime_root: Path | str, provider: str) -> Path:
    """The handle enrolment and revocation serialise on, per provider.

    Per provider rather than one lock for the directory: enrolling Claude and
    enrolling Codex touch disjoint files, and a shared lock would make one
    wait on the other for no reason. It holds no content and is never read.
    """

    return auth_root(runtime_root) / f"{_provider(provider)}.lock"


def _provider(provider: object) -> str:
    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise AuthError("provider_not_supported")
    return provider


def _validate_token(provider: str, token: object) -> dict[str, str]:
    """Hold the host to exactly the shape the guest will accept.

    Discovering at job time that an enrolled value can never be accepted is a
    failure in the wrong place: the person who could fix it was present at
    enrolment and is not present when a background job runs.
    """

    kind = guest_runner.AUTH_KINDS[provider]
    try:
        capsule = guest_runner.validate_auth({"kind": kind, "token": token},
                                             provider)
    except guest_runner.GuestRunnerError as exc:
        raise AuthError(f"token_{exc.code}") from None
    if capsule is None:  # pragma: no cover - validate_auth cannot return None here
        raise AuthError("token_auth_invalid")
    return capsule


# ---------------------------------------------------------------------------
# Enrol, load, revoke
# ---------------------------------------------------------------------------


def enrol(runtime_root: Path | str, provider: str, token: str, *,
          consent: bool, protector: Any = None, platform: Any = None,
          now: float | None = None) -> Enrolment:
    """Store one session token, encrypted for this account, with consent.

    ``consent`` is a parameter and not an assumption. Enrolment is the moment
    a long-lived credential starts being used by something that runs without
    the user watching, and that is precisely the decision they should be
    making rather than a side effect of setup.
    """

    name = _provider(provider)
    if consent is not True:
        raise AuthError("consent_required")
    capsule = _validate_token(name, token)
    box = _resolve_protector(name, protector, platform)

    root = auth_root(runtime_root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    writer = wpv.platform_secure_writer(platform)
    wpv.require_private_directory(root, platform=platform)

    payload = json.dumps({"schema_version": SCHEMA_VERSION, "provider": name,
                          "kind": capsule["kind"], "token": capsule["token"]},
                         sort_keys=True).encode("utf-8")
    try:
        protected = box.protect(payload)
    except AuthError:
        raise
    except Exception:  # a protector that failed protected nothing
        raise AuthError("os_protection_failed") from None
    finally:
        # Releases this reference, which shortens the plaintext's life. It
        # does not erase anything; see MEMORY_LIFETIME_NOTE.
        del payload
    if not isinstance(protected, (bytes, bytearray)) or not protected:
        raise AuthError("os_protection_failed")
    if len(protected) > MAX_PROTECTED_BYTES:
        raise AuthError("os_protection_failed")

    enrolment = Enrolment(provider=name, kind=capsule["kind"],
                          enrolled_at=float(now if now is not None else time.time()),
                          consent_statement=CONSENT_STATEMENT)

    # Two files, one logical change. There is no atomic replace across two
    # paths, so the failure this guards is a half-applied enrolment: a new
    # secret paired with the previous record, which unprotects to a token that
    # no longer matches what the record says was enrolled.
    #
    # The whole sequence runs under one lock. Without it the read that
    # captures the previous ciphertext, the write, and the rollback are three
    # separate moments, and a second enrolment landing in any of the gaps is
    # either lost or resurrected by the first one's rollback.
    secret = secret_path(runtime_root, name)
    record = record_path(runtime_root, name)
    try:
        with wpv.exclusive_lock(lock_path(runtime_root, name), root=root):
            return _write_enrolment(secret, record, bytes(protected), enrolment,
                                    writer=writer, root=root, platform=platform)
    except wpv.PrivacyError as exc:
        if exc.reason in ("lock_unavailable", "lock_unsupported"):
            raise AuthError("enrolment_busy") from None
        raise AuthError("enrolment_write_failed") from None


def _write_enrolment(secret: Path, record: Path, protected: bytes,
                     enrolment: Enrolment, *, writer: Any, root: Path,
                     platform: Any) -> Enrolment:
    """The two writes, pinned to what was on disk when the lock was taken.

    Every read here goes through the verified-descriptor path, so the bytes
    captured for the rollback are the bytes of the file that was checked, not
    of whatever the name pointed at by the time it was reopened.
    """

    previous, previous_identity = _existing_secret(secret, root=root,
                                                   platform=platform)
    # Secret first: a record without a secret reads as "enrolled" and then
    # fails at job time, which is the confusing order of the two.
    try:
        wpv.atomic_private_write(secret, protected, secure=writer, root=root,
                                 expect_identity=previous_identity)
    except wpv.PrivacyError as exc:
        if exc.reason == "replace_identity_changed":
            # Somebody enrolled between the lock and here, which can only
            # happen if they did not take the lock. Refuse rather than win.
            raise AuthError("enrolment_superseded") from None
        raise
    try:
        written_identity = wpv.file_identity(secret)
    except wpv.PrivacyError:
        written_identity = None

    try:
        wpv.atomic_private_write(
            record,
            (json.dumps(enrolment.as_record(), indent=2, sort_keys=True) + "\n"
             ).encode("utf-8"),
            secure=writer, root=root)
    except (wpv.PrivacyError, OSError):
        complete = _roll_back_secret(secret, previous, writer, root,
                                     expect_identity=written_identity)
        raise AuthError("enrolment_write_failed" if complete
                        else "enrolment_rollback_incomplete") from None
    return enrolment


def _existing_secret(path: Path, *, root: Path,
                     platform: Any) -> tuple[bytes | None, Any]:
    """The protected blob already there and its identity, or (None, None).

    Read through :func:`windows_privacy.read_private_file`, so the descriptor
    that was checked is the descriptor that was read. It is ciphertext, not a
    token: holding it briefly is what makes the rollback real rather than an
    apology in a comment.

    A blob that is present but fails the privacy check is reported as absent
    on purpose. Enrolment then refuses through the identity pin rather than
    silently replacing a file it could not verify.
    """

    try:
        payload, identity = wpv.read_private_file(path, root=root,
                                                  platform=platform)
    except wpv.PrivacyError as exc:
        if exc.reason == "file_absent":
            return None, None
        raise AuthError("enrolment_unreadable") from None
    return payload, identity


def _roll_back_secret(path: Path, previous: bytes | None, writer: Any,
                      root: Path, *, expect_identity: Any) -> bool:
    """Undo a secret write whose record never landed.

    Returns whether the previous state was actually restored, and the caller
    reports the difference. An incomplete rollback is a real state that a
    person has to resolve, so it gets its own reason code rather than being
    folded into the generic write failure.

    ``expect_identity`` is the identity of the secret this call wrote. If the
    file on disk is no longer that one, some other enrolment has landed since
    and the rollback stops: putting the old ciphertext back would destroy a
    newer, complete enrolment in the name of tidying up a failed one.
    """

    try:
        current = wpv.file_identity(path)
    except wpv.PrivacyError:
        # Not being able to identify the file is not evidence that a newer,
        # complete enrolment won the race.  Claiming success here leaves the
        # new secret paired with the old record while hiding that recovery is
        # incomplete.
        return False
    if expect_identity is not None and current != expect_identity:
        # A newer enrolment is on disk. Leave it alone; nothing to undo.
        return True
    try:
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            wpv.atomic_private_write(path, previous, secure=writer, root=root,
                                     expect_identity=current)
    except (wpv.PrivacyError, OSError):
        return False
    return True


def read_enrolment(runtime_root: Path | str, provider: str, *,
                   platform: Any = None) -> Enrolment | None:
    """The recorded enrolment, or None. Never touches the secret."""

    name = _provider(provider)
    path = record_path(runtime_root, name)
    if not path.exists():
        return None
    try:
        payload, _identity = wpv.read_private_file(
            path, root=auth_root(runtime_root), platform=platform)
        raw = json.loads(payload.decode("utf-8"))
    except wpv.PrivacyError as exc:
        if exc.reason == "file_absent":
            return None
        raise AuthError(f"enrolment_{exc.reason}") from None
    except (UnicodeDecodeError, ValueError):
        raise AuthError("enrolment_unreadable") from None
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        raise AuthError("enrolment_unreadable")
    if raw.get("provider") != name or raw.get("kind") != guest_runner.AUTH_KINDS[name]:
        raise AuthError("enrolment_mismatched")
    try:
        enrolled_at = float(raw.get("enrolled_at"))
    except (TypeError, ValueError):
        raise AuthError("enrolment_unreadable") from None
    statement = raw.get("consent_statement")
    if not isinstance(statement, str) or not statement:
        raise AuthError("enrolment_unreadable")
    return Enrolment(provider=name, kind=raw["kind"], enrolled_at=enrolled_at,
                     consent_statement=statement)


def load_capsule(runtime_root: Path | str, provider: str, *,
                 protector: Any = None, platform: Any = None) -> dict[str, str]:
    """Unprotect the session into memory for exactly one request.

    Returns the capsule the guest schema expects. The caller passes it
    straight into the bounded request and lets it go; there is nowhere in this
    project that a returned capsule is written down.
    """

    name = _provider(provider)
    enrolment = read_enrolment(runtime_root, name, platform=platform)
    if enrolment is None:
        raise AuthError("not_enrolled")
    path = secret_path(runtime_root, name)
    try:
        protected, identity = wpv.read_private_file(
            path, root=auth_root(runtime_root), platform=platform,
            max_bytes=MAX_PROTECTED_BYTES)
    except wpv.PrivacyError as exc:
        raise AuthError(f"secret_{exc.reason}") from None
    if len(protected) != identity.size or not protected:
        raise AuthError("secret_unreadable")

    box = _resolve_protector(name, protector, platform)
    try:
        payload = box.unprotect(protected)
    except AuthError:
        raise
    except Exception:
        # Wrong account, wrong machine, or a tampered blob. All the same
        # answer, and none of them say anything about the contents.
        raise AuthError("secret_unprotect_failed") from None
    try:
        raw = json.loads(bytes(payload).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise AuthError("secret_unreadable") from None
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        raise AuthError("secret_unreadable")
    if raw.get("provider") != name or raw.get("kind") != enrolment.kind:
        raise AuthError("secret_mismatched")
    return _validate_token(name, raw.get("token"))


def revoke(runtime_root: Path | str, provider: str) -> str:
    """Remove the local copy. Returns what to tell the person who asked.

    Missing files are not an error: "make sure this is not stored here" has
    the same right answer whether or not it was.
    """

    name = _provider(provider)
    root = auth_root(runtime_root)
    if not root.is_dir():
        # Nothing was ever stored here. Saying so is the right answer, and
        # creating a directory to lock in would be a side effect of a
        # question, not an answer to it.
        return REVOCATION_NOTE
    try:
        # Under the same lock as enrolment. Removing the secret while an
        # enrolment is mid-sequence is how a record survives with no key.
        with wpv.exclusive_lock(lock_path(runtime_root, name), root=root):
            _remove_enrolment_files(runtime_root, name)
    except wpv.PrivacyError as exc:
        if exc.reason in ("lock_unavailable", "lock_unsupported"):
            raise AuthError("enrolment_busy") from None
        raise AuthError("revocation_failed") from None
    return REVOCATION_NOTE


def _remove_enrolment_files(runtime_root: Path | str, name: str) -> None:
    for path in (secret_path(runtime_root, name), record_path(runtime_root, name)):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            raise AuthError("revocation_failed") from None


# ---------------------------------------------------------------------------
# The state a setup screen renders
# ---------------------------------------------------------------------------


def enrolment_state(runtime_root: Path | str, provider: str, *,
                    platform: Any = None, lane_open: bool | None = None
                    ) -> dict[str, Any]:
    """Everything a non-technical setup screen needs, in plain language.

    One dict per provider, with a status a UI can branch on and a sentence a
    person can act on. Deliberately never raises: a setup screen that cannot
    render because state is broken is the worst time to lose the explanation
    of what is broken.
    """

    try:
        name = _provider(provider)
    except AuthError as exc:
        return {"provider": str(provider), "status": "unsupported",
                "headline": "This provider is not supported.",
                "next_step": "", "reason": exc.reason,
                "consent_recorded": False, "enrolled_at": None}

    state: dict[str, Any] = {"provider": name, "consent_recorded": False,
                             "enrolled_at": None, "reason": ""}
    module = platform if platform is not None else __import__("os")
    if getattr(module, "name", "") != "nt":
        state.update(status="unsupported_platform",
                     headline="Saving a session securely needs Windows.",
                     next_step=(
                         "This machine cannot store the token in a way only "
                         "your account can read, so nothing is saved here."))
        return state

    try:
        enrolment = read_enrolment(runtime_root, name, platform=platform)
    except AuthError as exc:
        state.update(status="unreadable", reason=exc.reason,
                     headline="The saved session could not be read.",
                     next_step=(
                         "Remove it and enrol again: " + ENROLMENT_STEPS[name]))
        return state

    if enrolment is None:
        state.update(status="not_enrolled",
                     headline="No session is saved for this provider.",
                     next_step=ENROLMENT_STEPS[name])
        return state

    state.update(status="enrolled", consent_recorded=True,
                 enrolled_at=enrolment.enrolled_at,
                 headline="A session is saved, encrypted for your account.",
                 next_step=("Nothing to do. Remove it here whenever you want; "
                            + REVOCATION_NOTE))
    if lane_open is False:
        # Being enrolled and being usable are different facts, and a screen
        # that showed only the first would promise something that will refuse.
        state.update(status="enrolled_lane_closed",
                     headline=("A session is saved, but jobs are still "
                               "blocked."),
                     next_step=("Finish verifying the guest runtime. Until "
                                "that passes, the saved session is not used."))
    return state


def enrolment_states(runtime_root: Path | str, *, platform: Any = None,
                     lane: Any = None) -> list[dict[str, Any]]:
    """One state per supported provider, in a fixed order."""

    return [enrolment_state(
                runtime_root, provider, platform=platform,
                lane_open=(None if lane is None
                           else bool(lane.enabled_for(provider))))
            for provider in PROVIDERS]


def redact(text: str, capsule: Mapping[str, str] | None) -> str:
    """Host-side counterpart to the guest's redaction, for the same reason."""

    if not capsule:
        return text
    token = capsule.get("token", "")
    if not token:
        return text
    return text.replace(token, "[redacted]")
