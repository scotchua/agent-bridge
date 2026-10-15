# Mutation probe: `src/agent_bridge/orchestration/config.py`

## Method

Generated 36 one-change mutants across all seven functions. Exact source replacements were confined to function AST line spans, compiled, and executed in a fresh subprocess against an in-memory module namespace. The production file was never written. Each mutant run had a 15-second timeout; none timed out. Mutations avoided comments, logging, and docstrings. The operators included comparison flips, `and`/`or` swaps, condition negation, a numeric constant increment, return replacement, a removed statement, `-` to `+`, and a dropped raise.

The before set was 12 existing offline tests from `test_orchestration_server.py`, `test_gemma_certified.py`, `test_localq_client_data_health.py`, and `test_execution_worker.py`. The after set added the 11 tests in `test_orchestration_config_mutation.py`.

## Kill rates

| Phase | Killed | Generated | Kill rate |
| --- | ---: | ---: | ---: |
| Before new tests | 19 | 36 | 52.8% |
| After new tests | 36 | 36 | 100.0% |

## Mutants generated

| ID | Function | Change | Before | After |
| --- | --- | --- | --- | --- |
| W1 | `_windows_paths` | `if sidecar is not None:` → `if sidecar is None:` | killed | killed |
| W2 | `_windows_paths` | `if not all(value is not None for value in supplied):` → `if all(value is not None for value in supplied):` | killed | killed |
| W3 | `_windows_paths` | `resolved[name] = windows_wsl._validate_windows_host_path(name, value)` → `resolved[name] = value` | survived | killed |
| A1 | `_absolute_path` | `if not isinstance(value, str) or not value:` → `if not isinstance(value, str) and not value:` | survived | killed |
| A2 | `_absolute_path` | `if not path.is_absolute():` → `if path.is_absolute():` | killed | killed |
| A3 | `_absolute_path` | `return path` → `return None` | killed | killed |
| F1 | `_existing_file` | `if path.is_symlink() or not path.is_file():` → `if path.is_symlink() and not path.is_file():` | survived | killed |
| F2 | `_existing_file` | `return path` → `return None` | survived | killed |
| D1 | `_existing_dir` | `if path.is_symlink() or not path.is_dir():` → `if path.is_symlink() and not path.is_dir():` | survived | killed |
| D2 | `_existing_dir` | `return path` → `return None` | survived | killed |
| P1 | `parse_worktree_prune_roots` | `if not isinstance(values, list):` → `if isinstance(values, list):` | killed | killed |
| P2 | `parse_worktree_prune_roots` | `if not path.is_dir():` → `if path.is_dir():` | survived | killed |
| P3 | `parse_worktree_prune_roots` | `roots.append(path.resolve())` → `roots.append(path)` | survived | killed |
| P4 | `parse_worktree_prune_roots` | `return tuple(roots)` → `return ()` | survived | killed |
| P5 | `parse_worktree_prune_roots` | `roots.append(path.resolve())` → `pass` | survived | killed |
| G1 | `_gemma_paths` | `if local_backend != "gemma_certified":` → `if local_backend == "gemma_certified":` | killed | killed |
| G2 | `_gemma_paths` | `if any(value is None for value in supplied):` → `if all(value is None for value in supplied):` | killed | killed |
| G3 | `_gemma_paths` | `len(digest) != 64` → `len(digest) == 64` | killed | killed |
| G4 | `_gemma_paths` | `timeout < GEMMA_CERTIFIED_MIN_TIMEOUT_SECONDS` → `timeout <= GEMMA_CERTIFIED_MIN_TIMEOUT_SECONDS` | killed | killed |
| G5 | `_gemma_paths` | `len(set(pilot_tasks)) != len(pilot_tasks)` → `len(set(pilot_tasks)) == len(pilot_tasks)` | killed | killed |
| G6 | `_gemma_paths` | `or "gemma_pilot_tasks" in raw` → `and "gemma_pilot_tasks" in raw` | killed | killed |
| H1 | `_client_data_health` | `if not any(present):` → `if any(present):` | killed | killed |
| H2 | `_client_data_health` | `if not all(present):` → `if all(present):` | killed | killed |
| H3 | `_client_data_health` | `tuple(command[-2:]) != ("status", "--json")` → `tuple(command[-2:]) == ("status", "--json")` | killed | killed |
| H4 | `_client_data_health` | `timeout <= 0` → `timeout < 0` | survived | killed |
| H5 | `_client_data_health` | `"client_data_health_timeout_seconds": float(timeout)` → `"client_data_health_timeout_seconds": 30.0` | killed | killed |
| L1 | `load` | `set(raw) - _KEYS` → `set(raw) + _KEYS` | killed | killed |
| L2 | `load` | `if raw.get("config_version") != "1":` → `if raw.get("config_version") == "1":` | killed | killed |
| L3 | `load` | `interval <= 0` → `interval < 0` | survived | killed |
| L4 | `load` | `and not all(value is not None for value in execution_values)` → `or not all(value is not None for value in execution_values)` | killed | killed |
| L5 | `load` | `capacity_db == local_root or capacity_db == state_root` → `capacity_db == local_root and capacity_db == state_root` | survived | killed |
| L6 | `load` | `raise OrchestrationConfigError("capacity_db_must_be_file")` → `pass` | survived | killed |
| L7 | `load` | `interval_seconds=float(interval)` → `interval_seconds=5.0` | survived | killed |
| L8 | `load` | `client_derived_routes=frozenset(client_routes)` → `client_derived_routes=frozenset()` | survived | killed |
| L9 | `load` | `prune_roots = parse_worktree_prune_roots(raw)` → `prune_roots = ()` | survived | killed |
| L10 | `load` | `interval = raw.get("interval_seconds", 5.0)` → `interval = raw.get("interval_seconds", 6.0)` | killed | killed |

