# Mutation sweep: `windows_evidence.py`

## Method

- Generated 59 single-change AST mutants from the production source in memory. The selection covers every function and includes comparison, boolean, negation, numeric, arithmetic, return, statement removal, and dropped-raise mutations. AST selection excludes comments and docstrings; the module has no logging calls. No package installation or external service was used.
- A temporary import hook loaded each mutant into a separate Python process while leaving the source file untouched. Each run had a 5-second timeout. The before run used the original `tests/test_windows_evidence.py` from `HEAD`; the after run used the expanded test file.
- The source remained byte-identical: Git blob `e049a754c94f0b230ba5d69c5529523e216e48b6` before and after. The temporary runner and baseline copy were removed after the sweep.

## Kill rates

| Stage | Killed | Survived | Kill rate |
| --- | ---: | ---: | ---: |
| Existing tests | 42/59 | 17 | 71.2% |
| After new tests | 56/59 | 3 | 94.9% |

## Mutants generated

`K` means killed and `S` means survived. A killed mutant caused at least one test failure. No run timed out or failed to import the test harness.

| ID | Function:line | Mutation | Before | After |
| --- | --- | --- | :---: | :---: |
| M001 | `evidence_path:76` | return_none | S | K |
| M002 | `host_fingerprint:99` | boolean | K | K |
| M003 | `_read_machine_guid:104` | comparison | S | K |
| M004 | `observed:145` | return_none | K | K |
| M005 | `enabled_for:149` | return_none | K | K |
| M006 | `as_dict:152` | return_none | K | K |
| M007 | `_require:185` | negation | K | K |
| M008 | `parse_evidence:192` | remove_statement | K | K |
| M009 | `check_evidence:259` | remove_statement | K | K |
| M010 | `_as_evidence_error:299` | return_none | K | K |
| M011 | `read_evidence:317` | remove_statement | K | K |
| M012 | `read_evidence_with_identity:344` | remove_statement | S | K |
| M013 | `load_verified_state:374` | remove_statement | K | K |
| M014 | `record_verification:413` | comparison | K | K |
| M015 | `host_fingerprint:99` | boolean | S | K |
| M016 | `_read_machine_guid:105` | return_none | S | S |
| M017 | `observed:145` | boolean | K | K |
| M018 | `enabled_for:149` | boolean | K | K |
| M019 | `as_dict:170` | return_none | K | K |
| M020 | `_require:186` | drop_raise | K | K |
| M021 | `parse_evidence:193` | remove_statement | K | K |
| M022 | `check_evidence:259` | comparison | K | K |
| M023 | `read_evidence:321` | drop_raise | K | K |
| M024 | `read_evidence_with_identity:348` | drop_raise | S | K |
| M025 | `load_verified_state:379` | return_none | K | K |
| M026 | `record_verification:414` | drop_raise | K | K |
| M027 | `host_fingerprint:100` | return_none | K | K |
| M028 | `_read_machine_guid:109` | return_none | S | S |
| M029 | `parse_evidence:193` | comparison | K | K |
| M030 | `check_evidence:260` | remove_statement | K | K |
| M031 | `read_evidence:325` | drop_raise | K | K |
| M032 | `read_evidence_with_identity:352` | drop_raise | S | K |
| M033 | `load_verified_state:379` | comparison | S | K |
| M034 | `record_verification:417` | remove_statement | K | K |
| M035 | `_read_machine_guid:112` | number | S | K |
| M036 | `parse_evidence:194` | remove_statement | K | K |
| M037 | `check_evidence:260` | comparison | K | K |
| M038 | `read_evidence:326` | return_none | K | K |
| M039 | `read_evidence_with_identity:353` | return_none | S | K |
| M040 | `load_verified_state:388` | boolean | K | K |
| M041 | `record_verification:417` | number | S | K |
| M042 | `_read_machine_guid:116` | return_none | S | S |
| M043 | `parse_evidence:194` | comparison | K | K |
| M044 | `check_evidence:262` | remove_statement | K | K |
| M045 | `load_verified_state:389` | return_none | K | K |
| M046 | `record_verification:419` | drop_raise | S | K |
| M047 | `_read_machine_guid:117` | return_none | S | K |
| M048 | `parse_evidence:195` | remove_statement | K | K |
| M049 | `check_evidence:262` | comparison | K | K |
| M050 | `record_verification:421` | arithmetic | K | K |
| M051 | `parse_evidence:195` | comparison | K | K |
| M052 | `check_evidence:265` | remove_statement | K | K |
| M053 | `record_verification:421` | number | S | K |
| M054 | `parse_evidence:198` | remove_statement | K | K |
| M055 | `check_evidence:265` | negation | K | K |
| M056 | `record_verification:423` | remove_statement | S | K |
| M057 | `observed:145` | return_false | K | K |
| M058 | `as_dict:152` | return_empty | K | K |
| M059 | `_as_evidence_error:299` | return_false | K | K |

## New tests

Each named test passed against the original source and failed against at least one listed mutant.

| Test in `tests/test_windows_evidence.py` | Mutant killed |
| --- | --- |
| `test_path_uses_the_runtime_root` | M001 |
| `test_windows_guid_is_read_with_the_registry_default_view` | M003, M035, M047 |
| `test_private_directory_is_required_before_reading` | M012, M024 |
| `test_node_name_changes_the_fingerprint` | M015 |
| `test_file_privacy_error_is_translated` | M024 |
| `test_malformed_bytes_have_a_fixed_error` | M032 |
| `test_failed_load_keeps_the_running_platform` | M033 |
| `test_valid_record_returns_the_read_identity` | M039 |
| `test_new_directory_requests_owner_only_mode` | M041 |
| `test_directory_creation_failure_has_a_fixed_reason` | M046 |
| `test_written_record_uses_canonical_indentation` | M053 |
| `test_directory_privacy_refusal_prevents_a_write` | M056 |

## Survivors left

- M016 (`_read_machine_guid:105`), M028 (`_read_machine_guid:109`), and M042 (`_read_machine_guid:116`) are equivalent: each replaces an existing `return None` with `return None`. No behavioral survivors remain.
- Untestable offline: none.

## Defects found

- None.

## Verification

- `python3 tests/test_windows_evidence.py`: 63 tests passed.
- `python3 tests/test_suite.py`: 593 passed, 0 failed, 5 environmental skips.
- `python3 tests/test_onboard.py`: 28 passed.
- `python3 tests/test_hostenv.py`: 32 run, 11 skipped.
- `python3 tests/test_automatic_gate.py`: 169 passed.
- `python3 tests/test_delegation_audit.py`: 40 passed.
- `tests/test_local_worker.py` and `tests/test_automatic_delegation_e2e.py` could not bind their loopback test servers in this sandbox (`PermissionError: Operation not permitted`). No external API was contacted.
- The Windows registry branch was exercised with a fake `winreg` module. This does not establish live Windows host verification.
