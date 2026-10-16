# Mutation results: `src/agent_bridge/chat/__main__.py`

## Method

A temporary runner made one source mutation at a time in a separate file and executed the copied module in a subprocess. It never wrote to the production module. Mutations were selected from executable statements in all four callable paths: `_NoRedirect.redirect_request`, `_local_opener`, `build_app`, and `main`. They cover comparison and Boolean changes, condition negation, numeric changes, return replacement, statement removal, `-` to `+`, and dropped raises. No comment, logging statement, or docstring was mutated. Each run had a 12-second timeout; none timed out.

The relevant existing tests were `test_chat_launch.py` (22 tests). After adding `test_chat_launch_mutation.py`, the same run covered 32 tests. A socketless `ThreadingHTTPServer` double was installed only in the subprocess because this sandbox denies local socket binding. The tests otherwise exercised the original entry point, real room storage, and local state files. Each subprocess used an isolated temporary home and the committed default broker config. No model inference or external API call was made.

Production module SHA-256 before and after mutation runs: `0d7f3418ee1b42ec9c65da4718ccc8f4a81526ce94c25feaa3e43cb03da479ac`.

## Kill rates

| Phase | Mutants generated | Killed | Survived | Kill rate |
| --- | ---: | ---: | ---: | ---: |
| Existing tests | 42 | 17 | 25 | 40.5% |
| Existing and new tests | 42 | 42 | 0 | 100% |

| Mutant | Source line | Before | After |
| --- | ---: | --- | --- |
| `redirect-return` | 24 | killed | killed |
| `opener-return` | 28 | killed | killed |
| `umask` | 32 | killed | killed |
| `private-root` | 34 | survived | killed |
| `private-state` | 36 | killed | killed |
| `candidate-condition` | 38 | killed | killed |
| `state-boundary` | 40 | survived | killed |
| `state-raise` | 41 | survived | killed |
| `client-policy` | 46 | survived | killed |
| `verify-root` | 49 | survived | killed |
| `hermes-condition` | 51 | killed | killed |
| `hermes-file-condition` | 53 | killed | killed |
| `hermes-file-raise` | 54 | survived | killed |
| `hermes-ready` | 56 | survived | killed |
| `grok-condition` | 58 | killed | killed |
| `grok-root-equality` | 60 | killed | killed |
| `grok-boundary-or` | 60 | killed | killed |
| `grok-boundary-raise` | 61 | killed | killed |
| `grok-ready` | 63 | killed | killed |
| `max-chars-constant` | 67 | survived | killed |
| `max-chars-offset` | 67 | survived | killed |
| `max-chars-arithmetic` | 67 | survived | killed |
| `empty-room-condition` | 68 | killed | killed |
| `initial-room` | 69 | killed | killed |
| `recover` | 70 | killed | killed |
| `token-size` | 72 | survived | killed |
| `peer-filter` | 74 | killed | killed |
| `build-return` | 80 | killed | killed |
| `lock-timeout` | 95 | survived | killed |
| `probe-status` | 103 | survived | killed |
| `probe-status-raise` | 104 | survived | killed |
| `probe-return` | 107 | survived | killed |
| `busy-raise` | 109 | survived | killed |
| `runtime-write` | 113 | survived | killed |
| `peer-runtime-write` | 114 | survived | killed |
| `dispatcher-start` | 116 | survived | killed |
| `serve` | 120 | survived | killed |
| `dispatcher-close` | 125 | survived | killed |
| `server-close` | 128 | survived | killed |
| `runtime-unlink` | 129 | survived | killed |
| `lock-exit` | 131 | survived | killed |
| `main-return` | 132 | survived | killed |

## New tests

All ten tests pass on the original module. Each was also run alone against a recorded mutant and failed on that mutant.

- `test_build_app_prepares_and_verifies_room_root`
- `test_build_app_refuses_configured_state_outside_room`
- `test_build_app_refuses_client_opt_in_without_bridge_policy`
- `test_build_app_requires_hermes_file`
- `test_build_app_excludes_unready_hermes_from_participants`
- `test_build_app_caps_prompt_at_both_limits`
- `test_build_app_uses_full_size_tokens`
- `test_main_probes_healthy_existing_room`
- `test_main_rejects_stale_or_missing_existing_room`
- `test_main_writes_runtime_starts_workers_and_cleans_up`

## Survivors left

None. Equivalent: none. Untestable offline: none. Defect: none.

## Defects found

None.

## Verification

- Socketless offline launch suite on original source: 32 tests passed.
- `python3 tests/test_suite.py` with an isolated temporary home: 592 passed, 0 failed, 5 environmental skips.
- `test_onboard.py` with an isolated temporary home: 28 passed.
- `test_local_worker.py` was exercised, but its 14 tests could not bind their local test HTTP server in this sandbox (`PermissionError: Operation not permitted`).
