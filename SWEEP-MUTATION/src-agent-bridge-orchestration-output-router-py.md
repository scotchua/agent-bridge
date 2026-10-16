# Mutation sweep: `output_router.py`

## Method

- Read the complete module and its existing `tests/test_output_router.py` coverage.
- Generated 49 one-change, function-scoped mutants. The set covers all 15 functions and includes comparison flips, `and`/`or` swaps, condition negation, numeric changes, return changes, statement removal, `+`/`-` swap, and a dropped raise. No comment, logging, or docstring was mutated.
- Executed each mutant in memory in a fresh Python process with a temporary `HOME` and a five-second timeout. The baseline was the 13 existing router tests; the after run added 22 tests in `tests/test_output_router_mutation.py`.
- The production module was never written. Its SHA-256 before and after the sweep was `605bea6b68d7a316fc51d1369e2f097ac27b36907c01ce2814ab9a992a53f860`.

## Kill rates

| Test set | Killed | Generated | Kill rate |
| --- | ---: | ---: | ---: |
| Existing router tests | 19 | 49 | 38.78% |
| Existing plus new tests | 48 | 49 | 97.96% |

## Mutants

| ID | Function | Mutation | Before | After |
| --- | --- | --- | --- | --- |
| M01 | `is_wrapped_command` | return: `return bool(re.search(` → `return False and bool(re.search(` | killed | killed |
| M02 | `_git_routable` | and_or: `while rest and (` → `while rest or (` | killed | killed |
| M03 | `_git_routable` | compare: `len(rest) > 1` → `len(rest) >= 1` | survived | survived (equivalent) |
| M04 | `_git_routable` | compare: `subcommand == "log"` → `subcommand != "log"` | killed | killed |
| M05 | `_git_routable` | and_or: `"--stat" in arguments or "--" in arguments` → `"--stat" in arguments and "--" in arguments` | survived | killed |
| M06 | `_git_routable` | negate: `return not any(` → `return any(` | killed | killed |
| M07 | `is_routable_command` | and_or: `if not isinstance(command, str) or not command.strip()` → `if not isinstance(command, str) and not command.strip()` | survived | killed |
| M08 | `is_routable_command` | negate: `if re.search(r"\&#124;[^&#124;]` → `if not re.search(r"\&#124;[^&#124;]` | killed | killed |
| M09 | `is_routable_command` | and_or: `joined) or re.search(r"(?:^&#124;\s)python` → `joined) and re.search(r"(?:^&#124;\s)python` | killed | killed |
| M10 | `is_routable_command` | and_or: `program == "rg" or any(` → `program == "rg" and any(` | killed | killed |
| M11 | `is_routable_command` | and_or: `return recursive and not bounded` → `return recursive or not bounded` | survived | killed |
| M12 | `output_directory` | return: `return Path(state_root) / "routing" / OUTPUT_DIR` → `return None` | killed | killed |
| M13 | `ledger_path` | return: `return str(Path(state_root) / "routing" / LEDGER)` → `return None` | killed | killed |
| M14 | `prune_output_files` | negate: `if not directory.is_dir():` → `if directory.is_dir():` | survived | killed |
| M15 | `prune_output_files` | compare: `path.stat().st_mtime < cutoff` → `path.stat().st_mtime <= cutoff` | survived | killed |
| M16 | `prune_output_files` | and_or: `path.is_file() and path.stat()` → `path.is_file() or path.stat()` | survived | killed |
| M17 | `capture_output` | remove: `prune_output_files(state_root, now=now)` → `∅` | survived | killed |
| M18 | `capture_output` | remove: `store.atomic_write_bytes(str(path), output)` → `∅` | killed | killed |
| M19 | `capture_output` | return: `return str(path)` → `return None` | survived | killed |
| M20 | `_raw` | remove: `sys.stdout.flush()` → `∅` | survived | killed |
| M21 | `_fit_for_queue` | constant: `max_input_bytes * 0.6` → `max_input_bytes * 0.7` | survived | killed |
| M22 | `_fit_for_queue` | compare: `len(output) <= budget` → `len(output) < budget` | survived | killed |
| M23 | `_fit_for_queue` | return: `return output, False` → `return output, True` | survived | killed |
| M24 | `_fit_for_queue` | constant: `budget // 2` → `budget // 3` | survived | killed |
| M25 | `_failure_tail` | constant: `matches[-40:]` → `matches[-39:]` | survived | killed |
| M26 | `_digest_text` | negate: `if not isinstance(payload, dict):` → `if isinstance(payload, dict):` | killed | killed |
| M27 | `_digest_text` | and_or: `isinstance(output, dict) and isinstance(output.get("text"), str)` → `isinstance(output, dict) or isinstance(output.get("text"), str)` | survived | killed |
| M28 | `_digest_text` | return: `return output["text"]` → `return None` | killed | killed |
| M29 | `_digest_text` | negate: `if isinstance(output, str):` → `if not isinstance(output, str):` | survived | killed |
| M30 | `_digest_text` | return: `return output` → `return None` | survived | killed |
| M31 | `_record` | remove: `store.append_ledger(ledger_path(state_root), {` → `return store.append_ledger(ledger_path(state_root), {` | killed | killed |
| M32 | `route_output` | compare: `len(output) < SMALL_OUTPUT_BYTES` → `len(output) <= SMALL_OUTPUT_BYTES` | survived | killed |
| M33 | `route_output` | return: `return 0` → `return None` | survived | killed |
| M34 | `route_output` | compare: `intake.queue.backend_id == "gemma_certified"` → `intake.queue.backend_id != "gemma_certified"` | survived | killed |
| M35 | `route_output` | and_or: `receipt.get("decision") != "local" or not job_id` → `receipt.get("decision") != "local" and not job_id` | survived | killed |
| M36 | `route_output` | drop_raise: `raise RuntimeError(waiver)` → `∅` | survived | killed |
| M37 | `route_output` | plus_minus: `deadline = started + wait_seconds` → `deadline = started - wait_seconds` | killed | killed |
| M38 | `route_output` | compare: `while clock() < deadline:` → `while clock() <= deadline:` | survived | killed |
| M39 | `route_output` | and_or: `result.get("status") == "queued" and str(` → `result.get("status") == "queued" or str(` | killed | killed |
| M40 | `route_output` | and_or: `result.get("status") == "complete" and digest is not None` → `result.get("status") == "complete" or digest is not None` | survived | killed |
| M41 | `route_output` | negate: `if not digest.endswith("\n"):` → `if digest.endswith("\n"):` | survived | killed |
| M42 | `route_output` | negate: `if tail:` → `if not tail:` | killed | killed |
| M43 | `route_output` | negate: `if waiver is None:` → `if waiver is not None:` | killed | killed |
| M44 | `run_command` | return: `return 127` → `return None` | survived | killed |
| M45 | `run_command` | return: `return int(completed.returncode)` → `return None` | killed | killed |
| M46 | `run_command` | return: `return int(completed.returncode)` → `return None` | killed | killed |
| M47 | `main` | compare: `command[:1] == ["--"]` → `command[:1] != ["--"]` | survived | killed |
| M48 | `main` | negate: `if not command:` → `if command:` | survived | killed |
| M49 | `main` | return: `return run_command(command, config_path=args.config)` → `return None` | survived | killed |

