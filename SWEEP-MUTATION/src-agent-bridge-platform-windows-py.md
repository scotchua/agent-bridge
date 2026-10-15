# Windows platform mutation sweep

## Method

- Source: `src/agent_bridge/platform/windows.py`; SHA-256: `215ce398dcb714f6642f808545aac4bf216f39ac0d8999782831051e74a7ad16`.
- The throwaway runner selected 52 syntax-valid, one-at-a-time source mutants across every `WindowsPlatform` method, including nested stream reader and writer functions. It skipped comments, logging, and docstrings.
- Each test subprocess used a temporary `sitecustomize.py` overlay for reads of `windows.py`. The tracked production file was never written and its bytes were checked after each sweep.
- Each mutant run had a 15-second timeout. The before run used `test_windows_acl_security` and `test_windows_stream_security`; the after run added `test_windows_platform_mutations`. All three modules use offline scripted behavior on this host.

## Kill rates

| Run | Killed | Generated | Kill rate |
| --- | ---: | ---: | ---: |
| Before new tests | 8 | 52 | 15.4% |
| After new tests | 52 | 52 | 100% |

## Mutants

| ID | Function | Line | Change | Before | After |
| --- | --- | ---: | --- | --- | --- |
| M001 | `__init__` | 306 | boolean constant | survived | killed |
| M002 | `set_owner_only_umask` | 313 | numeric constant +1 | survived | killed |
| M003 | `enforce_owner_only_file` | 327 | negated condition | killed | killed |
| M004 | `verify_owner_only_path` | 357 | boolean operator | survived | killed |
| M005 | `lock_exclusive` | 367 | comparison | survived | killed |
| M006 | `spawn_isolated` | 428 | negated condition | survived | killed |
| M007 | `isolated_process_group` | 448 | return None | survived | killed |
| M008 | `terminate_process_tree` | 485 | boolean operator | survived | killed |
| M009 | `process_alive` | 510 | comparison | killed | killed |
| M010 | `process_liveness` | 530 | comparison | killed | killed |
| M011 | `process_tree_alive` | 551 | negated condition | survived | killed |
| M012 | `process_group_members` | 589 | comparison | survived | killed |
| M013 | `process_identity` | 597 | return None | survived | killed |
| M014 | `process_group_identity` | 601 | negated condition | survived | killed |
| M015 | `is_own_process_group` | 607 | comparison | survived | killed |
| M016 | `read_streams_with_caps` | 683 | comparison | survived | killed |
| M017 | `read_streams_with_caps.reader` | 628 | comparison | survived | killed |
| M018 | `read_streams_with_caps.writer` | 651 | negated condition | survived | killed |
| M019 | `_create_job` | 749 | negated condition | survived | killed |
| M020 | `_job_name` | 762 | return None | survived | killed |
| M021 | `_job_handle` | 773 | comparison | survived | killed |
| M022 | `_close_job` | 784 | negated condition | survived | killed |
| M023 | `_process_creation_time` | 789 | negated condition | survived | killed |
| M024 | `_path_from_fd` | 807 | negated condition | survived | killed |
| M025 | `_acl_round_trip_supported` | 828 | boolean operator | survived | killed |
| M026 | `_token_sid` | 876 | comparison | survived | killed |
| M027 | `_caller_sid` | 897 | negated condition | survived | killed |
| M028 | `_default_owner_sid` | 912 | negated condition | survived | killed |
| M029 | `_examine_handle` | 923 | negated condition | survived | killed |
| M030 | `_open_securable` | 946 | negated condition | survived | killed |
| M031 | `_open_child` | 987 | comparison | survived | killed |
| M032 | `_reopen_descriptor` | 1008 | negated condition | survived | killed |
| M033 | `_list_directory` | 1022 | comparison | survived | killed |
| M034 | `_sddl` | 1037 | negated condition | survived | killed |
| M035 | `_read_security` | 1054 | comparison | survived | killed |
| M036 | `_write_owner_only` | 1083 | comparison | survived | killed |
| M037 | `_protect_handle` | 1104 | comparison | killed | killed |
| M038 | `_protect_path` | 1134 | negated condition | survived | killed |
| M039 | `_protect_descriptor` | 1145 | negated condition | survived | killed |
| M040 | `_set_and_verify_owner_acl` | 1155 | numeric constant +1 | survived | killed |
| M041 | `_observe_handle` | 1165 | negated condition | survived | killed |
| M042 | `observe_owner_only_acl` | 1200 | negated condition | survived | killed |
| M043 | `acl_diagnostics` | 1215 | return None | survived | killed |
| M044 | `_walk_tree` | 1235 | comparison | survived | killed |
| M045 | `_open_tree` | 1264 | negated condition | killed | killed |
| M046 | `observe_owner_only_tree` | 1304 | comparison | survived | killed |
| M047 | `enforce_owner_only_tree` | 1367 | comparison | killed | killed |
| M048 | `enforce_owner_only_file` | 335 | removed statement | survived | killed |
| M049 | `enforce_owner_only_file` | 333 | dropped raise | killed | killed |
| M050 | `read_streams_with_caps` | 676 | arithmetic swap | killed | killed |
| M051 | `is_own_process_group` | 607 | return False | survived | killed |
| M052 | `_job_name` | 762 | return empty | survived | killed |

