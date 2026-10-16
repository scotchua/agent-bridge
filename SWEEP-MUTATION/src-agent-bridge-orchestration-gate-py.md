# Mutation sweep: `src/agent_bridge/orchestration/gate.py`

## Method

- Generated 93 one-change AST mutants, capped below 150: one in each of the 80 top-level functions and methods, one in each of 8 nested helpers, and 5 extra operator variants. The extra variants cover a numeric increment, a `+`/`-` swap, a dropped raise, and empty and false returns. The other mutants include comparison flips, `and`/`or` swaps, condition negations, `None` returns, and one removed statement. Comments, docstrings, and logging calls were excluded.
- Compiled each mutant into the gate module in an isolated Python process, ran relevant existing offline tests with an 8- or 15-second process ceiling, and restored the original function after each run. No production file was edited. A broader second pass checked initial survivors against the existing gate, output-router, and selected local-first tests. New tests were then run individually against their assigned mutants.
- Production source was byte-identical after every sweep: SHA-256 `f0a29eb85be43e41c36ed1da1599699eaeb7898498923e0447a4edac9f482da7`.

## Kill rates

| Stage | Killed | Generated | Kill rate |
| --- | ---: | ---: | ---: |
| Existing tests, before | 65 | 93 | 69.9% |
| With new tests, after | 92 | 93 | 98.9% |

Excluding the one equivalent mutant, the after rate is 92/92 (100%). A kill means the selected test failed or errored under the mutant while passing on the original. No mutant was counted as killed solely due to a timeout.

## Mutant manifest