## Surviving mutant

- **M03 (`_git_routable`) — equivalent.** It changes `len(rest) > 1` to `len(rest) >= 1` while consuming a `git -C` or `git -c` option. The branches differ only when `rest` contains the option alone; both `rest[1:]` and `rest[2:]` then yield `[]`, and the function returns `False`. No externally visible result changes.
- Untestable offline: none. Defects found: none.

## New tests

Each test below passes on the original module and fails on at least one mutant that survived the baseline. The IDs list those newly killed mutants; multiple tests can kill the same mutant.

| Test name | Newly killed mutant IDs |
| --- | --- |
| `test_git_routable_rejects_diff_stat_without_path_separator` | M05 |
| `test_is_routable_command_rejects_non_string_and_unbounded_grep` | M07, M11 |
| `test_prune_output_files_removes_old_capture` | M14 |
| `test_prune_output_files_keeps_capture_at_cutoff` | M15, M16 |
| `test_capture_output_prunes_before_writing_and_returns_path` | M14, M17, M19 |
| `test_raw_flushes_text_before_buffer_bytes` | M20 |
| `test_fit_for_queue_uses_sixty_percent_budget` | M21 |
| `test_fit_for_queue_keeps_exact_budget_without_clipping` | M22, M23 |
| `test_fit_for_queue_keeps_equal_head_and_tail_halves` | M21, M24 |
| `test_failure_tail_keeps_last_forty_matching_lines` | M25 |
| `test_digest_text_rejects_non_string_text_and_output` | M27, M29 |
| `test_digest_text_accepts_plain_string_output` | M27, M29, M30 |
| `test_route_output_routes_exact_threshold_and_returns_zero` | M32 |
| `test_route_output_small_output_returns_zero` | M33 |
| `test_route_output_certified_backend_omits_instruction` | M32, M34 |
| `test_route_output_refusal_waives_even_with_job_id` | M32, M35, M36 |
| `test_route_output_zero_wait_never_polls` | M32, M38 |
| `test_route_output_failed_result_with_digest_waives` | M32, M40 |
| `test_route_output_digest_with_newline_is_not_doubled` | M32, M41 |
| `test_run_command_returns_127_when_child_cannot_start` | M44 |
| `test_main_strips_separator_and_returns_child_code` | M47, M48, M49 |
| `test_main_rejects_empty_command` | M48 |

## Verification

- `python3 tests/test_output_router.py`: passed (13 tests).
- `python3 tests/test_output_router_mutation.py`: passed (22 tests).
- `python3 tests/test_onboard.py`: passed (28 tests).
- `python3 tests/test_hostenv.py`: passed (32 tests, 11 skipped).
- `python3 tests/test_automatic_gate.py`: passed (169 tests).
- `python3 tests/test_delegation_audit.py`: passed (40 tests).
- `python3 tests/test_local_worker.py` and `python3 tests/test_automatic_delegation_e2e.py`: fixture setup could not bind `127.0.0.1` in this sandbox (`PermissionError: [Errno 1] Operation not permitted`). No external service was contacted.
- `python3 tests/test_suite.py`: passed (593 checks, 5 environmental skips).
