# Mutation sweep: `src/agent_bridge/token_usage.py`

## Method and results

- Generated 35 single-edit AST mutants (150 maximum), with candidates in `_value`, `claude_model_usage`, and `codex_turn_usage`. Comments and docstrings were excluded. The module has no executable numeric constants, `+`/`-` operations, or `raise` statements, so those mutation types had no candidates.
- Each mutant was compiled and loaded from a temporary file in its own Python subprocess. `tests/test_token_usage.py` ran with a five-second timeout per mutant. No mutant timed out or was invalid. The production module was read only and remained byte-identical after every run.
- Before the new tests: **32/35 killed (91.4%)**; three survived. After the new tests: **34/35 killed (97.1%)**; one survived.
- Excluding the one equivalent mutant, behavioral kill rates were **32/34 (94.1%) before** and **34/34 (100%) after**.
- Original module SHA-256 after the sweep: `c0d69ae48901f19e60dd0a66eb65f9eecc08952ea6792028f5995d2dd96df93a`.

## Mutants generated

| ID | Function:line | Edit | Before | After |
| --- | --- | --- | --- | --- |
| M001 | `_value:12` | replace return value with `None` | survived | survived |
| M002 | `_value:12` | replace return value with `{}` | killed | killed |
| M003 | `_value:12` | replace return value with `False` | killed | killed |
| M004 | `claude_model_usage:17` | negate condition | killed | killed |
| M005 | `claude_model_usage:19` | remove statement | killed | killed |
| M006 | `claude_model_usage:45` | replace return value with `None` | killed | killed |
| M007 | `claude_model_usage:45` | replace return value with `{}` | killed | killed |
| M008 | `claude_model_usage:45` | replace return value with `False` | killed | killed |
| M009 | `codex_turn_usage:51` | remove statement | killed | killed |
| M010 | `codex_turn_usage:80` | replace return value with `None` | killed | killed |
| M011 | `codex_turn_usage:80` | replace return value with `{}` | killed | killed |
| M012 | `codex_turn_usage:80` | replace return value with `False` | killed | killed |
| M013 | `_value:10` | negate condition | killed | killed |
| M014 | `claude_model_usage:17` | swap `or` for `and` | survived | killed |
| M015 | `claude_model_usage:18` | replace return value with `None` | killed | killed |
| M016 | `claude_model_usage:18` | replace return value with `{}` | killed | killed |
| M017 | `claude_model_usage:18` | replace return value with `False` | killed | killed |
| M018 | `claude_model_usage:21` | remove statement | killed | killed |
| M019 | `claude_model_usage:22` | remove statement | survived | killed |
| M020 | `claude_model_usage:23` | remove statement | killed | killed |
| M021 | `claude_model_usage:38` | remove statement | killed | killed |
| M022 | `claude_model_usage:40` | remove statement | killed | killed |
| M023 | `claude_model_usage:44` | remove statement | killed | killed |
| M024 | `codex_turn_usage:53` | negate condition | killed | killed |
| M025 | `codex_turn_usage:55` | remove statement | killed | killed |
| M026 | `codex_turn_usage:56` | negate condition | killed | killed |
| M027 | `codex_turn_usage:61` | remove statement | killed | killed |
| M028 | `codex_turn_usage:77` | remove statement | killed | killed |
| M029 | `_value:11` | replace return value with `None` | killed | killed |
| M030 | `_value:11` | replace return value with `{}` | killed | killed |
| M031 | `_value:11` | replace return value with `False` | killed | killed |
| M032 | `claude_model_usage:22` | negate condition | killed | killed |
| M033 | `claude_model_usage:41` | negate condition | killed | killed |
| M034 | `codex_turn_usage:53` | flip `!=` to `==` | killed | killed |
| M035 | `codex_turn_usage:57` | remove statement | killed | killed |

## Surviving mutants

- **M001, equivalent:** `_value` already returns `None` when no name matches; replacing that return value with `None` leaves behavior unchanged. No test is appropriate.
- No behavioral survivors, untestable-offline survivors, or defect survivors remain.

## New tests

- `ClaudeUsageTests.test_nonmapping_usage_is_absent` kills M014 (`or` to `and`) with a nonempty nonmapping input.
- `ClaudeUsageTests.test_nonmapping_model_usage_has_missing_fields` kills M019 (removal of per-model mapping normalization) with an integer model value.
- Each test was run alone: it passed on the original module and failed on its named mutant. All nine token usage tests pass on the original module.

## Defects found

None.

## Verification and limits

- `python3 tests/test_suite.py`: 593 passed, 0 failed, 5 platform skips.
- `python3 tests/test_onboard.py`: 28 passed.
- `python3 tests/test_local_worker.py`: 14 setup errors because this sandbox denies binding a loopback socket for its local mock HTTP server.
- The standalone `test_claude_task.py` and `test_codex_task.py` files have baseline failures because this sandbox denies `sandbox-exec`; their outcomes were not counted as mutation kills. The requested full offline suite passed.
- No network API, QuickBooks, or Intuit service was contacted.
