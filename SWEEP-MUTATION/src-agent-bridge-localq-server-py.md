# Mutation sweep: `src/agent_bridge/localq/server.py`

## Method and result

The source was parsed as Python AST and each of 98 single-site mutants was compiled into an in-memory replacement module in a fresh subprocess. The production file was never written. Each run had a five-second timeout. Mutations covered every function, skipped comments and docstrings, and touched no logging code. The candidate operators were comparison flips, `and`/`or` swaps, condition negation, numeric `+1`, return replacement, one-statement removal, `+`/`-` swap, and dropping a raise. The test selection was the three pre-existing protocol tests in `tests/test_localq_server.py` plus `tests/test_system_acceptance.py` before; afterward it included the nine new protocol tests as well. The system acceptance tests use local fakes. No external API or inference was used.

| Phase | Assertion failures or errors | Time-boxed kills | Killed total | Survived | Kill rate |
| --- | ---: | ---: | ---: | ---: | ---: |
| Before | 63 | 1 | 64 / 98 | 34 | 65.3% |
| After | 96 | 1 | 97 / 98 | 1 | 99.0% |

The time-boxed mutant, #18, removes `_stop.set()` from `stop`, leaving the executor thread running during the protocol lifecycle test. Its direct isolated run against `test_stop_signals_and_joins_with_interval_grace` fails with `set` called zero times. Source SHA-256 before and after: `89abca6c567b38805a7d3483ca5a6f764934d8a20f3c7db00b9fc6136061c8a3`.

## Generated mutants

| Kind | Count |
| --- | ---: |
| `boolean` | 7 |
| `comparison` | 4 |
| `drop-raise` | 1 |
| `negate-condition` | 16 |
| `numeric-plus-one` | 12 |
| `remove-statement` | 37 |
| `return-empty` | 3 |
| `return-false` | 3 |
| `return-none` | 13 |
| `swap-arithmetic` | 2 |

| Function | Count |
| --- | ---: |
| `__init__` | 11 |
| `_content` | 2 |
| `_error` | 2 |
| `_executor` | 3 |
| `_valid_id` | 5 |
| `handle` | 39 |
| `main` | 11 |
| `serve` | 16 |
| `start` | 3 |
| `stop` | 6 |

## New tests, grouped by function

| Function | Test | Recorded mutants killed by the test |
| --- | --- | --- |
| `__init__` | `test_init_rejects_nonpositive_interval_and_keeps_default` | #1, #3, #5, #6 |
| `_executor` | `test_executor_waits_after_each_poll` | #14 |
| `stop` | `test_stop_signals_and_joins_with_interval_grace` | #18 (direct isolated failure), #20, #21, #22 |
| `_valid_id` | `test_valid_id_rejects_boolean_and_nonfinite_numbers` | #29, #30 |
| `handle` | `test_handle_preserves_ids_and_distinguishes_invalid_tool_inputs` | #41, #42, #56–#58, #61–#62, #65–#66 |
| `serve` | `test_serve_uses_default_streams_and_flushes_each_reply` | #72, #83, #86 |
| `main` | `test_main_passes_absolute_paths_and_interval_to_service` | #88, #92–#94, #97–#98 |
| `main` | `test_main_requires_each_path_argument` | #89–#91 |
| `main` | `test_main_rejects_relative_paths_before_service_creation` | #95–#96 |

Each listed test passes on the original. The after run records a failure on at least one mutant for every new test. The direct run for #18 confirms the stop test fails even though the combined run times out on that mutant.

## Survivors left

| Mutant | Site | Reason |
| --- | --- | --- |
| #39 | `handle`, line 75, replace `return None` with `return None` | Equivalent: the AST replacement is identical to the original notification return. |

Untestable offline: none. Defects found: none.

## Complete mutation ledger

`K` means the selected tests failed, `T` means the five-second time box expired, and `S` means the selected tests passed. A timeout counts as killed because the mutant prevents the protocol lifecycle test from completing; #18 also has an isolated assertion failure as noted above.

