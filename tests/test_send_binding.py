"""Each client-derived cloud send is bound to its exact bytes (workbench U3).

Synthetic data only. Every test points the runner state home at a temporary
directory and never touches the real HOME.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import operator_isolation  # noqa: E402,F401

from agent_bridge import operator_settings, send_binding as sb  # noqa: E402

CLEAN = "Summarize the synthetic ledger for Example Co: revenue up, costs flat.\n"
SYNTHETIC_EMAIL = "Reach the synthetic contact at test.person@example.invalid please."


class StateCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "state"
        self.state.mkdir(mode=0o700)
        os.chmod(self.state, 0o700)
        patcher = mock.patch.dict(os.environ, {operator_settings.STATE_ENV: str(self.state)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.log = str(Path(self.temp.name) / "decisions.jsonl")
        self.settings(["peer", "execution"])

    def settings(self, routes, raw=None):
        path = self.state / operator_settings.FILE_NAME
        path.write_text(raw if raw is not None else
                        json.dumps({"version": 1, "client_derived_routes": routes}),
                        encoding="utf-8")
        os.chmod(path, 0o600)

    def exception(self, exception_id, body, **overrides):
        folder = self.state / sb.EXCEPTIONS_DIR
        folder.mkdir(mode=0o700, exist_ok=True)
        document = {"exception_id": exception_id, "approval_ref": "synthetic-approval",
                    "body_sha256": sb._digest(body)[1], "route": "peer", "provider": "codex",
                    "classes": ["email"], "expires_at": 4_000_000_000}
        document.update(overrides)
        path = folder / (exception_id + ".json")
        path.write_text(json.dumps(document), encoding="utf-8")
        os.chmod(path, 0o600)

    def bind(self, body=CLEAN, **kw):
        kw.setdefault("route", "peer")
        kw.setdefault("provider", "codex")
        kw.setdefault("classification", "client_derived")
        return sb.bind(body, log_path=self.log, **kw)

    def release(self, binding, body=CLEAN, **kw):
        kw.setdefault("route", "peer")
        kw.setdefault("provider", "codex")
        kw.setdefault("classification", "client_derived")
        return sb.release(binding, body, log_path=self.log, **kw)

    def refused(self, reason, fn, *args, **kw):
        with self.assertRaises(sb.SendRefused) as caught:
            fn(*args, **kw)
        self.assertEqual(caught.exception.reason, reason)
        return caught.exception

    def entries(self):
        with open(self.log, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle]


class DetectionTests(unittest.TestCase):
    def test_each_class(self):
        cases = {
            "ssn": "id 123-45-6789 here",
            "ein": "ein 12-3456789 here",
            "email": "mail a.b@example.invalid now",
            "phone": "call (555) 010-0199 today",
            "account": "acct 4111 1111 1111 1111 on file",
        }
        for name, text in cases.items():
            with self.subTest(name=name):
                self.assertIn(name, sb.identifier_classes(text))

    def test_clean_text_has_none(self):
        self.assertEqual(sb.identifier_classes(CLEAN), ())
        self.assertEqual(sb.identifier_classes("Revenue was 1,234 in 2026."), ())

    def test_bare_nine_digits_fail_closed_as_ssn(self):
        self.assertIn("ssn", sb.identifier_classes("ref 123456789"))

    def test_separator_variants(self):
        cases = {
            "ssn": ("id 123 45 6789 here", "id 123.45.6789 here"),
            "ein": ("ein 12 3456789 here", "ein 12.3456789 here"),
            "phone": ("call 555.010.0199 today", "call +44 20 7946 0958 today",
                      "call +1 555 010 0199 today"),
            "account": ("acct 12345678 on file", "acct 1234-5678 on file"),
        }
        for name, texts in cases.items():
            for text in texts:
                with self.subTest(name=name, text=text):
                    self.assertIn(name, sb.identifier_classes(text))

    def test_date_shaped_numbers_fail_closed_as_account(self):
        # F9: stripping dates let "Account number: 2026-10-27" auto-send.
        for text in ("Account number: 2026-10-27", "Closed on 2026-10-27."):
            with self.subTest(text=text):
                self.assertIn("account", sb.identifier_classes(text))


class BindTests(StateCase):
    def test_clean_payload_auto_binds_and_releases(self):
        binding = self.bind()
        self.assertEqual(binding.body_sha256, sb._digest(CLEAN)[1])
        self.assertEqual(binding.body_length, len(CLEAN.encode()))
        self.assertEqual(binding.identifier_classes, ())
        self.release(binding)
        events = [e["event"] for e in self.entries()]
        self.assertEqual(events, ["bound", "released"])

    def test_preview_hash_equals_sent_hash(self):
        binding = self.bind()
        bound = self.entries()[0]
        self.release(binding)
        released = self.entries()[1]
        self.assertEqual(bound["body_sha256"], released["body_sha256"])

    def test_other_classifications_pass_unchanged(self):
        for label in ("public", "synthetic", "internal"):
            with self.subTest(label=label):
                self.assertIsNone(self.bind(SYNTHETIC_EMAIL, classification=label))
                self.release(None, SYNTHETIC_EMAIL, classification=label)

    def test_identifier_blocks_without_exception(self):
        exc = self.refused(sb.REFUSED_IDENTIFIERS, self.bind, SYNTHETIC_EMAIL)
        self.assertEqual(exc.classes, ("email",))

    def test_route_not_enabled(self):
        self.settings(["execution"])
        self.refused(sb.REFUSED_ROUTE_DISABLED, self.bind)

    def test_corrupt_settings_fail_closed(self):
        self.settings(None, raw="{not json")
        self.refused(sb.REFUSED_SETTINGS, self.bind)

    def test_absent_settings_use_legacy_intersection(self):
        (self.state / operator_settings.FILE_NAME).unlink()
        self.refused(sb.REFUSED_ROUTE_DISABLED, self.bind)
        binding = self.bind(legacy=[{"peer"}])
        self.release(binding, legacy=[{"peer"}])

    def test_unknown_destination(self):
        self.refused(sb.REFUSED_DESTINATION, self.bind, provider="grok")
        self.refused(sb.REFUSED_DESTINATION, self.bind, route="email")

    def test_parity_claude_and_codex(self):
        for provider in ("claude", "codex"):
            for route in ("peer", "execution"):
                with self.subTest(provider=provider, route=route):
                    binding = self.bind(provider=provider, route=route)
                    self.release(binding, provider=provider, route=route)
                    self.refused(sb.REFUSED_IDENTIFIERS, self.bind, SYNTHETIC_EMAIL,
                                 provider=provider, route=route)


class ExceptionTests(StateCase):
    def test_exact_exception_allows_once(self):
        self.exception("exc-one", SYNTHETIC_EMAIL)
        binding = self.bind(SYNTHETIC_EMAIL, exception_id="exc-one")
        self.assertEqual(binding.approval_ref, "synthetic-approval")
        self.release(binding, SYNTHETIC_EMAIL)
        self.refused(sb.REFUSED_EXCEPTION, self.bind, SYNTHETIC_EMAIL, exception_id="exc-one")

    def test_exception_for_other_bytes_refuses(self):
        self.exception("exc-two", SYNTHETIC_EMAIL + " ")
        self.refused(sb.REFUSED_EXCEPTION, self.bind, SYNTHETIC_EMAIL, exception_id="exc-two")

    def test_exception_mismatches_refuse(self):
        cases = {
            "exc-route": {"route": "execution"},
            "exc-prov": {"provider": "claude"},
            "exc-class": {"classes": ["ssn"]},
            "exc-expired": {"expires_at": 1},
            "exc-noref": {"approval_ref": ""},
        }
        for exception_id, override in cases.items():
            with self.subTest(exception_id=exception_id):
                self.exception(exception_id, SYNTHETIC_EMAIL, **override)
                self.refused(sb.REFUSED_EXCEPTION, self.bind, SYNTHETIC_EMAIL,
                             exception_id=exception_id)

    def test_untrusted_exception_file_refuses(self):
        self.exception("exc-open", SYNTHETIC_EMAIL)
        os.chmod(self.state / sb.EXCEPTIONS_DIR / "exc-open.json", 0o666)
        self.refused(sb.REFUSED_EXCEPTION, self.bind, SYNTHETIC_EMAIL, exception_id="exc-open")

    def test_exception_spend_survives_a_new_process(self):
        self.exception("exc-durable", SYNTHETIC_EMAIL)
        self.bind(SYNTHETIC_EMAIL, exception_id="exc-durable")
        with mock.patch.object(sb, "_SPENT", set()):
            self.refused(sb.REFUSED_EXCEPTION, self.bind, SYNTHETIC_EMAIL,
                         exception_id="exc-durable")

    def test_untrusted_spent_folder_refuses(self):
        self.exception("exc-spent", SYNTHETIC_EMAIL)
        spent = self.state / sb.EXCEPTIONS_DIR / "spent"
        spent.mkdir(mode=0o700)
        os.chmod(spent, 0o777)
        self.refused(sb.REFUSED_EXCEPTION, self.bind, SYNTHETIC_EMAIL, exception_id="exc-spent")

    def test_non_finite_exception_expiry_refuses(self):
        for exception_id, value in (("exc-inf", float("inf")), ("exc-nan", float("nan"))):
            with self.subTest(exception_id=exception_id):
                self.exception(exception_id, SYNTHETIC_EMAIL, expires_at=value)
                self.refused(sb.REFUSED_EXCEPTION, self.bind, SYNTHETIC_EMAIL,
                             exception_id=exception_id)

    def test_binding_never_outlives_its_exception(self):
        self.exception("exc-short", SYNTHETIC_EMAIL, expires_at=1005)
        binding = self.bind(SYNTHETIC_EMAIL, exception_id="exc-short", now=1000.0,
                            ttl_seconds=300)
        self.assertEqual(binding.expires_at, 1005)
        self.refused(sb.REFUSED_EXPIRED, self.release, binding, SYNTHETIC_EMAIL, now=1006.0)

    def test_bad_exception_id_refuses(self):
        self.refused(sb.REFUSED_EXCEPTION, self.bind, SYNTHETIC_EMAIL, exception_id="../x")


class ReleaseTests(StateCase):
    def test_one_byte_change_refuses(self):
        binding = self.bind()
        self.refused(sb.REFUSED_BODY, self.release, binding, CLEAN[:-1] + "!")

    def test_destination_change_refuses(self):
        binding = self.bind()
        self.refused(sb.REFUSED_DESTINATION, self.release, binding, provider="claude")
        self.refused(sb.REFUSED_DESTINATION, self.release, binding, route="execution")

    def test_expired_refuses(self):
        binding = self.bind(now=1000.0, ttl_seconds=10)
        self.refused(sb.REFUSED_EXPIRED, self.release, binding, now=1010.0)

    def test_replay_refuses(self):
        binding = self.bind()
        self.release(binding)
        self.refused(sb.REFUSED_REPLAY, self.release, binding)

    def test_tampered_capability_refuses(self):
        binding = self.bind()
        forged = sb.Binding(**{**binding.record(),
                               "identifier_classes": binding.identifier_classes},
                            capability="0" * 64)
        self.refused(sb.REFUSED_CAPABILITY, self.release, forged)

    def test_tampered_record_refuses(self):
        binding = self.bind()
        other = sb._digest("other bytes")[1]
        forged = sb.Binding(**{**binding.record(), "body_sha256": other,
                               "identifier_classes": binding.identifier_classes},
                            capability=binding.capability)
        self.refused(sb.REFUSED_CAPABILITY, self.release, forged, "other bytes")

    def test_missing_binding_refuses(self):
        self.refused(sb.REFUSED_CAPABILITY, self.release, None)

    def test_binding_under_another_label_refuses(self):
        binding = self.bind()
        self.refused(sb.REFUSED_CAPABILITY, self.release, binding, classification="public")
        self.release(binding)

    def test_bad_ttl_refuses(self):
        for ttl in (0, -1, float("inf"), float("nan"), True):
            with self.subTest(ttl=ttl):
                self.refused(sb.REFUSED_EXPIRED, self.bind, ttl_seconds=ttl)

    def test_policy_change_refuses(self):
        binding = self.bind()
        self.settings(["peer"])
        self.refused(sb.REFUSED_POLICY, self.release, binding)

    def test_route_revoked_refuses(self):
        binding = self.bind()
        self.settings([])
        self.refused(sb.REFUSED_POLICY, self.release, binding)

    def test_settings_corrupted_after_bind_refuses(self):
        binding = self.bind()
        self.settings(None, raw="{not json")
        self.refused(sb.REFUSED_POLICY, self.release, binding)

    def test_retry_needs_a_fresh_binding(self):
        first = self.bind()
        self.release(first)
        second = self.bind()
        self.assertNotEqual(first.record_id, second.record_id)
        self.release(second)


class LogTests(StateCase):
    def test_logs_hold_classes_never_values(self):
        self.exception("exc-log", SYNTHETIC_EMAIL)
        with self.assertRaises(sb.SendRefused):
            self.bind(SYNTHETIC_EMAIL)
        binding = self.bind(SYNTHETIC_EMAIL, exception_id="exc-log")
        self.release(binding, SYNTHETIC_EMAIL)
        text = Path(self.log).read_text(encoding="utf-8")
        self.assertNotIn("test.person", text)
        self.assertNotIn("example.invalid", text)
        self.assertNotIn("Reach the synthetic", text)
        self.assertNotIn(binding.capability, text)
        self.assertIn('"email"', text)

    def test_log_is_owner_only(self):
        self.bind()
        self.assertEqual(os.stat(self.log).st_mode & 0o777, 0o600)

    def test_approval_ref_is_hashed_in_the_log(self):
        self.exception("exc-ref", SYNTHETIC_EMAIL)
        self.bind(SYNTHETIC_EMAIL, exception_id="exc-ref")
        text = Path(self.log).read_text(encoding="utf-8")
        self.assertNotIn("synthetic-approval", text)
        self.assertEqual(self.entries()[0]["approval_ref_sha256"],
                         sb.hashlib.sha256(b"synthetic-approval").hexdigest())

    def test_symlinked_log_refuses(self):
        target = Path(self.temp.name) / "elsewhere.jsonl"
        target.touch(mode=0o600)
        os.symlink(target, self.log)
        self.refused(sb.REFUSED_LOG, self.bind)
        self.assertEqual(target.read_text(encoding="utf-8"), "")

    def test_short_write_completes_the_record(self):
        real = os.write
        with mock.patch.object(sb.os, "write", side_effect=lambda fd, b: real(fd, bytes(b)[:5])):
            self.bind()
        self.assertEqual(self.entries()[0]["event"], "bound")

    def test_stalled_or_failed_write_refuses(self):
        for effect in (lambda fd, b: 0, OSError("disk full")):
            with self.subTest(effect=effect), \
                    mock.patch.object(sb.os, "write", side_effect=effect):
                self.refused(sb.REFUSED_LOG, self.bind)

    def test_shared_log_refuses(self):
        Path(self.log).touch()
        os.chmod(self.log, 0o644)
        self.refused(sb.REFUSED_LOG, self.bind)

    def test_log_in_shared_folder_refuses(self):
        shared = Path(self.temp.name) / "shared"
        shared.mkdir()
        os.chmod(shared, 0o777)
        self.refused(sb.REFUSED_LOG, sb.bind, CLEAN, route="peer", provider="codex",
                     classification="client_derived", log_path=str(shared / "d.jsonl"))


if __name__ == "__main__":
    unittest.main()