| ID | Function | Source line | Change | Before | After |
| --- | --- | ---: | --- | --- | --- |
| M001 | `_shell_reader_words` | 274 | and or | killed | killed |
| M002 | `_shell_whole_file_read` | 310 | and or | killed | killed |
| M003 | `Decision.allowed` | 341 | comparison | killed | killed |
| M004 | `repo_key` | 348 | comparison | killed | killed |
| M005 | `enclosing_repos` | 362 | comparison | killed | killed |
| M006 | `receipt_name` | 393 | return none | survived | killed |
| M007 | `receipt_dir` | 408 | return none | killed | killed |
| M008 | `receipt_path` | 412 | return none | killed | killed |
| M009 | `_SingleDecision.__init__` | 449 | remove statement | survived | killed |
| M010 | `_SingleDecision.emit` | 453 | return none | survived | killed |
| M011 | `_watchdog_seconds` | 463 | comparison | survived | killed |
| M012 | `_arm_watchdog` | 473 | return none | survived | killed |
| M013 | `decision_lock` | 494 | and or | survived | killed |
| M014 | `is_another_clients_retain` | 517 | comparison | killed | killed |
| M015 | `route_decision` | 543 | comparison | killed | killed |
| M016 | `record_decision` | 551 | comparison | survived | killed |
| M017 | `read_receipt` | 644 | comparison | killed | killed |
| M018 | `_sqlite_uri_path` | 700 | return none | killed | killed |
| M019 | `capacity_digest` | 716 | return none | survived | killed |
| M020 | `stage_binding` | 743 | comparison | killed | killed |
| M021 | `_digest_job_state` | 775 | return none | killed | killed |
| M022 | `protected_paths` | 811 | return none | killed | killed |
| M023 | `_under` | 846 | comparison | killed | killed |
| M024 | `_reaches` | 883 | and or | killed | killed |
| M025 | `_program_name` | 916 | return none | killed | killed |
| M026 | `_shell_words` | 926 | comparison | killed | killed |
| M027 | `_command_paths` | 942 | comparison | killed | killed |
| M028 | `_windows_drive_path` | 1032 | comparison | killed | killed |
| M029 | `list_receipts` | 1055 | return none | killed | killed |
| M030 | `patch_paths` | 1075 | and or | killed | killed |
| M031 | `_git_tag_writes` | 1085 | comparison | killed | killed |
| M032 | `_formatter_writes` | 1098 | and or | killed | killed |
| M033 | `_git_worktree_writes` | 1102 | comparison | killed | killed |
| M034 | `shell_writes` | 1123 | and or | killed | killed |
| M035 | `_command_text` | 1130 | return none | killed | killed |
| M036 | `_edit_paths` | 1140 | comparison | killed | killed |
| M037 | `infer_task_type` | 1166 | return none | killed | killed |
| M038 | `_shell_read_paths` | 1192 | and or | survived | equivalent |
| M039 | `_read_tool_paths` | 1222 | and or | survived | killed |
| M040 | `classify` | 1233 | comparison | killed | killed |
| M041 | `automatic_receipt_overtaken` | 1269 | comparison | killed | killed |
| M042 | `manual_receipt_overtaken` | 1315 | return none | killed | killed |
| M043 | `receipt_overtaken` | 1338 | return none | killed | killed |
| M044 | `_same_path` | 1368 | comparison | killed | killed |
| M045 | `_judge_read_path` | 1385 | comparison | survived | killed |
| M046 | `judge_read` | 1515 | return none | killed | killed |
| M047 | `_worktree_prune_decision` | 1576 | comparison | survived | killed |
| M048 | `_judge_write` | 1644 | comparison | killed | killed |
| M049 | `judge` | 1840 | comparison | killed | killed |
| M050 | `hook_output` | 1913 | return none | killed | killed |
| M051 | `inline_output_update` | 1929 | comparison | killed | killed |
| M052 | `record_event` | 1988 | negate condition | killed | killed |
| M053 | `automatic_decider` | 2008 | and or | survived | killed |
| M054 | `observe_presence_best_effort` | 2044 | comparison | survived | killed |
| M055 | `_write_presence` | 2074 | negate condition | killed | killed |
| M056 | `_last_presence_failure` | 2102 | comparison | killed | killed |
| M057 | `_response_byte_length` | 2122 | return none | killed | killed |
| M058 | `record_inline_measurement` | 2139 | comparison | killed | killed |
| M059 | `run_post_hook` | 2163 | negate condition | killed | killed |
| M060 | `state_root_from_config` | 2184 | return none | killed | killed |
| M061 | `gate_paths_from_config` | 2188 | comparison | killed | killed |
| M062 | `gemma_pilot_tasks_from_config` | 2227 | comparison | survived | killed |
| M063 | `_hook_input` | 2249 | return none | survived | killed |
| M064 | `run_hook` | 2284 | return none | killed | killed |
| M065 | `quote_for_host_shell` | 2330 | comparison | killed | killed |
| M066 | `hook_command` | 2353 | comparison | killed | killed |
| M067 | `hook_entry` | 2361 | comparison | killed | killed |
| M068 | `_is_ours` | 2368 | and or | killed | killed |
| M069 | `hooks_file_update` | 2374 | comparison | killed | killed |
| M070 | `codex_hooks_flag_update` | 2439 | comparison | killed | killed |
| M071 | `install_paths` | 2495 | return none | killed | killed |
| M072 | `plan_install` | 2505 | comparison | survived | killed |
| M073 | `_entries_remaining` | 2585 | and or | killed | killed |
| M074 | `install` | 2603 | and or | killed | killed |
| M075 | `_codex_hook_key_path` | 2676 | return none | killed | killed |
| M076 | `codex_trust_state` | 2693 | and or | killed | killed |
| M077 | `_automatic_routing_state` | 2724 | return none | survived | killed |
| M078 | `_policy_state` | 2752 | return none | survived | killed |
| M079 | `report` | 2771 | comparison | survived | killed |
| M080 | `main` | 2838 | comparison | survived | killed |
| M081 | `_arm_watchdog.fire` | 480 | negate condition | survived | killed |
| M082 | `_judge_write.out_of_time` | 1680 | return none | survived | killed |
| M083 | `_judge_write.overtaken` | 1686 | return none | survived | killed |
| M084 | `_judge_write.effective` | 1692 | and or | killed | killed |
| M085 | `_judge_write.written` | 1712 | comparison | survived | killed |
| M086 | `_judge_write.decide_now` | 1720 | comparison | survived | killed |
| M087 | `_judge_write.judge_held` | 1745 | comparison | survived | killed |
| M088 | `automatic_decider.decide` | 2015 | and or | survived | killed |
| M089 | `_response_byte_length` | 2122 | numeric plus one | killed | killed |
| M090 | `record_decision` | 551 | plus minus | killed | killed |
| M091 | `record_decision` | 551 | drop raise | killed | killed |
| M092 | `_shell_reader_words` | 274 | return empty | killed | killed |
| M093 | `shell_writes` | 1123 | return false | killed | killed |

## Survivors left

