# Mutation sweep: `windows_activation.py`

## Method

Read the full module and its existing activation tests. Generated 150 single-edit AST mutants, covering all 17 functions and methods, and ran `tests/test_windows_activation.py` against each in a fresh subprocess with a five-second timeout. The mutated module was compiled in memory and injected into that subprocess; the production file was never written. The original test version came from `git show HEAD:tests/test_windows_activation.py`. Each mutation was then run against the expanded test version. The runner checked the source bytes after every mutant.

The selected edits comprised 29 statement removals, 25 `return None` replacements, 24 `return False` replacements, 23 empty-string return replacements, 24 condition negations, 11 dropped raises, 6 `and`/`or` swaps, 5 numeric increments, and 3 comparison flips. The runner also supported `+`/`-` swaps, but this module had no eligible addition or subtraction expression. Comments, logging, and docstrings were excluded as mutation targets.

| Suite | Killed | Kill rate | Survived |
| --- | ---: | ---: | ---: |
| Original 63 tests | 133 / 150 | 88.7% | 17 |
| Expanded 73 tests | 147 / 150 | 98.0% | 3 |

No mutant timed out or failed to import. Fourteen additional mutants were killed. The remaining three are equivalent: IDs 113 (`proves_ownership`, line 280), 119 (`action_executable`, line 247), and 132 (`proves_ownership`, line 268) each replace `return ""` with the same `return ""`. They do not change behavior and need no test. No survivor was untestable offline.

## New tests and recorded kills

Each named test passed on the original and failed when run alone against the listed mutant.

| Test in `tests/test_windows_activation.py` | Mutant ID | Mutation |
| --- | ---: | --- |
| `ActionTests.test_validate_executable_returns_the_accepted_path` | 3 | Return `None` instead of the accepted path |
| `ActionTests.test_validate_executable_rejects_missing_path_with_specific_reason` | 6 | Drop the initial invalid-path raise |
| `ActionTests.test_validate_executable_rejects_non_string_path_with_specific_reason` | 5 | Swap `or` to `and` in the type/empty check |
| `RegisterArgvTests.test_an_invalid_custom_task_name_is_refused` | 0 | Remove task name validation |
| `OtherArgvTests.test_status_refuses_an_invalid_custom_task_name` | 1 | Remove task name validation |
| `OtherArgvTests.test_run_now_refuses_an_invalid_custom_task_name` | 2 | Remove task name validation |
| `OwnershipTests.test_an_empty_action_has_no_executable` | 8 | Return `None` for an empty action |
| `OwnershipTests.test_an_extra_leading_quote_has_no_executable` | 9 | Search for the closing quote one character later |
| `OwnershipTests.test_an_unparseable_action_blocks_ownership` | 10 | Return `None` instead of the unparseable-action reason |
| `OwnershipTests.test_a_matching_executable_has_an_empty_ownership_blocker` | 7 | Return `None` instead of an empty blocker |

## Defects found

None in the tested mutations; no expected-failure test was needed.

## Verification

- `python3 tests/test_windows_activation.py`: 73 passed.
- `python3 tests/test_suite.py`: 593 passed, 0 failed, 5 platform skips.
- `python3 tests/test_onboard.py`: 28 passed.
- `python3 tests/test_local_worker.py`: could not run here because the sandbox denied binding its local HTTP fixture (`PermissionError: [Errno 1] Operation not permitted`).
- `src/agent_bridge/orchestration/windows_activation.py` SHA-256 before and after: `a8e42f72163738e174da9809bcba6444175652f907b75fc42359dd2fa786e27f`.

These are portable, offline checks. No live Windows Task Scheduler behavior was exercised.
