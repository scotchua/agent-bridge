# Claude backend mutation sweep

Target: `src/agent_bridge/backends/claude_backend.py` (SHA-256 before and after: `21544930b0a69a27926cda0ff6e22fe3fa82ae1dca44888fc47013dc0274156a`).

The temporary runner compiled one changed source string at a time into the already imported module in a fresh Python process. It never wrote the target. Each focused test run had a 12-second limit, an isolated temporary home, and no live peer or network call. Mutations were selected from executable statements only; comments, logging, and docstrings were skipped. The target has no explicit `raise` statement to drop. `M35` changes its `+=` operation to `-=`. The first phase ran the existing `test_health.ClassificationTests` and `test_security_boundaries.ClaudeExitBoundaryTests`; the second added the new classes in `tests/test_health.py`.

| Measure | Existing tests | Existing and new tests |
| --- | ---: | ---: |
| Mutants generated | 35 | 35 |
| Killed | 14 | 35 |
| Survived | 21 | 0 |
| Kill rate | 40.0% | 100.0% |

| ID | Function | One changed behavior | Before | After |
| --- | --- | --- | --- | --- |
| M01 | `_executable` | Ignore executable discovery | survived | killed |
| M02 | `_executable` | Remove command-name fallback | survived | killed |
| M03 | `new_session_id` | Return `None` | survived | killed |
| M04 | `build_argv` | Negate resume condition | survived | killed |
| M05 | `build_argv` | Negate model condition | survived | killed |
| M06 | `build_argv` | Remove model-argument statement | survived | killed |
| M07 | `build_argv` | Negate effort condition | killed | killed |
| M08 | `build_argv` | Include budget only when absent | survived | killed |
| M09 | `build_argv` | Remove safe-mode flag | survived | killed |
| M10 | `_extract_payload` | Negate structured-object type check | killed | killed |
| M11 | `_extract_payload` | Negate result-object type check | killed | killed |
| M12 | `_extract_payload` | Change string-and-content test to OR | survived | killed |
| M13 | `_extract_payload` | Invert parsed-result presence test | survived | killed |
| M14 | `_extract_payload` | Return empty object for missing payload | survived | killed |
| M15 | `run_consultation` | Negate spawn-failure condition | killed | killed |
| M16 | `run_consultation` | Negate timeout condition | killed | killed |
| M17 | `run_consultation` | Require both cap signals | survived | killed |
| M18 | `run_consultation` | Accept output exactly at cap | survived | killed |
| M19 | `run_consultation` | Negate incomplete-pipe condition | killed | killed |
| M20 | `run_consultation` | Invert envelope type check | killed | killed |
| M21 | `run_consultation` | Treat matching session as migrated | killed | killed |
| M22 | `run_consultation` | Invert single-model count test | survived | killed |
| M23 | `run_consultation` | Invert cost-presence note | survived | killed |
| M24 | `run_consultation` | Treat `is_error=False` as failure | killed | killed |
| M25 | `run_consultation` | Require both exhaustion markers | killed | killed |
| M26 | `run_consultation` | Invert success exit-status test | killed | killed |
| M27 | `run_consultation` | Invert session-migration guard | killed | killed |
| M28 | `run_consultation` | Invert missing-session guard | killed | killed |
| M29 | `run_consultation` | Invert missing-payload guard | killed | killed |
| M30 | `run_consultation` | Clear accepted payload | survived | killed |
| M31 | `run_consultation` | Clear extraction path | survived | killed |
| M32 | `run_consultation` | Change single-model threshold from 1 to 2 | survived | killed |
| M33 | `run_consultation` | Increase default timeout by 1 | survived | killed |
| M34 | `run_consultation` | Increase default grace by 1 | survived | killed |
| M35 | `build_argv` | Swap resume-argument `+=` for `-=` | survived | killed |

## New tests

Grouped by the function they exercise in `tests/test_health.py`. Each passed on the original and failed on at least the listed mutant when run alone:

| Function | New test | Recorded mutant killed alone |
| --- | --- | --- |
| `_executable` | `test_executable_uses_pin_then_discovery_then_name` | M01 |
| `new_session_id` | `test_new_session_id_is_a_canonical_uuid` | M03 |
| `build_argv` | `test_resume_and_fresh_session_flags_are_distinct` | M04 |
| `build_argv` | `test_model_and_zero_budget_are_passed` | M05 |
| `build_argv` | `test_isolation_flags_are_present` | M09 |
| `_extract_payload` | `test_json_string_result_is_extracted` | M13 |
| `_extract_payload` | `test_missing_payload_stays_missing` | M12 |
| `run_consultation` | `test_cap_flag_and_exact_limit_reject_output` | M17 |
| `run_consultation` | `test_default_timeout_and_grace_reach_runner` | M33 |
| `run_consultation` | `test_single_and_multiple_models_and_zero_cost_are_recorded` | M22 |
| `run_consultation` | `test_success_keeps_payload_and_extraction_path` | M30 |

## Survivors left

None. Equivalent: none. Untestable offline: none.

## Defects found

None.

## Verification

`python3 -B -m unittest discover -s tests -p test_health.py` passed (28 tests). `python3 tests/test_suite.py` passed with an isolated temporary home; its five process-group enumeration checks were skipped by the platform. The throwaway runner and intermediate result files were removed after this report was written.
