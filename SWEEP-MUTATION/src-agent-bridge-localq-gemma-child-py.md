# Gemma child mutation sweep

## Method

Read `src/agent_bridge/localq/gemma_child.py` and its offline tests in
`test_gemma_certified.py`, `test_extract_classify.py`, and
`test_local_vocabulary.py`. An AST runner made one mutation at a time in a
temporary source copy, loaded it into the adapter module in a fresh Python
process, and ran the relevant existing test classes. Each run had a 15-second
limit. The runner did not write to the production source. It skipped comments,
docstrings, and logging, and selected 42 mutants across all 15 functions
(under the 150-mutant cap). Operators included comparison flips, `and`/`or`
swaps, condition negation, numeric increment, return replacement with `None`,
empty, or `False`, statement
removal, `+`/`-` swaps, and dropped raises.

The same 42 mutants were rerun with `test_gemma_child_mutations.py`. A kill
means at least one relevant test failed. No mutant timed out. The module's
SHA-256 was `6b2fb6cb4d84465550854dfba996f6e67ff743380cd32ece5bf3a67d1d9712a4`
before and after both sweeps.

| Phase | Mutants | Killed | Survived | Raw kill rate | Behavior-changing kill rate |
| --- | ---: | ---: | ---: | ---: | ---: |
| Before new tests | 42 | 25 | 17 | 59.5% | 25/40 (62.5%) |
| After new tests | 42 | 40 | 2 | 95.2% | 40/40 (100%) |

## Mutants generated

Locations refer to the unmodified source. `boolean` swaps `and`/`or`,
`compare` flips one comparison, `negate` negates a condition, `constant`
increments one numeric constant, `return_none` returns `None`, `return_empty`
returns `[]`, `return_false` returns `False`, `remove`
removes one assignment, `arithmetic` swaps `+`/`-`, and `drop_raise` removes
one raise statement.

| ID | Location | Operator | Before | After |
| --- | --- | --- | --- | --- |
| M001 | `_existing_file`:57 | boolean | survived | killed |
| M002 | `_existing_file`:58 | drop_raise | survived | killed |
| M003 | `_existing_dir`:64 | boolean | survived | killed |
| M004 | `_existing_dir`:64 | negate | killed | killed |
| M005 | `_existing_dir`:65 | drop_raise | survived | killed |
| M006 | `_hex64`:70 | compare | killed | killed |
| M007 | `_hex64`:70 | boolean | survived | killed |
| M008 | `_hex64`:70 | return_none | killed | killed |
| M009 | `_file_sha256`:77 | constant | survived | survived |
| M010 | `_file_sha256`:75 | remove | killed | killed |
| M011 | `_file_sha256`:79 | return_none | killed | killed |
| M012 | `_load_validator`:88 | boolean | survived | killed |
| M013 | `_load_validator`:88 | negate | killed | killed |
| M014 | `_load_validator`:89 | drop_raise | survived | killed |
| M015 | `_effective_options`:135 | constant | killed | killed |
| M016 | `_effective_options`:134 | return_none | killed | killed |
| M017 | `params_refusal`:154 | compare | killed | killed |
| M018 | `params_refusal`:162 | return_none | survived | survived |
| M019 | `invoke`:185 | boolean | survived | killed |
| M020 | `invoke`:180 | compare | killed | killed |
| M021 | `invoke`:172 | drop_raise | survived | killed |
| M022 | `_invoke_validated`:246 | compare | killed | killed |
| M023 | `_invoke_validated`:247 | drop_raise | killed | killed |
| M024 | `_invoke_validated`:287 | return_none | killed | killed |
| M025 | `_document_params`:303 | compare | survived | killed |
| M026 | `_document_params`:303 | boolean | killed | killed |
| M027 | `_document_params`:304 | drop_raise | killed | killed |
| M028 | `_spans`:327 | compare | killed | killed |
| M029 | `_spans`:330 | arithmetic | survived | killed |
| M030 | `_spans`:323 | return_none | killed | killed |
| M031 | `_failure_reason`:336 | return_none | killed | killed |
| M032 | `_extract`:348 | compare | survived | killed |
| M033 | `_extract`:343 | arithmetic | killed | killed |
| M034 | `_extract`:349 | drop_raise | killed | killed |
| M035 | `_classify`:402 | compare | killed | killed |
| M036 | `_classify`:407 | boolean | killed | killed |
| M037 | `_classify`:403 | drop_raise | killed | killed |
| M038 | `main`:431 | negate | survived | killed |
| M039 | `main`:441 | return_none | survived | killed |
| M040 | `main`:432 | drop_raise | survived | killed |
| M041 | `_document_params`:318 | return_empty | killed | killed |
| M042 | `params_refusal`:162 | return_false | killed | killed |

## New tests and individual mutant proofs

Each listed test passed on the original and failed when run alone against its
mapped recorded mutant.

| Test in `tests/test_gemma_child_mutations.py` | Mutant killed |
| --- | --- |
| `ExistingFileTests.test_relative_and_missing_paths_are_refused` | M001 |
| `ExistingDirectoryTests.test_relative_and_missing_directories_are_refused` | M003 |
| `DigestShapeTests.test_only_lowercase_hex_digests_of_exact_length_are_accepted` | M007 |
| `ValidatorLoaderTests.test_absent_import_spec_has_fixed_receipt_error` | M012 |
| `InvokePreflightTests.test_empty_job_id_is_refused_before_file_access` | M019 |
| `InvokePreflightTests.test_unknown_task_has_fixed_reason_before_file_access` | M021 |
| `DocumentParameterTests.test_exact_document_limit_is_valid` | M025 |
| `SpanTests.test_repeated_value_has_every_occurrence` | M029 |
| `ExtractDeadlineTests.test_zero_remaining_budget_refuses_before_delegate` | M032 |
| `MainTests.test_non_object_payload_is_reported_as_invalid` | M038 |
| `MainTests.test_success_returns_zero` | M039 |

## Survivors left

- **M009, equivalent:** reading the hash input in 1,048,577-byte blocks instead
  of 1,048,576-byte blocks produces the same SHA-256 for every file content.
- **M018, equivalent:** replacing `return None` with `return None` changes no
  behavior.

Untestable offline: none. Defects found: none. No expected failures were added.

## Verification

- `python3 tests/test_suite.py`: 593 passed, 0 failed, 5 environmental skips.
- Relevant Gemma tests: 57 passed. New mutation tests: 11 passed.
- `tests/test_onboard.py`: 28 passed.
- `tests/test_local_worker.py`: its 14 loopback HTTP fixture cases could not bind
  `127.0.0.1` in this sandbox (`PermissionError: Operation not permitted`).
  This is an environment limit outside the Gemma adapter.
