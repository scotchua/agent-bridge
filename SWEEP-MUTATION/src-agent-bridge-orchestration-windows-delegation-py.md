# Mutation sweep: windows_delegation.py

Source SHA-256 before and after the in-memory sweep: `b1dc094d09a05c4722c3b609afe950fb1df5ca42632968a37a9fa4adebff6439`.
No production file was written during mutation runs. Each subprocess had a 20-second timeout and an isolated temporary HOME; no network or live Windows runtime was used.

## Results

| Phase | Killed | Total | Kill rate |
| --- | ---: | ---: | ---: |
| Existing offline tests | 39 | 42 | 92.86% |
| Existing plus new tests | 42 | 42 | 100% |

Mutants were selected across all 38 functions and methods. Operators used: comparison flip, `and`/`or` swap, condition negation, numeric increment, return replacement with `None`, empty string, or `False`, statement removal, `+`/`-` swap, and dropped `raise`. AST edits skipped comments, docstrings, and logging. The after sweep reran all three survivors; the original 39 kills carry forward because their tests were retained unchanged.

| ID | Function | Line | Mutation | Before | After |
| --- | --- | ---: | --- | --- | --- |
| M001 | `DelegationRefused.__init__` | 92 | `remove_statement` | killed | killed |
| M002 | `load_manifest` | 116 | `return_none` | killed | killed |
| M003 | `load_sidecar` | 124 | `negate_if` | killed | killed |
| M004 | `host_architecture` | 138 | `return_none` | killed | killed |
| M005 | `guest_runner_sha256` | 156 | `negate_if` | killed | killed |
| M006 | `classification_allowed` | 171 | `negate_if` | killed | killed |
| M007 | `build_job_request` | 202 | `negate_if` | killed | killed |
| M008 | `build_auth_probe_request` | 260 | `negate_if` | killed | killed |
| M009 | `_reparse_tag` | 337 | `swap_bool` | killed | killed |
| M010 | `_is_link_like` | 341 | `swap_bool` | killed | killed |
| M011 | `_PackRoot.__init__` | 354 | `remove_statement` | killed | killed |
| M012 | `_PackRoot.contains` | 360 | `flip_compare` | killed | killed |
| M013 | `pack_workspace` | 396 | `negate_if` | killed | killed |
| M014 | `_pack_directory` | 441 | `negate_if` | killed | killed |
| M015 | `_identity_of` | 477 | `negate_if` | survived | killed |
| M016 | `_pack_file` | 484 | `negate_if` | killed | killed |
| M017 | `read_brief` | 563 | `negate_if` | killed | killed |
| M018 | `_read_descriptor` | 617 | `negate_if` | killed | killed |
| M019 | `_constant_time_equal` | 625 | `return_none` | killed | killed |
| M020 | `resolve_base` | 656 | `negate_if` | killed | killed |
| M021 | `_run_git` | 684 | `negate_if` | killed | killed |
| M022 | `build_provider_job_request` | 720 | `negate_if` | killed | killed |
| M023 | `_decode_field` | 777 | `negate_if` | killed | killed |
| M024 | `parse_guest_response` | 800 | `negate_if` | killed | killed |
| M025 | `_parse_verification` | 898 | `negate_if` | killed | killed |
| M026 | `write_job_outputs` | 936 | `negate_if` | killed | killed |
| M027 | `_write_owner_only` | 971 | `negate_if` | survived | killed |
| M028 | `build_outcome` | 1028 | `negate_if` | killed | killed |
| M029 | `_refusal_outcome` | 1071 | `return_none` | killed | killed |
| M030 | `_no_auth_source` | 1089 | `drop_raise` | killed | killed |
| M031 | `enrolled_auth_source` | 1115 | `return_none` | killed | killed |
| M032 | `enrolled_auth_source.source` | 1104 | `negate_if` | killed | killed |
| M033 | `WindowsWslExecutor.__init__` | 1135 | `remove_statement` | killed | killed |
| M034 | `WindowsWslExecutor.refusal` | 1163 | `negate_if` | killed | killed |
| M035 | `WindowsWslExecutor.verify_image` | 1209 | `return_none` | killed | killed |
| M036 | `WindowsWslExecutor.translate` | 1240 | `negate_if` | killed | killed |
| M037 | `WindowsWslExecutor.__call__` | 1277 | `negate_if` | killed | killed |
| M038 | `verified_executor` | 1341 | `negate_if` | killed | killed |
| M039 | `resolve_base` | 659 | `increment_number` | survived | killed |
| M040 | `_pack_file` | 511 | `swap_arithmetic` | killed | killed |
| M041 | `_PackRoot.contains` | 360 | `return_empty` | killed | killed |
| M042 | `_constant_time_equal` | 625 | `return_false` | killed | killed |

## New tests

- `IdentityOfTests.test_known_identity_is_kept_without_reexamining_the_path` kills M015: a known file identity must be reused without a second pathname lookup.
- `WriteOwnerOnlyTests.test_existing_regular_result_is_replaced_with_new_bytes` kills M027: an existing regular output is safely replaced.
- `ResolveBaseTests.test_a_base_one_character_over_the_limit_is_refused` kills M039: a 257-character base is invalid before Git is called.

Each test passed on the original module and failed when run alone against its named mutant. The focused after sweep killed all three. `python3 tests/test_suite.py` passed: 592 passed, 0 failed, 5 environmental skips.

Additional offline checks passed: `test_onboard.py` (28), `test_hostenv.py` (32, 11 skipped), `test_automatic_gate.py` (169), and `test_delegation_audit.py` (40). `test_local_worker.py` could not start its local HTTP fixture because the sandbox denied loopback socket binding (14 setup errors). `test_automatic_delegation_e2e.py` reported 42 errors in this sandbox; it also uses a local HTTP fixture. Neither result indicates a failure in the mutated module.

## Survivors left

None. Equivalent: none. Untestable offline: none. Defect: none.

## Defects found

None.
