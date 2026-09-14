"""Host-side subscription enrolment and handoff.

The property under test throughout is that a token exists on disk only as a
blob some OS-protected mechanism produced, exists in memory only for the
duration of one request, and never appears in an error, a record or a log.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_bridge.orchestration import guest_runner as gr
from agent_bridge.orchestration import windows_auth as wa
from agent_bridge.orchestration import windows_delegation as wd

TOKEN = "sk-ant-oat01-" + "z" * 40
CODEX_TOKEN = "codex-access-" + "y" * 40

class _WindowsLike:
    """A platform layer that enforces owner-only the way the real one claims to.

    Not a permissive stub: ``verify_owner_only_path`` reads the mode actually
    on the file, so a test that loosens permissions genuinely fails the check
    rather than passing through a fake that always says yes.
    """

    name = "nt"

    def enforce_owner_only_file(self, descriptor: int) -> None:
        os.fchmod(descriptor, 0o600)

    def verify_owner_only_path(self, directory, probe_file):
        info = os.stat(probe_file)
        return (info.st_mode & 0o077) == 0, "mode"


NT = _WindowsLike()


class _Protector:
    """A stand-in for DPAPI that is reversible but not plaintext.

    Deliberately not a no-op: a test that "protected" by returning its input
    would pass while the real thing stored the token in the clear.
    """

    def __init__(self, tag: bytes = b"box:") -> None:
        self.tag = tag
        self.protect_calls = 0
        self.unprotect_calls = 0

    def protect(self, payload: bytes) -> bytes:
        self.protect_calls += 1
        return self.tag + bytes(byte ^ 0x5A for byte in payload)

    def unprotect(self, payload: bytes) -> bytes:
        self.unprotect_calls += 1
        if not payload.startswith(self.tag):
            raise ValueError("not mine")
        return bytes(byte ^ 0x5A for byte in payload[len(self.tag):])


class _Posix:
    name = "posix"


class _Broken:
    def protect(self, payload: bytes) -> bytes:
        raise RuntimeError("no key material")

    def unprotect(self, payload: bytes) -> bytes:
        raise RuntimeError("wrong account")


class AuthTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "runtime"
        self.root.mkdir(mode=0o700)
        self.box = _Protector()

    def _enrol(self, provider: str = "claude", token: str = TOKEN, **kwargs):
        options = {"consent": True, "protector": self.box, "platform": NT,
                   "now": 1000.0}
        options.update(kwargs)
        return wa.enrol(self.root, provider, token, **options)


class EnrolmentTests(AuthTestCase):
    def test_a_session_is_stored_only_as_protected_bytes(self):
        self._enrol()
        blob = wa.secret_path(self.root, "claude").read_bytes()
        self.assertNotIn(TOKEN.encode("utf-8"), blob)
        self.assertTrue(blob.startswith(b"box:"))
        self.assertEqual(self.box.protect_calls, 1)

    def test_the_record_beside_it_never_carries_the_token(self):
        self._enrol()
        text = wa.record_path(self.root, "claude").read_text(encoding="utf-8")
        self.assertNotIn(TOKEN, text)
        raw = json.loads(text)
        self.assertEqual(raw["provider"], "claude")
        self.assertEqual(raw["kind"], gr.AUTH_KIND_CLAUDE_OAUTH)
        self.assertEqual(raw["consent_statement"], wa.CONSENT_STATEMENT)
        self.assertNotIn("token", raw)

    def test_both_files_are_owner_only(self):
        self._enrol()
        for path in (wa.secret_path(self.root, "claude"),
                     wa.record_path(self.root, "claude")):
            self.assertEqual(path.stat().st_mode & 0o077, 0, path.name)
        self.assertEqual(wa.auth_root(self.root).stat().st_mode & 0o077, 0)

    def test_enrolment_without_consent_is_refused(self):
        with self.assertRaises(wa.AuthError) as caught:
            self._enrol(consent=False)
        self.assertEqual(caught.exception.reason, "consent_required")
        self.assertFalse(wa.secret_path(self.root, "claude").exists())

    def test_consent_must_be_a_real_yes_not_a_truthy_value(self):
        with self.assertRaises(wa.AuthError):
            self._enrol(consent="sure")

    def test_an_unsupported_provider_is_refused(self):
        with self.assertRaises(wa.AuthError) as caught:
            wa.enrol(self.root, "gemini", TOKEN, consent=True,
                     protector=self.box, platform=NT)
        self.assertEqual(caught.exception.reason, "provider_not_supported")

    def test_a_token_the_guest_would_reject_is_refused_at_enrolment(self):
        """Fail where the person who can fix it is standing."""
        for bad in ("", "has space", "has\nnewline", "x" * (gr.MAX_AUTH_TOKEN_BYTES + 1)):
            with self.assertRaises(wa.AuthError) as caught:
                self._enrol(token=bad)
            self.assertTrue(caught.exception.reason.startswith("token_"),
                            caught.exception.reason)
            if bad:
                self.assertNotIn(bad, str(caught.exception))

    def test_a_protector_that_fails_stores_nothing(self):
        with self.assertRaises(wa.AuthError) as caught:
            self._enrol(protector=_Broken())
        self.assertEqual(caught.exception.reason, "os_protection_failed")
        self.assertFalse(wa.secret_path(self.root, "claude").exists())
        self.assertFalse(wa.record_path(self.root, "claude").exists())

    def test_both_providers_can_be_enrolled_independently(self):
        self._enrol("claude", TOKEN)
        self._enrol("codex", CODEX_TOKEN)
        self.assertEqual(wa.load_capsule(self.root, "codex", protector=self.box,
                                         platform=NT),
                         {"kind": gr.AUTH_KIND_CODEX_ACCESS, "token": CODEX_TOKEN})
        self.assertEqual(
            wa.load_capsule(self.root, "claude", protector=self.box,
                            platform=NT)["token"], TOKEN)

    def test_re_enrolling_replaces_without_leaving_the_old_blob(self):
        self._enrol()
        replacement = "sk-ant-oat01-" + "q" * 40
        self._enrol(token=replacement)
        blob = wa.secret_path(self.root, "claude").read_bytes()
        self.assertNotIn(TOKEN.encode("utf-8"), blob)
        self.assertEqual(
            wa.load_capsule(self.root, "claude", protector=self.box,
                            platform=NT)["token"], replacement)


class HandoffTests(AuthTestCase):
    def test_the_capsule_is_exactly_what_the_guest_schema_accepts(self):
        self._enrol()
        capsule = wa.load_capsule(self.root, "claude", protector=self.box,
                                  platform=NT)
        self.assertEqual(gr.validate_auth(capsule, "claude"), capsule)

    def test_loading_without_an_enrolment_is_named_not_guessed(self):
        with self.assertRaises(wa.AuthError) as caught:
            wa.load_capsule(self.root, "claude", protector=self.box, platform=NT)
        self.assertEqual(caught.exception.reason, "not_enrolled")

    def test_a_blob_from_another_account_cannot_be_read(self):
        self._enrol()
        with self.assertRaises(wa.AuthError) as caught:
            wa.load_capsule(self.root, "claude", protector=_Protector(b"other:"),
                            platform=NT)
        self.assertEqual(caught.exception.reason, "secret_unprotect_failed")

    def test_a_tampered_blob_is_refused(self):
        self._enrol()
        path = wa.secret_path(self.root, "claude")
        path.write_bytes(b"box:" + b"\x00" * 32)
        os.chmod(path, 0o600)
        with self.assertRaises(wa.AuthError) as caught:
            wa.load_capsule(self.root, "claude", protector=self.box, platform=NT)
        self.assertEqual(caught.exception.reason, "secret_unreadable")

    def test_a_blob_relabelled_as_the_other_provider_is_refused(self):
        self._enrol("claude", TOKEN)
        payload = json.dumps({"schema_version": wa.SCHEMA_VERSION,
                              "provider": "codex",
                              "kind": gr.AUTH_KIND_CLAUDE_OAUTH,
                              "token": TOKEN}, sort_keys=True).encode("utf-8")
        path = wa.secret_path(self.root, "claude")
        path.write_bytes(self.box.protect(payload))
        os.chmod(path, 0o600)
        with self.assertRaises(wa.AuthError) as caught:
            wa.load_capsule(self.root, "claude", protector=self.box, platform=NT)
        self.assertEqual(caught.exception.reason, "secret_mismatched")

    def test_a_world_readable_secret_is_refused_rather_than_used(self):
        self._enrol()
        os.chmod(wa.secret_path(self.root, "claude"), 0o644)
        with self.assertRaises(wa.AuthError) as caught:
            wa.load_capsule(self.root, "claude", protector=self.box, platform=NT)
        self.assertEqual(caught.exception.reason, "secret_file_not_owner_only")

    def test_a_world_readable_record_is_refused(self):
        self._enrol()
        os.chmod(wa.record_path(self.root, "claude"), 0o644)
        with self.assertRaises(wa.AuthError) as caught:
            wa.load_capsule(self.root, "claude", protector=self.box, platform=NT)
        self.assertEqual(caught.exception.reason, "enrolment_file_not_owner_only")

    def test_a_record_for_a_different_kind_is_refused(self):
        self._enrol()
        path = wa.record_path(self.root, "claude")
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["kind"] = gr.AUTH_KIND_CODEX_ACCESS
        path.write_text(json.dumps(raw), encoding="utf-8")
        os.chmod(path, 0o600)
        with self.assertRaises(wa.AuthError) as caught:
            wa.read_enrolment(self.root, "claude", platform=NT)
        self.assertEqual(caught.exception.reason, "enrolment_mismatched")

    def test_no_error_message_anywhere_contains_the_token(self):
        self._enrol()
        os.chmod(wa.secret_path(self.root, "claude"), 0o644)
        for call in (lambda: wa.load_capsule(self.root, "claude",
                                             protector=self.box, platform=NT),
                     lambda: wa.load_capsule(self.root, "codex",
                                             protector=self.box, platform=NT),
                     lambda: wa.enrol(self.root, "claude", "bad token",
                                      consent=True, protector=self.box,
                                      platform=NT)):
            with self.assertRaises(wa.AuthError) as caught:
                call()
            self.assertNotIn(TOKEN, str(caught.exception))
            self.assertNotIn("bad token", str(caught.exception))

    def test_redaction_removes_a_token_from_host_text(self):
        capsule = {"kind": gr.AUTH_KIND_CLAUDE_OAUTH, "token": TOKEN}
        self.assertEqual(wa.redact(f"leaked {TOKEN} here", capsule),
                         "leaked [redacted] here")
        self.assertEqual(wa.redact("nothing", None), "nothing")


class RevocationTests(AuthTestCase):
    def test_revoking_removes_both_files(self):
        self._enrol()
        note = wa.revoke(self.root, "claude")
        self.assertFalse(wa.secret_path(self.root, "claude").exists())
        self.assertFalse(wa.record_path(self.root, "claude").exists())
        self.assertEqual(note, wa.REVOCATION_NOTE)

    def test_the_note_does_not_pretend_the_token_was_cancelled(self):
        self.assertIn("revoke it with the provider", wa.REVOCATION_NOTE)

    def test_revoking_what_was_never_enrolled_is_not_an_error(self):
        self.assertEqual(wa.revoke(self.root, "codex"), wa.REVOCATION_NOTE)

    def test_revoking_one_provider_leaves_the_other(self):
        self._enrol("claude", TOKEN)
        self._enrol("codex", CODEX_TOKEN)
        wa.revoke(self.root, "claude")
        self.assertTrue(wa.secret_path(self.root, "codex").exists())


class EnrolmentStateTests(AuthTestCase):
    def test_a_fresh_machine_is_told_exactly_what_to_do(self):
        state = wa.enrolment_state(self.root, "claude", platform=NT)
        self.assertEqual(state["status"], "not_enrolled")
        self.assertEqual(state["next_step"], wa.ENROLMENT_STEPS["claude"])
        self.assertFalse(state["consent_recorded"])

    def test_an_enrolled_provider_reports_consent_and_a_way_out(self):
        self._enrol()
        state = wa.enrolment_state(self.root, "claude", platform=NT)
        self.assertEqual(state["status"], "enrolled")
        self.assertTrue(state["consent_recorded"])
        self.assertEqual(state["enrolled_at"], 1000.0)
        self.assertIn("Remove it here", state["next_step"])

    def test_enrolled_but_unverified_is_a_different_status(self):
        """Being saved and being usable are separate facts."""
        self._enrol()
        state = wa.enrolment_state(self.root, "claude", platform=NT,
                                   lane_open=False)
        self.assertEqual(state["status"], "enrolled_lane_closed")
        self.assertIn("still", state["headline"])

    def test_a_broken_record_still_renders_a_screen(self):
        self._enrol()
        path = wa.record_path(self.root, "claude")
        path.write_text("{not json", encoding="utf-8")
        os.chmod(path, 0o600)
        state = wa.enrolment_state(self.root, "claude", platform=NT)
        self.assertEqual(state["status"], "unreadable")
        self.assertIn(wa.ENROLMENT_STEPS["claude"], state["next_step"])

    def test_a_non_windows_machine_says_so_rather_than_storing_anything(self):
        state = wa.enrolment_state(self.root, "claude",
                                   platform=_Posix())
        self.assertEqual(state["status"], "unsupported_platform")
        self.assertFalse(wa.auth_root(self.root).exists())

    def test_every_provider_gets_a_state_in_a_fixed_order(self):
        states = wa.enrolment_states(self.root, platform=NT)
        self.assertEqual([one["provider"] for one in states], list(wa.PROVIDERS))

    def test_the_lane_object_drives_the_per_provider_status(self):
        self._enrol("claude", TOKEN)
        self._enrol("codex", CODEX_TOKEN)

        class _Lane:
            def enabled_for(self, provider):
                return provider == "claude"

        states = {one["provider"]: one for one in
                  wa.enrolment_states(self.root, platform=NT, lane=_Lane())}
        self.assertEqual(states["claude"]["status"], "enrolled")
        self.assertEqual(states["codex"]["status"], "enrolled_lane_closed")


class OsProtectionTests(AuthTestCase):
    def test_there_is_no_plaintext_fallback_off_windows(self):
        with self.assertRaises(wa.AuthError) as caught:
            wa.dpapi_protector("claude",
                               platform=_Posix())
        self.assertEqual(caught.exception.reason, "os_protection_unavailable")

    def test_enrolling_without_a_protector_off_windows_stores_nothing(self):
        with self.assertRaises(wa.AuthError) as caught:
            wa.enrol(self.root, "claude", TOKEN, consent=True,
                     platform=_Posix())
        self.assertEqual(caught.exception.reason, "os_protection_unavailable")
        self.assertFalse(wa.secret_path(self.root, "claude").exists())

    def test_each_provider_gets_its_own_entropy(self):
        self.assertNotEqual(wa._entropy("claude"), wa._entropy("codex"))


class ExecutorWiringTests(AuthTestCase):
    """The executor's session source is this enrolment, gated on the lane."""

    def test_a_closed_lane_never_reaches_a_decryption_call(self):
        self._enrol()

        class _Closed:
            def enabled_for(self, provider):
                return False

        source = wd.enrolled_auth_source(self.root, lane=_Closed(),
                                         protector=self.box, platform=NT)
        with self.assertRaises(wd.DelegationRefused) as caught:
            source("claude")
        self.assertEqual(caught.exception.reason, "provider_lane_unverified")
        self.assertEqual(self.box.unprotect_calls, 0)

    def test_an_open_lane_hands_over_the_enrolled_capsule(self):
        self._enrol()

        class _Open:
            def enabled_for(self, provider):
                return True

        source = wd.enrolled_auth_source(self.root, lane=_Open(),
                                         protector=self.box, platform=NT)
        self.assertEqual(source("claude"),
                         {"kind": gr.AUTH_KIND_CLAUDE_OAUTH, "token": TOKEN})

    def test_a_missing_enrolment_refuses_by_name_without_the_token(self):
        source = wd.enrolled_auth_source(self.root, protector=self.box,
                                         platform=NT)
        with self.assertRaises(wd.DelegationRefused) as caught:
            source("codex")
        self.assertEqual(caught.exception.reason, "provider_session_unavailable")
        self.assertIn("codex", caught.exception.detail)
        self.assertNotIn(TOKEN, caught.exception.detail)

    def test_the_default_executor_source_still_refuses_by_name(self):
        with self.assertRaises(wd.DelegationRefused) as caught:
            wd._no_auth_source("claude")
        self.assertEqual(caught.exception.reason, "provider_session_unavailable")


