# Mutation sweep: `src/agent_bridge/broker.py`

## Method and rates

- Generated **37** single-change AST mutants, covering all **31** functions. The sample includes comparison flips, `and`/`or` swaps, condition negation, numeric increment, return replacements (`None`, empty dict, `False`), one-statement removal, `+`/`-` swap, and a dropped `raise`. Mutations were confined to executable syntax; comments, docstrings, and logging were skipped.
- Each mutant was compiled into the imported broker module in an isolated Python subprocess. The source file was never written. Each subprocess had a temporary home, used only offline fixtures and fake peers, and had an **18-second** timeout.
- The before run used selected existing broker checks from `tests/test_suite.py`, `tests/test_security_boundaries.py`, and `tests/redaction_handoff.py`. The after run added `tests/test_broker_mutations.py` to the same selection. A timed-out run was counted as survived, not killed.
- **Before:** 10/37 killed (27.0%); 27 survived.
- **After:** 37/37 killed (100.0%); 0 survived.
- Every one of the 23 new tests passed on the original module and was run alone against the recorded mutant named below; every isolated test killed its mutant.

## Mutants

| ID | Function:line | Change | Before | After | New test that kills it |
| --- | --- | --- | --- | --- | --- |
| M001 | `error_response:50` | return none | survived | killed | `test_error_response_keeps_closed_category_and_identifier` |
| M002 | `ok_response:54` | return none | killed | killed | — |
| M003 | `ok_response:54` | return empty | killed | killed | — |
| M004 | `_validate_common:69` | comparison | survived | killed | `test_validate_common_accepts_exact_budget_and_refuses_blank_prompt` |
| M005 | `_validate_common:67` | boolean | survived | killed | `test_validate_common_accepts_exact_budget_and_refuses_blank_prompt` |
| M006 | `_validate_common:67` | negation | survived | killed | `test_validate_common_accepts_exact_budget_and_refuses_blank_prompt` |
| M007 | `_receipt_count:129` | comparison | survived | killed | `test_receipt_count_accepts_zero_but_not_negative` |
| M008 | `_receipt_count:129` | number | survived | killed | `test_receipt_count_accepts_zero_but_not_negative` |
| M009 | `_receipt_duration:133` | comparison | survived | killed | `test_receipt_duration_accepts_zero_but_not_negative` |
| M010 | `_receipt_hash:137` | return none | survived | killed | `test_receipt_hash_accepts_exact_lowercase_digest` |
| M011 | `_receipt_chunk_counts:141` | return none | survived | killed | `test_receipt_chunk_counts_counts_each_state` |
| M012 | `_receipt_pass_states:153` | return none | survived | killed | `test_receipt_pass_states_keeps_pass_order` |
| M013 | `_validate_delegation_receipt:167` | comparison | survived | killed | `test_validate_delegation_receipt_refuses_unknown_task` |
| M014 | `_handoff_json:251` | drop raise | survived | killed | `test_handoff_json_refuses_nonfinite_constant` |
| M015 | `_read_clearance:258` | comparison | survived | killed | `test_read_clearance_accepts_matching_digest` |
| M016 | `_initial_handoff_identity:275` | return none | survived | killed | `test_initial_handoff_identity_returns_reviewed_identity` |
| M017 | `validate_handoff:292` | comparison | survived | killed | `test_validate_handoff_accepts_matching_preparation` |
| M018 | `_sqlite_uri_path:368` | return none | survived | killed | `test_sqlite_uri_path_escapes_path_metacharacters` |
| M019 | `_consume_receipt_once:401` | return none | survived | killed | `test_consume_receipt_once_rejects_replay` |
| M020 | `_consume_receipt_once:401` | return false | survived | killed | `test_consume_receipt_once_rejects_replay` |
| M021 | `_digest_receipt_routed_locally:453` | comparison | survived | killed | `test_digest_receipt_routed_locally_accepts_fresh_local_receipt` |
| M022 | `_validate_local_first:485` | comparison | survived | killed | `test_validate_local_first_checks_voluntary_shape` |
| M023 | `_validate_identifier_request:511` | return none | survived | killed | `test_validate_identifier_request_returns_canonical_id` |
| M024 | `_authorize_job_caller:524` | comparison | killed | killed | — |
| M025 | `_spawn_worker:588` | return none | survived | killed | `test_spawn_worker_returns_detached_process_pid` |
| M026 | `_timeout_response:595` | return none | survived | killed | `test_timeout_response_has_error_and_receipt` |
| M027 | `_request_preflight:607` | return none | survived | killed | `test_request_preflight_returns_timeout_response` |
| M028 | `start:623` | comparison | killed | killed | — |
| M029 | `continue_:680` | comparison | killed | killed | — |
| M030 | `_admission:720` | comparison | survived | killed | `test_admission_uses_remaining_deadline_as_gate_timeout` |
| M031 | `_release_quietly:741` | drop statement | survived | killed | `test_release_quietly_releases_owned_claim` |
| M032 | `_prepare_job:793` | drop statement | survived (time-boxed) | killed | `test_prepare_job_writes_request_and_deadline` |
| M033 | `_prepare_job:786` | arithmetic | killed | killed | — |
| M034 | `_dispatch:813` | return none | killed | killed | — |
| M035 | `poll:846` | return none | killed | killed | — |
| M036 | `read:874` | return none | killed | killed | — |
| M037 | `close:921` | return none | killed | killed | — |

