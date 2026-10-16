# Mutation sweep: `src/agent_bridge/capacity_router.py`

## Method and result

- Generated 89 single-change AST mutants across all 21 functions/methods. Operators: comparison flips, `and`/`or` swaps, condition negation, numeric constant `+1`, return replacement with `None`, `[]`, or `False`, single statement removal, `+`/`-` swap, and exception-raise removal. Comments, logging, and docstrings were not mutation targets.
- Each mutant ran in a fresh subprocess with an 8-second limit. The subprocess loaded the mutant as an in-memory `agent_bridge.capacity_router` module and ran `tests/test_capacity_router.py`; no production file was rewritten. No run timed out.
- The **before** run used the committed version of `tests/test_capacity_router.py` in a temporary directory. The **after** run used the updated test file. Both used the same 89 mutants and original source bytes.
- Before: **64/89 killed (71.9%)**; 25 survived. After: **89/89 killed (100.0%)**; 0 survived.
- Production source matched `HEAD` byte-for-byte after the sweep (SHA-256 `96e51e847b75b4f8b8a19340c5e15a8675e28c38b7cb2f61441d9c4184cbcb3e`).

## New tests grouped by router function

Each test below passed on the original and failed when run alone against at least one listed mutant. All names belong to `CapacityRouterMutationGuards` in `tests/test_capacity_router.py`.

| Router function | New test | Baseline survivors killed |
| --- | --- | --- |
| `StageRouter.__init__ / _init_db` | `test_init_creates_private_parent_and_wal_database` | M004, M007, M041 |
| `StageRouter._init_db` | `test_init_explicitly_sets_full_synchronization` | M027 |
| `StageRouter._connect` | `test_connect_uses_bounded_sqlite_wait` | M025 |
| `StageRouter._db` | `test_db_context_closes_connection` | M026 |
| `StageRouter._routes` | `test_routes_reject_empty_list` | M043, M057 |
| `StageRouter.register` | `test_register_rejects_blank_identity_and_returns_row` | M059, M071 |
| `StageRouter._row` | `test_missing_stage_has_routing_error` | M060 |
| `StageRouter._event` | `test_register_records_event` | M011 |
| `StageRouter.capacity_rows` | `test_capacity_rows_returns_stored_observation` | M015, M082, M083 |
| `validate_observation` | `test_observation_rejects_unknown_route` | M067 |
| `StageRouter._fresh_routes` | `test_future_capacity_cannot_assign` | M047 |
| `StageRouter.assign` | `test_assign_rejects_blank_owner` | M073 |
| `StageRouter.renew` | `test_renew_rejects_zero_lease` | M050, M063 |
| `StageRouter.report` | `test_report_distinguishes_stale_boundary_and_unavailable` | M053, M077 |
| `capacity_fingerprint` | `test_fingerprint_includes_inclusive_time_boundaries` | M040, M055, M068 |

## Individual mutants