## New tests

Each named test passed against the original module and failed in isolation against the first mutant shown. Additional mutants in the same row were killed by the full after set.

| Test in `tests/test_orchestration_config_mutation.py` | Mutant IDs |
| --- | --- |
| `test_windows_paths_reject_invalid_host_path` | W3 |
| `test_absolute_path_rejects_non_string_value` | A1 |
| `test_existing_file_returns_path_and_rejects_symlink` | F1, F2 |
| `test_existing_dir_returns_path_and_rejects_symlink` | D1, D2 |
| `test_parse_worktree_prune_roots_requires_existing_directory` | P2 |
| `test_parse_worktree_prune_roots_resolves_and_keeps_roots` | P3, P4, P5 |
| `test_client_data_health_rejects_zero_timeout` | H4 |
| `test_load_rejects_zero_interval` | L3 |
| `test_load_rejects_capacity_database_as_state_root` | L5, L6 |
| `test_load_preserves_interval_and_client_routes` | L7, L8 |
| `test_load_preserves_worktree_prune_roots` | L9 |

## Survivors left

None. No equivalent or offline-untestable mutant remained in this set.

## Defects found

None.

## Verification

- `python3 -m unittest discover -s tests -p test_orchestration_config_mutation.py`: 11 passed.
- The 11 new tests were also run individually against one recorded mutant each; all 11 killed their assigned mutant.
- `python3 tests/test_suite.py`, with an isolated temporary `HOME`: 592 passed, 0 failed, 5 platform skips.
- The four existing config-related modules passed: `test_orchestration_server.py` (9), `test_gemma_certified.py` (31), `test_localq_client_data_health.py` (15), and `test_execution_worker.py` (23).
- `test_onboard.py` (28), `test_hostenv.py` (21 passed, 11 skipped), `test_automatic_gate.py` (169), and `test_delegation_audit.py` (40) passed with isolated temporary homes.
- `test_local_worker.py` (14 setup errors) and `test_automatic_delegation_e2e.py` (42 setup errors) could not bind their local fake HTTP listeners: sandbox `PermissionError: [Errno 1] Operation not permitted`. These failures occurred before config behavior was exercised.
- The module SHA-256 before and after mutation was `4a63c6fa60d6a81cef89051fcd2722ca8f6b5f8601f1cff99a76f7d035fb702a`; no production file was changed.
