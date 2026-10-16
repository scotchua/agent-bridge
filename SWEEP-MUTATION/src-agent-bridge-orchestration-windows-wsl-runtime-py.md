# Mutation sweep: `windows_wsl_runtime.py`

## Method and results

- Source: `src/agent_bridge/orchestration/windows_wsl_runtime.py` (SHA-256 `f48786a51afec55a8522c74c921b0a3ebec571fe9889db3b161ed0a8f4868a6f`).
- Generated 78 single-change AST mutants, covering all 74 functions and methods; `HostOps.open_exclusive_write` has a second mutant to distinguish a behaviorally equivalent flag change. The cap was 150. Operators used: comparison flips, `and`/`or` swaps, condition negation, numeric increment, `+`/`-` swaps, return replacement with `None`, empty or `False`, single-statement removal, and exception-raise deletion. No candidate came from comments, logging, or docstrings.
- Each mutant was compiled and tested from a temporary module copy injected under the original module name. The production file was never written. Each subprocess had an 8-second timeout; none timed out.
- Before: **44/78 killed (56.41%)** by the 152 existing dedicated runtime tests.
- After: **77/78 killed (98.72%)** by the 152 existing plus 32 new runtime tests. Among behavior-changing mutants, **77/77 killed (100%)**.
- Isolated confirmation: each of the 32 new tests failed on its assigned behavior-changing mutant and passed on the original. The 32 tests killed 33 assigned mutants in isolation; the sole isolated survivor is equivalent mutant M019.

## Mutants

