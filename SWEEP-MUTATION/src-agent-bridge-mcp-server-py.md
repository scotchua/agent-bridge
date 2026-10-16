# Mutation check: `src/agent_bridge/mcp_server.py`

The 43 single-site AST mutants below cover every function in the module. The probe changed comparisons, Boolean operators and conditions, numeric constants, return values to None, empty, or False, one statement at a time, addition, and an exception raise. It skipped comments, logging, and docstrings. Each mutant ran in a separate subprocess with an eight-second limit. The mutated module was loaded from a temporary file under `SWEEP-MUTATION`; the production source was never written and its SHA-256 was checked after every run.

Per-mutant baseline: three MCP tests in `test_input_security.py` (deep JSON, oversized frame, container ID) and `SystemAcceptanceTests.test_consultation_surfaces_remain_directional_and_unmodified`. The after pass used the same four tests plus `test_mcp_server_mutation.py`. The separate full-suite verification is recorded below.

| Measure | Killed | Survived | Kill rate |
| --- | ---: | ---: | ---: |
| Before new tests | 20 | 23 | 46.5% |
| After new tests | 43 | 0 | 100% |

| Mutant | Function:line | Change | Before | After |
| --- | --- | --- | --- | --- |
| M001 | `_isolation_note:53` | comparison | survived | killed |
| M002 | `_local_first_schema:90` | return None | survived | killed |
| M003 | `_start_schema:105` | return None | killed | killed |
| M004 | `_continue_schema:153` | remove statement | killed | killed |
| M005 | `_continue_schema:155` | remove statement | survived | killed |
| M006 | `_job_schema:170` | return None | survived | killed |
| M007 | `build_tools:182` | negate condition | survived | killed |
| M008 | `build_tools:182` | and/or swap | survived | killed |
| M009 | `build_tools:186` | constant +1 | survived | killed |
| M010 | `build_tools:187` | constant +1 | survived | killed |
| M011 | `main:443` | return None | survived | killed |
| M012 | `Server.__init__:247` | negate condition | killed | killed |
| M013 | `Server.__init__:248` | replace exception with bare raise | survived | killed |
| M014 | `Server.handle:258` | negate condition | killed | killed |
| M015 | `Server.handle:262` | negate condition | killed | killed |
| M016 | `Server.handle:266` | negate condition | killed | killed |
| M017 | `Server.handle:269` | negate condition | killed | killed |
| M018 | `Server.handle:283` | negate condition | killed | killed |
| M019 | `Server.handle:294` | comparison | killed | killed |
| M020 | `Server.handle:296` | comparison | killed | killed |
| M021 | `Server.call_tool:308` | negate condition | survived | killed |
| M022 | `Server.call_tool:306` | and/or swap | survived | killed |
| M023 | `Server.call_tool:314` | return None | survived | killed |
| M024 | `Server._content:327` | return None | survived | killed |
| M025 | `Server._error:335` | return None | killed | killed |
| M026 | `Server._valid_request_id:342` | negate condition | killed | killed |
| M027 | `Server._valid_request_id:342` | and/or swap | killed | killed |
| M028 | `Server._valid_request_id:344` | and/or swap | survived | killed |
| M029 | `Server.serve:352` | negate condition | killed | killed |
| M030 | `Server.serve:354` | negate condition | killed | killed |
| M031 | `Server.serve:360` | negate condition | killed | killed |
| M032 | `Server.serve:368` | negate condition | killed | killed |
| M033 | `Server.serve:386` | negate condition | killed | killed |
| M034 | `Server.serve:395` | negate condition | killed | killed |
| M035 | `Server.serve:354` | comparison | survived | killed |
| M036 | `Server.serve:369` | negate condition | survived | killed |
| M037 | `Server.serve:382` | negate condition | survived | killed |
| M038 | `Server.serve:351` | +/− swap | killed | killed |
| M039 | `Server.serve:380` | negate condition | survived | killed |
| M040 | `Server.serve:394` | constant +1 | survived | killed |
| M041 | `Server.__init__:248` | drop exception raise | survived | killed |
| M042 | `_isolation_note:61` | return empty string | survived | killed |
| M043 | `Server._valid_request_id:341` | return False | survived | killed |

## New tests

Each name below was run alone against its listed mutant. Every one failed on that mutant and passed on the original module.

| Test in `tests/test_mcp_server_mutation.py` | Mutant killed alone |
| --- | --- |
| `SchemaTests.test_isolation_notes_match_the_peer` | M001 |
| `SchemaTests.test_local_first_schema_requires_a_known_bypass` | M002 |
| `SchemaTests.test_continuation_schema_includes_conversation_id` | M005 |
| `SchemaTests.test_job_schema_requires_job_id` | M006 |
| `SchemaTests.test_build_tools_advertises_only_allowed_classifications` | M007 |
| `SchemaTests.test_build_tools_uses_default_prompt_budgets` | M009 |
| `ServerInitializationTests.test_unknown_caller_raises_value_error` | M013 |
| `CallToolTests.test_unknown_tool_returns_input_error_envelope` | M021 |
| `CallToolTests.test_arguments_are_forwarded_to_the_handler` | M022 |
| `CallToolTests.test_content_keeps_structured_and_text_payloads_equal` | M024 |
| `RequestIdTests.test_finite_numeric_request_ids_are_accepted` | M028 |
| `RequestIdTests.test_string_request_ids_are_accepted` | M043 |
| `ServeTests.test_exact_frame_limit_is_accepted` | M035 |
| `ServeTests.test_empty_batch_returns_invalid_request` | M036 |
| `ServeTests.test_batch_emits_only_responses` | M037 |
| `ServeTests.test_handler_crash_becomes_internal_error` | M040 |
| `MainTests.test_preflight_refusal_returns_exit_code_two` | M011 |

## Survivors left

None. Equivalent mutants: **none**. Untestable offline mutants: **none**.

## Defects found

None.

Verification: `PYTHONPATH=src:tests python3 -m unittest test_mcp_server_mutation -q` passed 17 tests. `python3 tests/test_suite.py` passed 592 checks, failed 0, skipped 5 platform-dependent process-group checks, using an isolated temporary home. `tests/test_onboard.py` passed 28 tests. `tests/test_local_worker.py` could not run here: its 14 setups each failed when the sandbox denied binding the loopback fake HTTP server (`PermissionError: [Errno 1] Operation not permitted`). No external API was contacted.
