# Mutation test: `src/agent_bridge/localq/worker_child.py`

The temporary standard-library runner made 29 single-edit mutants, covering all four functions. It compiled each changed source in memory as `agent_bridge.localq.worker_child`, then ran `RuntimeTests` from `tests/test_localq_runtime.py` in a fresh process with an eight-second limit. The second pass also ran `WorkerChildMutationTests`. No production file was written. The source SHA-256 before and after was `43287841d84e77961268633a65eb868eeb8174ad8cbbef08515b5919ebdb43b7`.

| Pass | Killed | Generated | Kill rate |
| --- | ---: | ---: | ---: |
| Existing tests | 16 | 29 | 55.2% |
| Existing plus new tests | 29 | 29 | 100.0% |

Mutations were confined to executable code. They covered condition negation, `and`/`or`, `==`/`!=`, numeric constants changed by one, return replacement with `None`, one-statement removal, `+`/`-`, and dropped raises. The source has no executable `<`, `<=`, `>`, or `>=` comparison to flip. Comments, logging, and docstrings were excluded.

| Mutant | Function | Change | Before | After | New test that kills it |
| --- | --- | --- | --- | --- | --- |
| P01 | `_paths` | Negate worker absolute-path check | killed | killed | — |
| P02 | `_paths` | Negate worker file check | killed | killed | — |
| P03 | `_paths` | Negate worker symlink check | killed | killed | — |
| P04 | `_paths` | Drop invalid-state raise | survived | killed | `test_paths_reject_missing_state_directory` |
| P05 | `_paths` | Return `None` | killed | killed | — |
| V01 | `_provenance` | Negate database-file check | killed | killed | — |
| V02 | `_provenance` | Remove connection close | survived | killed | `test_provenance_closes_connection` |
| V03 | `_provenance` | Change rejected-row `or` to `and` | survived | killed | `test_provenance_rejects_unapproved_classification` |
| V04 | `_provenance` | Return `None` | killed | killed | — |
| V05 | `_provenance` | Change URI `+` to `-` | killed | killed | — |
| I01 | `invoke` | Change payload-check `or` to `and` | survived | killed | `test_invoke_rejects_unsupported_task_before_worker_call` |
| I02 | `invoke` | Negate instruction-type check | killed | killed | — |
| I03 | `invoke` | Negate provider-membership check | killed | killed | — |
| I04 | `invoke` | Change default timeout from 65 to 64 | survived | killed | `test_invoke_uses_default_timeout` |
| I05 | `invoke` | Flip process exit `!=` to `==` | killed | killed | — |
| I06 | `invoke` | Flip response-line `!=` to `==` | killed | killed | — |
| I07 | `invoke` | Read response line zero instead of one | killed | killed | — |
| I08 | `invoke` | Negate worker-error check | killed | killed | — |
| I09 | `invoke` | Flip content-length `!=` to `==` | killed | killed | — |
| I10 | `invoke` | Flip draft-status `!=` to `==` | killed | killed | — |
| I11 | `invoke` | Return `None` for provider | killed | killed | — |
| I12 | `invoke` | Return `None` for worker job ID | survived | killed | `test_invoke_returns_worker_job_id` |
| I13 | `invoke` | Change content-check `or` to `and` | survived | killed | `test_invoke_rejects_non_list_content` |
| M01 | `main` | Negate payload-object check | survived | killed | `test_main_success_prints_result_and_returns_zero` |
| M02 | `main` | Return `None` on success | survived | killed | `test_main_success_prints_result_and_returns_zero` |
| M03 | `main` | Return `None` on failure | survived | killed | `test_main_failure_returns_one_and_preserves_fixed_reason` |
| M04 | `main` | Negate fixed-error-detail check | survived | killed | `test_main_failure_returns_one_and_preserves_fixed_reason` |
| M05 | `main` | Drop invalid-JSON raise | survived | killed | `test_main_invalid_json_reports_fixed_reason` |
| M06 | `main` | Remove error-detail assignment | survived | killed | `test_main_failure_returns_one_and_preserves_fixed_reason` |

Each of the ten named tests was also run alone against the listed surviving mutant and exited with an assertion failure. The same ten tests pass on the original module.

## Survivors left

None. No mutants were classified as equivalent, untestable offline, or a defect in the original code.

## Defects found

None.

## Verification

- `python3 -m unittest discover -s tests -p test_worker_child_mutation.py`: 10 tests passed.
- `python3 tests/test_suite.py`: 592 passed, 0 failed, 5 environment skips.
- `python3 tests/test_onboard.py`: passed in an isolated temporary home.
- `python3 tests/test_local_worker.py`: its 14 tests could not start because the sandbox refused the local HTTP fixture's `127.0.0.1` bind (`PermissionError: [Errno 1] Operation not permitted`). This does not reflect a worker-child assertion failure.
