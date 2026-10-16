# Mutation results: `src/agent_bridge/chat/rounds.py`

The throwaway runner compiled each one-change mutant in memory, loaded it into a subprocess, and ran `tests/test_peer_rounds.py` with an 8-second timeout per mutant. The after pass also ran `tests/test_peer_rounds_mutations.py`. No production file was written during mutation.

- Mutants generated: **31**, covering all 14 `PeerRounds` methods.
- Killed before: **17/31 (54.8%)**.
- Killed after: **31/31 (100.0%)**.
- Timed out: **0**.
- Survivors left: **0**. Equivalent: **0**; untestable offline: **0**; defects: **0**.
- Original `rounds.py` SHA-256 before and after: `ab76ff05678ead2f5b21f0f96a5ec570dd19c847bf5290f435204babb42a38e3`.

## Mutants

Mutations changed executable code only: comparisons, Boolean conditions, an arithmetic expiry offset, return values, or one statement. Comments, logging, and docstrings were excluded.

| ID | Function:line | Change | Before | After |
| --- | --- | --- | --- | --- |
| M01 | `__init__:38` | remove startup expiry | survived | killed |
| M02 | `prepare:42` | change required-key subset check to strict subset | survived | killed |
| M03 | `prepare:48` | invert duplicate-target comparison | killed | killed |
| M04 | `prepare:52` | reject text at its allowed length limit | survived | killed |
| M05 | `prepare:52` | check blank context instead of required text | survived | killed |
| M06 | `prepare:70` | allow prompt exactly at local-first threshold | survived | killed |
| M07 | `prepare:80` | invert idempotency hash comparison | killed | killed |
| M08 | `prepare:83` | allow a fifty-first pending round | survived | killed |
| M09 | `_view:92` | return None for cleared selection | killed | killed |
| M10 | `read:100` | drop unknown-round raise | killed | killed |
| M11 | `pending:106` | return None for pending list | killed | killed |
| M12 | `_expire:110` | add rather than subtract expiry interval | killed | killed |
| M13 | `_expire:111` | add rather than subtract retention interval | killed | killed |
| M14 | `stop_room:115` | replace stop count with None | killed | killed |
| M15 | `delete_room:119` | remove room-round deletion | killed | killed |
| M16 | `_active:123` | accept running row after shutdown | survived | killed |
| M17 | `approve:130` | reject approval at exact expiry boundary | survived | killed |
| M18 | `approve:134` | replace approval result with None | survived | killed |
| M19 | `run_once:140` | replace idle False with None | survived | killed |
| M20 | `run_once:157` | replace successful True with False | killed | killed |
| M21 | `_run_claimed:168` | remove reply persistence | killed | killed |
| M22 | `_run_claimed:174` | remove completed status update | killed | killed |
| M23 | `_consult:179` | negate service-stopping guard | killed | killed |
| M24 | `_consult:180` | invert ready-state comparison | killed | killed |
| M25 | `_consult:182` | negate pre-dispatch active guard | killed | killed |
| M26 | `_consult:184` | invert start-result check | killed | killed |
| M27 | `_consult:194` | require both invalid-reply conditions | survived | killed |
| M28 | `_consult:196` | wait past exact deadline | survived | killed |
| M29 | `_consult:204` | remove job cancellation | survived | killed |
| M30 | `loop:208` | invert worker-loop condition | killed | killed |
| M31 | `loop:210` | wait after work instead of when idle | survived | killed |

## New tests

Each test below passed on the original file and failed when run alone against its paired mutant. Tests are in `tests/test_peer_rounds_mutations.py`, grouped by method name.

| Mutant | New test |
| --- | --- |
| M01 | `test_init_expires_old_pending_rows_immediately` |
| M02 | `test_prepare_accepts_exact_required_keys` |
| M04 | `test_prepare_accepts_each_length_limit` |
| M05 | `test_prepare_rejects_blank_required_text` |
| M06 | `test_prepare_local_first_limit_includes_equal_size` |
| M08 | `test_prepare_refuses_fifty_first_pending_round` |
| M16 | `test_active_respects_closed_event_for_running_round` |
| M17 | `test_approve_allows_exact_expiry_boundary` |
| M18 | `test_approve_returns_status_to_review_caller` |
| M19 | `test_run_once_idle_result_is_false` |
| M27 | `test_consult_rejects_oversized_reply` |
| M28 | `test_consult_stops_at_exact_deadline` |
| M29 | `test_consult_cancels_job_after_read` |
| M31 | `test_loop_waits_when_queue_is_idle` |

## Defects found

None.

## Verification

- `python3 -W ignore -m unittest discover -s tests -p 'test_peer_rounds*.py' -q`: 28 tests passed.
- `python3 tests/test_suite.py` with an isolated temporary `HOME`: 592 passed, 0 failed, 5 environmental skips.
- Individual paired-mutant checks: 14/14 new tests failed on their mutant and passed on original.
- `tests/test_onboard.py` with an isolated temporary `HOME`: 28 passed.
- `tests/test_local_worker.py` could not run here: all 14 cases fail in setup because the sandbox denies binding a local HTTP test server (`PermissionError: [Errno 1] Operation not permitted`). No remote service was contacted.
