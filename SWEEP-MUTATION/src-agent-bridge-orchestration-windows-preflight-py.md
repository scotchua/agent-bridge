# Mutation hardening: `windows_preflight.py`

## Method and result

The temporary runner parsed `src/agent_bridge/orchestration/windows_preflight.py` into an AST, changed one node at a time, compiled the changed copy in memory, and ran `tests/test_windows_preflight.py` in a fresh Python process with a five-second limit. It never wrote a mutant to the source file. Comments and docstrings were excluded by the AST selection, and the module has no logging statements to mutate. No packages, live Windows host, API, or network service were used. The test module mocks all preflight subprocess calls.

| Stage | Killed | Generated | Kill rate |
| --- | ---: | ---: | ---: |
| Existing 51 tests | 130 | 148 | 87.84% |
| Existing and 15 new tests | 148 | 148 | 100% |

The 148 mutants cover every function and three module-level numeric literals. They comprise 47 return-to-`None`, 32 condition negations, 32 single-statement removals, 16 numeric increments, 13 `and`/`or` swaps, 6 comparison flips, 1 `+`/`-` swap, and 1 removed raise. The comparison flips use `<`/`<=`, `>`/`>=`, and `==`/`!=` pairs where present in the selected nodes. The cap was 150. All mutants compiled; none timed out.

## Baseline survivors and tests that killed them

| ID | Function:line | Mutation | New test |
| ---: | --- | --- | --- |
| 1 | module:40 | Timeout constant `10` to `11` | `test_timeout_is_ten_seconds` |
| 14 | module:41 | Output multiplier `64` to `65` | `test_output_limit_is_64_kib` |
| 26 | module:41 | Output multiplier `1024` to `1025` | `test_output_limit_is_64_kib` |
| 31 | `feature_state_argv`:199 | `or` to `and` | `test_non_string_name_raises_value_error` |
| 41 | `feature_state_argv`:200 | Drop invalid-name raise | `test_empty_name_raises_value_error` |
| 58 | `collect_optional_feature`:214 | Return `None` for invalid name | `test_invalid_name_returns_failed_check` |
| 61 | `collect_manifest_check`:338 | `or` to `and` | `test_empty_path_fails_before_loader` |
| 88 | `_run_argv`:127 | `>` to `>=` at output limit | `test_output_at_limit_is_accepted` |
| 107 | `_run_argv`:139 | Return `None` for non-byte output | `test_non_bytes_output_fails_with_one_argument` |
| 117 | `collect_firmware_virtualization`:311 | Return `None` for missing colon | `test_firmware_line_without_colon_fails` |
| 118 | `collect_manifest_check`:359 | `or` to `and` in hash validation | `test_non_string_expected_hash_fails_validation` |
| 120 | `_collect_nested_virtualization`:254 | Split on two equals signs | `test_nested_model_with_equals_sign_is_parsed` |
| 130 | `_run_argv`:127 | Subtract stderr length | `test_stderr_counts_toward_output_limit` |
| 135 | `collect_firmware_virtualization`:314 | Split on two colons | `test_firmware_value_with_extra_colon_fails` |
| 138 | `_run_argv`:144 | Remove UTF-16LE decode | `test_utf16_le_without_bom_is_decoded` |
| 141 | `_run_argv`:127 | Ignore nonempty stderr length | `test_stderr_counts_toward_output_limit` |
| 144 | `_run_argv`:139 | Use `argv[1]` in failure detail | `test_non_bytes_output_fails_with_one_argument` |
| 146 | `_run_argv`:122 | Use `argv[1]` in timeout detail | `test_timeout_with_one_argument_is_explicit_failure` |

Each of the 15 new tests passed against the original source and failed against at least one baseline survivor, as recorded by the second mutation pass. The tests are grouped by function in `tests/test_windows_preflight.py`.

## Survivors left

None. Equivalent mutants: none. Untestable-offline mutants: none.

## Defects found

None. No expected-failure tests were needed.

## Verification

`python3 tests/test_windows_preflight.py`: 66 passed. The five existing `WindowsPreflightWiringTests` in `tests/test_automatic_delegation.py` also passed. `python3 tests/test_suite.py`: 592 passed, 0 failed, 5 platform skips. `tests/test_onboard.py`, `tests/test_hostenv.py`, `tests/test_automatic_gate.py`, and `tests/test_delegation_audit.py` passed. The broader suites ran with isolated temporary homes.

`tests/test_local_worker.py` and `tests/test_automatic_delegation_e2e.py` could not run their local-server cases because this sandbox denies binding to `127.0.0.1` (`PermissionError: [Errno 1] Operation not permitted`). They were attempted offline; no external service was contacted. This limits verification of those suites, not the mutation result or the full `tests/test_suite.py` result.

The source SHA-256 after mutation was `a1ba42c9bac1fcbb1dbb3a46c13d57e4bb928a145393a4ee48f553eb3e2f92fc`; `git diff` showed no production-file changes.