## Survivors left

None. Equivalent: none identified. Untestable offline: none. Defect: none.

## Defects found

None.

## New tests

- `BrokerMutationTests.test_error_response_keeps_closed_category_and_identifier` kills M001 in isolation.
- `BrokerMutationTests.test_validate_common_accepts_exact_budget_and_refuses_blank_prompt` kills M004 in isolation.
- `BrokerMutationTests.test_receipt_count_accepts_zero_but_not_negative` kills M007 in isolation.
- `BrokerMutationTests.test_receipt_duration_accepts_zero_but_not_negative` kills M009 in isolation.
- `BrokerMutationTests.test_receipt_hash_accepts_exact_lowercase_digest` kills M010 in isolation.
- `BrokerMutationTests.test_receipt_chunk_counts_counts_each_state` kills M011 in isolation.
- `BrokerMutationTests.test_receipt_pass_states_keeps_pass_order` kills M012 in isolation.
- `BrokerMutationTests.test_validate_delegation_receipt_refuses_unknown_task` kills M013 in isolation.
- `BrokerMutationTests.test_handoff_json_refuses_nonfinite_constant` kills M014 in isolation.
- `BrokerMutationTests.test_read_clearance_accepts_matching_digest` kills M015 in isolation.
- `BrokerMutationTests.test_initial_handoff_identity_returns_reviewed_identity` kills M016 in isolation.
- `BrokerMutationTests.test_validate_handoff_accepts_matching_preparation` kills M017 in isolation.
- `BrokerMutationTests.test_sqlite_uri_path_escapes_path_metacharacters` kills M018 in isolation.
- `BrokerMutationTests.test_consume_receipt_once_rejects_replay` kills M019 in isolation.
- `BrokerMutationTests.test_digest_receipt_routed_locally_accepts_fresh_local_receipt` kills M021 in isolation.
- `BrokerMutationTests.test_validate_local_first_checks_voluntary_shape` kills M022 in isolation.
- `BrokerMutationTests.test_validate_identifier_request_returns_canonical_id` kills M023 in isolation.
- `BrokerMutationTests.test_spawn_worker_returns_detached_process_pid` kills M025 in isolation.
- `BrokerMutationTests.test_timeout_response_has_error_and_receipt` kills M026 in isolation.
- `BrokerMutationTests.test_request_preflight_returns_timeout_response` kills M027 in isolation.
- `BrokerMutationTests.test_admission_uses_remaining_deadline_as_gate_timeout` kills M030 in isolation.
- `BrokerMutationTests.test_release_quietly_releases_owned_claim` kills M031 in isolation.
- `BrokerMutationTests.test_prepare_job_writes_request_and_deadline` kills M032 in isolation.

## Verification

`python3 tests/test_suite.py` passed. `test_onboard.py` passed. `test_broker_mutations.py` passed (23 tests). `test_local_worker.py` was exercised but its 14 tests could not start: this sandbox refused the local `127.0.0.1` fixture socket with `PermissionError: [Errno 1] Operation not permitted`. No external endpoint was contacted.
