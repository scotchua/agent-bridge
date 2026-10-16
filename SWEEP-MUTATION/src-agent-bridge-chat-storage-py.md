# Mutation sweep: `src/agent_bridge/chat/storage.py`

Source SHA-256: `04b0fd9c4190425274e25c33a5cb3fd77a0827db46a0dded601aa5c53c242500`. The original module was loaded unchanged throughout; each mutant was written to a unique temporary module path and loaded into a fresh subprocess. No network or packages were used.

## Results

| Stage | Killed | Generated | Kill rate | Survivors |
| --- | ---: | ---: | ---: | ---: |
| Existing storage, management, discussion and dispatch tests | 21 | 36 | 58.3% | 15 |
| Same tests plus new mutation tests | 36 | 36 | 100% | 0 |

Each subprocess had an 8-second timeout. No timeout occurred. Mutants cover every `RoomStore` method and include comparison flips, `and`/`or`, condition negation, a numeric change by one, empty/false/`None` returns, statement removal, `+`/`-`, and a removed raise. Comments, logging and docstrings were excluded.

## Mutants generated

| ID | Function | Line | Change | Before | After |
| --- | --- | ---: | --- | --- | --- |
| M01 | `__init__` | 18 | `or` → `and` | survived | killed |
| M02 | `__init__` | 23 | version `raise` → `pass` | survived | killed |
| M03 | `db` | 42 | foreign-key PRAGMA → `pass` | survived | killed |
| M04 | `create_room` | 50 | `> 100` → `>= 100` | survived | killed |
| M05 | `create_room` | 55 | `return room` → `return {}` | killed | killed |
| M06 | `rooms` | 59 | room list → `[]` | killed | killed |
| M07 | `_room` | 62 | UUID `!=` → `==` | killed | killed |
| M08 | `_room` | 64 | room existence condition negated | killed | killed |
| M09 | `submit` | 68 | message limit `>` → `>=` | survived | killed |
| M10 | `submit` | 70 | request ID limit `<=` → `<` | survived | killed |
| M11 | `submit` | 79 | note guard `and` → `or` | killed | killed |
| M12 | `submit` | 81 | recipient count `< 2` → `<= 2` | killed | killed |
| M13 | `submit` | 90 | idempotency `!=` → `==` | killed | killed |
| M14 | `submit` | 94 | active-round `or` → `and` | killed | killed |
| M15 | `submit` | 78 | recipient normalization → `pass` | survived | killed |
| M16 | `snapshot` | 121 | snapshot result → `None` | killed | killed |
| M17 | `recover_interrupted` | 126 | recovery count → `0` | killed | killed |
| M18 | `claim_next` | 132 | empty-queue condition negated | killed | killed |
| M19 | `claim_next` | 135 | round condition negated | killed | killed |
| M20 | `context` | 143 | initial cursor `0` → `1` | survived | killed |
| M21 | `context` | 146 | context rows → `[]` | killed | killed |
| M22 | `is_running` | 151 | running `==` → `!=` | killed | killed |
| M23 | `finish` | 156 | running-job guard negated | killed | killed |
| M24 | `finish` | 162 | completion update → `pass` | killed | killed |
| M25 | `fail` | 166 | failure update → `pass` | killed | killed |
| M26 | `stop` | 172 | cancel count result → `{}` | survived | killed |
| M27 | `reset_session` | 177 | pending-work guard negated | survived | killed |
| M28 | `reset_session` | 179 | session deletion → `pass` | survived | killed |
| M29 | `preferences` | 184 | lead update condition negated | killed | killed |
| M30 | `preferences` | 185 | invalid-lead `or` → `and` | survived | killed |
| M31 | `preferences` | 191 | default preferences → `{}` | killed | killed |
| M32 | `rename_room` | 193 | title limit `>` → `>=` | survived | killed |
| M33 | `rename_room` | 198 | rename result `True` → `False` | survived | killed |
| M34 | `delete_room` | 207 | room deletion → `pass` | killed | killed |
| M35 | `delete_room` | 207 | delete result `True` → `False` | survived | killed |
| M36 | `submit` | 83 | payload `+` → `-` | killed | killed |

## New tests

All tests below are in `tests/test_chat_storage_mutations.py`. Each was run alone against the listed mutant: it failed on the mutant and passed on the original.

| Mutant | Test |
| --- | --- |
| M01 | `test_init_rejects_duplicate_participants` |
| M02 | `test_init_rejects_unknown_database_version` |
| M03 | `test_db_enforces_foreign_keys` |
| M04 | `test_create_room_accepts_100_character_title` |
| M09 | `test_submit_accepts_exact_message_limit` |
| M10 | `test_submit_accepts_100_character_request_id` |
| M15 | `test_submit_deduplicates_and_sorts_recipients` |
| M20 | `test_context_includes_first_message_without_session` |
| M26 | `test_stop_reports_cancelled_job_count` |
| M27 | `test_reset_session_rejects_pending_job` |
| M28 | `test_reset_session_removes_previous_conversation` |
| M30 | `test_preferences_rejects_unregistered_lead` |
| M32 | `test_rename_room_accepts_100_character_title` |
| M33 | `test_rename_room_reports_success` |
| M35 | `test_delete_room_reports_success` |

## Survivors left

None. There were no equivalent, untestable-offline, or defect mutants in this bounded set.

## Defects found

None.

## Verification

- `python3 -m unittest discover -s tests -p test_chat_storage_mutations.py -q`: 15 passed.
- Targeted mutant checks: all 15 new tests failed on their mapped mutants.
- `python3 tests/test_onboard.py`: 28 passed.
- `python3 tests/test_suite.py`: 593 passed, 0 failed, 5 environmental skips.
- `python3 tests/test_local_worker.py`: 14 environment errors because this sandbox denies loopback socket binds (`PermissionError: [Errno 1] Operation not permitted`).

