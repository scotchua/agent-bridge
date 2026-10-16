# Mutation sweep: `src/agent_bridge/orchestration/execution_queue.py`

## Method

The throwaway runner parsed the original module with Python AST and generated 57 mutations, one per child process. A targeted constructor mutant (M58) completed coverage of every defined function. All mutations changed only executable AST nodes, excluding comments, logging, and docstrings. Each mutant was loaded into the canonical module in memory before running `test_execution_dispatcher`, `test_execution_outcome_contract`, `test_verify_runnable`, `test_windows_privacy`, and `test_success_gate_unity`; the after run also loaded `test_execution_queue_mutations`. Each child had a 12 second timeout. No packages, APIs, credentials, or client data were used. Source bytes were checked for identity after both sweeps and M58.

## Kill rates

| Stage | Killed | Generated | Kill rate |
| --- | ---: | ---: | ---: |
| Existing tests | 37 | 58 | 63.8% |
| Existing plus new tests | 56 | 58 | 96.6% |

Two generated mutants are equivalent. Among the 56 behavior-changing mutants, the kill rate rose from 37/56 (66.1%) to 56/56 (100%). No run timed out.

## Mutants generated

| ID | Function:line | Mutation | Before | After |
| ---: | --- | --- | --- | --- |
| 1 | `_atomic_json:68` | raise → `pass` | killed | killed |
| 2 | `_read_json:86` | raise → `pass` | survived | killed |
| 3 | `_read_json:87` | negate → `Not` | killed | killed |
| 4 | `_read_json:89` | return → `None` | killed | killed |
| 5 | `__init__:101` | negate → `Not` | killed | killed |
| 6 | `__init__:101` | boolop → `And` | survived | killed |
| 7 | `__init__:102` | raise → `pass` | survived | killed |
| 8 | `__call__:116` | compare → `NotEq` | killed | killed |
| 9 | `__call__:117` | negate → `Not` | killed | killed |
| 10 | `__call__:117` | boolop → `Or` | survived | killed |
| 11 | `validate_outcome:252` | negate → `Not` | killed | killed |
| 12 | `validate_outcome:253` | raise → `pass` | survived | killed |
| 13 | `validate_outcome:258` | boolop → `And` | killed | killed |
| 14 | `_harness_summary:282` | negate → `Not` | killed | killed |
| 15 | `_harness_summary:287` | return → `None` | killed | killed |
| 16 | `_harness_summary:293` | boolop → `Or` | killed | killed |
| 17 | `_clean_diagnostic:319` | negate → `Not` | killed | killed |
| 18 | `_clean_diagnostic:319` | boolop → `And` | killed | killed |
| 19 | `_clean_diagnostic:320` | return → `None` | survived | survived |
| 20 | `_harness_diagnostics:336` | negate → `Not` | killed | killed |
| 21 | `_harness_diagnostics:338` | return → `None` | killed | killed |
| 22 | `_unreadable:344` | return → `None` | killed | killed |
| 23 | `outcome_is_success:357` | negate → `Not` | killed | killed |
| 24 | `outcome_is_success:357` | compare → `Eq` | killed | killed |
| 25 | `outcome_is_success:358` | return → `None` | survived | killed |
| 26 | `require_private_queue:378` | boolop → `And` | survived | killed |
| 27 | `require_private_queue:380` | raise → `pass` | killed | killed |
| 28 | `reserve_nothing:394` | return → `None` | survived | survived |
| 29 | `_recover_interrupted:436` | negate → `Not` | killed | killed |
| 30 | `_recover_interrupted:443` | boolop → `And` | killed | killed |
| 31 | `_recover_interrupted:443` | compare → `NotEq` | killed | killed |
| 32 | `_clean_text:449` | number → `257` | survived | killed |
| 33 | `_clean_text:450` | negate → `Not` | killed | killed |
| 34 | `_clean_text:450` | boolop → `And` | survived | killed |
| 35 | `_refuse_reserved_model:480` | negate → `Not` | killed | killed |
| 36 | `_refuse_reserved_model:480` | boolop → `And` | survived | killed |
| 37 | `_refuse_reserved_model:481` | raise → `pass` | survived | killed |
| 38 | `submit:493` | number → `901` | survived | killed |
| 39 | `submit:497` | negate → `Not` | killed | killed |
| 40 | `submit:497` | boolop → `And` | killed | killed |
| 41 | `_directory:620` | negate → `Not` | killed | killed |
| 42 | `_directory:620` | boolop → `And` | survived | killed |
| 43 | `_directory:620` | compare → `Eq` | killed | killed |
| 44 | `status:633` | negate → `Not` | killed | killed |
| 45 | `status:634` | arithmetic → `Sub` | survived | killed |
| 46 | `status:635` | return → `None` | killed | killed |
| 47 | `hold_dependencies:646` | negate → `Not` | survived | killed |
| 48 | `hold_dependencies:649` | compare → `Eq` | survived | killed |
| 49 | `result:661` | negate → `Not` | killed | killed |
| 50 | `result:662` | raise → `pass` | survived | killed |
| 51 | `result:663` | return → `None` | killed | killed |
| 52 | `run_once:667` | negate → `Not` | killed | killed |
| 53 | `run_once:668` | raise → `pass` | killed | killed |
| 54 | `run_once:674` | boolop → `Or` | killed | killed |
| 55 | `state_report:725` | negate → `Not` | survived | killed |
| 56 | `state_report:727` | arithmetic → `Sub` | survived | killed |
| 57 | `state_report:728` | return → `None` | killed | killed |
| 58 | `ExecutionQueue.__init__:429` | negate recovery condition | killed | killed |

