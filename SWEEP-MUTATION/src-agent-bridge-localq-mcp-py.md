# Mutation sweep: `src/agent_bridge/localq/mcp.py`

The original module imported and ran offline. The sweep compiled one AST mutant at a time in a fresh subprocess, replacing only the in-memory module object. Each run used the relevant existing MCP tests and an 8-second timeout. The source file was byte-identical before and after the sweep. No external service or package was used.

| Measure | Existing tests | Existing and new tests |
| --- | ---: | ---: |
| Mutants generated | 32 | 32 |
| Killed | 20 | 32 |
| Kill rate | 62.5% | 100% |
| Survived | 12 | 0 |
| Timed out | 0 | 0 |

The 32 mutants cover `_schema`, `build_tools`, its nested `call`, `build_intake_tools`, and its nested `call`. The module has no comparison operators, arithmetic `+` or `-`, or explicit `raise` statements eligible for the other requested operators. Comments, docstrings, and logging were excluded.

| Mutants | Change | Before | After |
| --- | --- | --- | --- |
| M01–M03 | `_schema` return replaced by `None`, `{}`, or `False` | Killed | Killed |
| M04–M06 | Remove one `build_tools` schema assignment at lines 17, 26, or 27 | Killed | Killed |
| M07–M09 | `build_tools` return replaced by `None`, `{}`, or `False` | Killed | Killed |
| M10–M11 | Remove one `build_intake_tools` schema assignment at line 53 or 72 | Killed | Killed |
| M12–M14 | `build_intake_tools` return replaced by `None`, `{}`, or `False` | Killed | Killed |
| M15–M17 | Queue handler success return replaced by `None`, `{}`, or `False` | Killed | Killed |
| M18–M20 | Intake handler success return replaced by `None`, `{}`, or `False` | Killed | Killed |
| M21–M23 | Queue handler error return replaced by `None`, `{}`, or `False` | Survived | Killed |
| M24–M26 | Intake handler error return replaced by `None`, `{}`, or `False` | Survived | Killed |
| M27–M28 | Submit input `maxLength` changed from 24000 to 23999 or 24001 | Survived | Killed |
| M29–M30 | Route input `maxLength` changed from 24000 to 23999 or 24001 | Survived | Killed |
| M31 | Queue handler error fallback `or` changed to `and` | Survived | Killed |
| M32 | Intake handler error fallback `or` changed to `and` | Survived | Killed |

New tests, grouped by adapter function:

- `build_tools`: `McpQueueHandlerTests.test_submit_input_schema_has_exact_limit` kills M27–M28; `McpQueueHandlerTests.test_queue_handler_preserves_named_and_unnamed_errors` kills M21–M23 and M31.
- `build_intake_tools`: `McpIntakeHandlerTests.test_route_input_schema_has_exact_limit` kills M29–M30; `McpIntakeHandlerTests.test_intake_handler_preserves_named_and_unnamed_errors` kills M24–M26 and M32.

Each new test passed on the original module and was separately run against one listed mutant, where it failed. The existing offline local queue suite passed (66 tests). No survivors remain: equivalent 0, untestable offline 0, defects 0.

Verification: `python3 tests/test_suite.py` passed with 593 checks, 0 failures, and 5 documented platform skips. `python3 tests/test_onboard.py` passed 28 tests. `python3 tests/test_local_worker.py` could not run in this sandbox because binding its loopback test server was denied (`PermissionError`); it made no external request.

## Defects found

None.
