"""Bounded adapter for the installed certified Gemma local delegate.

The local queue speaks JSON to child adapters. The installed delegate reads
the exact document as UTF-8 stdin, writes the draft as plain stdout bytes,
and writes a ``local-delegate/v2`` receipt. This bridges those contracts
without selecting another provider, retrying another model, or creating a
second quarantine mechanism. Only ``summarize`` is certified here.

Repository tests exercise this adapter against contract fakes; they do not
prove compatibility with the installed delegate and validator. Before an
operator selects this backend in live configuration, a synthetic smoke test
must exercise the exact hash-pinned installed files. Those hashes are
compatibility assertions against accidental drift, not an integrity boundary
against a same-user process that can rewrite the files or configuration. The
model digest is attested by the delegate's validated receipt; this adapter does
not independently measure the model artifact.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import sysconfig
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any


RECEIPT_CONTRACT = "local-delegate/v2"
SUPPORTED_TASKS = frozenset({"summarize", "extract", "classify"})
CERTIFIED_SUMMARIZE_INSTRUCTION = "Summarize in one sentence."
UNSUPPORTED_KIND_REASON = "unsupported_kind"
EXTRACT_FIELDS = ("party", "date", "doc_type", "reference", "description",
                  "amount", "terms", "due_date", "account")
MAX_EXTRACT_DOCUMENTS = 16
MAX_EXTRACT_DOCUMENT_BYTES = 24_000


class GemmaRefusal(RuntimeError):
    """A fixed refusal from the configured Gemma route."""


class GemmaReceiptError(RuntimeError):
    """The delegate receipt is absent, invalid, or not bound to this call."""


def _existing_file(value: str, field: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError(f"{field} must be an existing absolute regular file")
    return path


def _existing_dir(value: str, field: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise ValueError(f"{field} must be an existing absolute directory")
    return path


def _hex64(value: Any) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(char in "0123456789abcdef" for char in value))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@contextmanager
def _load_validator(path: Path):
    """Load the configured validator using its installation and stdlib paths."""
    name = "_agent_bridge_delegate_receipt_" + hashlib.sha256(
        str(path).encode("utf-8")).hexdigest()[:16]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise GemmaReceiptError("receipt_validator_unloadable")
    module = importlib.util.module_from_spec(spec)
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = {alias.name.partition(".")[0]
                   for node in ast.walk(tree)
                   if isinstance(node, ast.Import) for alias in node.names}
        imports.update(node.module.partition(".")[0]
                       for node in ast.walk(tree)
                       if isinstance(node, ast.ImportFrom) and node.level == 0
                       and node.module)
    except (OSError, SyntaxError, UnicodeError) as exc:
        raise GemmaReceiptError("receipt_validator_unloadable") from exc
    # Cached third-party or cwd modules must not satisfy an installed sibling
    # import. The configured directory is first, followed only by stdlib paths.
    shadowed = {key: sys.modules.pop(key) for key in imports
                if key not in sys.stdlib_module_names and key in sys.modules}
    original_path = sys.path[:]
    stdlib_paths = {Path(sysconfig.get_path(name)).resolve()
                    for name in ("stdlib", "platstdlib")}
    stdlib_paths.update(path / "lib-dynload" for path in tuple(stdlib_paths))
    sys.path[:] = [str(path.parent)] + [entry for entry in original_path
                                      if entry and Path(entry).resolve() in stdlib_paths]
    prior_modules = set(sys.modules)
    try:
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            raise GemmaReceiptError("receipt_validator_unloadable") from exc
        if not callable(getattr(module, "read_success", None)):
            raise GemmaReceiptError("receipt_validator_contract_missing")
        yield module
    finally:
        for key in set(sys.modules) - prior_modules:
            loaded = sys.modules[key]
            location = getattr(loaded, "__file__", None)
            if location and Path(location).parent.resolve() == path.parent.resolve():
                sys.modules.pop(key, None)
        sys.modules.update(shadowed)
        sys.path[:] = original_path


def _effective_options(timeout: float) -> dict[str, Any]:
    """Exact defaults implied by the fixed delegate CLI invocation."""
    return {
        "chunk_chars": 24000,
        "job_timeout": float(timeout),
        "think": False,
        "schema": False,
        "second_pass": False,
        "repair_retries": 0,
        "map_scope": "run",
        "roster_sha256": None,
    }


def params_refusal(params: dict[str, Any], task: str = "summarize") -> str | None:
    """The certified delegate's whole parameter contract, as a fixed reason.

    The queue calls this at submission, so a job the delegate will refuse is
    refused before it is queued (two of the first nine live jobs failed
    ``custom_instruction_unsupported`` only after waiting their turn), and
    ``invoke`` calls it again before running anything.
    """
    if task == "extract":
        return None if set(params) == {"documents"} else "params_invalid_for_gemma_certified"
    if task == "classify":
        return None if not params else "params_invalid_for_gemma_certified"
    if set(params) - {"instruction"}:
        return "params_invalid_for_gemma_certified"
    if params.get("instruction", "") not in ("", CERTIFIED_SUMMARIZE_INSTRUCTION):
        return "custom_instruction_unsupported"
    return None


def invoke(payload: dict[str, Any], *, delegate: str, python: str,
           receipt_root: str, receipt_validator: str, model_digest: str,
           delegate_sha256: str, validator_sha256: str,
           timeout: float = 600.0) -> dict[str, Any]:
    """Invoke the certified delegate once and validate its exact receipt."""
    task = payload.get("task_type")
    if task not in SUPPORTED_TASKS:
        raise ValueError(UNSUPPORTED_KIND_REASON)
    text = payload.get("input")
    if not isinstance(text, str):
        raise ValueError("input_invalid")
    params = payload.get("params")
    if not isinstance(params, dict):
        raise ValueError("params_invalid")
    reason = params_refusal(params, task)
    if reason == "custom_instruction_unsupported":
        raise GemmaRefusal(reason)
    if reason is not None:
        raise ValueError(reason)
    job_id = payload.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("job_id_invalid")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError("timeout_invalid")
    for name, digest in (("model_digest", model_digest),
                         ("delegate_sha256", delegate_sha256),
                         ("validator_sha256", validator_sha256)):
        if not _hex64(digest):
            raise ValueError(f"{name}_invalid")

    delegate_path = _existing_file(delegate, "delegate")
    python_path = _existing_file(python, "python")
    receipt_root_path = _existing_dir(receipt_root, "receipt_root")
    validator_path = _existing_file(receipt_validator, "receipt_validator")
    if _file_sha256(delegate_path) != delegate_sha256:
        raise GemmaRefusal("delegate_source_changed")
    if _file_sha256(validator_path) != validator_sha256:
        raise GemmaRefusal("receipt_validator_source_changed")
    timeout = float(timeout)

    with _load_validator(validator_path) as validator:
        if task == "summarize":
            return _invoke_validated(
                text, job_id, delegate_path, python_path, receipt_root_path,
                model_digest, timeout, validator, task)
        if task == "classify":
            return _classify(text, job_id, delegate_path, python_path, receipt_root_path,
                             model_digest, timeout, validator)
        return _extract(text, params["documents"], job_id, delegate_path, python_path,
                        receipt_root_path, model_digest, timeout, validator)


def _invoke_validated(text: str, job_id: str, delegate_path: Path,
                      python_path: Path, receipt_root_path: Path,
                      model_digest: str, timeout: float, validator: Any,
                      task: str = "summarize") -> dict[str, Any]:
    """Run one delegate call after validator preflight succeeds."""
    invocation_id = uuid.uuid4().hex
    input_bytes = text.encode("utf-8")
    input_sha256 = hashlib.sha256(input_bytes).hexdigest()
    # The installed delegate owns this transformation: its documented CLI
    # accepts the raw parent identity and stores only sha256(raw). Pass job_id
    # below, but give the validator the binding the receipt must contain.
    # A real installed-file synthetic smoke remains the compatibility proof;
    # the repository fake only mirrors this documented boundary.
    parent_task_id = hashlib.sha256(job_id.encode("utf-8")).hexdigest()
    argv = [str(python_path), str(delegate_path), "--task", task,
            "--invocation-id", invocation_id, "--parent-task-id", job_id,
            "--job-timeout", str(timeout)]
    env = {"PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
           "CODEX_BRIDGE_RECEIPTS_DIR": str(receipt_root_path)}

    # The outer SubprocessBackend creates the process group. Keeping the
    # delegate in that group lets an outer timeout/cancel terminate both.
    try:
        completed = subprocess.run(
            argv, input=input_bytes, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env, timeout=timeout,
            check=False, shell=False)
    except subprocess.TimeoutExpired as exc:
        raise GemmaRefusal("gemma_delegate_timeout") from exc
    if completed.returncode != 0:
        raise GemmaRefusal("gemma_delegate_unavailable")
    output = completed.stdout

    receipt_path = receipt_root_path / f"local-delegate-{invocation_id}.json"
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise GemmaReceiptError("receipt_missing")
    try:
        receipt_bytes = receipt_path.read_bytes()
    except OSError as exc:
        raise GemmaReceiptError("receipt_unreadable") from exc

    try:
        record = validator.read_success(
            receipt_path, output,
            receipt_sha256=hashlib.sha256(receipt_bytes).hexdigest(),
            invocation_id=invocation_id,
            input_sha256=input_sha256,
            canonical_input_sha256=input_sha256,
            effective_options=_effective_options(timeout),
            parent_task_id=parent_task_id,
            attempt_id=None)
    except Exception as exc:
        raise GemmaReceiptError("receipt_invalid") from exc
    if not isinstance(record, dict):
        raise GemmaReceiptError("receipt_invalid")
    if record.get("contract") != RECEIPT_CONTRACT:
        raise GemmaReceiptError("receipt_contract_unsupported")
    if record.get("task") != task:
        raise GemmaReceiptError("receipt_task_mismatch")
    if record.get("result_status") != "success":
        raise GemmaReceiptError("receipt_not_success")
    if record.get("model_digest") != model_digest:
        raise GemmaReceiptError("receipt_model_digest_mismatch")
    if record.get("output_hash_kind") != "stdout":
        raise GemmaReceiptError("receipt_output_hash_kind_invalid")
    try:
        output_text = output.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GemmaReceiptError("output_not_utf8") from exc

    return {
        "output": ({"task": "summarize", "text": output_text}
                   if task == "summarize" else {"task": task, "text": output_text}),
        "provider": "gemma_certified",
        "task": task,
        "model_digest": record["model_digest"],
        "invocation_id": invocation_id,
        "input_sha256": record["input_sha256"],
        "output_sha256": record["output_sha256"],
        "result_status": "success",
        "receipt_contract": RECEIPT_CONTRACT,
    }


def _document_params(documents: Any) -> list[dict[str, str]]:
    """Validate the explicit, ordered extract document association contract."""
    if not isinstance(documents, list) or not documents or len(documents) > MAX_EXTRACT_DOCUMENTS:
        raise ValueError("extract_documents_invalid")
    result: list[dict[str, str]] = []
    ids: set[str] = set()
    for position, document in enumerate(documents):
        if not isinstance(document, dict) or set(document) != {"id", "text"}:
            raise ValueError(f"extract_document_{position}_invalid")
        identifier, text = document["id"], document["text"]
        if (not isinstance(identifier, str) or not identifier or identifier in ids
                or not isinstance(text, str)):
            raise ValueError(f"extract_document_{position}_invalid")
        if len(text.encode("utf-8")) > MAX_EXTRACT_DOCUMENT_BYTES:
            raise ValueError(f"extract_document_{position}_oversize")
        ids.add(identifier)
        result.append({"id": identifier, "text": text})
    return result


def _spans(text: str, value: str | None) -> list[list[int]]:
    if value is None:
        return []
    spans, start = [], 0
    while True:
        index = text.find(value, start)
        if index < 0:
            return spans
        spans.append([index, index + len(value)])
        start = index + 1


def _failure_reason(exc: BaseException) -> str:
    """Return the adapter's fixed reason without discarding its cause."""
    detail = str(exc)
    return detail if detail else type(exc).__name__


