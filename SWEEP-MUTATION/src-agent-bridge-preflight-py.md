# Mutation sweep: `src/agent_bridge/preflight.py`

The temporary runner made 28 single edits to an in-memory copy of the module, one per fresh process. It changed executable code only, covered all ten functions, used no packages or network access, and capped each mutant run at 12 seconds. The original file was never written. Its Git blob hash before and after was `3340b601f859ce5b7fd4f2af2550083baf2a23a3`, equal to `HEAD`.

Each mutant ran the existing `HealthTests` version and home checks, all `HealthPinningTests`, and the direct `test_suite` peer-home and state-root checks. The after pass added `tests/test_preflight_mutation.py`. A mutant was killed when a check failed or raised unexpectedly. All runs completed within the time box.

| Result | Killed | Total | Kill rate |
| --- | ---: | ---: | ---: |
| Before new tests | 8 | 28 | 28.6% |
| After new tests | 28 | 28 | 100% |

## Mutants generated

| ID | Function | Single edit | Before | After |
| --- | --- | --- | --- | --- |
| M01 | `_now` | milliseconds → seconds | survived | killed |
| M02 | `_record_contamination` | negate record destination condition | survived | killed |
| M03 | `observed_version` | invert deadline branch | survived | killed |
| M04 | `observed_version` | invert timeout condition | survived | killed |
| M05 | `observed_version` | failure flags OR → AND | survived | killed |
| M06 | `observed_version` | invert stderr fallback condition | killed | killed |
| M07 | `discover_executable` | invert name condition | survived | killed |
| M08 | `check_peer` | missing executable OR → AND | survived | killed |
| M09 | `check_peer` | allowlist AND → OR | killed | killed |
| M10 | `check_peer` | pinned report → false | survived | killed |
| M11 | `assert_state_root_secure` | remove probe write | killed | killed |
| M12 | `assert_state_root_secure` | invert insecure-state condition | killed | killed |
| M13 | `assert_workspace_clean` | invert visited-directory condition | survived | killed |
| M14 | `assert_workspace_clean` | contaminant intersection → union | survived | killed |
| M15 | `assert_workspace_clean` | invert offender condition | survived | killed |
| M16 | `assert_workspace_clean` | stopping condition OR → AND | survived | killed |
| M17 | `assert_workspace_empty` | invert nonempty condition | survived | killed |
| M18 | `assert_peer_home_has_no_config` | invert file-present condition | killed | killed |
| M19 | `assert_peer_home_has_no_config` | unknown-table difference → intersection | killed | killed |
| M20 | `peer_home_inventory` | invert directory-present condition | survived | killed |
| M21 | `peer_home_inventory` | count hidden entries instead of visible | survived | killed |
| M22 | `peer_home_inventory` | auth presence → false | survived | killed |
| M23 | `observed_version` | deadline result → `None` | survived | killed |
| M24 | `assert_peer_home_has_no_config` | drop parse-error raise | killed | killed |
| M25 | `observed_version` | default timeout 20 → 21 | survived | killed |
| M26 | `assert_workspace_clean` | boundary equality → inequality | survived | killed |
| M27 | `discover_executable` | return empty string | survived | killed |
| M28 | `assert_peer_home_has_no_config` | set difference → addition | killed | killed |

No `<`, `<=`, `>`, `>=`, or arithmetic `+` expression occurs in executable code here, so those operator flips had no valid site. M28 exercises the available subtraction site; M26 exercises comparison flipping. Mutants in comments, logging, and docstrings were excluded.

## New tests

Each named test passed on the original and failed when run alone against its mapped recorded mutant:

| Test name in `tests/test_preflight_mutation.py` | Kills at least |
| --- | --- |
| `ClockAndContaminationTests.test_now_records_millisecond_precision` | M01 |
| `ClockAndContaminationTests.test_record_contamination_writes_only_when_requested` | M02 |
| `ObservedVersionTests.test_deadline_uses_bounded_runner_and_first_output_line` | M03 |
| `ObservedVersionTests.test_deadline_refuses_timeout_and_each_runner_failure` | M04 |
| `DiscoverExecutableTests.test_known_peer_uses_path_lookup` | M07 |
| `CheckPeerTests.test_missing_executable_is_refused_before_version_probe` | M08 |
| `CheckPeerTests.test_allowed_version_report_is_pinned` | M10 |
| `WorkspaceCleanTests.test_clean_workspace_is_accepted` | M14 |
| `WorkspaceCleanTests.test_instruction_in_workspace_is_refused` | M13 |
| `WorkspaceCleanTests.test_stop_at_limits_ancestor_walk` | M16 |
| `WorkspaceCleanTests.test_boundary_directory_is_checked` | M26 |
| `WorkspaceEmptyTests.test_empty_workspace_is_accepted_and_nonempty_refused` | M17 |
| `PeerHomeInventoryTests.test_inventory_counts_visible_entries_and_reports_auth_presence` | M20 |

## Survivors left

None. Equivalent: none identified. Untestable offline: none. Defect: none.

## Defects found

None.

## Verification

- `python3 tests/test_preflight_mutation.py`: 13 passed.
- `python3 tests/test_suite.py`: 593 passed, 0 failed, 5 environmental skips.
- `python3 -m unittest discover -s tests -p 'test_onboard.py'`: 28 passed.
- `test_local_worker.py`: 14 setup errors because this sandbox forbids binding its loopback HTTP test server (`PermissionError: [Errno 1] Operation not permitted`).

The mutation runner was deleted after these runs. No production file changed.
