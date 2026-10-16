# Mutation sweep: windows_provision_driver.py

## Method

An AST based throwaway runner generated 36 single change mutants across all 29 functions in `src/agent_bridge/orchestration/windows_provision_driver.py`. It used comparison flips, `and`/`or` swaps, condition negation, numeric increments, return replacement, statement removal, `+`/`-` swaps, and dropped raises. It selected executable syntax only, excluding comments, docstrings, and logging. Each mutant was loaded from a temporary file into a fresh Python process; the production module was never written. Each run had an 8 second timeout and ran `test_windows_provision_driver` and `test_windows_setup` offline. The source bytes were checked after each sweep.

## Kill rates

| Measure | Before new tests | After new tests |
| --- | ---: | ---: |
| All generated mutants | 28/36 (77.8%) | 35/36 (97.2%) |
| Behavioral mutants, excluding M026 | 28/35 (80.0%) | 35/35 (100.0%) |

## Mutants generated

| ID | Function:line | Mutation | Before | After |
| --- | --- | --- | --- | --- |
| M001 | `_step:90` | negate | killed | killed |
| M002 | `verify_installed_image:151` | boolop | killed | killed |
| M003 | `_manifest_dict:163` | number | killed | killed |
| M004 | `acquire_guest_image:193` | number | survived | killed |
| M005 | `verify_boundary_live:252` | return | killed | killed |
| M006 | `record_boundary:277` | remove | killed | killed |
| M007 | `wa_kind:304` | return | killed | killed |
| M008 | `_probe_verdicts:313` | return | killed | killed |
| M009 | `run_auth_probe:361` | compare | killed | killed |
| M010 | `observe_provider_lane:393` | negate | killed | killed |
| M011 | `record_provider_lane:447` | negate | killed | killed |
| M012 | `_reload_lane:487` | return | killed | killed |
| M013 | `observe:546` | return | killed | killed |
| M014 | `_recorded_state:557` | remove | killed | killed |
| M015 | `step:588` | compare | killed | killed |
| M016 | `_now:638` | negate | survived | killed |
| M017 | `_image_step:660` | compare | survived | killed |
| M018 | `_boundary_step:672` | negate | killed | killed |
| M019 | `_provider_step:704` | negate | killed | killed |
| M020 | `schedule_resume:843` | number | killed | killed |
| M021 | `_resume_task_status:859` | return | killed | killed |
| M022 | `resume:873` | remove | killed | killed |
| M023 | `finish_resume:931` | compare | killed | killed |
| M024 | `activate_worker:958` | compare | survived | killed |
| M025 | `plan:985` | negate | killed | killed |
| M026 | `__init__:85` | remove | survived | survived |
| M027 | `as_dict:113` | return | survived | killed |
| M028 | `authenticated:326` | return | killed | killed |
| M029 | `rejected:330` | return | killed | killed |
| M030 | `as_dict:785` | return | killed | killed |
| M031 | `schedule_resume:815` | arithmetic | survived | killed |
| M032 | `_step:91` | raise | survived | killed |
| M033 | `verify_installed_image:138` | negate | killed | killed |
| M034 | `record_provider_lane:468` | compare | killed | killed |
| M035 | `schedule_resume:813` | compare | killed | killed |
| M036 | `finish_resume:904` | boolop | killed | killed |

## New tests

Each test passed on the original and failed when run alone against its paired mutant:

- M004: `test_windows_provision_driver.ImageAcquisitionMutationTests.test_new_image_directory_is_private`
- M016: `test_windows_provision_driver.ClockMutationTests.test_supplied_clock_is_used`
- M017: `test_windows_provision_driver.ImageStepMutationTests.test_missing_install_consent_is_reported_as_consent_required`
- M024: `test_windows_provision_driver.ActivationCommandMutationTests.test_failed_registration_command_is_reported`
- M027: `test_windows_provision_driver.ProvenanceDictionaryMutationTests.test_the_dictionary_preserves_each_image_fact`
- M031: `test_windows_provision_driver.ResumeMachineTests.test_exhausted_stage_reports_the_last_attempt_not_a_future_attempt`
- M032: `test_windows_provision_driver.StepResultMutationTests.test_an_unknown_status_is_a_programming_error`

## Survivors left

- M026 (`DriverError.__init__`, line 85, removed `RuntimeError.__init__` call): equivalent. `BaseException.__new__` already stores the constructor argument in `args`; the `reason` assignment remains, so exception text, arguments, and reason are unchanged.

## Defects found

- None.

## Verification and limits

- `python3 tests/test_suite.py`: 592 passed, 0 failed, 5 environmental skips.
- `python3 tests/test_onboard.py`: 28 passed.
- `python3 tests/test_windows_provision_driver.py`: 75 passed.
- `python3 tests/test_windows_setup.py`: 66 passed.
- `python3 tests/test_hostenv.py`: 21 passed, 11 skipped.
- `python3 tests/test_automatic_gate.py`: 169 passed.
- `python3 tests/test_delegation_audit.py`: 40 passed.
- `tests/test_local_worker.py` and `tests/test_automatic_delegation_e2e.py` could not run in this sandbox: their loopback HTTP servers failed at `bind()` with `PermissionError: [Errno 1] Operation not permitted`. No external service was contacted.
- All tests ran with a temporary home. No live Windows or provider behavior is claimed.
