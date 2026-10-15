# Mutation test: `src/agent_bridge/chat/dispatch.py`

## Method and result

The runner made 43 single edits in an in-memory copy of the module, covering `reply_text`, `Dispatcher.__init__`, `run_once`, `stop`, `cancel_room`, and `loop`. Edits included comparison flips, condition negation, `and`/`or` swaps, a `+`/`-` swap, a numeric boundary change, return replacement with `None`, empty text or `False`, statement removal, and a dropped raise. No comment, docstring, or logging text was mutated. Each mutant ran in a fresh Python process with a four-second limit; the dispatcher file was never written. Existing-test runs loaded `test_chat_dispatch`, `test_chat_discussion`, and `test_chat_management`. After-test runs also loaded `test_chat_dispatch_mutations`.

| Measure | Before | After |
| --- | ---: | ---: |
| Mutants generated | 43 | 43 |
| Killed by test assertion or error | 21 | 43 |
| Test kill rate | 21/43 (48.8%) | 43/43 (100%) |
| Time-boxed | 1 (M26) | 0 |
| Survived | 21 | 0 |

M26 read a response while the provider still reported `running`. Before the new test, an existing test stalled on that mutant and hit the runner limit. The new `test_reply_is_read_only_after_terminal_poll` fails on it immediately. No time-box result is counted as a test kill.

## Mutation inventory

| ID | Function | Single change | Before | After |
| --- | --- | --- | --- | --- |
| M01 | `reply_text` | `return content` → `return None` | survived | killed |
| M02 | `reply_text` | `not isinstance(content, dict) or content.get('contract_version') != '2'` → `not isinstance(content, dict) and content.get('contract_version') != '2'` | killed | killed |
| M03 | `reply_text` | `return json.dumps(content, ensure_ascii=False, indent=2)` → `return None` | killed | killed |
| M04 | `reply_text` | `parts.extend(content.get('analysis', []))` → `pass` | killed | killed |
| M05 | `reply_text` | `if content.get('risks'):` → `if not content.get('risks'):` | survived | killed |
| M06 | `reply_text` | `if item.get('mitigation') else ''` → `if not item.get('mitigation') else ''` | survived | killed |
| M07 | `reply_text` | `return '\n\n'.join(parts)` → `return None` | killed | killed |
| M08 | `__init__` | `self.store, self.adapters = store, adapters` → `self.store, self.adapters = adapters, store` | killed | killed |
| M09 | `run_once` | `if not job:` → `if job:` | killed | killed |
| M10 | `run_once` | `if not job: return False` → `if not job: return None` | survived | killed |
| M11 | `run_once` | `raise ValueError('This agent is not connected yet')` → `pass` | survived | killed |
| M12 | `run_once` | `status['state'] != 'ready'` → `status['state'] == 'ready'` | killed | killed |
| M13 | `run_once` | `'client-derived' in labels` → `'client-derived' not in labels` | survived | killed |
| M14 | `run_once` | `'internal' in labels` → `'internal' not in labels` | survived | killed |
| M15 | `run_once` | `'synthetic' in labels` → `'synthetic' not in labels` | survived | killed |
| M16 | `run_once` | `if job.get('round_id'):` → `if not job.get('round_id'):` | killed | killed |
| M17 | `run_once` | `job['role'] == 'summary'` → `job['role'] != 'summary'` | killed | killed |
| M18 | `run_once` | `if not self.store.is_running(job['id']):` → `if self.store.is_running(job['id']):` | killed | killed |
| M19 | `run_once` | `if not self.store.is_running(job['id']): return True` → `if not self.store.is_running(job['id']): return False` | survived | killed |
| M20 | `run_once` | `if session else adapter.start(prompt, classification)` → `if not session else adapter.start(prompt, classification)` | killed | killed |
| M21 | `run_once` | `if not result.get('ok'):` → `if result.get('ok'):` | killed | killed |
| M22 | `run_once` | `self._provider_jobs[job['id']] = (adapter, result['job_id'])` → `pass` | killed | killed |
| M23 | `run_once` | `getattr(adapter, 'timeout', 480)` → `getattr(adapter, 'timeout', 481)` | survived | killed |
| M24 | `run_once` | `while self.store.is_running(job['id']) and not self.closed.is_set():` → `while self.store.is_running(job['id']) or not self.closed.is_set():` | survived | killed |
| M25 | `run_once` | `if not poll.get('ok'):` → `if poll.get('ok'):` | killed | killed |
| M26 | `run_once` | `poll.get('status') in TERMINAL_STATUSES` → `poll.get('status') not in TERMINAL_STATUSES` | timeout | killed |
| M27 | `run_once` | `if not reply.get('ok'):` → `if reply.get('ok'):` | killed | killed |
| M28 | `run_once` | `text = reply_text(content)` → `text = None` | killed | killed |
| M29 | `run_once` | `if time.monotonic() >= deadline:` → `if time.monotonic() <= deadline:` | survived | killed |
| M30 | `run_once` | `str(message.value if message else exc)[:500]` → `str(message.value if message else exc)[:501]` | survived | killed |
| M31 | `run_once` | `self.closed.is_set() or not self.store.is_running(job['id'])` → `self.closed.is_set() and not self.store.is_running(job['id'])` | survived | killed |
| M32 | `run_once` | `adapter.cancel(active_provider_job[1])` → `pass` | survived | killed |
| M33 | `run_once` | `return True def stop` → `return None def stop` | killed | killed |
| M34 | `stop` | `return self.store.stop(room_id) def cancel_room` → `return None def cancel_room` | survived | killed |
| M35 | `cancel_room` | `job['status'] not in ('queued', 'running')` → `job['status'] in ('queued', 'running')` | killed | killed |
| M36 | `cancel_room` | `provider_job is not None and hasattr(provider_job[0], 'cancel')` → `provider_job is not None or hasattr(provider_job[0], 'cancel')` | killed | killed |
| M37 | `cancel_room` | `provider_job[0].cancel(provider_job[1])` → `pass` | killed | killed |
| M38 | `cancel_room` | `return self.store.stop(room_id) def loop` → `return None def loop` | killed | killed |
| M39 | `loop` | `while not self.closed.is_set():` → `while self.closed.is_set():` | survived | killed |
| M40 | `loop` | `if not self.run_once():` → `if self.run_once():` | survived | killed |
| M41 | `loop` | `self.closed.wait(0.3) except Exception:` → `pass except Exception:` | survived | killed |
| M42 | `run_once` | `deadline = time.monotonic() + getattr(adapter, 'timeout', 480)` → `deadline = time.monotonic() - getattr(adapter, 'timeout', 480)` | survived | killed |
| M43 | `reply_text` | `return content` → `return ''` | survived | killed |

