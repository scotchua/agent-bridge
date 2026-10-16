# Mutation sweep: src/agent_bridge/orchestration/delegation.py

- Source SHA-256 before and after: `666f0b4ef1e77e25b013674bd655a3c5a975346935b61a0325e88eff3ec72529` (byte-identical).
- 41 single-change mutants, covering all 18 functions; 20-second timeout per focused test run.
- Mutants ran from temporary files, compiled into a fresh subprocess module namespace. The production file was never edited.
- Existing tests: `ConfigGenerationTests`, `PlatformBoundaryTests`, `LaunchAgentTests`, and `EvidenceValidationTests` in `tests/test_automatic_delegation.py`, plus `EvidencePathTests` in `tests/test_success_gate_unity.py`.
- Original module passes the focused existing and expanded tests.

## Kill rates

| Phase | Killed | Survived | Kill rate |
| --- | ---: | ---: | ---: |
| Before new tests | 18 | 23 | 43.9% |
| After new tests | 41 | 0 | 100.0% |

## Mutants generated

Each row changes one executable expression or statement. No comments, logging, or docstrings were mutated.

| ID | Function | Change | Before | After |
| --- | --- | --- | --- | --- |
| M01 | `_portable_python` | swap and/or: `os.name == "nt" and` → `os.name == "nt" or` | survived | killed |
| M02 | `private_root` | return None: `return os.path.join(os.path.abspath(home), ".agent-bridge", "orchestration")` → `return None` | killed | killed |
| M03 | `paths_for` | return None: `"state_root": state,` → `"state_root": None,` | killed | killed |
| M04 | `build_config` | numeric +1: `interval_seconds: float = 5.0` → `interval_seconds: float = 6.0` | survived | killed |
| M05 | `build_config` | negate condition: `if local_worker_executable else` → `if not local_worker_executable else` | killed | killed |
| M06 | `render_config_bytes` | numeric +1: `indent=2` → `indent=3` | survived | killed |
| M07 | `config_sha256` | return None: `return store.sha256_bytes(render_config_bytes(cfg))` → `return None` | survived | killed |
| M08 | `harness_availability` | negate condition: `and not os.path.islink(path)` → `and os.path.islink(path)` | killed | killed |
| M09 | `harness_availability` | swap and/or: `python_ok and codex_ok and claude_ok,` → `python_ok or codex_ok and claude_ok,` | survived | killed |
| M10 | `harness_availability` | swap and/or: `python_ok and claude_ok and claude_config_dir_ready,` → `python_ok or claude_ok and claude_config_dir_ready,` | survived | killed |
| M11 | `platform_boundary_report` | comparison flip: `if name == "darwin":` → `if name != "darwin":` | killed | killed |
| M12 | `platform_boundary_report` | negate condition: `if name.startswith("win"):` → `if not name.startswith("win"):` | survived | killed |
| M13 | `_direction_status` | swap and/or: `or set(row) != _DIRECTION_ROW_KEYS:` → `and set(row) != _DIRECTION_ROW_KEYS:` | killed | killed |
| M14 | `_direction_status` | negate condition: `if row.get("attempted") is not True:` → `if row.get("attempted") is True:` | killed | killed |
| M15 | `_direction_status` | swap + and -: `return "blocked:" + reason` → `return "blocked:" - reason` | killed | killed |
| M16 | `_direction_status` | negate condition: `row.get("worktree_removed") is not True` → `row.get("worktree_removed") is True` | killed | killed |
| M17 | `validate_evidence` | comparison flip: `set(results) != REQUIRED_TOP_KEYS:` → `set(results) == REQUIRED_TOP_KEYS:` | killed | killed |
| M18 | `validate_evidence` | swap and/or: `if not required_directions or any(` → `if not required_directions and any(` | survived | killed |
| M19 | `validate_evidence` | swap all/any: `if all(value == "enabled" for value in graded):` → `if any(value == "enabled" for value in graded):` | killed | killed |
| M20 | `validate_evidence` | comparison flip: `local_model.get("status") != "complete"` → `local_model.get("status") == "complete"` | survived | killed |
| M21 | `required_directions_for` | return None: `return tuple(f"{caller}->{PROVIDER_FOR_CALLER[caller]}" for caller in callers)` → `return None` | survived | killed |
| M22 | `launch_agent_path` | return None: `return os.path.join(os.path.abspath(home), "Library", "LaunchAgents", LAUNCH_AGENT_LABEL + ".plist")` → `return None` | survived | killed |
| M23 | `_xml_escape` | return None: `return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")` → `return None` | killed | killed |
| M24 | `render_launch_agent` | swap and/or: `if not isinstance(value, str) or not os.path.isabs(value):` → `if not isinstance(value, str) and not os.path.isabs(value):` | killed | killed |
| M25 | `render_launch_agent` | swap and/or: `if not account or "/" in account or "\\\\" in account:` → `if not account and "/" in account or "\\\\" in account:` | survived | killed |
| M26 | `render_launch_agent` | comparison flip: `codex_bin_dir != claude_bin_dir` → `codex_bin_dir == claude_bin_dir` | killed | killed |
| M27 | `render_launch_agent` | return None: `return xml.encode("utf-8")` → `return None` | killed | killed |
| M28 | `_launchctl` | numeric +1: `timeout: float = 20.0` → `timeout: float = 21.0` | survived | killed |
| M29 | `launch_agent_status` | comparison flip: `if sys.platform != "darwin":` → `if sys.platform == "darwin":` | survived | killed |
| M30 | `launch_agent_status` | comparison flip: `probe.returncode == 0` → `probe.returncode != 0` | survived | killed |
| M31 | `activate_launch_agent` | comparison flip: `if os.getuid() == 0:` → `if os.getuid() != 0:` | killed | killed |
| M32 | `activate_launch_agent` | negate condition: `if not apply:` → `if apply:` | killed | killed |
| M33 | `activate_launch_agent` | comparison flip: `if result.returncode != 0:` → `if result.returncode == 0:` | survived | killed |
| M34 | `deactivate_launch_agent` | comparison flip: `if probe.returncode != 0:` → `if probe.returncode == 0:` | survived | killed |
| M35 | `deactivate_launch_agent` | negate condition: `if not apply:` → `if apply:` | survived | killed |
| M36 | `deactivate_launch_agent` | comparison flip: `if result.returncode != 0:` → `if result.returncode == 0:` | survived | killed |
| M37 | `render_launch_agent` | remove statement: `codex_bin_dir = codex_bin_dir or claude_bin_dir` → `pass` | killed | killed |
| M38 | `_portable_python` | drop exception raise: `raise DelegationConfigError(\n            "the Microsoft Store WindowsApps Python alias cannot be used as a durable launcher")` → `pass` | survived | killed |
| M39 | `launch_agent_status` | return empty: `return {"status": "unsupported_platform"}` → `return {}` | survived | killed |
| M40 | `required_directions_for` | return empty: `return tuple(f"{caller}->{PROVIDER_FOR_CALLER[caller]}" for caller in callers)` → `return ()` | survived | killed |
| M41 | `launch_agent_path` | return False: `return os.path.join(os.path.abspath(home), "Library", "LaunchAgents", LAUNCH_AGENT_LABEL + ".plist")` → `return False` | survived | killed |