| ID | Function or method | Line | Change | Before | After | New test, if needed |
| --- | --- | ---: | --- | --- | --- | --- |
| M001 | `_token` | 250 | `boolean` | killed | killed | Existing tests |
| M002 | `sanitize_detail` | 265 | `compare` | killed | killed | Existing tests |
| M003 | `classify_liveness` | 314 | `compare` | killed | killed | Existing tests |
| M004 | `WindowsWslRuntimeError.__init__` | 345 | `remove_statement` | survived | killed | `RuntimeValueMutationTests.test_runtime_error_preserves_safe_message` |
| M005 | `_positive_int` | 373 | `compare` | killed | killed | Existing tests |
| M006 | `_positive_seconds` | 386 | `compare` | killed | killed | Existing tests |
| M007 | `validate_limits` | 396 | `negate` | killed | killed | Existing tests |
| M008 | `CleanupReport.complete` | 471 | `return_none` | killed | killed | Existing tests |
| M009 | `CleanupReport.as_dict` | 478 | `return_none` | killed | killed | Existing tests |
| M010 | `RuntimeResult.ok` | 510 | `compare` | survived | killed | `RuntimeValueMutationTests.test_runtime_result_ok_tracks_status` |
| M011 | `HostOps.monotonic` | 532 | `return_none` | survived | killed | `HostOpsMutationTests.test_monotonic` |
| M012 | `HostOps.now_utc` | 535 | `return_none` | survived | killed | `HostOpsMutationTests.test_now_utc` |
| M013 | `HostOps.lock` | 542 | `remove_statement` | survived | killed | `HostOpsMutationTests.test_lock` |
| M014 | `HostOps.lstat` | 547 | `return_none` | survived | killed | `HostOpsMutationTests.test_lstat` |
| M015 | `HostOps.listdir` | 550 | `return_none` | survived | killed | `HostOpsMutationTests.test_listdir` |
| M016 | `HostOps.secure_mkdir` | 555 | `remove_statement` | survived | killed | `HostOpsMutationTests.test_secure_mkdir` |
| M017 | `HostOps.verify_owner_only` | 558 | `return_none` | survived | killed | `HostOpsMutationTests.test_verify_owner_only` |
| M018 | `HostOps.open_read` | 561 | `numeric` | survived | killed | `HostOpsMutationTests.test_open_read` |
| M019 | `HostOps.open_exclusive_write` | 566 | `numeric` | survived | survived | Equivalent; no new test |
| M020 | `HostOps.open_exclusive_write` | 564 | `return_none` | survived | killed | `HostOpsMutationTests.test_open_exclusive_write` |
| M021 | `HostOps.fstat` | 571 | `return_none` | survived | killed | `HostOpsMutationTests.test_fstat` |
| M022 | `HostOps.read` | 574 | `return_none` | survived | killed | `HostOpsMutationTests.test_read` |
| M023 | `HostOps.write` | 577 | `return_none` | survived | killed | `HostOpsMutationTests.test_write` |
| M024 | `HostOps.fsync` | 580 | `remove_statement` | survived | killed | `HostOpsMutationTests.test_fsync` |
| M025 | `HostOps.close` | 583 | `remove_statement` | survived | killed | `HostOpsMutationTests.test_close` |
| M026 | `HostOps.unlink` | 586 | `remove_statement` | survived | killed | `HostOpsMutationTests.test_unlink` |
| M027 | `HostOps.remove_tree` | 591 | `remove_statement` | survived | killed | `HostOpsMutationTests.test_remove_tree` |
| M028 | `HostOps.exists` | 594 | `return_none` | survived | killed | `HostOpsMutationTests.test_exists` |
| M029 | `HostOps.write_json` | 599 | `remove_statement` | survived | killed | `HostOpsMutationTests.test_write_json` |
| M030 | `HostOps.read_json` | 602 | `return_none` | survived | killed | `HostOpsMutationTests.test_read_json` |
| M031 | `HostOps.run_host` | 617 | `return_none` | survived | killed | `HostOpsMutationTests.test_run_host` |
| M032 | `HostOps.host_cwd` | 634 | `boolean` | survived | killed | `HostOpsMutationTests.test_host_cwd` |
| M033 | `HostOps.process_liveness` | 643 | `return_none` | survived | killed | `HostOpsMutationTests.test_process_liveness` |
| M034 | `HostOps.process_identity` | 646 | `return_none` | survived | killed | `HostOpsMutationTests.test_process_identity` |
| M035 | `HostOps.current_pid` | 649 | `return_none` | survived | killed | `HostOpsMutationTests.test_current_pid` |
| M036 | `_decode_wsl_listing` | 676 | `negate` | killed | killed | Existing tests |
| M037 | `registration_listing` | 711 | `negate` | killed | killed | Existing tests |
| M038 | `registration_after_failed_import` | 731 | `compare` | killed | killed | Existing tests |
| M039 | `command_failure` | 758 | `compare` | killed | killed | Existing tests |
| M040 | `_validate_local_windows_path` | 771 | `negate` | killed | killed | Existing tests |
| M041 | `_ancestors` | 787 | `arithmetic` | killed | killed | Existing tests |
| M042 | `_is_reparse_point` | 797 | `negate` | killed | killed | Existing tests |
| M043 | `_reject_reparse_ancestors` | 826 | `negate` | killed | killed | Existing tests |
| M044 | `_reject_existing` | 837 | `negate` | killed | killed | Existing tests |
| M045 | `_require_regular_file` | 850 | `negate` | killed | killed | Existing tests |
| M046 | `_identity_of` | 860 | `return_none` | killed | killed | Existing tests |
| M047 | `_directory_identity` | 872 | `negate` | killed | killed | Existing tests |
| M048 | `_verify_private_directory` | 913 | `negate` | killed | killed | Existing tests |
| M049 | `_prepare_private_tree` | 931 | `remove_statement` | killed | killed | Existing tests |
| M050 | `jobs_root` | 940 | `return_none` | killed | killed | Existing tests |
| M051 | `owner_records_root` | 944 | `return_none` | killed | killed | Existing tests |
| M052 | `receipts_root` | 948 | `return_none` | killed | killed | Existing tests |
| M053 | `lock_path` | 954 | `return_none` | survived | killed | `RuntimeValueMutationTests.test_lock_path_names_lock_file` |
| M054 | `job_directory` | 958 | `return_none` | killed | killed | Existing tests |
| M055 | `owner_record_path` | 962 | `arithmetic` | killed | killed | Existing tests |
| M056 | `receipt_path` | 966 | `arithmetic` | killed | killed | Existing tests |
| M057 | `_copy_rootfs` | 991 | `compare` | survived | killed | `RuntimeValueMutationTests.test_copy_rootfs_accepts_exact_size_cap` |
| M058 | `_revalidate_before_import` | 1079 | `compare` | killed | killed | Existing tests |
| M059 | `canary_expectations` | 1099 | `return_none` | killed | killed | Existing tests |
| M060 | `canary_job_spec` | 1123 | `return_none` | killed | killed | Existing tests |
| M061 | `_matches_exactly` | 1135 | `arithmetic` | killed | killed | Existing tests |
| M062 | `canary_timing_key` | 1142 | `arithmetic` | killed | killed | Existing tests |
| M063 | `_run_canaries` | 1182 | `negate` | survived | killed | `RuntimeValueMutationTests.test_run_canaries_records_each_timing` |
| M064 | `build_owner_record` | 1222 | `return_none` | killed | killed | Existing tests |
| M065 | `parse_owner_record` | 1240 | `compare` | killed | killed | Existing tests |
| M066 | `verify_registration_binding` | 1332 | `compare` | killed | killed | Existing tests |
| M067 | `_cleanup` | 1404 | `compare` | killed | killed | Existing tests |
| M068 | `build_receipt` | 1534 | `numeric` | survived | killed | `RuntimeValueMutationTests.test_build_receipt_rounds_timings_to_six_places` |
| M069 | `run_job` | 1558 | `boolean` | killed | killed | Existing tests |
| M070 | `_run_job_on_windows` | 1611 | `compare` | survived | killed | `RuntimeValueMutationTests.test_run_job_accepts_input_at_exact_byte_cap` |
| M071 | `ReapReport.reaped` | 1884 | `compare` | killed | killed | Existing tests |
| M072 | `reap_stale_instances` | 1905 | `boolean` | killed | killed | Existing tests |
| M073 | `_reap_on_windows` | 1934 | `compare` | survived | killed | `RuntimeValueMutationTests.test_reap_rejects_zero_max_age` |
| M074 | `owner_liveness` | 2066 | `compare` | killed | killed | Existing tests |
| M075 | `_reap_one` | 2116 | `compare` | killed | killed | Existing tests |
| M076 | `_positive_int` | 372 | `drop_raise` | killed | killed | Existing tests |
| M077 | `RuntimeResult.ok` | 510 | `return_false` | survived | killed | `RuntimeValueMutationTests.test_runtime_result_ok_tracks_status` |
| M078 | `_ancestors` | 792 | `return_empty` | killed | killed | Existing tests |

