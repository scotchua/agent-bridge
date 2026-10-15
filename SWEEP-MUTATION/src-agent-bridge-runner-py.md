# Mutation sweep: `src/agent_bridge/runner.py`

The sweep made 27 single-node AST mutations to an in-memory copy of the module. It changed no production file. It selected executable nodes across all six functions/methods, omitting comments, docstrings, and logging. Each mutant ran in a fresh process with a 20-second limit against the 17 existing `tests/test_health.py` tests. The after pass also ran the 12 tests in `tests/test_runner_mutation.py`. After adding the final zero-budget marker assertion, M07 was rerun individually; the other results carry forward because only that assertion changed.

| Pass | Killed | Generated | Kill rate |
| --- | ---: | ---: | ---: |
| Existing tests | 8 | 27 | 29.63% |
| Existing plus new tests | 27 | 27 | 100% |

| ID | Single mutation | Before | After |
| --- | --- | --- | --- |
| M01 | `sanitized_argv` return to `None` | survived | killed |
| M02 | argument validation `or` to `and` | survived | killed |
| M03 | finite budget validation `or` to `and` | survived | killed |
| M04 | negate finite deadline guard | killed | killed |
| M05 | deadline `+` to `-` | killed | killed |
| M06 | grace divisor 2 to 3 | survived | killed |
| M07 | timeout `<= 0` to `< 0` | survived | killed |
| M08 | negate marker presence guard | killed | killed |
| M09 | negate marker write failure guard | survived | killed |
| M10 | read deadline `>=` to `>` | survived | killed |
| M11 | termination `or` to `and` | survived | killed |
| M12 | negate reap condition | survived | killed |
| M13 | lingering group `and` to `or` | survived | killed |
| M14 | `run` return to `None` | killed | killed |
| M15 | remove marker `fsync` | survived | killed |
| M16 | successful marker write return to `False` | survived | killed |
| M17 | failed marker write return to `True` | survived | killed |
| M18 | pause phase `==` to `!=` | survived | killed |
| M19 | remove default `PATH` statement | survived | killed |
| M20 | remove peer environment marker statement | survived | killed |
| M21 | negate extra environment guard | killed | killed |
| M22 | remove extra environment update statement | killed | killed |
| M23 | scrubbed environment return to `None` | killed | killed |
| M24 | credential context return to empty dict | survived | killed |
| M25 | credential selector `in` to `not in` | killed | killed |
| M26 | drop invalid argument exception | survived | killed |
| M27 | drop nonfinite budget exception | survived | killed |

## New tests

Each new test passes on the original module and fails on at least one recorded mutant:

| Test method in `tests/test_runner_mutation.py` | Mutant evidence |
| --- | --- |
| `RunResultTests.test_sanitized_argv_is_a_separate_copy` | M01 |
| `RunTests.test_bad_arguments_and_nonfinite_budgets_are_rejected` | M02, M03, M26, M27 |
| `RunTests.test_exhausted_budget_does_not_spawn` | M04, M07 |
| `RunTests.test_read_deadline_boundary_does_not_spawn` | M10 |
| `RunTests.test_marker_failure_refuses_spawn` | M09 |
| `RunTests.test_deadline_and_grace_bound_stream_budget` | M05, M06, M12, M13 |
| `RunTests.test_cap_breach_terminates_the_group` | M11 |
| `MarkerTests.test_write_marker_creates_durable_json` | M15, M16 |
| `MarkerTests.test_write_marker_reports_open_error` | M17 |
| `PauseTests.test_pause_only_at_selected_phase` | M18 |
| `EnvironmentTests.test_scrubbed_env_has_defaults_and_explicit_overrides` | M19, M20, M21, M22, M23 |
| `EnvironmentTests.test_credential_context_contains_only_present_selectors` | M24, M25 |

## Survivors left

- **Equivalent:** none.
- **Untestable offline:** none.
- **Defects found:** none.

## Verification

- `python3 -m unittest discover -s tests -p test_runner_mutation.py -q`: 12 passed.
- M07 was rerun separately after the zero-budget marker assertion was added; `RunTests.test_exhausted_budget_does_not_spawn` failed on the mutant because it deleted the existing marker.
- `python3 tests/test_suite.py`: 593 passed, 0 failed, 5 environmental skips.
- The mutation runner compared `runner.py` bytes to its starting bytes after every mutant. No production file was edited.

This is a selected mutation sample, capped well below 150; it does not claim exhaustive mutation coverage.
