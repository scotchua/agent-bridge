# Mutation test: windows_wsl_provision.py

## Scope and method

- Source: `src/agent_bridge/orchestration/windows_wsl_provision.py`.
- Original SHA-256 before and after: `8ebecafc2100f1adc8c14650d6bdcc9c2dd3f830211daa1f3a798859b999bbdb`.
- 61 compile-valid, single-edit mutants selected across all 36 function definitions; maximum allowed was 150.
- Mutations were executed in a fresh Python test process by replacing the imported module in memory. The source file was never written. Each run had a 12-second timeout; none timed out.
- Existing tests: `test_windows_wsl_provision.py`, `test_windows_provision_driver.py`, `test_windows_evidence.py`, `test_windows_delegation.py`, and `test_windows_activation.py`. The after run also loaded `test_windows_wsl_provision_mutation.py`.
- Selection excluded comments, docstrings and logging. Operators used: comparison flip, `and`/`or` swap, condition negation, numeric increment, return replacement, statement removal, `+`/`-` swap and raise removal.

## Kill rates

| Run | Killed | Generated | Raw kill rate | Non-equivalent kill rate |
| --- | ---: | ---: | ---: | ---: |
| Before new tests | 54 | 61 | 88.5% | 54/60 (90.0%) |
| After new tests | 60 | 61 | 98.4% | 60/60 (100.0%) |

One surviving mutant, M034, is equivalent. A nonzero test result counted as killed; there were no timeouts.

## Mutants generated

| ID | Function | Line | Mutation | Before | After |
| --- | --- | ---: | --- | --- | --- |
| M001 | `_system32` | 104 | return replaced | killed | killed |
| M002 | `enable_feature_argv` | 115 | exception raise dropped | killed | killed |
| M003 | `feature_state_argv` | 124 | exception raise dropped | killed | killed |
| M004 | `install_wsl_argv` | 141 | return replaced | killed | killed |
| M005 | `update_wsl_argv` | 145 | return replaced | killed | killed |
| M006 | `wsl_status_argv` | 149 | return replaced | killed | killed |
| M007 | `restart_argv` | 155 | boolean operator | killed | killed |
| M008 | `cancel_restart_argv` | 164 | return replaced | killed | killed |
| M009 | `elevate_argv` | 179 | boolean operator | killed | killed |
| M010 | `current_stage` | 251 | condition negated | killed | killed |
| M011 | `delegation_may_be_enabled` | 277 | comparison | killed | killed |
| M012 | `next_stage` | 285 | comparison | killed | killed |
| M013 | `remaining_stages` | 405 | comparison | killed | killed |
| M014 | `plan` | 417 | comparison | killed | killed |
| M015 | `_state_at` | 448 | condition negated | killed | killed |
| M016 | `resume_record_path` | 474 | return replaced | killed | killed |
| M017 | `write_resume_record` | 511 | arithmetic operator | killed | killed |
| M018 | `read_resume_record` | 540 | comparison | killed | killed |
| M019 | `next_attempt` | 561 | comparison | killed | killed |
| M020 | `clear_resume_record` | 577 | exception raise dropped | survived | killed |
| M021 | `build_resume_record` | 587 | comparison | killed | killed |
| M022 | `parse_resume_record` | 606 | comparison | killed | killed |
| M023 | `should_stop_retrying` | 632 | comparison | killed | killed |
| M024 | `register_resume_argv` | 652 | condition negated | killed | killed |
| M025 | `unregister_resume_argv` | 669 | return replaced | killed | killed |
| M026 | `resume_status_argv` | 673 | return replaced | killed | killed |
| M027 | `resume_ownership_blocker` | 685 | comparison | killed | killed |
| M028 | `observe` | 764 | comparison | killed | killed |
| M029 | `require_consent` | 820 | boolean operator | killed | killed |
| M030 | `advance` | 843 | comparison | killed | killed |
| M031 | `features_all_enabled` | 217 | return replaced | killed | killed |
| M032 | `as_dict` | 235 | return replaced | survived | killed |
| M033 | `reached` | 440 | comparison | killed | killed |
| M034 | `__init__` | 481 | statement removed | survived | survived |
| M035 | `as_dict` | 718 | return replaced | killed | killed |
| M036 | `record` | 753 | return replaced | killed | killed |
| M037 | `enable_feature_argv` | 116 | return replaced | killed | killed |
| M038 | `feature_state_argv` | 125 | return replaced | killed | killed |
| M039 | `restart_argv` | 155 | condition negated | killed | killed |
| M040 | `elevate_argv` | 176 | condition negated | killed | killed |
| M041 | `current_stage` | 257 | condition negated | killed | killed |
| M042 | `delegation_may_be_enabled` | 277 | return replaced | killed | killed |
| M043 | `next_stage` | 294 | comparison | killed | killed |
| M044 | `remaining_stages` | 405 | return replaced | killed | killed |
| M045 | `plan` | 425 | comparison | survived | killed |
| M046 | `_state_at` | 442 | return replaced | killed | killed |
| M047 | `write_resume_record` | 511 | numeric constant | survived | killed |
| M048 | `read_resume_record` | 542 | exception raise dropped | killed | killed |
| M049 | `next_attempt` | 564 | arithmetic operator | killed | killed |
| M050 | `clear_resume_record` | 573 | statement removed | killed | killed |
| M051 | `build_resume_record` | 585 | boolean operator | killed | killed |
| M052 | `parse_resume_record` | 608 | comparison | killed | killed |
| M053 | `should_stop_retrying` | 632 | comparison | killed | killed |
| M054 | `register_resume_argv` | 653 | exception raise dropped | survived | killed |
| M055 | `resume_ownership_blocker` | 696 | comparison | killed | killed |
| M056 | `observe` | 763 | boolean operator | killed | killed |
| M057 | `require_consent` | 822 | boolean operator | killed | killed |
| M058 | `advance` | 851 | comparison | killed | killed |
| M059 | `reached` | 440 | return replaced | killed | killed |
| M060 | `__init__` | 482 | statement removed | killed | killed |
| M061 | `record` | 749 | statement removed | survived | killed |

