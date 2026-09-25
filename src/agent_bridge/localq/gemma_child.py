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
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any


RECEIPT_CONTRACT = "local-delegate/v2"
SUPPORTED_TASKS = frozenset({"summarize"})
CERTIFIED_SUMMARIZE_INSTRUCTION = "Summarize in one sentence."
UNSUPPORTED_KIND_REASON = "unsupported_kind"


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


def _load_validator(path: Path) -> Any:
    """Load only the explicitly configured installed validator file."""
    name = "_agent_bridge_delegate_receipt_" + hashlib.sha256(
        str(path).encode("utf-8")).hexdigest()[:16]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise GemmaReceiptError("receipt_validator_unloadable")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise GemmaReceiptError("receipt_validator_unloadable") from exc
    if not callable(getattr(module, "read_success", None)):
        raise GemmaReceiptError("receipt_validator_contract_missing")
    return module


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
    if set(params) - {"instruction"}:
        raise ValueError("params_invalid_for_gemma_certified")
    instruction = params.get("instruction", "")
    if instruction not in ("", CERTIFIED_SUMMARIZE_INSTRUCTION):
        raise GemmaRefusal("custom_instruction_unsupported")
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

    invocation_id = uuid.uuid4().hex
    input_bytes = text.encode("utf-8")
    input_sha256 = hashlib.sha256(input_bytes).hexdigest()
    # The installed delegate owns this transformation: its documented CLI
    # accepts the raw parent identity and stores only sha256(raw). Pass job_id
    # below, but give the validator the binding the receipt must contain.
    # A real installed-file synthetic smoke remains the compatibility proof;
    # the repository fake only mirrors this documented boundary.
    parent_task_id = hashlib.sha256(job_id.encode("utf-8")).hexdigest()
    argv = [str(python_path), str(delegate_path), "--task", "summarize",
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

    validator = _load_validator(validator_path)
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
    if record.get("task") != "summarize":
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
        "output": {"task": "summarize", "text": output_text},
        "provider": "gemma_certified",
        "task": "summarize",
        "model_digest": record["model_digest"],
        "invocation_id": invocation_id,
        "input_sha256": record["input_sha256"],
        "output_sha256": record["output_sha256"],
        "result_status": "success",
        "receipt_contract": RECEIPT_CONTRACT,
    }


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
