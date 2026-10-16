# Mutation sweep: `src/agent_bridge/orchestration/guest_runner.py`

## Method

- Original source SHA-256 before and after sweep: `b870ebb64907a94608963d11a434e080993e19a250850f7f323b7b902b12c903`. The runner checked byte equality at the end; each mutant lived in a temporary file and the production file was never written.
- A temporary standard-library AST runner generated 84 single-site mutants across all 83 functions (including methods and the nested response helper). It changed comparisons, boolean operators, conditions, numeric constants, returns, statements, arithmetic, and an exception raise. Comments, docstrings, and logging were excluded.
- Each mutant was loaded from a temporary file into an isolated Python subprocess in place of the imported module. Relevant existing offline unittest files ran with a 20-second per-mutant limit. New tests ran against each initial survivor. No package installation, API contact, or live Windows run occurred.
- A test timeout counts as a kill because the original tests completed while that mutant did not. Equivalent mutants are retained in the denominator.
- The after pass reran the 36 initial survivors with the new tests. The 48 mutants already killed by the same existing tests were carried forward.

## Results

| Phase | Killed | Survived | Kill rate |
| --- | ---: | ---: | ---: |
| Before new tests | 48 (46 failures, 2 timeouts) | 36 | 57.1% |
| After new tests | 82 | 2 equivalent | 97.6% |

## Mutants

