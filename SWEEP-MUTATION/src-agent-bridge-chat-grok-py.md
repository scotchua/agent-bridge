# Mutation test: `src/agent_bridge/chat/grok.py`

## Method

A temporary standard-library runner generated 42 single-change mutants, spanning every function in the module. Each mutant was compiled into a fresh process in memory, then exercised by `tests/test_chat_grok.py` with a 12-second process timeout and an isolated temporary home. The production source was never rewritten. Mutations targeted executable code only; comments, logging, and docstrings were skipped.

The baseline ran the 25 pre-existing Grok tests. The after run included those tests and 12 new tests. A mutant was killed when the tests failed. No run timed out or failed in the mutation harness.

## Kill rates

| Phase | Killed | Generated | Kill rate |
| --- | ---: | ---: | ---: |
| Before | 25 | 42 | 59.5% |
| After | 42 | 42 | 100.0% |

## Mutants

| ID | Function:line | Change | Before | After | New killing test |
| --- | --- | --- | --- | --- | --- |
| M01 | `job_path:26` | `!= job_id` → `== job_id` | killed | killed | — |
| M02 | `job_path:27` | `raise ValueError('Invalid job ID')` → `pass` | survived | killed | `test_job_path_rejects_noncanonical_uuid` |
| M03 | `_quarantine:36` | `path.replace(target)` → `pass` | killed | killed | — |
| M04 | `_load_record:47` | `path.stem != job_id` → `path.stem == job_id` | killed | killed | — |
| M05 | `_load_record:49` | `type(expires) not in` → `type(expires) in` | killed | killed | — |
| M06 | `_load_record:51` | `<= 100000` → `< 100000` | survived | killed | `test_load_record_accepts_maximum_reply_length` |
| M07 | `_load_record:53` | `return record` → `return None` | killed | killed | — |
| M08 | `_load_record:55` | `_quarantine(path)` → `pass` | killed | killed | — |
| M09 | `_cleanup:60` | `time.time() - JOB_RETENTION_SECONDS` → `time.time() + JOB_RETENTION_SECONDS` | killed | killed | — |
| M10 | `_cleanup:67` | `record is None or path.stat()` → `record is None and path.stat()` | survived | killed | `test_cleanup_removes_old_valid_record` |
| M11 | `_cleanup:68` | `path.unlink(missing_ok=True)` → `pass` | survived | killed | `test_cleanup_removes_old_valid_record` |
| M12 | `_cleanup:75` | `st_mtime < cutoff` → `st_mtime > cutoff` | killed | killed | — |
| M13 | `__init__:94` | `record and record.get` → `record or record.get` | survived | killed | `test_restart_preserves_completed_reply` |
| M14 | `__init__:96` | `store.atomic_write_json(str(path), record)` → `pass` | killed | killed | — |
| M15 | `status:101` | `< 90:` → `<= 90:` | survived | killed | `test_status_rejects_heartbeat_at_stale_boundary` |
| M16 | `status:107` | `!= ALLOWED_COMMANDS` → `== ALLOWED_COMMANDS` | killed | killed | — |
| M17 | `status:110` | `verify_private_directory(self.root)` → `pass` | survived | killed | `test_status_requires_private_directory_verification` |
| M18 | `status:114` | `return {'state': 'ready'` → `return None # {'state': 'ready'` | killed | killed | — |
| M19 | `start:117` | `\n' + prompt` → `\n' - prompt` | killed | killed | — |
| M20 | `start:117` | `return self.continue_` → `return None # self.continue_` | killed | killed | — |
| M21 | `continue_:120` | `!= 'ready'` → `== 'ready'` | killed | killed | — |
| M22 | `continue_:123` | `str) or not 0` → `str) and not 0` | survived | killed | `test_continue_rejects_empty_prompt` |
| M23 | `continue_:124` | `raise ValueError('Grok room context exceeds its limit')` → `pass` | survived | killed | `test_continue_rejects_empty_prompt` |
| M24 | `continue_:127` | `time.time() + 120` → `time.time() - 120` | killed | killed | — |
| M25 | `continue_:128` | `store.atomic_write_json(str(job_path(self.root, job_id)), record)` → `pass` | killed | killed | — |
| M26 | `poll:138` | `>= record['expires']` → `> record['expires']` | survived | killed | `test_poll_times_out_at_exact_expiry` |
| M27 | `poll:139` | `status = 'timed_out'` → `status = 'queued'` | survived | killed | `test_poll_times_out_at_exact_expiry` |
| M28 | `poll:142` | `return {'ok': True, 'status': status}` → `return None` | killed | killed | — |
| M29 | `read:150` | `!= 'complete'` → `== 'complete'` | killed | killed | — |
| M30 | `read:153` | `path.unlink(missing_ok=True)` → `pass` | killed | killed | — |
| M31 | `read:154` | `return result` → `return None` | killed | killed | — |
| M32 | `cancel:162` | `in ('queued', 'running')` → `not in ('queued', 'running')` | survived | killed | `test_cancel_removes_queued_record` |
| M33 | `cancel:165` | `path.unlink(missing_ok=True)` → `pass` | survived | killed | `test_cancel_removes_queued_record` |
| M34 | `receive:172` | `min(wait, 45)` → `min(wait, 44)` | survived | killed | `test_receive_caps_wait_at_45_seconds` |
| M35 | `receive:180` | `record.get('status') != 'queued' or` → `record.get('status') != 'queued' and` | killed | killed | — |
| M36 | `receive:182` | `record['status'] = 'running'` → `record['status'] = 'queued'` | killed | killed | — |
| M37 | `receive:184` | `return record` → `return None` | killed | killed | — |
| M38 | `receive:186` | `return None` → `return False` | killed | killed | — |
| M39 | `respond:191` | `str) or not 0` → `str) and not 0` | survived | killed | `test_respond_rejects_blank_reply` |
| M40 | `respond:192` | `raise ValueError('Reply must contain 1–100000 characters')` → `pass` | survived | killed | `test_respond_rejects_blank_reply` |
| M41 | `respond:202` | `!= 'running' or` → `!= 'running' and` | survived | killed | `test_respond_rejects_expired_running_record` |
| M42 | `respond:204` | `record.update(status='complete', text=text)` → `pass` | killed | killed | — |