| ID | Function | Line | Mutation | Before | After |
| ---: | --- | ---: | --- | :---: | :---: |
| 1 | `__init__` | 28 | `numeric-plus-one` | S | K |
| 2 | `__init__` | 29 | `negate-condition` | K | K |
| 3 | `__init__` | 29 | `comparison [0, 'Lt']` | S | K |
| 4 | `__init__` | 29 | `numeric-plus-one` | K | K |
| 5 | `__init__` | 30 | `remove-statement` | S | K |
| 6 | `__init__` | 30 | `drop-raise` | S | K |
| 7 | `__init__` | 31 | `remove-statement` | K | K |
| 8 | `__init__` | 32 | `remove-statement` | K | K |
| 9 | `__init__` | 33 | `remove-statement` | K | K |
| 10 | `__init__` | 34 | `remove-statement` | K | K |
| 11 | `__init__` | 35 | `remove-statement` | K | K |
| 12 | `_executor` | 38 | `negate-condition` | K | K |
| 13 | `_executor` | 40 | `remove-statement` | K | K |
| 14 | `_executor` | 44 | `remove-statement` | S | K |
| 15 | `start` | 47 | `negate-condition` | K | K |
| 16 | `start` | 48 | `remove-statement` | K | K |
| 17 | `start` | 49 | `remove-statement` | K | K |
| 18 | `stop` | 52 | `remove-statement` | T | T |
| 19 | `stop` | 53 | `negate-condition` | K | K |
| 20 | `stop` | 54 | `remove-statement` | S | K |
| 21 | `stop` | 54 | `swap-arithmetic Sub` | S | K |
| 22 | `stop` | 54 | `numeric-plus-one` | S | K |
| 23 | `stop` | 55 | `remove-statement` | K | K |
| 24 | `_error` | 59 | `return-none` | K | K |
| 25 | `_error` | 59 | `return-empty` | K | K |
| 26 | `_valid_id` | 63 | `return-none` | K | K |
| 27 | `_valid_id` | 63 | `return-false` | K | K |
| 28 | `_valid_id` | 63 | `boolean And` | K | K |
| 29 | `_valid_id` | 63 | `boolean Or` | S | K |
| 30 | `_valid_id` | 63 | `boolean And` | S | K |
| 31 | `_content` | 67 | `return-none` | K | K |
| 32 | `_content` | 67 | `return-empty` | K | K |
| 33 | `handle` | 71 | `negate-condition` | K | K |
| 34 | `handle` | 71 | `boolean And` | K | K |
| 35 | `handle` | 72 | `return-none` | K | K |
| 36 | `handle` | 72 | `numeric-plus-one` | K | K |
| 37 | `handle` | 73 | `remove-statement` | K | K |
| 38 | `handle` | 74 | `negate-condition` | K | K |
| 39 | `handle` | 75 | `return-none` | S | S |
| 40 | `handle` | 76 | `negate-condition` | K | K |
| 41 | `handle` | 77 | `return-none` | S | K |
| 42 | `handle` | 77 | `numeric-plus-one` | S | K |
| 43 | `handle` | 78 | `remove-statement` | K | K |
| 44 | `handle` | 79 | `negate-condition` | K | K |
| 45 | `handle` | 79 | `comparison [0, 'NotEq']` | K | K |
| 46 | `handle` | 80 | `remove-statement` | K | K |
| 47 | `handle` | 82 | `negate-condition` | K | K |
| 48 | `handle` | 82 | `comparison [0, 'NotEq']` | K | K |
| 49 | `handle` | 83 | `remove-statement` | K | K |
| 50 | `handle` | 84 | `negate-condition` | K | K |
| 51 | `handle` | 85 | `remove-statement` | K | K |
| 52 | `handle` | 87 | `negate-condition` | K | K |
| 53 | `handle` | 87 | `comparison [0, 'NotEq']` | K | K |
| 54 | `handle` | 88 | `remove-statement` | K | K |
| 55 | `handle` | 89 | `negate-condition` | K | K |
| 56 | `handle` | 89 | `boolean And` | S | K |
| 57 | `handle` | 90 | `return-none` | S | K |
| 58 | `handle` | 90 | `numeric-plus-one` | S | K |
| 59 | `handle` | 91 | `remove-statement` | K | K |
| 60 | `handle` | 92 | `negate-condition` | K | K |
| 61 | `handle` | 93 | `return-none` | S | K |
| 62 | `handle` | 93 | `numeric-plus-one` | S | K |
| 63 | `handle` | 94 | `remove-statement` | K | K |
| 64 | `handle` | 95 | `negate-condition` | K | K |
| 65 | `handle` | 96 | `return-none` | S | K |
| 66 | `handle` | 96 | `numeric-plus-one` | S | K |
| 67 | `handle` | 97 | `remove-statement` | K | K |
| 68 | `handle` | 99 | `return-none` | K | K |
| 69 | `handle` | 99 | `numeric-plus-one` | K | K |
| 70 | `handle` | 100 | `return-none` | K | K |
| 71 | `handle` | 100 | `return-empty` | K | K |
| 72 | `serve` | 103 | `remove-statement` | S | K |
| 73 | `serve` | 103 | `boolean And` | K | K |
| 74 | `serve` | 103 | `boolean And` | K | K |
| 75 | `serve` | 104 | `remove-statement` | K | K |
| 76 | `serve` | 108 | `remove-statement` | K | K |
| 77 | `serve` | 110 | `remove-statement` | K | K |
| 78 | `serve` | 110 | `numeric-plus-one` | K | K |
| 79 | `serve` | 112 | `remove-statement` | K | K |
| 80 | `serve` | 113 | `negate-condition` | K | K |
| 81 | `serve` | 114 | `remove-statement` | K | K |
| 82 | `serve` | 114 | `swap-arithmetic Sub` | K | K |
| 83 | `serve` | 115 | `remove-statement` | S | K |
| 84 | `serve` | 117 | `remove-statement` | K | K |
| 85 | `serve` | 118 | `return-none` | K | K |
| 86 | `serve` | 118 | `return-false` | S | K |
| 87 | `serve` | 118 | `numeric-plus-one` | K | K |
| 88 | `main` | 122 | `remove-statement` | S | K |
| 89 | `main` | 123 | `remove-statement` | S | K |
| 90 | `main` | 124 | `remove-statement` | S | K |
| 91 | `main` | 125 | `remove-statement` | S | K |
| 92 | `main` | 126 | `remove-statement` | S | K |
| 93 | `main` | 126 | `numeric-plus-one` | S | K |
| 94 | `main` | 127 | `remove-statement` | S | K |
| 95 | `main` | 128 | `negate-condition` | S | K |
| 96 | `main` | 129 | `remove-statement` | S | K |
| 97 | `main` | 130 | `return-none` | S | K |
| 98 | `main` | 130 | `return-false` | S | K |

## Verification

- `python3 -m unittest discover -s tests -p test_localq_server.py -q`: 12 passed.
- `python3 -m unittest discover -s tests -p test_system_acceptance.py -q`: 5 passed.
- `python3 tests/test_suite.py`: 593 passed, 0 failed, 5 platform-dependent skips.
- The production source SHA-256 matched before and after every mutation run.