class EnrolmentRollbackTests(AuthTestCase):
    """Two files, one logical change, and no half-applied state.

    The failure these guard is a new secret paired with the old record: the
    record then describes an enrolment whose ciphertext unprotects to a
    different token, and the mismatch only surfaces at job time.
    """

    def _fail_on(self, target: Path):
        real = wa.wpv.atomic_private_write

        def guarded(path, payload, **kwargs):
            if Path(path) == target:
                raise OSError("record write refused")
            return real(path, payload, **kwargs)

        return mock.patch.object(wa.wpv, "atomic_private_write", guarded)

    def test_a_failed_record_write_restores_the_previous_secret(self):
        self._enrol()
        secret = wa.secret_path(self.root, "claude")
        before = secret.read_bytes()

        with self._fail_on(wa.record_path(self.root, "claude")):
            with self.assertRaises(wa.AuthError) as caught:
                self._enrol(token="sk-ant-oat01-" + "q" * 40)
        self.assertEqual(caught.exception.reason, "enrolment_write_failed")
        self.assertEqual(secret.read_bytes(), before)

    def test_a_failed_first_enrolment_leaves_no_orphan_secret(self):
        secret = wa.secret_path(self.root, "claude")
        with self._fail_on(wa.record_path(self.root, "claude")):
            with self.assertRaises(wa.AuthError):
                self._enrol()
        self.assertFalse(secret.exists())

    def test_the_rolled_back_secret_still_unprotects_the_original_token(self):
        self._enrol()
        with self._fail_on(wa.record_path(self.root, "claude")):
            with self.assertRaises(wa.AuthError):
                self._enrol(token="sk-ant-oat01-" + "q" * 40)
        capsule = wa.load_capsule(self.root, "claude", protector=self.box,
                                  platform=NT)
        self.assertEqual(capsule["token"], TOKEN)

    def test_the_failure_reason_carries_no_token_material(self):
        with self._fail_on(wa.record_path(self.root, "claude")):
            with self.assertRaises(wa.AuthError) as caught:
                self._enrol()
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertNotIn(TOKEN[-8:], str(caught.exception))


