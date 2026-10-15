# Mutation results: `src/agent_bridge/backends/codex_backend.py`

## Method and totals

The temporary runner changed one executable expression or statement at a time in an in-memory copy of the imported backend module. It used 59 hand-selected mutants across `_executable`, `build_argv`, `parse_events`, and `run_consultation`; no comments, logging, or docstrings were mutated. The mutations cover comparison and Boolean changes, negated conditions, a numeric bound changed by one, removed statements, return and payload replacement, and `+=` changed to `-=`. The backend contains no explicit `raise` statement to drop. Each mutant ran in a separate offline subprocess with an 8-second time limit. The baseline ran `tests/test_health.py`; the after pass also ran `tests/test_codex_backend_mutation.py`. The source file was read before and after each run and never written.

| Stage | Killed | Generated | Kill rate |
| --- | ---: | ---: | ---: |
| Existing direct tests | 19 | 59 | 32.2% |
| Existing plus new tests | 58 | 59 | 98.3% |

## Mutants

| Mutant | Before | After |
| --- | --- | --- |
| `executable_discovery` | survived | killed |
| `executable_fallback` | survived | killed |
| `argv_resume_condition` | survived | killed |
| `argv_resume_prompt` | survived | killed |
| `argv_json` | survived | killed |
| `argv_ignore_config` | survived | killed |
| `argv_ignore_rules` | survived | killed |
| `argv_strict_config` | survived | killed |
| `argv_schema` | survived | killed |
| `argv_message` | survived | killed |
| `argv_sandbox` | survived | killed |
| `argv_effort_condition` | survived | killed |
| `argv_effort_value` | survived | killed |
| `argv_start_condition` | survived | killed |
| `argv_start_sandbox` | survived | killed |
| `argv_model_condition` | survived | killed |
| `argv_model_value` | survived | killed |
| `events_skip_noise` | survived | survived |
| `events_strip` | survived | killed |
| `events_append` | killed | killed |
| `events_type` | killed | killed |
| `events_thread_field` | killed | killed |
| `events_nonempty` | survived | killed |
| `events_distinct` | survived | killed |
| `events_first_id` | killed | killed |
| `events_failure_type` | killed | killed |
| `events_nested_error` | survived | killed |
| `events_string_error` | killed | killed |
| `events_return_id` | killed | killed |
| `events_return_errors` | killed | killed |
| `events_return_ids` | survived | killed |
| `run_secure_home` | survived | killed |
| `run_home_guard` | survived | killed |
| `run_home_env` | survived | killed |
| `run_cwd` | survived | killed |
| `run_stdin` | survived | killed |
| `run_stdout_cap` | survived | killed |
| `run_pgid` | survived | killed |
| `run_spawn` | killed | killed |
| `run_migration` | survived | killed |
| `run_ambiguous` | survived | killed |
| `run_session` | killed | killed |
| `run_timeout` | killed | killed |
| `run_cap_flag` | survived | killed |
| `run_cap_boundary` | survived | killed |
| `run_incomplete` | killed | killed |
| `run_failed_code` | killed | killed |
| `run_failed_event` | killed | killed |
| `run_auth_reason` | killed | killed |
| `run_migration_gate` | survived | killed |
| `run_message_file` | killed | killed |
| `run_message_size` | survived | killed |
| `run_raw_message` | survived | killed |
| `run_decode` | killed | killed |
| `run_payload_gate` | killed | killed |
| `run_payload` | survived | killed |
| `run_session_missing` | killed | killed |
| `run_malformed` | survived | killed |
| `argv_add_to_subtract` | survived | killed |

## Survivors left

- `events_skip_noise` is equivalent. Replacing the `or` with `and` lets empty and non-object lines reach `json.loads`, but empty and malformed input raises `ValueError`, while valid non-dictionary JSON is rejected by the existing dictionary check. The parsed events, errors, and thread IDs are unchanged.
- Untestable offline: none.
- Defects found: none.

## New tests and recorded mutants they kill

