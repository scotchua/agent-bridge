# Mutation sweep: `src/agent_bridge/orchestration/localfirst.py`

The source was read and executed from in-memory AST mutations. The throwaway runner wrote only a temporary shadow module, never the production file. Each mutant ran in a separate subprocess with a 12-second timeout and an isolated temporary `HOME`; only offline local-first unit classes were selected. No timeouts occurred. The source SHA-256 before and after was `862e286fcdf15502c0a6bcab46b5e5a496dbca10bfe35d0db0fa247f92fe6f4e`.

## Results

| Measure | Before new tests | After new tests |
| --- | ---: | ---: |
| Mutants generated | 50 | 50 |
| Killed | 21 | 49 |
| Kill rate | 42.00% | 98.00% |
| Survived | 29 | 1 |

The 50 mutants cover all 35 module functions. Operators include comparison flips, `and`/`or` swaps, condition negation, a numeric increment, returns replaced with `None`, empty dict, or `False`, one-statement deletion, `+`/`-` swaps, and a dropped raise. Comments, logging, and docstrings were excluded.

## Mutants

| ID | Function:line | Mutation | Before | After |
| --- | --- | --- | --- | --- |
| M001 | `routing_dir:108` | return_none | survived | killed |
| M002 | `calibration_path:112` | return_none | killed | killed |
| M003 | `heartbeat_path:116` | return_none | killed | killed |
| M004 | `compile_glob:144` | compare | killed | killed |
| M005 | `relative_posix_path:185` | compare | survived | killed |
| M006 | `matches_any:211` | compare | survived | killed |
| M007 | `effective_globs:222` | bool | killed | killed |
| M008 | `file_task_for_path:236` | bool | survived | killed |
| M009 | `digest_window:266` | compare | survived | killed |
| M010 | `_not_ready:301` | return_none | killed | killed |
| M011 | `readiness:338` | compare | killed | killed |
| M012 | `_covering_calibration:493` | compare | survived | killed |
| M013 | `build_calibration_record:532` | bool | survived | killed |
| M014 | `write_calibration_record:539` | remove_statement | survived | killed |
| M015 | `load_calibration_record:544` | return_none | survived | killed |
| M016 | `_reparse_tag:561` | bool | survived | killed |
| M017 | `_open_verified:609` | compare | killed | killed |
| M018 | `_read_exact:634` | compare | survived | survived |
| M019 | `_verify_unchanged:650` | compare | killed | killed |
| M020 | `_count_decode_errors:670` | return_none | killed | killed |
| M021 | `_decode_window:679` | return_none | killed | killed |
| M022 | `read_window:700` | compare | killed | killed |
| M023 | `digest_read:745` | return_none | killed | killed |
| M024 | `render_instruction:797` | compare | survived | killed |
| M025 | `digest_key:816` | return_none | survived | killed |
| M026 | `digest_intent_path:824` | return_none | survived | killed |
| M027 | `digest_receipt_path:828` | return_none | survived | killed |
| M028 | `read_digest_intent:833` | return_none | survived | killed |
| M029 | `read_digest_receipt:838` | return_none | survived | killed |
| M030 | `write_digest_intent:883` | return_none | survived | killed |
| M031 | `retire_digest_intent:902` | compare | survived | killed |
| M032 | `write_digest_receipt:950` | return_none | survived | killed |
| M033 | `digest_job_index_path:967` | return_none | survived | killed |
| M034 | `record_digest_job:971` | negate | survived | killed |
| M035 | `digest_job_max_output_chars:984` | compare | survived | killed |
| M036 | `compile_glob:143` | number | killed | killed |
| M037 | `compile_glob:169` | arithmetic | killed | killed |
| M038 | `compile_glob:141` | drop_raise | killed | killed |
| M039 | `compile_glob:140` | negate | killed | killed |
| M040 | `matches_any:205` | negate | survived | killed |
| M041 | `digest_window:266` | negate | killed | killed |
| M042 | `readiness:321` | negate | killed | killed |
| M043 | `_covering_calibration:489` | negate | killed | killed |
| M044 | `_open_verified:596` | negate | killed | killed |
| M045 | `_read_exact:641` | negate | killed | killed |
| M046 | `render_instruction:795` | negate | survived | killed |
| M047 | `retire_digest_intent:898` | negate | survived | killed |
| M048 | `digest_job_max_output_chars:978` | negate | survived | killed |
| M049 | `read_digest_intent:833` | return_empty | survived | killed |
| M050 | `routing_dir:108` | return_false | survived | killed |

