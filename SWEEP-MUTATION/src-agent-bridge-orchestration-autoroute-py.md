# Mutation sweep: `src/agent_bridge/orchestration/autoroute.py`

The source was imported and exercised offline. Twenty-two single-change mutants were generated across every function and property in the module, including the two nested helpers. Each mutant was compiled and executed in a fresh Python subprocess by replacing the module's in-memory namespace; the production file was never edited. The runner gave each subprocess a 20-second limit and checked the source bytes after every run. The runner was removed after this report.

The baseline used relevant existing `unittest` classes in `test_automatic_gate.py`, `test_local_first.py`, and `test_reserved_models.py`. The second pass used the same classes plus `test_autoroute_mutation_hardening.py`.

| Result | Killed | Generated | Kill rate |
| --- | ---: | ---: | ---: |
| Before new tests | 14 | 22 | 63.6% |
| After new tests | 22 | 22 | 100% |

| ID | Function | Mutation | Before | After | New test that kills survivor |
| --- | --- | --- | --- | --- | --- |
| M01 | `RepoPolicy.__post_init__` | `not in` to `in` for routes | killed | killed | — |
| M02 | `LocalFirstConfig.__post_init__` | positive bound `<= 0` to `< 0` | killed | killed | — |
| M03 | `_parse_local_first` | unknown-key set subtraction to intersection | killed | killed | — |
| M04 | `Policy.for_repo` | return default instead of matched entry | killed | killed | — |
| M05 | `_main_worktree` | symlink guard `or` to `and` | survived | killed | `MainWorktreeTests.test_symlinked_git_marker_cannot_inherit_main_policy` |
| M06 | `Load.busy_at` | negate known-load condition | survived | killed | `LoadTests.test_known_load_formats_its_ratio` |
| M07 | `probe_load` | zero-core bound `<= 0` to `< 0` | survived | killed | `ProbeLoadTests.test_zero_cores_reports_unknown_load` |
| M08 | `Signal.__post_init__` | valid task membership inverted | killed | killed | — |
| M09 | `Decision.dispatches` | `!=` to `==` for retain | survived | killed | `DecisionTests.test_dispatches_only_for_a_route` |
| M10 | `decide` | idle rescue `>=` to `>` | survived | killed | `DecideTests.test_idle_at_rescue_threshold_allows_local_route` |
| M11 | `decide` | load ceiling `>=` to `>` | survived | killed | `DecideTests.test_load_at_ceiling_defers_local_route` |
| M12 | `decide` | unclassified comparison `==` to `!=` | killed | killed | — |
| M13 | `_prefers` | preference `<` to `<=` | killed | killed | — |
| M14 | `policy_path` | return `None` | killed | killed | — |
| M15 | `policy_fingerprint` | truncate digest at 31 instead of 32 | survived | killed | `PolicyFingerprintTests.test_policy_digest_has_full_recorded_length` |
| M16 | `parse_policy` | upper load bound `<= 64` to `< 64` | survived | killed | `ParsePolicyTests.test_maximum_documented_local_load_is_accepted` |
| M17 | `reserved_model_match` | substring membership inverted | killed | killed | — |
| M18 | `model_reserved_for` | return `None` instead of matcher | killed | killed | — |
| M19 | `load_policy` | return fingerprint tuple element instead of policy | killed | killed | — |
| M20 | `load_policy_and_fingerprint` | return `None` instead of parsed policy | killed | killed | — |
| M21 | `decide.retain` | return `None` instead of retain route | killed | killed | — |
| M22 | `model_reserved_for.matcher` | return `None` instead of matching token | killed | killed | — |

Survivors left: none. Equivalent mutants: none. Untestable offline mutants: none. Every new test passed on the original and failed on the corresponding mutant above.

## Defects found

None.

## Verification

`python3 tests/test_suite.py`: 593 passed, 0 failed, 5 platform skips. `test_automatic_gate.py`: 169 passed. `test_onboard.py`: 28 passed. `test_reserved_models.py`: 16 passed. `test_hostenv.py`: 21 passed, 11 platform skips. `test_delegation_audit.py`: 40 passed. The eight new tests passed on the original module.

The sandbox denied loopback socket binds, so `test_local_worker.py`, six HTTP-backed cases in `test_local_first.py`, and `test_automatic_delegation_e2e.py` errored in setup before exercising the implementation. This is an environment limit of this run, not a mutation survivor.