## New tests and mutants they kill

| Test name in `tests/test_chat_dispatch_mutations.py` | Mutants verified individually |
| --- | --- |
| `ReplyTextMutationTests.test_plain_text_is_preserved` | M01, M43 |
| `ReplyTextMutationTests.test_structured_risks_are_rendered` | M05, M06 |
| `RunOnceMutationTests.test_empty_queue_returns_false` | M10 |
| `RunOnceMutationTests.test_missing_adapter_reports_connection_error` | M11 |
| `RunOnceMutationTests.test_each_classification_reaches_the_adapter` | M13, M14, M15 |
| `RunOnceMutationTests.test_stopped_job_is_not_started_but_run_was_claimed` | M19 |
| `RunOnceMutationTests.test_default_timeout_uses_exact_boundary` | M23, M29 |
| `RunOnceMutationTests.test_reply_is_read_only_after_terminal_poll` | M26, M42 |
| `RunOnceMutationTests.test_close_before_poll_skips_reply_and_cancels_provider_job` | M24, M31, M32 |
| `RunOnceMutationTests.test_failure_hint_is_capped_at_500_characters` | M30 |
| `StopMutationTests.test_stop_returns_store_result` | M34 |
| `LoopMutationTests.test_idle_loop_waits_once` | M39, M40, M41 |

Each listed test passed on the original module and failed on at least one listed mutant. The after matrix killed all 43 mutants. The test inputs use invented room and invoice details; no provider or API was contacted.

## Survivors left

None. Equivalent: none identified. Untestable offline: none. Defects found: none.

## Verification and source integrity

- `python3 tests/test_suite.py`: 592 passed, 0 failed, 5 environmental skips, using an isolated temporary home.
- `python3 -m unittest discover -s tests -p test_chat_dispatch_mutations.py`: 12 passed.
- `tests/test_onboard.py`: 28 passed with an isolated temporary home.
- `tests/test_local_worker.py`: attempted, but all 14 tests errored in setup because this sandbox refused a `127.0.0.1` server bind (`PermissionError: [Errno 1] Operation not permitted`). No test body ran.
- `src/agent_bridge/chat/dispatch.py` SHA-256 before and after: `dc862d09a6c3b779dba232857d7bcb3cc6488e51354bfb3d89da30bb1791a5a1`. `git diff` shows no production changes.
