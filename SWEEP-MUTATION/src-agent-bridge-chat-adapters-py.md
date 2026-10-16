# Mutation sweep: `src/agent_bridge/chat/adapters.py`

## Scope and method

- Generated 39 one-change AST mutants across all eight `BridgeAdapter` methods (cap: 150). Mutations flip comparisons, swap `and`/`or`, negate conditions, increment integer constants, replace return values with `None`, `{}`, or `False`, remove statements, swap `+`/`-`, and drop raises. Comments, logging, and docstrings were excluded.
- Each mutant was compiled from an in-memory copy and executed in a fresh Python process with an eight-second timeout. The production file was never written. The child loaded the mutant into the adapter module before running the relevant existing launch test; its `build_app` was given an isolated adapter-only shim because binding a loopback server is unavailable in this sandbox. No provider calls or network requests were made.
- Original adapter SHA-256 before and after: `3074663383898ab5bc104356e53b430826f89298e907e104b0fc2cad1cf7a976`. The original passed the isolated existing test and all 11 new tests.

## Kill rates

| Stage | Killed | Generated | Kill rate |
| --- | ---: | ---: | ---: |
| Existing test only | 5 | 39 | 12.8% |
| Existing plus new tests | 39 | 39 | 100.0% |

## Mutants

| ID | Function | Line | Change | Before | After |
| --- | --- | ---: | --- | --- | --- |
| M001 | `__init__` | 8 | negate condition | killed | killed |
| M002 | `__init__` | 10 | remove statement | killed | killed |
| M003 | `__init__` | 11 | remove statement | survived | killed |
| M004 | `__init__` | 12 | remove statement | survived | killed |
| M005 | `__init__` | 9 | drop exception raise | survived | killed |
| M006 | `__init__` | 12 | swap + and - | survived | killed |
| M007 | `__init__` | 11 | flip comparison | survived | killed |
| M008 | `__init__` | 12 | change integer by one | survived | killed |
| M009 | `status` | 25 | return None | survived | killed |
| M010 | `status` | 25 | return empty dict | survived | killed |
| M011 | `status` | 20 | remove statement | survived | killed |
| M012 | `status` | 21 | negate condition | survived | killed |
| M013 | `status` | 18 | return None | survived | killed |
| M014 | `status` | 18 | return empty dict | survived | killed |
| M015 | `status` | 21 | flip comparison | survived | killed |
| M016 | `status` | 22 | return None | survived | killed |
| M017 | `status` | 22 | return empty dict | survived | killed |
| M018 | `status` | 24 | return None | survived | killed |
| M019 | `status` | 24 | return empty dict | survived | killed |
| M020 | `_admit` | 28 | remove statement | survived | killed |
| M021 | `_admit` | 29 | negate condition | survived | killed |
| M022 | `_admit` | 29 | flip comparison | survived | killed |
| M023 | `_admit` | 30 | drop exception raise | survived | killed |
| M024 | `_check_local_first` | 33 | negate condition | killed | killed |
| M025 | `_check_local_first` | 33 | swap and/or | survived | killed |
| M026 | `_check_local_first` | 34 | drop exception raise | killed | killed |
| M027 | `_check_local_first` | 33 | flip comparison | survived | killed |
| M028 | `start` | 37 | remove statement | killed | killed |
| M029 | `start` | 38 | remove statement | survived | killed |
| M030 | `start` | 39 | return None | survived | killed |
| M031 | `start` | 39 | return False | survived | killed |
| M032 | `continue_` | 42 | remove statement | survived | killed |
| M033 | `continue_` | 43 | remove statement | survived | killed |
| M034 | `continue_` | 44 | return None | survived | killed |
| M035 | `continue_` | 44 | return False | survived | killed |
| M036 | `poll` | 47 | return None | survived | killed |
| M037 | `poll` | 47 | return False | survived | killed |
| M038 | `read` | 50 | return None | survived | killed |
| M039 | `read` | 50 | return False | survived | killed |

## New tests

All are in `tests/test_chat_adapters.py`. Each passes on the original and fails on the named recorded mutant when run alone:

- `test_init_rejects_unknown_target_and_maps_peer_timeout` → M003
- `test_status_ready_requires_valid_evidence_and_signed_in_peer` → M009
- `test_status_invalid_evidence_requires_verification` → M013
- `test_status_unsigned_peer_requires_login` → M016
- `test_status_preflight_failure_is_failed` → M018
- `test_admit_refuses_nonready_status_with_detail` → M021
- `test_check_local_first_uses_enabled_byte_threshold` → M025
- `test_start_checks_admission_and_forwards_payload` → M029
- `test_continue_checks_admission_and_forwards_conversation` → M033
- `test_poll_forwards_job_and_result` → M036
- `test_read_forwards_job_and_result` → M038

## Survivors left

None. Equivalent: none; untestable offline: none; defect: none.

## Defects found

None.

## Verification

- `python3 -m unittest discover -s tests -p test_chat_adapters.py`: 11 passed.
- `python3 tests/test_suite.py`: 593 passed, 0 failed, 5 environmental skips.
- `python3 tests/test_onboard.py`: exited successfully.
- `python3 tests/test_local_worker.py`: blocked in this sandbox; all 14 cases fail in setup because loopback socket binding raises `PermissionError`. No adapter behavior is involved.