| ID | Function | Line | Change | Before | After |
| --- | --- | ---: | --- | --- | --- |
| M001 | `GuestRunnerError.__init__` | 413 | remove_statement | survived | equivalent |
| M002 | `_read_bytes` | 432 | comparison | survived | killed |
| M003 | `sha256_file` | 442 | negate | survived | killed |
| M004 | `parse_mounts` | 457 | comparison | survived | killed |
| M005 | `host_filesystem_present` | 468 | comparison | killed | killed (existing tests) |
| M006 | `canary_host_mount` | 480 | negate | killed | killed (existing tests) |
| M007 | `canary_interop` | 494 | negate | killed | killed (existing tests) |
| M008 | `canary_wsl_conf` | 510 | numeric | survived | killed |
| M009 | `canary_guest_runner` | 521 | return_none | killed | killed (existing tests) |
| M010 | `load_versions` | 534 | comparison | killed | killed (existing tests) |
| M011 | `read_versions` | 559 | numeric | survived | killed |
| M012 | `canary_versions` | 575 | comparison | killed | killed (existing tests) |
| M013 | `egress_ruleset` | 610 | arithmetic | killed | killed (existing tests) |
| M014 | `apply_egress_policy` | 631 | comparison | killed | killed (existing tests) |
| M015 | `_egress_rules_present` | 643 | comparison | killed | killed (existing tests) |
| M016 | `verify_egress_ruleset` | 690 | arithmetic | killed | killed (existing tests) |
| M017 | `apply_verify_egress_policy` | 714 | comparison | survived | killed |
| M018 | `remove_verify_egress_policy` | 732 | numeric | survived | killed |
| M019 | `_verify_egress_rules_present` | 747 | comparison | killed | killed (existing tests) |
| M020 | `public_egress_reachable` | 769 | return_none | killed | killed (existing tests) |
| M021 | `enforce_verification_egress` | 788 | negate | killed | killed (existing tests) |
| M022 | `canary_egress` | 809 | negate | killed | killed (existing tests) |
| M023 | `default_gateway` | 831 | comparison | survived | killed |
| M024 | `probe_destination` | 867 | comparison | survived | killed |
| M025 | `probe_targets` | 898 | negate | killed | killed (existing tests) |
| M026 | `canary_egress_probe` | 918 | comparison | killed | killed (existing tests) |
| M027 | `validate_request` | 978 | comparison | killed | killed (existing tests) |
| M028 | `_validate_auth_probe_request` | 998 | comparison | survived | killed |
| M029 | `_validate_tool_request` | 1015 | comparison | killed | killed (existing tests) |
| M030 | `_validate_timeout` | 1081 | boolean | survived | killed |
| M031 | `_validate_workdir` | 1089 | boolean | survived | killed |
| M032 | `_validate_env` | 1100 | boolean | survived | killed |
| M033 | `validate_verify_argv` | 1121 | comparison | survived | killed |
| M034 | `_validate_provider_request` | 1149 | comparison | killed | killed (existing tests) |
| M035 | `validate_workspace` | 1203 | comparison | survived | killed |
| M036 | `validate_auth` | 1230 | comparison | killed | killed (existing tests) |
| M037 | `build_child_env` | 1252 | return_none | killed | killed (existing tests) |
| M038 | `build_response` | 1277 | comparison | survived | killed |
| M039 | `_git_env` | 1303 | return_none | survived | killed |
| M040 | `_job_identity` | 1319 | comparison | killed | killed (existing tests) |
| M041 | `_job_subprocess_kwargs` | 1327 | comparison | killed | killed (existing tests) |
| M042 | `_job_user_pids` | 1338 | comparison | killed | killed (existing tests) |
| M043 | `_sweep_job_user_processes` | 1366 | comparison | survived | killed |
| M044 | `_grant_workspace_to_job_user` | 1379 | comparison | killed | killed (existing tests) |
| M045 | `_git` | 1404 | arithmetic | killed | killed (existing tests) |
| M046 | `unpack_workspace` | 1442 | comparison | survived | killed |
| M047 | `_ExtractionBudget.__init__` | 1492 | numeric | survived | killed |
| M048 | `_ExtractionBudget.count_member` | 1501 | comparison | survived | killed |
| M049 | `_ExtractionBudget.claim_name` | 1506 | negate | killed | killed (existing tests) |
| M050 | `_ExtractionBudget.count_bytes` | 1512 | comparison | survived | killed |
| M051 | `_checked_member_name` | 1524 | comparison | survived | killed |
| M052 | `_resolve_within` | 1551 | comparison | killed | killed (existing tests) |
| M053 | `_make_workspace_dir` | 1558 | negate | killed | killed (existing tests) |
| M054 | `_write_workspace_file` | 1590 | comparison | survived | killed |
| M055 | `capture_diff` | 1621 | comparison | killed | killed (existing tests) |
| M056 | `_bounded_git` | 1657 | comparison | survived | killed |
| M057 | `_cap` | 1692 | comparison | survived | killed |
| M058 | `_AuthCapsule.__init__` | 1708 | remove_statement | killed | killed (existing tests) |
| M059 | `_AuthCapsule.__enter__` | 1729 | comparison | survived | killed |
| M060 | `_AuthCapsule.__exit__` | 1753 | comparison | survived | killed |
| M061 | `_AuthCapsule.child_env` | 1772 | comparison | killed | killed (existing tests) |
| M062 | `_AuthCapsule.prepare` | 1785 | comparison | survived | killed |
| M063 | `redact` | 1822 | negate | killed | killed (existing tests) |
| M064 | `verify_program_path` | 1840 | boolean | killed | killed (existing tests) |
| M065 | `provider_argv` | 1854 | comparison | killed | killed (existing tests) |
| M066 | `auth_probe_argv` | 1982 | comparison | killed | killed (existing tests) |
| M067 | `_claude_probe_verdict` | 2016 | comparison | killed | killed (existing tests) |
| M068 | `read_probe_message` | 2057 | comparison | survived | killed |
| M069 | `discard_probe_message` | 2078 | remove_statement | killed | killed (existing tests) |
| M070 | `_codex_probe_verdict` | 2098 | comparison | killed | killed (existing tests) |
| M071 | `_classify_failure` | 2114 | negate | killed | killed (existing tests) |
| M072 | `_execute_auth_probe` | 2160 | comparison | killed | killed (existing tests) |
| M073 | `_execute_auth_probe.done` | 2141 | comparison | killed | killed (existing tests) |
| M074 | `_execute_provider_job` | 2235 | comparison | killed | killed (existing tests) |
| M075 | `execute` | 2310 | comparison | killed | killed (existing tests) |
| M076 | `_BoundedReader.__init__` | 2410 | remove_statement | timeout | killed (timeout) |
| M077 | `_BoundedReader._pump` | 2423 | comparison | survived | equivalent |
| M078 | `_terminate_group` | 2446 | numeric | survived | killed |
| M079 | `_write_stdin` | 2456 | negate | timeout | killed (timeout) |
| M080 | `_subprocess_runner` | 2495 | comparison | survived | killed |
| M081 | `_untrusted_subprocess_runner` | 2550 | comparison | killed | killed (existing tests) |
| M082 | `read_request` | 2561 | comparison | survived | killed |
| M083 | `main` | 2582 | comparison | killed | killed (existing tests) |
| M084 | `_read_bytes` | 433 | drop_raise | survived | killed |