## Survivors left

None. Equivalent mutants: none. Untestable offline: none. Defect mutants: none.

## Defects found

None.

## New tests

All new tests are in `tests/test_chat_grok.py` and passed on the original. Each fails on the corresponding mutant listed above.

- `test_job_path_rejects_noncanonical_uuid`: M02
- `test_load_record_accepts_maximum_reply_length`: M06
- `test_cleanup_removes_old_valid_record`: M10, M11
- `test_restart_preserves_completed_reply`: M13
- `test_status_rejects_heartbeat_at_stale_boundary`: M15
- `test_status_requires_private_directory_verification`: M17
- `test_continue_rejects_empty_prompt`: M22, M23
- `test_poll_times_out_at_exact_expiry`: M26, M27
- `test_cancel_removes_queued_record`: M32, M33
- `test_receive_caps_wait_at_45_seconds`: M34
- `test_respond_rejects_blank_reply`: M39, M40
- `test_respond_rejects_expired_running_record`: M41

## Verification

- `python3 -m unittest discover -s tests -p test_chat_grok.py -q`: 37 tests passed.
- `python3 tests/test_suite.py`: 592 passed, 0 failed, 5 environmental skips.
- `test_onboard.py`: 28 tests passed with an isolated temporary home.
- `test_local_worker.py`: 14 tests could not start because this sandbox denied local socket binding (`PermissionError: [Errno 1] Operation not permitted`).
- `src/agent_bridge/chat/grok.py` SHA-256 after both runs: `ddb667648381405cac2ab89adba51d5907866709d8b9652ffbef226a65ab506d`; `git diff` shows no production changes.
