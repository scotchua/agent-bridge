# POSIX platform mutation sweep

Source: `src/agent_bridge/platform/posix.py`
SHA-256 before and after: `07616ac8f1823f2088ee8cfea2990afcad4038ce714cd7129eaffb42deaed36a`

## Method

The throwaway runner made one textual change per mutant, confined to the AST span of a single method. It compiled each copy in memory and installed the mutated class in a fresh Python subprocess; it never wrote the production module. Mutations avoided comments, docstrings, and logging. Each subprocess used an isolated temporary home and an eight-second time limit. No network or API calls were made.

The existing-test pass ran `test_store.py`, `test_execution_worker.py`, and the `test_state_root_permissions` and `test_platform_boundary` checks from `test_suite.py` for every mutant. It also ran `test_permissions` for permission and lock methods and `test_output_cap` for stream methods. The full `test_timeout_and_process_group_cleanup` check ran on the original in the full suite, but was excluded from mutant subprocesses because a `start_new_session=False` mutant can signal the runner's own process group.

A failed assertion, exception, or time-box expiration counts as killed. M04 removes the lock timeout raise and exceeded the time box in both passes; it is shown separately as `timeout` below.

## Kill rates

| Pass | Killed by failure | Timed out | Survived | Kill rate including timeout |
| --- | ---: | ---: | ---: | ---: |
| Existing tests | 4 | 1 | 32 | 5/37 (13.5%) |
| Existing and new tests | 36 | 1 | 0 | 37/37 (100.0%) |

## Mutants generated

