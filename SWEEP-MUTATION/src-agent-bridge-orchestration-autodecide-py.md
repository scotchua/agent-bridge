# Mutation sweep: `src/agent_bridge/orchestration/autodecide.py`

## Method and result

Sixteen single-change mutants were chosen, one for each function or property in the module. The temporary runner read the source once, wrote each mutated copy under `/private/tmp`, and loaded it through a temporary `sitecustomize.py`. It also propagated that hook to gate subprocesses launched by the existing tests. Each mutant ran against one relevant existing test with a 30-second limit; the five survivors then ran against one new test each. No network access, package installation, or production-file write was used.

| Measure | Killed | Total | Kill rate |
| --- | ---: | ---: | ---: |
| Before new tests | 11 | 16 | 68.75% |
| After new tests | 16 | 16 | 100% |

The original source's SHA-256 was `2e76374338573a2d6849a04aa5805603c3a470f477905d125e07dcfae14d8bbb` before and after the sweep. All five new tests passed on the original module and each failed when run alone against its named mutant.

Verification on the original source: `python3 tests/test_suite.py` reported 593 passed, zero failed, and five environmental skips; `python3 tests/test_automatic_gate.py -q` passed 169 tests; `python3 tests/test_delegation_audit.py -q` passed 40 tests; `python3 tests/test_autodecide_mutation.py -q` passed all five new tests; and `python3 tests/test_onboard.py -q` passed 28 tests. The optional `test_local_worker.py` and `test_automatic_delegation_e2e.py` runs could not start their local HTTP fixtures because the sandbox denied `127.0.0.1` socket binds. Neither failure was an assertion against the changed tests. `test_hostenv.py` was not run because it includes an outbound network probe prohibited by this task.

## Mutants

| Function | Mutant | Change | Existing test | New test |
| --- | --- | --- | --- | --- |
| `Outcome.retained` | `retained-compare` | `==` to `!=` | Survived | `OutcomeRetained.test_retained_matches_only_the_retain_route` killed |
| `item_id_for` | `item-id-length` | digest slice 16 to 15 | Survived | `ItemIdFor.test_repository_id_has_a_sixteen_character_digest` killed |
| `owner_id_for` | `owner-id-empty` | return `None` | Killed | — |
| `fresh_routes` | `fresh-routes-compare` | `==` to `!=` | Survived | `FreshRoutes.test_only_available_routes_are_eligible` killed |
| `_observe_client_presence` | `presence-minus` | expiry `+` to `-` | Killed | — |
| `_observe_declared_routes` | `declared-and` | `or` to `and` | Survived | `ObserveDeclaredRoutes.test_current_declaration_is_not_withdrawn_before_replay` killed |
| `capacity_fingerprint` | `fingerprint-none` | return `None` | Killed | — |
| `_own_stage` | `own-stage-not-owned` | `==` to `!=` | Killed | — |
| `stage_name` | `stage-first-generation` | `==` to `!=` | Killed | — |
| `intent_path` | `intent-path-empty` | remove intent directory component | Survived | `IntentPath.test_intent_lives_in_the_intents_directory` killed |
| `read_intent` | `read-intent-negate` | negate existence check | Killed | — |
| `_write_intent` | `write-intent-empty` | empty state value | Killed | — |
| `clear_intent` | `clear-intent-negate` | negate mismatch check | Killed | — |
| `retire_superseded_intent` | `retire-intent-negate` | negate existence check | Killed | — |
| `_measured_cpu_idle_ratio` | `idle-bool-and` | remove non-boolean requirement | Killed | — |
| `ensure_decision` | `ensure-client-negate` | negate client membership check | Killed | — |

The existing-test column reflects the selected relevant test for each mutant, not an exhaustive run of every existing test against each mutant. The new tests are in `tests/test_autodecide_mutation.py` and grouped by function.

## Survivors left

None. Equivalent: none. Untestable offline: none. Defects found: none.