| Test | Kills at least these recorded mutants |
| --- | --- |
| `test_build_argv_start_and_resume_are_distinct` | `argv_resume_condition`, `argv_resume_prompt`, `argv_json`, `argv_ignore_config`, `argv_ignore_rules`, `argv_strict_config`, `argv_schema`, `argv_message`, `argv_sandbox`, `argv_effort_condition`, `argv_effort_value`, `argv_start_condition`, `argv_start_sandbox`, `argv_model_condition`, `argv_model_value`, `argv_add_to_subtract` |
| `test_executable_uses_pin_then_discovery_then_name` | `executable_discovery`, `executable_fallback` |
| `test_parse_events_accepts_indented_json` | `events_strip`, `events_append`, `events_type`, `events_thread_field`, `events_distinct`, `events_first_id`, `events_return_id`, `events_return_ids` |
| `test_parse_events_ignores_noise_and_tracks_distinct_threads` | `events_append`, `events_type`, `events_thread_field`, `events_distinct`, `events_first_id`, `events_failure_type`, `events_string_error`, `events_return_id`, `events_return_errors`, `events_return_ids` |
| `test_parse_events_rejects_empty_and_nonstring_ids` | `events_type`, `events_thread_field`, `events_nonempty`, `events_distinct`, `events_first_id`, `events_string_error`, `events_return_id`, `events_return_ids` |
| `test_parse_events_uses_top_level_error_message` | `events_type`, `events_nested_error`, `events_string_error`, `events_return_errors` |
| `test_run_consultation_creates_isolated_home` | `run_secure_home` |
| `test_run_consultation_failed_turn_and_auth_reason` | `events_append`, `events_type`, `events_failure_type`, `events_string_error`, `events_return_errors`, `run_spawn`, `run_timeout`, `run_incomplete`, `run_failed_code`, `run_auth_reason` |
| `test_run_consultation_failure_precedence` | `run_spawn`, `run_timeout`, `run_cap_flag`, `run_incomplete` |
| `test_run_consultation_honours_requested_thread_and_rejects_migration` | `events_type`, `events_thread_field`, `events_distinct`, `events_first_id`, `events_return_id`, `events_return_ids`, `run_spawn`, `run_migration`, `run_ambiguous`, `run_session`, `run_timeout`, `run_incomplete`, `run_failed_code`, `run_failed_event`, `run_migration_gate`, `argv_add_to_subtract` |
| `test_run_consultation_last_message_size_boundary` | `events_type`, `events_thread_field`, `events_first_id`, `events_return_id`, `run_spawn`, `run_session`, `run_timeout`, `run_incomplete`, `run_failed_code`, `run_failed_event`, `run_message_file`, `run_message_size`, `run_decode`, `run_payload_gate`, `run_session_missing` |
| `test_run_consultation_missing_or_bad_last_message` | `run_spawn`, `run_timeout`, `run_incomplete`, `run_failed_code`, `run_failed_event`, `run_message_file`, `run_decode`, `run_payload_gate`, `run_payload`, `run_session_missing`, `run_malformed` |
| `test_run_consultation_output_cap_boundary` | `run_spawn`, `run_timeout`, `run_cap_flag`, `run_cap_boundary` |
| `test_run_consultation_passes_isolated_execution_inputs` | `events_type`, `events_thread_field`, `events_first_id`, `events_return_id`, `run_home_env`, `run_cwd`, `run_stdin`, `run_stdout_cap`, `run_pgid`, `run_spawn`, `run_session`, `run_timeout`, `run_incomplete`, `run_failed_code`, `run_failed_event`, `run_message_file`, `run_raw_message`, `run_decode`, `run_payload_gate`, `run_payload`, `run_session_missing` |
| `test_run_consultation_rejects_home_config` | `run_home_guard` |

## Verification

- `python3 -m unittest discover -s tests -p 'test_codex_backend_mutation.py' -q`: 15 tests passed on original source.
- `python3 tests/test_suite.py`: 594 passed, 0 failed, 5 environmental skips.
- `python3 tests/test_onboard.py`: 28 passed in an isolated temporary home.
- `python3 tests/test_local_worker.py`: fixture setup failed on all 14 tests because this sandbox denies binding `127.0.0.1`; no external service or API was contacted.
- The production backend remained byte-identical: SHA-256 `4c94fa64bfde590d3d91cd209e074fab1af57b3850837d51dcf7c9fe26dfc220`.
