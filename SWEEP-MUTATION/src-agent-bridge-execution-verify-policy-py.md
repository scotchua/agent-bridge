# Mutation sweep: `src/agent_bridge/execution/verify_policy.py`

## Method

- Generated 66 distinct, one-change AST mutants across all three functions; each ran against `test_verify_policy.py` and `test_verify_runnable.py` with a 12-second limit.
- The runner wrote each mutant to a temporary module and injected it into the offline unittest process. It never wrote to the production source. Comments, logging, and docstrings were excluded.
- Operators used where present: comparison flips, `and`/`or` swaps, condition negation, integer changes of one, replacement of return values, removal of one statement, and removal of exception raises. There is no `+` or `-` binary expression in this module to swap.
- Original source SHA-256 before and after: `54ce3c4d4c9cc78ed74684e2901f1ee4ce8005a9878a183faa8aa635bff2940a`.

## Results

| Scope | Generated | Killed before | Before kill rate | Killed after | After kill rate |
| --- | ---: | ---: | ---: | ---: | ---: |
| `_bare_name` | 6 | 5 | 83.3% | 6 | 100.0% |
| `check_verify_argv` | 46 | 41 | 89.1% | 46 | 100.0% |
| `check_runnable` | 14 | 14 | 100.0% | 14 | 100.0% |
| **Total** | 66 | 60 | 90.9% | 66 | 100.0% |

## Surviving mutants before new tests