## New tests

Each named test in `tests/test_windows_platform_mutations.py` passed on the original source and failed on the listed mutant. The table groups them by mutated function.

| Function | Test | Mutant killed |
| --- | --- | --- |
| `acl_diagnostics` | `test_acl_diagnostics_returns_evidence_with_verdict_and_path` | M043 |
| `_acl_round_trip_supported` | `test_acl_round_trip_requires_both_probe_objects` | M025 |
| `_caller_sid` | `test_caller_sid_loads_an_empty_cache` | M027 |
| `_close_job` | `test_close_job_closes_registered_handle` | M022 |
| `_create_job` | `test_create_job_rejects_zero_handle` | M019 |
| `_default_owner_sid` | `test_default_owner_sid_loads_an_empty_cache` | M028 |
| `lock_exclusive` | `test_empty_lock_file_gets_a_byte_before_locking` | M005 |
| `enforce_owner_only_file` | `test_enforce_owner_only_file_sets_capability_after_success` | M048 |
| `read_streams_with_caps` | `test_exact_deadline_is_timeout` | M016 |
| `_examine_handle` | `test_examine_handle_closes_a_failed_query` | M029 |
| `__init__` | `test_initial_capability_is_unknown_until_verified` | M001 |
| `is_own_process_group` | `test_is_own_process_group_matches_current_pid` | M015 |
| `isolated_process_group` | `test_isolated_process_group_reports_only_registered_pid` | M007 |
| `_job_handle` | `test_job_handle_accepts_only_an_existing_named_job` | M021 |
| `_job_name` | `test_job_name_contains_pid_and_identity` | M020 |
| `_list_directory` | `test_list_directory_finishes_on_no_more_files` | M033 |
| `observe_owner_only_acl` | `test_observe_acl_reports_open_error_without_reading` | M042 |
| `_observe_handle` | `test_observe_handle_rejects_unknown_caller` | M041 |
| `observe_owner_only_tree` | `test_observe_tree_judges_root_strictly` | M046 |
| `_open_child` | `test_open_child_accepts_successful_native_status` | M031 |
| `_open_securable` | `test_open_securable_rejects_invalid_handle` | M030 |
| `set_owner_only_umask` | `test_owner_only_umask_uses_077` | M002 |
| `_path_from_fd` | `test_path_from_fd_rejects_failed_length_query` | M024 |
| `_process_creation_time` | `test_process_creation_time_returns_empty_on_failed_open` | M023 |
| `process_group_identity` | `test_process_group_identity_requires_creation_time` | M014 |
| `process_group_members` | `test_process_group_members_stops_on_non_resize_error` | M012 |
| `process_identity` | `test_process_identity_returns_creation_time` | M013 |
| `process_tree_alive` | `test_process_tree_alive_uses_members_when_query_succeeds` | M011 |
| `_protect_descriptor` | `test_protect_descriptor_reports_reopen_error_without_protecting` | M039 |
| `_protect_path` | `test_protect_path_reports_open_error_without_protecting` | M038 |
| `_read_security` | `test_read_security_retries_after_size_query` | M035 |
| `_reopen_descriptor` | `test_reopen_descriptor_rejects_invalid_handle` | M032 |
| `_sddl` | `test_sddl_returns_text_after_successful_conversion` | M034 |
| `_set_and_verify_owner_acl` | `test_set_and_verify_owner_acl_returns_protection_result` | M040 |
| `spawn_isolated` | `test_spawn_isolated_rejects_missing_process_identity` | M006 |
| `terminate_process_tree` | `test_terminate_process_tree_does_not_terminate_missing_job` | M008 |
| `_token_sid` | `test_token_sid_continues_after_expected_size_error` | M026 |
| `verify_owner_only_path` | `test_verify_owner_only_path_requires_both_objects` | M004 |
| `_walk_tree` | `test_walk_tree_refuses_depth_limit` | M044 |
| `_write_owner_only` | `test_write_owner_only_accepts_success_status` | M036 |
| `read_streams_with_caps.writer` | `test_writer_sends_stdin_when_present` | M018 |
| `read_streams_with_caps.reader` | `test_zero_stdout_cap_is_exceeded_before_read` | M017 |

## Survivors left

None. No equivalent mutants, offline untestable survivors, or surviving defects were identified in this selected set.

## Defects found

None.

## Verification

- `python3 tests/test_suite.py`: 592 passed, 0 failed, 5 environment skips.
- `python3 -m unittest -q test_onboard test_windows_acl_security test_windows_stream_security test_windows_platform_mutations` with `tests:src` on `PYTHONPATH` and an isolated temporary home: 157 run, 0 failures, 6 skips.
- `test_local_worker` was exercised but 14 tests could not start their local `127.0.0.1` fixture because this sandbox refused socket binding (`PermissionError: [Errno 1] Operation not permitted`). This is an environment limit, unrelated to `windows.py`; no remote endpoint was contacted.

## Limits

- These results establish scripted offline behavior, not live Windows kernel behavior.
- The 52 selected mutants sample the requested mutation forms and cover each function; they are not an exhaustive mutation set.
