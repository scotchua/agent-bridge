# Mutation test: `src/agent_bridge/windows_setup.py`

The runner compiled one mutated copy of the module at a time inside an isolated Python process. It did not write the production source. Each process had a 12-second limit and a temporary home. The offline `tests/test_windows_setup.py` suite was the before baseline; the after run added `tests/test_windows_setup_mutation.py`.

## Kill rates

| Run | Generated | Killed | Survived | Kill rate |
| --- | ---: | ---: | ---: | ---: |
| Before | 24 | 14 | 10 | 58.3% |
| After | 24 | 24 | 0 | 100.0% |

The 24 selected mutants cover every executable function in the module. Targets excluded comments, logging, and docstrings. No mutant timed out.

## Mutants generated

| ID | Function | Mutation | Before | After |
| --- | --- | --- | --- | --- |
| M01 | `default_runtime_root` | negate condition | killed | killed |
| M02 | `build_config` | swap and/or | killed | killed |
| M03 | `run_command` | numeric +1 | survived | killed |
| M04 | `collect_feature_states` | flip comparison | killed | killed |
| M05 | `reboot_pending` | flip comparison | killed | killed |
| M06 | `launcher_script` | negate condition | killed | killed |
| M07 | `stable_resume_launcher` | replace return | killed | killed |
| M08 | `install_resume_launcher` | negate condition | killed | killed |
| M09 | `resume_command` | remove statement | killed | killed |
| M10 | `build_context` | negate condition | killed | killed |
| M11 | `_wsl_version` | replace return | survived | killed |
| M12 | `command_plan` | replace return value | killed | killed |
| M13 | `command_step` | swap and/or | survived | killed |
| M14 | `command_status` | flip comparison | survived | killed |
| M15 | `command_resume` | negate condition | killed | killed |
| M16 | `command_validate` | replace return value | survived | killed |
| M17 | `_validation_runners` | negate condition | survived | killed |
| M18 | `_now` | numeric +1 | survived | killed |
| M19 | `build_parser` | replace default | survived | killed |
| M20 | `main` | replace return value | killed | killed |
| M21 | `default_runtime_root` | drop exception raise | killed | killed |
| M22 | `reboot_pending` | swap + and - | killed | killed |
| M23 | `command_step` | flip comparison | survived | killed |
| M24 | `command_resume` | numeric +1 | survived | killed |

## New tests

Each test passed on the original module and failed alone on its paired recorded mutant:

| Mutant | Test in `tests/test_windows_setup_mutation.py` |
| --- | --- |
| M03 | `RunCommandTests.test_command_uses_the_declared_timeout` |
| M11 | `WslVersionTests.test_collected_version_is_returned` |
| M13 | `CommandStepTests.test_launcher_install_requires_both_reboot_consents` |
| M14 | `CommandStatusTests.test_successful_task_query_reports_registration` |
| M16 | `CommandValidateTests.test_ready_verdict_exits_successfully` |
| M17 | `ValidationRunnersTests.test_valid_manifest_supplies_rootfs_to_image_check` |
| M18 | `NowTests.test_timestamp_omits_fractional_seconds` |
| M19 | `ParserTests.test_report_path_defaults_to_none` |
| M23 | `CommandStepTests.test_launcher_is_installed_when_both_consents_are_given` |
| M24 | `CommandResumeTests.test_resume_stops_after_eight_successful_nonterminal_steps` |

## Survivors left

None. No equivalent or offline-untestable mutants remained in this selected set.

## Defects found

None. No mutant represented behavior that should replace the original.

## Verification

- `python3 -m unittest discover -s tests -p test_windows_setup.py -q`: 66 passed before mutation.
- `python3 -m unittest discover -s tests -p test_windows_setup_mutation.py -q`: 10 passed on the original module.
- Targeted one-test runs: each of the 10 new tests failed its paired mutant.
- `python3 tests/test_suite.py`: 592 passed, 0 failed, 5 environmental skips, under a temporary home.
- `tests/test_onboard.py`: 28 passed.
- `tests/test_local_worker.py`: 14 errors in this sandbox because its loopback HTTP fixture cannot bind (`PermissionError: [Errno 1] Operation not permitted`).
- No live Windows host or external service was contacted.
