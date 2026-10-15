# Mutation sweep: `src/agent_bridge/orchestration/mcp.py`

## Method

Nineteen single-site mutants covered every named function in the module, including
the handlers nested in `build_tools`. Each mutant changed executable code only.
The operators included comparison flips, `or` to `and`, Boolean inversions,
a numeric increment, a return-value change, statement removal, `+` to `-`,
and dropping a raise. Each run loaded one mutated copy in a fresh Python
process and ran the focused offline tests with a 45-second timeout. The
production file was never written; the runner compared its bytes before and
after the sweep. Its final SHA-256 was
`586afba9a1102dd21b8a181230948dd9a75c3b33b008adf552be91c67a14df75`.

The focused existing run used 53 tests from `test_orchestration_server`,
`test_local_vocabulary`, `test_local_first.UnderAnyTests`,
`test_extract_classify.ExtractClassifyTests`,
`test_execution_dispatcher.ExecutionDispatchClassificationTests`,
`test_delegation_gate.RoutingDecideToolTests`, and two MCP helper tests in
`test_codex_review`. The after run added the eight tests below (61 total).

| Stage | Killed | Generated | Kill rate |
| --- | ---: | ---: | ---: |
| Existing tests | 11 | 19 | 57.9% |
| Existing and new tests | 19 | 19 | 100% |

## Mutants

| ID | Function | Single change | Before | After | New test |
| --- | --- | --- | --- | --- | --- |
| `schema_extra` | `_schema` | Allow additional properties | survived | killed | `test_schema_disallows_unknown_properties` |
| `under_any_or` | `_under_any` | `or` to `and` for root matching | killed | killed | — |
| `trailer_count` | `_trailer_author` | `== 1` to `!= 1` | killed | killed | — |
| `trailer_numeric` | `_trailer_author` | Change provider count from 1 to 2 | killed | killed | — |
| `trailer_statement` | `_trailer_author` | Remove provider insertion | killed | killed | — |
| `route_mismatch` | `_execution_route_classifications` | `!=` to `==` | killed | killed | — |
| `route_drop_raise` | `_execution_route_classifications` | Drop mismatch raise | killed | killed | — |
| `commit_check` | `_commit` | Disable subprocess error check | survived | killed | `test_commit_rejects_invalid_revision` |
| `caller_guard` | `build_tools` | Negate allowed-caller guard | killed | killed | — |
| `call_ok` | `call` | Return `ok: False` on success | killed | killed | — |
| `route_conflict` | `route_local` | Invert idempotency conflict guard | survived | killed | `test_route_local_rejects_checkpoint_and_idempotency_key_together` |
| `route_add_sub` | `route_local` | Change task-id `+` to `-` | killed | killed | — |
| `digest_symlink` | `digest_file` | Invert symlink guard | survived | killed | `test_digest_file_rejects_symlink_before_policy_lookup` |
| `certified_cap` | `certified_file` | `>` to `>=` at input cap | survived | killed | `test_certified_file_accepts_exact_input_cap` |
| `result_boundary` | `work_result` | `>` to `>=` at output cap | survived | killed | `test_work_result_at_character_limit_is_not_truncated` |
| `decision_revision` | `decide_routing` | `!=` to `==` on revision | killed | killed | — |
| `dispatch_route` | `dispatch_execution` | Invert allowed-route membership | killed | killed | — |
| `review_author` | `dispatch_review` | `==` to `!=` on author/provider | survived | killed | `test_review_dispatch_refuses_same_author_and_reviewer` |
| `verify_exclusive` | `verify_review_result` | `==` to `!=` on receipt-source check | survived | killed | `test_review_verify_requires_exactly_one_receipt_source` |

All eight new tests pass against the original module and fail on their named
mutant. No equivalent mutants or behavior-changing survivors remain in this
sample. No expected failures were added.

## Survivors left

None. Equivalent: 0. Untestable offline: 0. Defect: 0.

## Defects found

None in the sampled mutations.

## Verification

`python3 -m unittest discover -s tests -p 'test_mcp_mutation.py' -q` passed (8 tests).
`python3 tests/test_suite.py` passed (593 checks, 0 failures, 5 environmental skips).
`test_hostenv.py`, `test_automatic_gate.py`, and `test_delegation_audit.py`
passed together (241 tests, 11 skips).
`test_onboard.py` passed (28 tests). `test_local_worker.py` could not start its
localhost HTTP fixture because socket binding is denied by this sandbox; all
14 tests errored during fixture setup. The orchestration end-to-end tests had
the same fixture limitation (42 errors). These did not affect the mutation runs.