- M038 (`_shell_read_paths`, line 1192): equivalent for reachable inputs. `_shell_reader_words` returns a nonempty reader command word, and `_command_paths` includes that same command-name path in its paths for the full command. Thus `resolved[0] in paths` is true whenever `resolved` is nonempty; swapping `and` for `or` does not change which path is removed. No behaviorally different survivor remained. No mutant was untestable offline.

## New tests

Every test below is in `tests/test_gate_mutations.py`, passes on the original source, and failed on at least one recorded mutant. Tests are grouped by the function they exercise.

| Function group | Test | Kills |
| --- | --- | --- |
| `ReceiptNameMutationTests` | `test_receipt_name_uses_a_distinct_slot_for_another_clients_retain` | M006 |
| `DecisionLockMutationTests` | `test_decision_lock_does_not_resolve_a_relative_repository` | M013 |
| `RecordDecisionMutationTests` | `test_record_decision_rejects_a_lease_ending_at_the_decision_time` | M016 |
| `SingleDecisionMutationTests` | `test_single_decision_initializes_the_output_lock` | M009 |
| `SingleDecisionMutationTests` | `test_single_decision_emit_reports_its_first_write` | M010 |
| `WatchdogMutationTests` | `test_watchdog_seconds_rejects_zero` | M011 |
| `WatchdogMutationTests` | `test_arm_watchdog_returns_the_started_timer` | M012 |
| `WatchdogMutationTests` | `test_arm_watchdog_fire_exits_after_emitting_a_deny` | M081 |
| `CapacityDigestMutationTests` | `test_capacity_digest_returns_a_fingerprint_for_an_empty_table` | M019 |
| `WorktreePruneMutationTests` | `test_worktree_prune_accepts_a_shell_escaped_space_in_the_checkout_path` | M047 |
| `ReadToolPathsMutationTests` | `test_read_tool_paths_ignores_a_non_string_path` | M039 |
| `JudgeWriteMutationTests` | `test_judge_write_expires_its_budget_at_the_exact_deadline` | M082, M087 |
| `JudgeWriteMutationTests` | `test_judge_write_redecides_an_overtaken_receipt` | M083 |
| `JudgeWriteMutationTests` | `test_judge_write_accepts_the_receipt_reported_by_its_decider` | M085 |
| `JudgeWriteMutationTests` | `test_judge_write_hides_unvetted_decider_exception_text` | M086 |
| `AutomaticDeciderMutationTests` | `test_automatic_decider_passes_the_local_queue_root` | M053, M088 |
| `PresenceMutationTests` | `test_presence_failure_is_logged_at_the_rate_limit_boundary` | M054 |
| `ConfigMutationTests` | `test_gate_paths_selects_the_certified_delegate` | M061 |
| `ConfigMutationTests` | `test_gemma_pilot_tasks_are_enabled_only_for_the_certified_backend` | M062 |
| `HookInputMutationTests` | `test_hook_input_returns_decoded_utf8_bytes` | M063 |
| `ReportMutationTests` | `test_automatic_routing_state_returns_both_clients` | M077 |
| `ReportMutationTests` | `test_policy_state_summarizes_a_valid_policy` | M078 |
| `ReportMutationTests` | `test_report_expires_a_receipt_at_its_exact_deadline` | M079 |
| `PlanInstallMutationTests` | `test_plan_install_for_claude_does_not_update_the_codex_feature_flag` | M072 |
| `MainMutationTests` | `test_main_install_dispatches_to_install` | M080 |
| `ReadThresholdMutationTests` | `test_judge_read_path_gates_a_file_exactly_at_the_size_threshold` | M045 |

## Defects found

None.

## Verification

- `python3 tests/test_suite.py`: 593 passed, 0 failed, 5 environment skips.
- `tests/test_delegation_gate.py`: 196 run, 4 skipped; `tests/test_automatic_gate.py`: 169 passed.
- `tests/test_gate_mutations.py`: 26 passed; `tests/test_hostenv.py`: 32 run, 11 skipped; `tests/test_delegation_audit.py`: 40 passed; `tests/test_onboard.py`: 28 passed.
- `tests/test_automatic_delegation_e2e.py` and `tests/test_local_worker.py` could not enter their loopback HTTP fixtures: this sandbox refuses binding `127.0.0.1` with `PermissionError`. No external service or API was contacted.