## New tests by mutated function

- `_AuthCapsule.__enter__`: `test_auth_capsule_enter_rejects_failed_mount`
- `_AuthCapsule.__exit__`: `test_auth_capsule_exit_rejects_failed_unmount`
- `_AuthCapsule.prepare`: `test_auth_capsule_prepare_rejects_failed_login`
- `_ExtractionBudget.__init__`: `test_extraction_budget_uses_one_for_zero_compressed_bytes`
- `_ExtractionBudget.count_bytes`: `test_extraction_budget_accepts_exact_content_limit`
- `_ExtractionBudget.count_member`: `test_extraction_budget_accepts_exact_member_limit`
- `_bounded_git`: `test_bounded_git_times_out_at_exact_deadline`
- `_cap`: `test_cap_preserves_exact_limit_without_truncation`
- `_checked_member_name`: `test_checked_member_name_accepts_exact_path_limit`
- `_git_env`: `test_git_env_is_a_separate_pinned_mapping`
- `_read_bytes`: `test_canary_wsl_conf_refuses_a_file_over_its_limit`, `test_read_bytes_accepts_the_limit_and_refuses_one_more`, `test_read_versions_refuses_a_valid_but_oversized_file`
- `_subprocess_runner`: `test_subprocess_runner_times_out_at_exact_deadline`
- `_sweep_job_user_processes`: `test_sweep_job_user_processes_refuses_exact_deadline`
- `_terminate_group`: `test_terminate_group_uses_kill_signal`
- `_validate_auth_probe_request`: `test_auth_probe_request_accepts_exact_keys`
- `_validate_env`: `test_validate_env_reports_bad_value_type`
- `_validate_timeout`: `test_validate_timeout_refuses_boolean`
- `_validate_workdir`: `test_validate_workdir_refuses_outside_prefix`
- `_write_workspace_file`: `test_unpack_workspace_accepts_file_at_member_limit`, `test_write_workspace_file_accepts_exact_file_limit`
- `apply_verify_egress_policy`: `test_apply_verify_egress_policy_rejects_nonzero_exit`
- `build_response`: `test_build_response_defaults_completed_harness_to_complete`
- `canary_wsl_conf`: `test_canary_wsl_conf_refuses_a_file_over_its_limit`
- `default_gateway`: `test_default_gateway_accepts_minimal_route_fields`
- `parse_mounts`: `test_parse_mounts_accepts_three_fields`
- `probe_destination`: `test_probe_destination_treats_refusal_as_reachable`
- `read_probe_message`: `test_read_probe_message_accepts_exact_size_limit`
- `read_request`: `test_read_request_accepts_exact_byte_limit`
- `read_versions`: `test_read_versions_refuses_a_valid_but_oversized_file`
- `remove_verify_egress_policy`: `test_remove_verify_egress_policy_uses_bounded_timeout`
- `sha256_file`: `test_sha256_file_reads_nonempty_content`
- `unpack_workspace`: `test_unpack_workspace_accepts_file_at_member_limit`
- `validate_verify_argv`: `test_validate_verify_argv_accepts_exact_command_limit`
- `validate_workspace`: `test_validate_workspace_accepts_exact_encoded_limit`

## Survivors left

- **M001, `GuestRunnerError.__init__` (equivalent):** dropping `super().__init__(code)` leaves `Exception.__new__`'s existing `args` and the explicit `code` assignment intact for the declared string input. The error message and code are unchanged.
- **M077, `_BoundedReader._pump` (equivalent):** when `room == 0`, the previous `if not chunk: return` establishes a nonempty chunk, so the following `len(chunk) > room` branch sets `overflowed` and returns exactly as the removed `room <= 0` branch did.

## Defects found

None. No new expected failures were needed.

## Verification

- `python3 -m unittest discover -s tests -p test_guest_runner_mutations.py -q` passed (33 tests).
- `python3 tests/test_suite.py` passed (593 passed, 0 failed, 5 environmental skips).
- `test_onboard.py`, `test_hostenv.py`, `test_automatic_gate.py`, and `test_delegation_audit.py` passed. `test_local_worker.py` and `test_automatic_delegation_e2e.py` could not bind their local fake HTTP servers in this sandbox (`PermissionError: Operation not permitted`); no external API was contacted.
- The before and after statuses, including each new test failure, were captured in temporary runner output and summarized above. The throwaway runner and intermediate JSON were deleted after the report was written.
