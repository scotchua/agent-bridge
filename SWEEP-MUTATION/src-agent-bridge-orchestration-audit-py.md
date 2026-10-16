# Mutation sweep: `src/agent_bridge/orchestration/audit.py`

The temporary runner compiled one AST edit at a time into the imported audit module in a fresh subprocess. It never wrote the source file. Mutants in comments and docstrings were excluded by AST selection; the module has no logging calls or explicit `raise` statements to mutate. Each mutant ran the existing `tests/test_delegation_audit.py` under a five-second timeout in an isolated temporary home. The after pass also ran `tests/test_audit_mutation.py`. All 12 top-level functions were sampled; 114 mutants were generated, below the 150 cap.

## Kill rates

| Pass | Killed | Generated | Kill rate |
| --- | ---: | ---: | ---: |
| Before new tests | 88 | 114 | 77.2% |
| After new tests | 111 | 114 | 97.4% |

The three remaining mutants are equivalent. Among the 111 behavior-changing mutants, the after kill rate is 111/111 (100%). No mutant run timed out.

## New tests

Each test passed on the original module and failed when run alone against the listed mutant. Tests are grouped by audited function in `tests/test_audit_mutation.py`.

| Test | Mutant killed alone |
| --- | ---: |
| `LedgerMutationCases.test_record_at_window_boundary_is_included` | M004 |
| `LedgerMutationCases.test_unreadable_ledger_returns_error_row` | M005 |
| `EligibilityMutationCases.test_unclassified_decision_is_explicitly_ineligible` | M021 |
| `EligibilityMutationCases.test_opted_in_client_work_accepts_either_local_or_permitted_peer` | M019 |
| `EligibilityMutationCases.test_nonclient_route_must_differ_from_caller` | M020 |
| `HookCoverageMutationCases.test_untrusted_codex_hook_explains_ungated_calls` | M035 |
| `QueueMutationCases.test_local_queue_uses_five_second_read_only_timeout` | M045 |
| `QueueMutationCases.test_execution_queue_counts_each_receipt_once` | M052 |
| `LocalFirstMutationCases.test_missing_readiness_inputs_report_unknown` | M057 |
| `LocalFirstMutationCases.test_compelled_reads_are_counted_by_repository` | M060 |
| `ReportMutationCases.test_default_window_is_24_hours` | M071 |
| `ReportMutationCases.test_gemma_backend_uses_its_delegate_executable_for_readiness` | M073 |
| `RenderMutationCases.test_installed_hook_with_automatic_routing_off_is_labelled` | M076 |
| `RenderMutationCases.test_automatic_hook_is_not_labelled_off` | M083 |
| `RenderMutationCases.test_gate_failure_code_and_queue_state_are_rendered` | M077 |

## Survivors left

| Mutant | Reason |
| --- | --- |
| M085 | Equivalent: replaces `_read_ledger` missing-path `return []` with another empty list. |
| M094 | Equivalent: replaces `_eligible` unclassified `return False` with the same value. |
| M099 | Equivalent: replaces `outstanding_intents` missing-directory `return []` with another empty list. |

## Defects found

None.

## Verification

- `python3 tests/test_suite.py`: 592 passed, 0 failed, 5 environmental skips, using an isolated temporary home.
- `tests/test_delegation_audit.py`: 40 passed; `tests/test_audit_mutation.py`: 15 passed.
- `tests/test_hostenv.py`: 32 passed with 11 skips; `tests/test_automatic_gate.py`: 169 passed; `tests/test_onboard.py`: 28 passed.
- `tests/test_automatic_delegation_e2e.py` and `tests/test_local_worker.py` could not complete because this sandbox refused their loopback HTTP server socket binds (`PermissionError: Operation not permitted`). No external service was contacted.
- `git diff --exit-code -- src/agent_bridge` succeeded; the audit source and all production files are unchanged.

## Mutants generated