class DescriptorBoundReadTests(AuthTestCase):
    """The checked file is the read file, not a name checked and reopened."""

    def test_the_record_is_read_through_the_privacy_layer(self):
        self._enrol()
        calls = []
        real = wa.wpv.read_private_file

        def counting(path, **kwargs):
            calls.append(Path(path).name)
            return real(path, **kwargs)

        with mock.patch.object(wa.wpv, "read_private_file", counting):
            wa.read_enrolment(self.root, "claude", platform=NT)
        self.assertEqual(calls, [wa.record_path(self.root, "claude").name])

    def test_the_secret_is_read_through_the_privacy_layer(self):
        self._enrol()
        names = []
        real = wa.wpv.read_private_file

        def counting(path, **kwargs):
            names.append(Path(path).name)
            return real(path, **kwargs)

        with mock.patch.object(wa.wpv, "read_private_file", counting):
            wa.load_capsule(self.root, "claude", protector=self.box, platform=NT)
        self.assertIn(wa.secret_path(self.root, "claude").name, names)

    @unittest.skipIf(os.name == "nt", "POSIX mode bits")
    def test_a_secret_anyone_could_read_is_refused_not_repaired(self):
        self._enrol()
        secret = wa.secret_path(self.root, "claude")
        os.chmod(secret, 0o644)
        with self.assertRaises(wa.AuthError):
            wa.load_capsule(self.root, "claude", protector=self.box, platform=NT)
        self.assertEqual(secret.stat().st_mode & 0o777, 0o644)

    def test_a_secret_swapped_mid_read_is_refused(self):
        self._enrol()
        real_fstat = os.fstat
        seen = []

        class _Drifted:
            def __init__(self, info):
                self._info = info
                self.st_ino = info.st_ino + 1

            def __getattr__(self, name):
                return getattr(self._info, name)

        def drifting(descriptor):
            info = real_fstat(descriptor)
            seen.append(descriptor)
            return _Drifted(info) if len(seen) > 1 else info

        with mock.patch.object(wa.wpv.os, "fstat", drifting):
            with self.assertRaises(wa.AuthError):
                wa.load_capsule(self.root, "claude", protector=self.box,
                                platform=NT)


