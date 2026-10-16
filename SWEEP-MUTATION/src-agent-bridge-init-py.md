# Mutation sweep: `src/agent_bridge/__init__.py`

The original module was copied to an isolated temporary package for each mutant. Each subprocess ran `test_suite.test_platform_guard`, with a 20-second limit and an isolated temporary home. The after run also ran `test_suite.test_assert_platform_supported_return`. No production file was edited. The source SHA-256 before and after both runs was `21f06be3e2bc72003d27de948340ae634df07fae6c9f29900259de12962768a6`.

| Measure | Before | After |
| --- | ---: | ---: |
| Mutants generated | 12 | 12 |
| Killed | 9 | 11 |
| Survived | 3 | 1 |
| Kill rate, all mutants | 75.0% | 91.7% |
| Kill rate, excluding the equivalent mutant | 81.8% | 100.0% |

The executable code has no comparison operators from `<`, `<=`, `>`, `>=`, `==`, or `!=`, no `and`/`or`, no numeric constants, and no `+`/`-` operations. Applicable condition, return, statement removal, and exception mutations were used. No comment, logging, or docstring was mutated. Both functions and the import-time call were covered.

| ID | Function or site | Mutation | Before | After |
| --- | --- | --- | --- | --- |
| M01 | `platform_supported` | Negate membership condition | Killed | Killed |
| M02 | `platform_supported` | Replace return with `None` | Killed | Killed |
| M03 | `platform_supported` | Replace return with `False` | Killed | Killed |
| M04 | `platform_supported` | Replace return with `()` | Killed | Killed |
| M05 | `assert_platform_supported` | Negate guard condition | Killed | Killed |
| M06 | `assert_platform_supported` | Replace bare return with `return None` | Survived | Survived |
| M07 | `assert_platform_supported` | Replace bare return with `return False` | Survived | Killed |
| M08 | `assert_platform_supported` | Replace bare return with `return ()` | Survived | Killed |
| M09 | `assert_platform_supported` | Remove guarded return statement | Killed | Killed |
| M10 | `assert_platform_supported` | Remove requirements assignment | Killed | Killed |
| M11 | `assert_platform_supported` | Drop exception raise | Killed | Killed |
| M12 | Package import | Remove import-time guard call | Killed | Killed |

## New test

`test_assert_platform_supported_return` checks that the guard returns `None` on both supported OS names. It passes on the original and fails on M07 and M08.

## Survivors left

M06 is equivalent: a bare `return` and `return None` have the same result and side effects.

## Defects found

None.

## Verification

`python3 tests/test_suite.py` passed with 594 checks, 0 failures, and 5 environment-dependent skips. `test_onboard.py` passed 28 tests. `test_local_worker.py` was exercised, but all 14 tests stopped during fixture setup because the sandbox refused a localhost socket bind (`PermissionError: [Errno 1] Operation not permitted`).
