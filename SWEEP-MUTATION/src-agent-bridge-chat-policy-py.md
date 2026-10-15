# Mutation test report: `src/agent_bridge/chat/policy.py`

## Results

| Test set | Killed | Total | Kill rate | Survived | Timed out |
| --- | ---: | ---: | ---: | ---: | ---: |
| Existing policy tests | 39 | 48 | 81.25% | 9 | 0 |
| Existing plus new mutation tests | 48 | 48 | 100% | 0 | 0 |

Existing tests: `test_chat_policy.PolicyTests` and `test_client_parity.ClientDerivedParityTests.test_room_policy_follows_the_peer_opt_in_symmetrically`.
Each mutant ran in a separate Python process with a 10-second timeout and an isolated temporary HOME. The runner imported the original module and compiled one mutated copy in its module namespace; it never wrote to the production source file. The source bytes were checked after each complete run.

Mutants were selected from executable syntax in all five functions, with a cap of 150. Operators exercised: comparison flip, `and`/`or` swap, condition negation, return replacement with `None`, `False`, or an empty tuple, single statement removal, and exception raise removal. The module has no numeric constants or `+`/`-` expressions eligible for the requested arithmetic mutations. Comments, logging, and docstrings were excluded.

## Mutants generated

| ID | Function | Line | Mutation | Before | After |
| --- | --- | ---: | --- | --- | --- |
| M001 | `__init__` | 12 | negate condition | killed | killed |
| M002 | `__init__` | 12 | swap and/or | killed | killed |
| M003 | `__init__` | 13 | remove statement | killed | killed |
| M004 | `__init__` | 14 | remove statement | killed | killed |
| M005 | `__init__` | 15 | remove statement | killed | killed |
| M006 | `__init__` | 15 | swap and/or | killed | killed |
| M007 | `__init__` | 16 | negate condition | killed | killed |
| M008 | `__init__` | 16 | swap and/or | killed | killed |
| M009 | `__init__` | 17 | drop exception raise | killed | killed |
| M010 | `_allowed` | 20 | negate condition | killed | killed |
| M011 | `_allowed` | 21 | replace return with None | killed | killed |
| M012 | `_allowed` | 21 | replace return with False | killed | killed |
| M013 | `_allowed` | 21 | replace return with () | survived | killed |
| M014 | `_allowed` | 22 | negate condition | killed | killed |
| M015 | `_allowed` | 23 | replace return with None | killed | killed |
| M016 | `_allowed` | 23 | replace return with False | killed | killed |
| M017 | `_allowed` | 23 | replace return with () | survived | killed |
| M018 | `_allowed` | 24 | replace return with None | killed | killed |
| M019 | `_allowed` | 24 | replace return with False | killed | killed |
| M020 | `_allowed` | 24 | replace return with () | killed | killed |
| M021 | `effective_classification` | 27 | negate condition | killed | killed |
| M022 | `effective_classification` | 28 | drop exception raise | killed | killed |
| M023 | `effective_classification` | 29 | negate condition | killed | killed |
| M024 | `effective_classification` | 30 | negate condition | killed | killed |
| M025 | `effective_classification` | 31 | drop exception raise | killed | killed |
| M026 | `effective_classification` | 32 | replace return with None | survived | killed |
| M027 | `effective_classification` | 32 | replace return with False | survived | killed |
| M028 | `effective_classification` | 32 | replace return with () | survived | killed |
| M029 | `effective_classification` | 33 | replace return with None | killed | killed |
| M030 | `effective_classification` | 33 | replace return with False | killed | killed |
| M031 | `effective_classification` | 33 | replace return with () | killed | killed |
| M032 | `authorize` | 36 | negate condition | killed | killed |
| M033 | `authorize` | 37 | negate condition | survived | killed |
| M034 | `authorize` | 39 | remove statement | survived | killed |
| M035 | `authorize` | 41 | drop exception raise | survived | killed |
| M036 | `authorize` | 42 | negate condition | killed | killed |
| M037 | `authorize` | 43 | drop exception raise | killed | killed |
| M038 | `authorize` | 44 | remove statement | killed | killed |
| M039 | `authorize` | 44 | flip comparison (operator 1) | killed | killed |
| M040 | `authorize` | 45 | negate condition | killed | killed |
| M041 | `authorize` | 45 | swap and/or | killed | killed |
| M042 | `authorize` | 47 | swap and/or | killed | killed |
| M043 | `authorize` | 47 | flip comparison (operator 1) | survived | killed |
| M044 | `authorize` | 48 | drop exception raise | killed | killed |
| M045 | `room_config` | 52 | remove statement | killed | killed |
| M046 | `room_config` | 53 | replace return with None | killed | killed |
| M047 | `room_config` | 53 | replace return with False | killed | killed |
| M048 | `room_config` | 53 | replace return with () | killed | killed |

## New tests

| Test in `tests/test_chat_policy_mutations.py` | Recorded mutant it kills |
| --- | --- |
| `AllowedTests.test_unconfigured_policy_uses_all_room_labels` | M013 |
| `AllowedTests.test_global_policy_applies_to_hermes_and_grok` | M017 |
| `EffectiveClassificationTests.test_opted_in_client_derived_label_stays_client_derived` | M026 |
| `AuthorizeTests.test_configured_policy_rejects_unknown_peer` | M034 |
| `AuthorizeTests.test_public_does_not_need_client_opt_in` | M043 |

Each listed test passed on the original source and failed on its listed mutant when run alone. The new tests collectively killed all nine original survivors: M013, M017, M026–M028, M033–M035, and M043.

## Survivors left

None. Equivalent mutants: none identified. Untestable offline mutants: none. Defects found: none.

## Verification

`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests python3 -m unittest -q test_chat_policy_mutations` passed (5 tests).
`python3 tests/test_suite.py` passed with an isolated temporary HOME: 592 passed, 0 failed, 5 environmental skips. `test_onboard` passed with an isolated temporary HOME. `test_local_worker` was exercised but 14 tests could not bind a loopback socket under this sandbox (`PermissionError: [Errno 1] Operation not permitted`); this is unrelated to the chat policy mutation tests.
