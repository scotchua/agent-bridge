"""Per-call research mode: web access is off by default and opt-in for one turn.

Synthetic fixtures and the stand-in peers only. No network, no real provider.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import redaction_handoff  # noqa: E402
from harness import Sandbox  # noqa: E402
from agent_bridge import broker, drift, mcp_server, registry, send_binding, store, worker  # noqa: E402
from agent_bridge.backends import claude_backend, codex_backend  # noqa: E402
from agent_bridge.backends.base import PeerOutcome  # noqa: E402
from agent_bridge.errors import BrokerError, ErrorCategory  # noqa: E402

WEB_TOOLS = "WebSearch,WebFetch"
CLIENT_SPELLINGS = ("client-derived", "client_derived", " Client_Derived ")


def _tools_pair(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def _read_argv_log(path: str) -> list[list[str]]:
    with open(path, encoding="utf-8") as handle:
        calls = [json.loads(line) for line in handle if line.strip()]
    return [call for call in calls if "--version" not in call]


class ArgvTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.cleanup)
        self.schema, _ = self.sb.cfg.load_schema()

    def codex(self, **kwargs) -> list[str]:
        return codex_backend.build_argv(
            self.sb.cfg, schema_file="/s.json", last_message_file="/m.txt",
            workspace="/ws", **kwargs)

    def test_claude_default_keeps_tools_empty_and_no_allowlist(self) -> None:
        for resume in (False, True):
            argv = claude_backend.build_argv(self.sb.cfg, self.schema, "SID", resume)
            self.assertEqual(_tools_pair(argv, "--tools"), "")
            self.assertNotIn("--allowedTools", argv)
            self.assertEqual(argv, claude_backend.build_argv(
                self.sb.cfg, self.schema, "SID", resume, research=False))

    def test_claude_research_enables_only_the_web_tools(self) -> None:
        for resume in (False, True):
            argv = claude_backend.build_argv(self.sb.cfg, self.schema, "SID", resume, research=True)
            self.assertEqual(_tools_pair(argv, "--tools"), WEB_TOOLS)
            self.assertEqual(_tools_pair(argv, "--allowedTools"), WEB_TOOLS)

    def test_claude_research_changes_nothing_else(self) -> None:
        for resume in (False, True):
            off = claude_backend.build_argv(self.sb.cfg, self.schema, "SID", resume)
            on = claude_backend.build_argv(self.sb.cfg, self.schema, "SID", resume, research=True)
            self.assertEqual(on[:len(off) - 2], off[:-2])
            self.assertEqual(off[-2:], ["--tools", ""])
            self.assertEqual(on[len(off) - 2:], ["--tools", WEB_TOOLS, "--allowedTools", WEB_TOOLS])
            for flag in ("--safe-mode", "--strict-mcp-config", "--mcp-config", "--setting-sources"):
                self.assertIn(flag, on)
            self.assertEqual(_tools_pair(on, "--mcp-config"), '{"mcpServers":{}}')
            self.assertEqual(_tools_pair(on, "--setting-sources"), "")

    def test_claude_spend_ceiling_survives_research(self) -> None:
        self.sb.cfg.raw["peers"]["claude"]["max_budget_usd"] = 1.5
        argv = claude_backend.build_argv(self.sb.cfg, self.schema, "SID", False, research=True)
        self.assertEqual(_tools_pair(argv, "--max-budget-usd"), "1.5")

    def test_codex_default_pins_web_search_off_on_start_and_resume(self) -> None:
        for thread in (None, "thread-aster"):
            argv = self.codex(thread_id=thread)
            self.assertIn('web_search="disabled"', argv)
            self.assertEqual(argv[argv.index('web_search="disabled"') - 1], "-c")
            self.assertEqual(argv, self.codex(thread_id=thread, research=False))

    def test_codex_research_pins_web_search_live_on_start_and_resume(self) -> None:
        for thread in (None, "thread-aster"):
            argv = self.codex(thread_id=thread, research=True)
            self.assertIn('web_search="live"', argv)
            self.assertNotIn('web_search="disabled"', argv)
            self.assertEqual(argv[argv.index('web_search="live"') - 1], "-c")

    def test_codex_research_changes_nothing_else(self) -> None:
        for thread in (None, "thread-aster"):
            off = self.codex(thread_id=thread)
            on = self.codex(thread_id=thread, research=True)
            self.assertEqual([a for a in off if a != 'web_search="disabled"'],
                             [a for a in on if a != 'web_search="live"'])

    def test_codex_start_only_flags_stay_start_only(self) -> None:
        start = self.codex(thread_id=None, research=True)
        resume = self.codex(thread_id="thread-aster", research=True)
        for option in ("-s", "-C"):
            self.assertIn(option, start)
            self.assertNotIn(option, resume)

    def test_research_is_keyword_only_for_codex(self) -> None:
        with self.assertRaises(TypeError):
            codex_backend.build_argv(self.sb.cfg, "/s.json", "/m.txt", None, "/ws", True)

    def test_drift_flag_list_covers_the_new_claude_flag(self) -> None:
        self.assertIn("--allowedTools", drift.CLAUDE_FLAGS)


class BrokerValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.cleanup)

    def validate(self, peer="claude", operation="start", **fields) -> None:
        args = {"prompt": "invented question", "source_classification": "internal", **fields}
        allowed = broker.CONTINUE_FIELDS if operation == "continue" else broker.START_FIELDS
        if operation == "continue":
            args.setdefault("conversation_id", "00000000-0000-4000-8000-000000000000")
        broker._validate_common(self.sb.cfg, args, allowed, peer, operation)

    def test_research_is_an_accepted_field_for_start_and_continue(self) -> None:
        self.assertIn("research", broker.START_FIELDS)
        self.assertIn("research", broker.CONTINUE_FIELDS)
        for peer in ("claude", "codex"):
            for operation in ("start", "continue"):
                for value in (True, False):
                    self.validate(peer, operation, research=value)

    def test_non_bool_research_is_refused(self) -> None:
        for value in (1, 0, "true", "false", "", None, [], {}, 1.0):
            for peer in ("claude", "codex"):
                with self.subTest(value=value, peer=peer):
                    with self.assertRaises(BrokerError) as caught:
                        self.validate(peer, research=value)
                    self.assertIs(caught.exception.category, ErrorCategory.INPUT_SCHEMA_INVALID)

    def test_research_with_client_derived_is_refused_for_both_spellings(self) -> None:
        # Opt the peer route in so the classification itself is admissible;
        # the research refusal must still hold.
        self.sb.cfg.raw["client_derived_routes"] = ["peer"]
        for label in CLIENT_SPELLINGS:
            for peer in ("claude", "codex"):
                for operation in ("start", "continue"):
                    with self.subTest(label=label, peer=peer, operation=operation):
                        self.validate(peer, operation, source_classification=label)
                        with self.assertRaises(BrokerError) as caught:
                            self.validate(peer, operation, source_classification=label,
                                          research=True)
                        self.assertIs(caught.exception.category,
                                      ErrorCategory.SOURCE_CLASSIFICATION_REFUSED)
                        # Explicitly off stays admissible.
                        self.validate(peer, operation, source_classification=label,
                                      research=False)

    def test_research_with_client_derived_is_refused_without_the_route_too(self) -> None:
        for label in CLIENT_SPELLINGS:
            with self.assertRaises(BrokerError) as caught:
                self.validate(source_classification=label, research=True)
            self.assertIs(caught.exception.category, ErrorCategory.SOURCE_CLASSIFICATION_REFUSED)

    def test_research_is_allowed_for_other_classifications(self) -> None:
        for label in ("internal", "synthetic", "public"):
            self.validate(source_classification=label, research=True)

    def test_prepare_job_persists_research_flag(self) -> None:
        for given, expected in ((None, False), (False, False), (True, True)):
            args = {"prompt": "invented prompt", "source_classification": "internal"}
            if given is not None:
                args["research"] = given
            job_id = f"00000000-0000-4000-8000-00000000000{int(expected)}"
            conversation_id = "11111111-1111-4111-8111-111111111111"
            broker._prepare_job(self.sb.cfg, "codex", "claude", conversation_id,
                                job_id, args, resume=False, budget_started=1000.0)
            request = store.read_json_atomic(
                os.path.join(self.sb.cfg.job_dir(job_id), "request.json"))
            self.assertIs(request["research"], expected)


class SchemaTests(unittest.TestCase):
    def test_research_is_optional_boolean_defaulting_off(self) -> None:
        for peer in ("claude", "codex"):
            for build in (mcp_server._start_schema, mcp_server._continue_schema):
                schema = build(peer)
                prop = schema["properties"]["research"]
                self.assertEqual(prop["type"], "boolean")
                self.assertIs(prop["default"], False)
                self.assertNotIn("research", schema["required"])
                self.assertIs(schema["additionalProperties"], False)
                for phrase in ("public web", "this turn only", "Off by default",
                               "client-derived"):
                    self.assertIn(phrase, prop["description"])

    def test_isolation_note_states_research_rule_and_keeps_existing_phrases(self) -> None:
        for peer in ("claude", "codex"):
            note = mcp_server._isolation_note(peer)
            self.assertIn("off unless you set research", note)
            self.assertIn("public web pages", note)
        self.assertIn("hard spend ceiling", mcp_server._isolation_note("claude"))
        self.assertIn("restricts writes, not reads", mcp_server._isolation_note("codex"))


class EndToEndTests(unittest.TestCase):
    """Real broker and worker, stand-in peers: the argv the peer actually got."""

    def run_turns(self, caller: str, **start_extra):
        peer = broker.PEER_OF[caller]
        sb = Sandbox()
        self.addCleanup(sb.cleanup)
        log = sb.marker(f"argv-{peer}.log")
        sb.env(**{f"FAKE_{peer.upper()}_ARGV_LOG": log})
        started, status, result = sb.run_to_completion(caller, **start_extra)
        self.assertEqual(status["status"], "complete", json.dumps(status))
        second = broker.continue_(sb.cfg, caller, {
            "conversation_id": started["conversation_id"],
            "prompt": "Second turn.", "source_classification": "internal",
            **start_extra})
        self.assertEqual(sb.wait(second["job_id"])["status"], "complete")
        return sb, started, second, result, _read_argv_log(log)

    def assert_research(self, peer: str, argv: list[str], on: bool) -> None:
        if peer == "claude":
            self.assertEqual(_tools_pair(argv, "--tools"), WEB_TOOLS if on else "")
            self.assertEqual("--allowedTools" in argv, on)
        else:
            self.assertIn('web_search="live"' if on else 'web_search="disabled"', argv)

    def test_default_calls_pin_web_off_for_both_peers(self) -> None:
        for caller in ("codex", "claude"):
            peer = broker.PEER_OF[caller]
            with self.subTest(peer=peer):
                sb, started, second, result, calls = self.run_turns(caller)
                self.assertEqual(len(calls), 2)
                for argv in calls:
                    self.assert_research(peer, argv, False)
                prov = sb.provenance(started["job_id"])
                self.assertIs(prov["research"], False)
                self.assertIs(result["provenance"]["research"], False)

    def test_research_true_reaches_the_peer_and_every_record(self) -> None:
        for caller in ("codex", "claude"):
            peer = broker.PEER_OF[caller]
            with self.subTest(peer=peer):
                sb, started, second, result, calls = self.run_turns(caller, research=True)
                self.assertEqual(len(calls), 2)
                for argv in calls:
                    self.assert_research(peer, argv, True)
                for job_id in (started["job_id"], second["job_id"]):
                    request = store.read_json_atomic(
                        os.path.join(sb.cfg.job_dir(job_id), "request.json"))
                    prov = sb.provenance(job_id)
                    self.assertIs(request["research"], True)
                    self.assertIs(prov["research"], True)
                    self.assertTrue(all(a["notes"]["research"] is True
                                        for a in prov["attempts"]))
                    ledger = [r for r in sb.ledger() if r.get("job_id") == job_id]
                    self.assertEqual([r["research"] for r in ledger], [True])
                read = broker.read(sb.cfg, caller, {"job_id": started["job_id"]})
                self.assertIs(read["provenance"]["research"], True)

    def test_research_is_per_turn_not_sticky(self) -> None:
        caller, peer = "codex", "claude"
        sb = Sandbox()
        self.addCleanup(sb.cleanup)
        log = sb.marker("argv-claude.log")
        sb.env(FAKE_CLAUDE_ARGV_LOG=log)
        started, status, _ = sb.run_to_completion(caller, research=True)
        second = broker.continue_(sb.cfg, caller, {
            "conversation_id": started["conversation_id"],
            "prompt": "Second turn.", "source_classification": "internal"})
        sb.wait(second["job_id"])
        first_call, second_call = _read_argv_log(log)
        self.assert_research(peer, first_call, True)
        self.assert_research(peer, second_call, False)

    def test_broker_refuses_research_for_client_derived_before_any_job(self) -> None:
        sb = Sandbox()
        self.addCleanup(sb.cleanup)
        sb.cfg.raw["client_derived_routes"] = ["peer"]
        for label in CLIENT_SPELLINGS:
            with self.assertRaises(BrokerError) as caught:
                sb.consult("codex", classification=label, research=True)
            self.assertIs(caught.exception.category, ErrorCategory.SOURCE_CLASSIFICATION_REFUSED)

    def test_broker_refuses_non_bool_research(self) -> None:
        sb = Sandbox()
        self.addCleanup(sb.cleanup)
        for value in (1, "true"):
            with self.assertRaises(BrokerError) as caught:
                sb.consult("codex", research=value)
            self.assertIs(caught.exception.category, ErrorCategory.INPUT_SCHEMA_INVALID)


class WorkerTests(unittest.TestCase):
    def queued_job(self, sb: Sandbox, **start_extra) -> str:
        with patch.object(broker, "_dispatch", side_effect=lambda cfg, peer, cid, job: {
                "job_id": job, "conversation_id": cid}):
            return sb.consult("codex", **start_extra)["job_id"]

    def test_worker_refuses_client_derived_research_before_spawning_the_peer(self) -> None:
        for label in ("client_derived", "client-derived"):
            with self.subTest(label=label):
                sb = Sandbox()
                self.addCleanup(sb.cleanup)
                job_id = self.queued_job(sb)
                job_dir = sb.cfg.job_dir(job_id)
                request_path = os.path.join(job_dir, "request.json")
                request = store.read_json_atomic(request_path)
                # A request that bypassed the broker: classification and
                # research forged into the stored file.
                request.update(research=True, source_classification=label)
                store.atomic_write_json(request_path, request)
                with patch.object(worker, "_run_peer") as run_peer:
                    code = worker.execute(job_dir)
                run_peer.assert_not_called()
                self.assertEqual(code, 1)
                prov = sb.provenance(job_id)
                self.assertEqual(prov["error_category"],
                                 ErrorCategory.SOURCE_CLASSIFICATION_REFUSED.value)
                self.assertIs(prov["research"], True)
                self.assertEqual(prov["attempt_count"], 0)

    def test_worker_treats_a_missing_or_non_bool_research_as_off(self) -> None:
        for forged, expected in ((None, False), ("yes", False), (1, False), (True, True)):
            with self.subTest(forged=forged):
                sb = Sandbox()
                self.addCleanup(sb.cleanup)
                job_id = self.queued_job(sb)
                job_dir = sb.cfg.job_dir(job_id)
                request_path = os.path.join(job_dir, "request.json")
                request = store.read_json_atomic(request_path)
                request.pop("research", None)
                if forged is not None:
                    request["research"] = forged
                store.atomic_write_json(request_path, request)
                seen = []

                def fake_run_peer(*args, research=False, **kwargs):
                    seen.append(research)
                    raise RuntimeError("stop after observing the flag")

                with patch.object(worker, "_run_peer", side_effect=fake_run_peer):
                    worker.execute(job_dir)
                self.assertEqual(seen, [expected])
                self.assertIs(sb.provenance(job_id)["research"], expected)

    def test_run_peer_forwards_research_to_each_backend(self) -> None:
        from agent_bridge.backends.base import PeerOutcome
        outcome = PeerOutcome(ErrorCategory.OK)
        common = dict(prompt="invented", schema={}, schema_file="unused",
                      peer_session_id="s", resume=True, workspace="unused",
                      attempt_dir="unused", deadline=10)
        for peer, backend in (("claude", worker.claude_backend), ("codex", worker.codex_backend)):
            for research in (False, True):
                with self.subTest(peer=peer, research=research):
                    with patch.object(backend, "run_consultation", return_value=outcome) as call:
                        worker._run_peer(None, peer, research=research, **common)
                    self.assertIs(call.call_args.kwargs["research"], research)

    def test_client_derived_set_is_the_one_send_binding_defines(self) -> None:
        self.assertEqual(send_binding.CLIENT_DERIVED, frozenset({"client_derived", "client-derived"}))


class ConversationExposureTests(unittest.TestCase):
    """A conversation that ever carried client data never turns research on."""

    def setUp(self) -> None:
        self.sb = Sandbox()
        self.addCleanup(self.sb.cleanup)

    def finished_internal(self) -> str:
        started, status, _ = self.sb.run_to_completion("codex")
        self.assertEqual(status["status"], "complete", json.dumps(status))
        return started["conversation_id"]

    def continue_(self, cid: str, **extra) -> dict:
        args = {"conversation_id": cid, "prompt": "Next turn.",
                "source_classification": "internal", **extra}
        return broker.continue_(self.sb.cfg, "codex", args)

    def test_start_records_whether_the_first_turn_was_client_derived(self) -> None:
        self.sb.cfg.raw["client_derived_routes"] = ["peer"]
        with patch.object(broker, "_dispatch", side_effect=lambda cfg, peer, cid, job: {
                "job_id": job, "conversation_id": cid}):
            for label, expected in (("internal", False), ("client_derived", True),
                                    ("client-derived", True)):
                with self.subTest(label=label):
                    cid = self.sb.consult("codex", classification=label)["conversation_id"]
                    record = registry.load_conversation(self.sb.cfg, cid)
                    self.assertIs(record["client_derived_seen"], expected)

    def test_broker_refuses_research_after_any_client_derived_turn(self) -> None:
        cid = self.finished_internal()
        registry.update_conversation(self.sb.cfg, cid, client_derived_seen=True)
        with patch.object(registry, "claim_conversation_slot") as claim:
            with self.assertRaises(BrokerError) as caught:
                self.continue_(cid, research=True)
        claim.assert_not_called()
        self.assertIs(caught.exception.category, ErrorCategory.SOURCE_CLASSIFICATION_REFUSED)
        # Research off still works on the same conversation.
        self.assertEqual(self.sb.wait(self.continue_(cid)["job_id"])["status"], "complete")

    def test_legacy_record_without_the_field_refuses_research(self) -> None:
        cid = self.finished_internal()
        path = self.sb.cfg.conversation_path(cid)
        record = store.read_json_atomic(path)
        del record["client_derived_seen"]
        store.atomic_write_json(path, record)
        with self.assertRaises(BrokerError) as caught:
            self.continue_(cid, research=True)
        self.assertIs(caught.exception.category, ErrorCategory.SOURCE_CLASSIFICATION_REFUSED)
        self.assertEqual(self.sb.wait(self.continue_(cid)["job_id"])["status"], "complete")

    def test_claim_refuses_research_under_the_lock_and_leaves_the_slot_free(self) -> None:
        cid = self.finished_internal()
        registry.update_conversation(self.sb.cfg, cid, client_derived_seen=True)
        with self.assertRaises(BrokerError) as caught:
            registry.claim_conversation_slot(
                self.sb.cfg, cid, "codex", "job-forged", 10, research=True)
        self.assertIs(caught.exception.category, ErrorCategory.SOURCE_CLASSIFICATION_REFUSED)
        self.assertNotEqual(
            registry.load_conversation(self.sb.cfg, cid).get("active_job_id"), "job-forged")

    def test_a_client_derived_continuation_marks_the_conversation_for_good(self) -> None:
        cid = self.finished_internal()
        self.assertIs(registry.load_conversation(self.sb.cfg, cid)["client_derived_seen"], False)
        registry.claim_conversation_slot(
            self.sb.cfg, cid, "codex", "job-client", 10, client_derived=True)
        self.assertIs(registry.load_conversation(self.sb.cfg, cid)["client_derived_seen"], True)
        # Sticky: a later internal claim does not clear it.
        registry.release_conversation_slot(self.sb.cfg, cid, "job-client")
        registry.claim_conversation_slot(self.sb.cfg, cid, "codex", "job-internal", 10)
        self.assertIs(registry.load_conversation(self.sb.cfg, cid)["client_derived_seen"], True)

    def test_internal_only_conversation_keeps_per_turn_research(self) -> None:
        cid = self.finished_internal()
        self.assertEqual(
            self.sb.wait(self.continue_(cid, research=True)["job_id"])["status"], "complete")
        self.assertIs(registry.load_conversation(self.sb.cfg, cid)["client_derived_seen"], False)

    def test_worker_refuses_research_when_the_record_shows_exposure(self) -> None:
        for state in ("exposed", "legacy"):
            with self.subTest(state=state):
                sb = Sandbox()
                self.addCleanup(sb.cleanup)
                with patch.object(broker, "_dispatch", side_effect=lambda cfg, peer, cid, job: {
                        "job_id": job, "conversation_id": cid}):
                    started = sb.consult("codex")
                job_dir = sb.cfg.job_dir(started["job_id"])
                request_path = os.path.join(job_dir, "request.json")
                request = store.read_json_atomic(request_path)
                # A request that bypassed the broker's exposure check.
                request["research"] = True
                store.atomic_write_json(request_path, request)
                path = sb.cfg.conversation_path(started["conversation_id"])
                record = store.read_json_atomic(path)
                if state == "exposed":
                    record["client_derived_seen"] = True
                else:
                    del record["client_derived_seen"]
                store.atomic_write_json(path, record)
                with patch.object(worker, "_run_peer") as run_peer:
                    code = worker.execute(job_dir)
                run_peer.assert_not_called()
                self.assertEqual(code, 1)
                self.assertEqual(sb.provenance(started["job_id"])["error_category"],
                                 ErrorCategory.SOURCE_CLASSIFICATION_REFUSED.value)

    def test_worker_exposure_check_fails_closed(self) -> None:
        self.assertTrue(worker._conversation_refuses_research(self.sb.cfg, "no-such-conversation"))
        cid = self.finished_internal()
        self.assertFalse(worker._conversation_refuses_research(self.sb.cfg, cid))
        with open(self.sb.cfg.conversation_path(cid), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertTrue(worker._conversation_refuses_research(self.sb.cfg, cid))


class ClearedPromptRetryTests(unittest.TestCase):
    """A handoff job re-validates its bytes through _cleared_prompt on a retry."""

    def test_identical_retry_of_a_handoff_job_keeps_research(self) -> None:
        for research in (True, False):
            with self.subTest(research=research), redaction_handoff.local_job("claude") as (
                    sb, prep, sends, sender):
                prep.args["research"] = research
                job = redaction_handoff.admit(sb, prep, "claude")
                request = store.read_json_atomic(
                    os.path.join(sb.cfg.job_dir(job["job_id"]), "request.json"))
                self.assertIs(request["research"], research)
                valid = sender.side_effect

                def flaky(cfg, peer, **kwargs):
                    if not sends:
                        sends.append(kwargs)
                        return PeerOutcome(ErrorCategory.PEER_NONZERO_EXIT)
                    return valid(cfg, peer, **kwargs)

                sender.side_effect = flaky
                worker.execute(sb.cfg.job_dir(job["job_id"]))
                self.assertEqual(len(sends), 2)
                self.assertEqual([call["research"] for call in sends], [research, research])
                self.assertEqual(sends[0]["prompt"], sends[1]["prompt"])
                self.assertEqual(registry.read_status(sb.cfg, job["job_id"])["status"], "complete")


if __name__ == "__main__":
    unittest.main()