| ID | Function | Source line | Change | Before | After |
| --- | --- | ---: | --- | --- | --- |
| M001 | `validate_observation` | 382 | `negate_condition` | killed | killed |
| M002 | `upsert_observation` | 394 | `remove_statement` | killed | killed |
| M003 | `capacity_fingerprint` | 442 | `return_none` | killed | killed |
| M004 | `__init__` | 78 | `remove_statement` | survived | killed |
| M005 | `_connect` | 85 | `return_none` | killed | killed |
| M006 | `_db` | 91 | `remove_statement` | killed | killed |
| M007 | `_init_db` | 97 | `remove_statement` | survived | killed |
| M008 | `_routes` | 128 | `negate_condition` | killed | killed |
| M009 | `observe_capacity` | 142 | `remove_statement` | killed | killed |
| M010 | `register` | 152 | `negate_condition` | killed | killed |
| M011 | `_event` | 179 | `remove_statement` | survived | killed |
| M012 | `_row` | 186 | `negate_condition` | killed | killed |
| M013 | `_fresh_routes` | 197 | `return_none` | killed | killed |
| M014 | `retract_capacity` | 213 | `negate_condition` | killed | killed |
| M015 | `capacity_rows` | 225 | `return_none` | survived | killed |
| M016 | `assign` | 231 | `negate_condition` | killed | killed |
| M017 | `renew` | 285 | `negate_condition` | killed | killed |
| M018 | `reconcile_release` | 306 | `negate_condition` | killed | killed |
| M019 | `complete` | 344 | `return_none` | killed | killed |
| M020 | `get` | 353 | `return_none` | killed | killed |
| M021 | `report` | 376 | `return_none` | killed | killed |
| M022 | `validate_observation` | 384 | `negate_condition` | killed | killed |
| M023 | `capacity_fingerprint` | 439 | `boolean` | killed | killed |
| M024 | `__init__` | 80 | `remove_statement` | killed | killed |
| M025 | `_connect` | 83 | `constant_plus_one` | survived | killed |
| M026 | `_db` | 93 | `remove_statement` | survived | killed |
| M027 | `_init_db` | 98 | `remove_statement` | survived | killed |
| M028 | `_routes` | 130 | `return_none` | killed | killed |
| M029 | `observe_capacity` | 144 | `remove_statement` | killed | killed |
| M030 | `register` | 156 | `negate_condition` | killed | killed |
| M031 | `_row` | 188 | `return_none` | killed | killed |
| M032 | `_fresh_routes` | 200 | `arithmetic` | killed | killed |
| M033 | `retract_capacity` | 213 | `comparison` | killed | killed |
| M034 | `assign` | 233 | `negate_condition` | killed | killed |
| M035 | `renew` | 301 | `return_none` | killed | killed |
| M036 | `reconcile_release` | 325 | `return_none` | killed | killed |
| M037 | `complete` | 331 | `remove_statement` | killed | killed |
| M038 | `report` | 363 | `negate_condition` | killed | killed |
| M039 | `validate_observation` | 387 | `negate_condition` | killed | killed |
| M040 | `capacity_fingerprint` | 440 | `comparison` | survived | killed |
| M041 | `__init__` | 78 | `constant_plus_one` | survived | killed |
| M042 | `_init_db` | 99 | `remove_statement` | killed | killed |
| M043 | `_routes` | 128 | `boolean` | survived | killed |
| M044 | `observe_capacity` | 145 | `remove_statement` | killed | killed |
| M045 | `register` | 158 | `negate_condition` | killed | killed |
| M046 | `_row` | 186 | `comparison` | killed | killed |
| M047 | `_fresh_routes` | 200 | `constant_plus_one` | survived | killed |
| M048 | `retract_capacity` | 214 | `drop_raise` | killed | killed |
| M049 | `assign` | 281 | `return_none` | killed | killed |
| M050 | `renew` | 285 | `comparison` | survived | killed |
| M051 | `reconcile_release` | 306 | `boolean` | killed | killed |
| M052 | `complete` | 333 | `negate_condition` | killed | killed |
| M053 | `report` | 363 | `comparison` | survived | killed |
| M054 | `validate_observation` | 382 | `comparison` | killed | killed |
| M055 | `capacity_fingerprint` | 441 | `comparison` | survived | killed |
| M056 | `_init_db` | 109 | `negate_condition` | killed | killed |
| M057 | `_routes` | 129 | `drop_raise` | survived | killed |
| M058 | `observe_capacity` | 146 | `remove_statement` | killed | killed |
| M059 | `register` | 175 | `return_none` | survived | killed |
| M060 | `_row` | 187 | `drop_raise` | survived | killed |
| M061 | `retract_capacity` | 216 | `remove_statement` | killed | killed |
| M062 | `assign` | 232 | `drop_raise` | killed | killed |
| M063 | `renew` | 286 | `drop_raise` | survived | killed |
| M064 | `reconcile_release` | 307 | `drop_raise` | killed | killed |
| M065 | `complete` | 336 | `negate_condition` | killed | killed |
| M066 | `report` | 365 | `negate_condition` | killed | killed |
| M067 | `validate_observation` | 383 | `drop_raise` | survived | killed |
| M068 | `capacity_fingerprint` | 440 | `arithmetic` | survived | killed |
| M069 | `_init_db` | 113 | `remove_statement` | killed | killed |
| M070 | `_routes` | 128 | `comparison` | killed | killed |
| M071 | `register` | 152 | `boolean` | survived | killed |
| M072 | `retract_capacity` | 219 | `remove_statement` | killed | killed |
| M073 | `assign` | 233 | `boolean` | survived | killed |
| M074 | `renew` | 289 | `remove_statement` | killed | killed |
| M075 | `reconcile_release` | 310 | `remove_statement` | killed | killed |
| M076 | `complete` | 339 | `remove_statement` | killed | killed |
| M077 | `report` | 362 | `boolean` | survived | killed |
| M078 | `_connect` | 85 | `return_empty` | killed | killed |
| M079 | `_connect` | 85 | `return_false` | killed | killed |
| M080 | `_routes` | 130 | `return_empty` | killed | killed |
| M081 | `_routes` | 130 | `return_false` | killed | killed |
| M082 | `capacity_rows` | 225 | `return_empty` | survived | killed |
| M083 | `capacity_rows` | 225 | `return_false` | survived | killed |
| M084 | `retract_capacity` | 220 | `return_empty` | killed | killed |
| M085 | `retract_capacity` | 220 | `return_false` | killed | killed |
| M086 | `capacity_fingerprint` | 442 | `return_empty` | killed | killed |
| M087 | `capacity_fingerprint` | 442 | `return_false` | killed | killed |
| M088 | `report` | 376 | `return_empty` | killed | killed |
| M089 | `report` | 376 | `return_false` | killed | killed |

## Survivors left

None. Equivalent: 0; untestable offline: 0; defects: 0.

## Defects found

None.

## Verification

- `python3 tests/test_capacity_router.py`: 35 tests passed.
- `python3 tests/test_suite.py`: 593 passed, 0 failed, 5 platform skips.
- `python3 tests/test_onboard.py`: 28 tests passed with an isolated temporary home.
- `python3 tests/test_local_worker.py`: 14 setup errors because the sandbox denied binding the local test server to `127.0.0.1`; this was not a capacity-router failure.
- The 15 new tests were also run individually against one listed mutant each; each failed on its mutant.