| ID | Function | Source line | Mutation | Before | After |
| --- | --- | ---: | --- | --- | --- |
| M000 | `_read_ledger` | 73 | negate_condition | killed | killed |
| M001 | `_read_ledger` | 74 | return_none | killed | killed |
| M002 | `_read_ledger` | 85 | remove_statement | killed | killed |
| M003 | `_read_ledger` | 93 | and_or | killed | killed |
| M004 | `_read_ledger` | 93 | comparison | survived | killed |
| M005 | `_read_ledger` | 79 | return_none | survived | killed |
| M006 | `_read_ledger` | 93 | negate_condition | killed | killed |
| M007 | `_read_ledger` | 92 | remove_statement | killed | killed |
| M008 | `_histogram` | 104 | return_none | killed | killed |
| M009 | `_histogram` | 102 | remove_statement | killed | killed |
| M010 | `_histogram` | 103 | plus_minus | killed | killed |
| M011 | `_histogram` | 103 | numeric_plus_one | killed | killed |
| M012 | `_histogram` | 103 | numeric_plus_one | killed | killed |
| M013 | `_histogram` | 103 | remove_statement | killed | killed |
| M014 | `_decision_rows` | 108 | return_none | killed | killed |
| M015 | `_decision_rows` | 108 | comparison | killed | killed |
| M016 | `_eligible` | 146 | remove_statement | killed | killed |
| M017 | `_eligible` | 128 | negate_condition | killed | killed |
| M018 | `_eligible` | 138 | return_none | killed | killed |
| M019 | `_eligible` | 145 | and_or | survived | killed |
| M020 | `_eligible` | 147 | comparison | survived | killed |
| M021 | `_eligible` | 126 | return_none | survived | killed |
| M022 | `_eligible` | 124 | remove_statement | killed | killed |
| M023 | `_eligible` | 145 | comparison | survived | killed |
| M024 | `outstanding_intents` | 160 | remove_statement | killed | killed |
| M025 | `outstanding_intents` | 158 | negate_condition | killed | killed |
| M026 | `outstanding_intents` | 173 | return_none | killed | killed |
| M027 | `outstanding_intents` | 159 | return_none | killed | killed |
| M028 | `outstanding_intents` | 165 | negate_condition | killed | killed |
| M029 | `outstanding_intents` | 157 | remove_statement | killed | killed |
| M030 | `outstanding_intents` | 164 | remove_statement | killed | killed |
| M031 | `outstanding_intents` | 162 | negate_condition | killed | killed |
| M032 | `_hook_coverage` | 187 | remove_statement | killed | killed |
| M033 | `_hook_coverage` | 190 | negate_condition | killed | killed |
| M034 | `_hook_coverage` | 217 | return_none | killed | killed |
| M035 | `_hook_coverage` | 213 | comparison | survived | killed |
| M036 | `_hook_coverage` | 200 | and_or | killed | killed |
| M037 | `_hook_coverage` | 188 | negate_condition | killed | killed |
| M038 | `_hook_coverage` | 203 | remove_statement | killed | killed |
| M039 | `_hook_coverage` | 201 | negate_condition | killed | killed |
| M040 | `_local_queue_report` | 228 | negate_condition | killed | killed |
| M041 | `_local_queue_report` | 238 | remove_statement | killed | killed |
| M042 | `_local_queue_report` | 232 | return_none | killed | killed |
| M043 | `_local_queue_report` | 228 | and_or | killed | killed |
| M044 | `_local_queue_report` | 236 | plus_minus | killed | killed |
| M045 | `_local_queue_report` | 238 | numeric_plus_one | survived | killed |
| M046 | `_local_queue_report` | 230 | remove_statement | killed | killed |
| M047 | `_local_queue_report` | 244 | remove_statement | killed | killed |
| M048 | `_queue_report` | 271 | negate_condition | killed | killed |
| M049 | `_queue_report` | 272 | return_none | killed | killed |
| M050 | `_queue_report` | 271 | and_or | killed | killed |
| M051 | `_queue_report` | 286 | remove_statement | killed | killed |
| M052 | `_queue_report` | 284 | plus_minus | survived | killed |
| M053 | `_queue_report` | 284 | numeric_plus_one | survived | killed |
| M054 | `_queue_report` | 281 | numeric_plus_one | survived | killed |
| M055 | `_queue_report` | 279 | remove_statement | killed | killed |
| M056 | `_local_first_report` | 335 | remove_statement | killed | killed |
| M057 | `_local_first_report` | 343 | negate_condition | survived | killed |
| M058 | `_local_first_report` | 358 | return_none | killed | killed |
| M059 | `_local_first_report` | 340 | plus_minus | killed | killed |
| M060 | `_local_first_report` | 367 | and_or | survived | killed |
| M061 | `_local_first_report` | 330 | comparison | killed | killed |
| M062 | `_local_first_report` | 330 | numeric_plus_one | killed | killed |
| M063 | `_local_first_report` | 338 | remove_statement | killed | killed |
| M064 | `_inline_measurement_report` | 446 | remove_statement | killed | killed |
| M065 | `_inline_measurement_report` | 449 | return_none | killed | killed |
| M066 | `_inline_measurement_report` | 448 | and_or | killed | killed |
| M067 | `_inline_measurement_report` | 447 | remove_statement | killed | killed |
| M068 | `report` | 516 | remove_statement | killed | killed |
| M069 | `report` | 497 | negate_condition | killed | killed |
| M070 | `report` | 476 | return_none | killed | killed |
| M071 | `report` | 462 | numeric_plus_one | survived | killed |
| M072 | `report` | 507 | and_or | killed | killed |
| M073 | `report` | 503 | comparison | survived | killed |
| M074 | `report` | 467 | plus_minus | killed | killed |
| M075 | `report` | 502 | remove_statement | survived | killed |
| M076 | `render` | 671 | remove_statement | survived | killed |
| M077 | `render` | 660 | negate_condition | survived | killed |
| M078 | `render` | 691 | return_none | killed | killed |
| M079 | `render` | 657 | and_or | survived | killed |
| M080 | `render` | 691 | plus_minus | killed | killed |
| M081 | `render` | 659 | remove_statement | killed | killed |
| M082 | `render` | 688 | negate_condition | killed | killed |
| M083 | `render` | 670 | and_or | survived | killed |
| M084 | `_read_ledger` | 74 | return_false | killed | killed |
| M085 | `_read_ledger` | 74 | return_empty | survived | survived |
| M086 | `_read_ledger` | 79 | return_false | survived | killed |
| M087 | `_read_ledger` | 79 | return_empty | survived | killed |
| M088 | `_histogram` | 104 | return_false | killed | killed |
| M089 | `_histogram` | 104 | return_empty | killed | killed |
| M090 | `_decision_rows` | 108 | return_false | killed | killed |
| M091 | `_decision_rows` | 108 | return_empty | killed | killed |
| M092 | `_eligible` | 138 | return_false | killed | killed |
| M093 | `_eligible` | 138 | return_empty | killed | killed |
| M094 | `_eligible` | 126 | return_false | survived | survived |
| M095 | `_eligible` | 126 | return_empty | survived | killed |
| M096 | `outstanding_intents` | 173 | return_false | killed | killed |
| M097 | `outstanding_intents` | 173 | return_empty | killed | killed |
| M098 | `outstanding_intents` | 159 | return_false | killed | killed |
| M099 | `outstanding_intents` | 159 | return_empty | survived | survived |
| M100 | `_hook_coverage` | 217 | return_false | killed | killed |
| M101 | `_hook_coverage` | 217 | return_empty | killed | killed |
| M102 | `_local_queue_report` | 232 | return_false | killed | killed |
| M103 | `_local_queue_report` | 232 | return_empty | killed | killed |
| M104 | `_queue_report` | 272 | return_false | killed | killed |
| M105 | `_queue_report` | 272 | return_empty | killed | killed |
| M106 | `_local_first_report` | 358 | return_false | killed | killed |
| M107 | `_local_first_report` | 358 | return_empty | killed | killed |
| M108 | `_inline_measurement_report` | 449 | return_false | killed | killed |
| M109 | `_inline_measurement_report` | 449 | return_empty | killed | killed |
| M110 | `report` | 476 | return_false | killed | killed |
| M111 | `report` | 476 | return_empty | killed | killed |
| M112 | `render` | 691 | return_false | killed | killed |
| M113 | `render` | 691 | return_empty | killed | killed |