## New tests

- `LocalFirstPathMutationTests.test_routing_dir_joins_the_state_root`
- `LocalFirstPathMutationTests.test_relative_posix_path_converts_windows_separators`
- `LocalFirstPathMutationTests.test_matches_any_accepts_a_file_inside_the_repo`
- `LocalFirstPathMutationTests.test_file_task_for_path_requires_pilot_authorization`
- `LocalFirstPathMutationTests.test_digest_key_has_a_stable_32_character_identity`
- `LocalFirstPathMutationTests.test_digest_intent_path_names_a_json_record`
- `LocalFirstPathMutationTests.test_digest_receipt_path_names_a_json_record`
- `LocalFirstPathMutationTests.test_read_digest_intent_returns_the_saved_record`
- `LocalFirstPathMutationTests.test_read_digest_receipt_returns_the_saved_record`
- `LocalFirstPathMutationTests.test_write_digest_intent_returns_and_saves_the_same_record`
- `LocalFirstPathMutationTests.test_retire_digest_intent_requires_the_complete_binding`
- `LocalFirstPathMutationTests.test_write_digest_receipt_returns_and_saves_the_same_record`
- `LocalFirstPathMutationTests.test_digest_job_index_path_names_a_json_record`
- `LocalFirstPathMutationTests.test_record_digest_job_saves_the_output_limit`
- `LocalFirstPathMutationTests.test_digest_job_max_output_chars_accepts_positive_only`
- `LocalFirstValueMutationTests.test_digest_window_accepts_a_zero_byte_file`
- `LocalFirstValueMutationTests.test_covering_calibration_accepts_an_eighteen_digit_size_key`
- `LocalFirstValueMutationTests.test_build_calibration_record_rejects_a_non_string_backend`
- `LocalFirstValueMutationTests.test_write_calibration_record_persists_the_record`
- `LocalFirstValueMutationTests.test_load_calibration_record_returns_the_record`
- `LocalFirstValueMutationTests.test_reparse_tag_preserves_a_nonzero_tag`
- `LocalFirstValueMutationTests.test_render_instruction_accepts_a_supported_task`

All 22 new tests pass on the original module, and each was also run alone against an assigned recorded mutant and failed there. The focused before/after runs use `test_local_first` glob, window, read, digest, and readiness classes, plus the new tests in the after run.

## Survivors left

- M018: equivalent for ordinary regular-file reads. Changing `_read_exact` from `while remaining > 0` to `while remaining >= 0` adds one `os.read(descriptor, 0)` after the byte count reaches zero. That read returns `b""`, so the existing break follows and the returned bytes and errors are unchanged.
- Untestable offline: none.
- Defects found: none.

## Verification

- `python3 tests/test_suite.py`: 593 passed, 0 failed, 5 environment skips.
- `python3 tests/test_localfirst_mutation.py`: 22 passed.
- `python3 tests/test_onboard.py`: 28 passed.
- `python3 tests/test_hostenv.py`: 21 passed, 11 skipped.
- `python3 tests/test_automatic_gate.py`: 169 passed.
- `python3 tests/test_delegation_audit.py`: 40 passed.
- `tests/test_local_worker.py` and `tests/test_automatic_delegation_e2e.py` could not set up their local HTTP servers because the sandbox rejected binds to `127.0.0.1` with `PermissionError`.