## Survivors left

- **M019 — equivalent:** The fallback in `HostOps.open_exclusive_write` changes `getattr(os, "O_BINARY", 0)` to `getattr(os, "O_BINARY", 1)`. On Windows, `O_BINARY` exists and the fallback is unused. Where it is absent, `O_WRONLY` already sets bit 1, so the combined open flags are identical. No behavior-changing survivor remains.
- Untestable offline: none in this sampled set.
- Defect: none in this sampled set.

## Defects found

None in this sampled set.

## Verification

- `python3 -m unittest discover -s tests -p "test_windows_wsl_runtime*.py" -q`: 184 passed.
- `python3 tests/test_suite.py`: 593 passed, 0 failed, 5 environmental skips.
- `python3 tests/test_onboard.py -q`: 28 passed.
- `python3 tests/test_hostenv.py -q`: 32 run, 11 skipped.
- `python3 tests/test_automatic_gate.py -q`: 169 passed.
- `python3 tests/test_delegation_audit.py -q`: 40 passed.
- `tests/test_local_worker.py` and `tests/test_automatic_delegation_e2e.py` could not start their local HTTP fixtures because the sandbox denied binding `127.0.0.1` (`PermissionError: [Errno 1] Operation not permitted`).
- `git diff --exit-code -- src/agent_bridge/orchestration/windows_wsl_runtime.py` passed; the source SHA-256 above remained unchanged.

Offline tests verify Python control flow with synthetic host operations. They do not establish behavior on a live Windows or WSL host.
