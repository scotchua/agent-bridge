# Mutation sweep: `src/agent_bridge/registry.py`

## Method

- Read the complete 696-line module and the existing registry-focused tests. Selected one executable-code mutation in each of its 25 functions; no comment, logging, or docstring was mutated.
- The throwaway runner compiled each mutated source in a fresh child process and executed the existing `test_input_security` and `test_security_boundaries` unittest modules. Each child had a 45-second limit. These tests use isolated state and fake peers; no provider or network call was made.
- Repeated the same runs with `test_registry_mutations` included, then ran each new test alone against its matching mutant. All new tests passed against the original module.
- Mutations were held in temporary files and executed in memory. The production file was never edited. SHA-256 before, after every mutant, and at completion: `27797ba75a8851e78ee963b88b1c072b7ed959d8da5fe37afdbc8bb365a21a0c`.

## Kill rates

| Phase | Killed | Generated | Kill rate |
| --- | ---: | ---: | ---: |
| Existing focused tests | 7 | 25 | 28% |
| Existing plus new tests | 25 | 25 | 100% |

## Mutants and new tests

| ID | Function | Change | Before | After | New test |
| --- | --- | --- | --- | --- | --- |
| M01 | `create_conversation` | `store.atomic_write_json(path, record)` → `pass` | killed | killed | `test_create_conversation_persists_record` |
| M02 | `load_conversation` | `return data` → `return None` | killed | killed | `test_load_conversation_returns_record` |
| M03 | `claim_conversation_slot` | `>= max_turns:` → `> max_turns:` | survived | killed | `test_claim_conversation_slot_refuses_exact_turn_limit` |
| M04 | `claim_grace` | `return float(CLAIM_GRACE_SECONDS)` → `return None` | survived | killed | `test_claim_grace_falls_back_on_invalid_limit` |
| M05 | `assert_conversation_ownership` | `raise BrokerError(ErrorCategory.CONVERSATION_NOT_OWNED)` → `pass` | survived | killed | `test_assert_conversation_ownership_rejects_other_job` |
| M06 | `_job_still_running` | `(now - claimed_at) < grace` → `(now - claimed_at) <= grace` | survived | killed | `test_job_still_running_expires_at_grace_boundary` |
| M07 | `_claim_epoch` | `return _dt.datetime.fromisoformat(str(raw)).timestamp()` → `return None` | survived | killed | `test_claim_epoch_parses_synthetic_timestamp` |
| M08 | `retire_attempt_markers` | `store.replace(path, path + ".committed")` → `pass` | survived | killed | `test_retire_attempt_markers_renames_marker` |
| M09 | `resolve_indeterminate` | `record["indeterminate"] = False` → `record["indeterminate"] = True` | survived | killed | `test_resolve_indeterminate_clears_hold` |
| M10 | `release_conversation_slot` | `int(record.get("turns", 0)) + 1` → `int(record.get("turns", 0)) - 1` | survived | killed | `test_release_conversation_slot_increments_turns` |
| M11 | `update_conversation` | `data.update(changes)` → `pass` | survived | killed | `test_update_conversation_applies_changes` |
| M12 | `write_status` | `"seq": int(previous.get("seq", 0)) + 1` → `"seq": int(previous.get("seq", 0)) + 2` | survived | killed | `test_write_status_starts_sequence_at_one` |
| M13 | `attach_worker_pid` | `document["worker_pid"] = pid` → `pass` | survived | killed | `test_attach_worker_pid_records_pid` |
| M14 | `read_status` | `if data is not None:` → `if data is None:` | killed | killed | `test_read_status_rejects_unknown_job` |
| M15 | `read_result` | `return store.read_json_or_none(os.path.join(cfg.job_dir(job_id), "result.json"))` → `return None` | survived | killed | `test_read_result_returns_saved_result` |
| M16 | `pid_alive` | `if not pid:` → `if pid:` | killed | killed | `test_pid_alive_rejects_missing_pid` |
| M17 | `inflight_attempts` | `if not os.path.isfile(marker_path):` → `if os.path.isfile(marker_path):` | survived | killed | `test_inflight_attempts_finds_marker` |
| M18 | `_group_identity_matches` | `if observed_pgid != str(pgid):` → `if observed_pgid == str(pgid):` | survived | killed | `test_group_identity_matches_verified_group` |
| M19 | `reap_orphaned_peers` | `if not safe:` → `if safe:` | survived | killed | `test_reap_orphaned_peers_leaves_unverified_group_alone` |
| M20 | `reconcile` | `if status.get("status") in TERMINAL_STATUSES:` → `if status.get("status") not in TERMINAL_STATUSES:` | killed | killed | `test_reconcile_terminal_job_skips_reaper` |
| M21 | `_active_marker` | `return cfg.state("active", job_id)` → `return None` | killed | killed | `test_active_marker_uses_job_id` |
| M22 | `_index_active` | `if status in TERMINAL_STATUSES:` → `if status not in TERMINAL_STATUSES:` | survived | killed | `test_index_active_adds_running_job` |
| M23 | `count_active` | `active += 1` → `active -= 1` | survived | killed | `test_count_active_counts_live_job` |
| M24 | `admission_gate` | `        yield
` → `        return
` | killed | killed | `test_admission_gate_enters_context` |
| M25 | `conversation_has_active_job` | `return bool(active) and _job_still_running` → `return bool(active) or _job_still_running` | survived | killed | `test_conversation_has_active_job_without_claim_is_false` |

## Survivors left

None. No equivalent mutants, offline-untestable mutants, or defects were found in this sample.

## Defects found

None.

## Verification

- `PYTHONPATH=src:tests python3 -m unittest -q test_registry_mutations`: 25 passed on the original.
- Each of the 25 new tests failed on its matching mutant in an isolated child process.
- `python3 tests/test_suite.py`: passed; 594 checks passed, 0 failed, 5 environmental skips.
- `test_onboard`: passed in an isolated temporary home. `test_local_worker` was exercised but its loopback server setup was denied by the OS sandbox (`PermissionError` on `127.0.0.1` bind).

The measured before rate uses the two existing focused unittest modules. The broader fake-peer suite was verified on the original code, not rerun for each mutant.
