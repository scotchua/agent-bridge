# Mutation test: `src/agent_bridge/localq/backend_select.py`

The original module was read in full. Mutants were compiled from a temporary source copy and loaded into a fresh Python subprocess; the production file was never rewritten. Each run had a 15-second timeout, an isolated temporary `HOME`, and bytecode writing disabled. The pre-existing probes were four `ConfigAndSelectionTests` methods in `tests/test_gemma_certified.py`: `test_selected_backend_is_fixed_and_summarize_only_by_default`, `test_explicit_pilot_opt_in_selects_only_named_tasks`, `test_live_digest_uses_certified_gemma_call_shape`, and `test_checkpoint_and_route_measure_the_same_unicode_payload`. The same probes and the new tests were run after hardening.

| Result | Killed | Generated | Kill rate |
| --- | ---: | ---: | ---: |
| Before new tests | 17 | 33 | 51.5% |
| After new tests | 33 | 33 | 100% |

The 33 single-edit mutants cover both functions, their constants, each backend branch, command construction, validation, and return values. No comments, logging, or docstrings were mutated. `M18` simulates an unsupported platform in a test; it is not a live Windows run.

| ID | Mutation | Before | After |
| --- | --- | --- | --- |
| M01 | Input cap plus one | Survived | Killed |
| M02 | Timeout subtracts margin | Survived | Killed |
| M03 | Lease multiplier plus one | Survived | Killed |
| M04 | Lease subtracts margin | Killed | Killed |
| M05 | Queue caps return `None` | Killed | Killed |
| M06 | Certified default timeout plus one | Survived | Killed |
| M07 | Queue margin plus one | Survived | Killed |
| M08 | Invert backend membership condition | Killed | Killed |
| M09 | Drop unsupported-backend raise | Survived | Killed |
| M10 | Flip private-backend comparison | Killed | Killed |
| M11 | Private branch uses Gemma child | Survived | Killed |
| M12 | Change private worker flag | Survived | Killed |
| M13 | Change private state flag | Survived | Killed |
| M14 | Change private queue-root flag | Survived | Killed |
| M15 | Private branch returns empty caps | Survived | Killed |
| M16 | Private branch returns empty allowed tasks | Survived | Killed |
| M17 | Flip platform comparison | Killed | Killed |
| M18 | Drop platform refusal raise | Survived | Killed |
| M19 | Flip missing-field comparison | Killed | Killed |
| M20 | Drop missing-field raise | Survived | Killed |
| M21 | Negate missing-field condition | Killed | Killed |
| M22 | Change Gemma delegate flag | Killed | Killed |
| M23 | Change Gemma Python flag | Killed | Killed |
| M24 | Change Gemma receipt-root flag | Killed | Killed |
| M25 | Change Gemma validator flag | Killed | Killed |
| M26 | Change Gemma digest flag | Killed | Killed |
| M27 | Invert pilot-task type check | Killed | Killed |
| M28 | Swap pilot-task `or` for `and` | Survived | Killed |
| M29 | Invert pilot-task membership check | Killed | Killed |
| M30 | Drop pilot-task rejection raise | Survived | Killed |
| M31 | Gemma branch returns empty allowed tasks | Killed | Killed |
| M32 | Gemma branch returns `False` | Killed | Killed |
| M33 | Remove pilot-task assignment | Killed | Killed |

## New tests

All six tests pass against the original module. Each was also run alone against the named mutant and failed:

| Test in `tests/test_backend_select_mutation.py` | Mutant killed alone |
| --- | --- |
| `GemmaQueueCapsTests.test_certified_defaults_and_explicit_timeout_keep_exact_queue_headroom` | M01 |
| `BuildBackendAndCapsTests.test_private_worker_defaults_to_exact_child_command_and_caps` | M11 |
| `BuildBackendAndCapsTests.test_unknown_backend_reports_unsupported` | M09 |
| `BuildBackendAndCapsTests.test_gemma_refuses_non_posix_platform_before_building_command` | M18 |
| `BuildBackendAndCapsTests.test_gemma_requires_every_field_on_hand_built_config` | M20 |
| `BuildBackendAndCapsTests.test_gemma_rejects_unreviewed_pilot_task_on_hand_built_config` | M28 |

## Survivors left

None. No equivalent or untestable-offline mutants were identified among the 33 generated edits.

## Defects found

None. No expected failures were added.

## Verification

- `python3 tests/test_suite.py`: 592 passed, 0 failed, 5 environmental skips.
- `test_gemma_certified.py`: 31 tests passed.
- `test_backend_select_mutation.py`: 6 tests passed.
- `test_onboard.py`: 28 tests passed.
- The production module's SHA-256 was `f67e14aebf5d04282291c9f292e2727674f9fd901534491c20028eab9127d2fc` before and after the mutation runs.
