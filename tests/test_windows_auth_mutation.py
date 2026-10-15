"""Focused regressions for the Windows auth mutation probe."""

from __future__ import annotations

import ctypes
import json
import os
from types import SimpleNamespace
import unittest
from unittest import mock

from test_windows_auth import AuthTestCase, NT, TOKEN, wa


class AuthPathTests(AuthTestCase):
    def test_auth_root_uses_the_auth_directory(self):
        self.assertEqual(wa.auth_root(self.root), self.root / "auth")

    def test_record_path_uses_the_enrolment_suffix(self):
        self.assertEqual(wa.record_path(self.root, "claude"),
                         self.root / "auth" / "claude.enrolment.json")

    def test_secret_path_uses_the_secret_suffix(self):
        self.assertEqual(wa.secret_path(self.root, "claude"),
                         self.root / "auth" / "claude.secret")

    def test_lock_path_uses_the_lock_suffix(self):
        self.assertEqual(wa.lock_path(self.root, "claude"),
                         self.root / "auth" / "claude.lock")


class EnrolBoundaryTests(AuthTestCase):
    def test_zero_timestamp_is_preserved(self):
        enrolment = self._enrol(now=0.0)
        self.assertEqual(enrolment.enrolled_at, 0.0)


class ReadEnrolmentBoundaryTests(AuthTestCase):
    def _change_record(self, key, value):
        self._enrol()
        path = wa.record_path(self.root, "claude")
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw[key] = value
        path.write_text(json.dumps(raw), encoding="utf-8")
        os.chmod(path, 0o600)

    def test_wrong_record_schema_is_unreadable(self):
        self._change_record("schema_version", wa.SCHEMA_VERSION + 1)
        with self.assertRaises(wa.AuthError) as caught:
            wa.read_enrolment(self.root, "claude", platform=NT)
        self.assertEqual(caught.exception.reason, "enrolment_unreadable")

    def test_empty_consent_statement_is_unreadable(self):
        self._change_record("consent_statement", "")
        with self.assertRaises(wa.AuthError) as caught:
            wa.read_enrolment(self.root, "claude", platform=NT)
        self.assertEqual(caught.exception.reason, "enrolment_unreadable")


class LoadCapsuleBoundaryTests(AuthTestCase):
    def test_wrong_secret_schema_is_unreadable(self):
        self._enrol()
        path = wa.secret_path(self.root, "claude")
        raw = json.loads(self.box.unprotect(path.read_bytes()).decode("utf-8"))
        raw["schema_version"] = wa.SCHEMA_VERSION + 1
        path.write_bytes(self.box.protect(json.dumps(raw).encode("utf-8")))
        os.chmod(path, 0o600)
        with self.assertRaises(wa.AuthError) as caught:
            wa.load_capsule(self.root, "claude", protector=self.box, platform=NT)
        self.assertEqual(caught.exception.reason, "secret_unreadable")


class RevokeBoundaryTests(AuthTestCase):
    def test_missing_secret_does_not_prevent_record_removal(self):
        self._enrol()
        wa.secret_path(self.root, "claude").unlink()
        self.assertEqual(wa.revoke(self.root, "claude"), wa.REVOCATION_NOTE)
        self.assertFalse(wa.record_path(self.root, "claude").exists())


class _FakeWindll:
    def __init__(self, *, succeeds=True):
        self.buffers = []
        self.flags = []
        self.succeeds = succeeds
        self.crypt32 = SimpleNamespace(CryptProtectData=self.call,
                                       CryptUnprotectData=self.call)
        self.kernel32 = SimpleNamespace(LocalFree=lambda pointer: None)

    def call(self, data, description, entropy, reserved, prompt, flags, out):
        self.flags.append(flags)
        if not self.succeeds:
            return 0
        buffer = ctypes.create_string_buffer(b"sealed", 6)
        self.buffers.append(buffer)
        blob = ctypes.cast(out, ctypes.POINTER(wa._Blob)).contents
        blob.cbData = 6
        blob.pbData = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))
        return 1


class DpapiBoundaryTests(unittest.TestCase):
    def _box(self, fake):
        with mock.patch.object(wa.ctypes, "windll", fake, create=True):
            return wa.dpapi_protector("claude", platform=SimpleNamespace(name="nt"))

    def test_windows_branch_creates_a_protector(self):
        fake = _FakeWindll()
        self.assertIsNotNone(self._box(fake))

    def test_failed_dpapi_call_raises_fixed_error(self):
        fake = _FakeWindll(succeeds=False)
        box = self._box(fake)
        with self.assertRaises(wa.AuthError) as caught:
            box.protect(b"invented-session")
        self.assertEqual(caught.exception.reason, "os_protection_failed")

    def test_protect_returns_dpapi_output(self):
        fake = _FakeWindll()
        box = self._box(fake)
        self.assertEqual(box.protect(b"invented-session"), b"sealed")
        self.assertEqual(fake.flags, [0x1])

    def test_unprotect_returns_dpapi_output(self):
        fake = _FakeWindll()
        box = self._box(fake)
        self.assertEqual(box.unprotect(b"invented-ciphertext"), b"sealed")


if __name__ == "__main__":
    unittest.main()