def _extract(text: str, documents: Any, job_id: str, delegate: Path, python: Path,
             receipt_root: Path, model_digest: str, timeout: float, validator: Any) -> dict[str, Any]:
    """Call the delegate once per explicit document; never infer association."""
    valid_documents = _document_params(documents)
    deadline = time.monotonic() + timeout
    output_documents = []
    batch_model_digest: str | None = None
    for position, document in enumerate(valid_documents):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GemmaRefusal(f"extract_document_{position}_deadline_exceeded")
        try:
            call = _invoke_validated(document["text"], f"{job_id}:{position}", delegate,
                                     python, receipt_root, model_digest, remaining, validator, "extract")
            parsed = json.loads(call["output"]["text"])
        except (GemmaRefusal, GemmaReceiptError, ValueError, json.JSONDecodeError) as exc:
            raise GemmaRefusal(
                f"extract_document_{position}_failed:{_failure_reason(exc)}") from exc
        if not isinstance(parsed, list):
            raise GemmaRefusal(f"extract_document_{position}_schema_invalid")
        records = []
        for candidate in parsed:
            if not isinstance(candidate, dict) or set(candidate) != set(EXTRACT_FIELDS):
                raise GemmaRefusal(f"extract_document_{position}_schema_invalid")
            fields = {}
            for field in EXTRACT_FIELDS:
                value = candidate[field]
                if value is not None and not isinstance(value, str):
                    raise GemmaRefusal(f"extract_document_{position}_schema_invalid")
                spans = _spans(document["text"], value)
                if value is not None and not spans:
                    raise GemmaRefusal(f"extract_document_{position}_value_absent")
                fields[field] = {"value": value, "spans": spans, "ambiguous": len(spans) > 1}
            records.append({"fields": fields})
        status = "no_record" if not records else "ok" if len(records) == 1 else "multiple_records"
        if call["model_digest"] != model_digest:
            raise GemmaRefusal(f"extract_document_{position}_failed:receipt_model_digest_mismatch")
        if batch_model_digest is None:
            batch_model_digest = call["model_digest"]
        elif call["model_digest"] != batch_model_digest:
            raise GemmaRefusal(f"extract_document_{position}_failed:receipt_model_digest_mismatch")
        output_documents.append({"position": position, "id": document["id"],
                                 "sha256": hashlib.sha256(document["text"].encode()).hexdigest(),
                                 "record_status": status, "records": records,
                                 "invocation_id": call["invocation_id"],
                                 "model_digest": call["model_digest"],
                                 "input_sha256": call["input_sha256"]})
    return {"output": {"task": "extract", "input_sha256": hashlib.sha256(text.encode()).hexdigest(),
                        "documents": output_documents}, "provider": "gemma_certified", "task": "extract",
            "model_digest": batch_model_digest,
            "result_status": "success"}


