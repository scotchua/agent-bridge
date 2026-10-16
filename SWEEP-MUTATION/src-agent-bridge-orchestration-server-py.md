# Mutation sweep: `src/agent_bridge/orchestration/server.py`

The original source had SHA-256 `42e2a77a27cd5cee0af774e29e8e7f511b051e2931ffc861079485c6c7a3f0e2`. A temporary standard-library runner replaced one expression or statement at a time in an in-memory copy of the module, then ran `test_orchestration_server.py` in a fresh process. Each process had an eight-second limit. The after pass also ran `test_orchestration_server_mutations.py`. The source file was checked byte-for-byte after every run; no production file was edited. Mutations were selected from executable code only, spanning every function including the nested `dependency_check`. No comments, logging, or docstrings were mutated.

| Phase | Killed | Generated | Kill rate |
| --- | ---: | ---: | ---: |
| Existing tests | 11 | 35 | 31.4% |
| Existing plus new tests | 35 | 35 | 100.0% |

## Mutants generated

| ID | Function | Single change | Before | After | New test that kills a before survivor |
| --- | --- | --- | --- | --- | --- |
| M01 | `__init__` | Invert caller membership | killed | killed | — |
| M02 | `__init__` | `interval <= 0` to `< 0` | survived | killed | `test_init_rejects_zero_interval` |
| M03 | `__init__` | Drop invalid interval raise | survived | killed | `test_init_rejects_zero_interval` |
| M04 | `start` | Invert absent thread check | killed | killed | — |
| M05 | `start` | Remove stop event clear | survived | killed | `test_start_clears_previous_stop` |
| M06 | `_executor` | Invert dependency check presence | killed | killed | — |
| M07 | `_executor` | Remove dependency hold call | survived | killed | `test_executor_holds_dependency_before_work` |
| M08 | `_executor` | Negate held condition | survived | killed | `test_executor_holds_dependency_before_work` |
| M09 | `stop` | Remove stop event set | survived | killed | `test_stop_sets_event_without_worker` |
| M10 | `stop` | Join timeout `+ 1` to `- 1` | killed | killed | — |
| M11 | `_error` | Return `None` | survived | killed | `test_error_preserves_request_id_and_code` |
| M12 | `_valid_id` | String alternative `or` to `and` | killed | killed | — |
| M13 | `_valid_id` | Accept booleans instead of excluding them | killed | killed | — |
| M14 | `_valid_id` | Negate finite-number check | survived | killed | `test_valid_id_rejects_nonfinite_numbers` |
| M15 | `_content` | Invert `isError` | survived | killed | `test_content_marks_success_and_failure` |
| M16 | `handle` | Invalid-message `or` to `and` | survived | killed | `test_handle_rejects_non_object_message` |
| M17 | `handle` | Invert notification check | killed | killed | — |
| M18 | `handle` | Invert valid-ID check | killed | killed | — |
| M19 | `handle` | Empty initialize capabilities | survived | killed | `test_handle_initialize_includes_capability_and_instructions` |
| M20 | `handle` | Empty initialize instructions | survived | killed | `test_handle_initialize_includes_capability_and_instructions` |
| M21 | `handle` | Ping result `None` | survived | killed | `test_handle_ping_result_is_empty_object` |
| M22 | `handle` | Remove `tools/listChanged` method | survived | killed | `test_handle_accepts_list_changed_request` |
| M23 | `handle` | Invalid-params `or` to `and` | survived | killed | `test_handle_rejects_non_object_params` |
| M24 | `handle` | Missing arguments default `None` | survived | killed | `test_handle_supplies_empty_arguments` |
| M25 | `handle` | Invert unknown-tool check | killed | killed | — |
| M26 | `handle` | Invert argument-dict check | killed | killed | — |
| M27 | `handle` | Change unknown-method error code by one | survived | killed | `test_handle_unknown_method_uses_method_code` |
| M28 | `handle` | Return `None` after handler exception | survived | killed | `test_handle_converts_handler_exception_to_error_content` |
| M29 | `serve` | Frame size `>` to `>=` | survived | killed | `test_serve_accepts_exact_frame_limit` |
| M30 | `serve` | Remove normal-reply flush | survived | killed | `test_serve_flushes_each_reply` |
| M31 | `serve` | Invert reply-presence check | killed | killed | — |
| M32 | `serve` | Return `False` instead of integer zero | survived | killed | `test_serve_returns_integer_success_status` |
| M33 | `main` | Invert execution queue configuration check | survived | killed | `test_main_constructs_execution_queue_when_configured` |
| M34 | `main` | Return `None` without serving | survived | killed | `test_main_returns_server_status` |
| M35 | `dependency_check` | Invert changed-component membership | survived | killed | `test_dependency_check_reports_changed_local_component` |

## New tests by function

All 21 new tests are in `tests/test_orchestration_server_mutations.py`, use synthetic values and mocks, pass against the original module, and each fail against at least one mutant shown above. The tests are grouped into constructor, worker, protocol, serve, and main classes. `test_dependency_check_reports_changed_local_component` exercises the closure created by `main`.

## Survivors left

None. No equivalent mutants, offline-untestable mutants, or defects were found in this set.

## Verification

- `python3 -m unittest discover -s tests -p test_orchestration_server_mutations.py -v`: 21 passed.
- `python3 tests/test_suite.py`: 593 passed, 0 failed, 5 environmental skips.
- `python3 -m unittest discover -s tests -p test_onboard.py -q`: 28 passed.
- `python3 -m unittest discover -s tests -p test_automatic_gate.py -q`: 169 passed.
- `python3 -m unittest discover -s tests -p test_delegation_audit.py -q`: 40 passed.

The local-worker and automatic-delegation end-to-end modules exercise loopback HTTP, while a host-environment test probes an external address; they were not run because this task prohibits network calls. The mutation sweep itself made no network calls.