class MemoryHonestyTests(unittest.TestCase):
    """The note says what Python cannot do, and nothing claims otherwise."""

    def test_the_note_refuses_to_claim_erasure(self):
        self.assertIn("cannot guarantee", wa.MEMORY_LIFETIME_NOTE)
        self.assertIn("not", wa.MEMORY_LIFETIME_NOTE)

    def test_no_comment_claims_a_token_is_wiped_or_zeroized(self):
        source = (ROOT / "src" / "agent_bridge" / "orchestration" /
                  "windows_auth.py").read_text(encoding="utf-8")
        lowered = source.lower()
        for claim in ("zeroize", "zeroise", "scrubbed from memory",
                      "erased from memory", "wiped from memory"):
            if claim in lowered:
                # Permitted only where the text is denying it.
                for line in lowered.splitlines():
                    if claim in line:
                        self.assertTrue(
                            any(word in line for word in
                                ("not ", "cannot", "no ", "never")),
                            line)



class EnrolmentLockingTests(AuthTestCase):
    """Concurrent enrolment, which the rollback was not safe against.

    The sequence "read the old ciphertext, write the new one, put the old one
    back if the record fails" is only correct if nothing else writes in
    between. Unserialised, a second enrolment landing in any of those gaps is
    either lost or destroyed by the first one's rollback, and the machine ends
    up with a secret and a record describing different tokens.
    """

    def test_the_lock_is_taken_for_the_whole_write_sequence(self):
        held = []
        real = wa.wpv.exclusive_lock

        @contextlib.contextmanager
        def watched(path, **kwargs):
            with real(path, **kwargs) as descriptor:
                held.append(Path(path).name)
                yield descriptor

        with mock.patch.object(wa.wpv, "exclusive_lock", watched):
            self._enrol()
        self.assertEqual(held, [wa.lock_path(self.root, "claude").name])

    def test_a_lock_another_holder_will_not_release_refuses_rather_than_waits(self):
        self._enrol()
        with wa.wpv.exclusive_lock(wa.lock_path(self.root, "claude")):
            with self.assertRaises(wa.AuthError) as caught:
                self._enrol_with_short_lock_timeout()
        self.assertEqual(caught.exception.reason, "enrolment_busy")

    def _enrol_with_short_lock_timeout(self):
        real = wa.wpv.exclusive_lock

        def impatient(path, **kwargs):
            kwargs["timeout"] = 0.05
            return real(path, **kwargs)

        with mock.patch.object(wa.wpv, "exclusive_lock", impatient):
            return self._enrol()

    def test_two_enrolments_in_sequence_leave_a_matching_pair(self):
        """The invariant the rollback exists to protect, stated directly."""

        self._enrol()
        second = "sk-ant-oat01-" + "w" * 40
        self._enrol(token=second)
        capsule = wa.load_capsule(self.root, "claude", protector=self.box,
                                  platform=NT)
        record = wa.read_enrolment(self.root, "claude", platform=NT)
        self.assertEqual(capsule["token"], second)
        self.assertEqual(capsule["kind"], record.kind)

    def test_an_enrolment_that_lost_the_race_refuses_instead_of_winning(self):
        """A writer that did not take the lock must still not be clobbered.

        The identity pin is the second line of defence: it does not need the
        other writer to have cooperated.
        """

        self._enrol()
        secret = wa.secret_path(self.root, "claude")
        real = wa.wpv.atomic_private_write

        def racing(path, payload, **kwargs):
            if Path(path) == secret and kwargs.get("expect_identity") is not None:
                # Somebody else's enrolment lands after ours was captured.
                real(path, b"a newer enrolment", secure=kwargs["secure"],
                     root=kwargs["root"])
            return real(path, payload, **kwargs)

        with mock.patch.object(wa.wpv, "atomic_private_write", racing):
            with self.assertRaises(wa.AuthError) as caught:
                self._enrol(token="sk-ant-oat01-" + "v" * 40)
        self.assertEqual(caught.exception.reason, "enrolment_superseded")
        self.assertEqual(secret.read_bytes(), b"a newer enrolment")

    def test_a_rollback_never_overwrites_a_newer_enrolment(self):
        """The rollback's own race: the record write fails, and by the time
        the undo runs somebody has completed a different enrolment."""

        self._enrol()
        secret = wa.secret_path(self.root, "claude")
        record = wa.record_path(self.root, "claude")
        real = wa.wpv.atomic_private_write

        def guarded(path, payload, **kwargs):
            if Path(path) == record:
                # Our record write fails, and a newer secret appears before
                # the rollback gets a chance to put the old one back.
                real(secret, b"newer complete enrolment",
                     secure=kwargs["secure"], root=kwargs["root"])
                raise OSError("record write refused")
            return real(path, payload, **kwargs)

        with mock.patch.object(wa.wpv, "atomic_private_write", guarded):
            with self.assertRaises(wa.AuthError):
                self._enrol(token="sk-ant-oat01-" + "u" * 40)
        self.assertEqual(secret.read_bytes(), b"newer complete enrolment")

    def test_an_incomplete_rollback_is_surfaced_and_not_hidden(self):
        self._enrol()
        record = wa.record_path(self.root, "claude")
        secret = wa.secret_path(self.root, "claude")
        real = wa.wpv.atomic_private_write
        state = {"failed_record": False}

        def guarded(path, payload, **kwargs):
            if Path(path) == record:
                state["failed_record"] = True
                raise OSError("record write refused")
            if state["failed_record"] and Path(path) == secret:
                raise OSError("rollback refused too")
            return real(path, payload, **kwargs)

        with mock.patch.object(wa.wpv, "atomic_private_write", guarded):
            with self.assertRaises(wa.AuthError) as caught:
                self._enrol(token="sk-ant-oat01-" + "t" * 40)
        self.assertEqual(caught.exception.reason, "enrolment_rollback_incomplete")

    def test_the_previous_ciphertext_is_captured_through_the_privacy_layer(self):
        """Not by reopening the pathname. The checked file is the read file."""

        self._enrol()
        reads = []
        real = wa.wpv.read_private_file

        def counting(path, **kwargs):
            reads.append(Path(path).name)
            return real(path, **kwargs)

        with mock.patch.object(wa.wpv, "read_private_file", counting):
            self._enrol(token="sk-ant-oat01-" + "s" * 40)
        self.assertIn(wa.secret_path(self.root, "claude").name, reads)

    @unittest.skipIf(os.name == "nt", "POSIX mode bits")
    def test_an_unverifiable_previous_secret_stops_the_enrolment(self):
        """A secret that fails the privacy check is not quietly replaced."""

        self._enrol()
        os.chmod(wa.secret_path(self.root, "claude"), 0o644)
        with self.assertRaises(wa.AuthError) as caught:
            self._enrol(token="sk-ant-oat01-" + "r" * 40)
        self.assertEqual(caught.exception.reason, "enrolment_unreadable")

    def test_revocation_takes_the_same_lock(self):
        self._enrol()
        held = []
        real = wa.wpv.exclusive_lock

        @contextlib.contextmanager
        def watched(path, **kwargs):
            with real(path, **kwargs) as descriptor:
                held.append(Path(path).name)
                yield descriptor

        with mock.patch.object(wa.wpv, "exclusive_lock", watched):
            wa.revoke(self.root, "claude")
        self.assertEqual(held, [wa.lock_path(self.root, "claude").name])

    def test_the_lock_file_never_holds_any_content(self):
        self._enrol()
        self.assertEqual(wa.lock_path(self.root, "claude").read_bytes(), b"")



class NoCredentialStoreTests(unittest.TestCase):
    """The design claim, asserted rather than only documented."""

    def test_nothing_reads_a_provider_credential_directory(self):
        source = (ROOT / "src" / "agent_bridge" / "orchestration" /
                  "windows_auth.py").read_text(encoding="utf-8")
        body = source.split('"""', 2)[2]
        for forbidden in (".claude", ".codex", "CLAUDE_CONFIG_DIR", "CODEX_HOME",
                          "expanduser", "copytree"):
            self.assertNotIn(forbidden, body, forbidden)


if __name__ == "__main__":
    unittest.main()
