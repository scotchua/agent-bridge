# Mutation results: `src/agent_bridge/execution/claude_config.py`

The runner sampled 30 single-edit mutants across all 12 functions. Each mutant was loaded from a temporary file into a fresh Python process; the production module was never written. Each focused test run had an 8-second limit. The original module SHA-256 at the end was `1b1382466a9b5922edf1ea182e387754a1c8815ea6e455a8419920daaf8d395c`.

| Stage | Killed | Generated | Kill rate |
| --- | ---: | ---: | ---: |
| Existing tests | 21 | 30 | 70.0% |
| After new tests | 30 | 30 | 100.0% |

Mutations covered comparison flips, `and`/`or` swaps, condition negation, a numeric increment, return replacement with `None`, removal of a call statement, and removal of exception raises. There is no executable `+` or `-` binary operator in this module to swap. Comments, logging, and docstrings were not mutated.

| ID | Function | Line | Mutation | Before | After |
| --- | --- | ---: | --- | --- | --- |
| M001 | `canonical_config_path` | 65 | return_none | killed | killed |
| M002 | `canonical_config_dir` | 72 | return_none | killed | killed |
| M003 | `shared_store_dir` | 79 | return_none | killed | killed |
| M004 | `_real` | 86 | return_none | killed | killed |
| M005 | `_is_reparse` | 92 | return_none | survived | killed |
| M006 | `assert_canonical` | 112 | comparison | killed | killed |
| M007 | `assert_canonical` | 129 | boolean | killed | killed |
| M008 | `assert_canonical` | 107 | negation | killed | killed |
| M009 | `assert_canonical` | 137 | return_none | killed | killed |
| M010 | `enforce_private` | 154 | comparison | killed | killed |
| M011 | `enforce_private` | 192 | boolean | killed | killed |
| M012 | `enforce_private` | 189 | constant | killed | killed |
| M013 | `enforce_private` | 200 | return_none | killed | killed |
| M014 | `_store_entries` | 205 | comparison | survived | killed |
| M015 | `_store_entries` | 209 | return_none | killed | killed |
| M016 | `_store_entries` | 206 | drop_raise | survived | killed |
| M017 | `_refuse_links` | 213 | boolean | killed | killed |
| M018 | `_refuse_links` | 214 | drop_raise | killed | killed |
| M019 | `_enforce_private_nt` | 266 | boolean | survived | killed |
| M020 | `_enforce_private_nt` | 254 | negation | survived | killed |
| M021 | `_enforce_private_nt` | 273 | return_none | survived | killed |
| M022 | `_enforce_private_nt` | 259 | drop_raise | survived | killed |
| M023 | `_refuse_nt` | 283 | comparison | survived | killed |
| M024 | `_refuse_nt` | 280 | drop_raise | survived | killed |
| M025 | `checked_config_dir` | 296 | return_none | killed | killed |
| M026 | `is_ready` | 316 | comparison | killed | killed |
| M027 | `is_ready` | 306 | boolean | killed | killed |
| M028 | `is_ready` | 331 | negation | killed | killed |
| M029 | `is_ready` | 329 | constant | killed | killed |
| M030 | `_enforce_private_nt` | 245 | remove_statement | killed | killed |

## New tests

Each named test passed on the original and failed when run alone against its listed mutant.

| Function | Test | Mutant |
| --- | --- | --- |
| `_is_reparse` | `ReparseTests.test_a_nonzero_reparse_tag_is_detected` | M005 |
| `_store_entries` | `StoreEntryTests.test_exactly_the_maximum_entries_are_allowed` | M014 |
| `_store_entries` | `StoreEntryTests.test_one_entry_over_the_maximum_is_refused` | M016 |
| `_enforce_private_nt` | `WindowsEnforcementWrapperTests.test_a_failed_child_acl_read_back_is_refused_as_content` | M019 |
| `_enforce_private_nt` | `WindowsEnforcementWrapperTests.test_a_failed_root_acl_is_refused` | M020 |
| `_enforce_private_nt` | `WindowsEnforcementWrapperTests.test_successful_acl_pass_returns_the_directory` | M021 |
| `_enforce_private_nt` | `WindowsEnforcementWrapperTests.test_a_failed_child_acl_is_refused` | M022 |
| `_refuse_nt` | `WindowsRefusalTests.test_reparse_point_has_its_own_refusal` | M023 |
| `_refuse_nt` | `WindowsRefusalTests.test_foreign_owner_has_its_own_refusal` | M024 |

## Survivors left

None. Equivalent: 0; untestable offline: 0; defects: 0.

## Defects found

None.

## Verification

- `python3 tests/test_claude_config_dir.py`: 45 tests passed, 3 skipped.
- `python3 tests/test_suite.py`: 592 checks passed, 0 failed, 5 environmental skips.
- `tests/test_onboard.py`, `tests/test_hostenv.py`, `tests/test_automatic_gate.py`, and `tests/test_delegation_audit.py` passed under a temporary home.
- `tests/test_local_worker.py` and `tests/test_automatic_delegation_e2e.py` could not start their local fake HTTP server because this sandbox denies loopback socket binding (`PermissionError: [Errno 1] Operation not permitted`).