| Mutant | Behavioral gap | New test that kills it |
| --- | --- | --- |
| M004 | `and` to `or` admits names containing `/` or `\` in `_bare_name`. | `BareNameTests.test_paths_are_not_bare_names_on_either_platform` |
| M008, M013 | Removing the outer-list check or its raise accepts a tuple of valid commands. | `CheckVerifyArgvBoundaryTests.test_outer_container_must_be_a_list` |
| M039, M043 | Raising the Python argument minimum rejects valid three-part `python3 -m pytest` and `python3 -m unittest` commands. | `CheckVerifyArgvBoundaryTests.test_python_module_boundary` |
| M042 | Lowering the Python argument minimum causes `IndexError` for `python3 -m`. | `CheckVerifyArgvBoundaryTests.test_python_module_boundary` |

Each new test passed on the original and failed individually on at least one named mutant. After the tests, **no mutants survived**. Equivalent: none observed. Untestable offline: none among these mutants. Defects found: none. Expected failures added: none.

## Verification

- `python3 -m unittest discover -s tests -p test_verify*.py`: 15 passed.
- `python3 tests/test_suite.py` with an isolated temporary home: 592 passed, 0 failed, 5 platform skips.
- `python3 tests/test_onboard.py`: 28 passed.
- `python3 tests/test_local_worker.py`: 14 setup errors because sandbox policy denied binding a loopback socket (`PermissionError: [Errno 1] Operation not permitted`); these tests did not reach the policy under test.

## Mutant inventory

| ID | Function:line | Mutation | Before | After |
| --- | --- | --- | --- | --- |
| M001 | `_bare_name:42` | return None | killed | killed |
| M002 | `_bare_name:42` | return [] | killed | killed |
| M003 | `_bare_name:42` | return False | killed | killed |
| M004 | `_bare_name:42` | swap and/or | survived | killed |
| M005 | `_bare_name:42` | compare 0 Eq | killed | killed |
| M006 | `_bare_name:43` | compare 0 Eq | killed | killed |
| M007 | `check_verify_argv:54` | negate if | killed | killed |
| M008 | `check_verify_argv:54` | remove If | survived | killed |
| M009 | `check_verify_argv:56` | remove For | killed | killed |
| M010 | `check_verify_argv:71` | return None | killed | killed |
| M011 | `check_verify_argv:71` | return [] | killed | killed |
| M012 | `check_verify_argv:71` | return False | killed | killed |
| M013 | `check_verify_argv:55` | drop raise | survived | killed |
| M014 | `check_verify_argv:57` | negate if | killed | killed |
| M015 | `check_verify_argv:57` | remove If | killed | killed |
| M016 | `check_verify_argv:60` | remove Assign | killed | killed |
| M017 | `check_verify_argv:61` | negate if | killed | killed |
| M018 | `check_verify_argv:61` | remove If | killed | killed |
| M019 | `check_verify_argv:63` | negate if | killed | killed |
| M020 | `check_verify_argv:63` | remove If | killed | killed |
| M021 | `check_verify_argv:67` | negate if | killed | killed |
| M022 | `check_verify_argv:67` | remove If | killed | killed |
| M023 | `check_verify_argv:69` | negate if | killed | killed |
| M024 | `check_verify_argv:69` | remove If | killed | killed |
| M025 | `check_verify_argv:57` | swap and/or | killed | killed |
| M026 | `check_verify_argv:59` | drop raise | killed | killed |
| M027 | `check_verify_argv:61` | swap and/or | killed | killed |
| M028 | `check_verify_argv:62` | drop raise | killed | killed |
| M029 | `check_verify_argv:63` | swap and/or | killed | killed |
| M030 | `check_verify_argv:66` | drop raise | killed | killed |
| M031 | `check_verify_argv:67` | swap and/or | killed | killed |
| M032 | `check_verify_argv:68` | drop raise | killed | killed |
| M033 | `check_verify_argv:70` | drop raise | killed | killed |
| M034 | `check_verify_argv:60` | integer -1 | killed | killed |
| M035 | `check_verify_argv:60` | integer +1 | killed | killed |
| M036 | `check_verify_argv:64` | swap and/or | killed | killed |
| M037 | `check_verify_argv:67` | compare 0 Eq | killed | killed |
| M038 | `check_verify_argv:67` | swap and/or | killed | killed |
| M039 | `check_verify_argv:64` | compare 0 Lt | survived | killed |
| M040 | `check_verify_argv:64` | compare 0 NotEq | killed | killed |
| M041 | `check_verify_argv:67` | compare 0 Lt | killed | killed |
| M042 | `check_verify_argv:64` | integer -1 | survived | killed |
| M043 | `check_verify_argv:64` | integer +1 | survived | killed |
| M044 | `check_verify_argv:67` | integer -1 | killed | killed |
| M045 | `check_verify_argv:67` | integer +1 | killed | killed |
| M046 | `check_verify_argv:58` | swap and/or | killed | killed |
| M047 | `check_verify_argv:64` | integer -1 | killed | killed |
| M048 | `check_verify_argv:64` | integer +1 | killed | killed |
| M049 | `check_verify_argv:65` | integer -1 | killed | killed |
| M050 | `check_verify_argv:65` | integer +1 | killed | killed |
| M051 | `check_verify_argv:67` | integer -1 | killed | killed |
| M052 | `check_verify_argv:67` | integer +1 | killed | killed |
| M053 | `check_runnable:92` | remove Assign | killed | killed |
| M054 | `check_runnable:94` | remove For | killed | killed |
| M055 | `check_runnable:95` | remove Assign | killed | killed |
| M056 | `check_runnable:96` | negate if | killed | killed |
| M057 | `check_runnable:96` | remove If | killed | killed |
| M058 | `check_runnable:100` | negate if | killed | killed |
| M059 | `check_runnable:100` | remove If | killed | killed |
| M060 | `check_runnable:96` | compare 0 Eq | killed | killed |
| M061 | `check_runnable:97` | remove Continue | killed | killed |
| M062 | `check_runnable:100` | swap and/or | killed | killed |
| M063 | `check_runnable:101` | drop raise | killed | killed |
| M064 | `check_runnable:95` | integer -1 | killed | killed |
| M065 | `check_runnable:95` | integer +1 | killed | killed |
| M066 | `check_runnable:93` | swap and/or | killed | killed |
