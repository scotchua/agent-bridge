# Mutation sweep: `src/agent_bridge/local_worker.py`

The source was read in full. This sweep generated 23 single-change mutants, covering every function and method in the module. Each mutation was applied to an in-memory copy and executed in a fresh Python process with a 25-second timeout. The original file was checked byte-for-byte after every run; it was never written. The existing `test_local_worker.py` tests ran against an in-process Ollama response stub because this sandbox forbids localhost socket binds. The stub inspected the same request bodies and returned the fixture responses without making network calls. The after run added `test_local_worker_mutation.py`. The unmutated module passed all 27 tests in this harness.

| Measure | Killed | Generated | Kill rate |
| --- | ---: | ---: | ---: |
| Existing tests, before | 9 | 23 | 39.1% |
| Existing plus new tests, after | 22 | 23 | 95.7% |

The one remaining survivor is equivalent for redirect refusal, so the after rate among behavior-changing mutants is 22/22 (100%).

Verification: `python3 tests/test_suite.py` passed with 592 checks, 0 failures, and 5 environmental skips in an isolated temporary home. `python3 tests/test_onboard.py` passed 28 tests. The 14 existing local worker tests and 13 new tests passed in the offline mutation harness; direct execution of `tests/test_local_worker.py` is unavailable in this sandbox because localhost socket bind raises `PermissionError`.

| ID | Function, line | Mutation | Before | After / new test |
| --- | --- | --- | --- | --- |
| M01 | `_NoRedirect.redirect_request`, 39 | `None` to `False` | Survived | Survived; equivalent |
| M02 | `validate_endpoint`, 62 | upper port bound `<=` to `<` | Survived | Killed; `test_validate_endpoint_accepts_highest_port` |
| M03 | `_schema`, 73 | allow additional properties | Survived | Killed; `test_schema_refuses_extra_properties` |
| M04 | `build_tools`, 78 | negate internal admission choice | Survived | Killed; `test_build_tools_admits_internal_only_when_enabled` |
| M05 | `LocalWorkerServer.__init__`, 117 | model byte bound `>` to `>=` | Survived | Killed; `test_init_accepts_model_at_byte_limit` |
| M06 | `_content`, 129 | disable ASCII JSON escaping | Killed | Killed |
| M07 | `_error`, 135 | replace request ID with `None` | Survived | Killed; `test_error_preserves_request_id` |
| M08 | `_failure`, 139 | return `isError=False` | Survived | Killed; `test_failure_marks_tool_result_as_error` |
| M09 | `_post_json`, 149 | same-URL comparison `!=` to `==` | Killed | Killed |
| M10 | `_has_cloud_marker`, 164 | depth threshold `>=` to `>` | Survived | Killed; `test_has_cloud_marker_refuses_at_depth_limit` |
| M11 | `_model_preflight`, 193 | negate cloud marker check | Killed | Killed |
| M12 | `_process`, 212 | input byte bound `>` to `>=` | Survived | Killed; `test_process_accepts_exact_input_byte_limit` |
| M13 | `call_tool`, 248 | negate no-arguments check | Killed | Killed |
| M14 | `handle`, 266 | negate notification check | Killed | Killed |
| M15 | `serve`, 290 | line byte bound `>` to `>=` | Survived | Killed; `test_serve_accepts_exact_line_byte_limit` |
| M16 | `main`, 322 | return `None` instead of server status | Survived | Killed; `test_main_returns_server_status` |
| M17 | `_has_cloud_marker`, 169 | swap `and` to `or` | Killed | Killed |
| M18 | `_process`, 209 | swap input length `+` to `-` | Killed | Killed |
| M19 | `_process`, 215 | negate local admission check | Killed | Killed |
| M20 | `_post_json`, 156 | drop oversize response raise | Survived | Killed; `test_post_json_rejects_oversized_response` |
| M21 | `_process`, 234 | remove output truncation statement | Killed | Killed |
| M22 | `serve`, 305 | remove response flush statement | Survived | Killed; `test_serve_flushes_each_response` |
| M23 | `main`, 317 | drop `ValueError` from argument error handler | Survived | Killed; `test_main_reports_invalid_model_as_argument_error` |

## Survivors left

- **M01, equivalent:** urllib's redirect handler treats either falsey return as refusal, so replacing `None` with `False` does not change the worker's redirect behavior. The private method's direct return value differs, but no reachable worker outcome changes.

## Defects found

None.
