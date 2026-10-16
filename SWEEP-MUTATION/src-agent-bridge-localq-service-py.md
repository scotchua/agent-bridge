# Mutation results: `src/agent_bridge/localq/service.py`

The temporary standard-library runner compiled one AST change at a time in a subprocess. It preloaded the mutated service module before the selected offline tests imported it. It never wrote to the source file. Each subprocess had a 12-second timeout. Mutants in comments, logging, and docstrings were excluded.

| Phase | Generated | Killed | Survived | Timed out | Kill rate |
| --- | ---: | ---: | ---: | ---: | ---: |
| Before new tests | 56 | 28 | 28 | 0 | 50.0% |
| After new tests | 56 | 56 | 0 | 0 | 100.0% |

The existing-test baseline ran `RuntimeTests.test_service_once_writes_atomic_restartable_metadata_without_inference` and `ConfigAndSelectionTests.test_selected_backend_is_fixed_and_summarize_only_by_default`. The after run added `ServiceMutationTests` from `tests/test_localq_service_mutation.py`. Each new test passed on the original and failed independently on the recorded mutant shown below.

| New test | Mutant killed independently |
| --- | ---: |
| `test_atomic_state_creates_parent_and_writes_json` | M01 |
| `test_atomic_state_requests_private_parent_permissions` | M02 |
| `test_atomic_state_flushes_before_fsync` | M06 |
| `test_atomic_state_cleans_temporary_file_after_replace_error` | M09 |
| `test_service_init_rejects_truthy_nonstring_backend_id` | M11 |
| `test_main_once_requires_options_and_returns_integer_zero` | M51 |
| `test_main_rejects_zero_and_negative_intervals` | M45 |
| `test_main_accepts_positive_fractional_interval_and_sleeps` | M46 |
| `test_main_default_interval_runs_between_service_calls` | M42 |

## Mutant ledger

Mutations cover every function: `atomic_state`, `Service.__init__`, `Service.for_config`, `Service.once`, and `main`. Applicable operators were statement removal, dropped raise, condition negation, `and`/`or` swap, `<=` to `<`, numeric constant increment, and return replacement with `None`, empty dict, or `False`. There is no arithmetic `+` or `-` expression in this source to swap.

| ID | Function:line | Change | Before | After |
| --- | --- | --- | --- | --- |
| M01 | `atomic_state:20` | remove statement | survived | killed |
| M02 | `atomic_state:20` | 448->449 | survived | killed |
| M03 | `atomic_state:21` | remove statement | killed | killed |
| M04 | `atomic_state:24` | remove statement | killed | killed |
| M05 | `atomic_state:25` | remove statement | survived | killed |
| M06 | `atomic_state:26` | remove statement | survived | killed |
| M07 | `atomic_state:27` | remove statement | killed | killed |
| M08 | `atomic_state:29` | negate condition | killed | killed |
| M09 | `atomic_state:30` | remove statement | survived | killed |
| M10 | `__init__:48` | negate condition | killed | killed |
| M11 | `__init__:48` | and/or swap | survived | killed |
| M12 | `__init__:49` | drop raise | survived | killed |
| M13 | `__init__:50` | remove statement | killed | killed |
| M14 | `__init__:51` | remove statement | killed | killed |
| M15 | `__init__:52` | remove statement | killed | killed |
| M16 | `__init__:52` | and/or swap | killed | killed |
| M17 | `__init__:53` | negate condition | killed | killed |
| M18 | `__init__:54` | remove statement | killed | killed |
| M19 | `__init__:56` | remove statement | killed | killed |
| M20 | `__init__:57` | remove statement | killed | killed |
| M21 | `__init__:58` | and/or swap | killed | killed |
| M22 | `__init__:60` | remove statement | killed | killed |
| M23 | `for_config:76` | remove statement | killed | killed |
| M24 | `for_config:77` | remove statement | killed | killed |
| M25 | `for_config:78` | remove statement | killed | killed |
| M26 | `for_config:79` | return None | killed | killed |
| M27 | `for_config:79` | return empty | killed | killed |
| M28 | `for_config:79` | return False | killed | killed |
| M29 | `once:85` | remove statement | killed | killed |
| M30 | `once:86` | remove statement | killed | killed |
| M31 | `once:86` | 1->2 | killed | killed |
| M32 | `once:89` | remove statement | killed | killed |
| M33 | `once:90` | return None | killed | killed |
| M34 | `once:90` | return empty | killed | killed |
| M35 | `once:90` | return False | killed | killed |
| M36 | `main:94` | remove statement | survived | killed |
| M37 | `main:95` | remove statement | survived | killed |
| M38 | `main:96` | remove statement | survived | killed |
| M39 | `main:97` | remove statement | survived | killed |
| M40 | `main:98` | remove statement | survived | killed |
| M41 | `main:99` | remove statement | survived | killed |
| M42 | `main:99` | 5.0->6.0 | survived | killed |
| M43 | `main:100` | remove statement | survived | killed |
| M44 | `main:101` | negate condition | survived | killed |
| M45 | `main:101` | LtE->Lt | survived | killed |
| M46 | `main:101` | 0->1 | survived | killed |
| M47 | `main:102` | remove statement | survived | killed |
| M48 | `main:103` | remove statement | survived | killed |
| M49 | `main:104` | negate condition | survived | killed |
| M50 | `main:105` | remove statement | survived | killed |
| M51 | `main:106` | return None | survived | killed |
| M52 | `main:106` | return empty | survived | killed |
| M53 | `main:106` | return False | survived | killed |
| M54 | `main:106` | 0->1 | survived | killed |
| M55 | `main:108` | remove statement | survived | killed |
| M56 | `main:109` | remove statement | survived | killed |

## Survivors left

None. Equivalent: 0. Untestable offline: 0. Defect: 0.

## Defects found

None.

## Verification

- `python3 tests/test_localq_service_mutation.py`: 9 passed.
- `python3 tests/test_suite.py` with an isolated temporary home: 592 passed, 0 failed, 5 environmental skips.
- `python3 tests/test_onboard.py` with an isolated temporary home: 28 passed.
- `python3 tests/test_local_worker.py` was attempted with an isolated temporary home; all 14 cases stopped in setup because this sandbox denied binding `127.0.0.1` (`PermissionError: [Errno 1] Operation not permitted`). This does not affect the mutation results.
