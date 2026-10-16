# Mutation sweep: `src/agent_bridge/upgrade.py`

## Method and result

The offline sweep used 28 single-site AST mutants, including at least one in each of the module's 23 functions. It exercised comparison flips, `and`/`or` swaps, removed negations, a numeric increment, return replacement with `None`, empty, or `False`, statement removal, `+`/`-` swap, and a dropped raise. AST selection excluded comments, logging, and docstrings. Each mutant was compiled from a temporary copy and run in a fresh process against `tests/test_upgrade.py` with an isolated temporary home and a 15-second limit. The production module was never rewritten. The before run excluded the six new test methods; the after run included them.

| Measure | Killed | Generated | Kill rate |
| --- | ---: | ---: | ---: |
| Existing tests | 21 | 28 | 75.0% |
| With new tests | 28 | 28 | 100.0% |

| ID | Function and source line | Mutation | Before | After |
| --- | --- | --- | --- | --- |
| M01 | `_base`:24 | return `None` | killed | killed |
| M02 | `_upgrade_path`:28 | return `None` | killed | killed |
| M03 | `_read_json`:32 | return `None` | killed | killed |
| M04 | `_exists`:39 | return `None` | killed | killed |
| M05 | `_state`:47 | return `None` | killed | killed |
| M06 | `_valid_manifest`:53 | `>=` to `>` | survived | killed |
| M07 | `_delegation_document`:67 | remove `not` | killed | killed |
| M08 | `launcher_path`:79 | `==` to `!=` | survived | killed |
| M09 | `_launcher_args`:91 | `!=` to `==` | killed | killed |
| M10 | `_launcher_companions`:114 | `==` to `!=` | survived | killed |
| M11 | `detect`:173 | `==` to `!=` | survived | killed |
| M12 | `_previous_decisions`:226 | return `None` | killed | killed |
| M13 | `_yes`:232 | return `None` | killed | killed |
| M14 | `_yes_default`:236 | `or` to `and` | killed | killed |
| M15 | `_card`:240 | remove heading output | survived | killed |
| M16 | `_cmd_quote`:256 | `or` to `and` | killed | killed |
| M17 | `launcher_bytes`:268 | return `None` | killed | killed |
| M18 | `_sha256`:273 | return `None` | killed | killed |
| M19 | `_inside_base`:281 | `==` to `!=` | killed | killed |
| M20 | `_commit`:309 | `!=` to `==` | killed | killed |
| M21 | `undo`:366 | `!=` to `==` | killed | killed |
| M22 | `run`:384 | `==` to `!=` | killed | killed |
| M23 | `main`:548 | `==` to `!=` | survived | killed |
| M24 | `_launcher_args`:91 | `2` to `3` | killed | killed |
| M25 | `_launcher_companions`:115 | `+` to `-` | survived | killed |
| M26 | `_cmd_quote`:257 | drop `raise` | killed | killed |
| M27 | `_state`:47 | return empty dict | killed | killed |
| M28 | `_yes`:232 | return `False` | killed | killed |

## New tests

Each listed test passed on the original source and failed when run alone against the matching recorded mutant:

| Test method in `tests/test_upgrade.py` | Mutant killed |
| --- | --- |
| `test_valid_manifest_accepts_exact_required_keys` | M06 |
| `test_launcher_path_uses_mac_suffix` | M08 |
| `test_launcher_companions_extracts_exact_hermes_path` | M10, M25 |
| `test_detect_certified_gemma_backend` | M11 |
| `test_card_prints_heading` | M15 |
| `test_main_refuses_root` | M23 |

Survivors left: **none**. Equivalent mutants: **none**. Untestable offline mutants: **none**. Defects found: **none**.

The source SHA-256 before and after the sweep was `4ac2bf6be10d31b3541dd3dcd5185440c7b2567cffc3c5b753e05477dec222c6`.

## Verification

- `python3 tests/test_upgrade.py`: 25 tests passed.
- `python3 tests/test_onboard.py`: 28 tests passed.
- `python3 tests/test_suite.py`: 592 checks passed, 0 failed, 5 platform-related skips.
- `python3 tests/test_local_worker.py`: blocked before test bodies because the sandbox refused the test server's loopback socket bind (`PermissionError: [Errno 1] Operation not permitted`).
