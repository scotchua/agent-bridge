# Mutation sweep: `src/agent_bridge/backends/base.py`

## Method

The temporary runner parsed the complete module with Python's standard-library `ast`, changed one site at a time, and executed the resulting module from a temporary file in a fresh Python process. The source file was never rewritten. Each subprocess had a 12-second timeout. The baseline was the relevant `test_health.ClassificationTests` cases and the three direct JSON checks from `test_suite.py` F7. The after pass added `tests/test_base_mutation.py`. No packages or network services were used.

The 31 selected mutants cover all five functions. They flip comparisons, swap `and`/`or`, negate conditions, increment numeric constants, replace return values with `None`, `False`, or an empty list, and remove one statement. There were no `+`/`-` binary expressions or exception raises in these functions, so those operators had no eligible site. Comments, logging, and docstrings were excluded.

Source SHA-256 before and after every mutant: `28642a21fac6dd7e1f1828ce2095264ef35abbb39c420c29ec492f8d2b4ce960`.

## Results

| Measure | Before | After |
| --- | ---: | ---: |
| Mutants generated | 31 | 31 |
| Killed | 17 | 28 |
| Survived | 14 | 3 |
| Kill rate, all mutants | 54.8% | 90.3% |
| Kill rate, behavior-changing mutants | 60.7% (17/28) | 100% (28/28) |

| ID | Function:line | Change | Before | After |
| --- | --- | --- | --- | --- |
| M001 | `auth_failure_reason:58` | remove statement | killed | killed |
| M002 | `auth_failure_reason:59` | `and` to `or` | survived | killed |
| M003 | `auth_failure_reason:61` | negate condition | killed | killed |
| M004 | `auth_failure_reason:62` | return `None` | killed | killed |
| M005 | `auth_failure_reason:63` | return `None` | survived | survived |
| M006 | `looks_like_auth_failure:67` | return `None` | survived | killed |
| M007 | `looks_like_auth_failure:67` | return `False` | survived | killed |
| M008 | `strip_fences:72` | remove statement | killed | killed |
| M009 | `strip_fences:73` | negate condition | killed | killed |
| M010 | `strip_fences:74` | return `None` | killed | killed |
| M011 | `strip_fences:75` | remove statement | killed | killed |
| M012 | `strip_fences:76` | negate condition | killed | killed |
| M013 | `strip_fences:76` | `<` to `>=` | killed | killed |
| M014 | `strip_fences:76` | `2` to `3` | survived | killed |
| M015 | `strip_fences:77` | return `None` | survived | killed |
| M016 | `strip_fences:78` | remove statement | killed | killed |
| M017 | `strip_fences:78` | `1` to `2` | killed | killed |
| M018 | `strip_fences:79` | `and` to `or` | killed | killed |
| M019 | `strip_fences:79` | index `-1` to `-2` | killed | killed |
| M020 | `strip_fences:81` | return `None` | killed | killed |
| M021 | `parse_single_object:95` | remove statement | killed | killed |
| M022 | `parse_single_object:96` | negate condition | killed | killed |
| M023 | `parse_single_object:97` | return `None` | survived | survived |
| M024 | `parse_single_object:101` | return `None` | survived | survived |
| M025 | `parse_single_object:102` | return `None` | killed | killed |
| M026 | `argv_shape:119` | negate condition | survived | killed |
| M027 | `argv_shape:119` | `>` to `<=` | survived | killed |
| M028 | `argv_shape:120` | remove statement | survived | killed |
| M029 | `argv_shape:122` | remove statement | survived | killed |
| M030 | `argv_shape:123` | return `None` | survived | killed |
| M031 | `argv_shape:123` | return empty list | survived | killed |

## New tests

| Test | Mutants it kills |
| --- | --- |
| `AuthFailureReasonTests.test_ignores_non_text_diagnostics` | M002 |
| `LooksLikeAuthFailureTests.test_returns_true_for_auth_diagnostic` | M006, M007 |
| `StripFencesTests.test_two_line_fence_and_unclosed_single_line` | M014, M015 |
| `ArgvShapeTests.test_preserves_short_values_and_summarizes_long_values` | M026–M031 |

Each test passed on the original module and failed when run alone against its representative recorded mutant (M002, M006, M014, and M026 respectively).

## Survivors left

M005, M023, and M024 are equivalent: each replaces an existing `return None` with `return None`. There are no behavior-changing survivors, untestable offline survivors, or expected failures.

## Defects found

None.

## Verification

`python3 -m unittest discover -s tests -p test_base_mutation.py`: 4 passed. `python3 tests/test_suite.py`: 593 passed, 0 failed, 5 environmental skips. `python3 tests/test_onboard.py`: 28 passed. `python3 tests/test_local_worker.py`: 14 fixture setup errors because this sandbox denies binding `127.0.0.1`; no test body ran.
