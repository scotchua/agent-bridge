"""Synthetic local preparations and fake peer sends; no plugin imports or models."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import uuid
from pathlib import Path
from unittest import mock

from harness import Sandbox, check
from agent_bridge import broker, envelope, registry, store, worker
from agent_bridge.backends.base import PeerOutcome
from agent_bridge.errors import BrokerError, ErrorCategory
from agent_bridge.mcp_server import Server

PLANTED = "Zorvella Quenwick"
VALID = {"contract_version": "2", "status": "answer", "summary": "Synthetic answer",
         "analysis": [], "disagreements": [], "risks": [], "questions": [], "confidence": "low"}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


class Preparation:
    def __init__(self, sb, peer, classification="synthetic", conversation_id=None, session_id=None):
        self.root = Path(sb.root) / str(uuid.uuid4())
        self.root.mkdir()
        self.resume = conversation_id is not None
        self.cid = conversation_id or str(uuid.uuid4())
        self.session_id = session_id if self.resume else self.cid if peer == "claude" else None
        self.output = f"Source classification: {classification}\nAssess [PERSON_1]'s café design.\n"
        schema, schema_sha = sb.cfg.load_schema()
        prompt = (envelope.build_continuation(self.output, sb.cfg.contract_version) if self.resume
                  else envelope.build_initial(self.output, schema, sb.cfg.contract_version))
        options = {"chunk_chars": 1000, "job_timeout": 60, "think": False, "schema": True,
                   "second_pass": True, "repair_retries": 0, "map_scope": "run", "roster_sha256": None}
        h = digest(b"synthetic metadata")
        self.receipt = {
            "version": 2, "contract": "local-delegate/v2", "invocation_id": uuid.uuid4().hex,
            "task": "redact", "model_digest": h, "prompt_version": h, "config_version": h,
            "input_sha256": digest(PLANTED.encode()), "canonical_input_sha256": digest(PLANTED.encode()),
            "output_sha256": digest(self.output.encode()), "validator_version": h,
            "parent_task_id": None, "attempt_id": None,
            "chunk_counts": {"pending": 0, "running": 0, "complete": 2, "failed": 0, "total": 2},
            "chunks": [{"index": i, "pass": i, "status": "complete", "parser_status": "complete",
                        "duration_seconds": 0.01} for i in (1, 2)],
            "passes": [{"pass": i, "status": "complete", "chunk_counts": {
                "pending": 0, "running": 0, "complete": 1, "failed": 0, "total": 1}} for i in (1, 2)],
            "parser_repairs": 0, "triage_repairs": 0, "duration_seconds": 0.02,
            "result_status": "success", "output_hash_kind": "stdout", "parser_status": "complete",
            "effective_options": options,
            "request_options": [{"num_ctx": 4096, "think": False, "schema": True,
                                 "prompt_version": h} for i in (1, 2)],
            "input_count": 1, "output_count": 1, "human_review_required": True,
            "semantic_coverage": "unproven",
        }
        route = {"contract": "local-delegate-certification/v2", "task": "redact",
                 "effective_options": options.copy(), "digest": h, "prompt_sha256": h,
                 "validator_sha256": h, "endpoint": "http://certification.invalid",
                 "runtime_version": "synthetic-1"}
        self.certificate = {"contract": route["contract"], "binding": route,
                            "evidence_kind": "fake",
                            "result": {"eligible": True, "complete": True, "failed_metrics": []}}
        self.certificate["route_key"] = self.route_key()
        bindings = ("invocation_id", "input_sha256", "canonical_input_sha256", "effective_options",
                    "parent_task_id", "attempt_id")
        self.manifest = {"contract": "redaction-handoff/v1", "source_classification": classification,
                         "artifacts": {}, "receipt_bindings": {
                             key: json.loads(json.dumps(self.receipt[key])) for key in bindings}}
        self.clearance = {
            "contract": "redaction-peer-handoff/v1", "human_reviewed": True,
            "assembly": {"peer": peer, "contract_version": sb.cfg.contract_version,
                         "contract_schema_sha256": schema_sha, "mode": "continuation" if self.resume else "initial",
                         "conversation_id": self.cid, "peer_session_id": self.session_id},
        }
        (self.root / "output.txt").write_bytes(self.output.encode())
        (self.root / "candidates.txt").write_bytes(("Human must review PERSON: " + PLANTED).encode())
        (self.root / "prompt.txt").write_bytes(prompt.encode())
        self.args = {"prompt": self.output, "source_classification": classification,
                     "preparation_dir": str(self.root)}
        if self.resume:
            self.args["conversation_id"] = self.cid
        self.publish()

    def route_key(self):
        route = self.certificate["binding"]
        return digest(json.dumps(route, sort_keys=True, ensure_ascii=False, allow_nan=False).encode())

    def publish(self):
        (self.root / "receipt.json").write_bytes(encoded(self.receipt))
        (self.root / "certificate.json").write_bytes(encoded(self.certificate))
        self.manifest["artifacts"] = {name: digest((self.root / name).read_bytes()) for name in (
            "output.txt", "candidates.txt", "prompt.txt", "receipt.json", "certificate.json")}
        (self.root / "preparation.json").write_bytes(encoded(self.manifest))
        self.clearance.update({"preparation_sha256": digest((self.root / "preparation.json").read_bytes()),
                               "output_sha256": self.manifest["artifacts"]["output.txt"],
                               "candidate_review_sha256": self.manifest["artifacts"]["candidates.txt"],
                               "prompt_sha256": self.manifest["artifacts"]["prompt.txt"]})
        self.clear()

    def clear(self):
        raw = encoded(self.clearance)
        (self.root / "clearance.json").write_bytes(raw)
        self.args["clearance_sha256"] = digest(raw)


@contextlib.contextmanager
def local_job(peer="claude", classification="synthetic"):
    sb = Sandbox()
    # Schema mutations must stay inside the disposable fixture.
    schema_path = Path(sb.root) / "schema.json"
    schema_path.write_bytes(Path(sb.cfg.schema_path).read_bytes())
    sb.cfg.raw["schema_path"] = str(schema_path)
    prep = Preparation(sb, peer, classification)
    sends = []

    def send(cfg, actual_peer, **kwargs):
        sends.append(kwargs)
        return PeerOutcome(ErrorCategory.OK, payload=VALID.copy(),
                           peer_session_id=kwargs["peer_session_id"] or "synthetic-session")

    try:
        with mock.patch.object(broker, "_spawn_worker", return_value=os.getpid()), \
                mock.patch.object(worker.preflight, "check_peer", return_value={
                    "executable": "synthetic-peer", "observed_version": "synthetic-1"}), \
                mock.patch.object(worker, "_run_peer", side_effect=send) as sender:
            yield sb, prep, sends, sender
    finally:
        sb.cleanup()


def admit(sb, prep, peer="claude"):
    operation = broker.continue_ if prep.resume else broker.start
    return operation(sb.cfg, broker.PEER_OF[peer], prep.args)


def test_delivery():
    for peer in ("claude", "codex"):
        with local_job(peer) as (sb, prep, sends, sender):
            first = admit(sb, prep, peer)
            worker.execute(sb.cfg.job_dir(first["job_id"]))
            check(f"RH: {peer} initial sends exactly the cleared UTF-8 bytes once",
                  len(sends) == 1 and sends[0]["prompt"] == (prep.root / "prompt.txt").read_bytes()
                  and registry.read_status(sb.cfg, first["job_id"])["status"] == "complete")
            session = registry.load_conversation(sb.cfg, prep.cid)["peer_session_id"]
            followup = Preparation(sb, peer, conversation_id=prep.cid, session_id=session)
            second = admit(sb, followup, peer)
            worker.execute(sb.cfg.job_dir(second["job_id"]))
            check(f"RH: {peer} continuation uses its own clearance and exact session",
                  len(sends) == 2 and sends[1]["prompt"] == (followup.root / "prompt.txt").read_bytes()
                  and sends[1]["resume"] and sends[1]["peer_session_id"] == session
                  and prep.args["clearance_sha256"] != followup.args["clearance_sha256"])
            # Also exercise a broker failure receipt, which retains only the redacted question.
            third_prep = Preparation(sb, peer, conversation_id=prep.cid, session_id=session)
            third = admit(sb, third_prep, peer)
            sender.side_effect = lambda *a, **kw: PeerOutcome(ErrorCategory.PEER_TIMEOUT)
            worker.execute(sb.cfg.job_dir(third["job_id"]))
            files = [p for p in Path(sb.state).rglob("*") if p.is_file()]
            check(f"RH: {peer} planted name never enters request, receipts or telemetry",
                  all(PLANTED.encode() not in p.read_bytes() for p in files)
                  and (Path(sb.cfg.job_dir(third["job_id"])) / "receipt.json").is_file())
            request = store.read_json_atomic(os.path.join(sb.cfg.job_dir(first["job_id"]), "request.json"))
            check(f"RH: {peer} request retains only payload, reference and hash bindings",
                  request["prompt"] == prep.output
                  and set(request["redaction_handoff"]) == {
                      "contract", "preparation_sha256", "clearance_sha256", "artifacts"}
                  and "receipt_bindings" not in request and "candidates" not in request)


def test_mcp_admission():
    for peer in ("claude", "codex"):
        with local_job(peer) as (sb, prep, sends, sender):
            server = Server(broker.PEER_OF[peer], sb.cfg)
            for missing in ("preparation_dir", "clearance_sha256"):
                args = {key: value for key, value in prep.args.items() if key != missing}
                result = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                        "params": {"name": f"{peer}_start", "arguments": args}})["result"]
                check(f"RH: MCP {peer} refuses missing {missing} before any send",
                      result["isError"] and result["structuredContent"]["error_category"]
                      == ErrorCategory.INPUT_SCHEMA_INVALID.value and not sends
                      and not list(Path(sb.state).rglob("request.json")))
            result = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                    "params": {"name": f"{peer}_start", "arguments": prep.args}})["result"]
            first = result["structuredContent"]
            check(f"RH: MCP {peer} admits a complete synthetic handoff",
                  not result["isError"] and first["ok"])
            worker.execute(sb.cfg.job_dir(first["job_id"]))
            check(f"RH: MCP {peer} sends exactly the cleared UTF-8 bytes once",
                  len(sends) == 1 and sends[0]["prompt"] == (prep.root / "prompt.txt").read_bytes()
                  and registry.read_status(sb.cfg, first["job_id"])["status"] == "complete")


def test_admission_refusals():
    cases = [("missing " + name, lambda p, name=name: (p.root / name).unlink()) for name in (
        "clearance.json", "receipt.json", "certificate.json", "preparation.json", "candidates.txt")]
    cases += [
        ("missing clearance pin", lambda p: p.args.pop("clearance_sha256")),
        ("missing directory", lambda p: p.args.pop("preparation_dir")),
        ("relative directory", lambda p: p.args.update(preparation_dir="relative")),
        ("classification substitution", lambda p: p.args.update(source_classification="internal")),
        ("payload substitution", lambda p: p.args.update(prompt=PLANTED)),
        ("unreviewed label", lambda p: p.args.update(label=PLANTED)),
        ("codex_task clearance", lambda p: p.clearance.update(contract="redaction-handoff/v1")),
        ("no human review", lambda p: p.clearance.update(human_reviewed=False)),
        ("changed cleared peer", lambda p: p.clearance["assembly"].update(peer="codex")),
        ("changed cleared mode", lambda p: p.clearance["assembly"].update(mode="continuation")),
        ("changed cleared session", lambda p: p.clearance["assembly"].update(peer_session_id="other")),
        ("changed cleared version", lambda p: p.clearance["assembly"].update(contract_version="1")),
        ("changed cleared schema", lambda p: p.clearance["assembly"].update(contract_schema_sha256="0" * 64)),
        ("unsafe conversation identity", lambda p: p.clearance["assembly"].update(conversation_id="../other")),
    ]
    for name, mutate in cases:
        with local_job() as (sb, prep, sends, sender):
            mutate(prep)
            if name.startswith("changed cleared") or name in (
                    "codex_task clearance", "no human review", "unsafe conversation identity"):
                prep.clear()
            category = None
            try:
                admit(sb, prep)
            except BrokerError as exc:
                category = exc.category
            check("RH: admission refuses " + name + " without persisting or sending",
                  category is ErrorCategory.INPUT_SCHEMA_INVALID and not sends
                  and not list(Path(sb.state).rglob("request.json")))

    for name in ("output.txt", "prompt.txt", "candidates.txt", "receipt.json", "certificate.json", "clearance.json"):
        with local_job() as (sb, prep, sends, sender):
            with (prep.root / name).open("ab") as stream:
                stream.write(b"changed")
            category = None
            try:
                admit(sb, prep)
            except BrokerError as exc:
                category = exc.category
            check("RH: admission refuses changed " + name + " with zero sends",
                  category is ErrorCategory.INPUT_SCHEMA_INVALID and not sends
                  and not list(Path(sb.state).rglob("request.json")))


def test_receipt_and_route():
    cases = [
        ("incomplete receipt", lambda p: p.receipt.pop("duration_seconds")),
        ("non-success receipt", lambda p: p.receipt.update(result_status="timeout")),
        ("wrong receipt version", lambda p: p.receipt.update(version=1)),
        ("malformed pass two", lambda p: p.receipt["chunks"][1].update(parser_status="parse_failure")),
        ("pass-two timeout", lambda p: p.receipt["chunks"][1].update(status="failed")),
        ("missing pass two", lambda p: p.receipt["chunks"].pop()),
        ("repaired parser", lambda p: p.receipt.update(parser_repairs=1)),
        ("wrong output hash", lambda p: p.receipt.update(output_sha256="0" * 64)),
        ("wrong request options", lambda p: p.receipt["request_options"][1].update(schema=False)),
        ("wrong request prompt", lambda p: p.receipt["request_options"][1].update(prompt_version="0" * 64)),
        ("ineligible route", lambda p: p.certificate["result"].update(eligible=False)),
        ("incomplete route", lambda p: p.certificate["result"].update(complete=False)),
        ("failed route metric", lambda p: p.certificate["result"].update(failed_metrics=["synthetic"])),
        ("route model mismatch", lambda p: p.certificate["binding"].update(digest="0" * 64)),
        ("route prompt mismatch", lambda p: p.certificate["binding"].update(prompt_sha256="0" * 64)),
        ("route validator mismatch", lambda p: p.certificate["binding"].update(validator_sha256="0" * 64)),
        ("route options mismatch", lambda p: p.certificate["binding"]["effective_options"].update(think=True)),
        ("wrong route task", lambda p: p.certificate["binding"].update(task="summarize")),
        ("fake route endpoint", lambda p: p.certificate["binding"].update(endpoint="http://other.invalid")),
    ]
    for field in ("invocation_id", "input_sha256", "canonical_input_sha256", "parent_task_id", "attempt_id"):
        cases.append(("wrong " + field, lambda p, field=field: p.manifest["receipt_bindings"].update(
            {field: "0" * (32 if field == "invocation_id" else 64)})))
    cases.append(("wrong bound options", lambda p: p.manifest["receipt_bindings"]["effective_options"].update(think=True)))
    for name, mutate in cases:
        with local_job() as (sb, prep, sends, sender):
            mutate(prep)
            prep.certificate["route_key"] = prep.route_key()
            prep.publish()  # Re-pin bytes: semantic validation must still reject them.
            category = None
            try:
                admit(sb, prep)
            except BrokerError as exc:
                category = exc.category
            check("RH: complete binding validation rejects " + name,
                  category is ErrorCategory.INPUT_SCHEMA_INVALID and not sends
                  and not list(Path(sb.state).rglob("request.json")))


def test_worker_refusals():
    for name in ("output.txt", "prompt.txt", "candidates.txt", "receipt.json", "certificate.json",
                 "preparation.json", "clearance.json", "request payload", "request classification",
                 "stripped handoff", "schema", "envelope", "session", "request during assembly", "contract snapshot"):
        with local_job() as (sb, prep, sends, sender):
            job = admit(sb, prep)
            job_dir = sb.cfg.job_dir(job["job_id"])
            request_path = Path(job_dir) / "request.json"
            request = json.loads(request_path.read_bytes())
            with contextlib.ExitStack() as stack:
                if name == "request payload":
                    request["prompt"] = "Source classification: synthetic\nChanged payload"
                    request_path.write_bytes(encoded(request))
                elif name == "request classification":
                    request["source_classification"] = "internal"
                    request_path.write_bytes(encoded(request))
                elif name == "stripped handoff":
                    for key in ("preparation_dir", "clearance_sha256", "redaction_handoff"):
                        request.pop(key)
                    request_path.write_bytes(encoded(request))
                elif name == "schema":
                    with Path(sb.cfg.schema_path).open("ab") as stream:
                        stream.write(b"\n")
                elif name == "envelope":
                    original = envelope.build_initial
                    stack.enter_context(mock.patch.object(envelope, "build_initial", side_effect=(
                        lambda *a: original(*a) + " changed")))
                elif name == "session":
                    registry.update_conversation(sb.cfg, prep.cid, peer_session_id="other")
                elif name in ("request during assembly", "contract snapshot"):
                    original = envelope.build_initial

                    def change(*args):
                        if name == "contract snapshot":
                            (Path(job_dir) / "contract.schema.json").write_bytes(b"{}")
                        else:
                            request["prompt"] = "Changed while assembling"
                            request_path.write_bytes(encoded(request))
                        return original(*args)

                    stack.enter_context(mock.patch.object(envelope, "build_initial", side_effect=change))
                else:
                    with (prep.root / name).open("ab") as stream:
                        stream.write(b"changed")
                worker.execute(job_dir)
            status = registry.read_status(sb.cfg, job["job_id"])
            check("RH: worker refuses changed " + name + " with zero sends",
                  status["error_category"] == "input_schema_invalid" and not sends,
                  json.dumps(status))


def test_retries_and_policy():
    for failure in ("transient", "changed transient clearance", "changed transient candidates", "corrective"):
        with local_job() as (sb, prep, sends, sender):
            def send(cfg, peer, **kwargs):
                sends.append(kwargs)
                if len(sends) == 1:
                    if failure.startswith("changed transient"):
                        name = "clearance.json" if failure.endswith("clearance") else "candidates.txt"
                        with (prep.root / name).open("ab") as stream:
                            stream.write(b"changed")
                    category = (ErrorCategory.PEER_OUTPUT_SCHEMA_INVALID if failure == "corrective"
                                else ErrorCategory.PEER_NONZERO_EXIT)
                    return PeerOutcome(category, peer_session_id=prep.session_id)
                return PeerOutcome(ErrorCategory.OK, payload=VALID.copy(), peer_session_id=prep.session_id)

            sender.side_effect = send
            job = admit(sb, prep)
            worker.execute(sb.cfg.job_dir(job["job_id"]))
            status = registry.read_status(sb.cfg, job["job_id"])
            if failure == "transient":
                check("RH: identical transient retry rechecks and sends the same cleared bytes",
                      len(sends) == 2 and sends[0]["prompt"] == sends[1]["prompt"]
                      and status["status"] == "complete")
            else:
                check("RH: " + failure + " is a typed refusal with zero retry sends",
                      len(sends) == 1 and status["error_category"] == "input_schema_invalid")

    for peer in ("claude", "codex"):
        with local_job(peer, "client-derived") as (sb, prep, sends, sender):
            # Even a permissive local config cannot turn clearance into a bypass.
            sb.cfg.raw["allowed_source_classifications"].append("client-derived")
            sb.cfg.raw["refused_source_classifications"] = []
            category = None
            try:
                admit(sb, prep, peer)
            except BrokerError as exc:
                category = exc.category
            check(f"RH: {peer} complete client-derived preparation stays refused",
                  category is ErrorCategory.SOURCE_CLASSIFICATION_REFUSED and not sends
                  and not list(Path(sb.state).rglob("request.json")))
        with local_job(peer) as (sb, prep, sends, sender):
            sb.cfg.raw["peers"][peer]["allowed_source_classifications"] = ["public"]
            category = None
            try:
                admit(sb, prep, peer)
            except BrokerError as exc:
                category = exc.category
            check(f"RH: {peer} allowance is independent of complete clearance",
                  category is ErrorCategory.SOURCE_CLASSIFICATION_REFUSED and not sends)
    with local_job() as (sb, prep, sends, sender):
        prep.args["allow_client_derived"] = True
        category = None
        try:
            admit(sb, prep)
        except BrokerError as exc:
            category = exc.category
        check("RH: no handoff bypass flag exists", category is ErrorCategory.INPUT_UNKNOWN_FIELD and not sends)


def test_boundaries():
    with local_job() as (sb, prep, sends, sender):
        create = registry.create_conversation

        def change(cfg, record):
            create(cfg, record)
            (prep.root / "candidates.txt").write_bytes(b"changed after admission")

        with mock.patch.object(registry, "create_conversation", side_effect=change):
            category = None
            try:
                admit(sb, prep)
            except BrokerError as exc:
                category = exc.category
        check("RH: preparation is rechecked before request.json and a failed claim is released",
              category is ErrorCategory.INPUT_SCHEMA_INVALID and not sends
              and not list(Path(sb.state).rglob("request.json"))
              and registry.load_conversation(sb.cfg, prep.cid).get("active_job_id") is None)

    for name, raw in (("duplicate keys", b'{"contract": 1, "contract": 2}'),
                      ("nonfinite JSON", b'{"duration_seconds": NaN}')):
        with local_job() as (sb, prep, sends, sender):
            (prep.root / "receipt.json").write_bytes(raw)
            prep.manifest["artifacts"]["receipt.json"] = digest(raw)
            manifest = encoded(prep.manifest)
            (prep.root / "preparation.json").write_bytes(manifest)
            prep.clearance["preparation_sha256"] = digest(manifest)
            prep.clear()
            category = None
            try:
                admit(sb, prep)
            except BrokerError as exc:
                category = exc.category
            check("RH: pinned " + name + " are refused with zero sends",
                  category is ErrorCategory.INPUT_SCHEMA_INVALID and not sends)

    with local_job() as (sb, prep, sends, sender):
        job = admit(sb, prep)
        worker.execute(sb.cfg.job_dir(job["job_id"]))
        before = registry.load_conversation(sb.cfg, prep.cid)
        category = None
        try:
            admit(sb, prep)
        except BrokerError as exc:
            category = exc.category
        check("RH: replayed initial clearance cannot overwrite its conversation",
              category is ErrorCategory.INPUT_SCHEMA_INVALID and len(sends) == 1
              and registry.load_conversation(sb.cfg, prep.cid) == before)
        prep.resume = True
        prep.args["conversation_id"] = prep.cid
        category = None
        try:
            admit(sb, prep)
        except BrokerError as exc:
            category = exc.category
        check("RH: initial clearance never authorizes a continuation",
              category is ErrorCategory.INPUT_SCHEMA_INVALID and len(sends) == 1)


def test_backend_delivery():
    for peer in ("claude", "codex"):
        sb = Sandbox()
        try:
            log = sb.marker("prompts.jsonl")
            sb.env(**{f"FAKE_{peer.upper()}_PROMPT_LOG": log})
            prep = Preparation(sb, peer)
            first = admit(sb, prep, peer)
            first_status = sb.wait(first["job_id"])
            session = registry.load_conversation(sb.cfg, prep.cid)["peer_session_id"]
            followup = Preparation(sb, peer, conversation_id=prep.cid, session_id=session)
            second = admit(sb, followup, peer)
            second_status = sb.wait(second["job_id"])
            prompts = [json.loads(line)["prompt"].encode("utf-8") for line in Path(log).read_text().splitlines()]
            check(f"RH: {peer} backend pipe delivers exactly both cleared UTF-8 prompts",
                  prompts == [(prep.root / "prompt.txt").read_bytes(), (followup.root / "prompt.txt").read_bytes()]
                  and first_status["status"] == second_status["status"] == "complete")
        finally:
            sb.cleanup()


def test_redaction_handoff():
    print("\n[redaction peer handoff]")
    test_delivery()
    test_mcp_admission()
    test_admission_refusals()
    test_receipt_and_route()
    test_worker_refusals()
    test_retries_and_policy()
    test_boundaries()
    test_backend_delivery()
