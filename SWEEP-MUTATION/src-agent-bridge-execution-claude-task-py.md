# Mutation sweep: `src/agent_bridge/execution/claude_task.py`

## Method

- Read the complete 750-line module and the existing execution tests. Generated 40 one-change AST mutants: one in each of 35 functions and five additional operator variants (below the 150-mutant cap). Comments, logging, and docstrings were not mutated.
- Executed each mutant in memory in a fresh Python process, with a 25-second subprocess limit. The on-disk module was never edited. Ran baseline-passing tests from `test_claude_task`, `test_execution_harness_env`, `test_hostenv`, `test_verify_policy`, `test_claude_config_dir`, and `test_client_parity`; the after run added `test_claude_task_mutation`.
- The baseline selection had 129 passing tests; 8 failures, 6 errors, and 16 skips were excluded from kill scoring. The network-denial probe was excluded so the sweep made no network request. The host sandbox rejects `sandbox-exec` with exit 71, so some existing end-to-end execution tests are unavailable here.
- A mutant is killed when a test that passed against the original fails or errors against it. The `_netns_confined` result was checked again after a final assertion was added; M16 was checked with the existing `ResultDetailTests.test_none_when_stdout_is_not_json`. Both targeted checks passed on the original and failed on their mutants.

## Kill rates

| Stage | Killed / generated | Kill rate | Killed / non-equivalent |
| --- | ---: | ---: | ---: |
| Existing tests | 27 / 40 | 67.5% | 27 / 40 (67.5%) |
| With new tests | 40 / 40 | 100.0% | 40 / 40 (100.0%) |

## Mutants generated

| ID | Function | Line | Mutation | Before | After | New killing test |
| ---: | --- | ---: | --- | --- | --- | --- |
| M01 | `_git_bin` | 39 | return_none | killed | killed | — |
| M02 | `_run` | 45 | negate_if | killed | killed | — |
| M03 | `_spawn_detail` | 60 | return_none | killed | killed | — |
| M04 | `_env` | 66 | negate_if | killed | killed | — |
| M05 | `_checked_config_dir` | 86 | return_none | killed | killed | — |
| M06 | `_git` | 91 | negate_if | killed | killed | — |
| M07 | `_git_argv` | 95 | return_none | killed | killed | — |
| M08 | `_sha` | 99 | return_none | survived | killed | `test_hashes_file_bytes` |
| M09 | `_relevant_paths` | 117 | compare | killed | killed | — |
| M10 | `_content_snapshot` | 141 | compare | killed | killed | — |
| M11 | `_hash_regular_file` | 195 | compare | killed | killed | — |
| M12 | `_source_state` | 225 | return_none | killed | killed | — |
| M13 | `_require_confinement` | 240 | return_none | killed | killed | — |
| M14 | `_atomic_json` | 250 | remove_stmt | survived | killed | `test_refuses_receipt_when_private_file_cannot_be_enforced` |
| M15 | `_json` | 264 | negate_if | survived | killed | `test_rejects_a_json_array_as_a_result_document` |
| M16 | `_result_detail` | 283 | return_false | killed | killed | — |
| M17 | `_with_result_detail` | 292 | return_none | killed | killed | — |
| M18 | `_verify_argv` | 295 | negate_if | killed | killed | — |
| M19 | `_auth` | 301 | negate_if | survived | killed | `test_nonzero_status_refuses_even_a_valid_auth_envelope` |
| M20 | `_command` | 308 | return_none | killed | killed | — |
| M21 | `_is_cache_artifact` | 324 | return_false | killed | killed | — |
| M22 | `_unlink_beneath` | 340 | negate_if | killed | killed | — |
| M23 | `_drop_caches` | 359 | negate_if | killed | killed | — |
| M24 | `_patch` | 374 | negate_if | killed | killed | — |
| M25 | `_remove` | 383 | return_false | killed | killed | — |
| M26 | `_pinned_python` | 401 | return_none | killed | killed | — |
| M27 | `_sandboxed` | 409 | negate_if | killed | killed | — |
| M28 | `_netns_confined` | 425 | compare | survived | killed | `test_normal_exit_is_not_a_confinement_selftest_failure` |
| M29 | `_confinement_canaries` | 452 | negate_if | survived | killed | `test_includes_the_source_checkout_of_a_linked_worktree` |
| M30 | `_sandbox_exec_confined` | 478 | negate_if | survived | killed | `test_profile_can_read_a_pinned_virtual_environment` |
| M31 | `_generation_fence` | 531 | negate_if | killed | killed | — |
| M32 | `_refuse_hard_links` | 563 | negate_if | killed | killed | — |
| M33 | `_fenced_generation` | 576 | compare | killed | killed | — |
| M34 | `run_task` | 589 | negate_if | killed | killed | — |
| M35 | `main` | 747 | compare | survived | killed | `test_complete_result_has_success_exit_status` |
| M36 | `_auth#bool_swap` | 303 | bool_swap | survived | killed | `test_rejects_an_unsupported_auth_method` |
| M37 | `_content_snapshot#numeric_plus_one` | 143 | numeric_plus_one | survived | killed | `test_counts_exact_dirty_file_bytes` |
| M38 | `_content_snapshot#add_sub_swap` | 158 | add_sub_swap | survived | killed | `test_counts_exact_dirty_file_bytes` |
| M39 | `_auth#drop_raise` | 301 | drop_raise | survived | killed | `test_nonzero_status_refuses_even_a_valid_auth_envelope` |
| M40 | `_sha#return_empty` | 99 | return_empty | survived | killed | `test_hashes_file_bytes` |

## Survivors left

None. Equivalent mutants: none. Untestable offline: none. Defects found: none.

## New tests

All ten tests are in `tests/test_claude_task_mutation.py`, pass on the original, and fail on the recorded mutant(s) named in the table:

- `test_hashes_file_bytes`
- `test_counts_exact_dirty_file_bytes`
- `test_refuses_receipt_when_private_file_cannot_be_enforced`
- `test_rejects_a_json_array_as_a_result_document`
- `test_rejects_an_unsupported_auth_method`
- `test_nonzero_status_refuses_even_a_valid_auth_envelope`
- `test_normal_exit_is_not_a_confinement_selftest_failure`
- `test_includes_the_source_checkout_of_a_linked_worktree`
- `test_profile_can_read_a_pinned_virtual_environment`
- `test_complete_result_has_success_exit_status`

## Verification and limits

- `python3 -m unittest discover -s tests -p test_claude_task_mutation.py -q`: 10 tests passed.
- `python3 tests/test_suite.py` under an isolated temporary home: 592 passed, 0 failed, 5 environment skips.
- `tests/test_onboard.py` under an isolated temporary home: 28 passed.
- `tests/test_local_worker.py` could not run its 14 tests: its local HTTP fixture cannot bind `127.0.0.1` in this OS sandbox (`PermissionError: Operation not permitted`). No live worker or external API was contacted.
- The sweep proves this selected mutant set, not exhaustive mutation coverage of every expression. Host-specific confinement behavior was tested with offline mocks where practical.