## New tests

All tests are in `tests/test_delegation_mutations.py`, grouped by function. Each passed on the original and failed individually on at least one recorded mutant.

- `_portable_python`: `test_windowsapps_name_on_posix_is_not_rejected` (M01), `test_windowsapps_alias_on_windows_is_refused` (M38).
- `build_config`: `test_default_poll_interval_is_five_seconds` (M04).
- `render_config_bytes`: `test_config_render_uses_two_space_indent` (M06).
- `config_sha256`: `test_hash_matches_rendered_bytes` (M07).
- `harness_availability`: `test_missing_python_disables_both_execution_verdicts` (M09, M10).
- `platform_boundary_report`: `test_windows_prefix_is_reported_as_windows` (M12).
- `validate_evidence`: `test_empty_required_directions_is_refused` (M18), `test_complete_synthetic_local_model_is_accepted_when_required` (M20).
- `required_directions_for`: `test_caller_order_maps_to_opposite_provider` (M21, M40).
- `launch_agent_path`: `test_path_is_under_user_launchagents` (M22, M41).
- `render_launch_agent`: `test_empty_account_is_refused` (M25).
- `_launchctl`: `test_default_timeout_is_twenty_seconds` (M28).
- `launch_agent_status`: `test_non_darwin_status_is_unsupported` (M29, M39), `test_probe_returncode_controls_active_status` (M30).
- `activate_launch_agent`: `test_successful_bootstrap_reports_activated` (M33).
- `deactivate_launch_agent`: `test_inactive_agent_returns_without_bootout` (M34), `test_active_agent_is_staged_without_apply` (M35), `test_failed_bootout_is_reported` (M36).

## Survivors left

None. No equivalent, untestable-offline, or defect survivors remained.

## Defects found

None.

## Verification

- `python3 -m unittest discover -s tests -p test_delegation_mutations.py`: 19 passed.
- Focused existing and expanded mutation harness: original passed; 41/41 mutants killed after additions.
- `python3 -m unittest discover -s tests -p test_automatic_delegation.py`: 84 passed.
- `test_hostenv.py`: 32 run (11 skipped); `test_automatic_gate.py`: 169 passed; `test_delegation_audit.py`: 40 passed; `test_onboard.py`: 28 passed.
- `test_automatic_delegation_e2e.py` and `test_local_worker.py` could not run in this sandbox: their local HTTP test servers fail to bind `127.0.0.1` with `PermissionError` before test bodies execute.
- `python3 tests/test_suite.py`: 593 passed, 0 failed, 5 environmental skips.
