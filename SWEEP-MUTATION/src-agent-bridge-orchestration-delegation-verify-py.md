# `delegation_verify.py` mutation sweep

The source was read in full. Seventeen single-change mutants were run, covering
all 14 functions. Each mutant was compiled into an already imported module in
a separate Python process; the production file was never rewritten. Each run
had a 30-second timeout. The existing baseline ran
`test_delegation_verify_isolation`, the verification queue tests in
`test_public_hotfix`, and three offline calibration refusal tests in
`test_local_first`. The after run added `test_delegation_verify_mutations`.

| Measure | Before | After |
| --- | ---: | ---: |
| Generated | 17 | 17 |
| Killed | 6 | 16 |
| Survived | 11 | 1 |
| Raw kill rate | 35.3% | 94.1% |
| Kill rate excluding equivalent mutant | 37.5% | 100% |

| ID | Function | Mutation | Before | After |
| ---: | --- | --- | --- | --- |
| 1 | `_empty_row` | `attempted` false to true | survived | killed |
| 2 | `_disposable_repo` | remove fixture write | killed | killed |
| 3 | `_verification_queue_root` | return `None` | killed | killed |
| 4 | `_remove_tree` | negate removal result | killed | killed |
| 5 | `select_executor` | negate Windows condition | killed | killed |
| 6 | `_run_direction` | `attempted` true to false | survived | killed |
| 7 | `_local_model_params` | `!=` to `==` | survived | killed |
| 8 | `_local_model_check` | `!=` to `==` | survived | killed |
| 9 | `_local_model_check_in` | `!=` to `==` | survived | killed |
| 10 | `_synthetic_calibration_input` | `< 1024` to `<= 1024` | survived | survived; equivalent |
| 11 | `_production_queue_busy` | return false | killed | killed |
| 12 | `calibrate` | negate busy condition | killed | killed |
| 13 | `run` | `no_client_data` true to false | survived | killed |
| 14 | `main` | `or` to `and` in required arguments | survived | killed |
| 15 | `_synthetic_calibration_input` | `99` to `98` | survived | killed |
| 16 | `_synthetic_calibration_input` | `-` to `+` in sentence budget | survived | killed |
| 17 | `_synthetic_calibration_input` | drop invalid-input raise | survived | killed |

Mutant 10 differs only for input length 1024. Both versions raise
`ValueError("calibration_input_invalid")` there: the later filler-length check
rejects it. No behavior-changing survivor remains.

## Defects found

None.

New tests, grouped by function:

- `_empty_row`: `EmptyRowTests.test_unattempted_direction_is_explicit` (kills 1)
- `_run_direction`: `RunDirectionTests.test_submitted_direction_is_marked_attempted` (kills 6)
- `_local_model_params`: `LocalModelParamsTests.test_private_worker_receives_automatic_provider` (kills 7)
- `_local_model_check`: `LocalModelCheckTests.test_certified_backend_does_not_require_private_worker` (kills 8)
- `_local_model_check_in`: `LocalModelCheckInTests.test_completed_job_reports_synthetic_source` (kills 9)
- `_synthetic_calibration_input`: `SyntheticCalibrationInputTests.test_final_run_index_is_accepted` (kills 15), `test_requested_byte_length_is_exact` (kills 16), `test_negative_run_index_is_rejected` (kills 17)
- `run`: `RunTests.test_report_affirms_no_client_data` (kills 13)
- `main`: `MainTests.test_partial_arguments_are_rejected_before_running` (kills 14)

The production source SHA-256 after both sweeps was
`37f705f486f40068fc3da061322b64819be969d0d9af531bc66d97447eaf62e2`.

Verification: each new test passed on the original and failed when run alone
against the numbered mutant above. `python3 tests/test_suite.py` passed (593
checks, 0 failures, 5 platform skips). `test_onboard`, `test_hostenv`,
`test_automatic_gate`, and `test_delegation_audit` passed. The additional
`test_local_worker` and `test_automatic_delegation_e2e` runs could not enter
their tests because this sandbox refused local HTTP socket binds with
`PermissionError`; they were not used to classify mutants.