## New tests

All tests below are in `tests/test_execution_queue_mutations.py`, pass on the original source, and each was run alone against a recorded mutant that it killed. They are grouped by the queue function under test.

| Function | Test | Mutant IDs killed in isolated check |
| --- | --- | --- |
| `_read_json` | `test_read_json_missing_file_has_named_refusal` | 2 |
| `SubprocessHarnessExecutor.__init__` | `test_executor_init_requires_absolute_harness_paths` | 6, 7 |
| `SubprocessHarnessExecutor.__call__` | `test_executor_call_allows_a_normal_claude_job_past_review_gate` | 10 |
| `validate_outcome` | `test_validate_outcome_names_a_non_mapping_result` | 12 |
| `outcome_is_success` | `test_outcome_is_success_returns_boolean_false` | 25 |
| `require_private_queue` | `test_require_private_queue_defaults_root_to_the_path` | 26 |
| `ExecutionQueue._clean_text` | `test_clean_text_refuses_its_exact_upper_boundary` | 32 |
| `ExecutionQueue._clean_text` | `test_clean_text_refuses_empty_text` | 34 |
| `ExecutionQueue._refuse_reserved_model` | `test_refuse_reserved_model_refuses_empty_model` | 36, 37 |
| `ExecutionQueue.submit` | `test_submit_uses_the_declared_default_timeout` | 38 |
| `ExecutionQueue._directory` | `test_directory_rejects_invalid_hex_id_by_name` | 42 |
| `ExecutionQueue.status` | `test_status_formats_the_dependency_reason` | 45 |
| `ExecutionQueue.hold_dependencies` | `test_hold_dependencies_annotates_only_matching_queued_jobs` | 47, 48 |
| `ExecutionQueue.result` | `test_result_refuses_queued_job_by_name` | 50 |
| `ExecutionQueue.state_report` | `test_state_report_counts_two_queued_jobs` | 55, 56 |

## Survivors left

- M19, `_clean_diagnostic:320`: replacing `return None` with `return None` is equivalent.
- M28, `reserve_nothing:394`: replacing `return None` with `return None` is equivalent.
- Untestable offline: none.
- Defects found: none.

## Verification

- `python3 tests/test_suite.py`: passed (593 checks, 0 failed, 5 environmental skips).
- `PYTHONPATH=tests:src python3 -m unittest test_execution_queue_mutations test_hostenv test_automatic_gate test_delegation_audit test_onboard -q`: passed (284 tests, 11 skips).
- `PYTHONPATH=tests:src python3 -m unittest test_execution_dispatcher test_execution_outcome_contract test_verify_runnable test_windows_privacy test_success_gate_unity -q`: passed (123 tests).
- The additional `test_automatic_delegation_e2e` and `test_local_worker` run could not finish in this sandbox: loopback socket bind raised `PermissionError: [Errno 1] Operation not permitted` during setup. This did not affect the requested full suite or the mutation sweep.
- `git diff --exit-code -- src/agent_bridge/orchestration/execution_queue.py src/agent_bridge/execution` passed; production source SHA-256: `942a517a2ff93357d3804fecd984952f940c89fec43839fd2561c29dd46e922e`.
