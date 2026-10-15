# Mutation test: `src/agent_bridge/localq/runtime.py`

The source was read and mutated only in copies under a temporary directory. Each mutant was run in a fresh Python process with an eight-second timeout against `tests/test_localq_runtime.py`. The source file remained byte-identical (SHA-256 `a9f29837d81affa956111626ae4c2aef2ae6007dcff2d760596df086a31fedd0`). No network or live inference was used.

| Result | Before new tests | After new tests |
| --- | ---: | ---: |
| Mutants generated | 30 | 30 |
| Killed | 21 | 30 |
| Survived | 9 | 0 |
| Kill rate | 70% | 100% |

Each row below is one executable-code mutation; `K` means killed and `S` means survived. Comments, docstrings, and logging were excluded. There is no executable `+` or `-` binary operator in this module to swap, so that operator family yielded no mutant.

| Function | Mutant (source line) | Before | After |
| --- | --- | :---: | :---: |
| `system_runner` | timeout 2 to 3 (24) | S | K |
| `system_runner` | return None (24) | S | K |
| `foundation_thermal_state` | negate process guard (39) | K | K |
| `foundation_thermal_state` | drop missing-process raise (40) | S | K |
| `foundation_thermal_state` | return None (42) | K | K |
| `thermal_level_name` | negate normal membership (53) | K | K |
| `thermal_level_name` | negate high membership (55) | K | K |
| `thermal_level_name` | return empty unknown state (57) | K | K |
| `MacSampler.__init__` | remove details initialization (75) | S | K |
| `MacSampler.__init__` | remove probe bindings (74) | K | K |
| `MacSampler._memory` | `>=` to `>` (82) | K | K |
| `MacSampler._memory` | drop invalid-output raise (81) | S | K |
| `MacSampler._memory` | return False for high pressure (83) | K | K |
| `MacSampler._ac_power` | return False (87) | K | K |
| `MacSampler._idle_seconds` | drop invalid-output raise (93) | S | K |
| `MacSampler._idle_seconds` | denominator plus one (94) | K | K |
| `MacSampler._idle_seconds` | return None (94) | K | K |
| `MacSampler._load` | drop invalid-output raise (100) | K | K |
| `MacSampler._load` | return None (101) | K | K |
| `MacSampler._cpu_idle` | negate no-match guard (107) | K | K |
| `MacSampler._cpu_idle` | denominator plus one (110) | K | K |
| `MacSampler._cpu_idle` | return None (110) | K | K |
| `MacSampler.sample` | negate thermal membership (121) | K | K |
| `MacSampler.sample` | drop invalid-thermal raise (122) | S | K |
| `MacSampler.sample` | `or` to `and` for core count (123) | K | K |
| `MacSampler.sample` | `<=` to `<` for core count (125) | S | K |
| `MacSampler.sample` | drop invalid-core raise (126) | S | K |
| `MacSampler.sample` | negate top-result guard (138) | K | K |
| `MacSampler.sample` | remove top fallback assignment (137) | K | K |
| `MacSampler.sample` | return None (139) | K | K |

## New tests

Each named test passed on the original module and failed when run alone against the listed recorded mutant.

| Function | New test in `tests/test_localq_runtime.py` | Mutant killed alone |
| --- | --- | --- |
| `system_runner` | `test_system_runner_returns_output_with_bounded_quiet_command` | timeout 2 to 3 |
| `foundation_thermal_state` | `test_foundation_thermal_probe_rejects_missing_process_info` | drop missing-process raise |
| `MacSampler.__init__` | `test_sampler_initializes_empty_details` | remove details initialization |
| `MacSampler._memory` | `test_memory_rejects_unrecognized_output` | drop invalid-output raise |
| `MacSampler._idle_seconds` | `test_idle_seconds_rejects_unrecognized_output` | drop invalid-output raise |
| `MacSampler.sample` | `test_sample_rejects_invalid_thermal_state` | drop invalid-thermal raise |
| `MacSampler.sample` | `test_sample_rejects_zero_core_count` | `<=` to `<` for core count |

The full relevant test module passed: 16 tests. The required `python3 tests/test_suite.py` run passed in an isolated temporary home: 592 passed, 0 failed, 5 environmental skips.

## Survivors left

None. There were no equivalent or offline-untestable survivors in this bounded set.

## Defects found

None.