## New tests

Each test passed on the original and was run alone against the listed mutant, where it failed.

| Function | Test | Mutant killed |
| --- | --- | --- |
| `clear_resume_record` | `ClearResumeRecordTests.test_unlink_failure_reports_resume_not_removable` | M020 |
| `Stage.as_dict` | `StageAsDictTests.test_stage_serializes_its_commands_and_flags` | M032 |
| `plan` | `PlanBlockedOnUserTests.test_unsupported_platform_is_blocked_on_user` | M045 |
| `write_resume_record` | `WriteResumeRecordTests.test_resume_record_has_two_space_indentation` | M047 |
| `register_resume_argv` | `RegisterResumeArgvTests.test_empty_command_has_a_direct_validation_error` | M054 |
| `observe.record` | `ObserveRecordTests.test_uncollected_preflight_checks_are_recorded` | M061 |

## Survivors left

- **M034 — equivalent:** removing `super().__init__(reason)` in `ResumeError.__init__` leaves `args`, `str(error)`, and the stored `reason` unchanged because `BaseException.__new__` already records the constructor arguments. No test is warranted.
- Untestable offline: none among selected mutants.
- Defect: none among selected mutants.

## Defects found

None.

## Verification

- `PYTHONPATH=src:tests python3 -m unittest -q test_windows_wsl_provision test_windows_provision_driver test_windows_evidence test_windows_delegation test_windows_activation test_windows_wsl_provision_mutation`: 375 passed.
- `python3 -m unittest discover -s tests -p "test_windows_wsl_provision*.py" -q`: 92 passed.
- `python3 tests/test_suite.py`: 593 passed, 0 failed, 5 environmental skips.
- `python3 tests/test_onboard.py -q`: 28 passed using temporary homes.
- `tests/test_local_worker.py` and `tests/test_automatic_delegation_e2e.py` could not bind their local HTTP fixtures because the sandbox refused loopback sockets (`PermissionError: Operation not permitted`).
- No live Windows host was exercised.