def _classify(text: str, job_id: str, delegate: Path, python: Path, receipt_root: Path,
              model_digest: str, timeout: float, validator: Any) -> dict[str, Any]:
    try:
        call = _invoke_validated(text, job_id, delegate, python, receipt_root, model_digest,
                                 timeout, validator, "classify")
    except (GemmaRefusal, GemmaReceiptError, ValueError) as exc:
        raise GemmaRefusal(f"classify_failed:{_failure_reason(exc)}") from exc
    labels = {"INCOME", "COGS", "PAYROLL", "OPEX", "OWNER", "TRANSFER", "FEE", "UNSURE", "SKIP"}
    lines = text.splitlines()
    output_lines = call["output"]["text"].splitlines()
    if len(output_lines) != len(lines):
        raise GemmaRefusal("classify_line_count_mismatch")
    result = []
    for number, (source, output) in enumerate(zip(lines, output_lines), 1):
        label, separator, preview = output.partition(" | ")
        if not separator or label not in labels or preview != source[:60]:
            raise GemmaRefusal(f"classify_line_{number}_invalid")
        result.append({"line": number, "line_sha256": hashlib.sha256(source.encode()).hexdigest(),
                       "label": label})
    return {"output": {"task": "classify", "input_sha256": hashlib.sha256(text.encode()).hexdigest(),
                        "lines": result}, "provider": "gemma_certified", "task": "classify",
            "model_digest": call["model_digest"], "invocation_id": call["invocation_id"],
            "input_sha256": call["input_sha256"],
            "result_status": "success"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--delegate", required=True)
    parser.add_argument("--python", required=True)
    parser.add_argument("--receipt-root", required=True)
    parser.add_argument("--receipt-validator", required=True)
    parser.add_argument("--delegate-sha256", required=True)
    parser.add_argument("--validator-sha256", required=True)
    parser.add_argument("--model-digest", required=True)
    parser.add_argument("--timeout", type=float, default=600.0)
    args = parser.parse_args(argv)
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("payload_invalid")
        result = invoke(
            payload, delegate=args.delegate, python=args.python,
            receipt_root=args.receipt_root,
            receipt_validator=args.receipt_validator,
            delegate_sha256=args.delegate_sha256,
            validator_sha256=args.validator_sha256,
            model_digest=args.model_digest, timeout=args.timeout)
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except Exception as exc:  # parent consumes only fixed adapter reasons
        failure = {"ok": False, "error": type(exc).__name__}
        if type(exc) in (ValueError, GemmaRefusal, GemmaReceiptError):
            failure["error_detail"] = str(exc)
        print(json.dumps(failure, ensure_ascii=True, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
