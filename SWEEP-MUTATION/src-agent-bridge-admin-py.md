# Mutation test: `src/agent_bridge/admin.py`

The offline probe generated 31 single-change mutants across all 14 functions in
`admin.py`, including the nested `rate` function. It changed one executable
expression or statement at a time in an in-memory copy of the module, restored
the module namespace after each case, and gave each case a 15-second subprocess
timeout. No source file was overwritten. The module contains no explicit
`raise` statement to drop. Mutations did not target comments, logging, or
docstrings.

The before probe ran `test_local_first_reporting` and
`test_reporting_command` from `tests/test_suite.py`, plus
`IdentifierBoundaryTests.test_admin_resolve_returns_a_structured_error_for_an_invalid_id`
from `tests/test_security_boundaries.py`. The after probe added all tests in
`tests/test_admin_mutation.py` to those same checks.

| Measure | Killed | Generated | Kill rate |
| --- | ---: | ---: | ---: |
| Before new tests | 7 | 31 | 22.6% |
| After new tests | 31 | 31 | 100% |

| Function | Mutants generated | Killed before | Killed after | New test |
| --- | --- | --- | --- | --- |
| `_inside` | `inside-root-equality`, `inside-return` | 0/2 | 2/2 | `test_inside_rejects_root_and_accepts_child` |
| `_has_symlink_component` | `symlink-detection`, `symlink-root-comparison` | 0/2 | 2/2 | `test_symlink_components_and_unrelated_paths` |
| `_age_days` | `age-subtraction`, `age-fallback` | 0/2 | 2/2 | `test_age_days_uses_mtime_and_missing_status_fallback` |
| `_hold_age_seconds` | `hold-age-subtraction`, `hold-age-fallback` | 0/2 | 2/2 | `test_hold_age_uses_record_timestamp_and_legacy_mtime` |
| `cmd_status` | `status-job-count`, `status-open`, `status-ledger-constant` | 0/3 | 3/3 | `test_status_counts_jobs_and_open_conversations` |
| `cmd_cleanup` | `cleanup-job-threshold`, `cleanup-conversation-threshold`, `cleanup-apply` | 0/3 | 3/3 | `test_cleanup_includes_exact_retention_boundary`, `test_cleanup_dry_run_never_deletes_and_apply_does` |
| `cmd_ledger` | `ledger-tail`, `ledger-drop-append`, `ledger-full` | 0/3 | 3/3 | `test_ledger_tail_and_full_shape` |
| `_reporting_rows` | `reporting-rows-return` | 1/1 | 1/1 | None needed |
| `cmd_reporting` and nested `rate` | `reporting-peer`, `reporting-sample`, `reporting-rate`, `reporting-threshold` | 2/4 | 4/4 | `test_reporting_rate_and_exact_minimum_sample` |
| `cmd_local_first` | `local-first-peer`, `local-first-required`, `local-first-malformed` | 2/3 | 3/3 | `test_local_first_counts_malformed_declaration` |
| `cmd_resolve` | `resolve-missing-return`, `resolve-dry-run`, `resolve-ack` | 0/3 | 3/3 | `test_resolve_missing_conversation_returns_failure`, `test_resolve_dry_run_preserves_hold`, `test_resolve_clean_hold_requires_no_live_process_acknowledgement` |
| `cmd_indeterminate` | `indeterminate-filter` | 0/1 | 1/1 | `test_indeterminate_lists_only_held_conversations` |
| `main` | `main-error-return`, `main-dispatch` | 2/2 | 2/2 | None needed |

Each of the 14 new tests passed on the original module and failed on at least
one of the listed mutants. All invented conversation and job records live in
isolated temporary broker state; no provider or network call was made.

Verification: `PYTHONPATH=src:tests python3 -m unittest tests/test_admin_mutation.py`
passed 14/14 tests. `python3 tests/test_suite.py` passed 593 checks with zero
failures and five platform skips. The SHA-256 of `admin.py` before and after
the probe was `096f1a1fb8e5a013e48e730223014ab12dccba7316dba030cf6e977cfc20354b`.

## Survivors left

None. No equivalent mutants, untestable offline mutants, or mutants representing
a defect in the original code remained in this set.

## Defects found

None.