| ID | Method | Mutation | Before | After |
| --- | --- | --- | --- | --- |
| M01 | `set_owner_only_umask` | constant: 0o077 → 0o076 | survived | killed |
| M02 | `enforce_owner_only_file` | constant: 0o600 → 0o601 | killed | killed |
| M03 | `lock_exclusive` | operator: time.monotonic() >= deadline → time.monotonic() > deadline | survived | killed |
| M04 | `lock_exclusive` | raise: raise TimeoutError(f"could not lock {lock_path}") → pass | timeout | timeout |
| M05 | `lock_exclusive` | statement: time.sleep(0.02) → pass | survived | killed |
| M06 | `verify_owner_only_path` | comparison: observed_dir == 0o700 → observed_dir != 0o700 | killed | killed |
| M07 | `verify_owner_only_path` | boolean: and observed_file == 0o600 → or observed_file == 0o600 | survived | killed |
| M08 | `verify_owner_only_path` | return: "file_mode": oct(observed_file) → "file_mode": None | killed | killed |
| M09 | `spawn_isolated` | return: return subprocess.Popen( → return None if True else subprocess.Popen( | survived | killed |
| M10 | `spawn_isolated` | boolean: start_new_session=True → start_new_session=False | survived | killed |
| M11 | `spawn_isolated` | boolean: close_fds=True → close_fds=False | survived | killed |
| M12 | `isolated_process_group` | return: return os.getpgid(pid) → return None | survived | killed |
| M13 | `terminate_process_tree` | constant: max(0.0, grace) → max(1.0, grace) | survived | killed |
| M14 | `terminate_process_tree` | return: report["sigterm"] = True → report["sigterm"] = False | survived | killed |
| M15 | `terminate_process_tree` | statement: os.killpg(group_id, signal.SIGKILL) → pass | survived | killed |
| M16 | `process_alive` | return: os.kill(int(pid), 0) return True → os.kill(int(pid), 0) return False | survived | killed |
| M17 | `process_alive` | return: except ProcessLookupError: return False → except ProcessLookupError: return True | survived | killed |
| M18 | `process_tree_alive` | return: return True → return False | survived | killed |
| M19 | `process_group_members` | comparison: len(parts) < 2 → len(parts) <= 2 | survived | killed |
| M20 | `process_group_members` | boolean: and int(parts[0]) != os.getpid() → or int(parts[0]) != os.getpid() | survived | killed |
| M21 | `process_group_members` | return: return members → return None | survived | killed |
| M22 | `process_liveness` | return: evidence["alive"] = True → evidence["alive"] = False | survived | killed |
| M23 | `process_liveness` | return: evidence.update(alive=False, reason="no such process") → evidence.update(alive=True, reason="no such process") | survived | killed |
| M24 | `process_identity` | return: return probe.stdout.decode("utf-8", "replace").strip() → return "" | survived | killed |
| M25 | `process_identity` | return: return "" → return None | survived | killed |
| M26 | `process_group_identity` | condition: if not text: → if text: | survived | killed |
| M27 | `process_group_identity` | comparison: len(parts) != 2 → len(parts) == 2 | survived | killed |
| M28 | `process_group_identity` | return: return parts[0].strip(), parts[1].strip() → return ("", "") | survived | killed |
| M29 | `is_own_process_group` | comparison: group_id == os.getpgrp() → group_id != os.getpgrp() | survived | killed |
| M30 | `read_streams_with_caps` | constant: max(0, stdout_cap) → max(1, stdout_cap) | survived | killed |
| M31 | `read_streams_with_caps` | comparison: time.monotonic() >= deadline → time.monotonic() > deadline | survived | killed |
| M32 | `read_streams_with_caps` | condition: if pending: → if not pending: | survived | killed |
| M33 | `read_streams_with_caps` | comparison: room > 0 → room < 0 | killed | killed |
| M34 | `read_streams_with_caps` | comparison: len(buffer) >= caps[name] → len(buffer) > caps[name] | survived | killed |
| M35 | `read_streams_with_caps` | operator: caps[name] - len(buffer) → caps[name] + len(buffer) | survived | killed |
| M36 | `read_streams_with_caps` | return: timed_out=timed_out → timed_out=False | survived | killed |
| M37 | `read_streams_with_caps` | return: descendant_held_pipes=descendant_held_pipes → descendant_held_pipes=False | survived | killed |

## New tests

Each of the following tests passed on the original and failed when run alone against the listed recorded mutant:

| Test in `tests/test_posix_mutation.py` | Mutant killed |
| --- | --- |
| `test_set_owner_only_umask` | M01 |
| `test_enforce_owner_only_file` | M02 |
| `test_lock_exclusive_retries_and_unlocks` | M05 |
| `test_lock_exclusive_times_out_at_deadline` | M03 |
| `test_verify_owner_only_path_requires_both_modes` | M07 |
| `test_spawn_isolated_passes_isolation_options` | M09 |
| `test_isolated_process_group_uses_pid_and_handles_missing_process` | M12 |
| `test_terminate_process_tree_signals_and_reports` | M15 |
| `test_process_alive_distinguishes_probe_results` | M16 |
| `test_process_tree_alive_distinguishes_probe_results` | M18 |
| `test_process_group_members_filters_group_and_self` | M19 |
| `test_process_liveness_reports_probe_evidence` | M22 |
| `test_process_identity_decodes_ps_or_returns_empty` | M24 |
| `test_process_group_identity_parses_ps_or_returns_empty` | M26 |
| `test_is_own_process_group_compares_current_group` | M29 |
| `test_read_streams_with_caps_writes_stdin_and_reads_both_streams` | M32 |
| `test_read_streams_with_caps_enforces_exact_and_zero_caps` | M30 |
| `test_read_streams_with_caps_reports_timeout` | M36 |
| `test_read_streams_with_caps_stops_at_exact_deadline` | M31 |
| `test_read_streams_with_caps_reports_held_pipe` | M37 |

## Survivors left

- Equivalent: none.
- Untestable offline: none.
- Defect in the original: none.
- Behavioral survivors: none.

## Defects found

None.

## Verification

- `python3 tests/test_suite.py` with an isolated temporary home: 592 passed, 0 failed, 5 environmental skips.
- `python3 -m unittest discover -s tests -p test_posix_mutation.py -q`: 20 passed.
- `test_onboard.py` with an isolated temporary home: 28 passed.
- `test_local_worker.py` was attempted with an isolated temporary home; all 14 cases could not start because this sandbox refuses a loopback socket bind (`PermissionError: [Errno 1] Operation not permitted`).
- The original production source was byte-identical after every mutant and at completion; `git diff -- src/agent_bridge/platform/posix.py` was empty.
