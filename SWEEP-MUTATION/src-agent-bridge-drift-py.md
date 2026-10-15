# Mutation sweep: `src/agent_bridge/drift.py`

## Method

Read the full module and `tests/test_drift.py`. An AST-based throwaway runner generated one mutant per function plus nine operator mutants. Each mutation changed one node in a temporary copy, loaded in a separate Python process. The original module was never written. Each test process had a 20-second timeout. Tests used temporary homes, fake runners, and mocks; no provider or API was contacted.

Source SHA-256 before and after: `4cba77b58328d8784c4d1d606129351124a177f8ae946c54d106d03844513ada`. The source was byte-identical at the end.

## Kill rates

| Stage | Killed | Generated | Kill rate |
| --- | ---: | ---: | ---: |
| Existing `tests/test_drift.py` | 36 | 64 | 56.25% |
| After `tests/test_drift_mutation.py` | 64 | 64 | 100.00% |

## Mutants

| ID | Function | Line | Mutation | Before | After | New test for survivor |
| --- | --- | ---: | --- | --- | --- | --- |
| M001 | `_home` | 52 | return_none | killed | killed | — |
| M002 | `paths` | 57 | return_none | killed | killed | — |
| M003 | `_config_candidates` | 74 | return_none | survived | killed | `test_config_candidates_preserves_receipt_then_default` |
| M004 | `discover_config` | 84 | return_none | survived | killed | `test_discover_config_finds_existing_candidate` |
| M005 | `default_config_path` | 89 | return_none | survived | killed | `test_default_config_path_returns_discovered_path` |
| M006 | `_sha256` | 100 | return_none | survived | killed | `test_sha256_hashes_existing_file` |
| M007 | `_version` | 119 | return_none | survived | killed | `test_version_reads_first_line_without_injected_runner` |
| M008 | `_bridge` | 124 | return_none | survived | killed | `test_bridge_returns_loaded_configuration` |
| M009 | `_executable` | 137 | return_none | survived | killed | `test_executable_returns_configured_file` |
| M010 | `_normal_digest` | 144 | return_none | survived | killed | `test_normal_digest_removes_prefix_and_lowercases` |
| M011 | `_ollama` | 172 | return_none | killed | killed | — |
| M012 | `_hook_state` | 197 | return_none | killed | killed | — |
| M013 | `_absolute_paths` | 201 | return_none | killed | killed | — |
| M014 | `_codex_default_model` | 209 | return_none | survived | killed | `test_codex_default_model_reads_isolated_home` |
| M015 | `inventory` | 238 | return_none | killed | killed | — |
| M016 | `_stable` | 249 | return_none | survived | killed | `test_stable_removes_nested_timestamps` |
| M017 | `changes` | 263 | return_none | killed | killed | — |
| M018 | `_read_baseline` | 269 | return_none | killed | killed | — |
| M019 | `_write_baseline` | 276 | drop_statement | killed | killed | — |
| M020 | `_merge_good_baseline` | 310 | return_none | survived | killed | `test_merge_good_baseline_retains_old_failed_codex` |
| M021 | `_run` | 315 | return_none | survived | killed | `test_run_returns_completed_process` |
| M022 | `_quick_run` | 330 | return_none | survived | killed | `test_quick_run_returns_bounded_probe` |
| M023 | `_completed_text` | 334 | return_none | killed | killed | — |
| M024 | `_help_has_flags` | 345 | return_none | killed | killed | — |
| M025 | `_claude_help_has_flags` | 355 | return_none | killed | killed | — |
| M026 | `_model_catalog` | 369 | return_none | killed | killed | — |
| M027 | `_model_check` | 389 | return_none | killed | killed | — |
| M028 | `_consumer_pins` | 412 | return_none | killed | killed | — |
| M029 | `_version_key` | 417 | return_none | killed | killed | — |
| M030 | `_newer` | 421 | return_none | killed | killed | — |
| M031 | `_quarantine_hours` | 433 | return_none | killed | killed | — |
| M032 | `_eligible_release` | 461 | return_none | killed | killed | — |
| M033 | `run_cli_check` | 486 | return_none | killed | killed | — |
| M034 | `run_gemma_check` | 502 | return_none | killed | killed | — |
| M035 | `_audit` | 509 | swap_arithmetic | killed | killed | — |
| M036 | `doctor` | 594 | return_none | killed | killed | — |
| M037 | `quick_check` | 621 | return_none | killed | killed | — |
| M038 | `scheduled_baseline` | 670 | return_none | killed | killed | — |
| M039 | `schedule_path` | 675 | return_none | killed | killed | — |
| M040 | `_schedule_launchctl` | 681 | return_none | survived | killed | `test_schedule_launchctl_returns_activation_result` |
| M041 | `install_schedule` | 693 | return_none | killed | killed | — |
| M042 | `remove_schedule` | 700 | return_none | survived | killed | `test_remove_schedule_reports_removed_file` |
| M043 | `_npm_for_prefix` | 709 | return_none | survived | killed | `test_npm_for_prefix_finds_node_adjacent_npm` |
| M044 | `_codex_prefix` | 719 | return_none | killed | killed | — |
| M045 | `_npm_version` | 729 | return_none | survived | killed | `test_npm_version_extracts_version_from_offline_output` |
| M046 | `_npm_version_for_executable` | 741 | return_none | killed | killed | — |
| M047 | `_consumer_message` | 745 | return_none | killed | killed | — |
| M048 | `_staging_checks` | 761 | return_none | killed | killed | — |
| M049 | `_post_switch_checks` | 775 | return_none | killed | killed | — |
| M050 | `_prune_staging` | 787 | drop_statement | killed | killed | — |
| M051 | `update` | 887 | return_none | killed | killed | — |
| M052 | `report_attention` | 910 | negate_if | survived | killed | `test_report_attention_skips_notification_for_clear_report` |
| M053 | `_notify_attention` | 917 | drop_statement | survived | killed | `test_notify_attention_delegates_to_report_attention` |
| M054 | `_notify_updated` | 922 | drop_statement | survived | killed | `test_notify_updated_uses_local_notification_command` |
| M055 | `main` | 948 | return_none | survived | killed | `test_main_returns_success_for_clean_report` |
| M056 | `quick_check` | 600 | flip_compare | survived | killed | `test_quick_check_cache_expires_at_limit` |
| M057 | `_eligible_release` | 456 | flip_compare | survived | killed | `test_eligible_release_accepts_cutoff` |
| M058 | `_newer` | 421 | flip_compare | survived | killed | `test_newer_rejects_equal_version` |
| M059 | `_quarantine_hours` | 429 | flip_compare | survived | killed | `test_quarantine_hours_accepts_zero_setting` |
| M060 | `_version` | 111 | flip_compare | survived | killed | `test_version_reads_first_line_without_injected_runner` |
| M061 | `_stable` | 246 | flip_compare | survived | killed | `test_stable_removes_nested_timestamps` |
| M062 | `_staging_checks` | 762 | swap_boolean | killed | killed | — |
| M063 | `_prune_staging` | 782 | increment_number | killed | killed | — |
| M064 | `remove_schedule` | 697 | drop_raise | survived | killed | `test_remove_schedule_reports_removed_file` |

The generated operators include all six comparison directions (`<`, `<=`, `>`, `>=`, `==`, `!=`), `and`/`or`, negated conditions, numeric increment, return replacement, statement removal, `+`/`-`, and a removed raise. Mutants in comments, logging, and docstrings were excluded.

## Survivors left

None. Equivalent: none; untestable offline: none; defect: none.

## Defects found

None. No expected failures were added.

## Verification

- `python3 tests/test_drift.py`: 27 passed.
- `python3 tests/test_drift_mutation.py`: 25 passed on the original. Every new test killed at least one recorded mutant when run against that mutant in isolation.
- `python3 tests/test_suite.py`: 593 passed, 0 failed, 5 environment skips.
- `python3 tests/test_onboard.py`: 28 passed.
- `python3 tests/test_local_worker.py`: 14 fixture setup errors because this sandbox denies binding to `127.0.0.1`; no test body ran.

## Limits

This is a selected 64-mutant sweep, not exhaustive mutation coverage. The loopback HTTP fixture in `test_local_worker.py` could not run under this sandbox.
