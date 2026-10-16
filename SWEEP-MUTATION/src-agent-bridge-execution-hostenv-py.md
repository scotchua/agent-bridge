# Mutation sweep: `execution/hostenv.py`

The runner generated 40 single-site mutants from executable code, including
the Python helper embedded in `_HELPER`. It loaded each mutant in memory in a
fresh Python process, ran `tests/test_hostenv.py`, and capped each process at
20 seconds. The before run selected the 32 tests present before this change;
the after run selected all 45 tests. Eleven Linux boundary integration tests
were skipped on this host because its namespace probe is unavailable. The
source file was never written by the runner.

| Measure | Killed | Kill rate |
| --- | ---: | ---: |
| Existing tests | 14 / 40 | 35% |
| Existing and new tests | 40 / 40 | 100% |

## Generated mutants

Each ID below is one independent mutation. `Before` and `After` list the IDs
killed by the corresponding suite; all others survived that run.

| Function | Mutants |
| --- | --- |
| `HostCapabilityError.__init__` | E01 swap message detail and code; E02 erase `code` |
| `resolve_git` | G01 `or` to `and` in platform choice; G02 omit candidate from refusal; G03 `and` to `or` in candidate check; G04 candidate return to `None`; G05 `or` to `and` in PATH choice; G06 `and` to `or` in fallback check; G07 fallback return to `None`; G08 drop refusal raise; G09 `+` to `-` in refusal text |
| `_HELPER.mount` | M01 `!=` to `==`; M02 drop failed mount raise |
| `_HELPER.mount_points` | M03 `<` to `>=` in record length check |
| `_HELPER.writable` | M04 failed probe returns `True`; M05 successful probe returns `False` |
| `helper_source` | H01 replace exit token with empty text; H02 return `None` |
| `confined_argv` | A01 `or` to `and` for interpreter; A02 replace `--net` with `--mount`; A03 omit canaries |
| `_selftest_argv` | S01 omit directory creation; S02 empty home canary; S03 negate extra location check; S04 empty probe command |
| `_userns_usable` | U01 `and` to `or` in binary check; U02 missing binary returns `True`; U03 timeout 30 to 31; U04 caught probe error returns `True`; U05 `==` to `!=` for exit status |
| `Confinement.permits` | P01 invert membership |
| `confinement` | C01 `and` to `or` for macOS; C02 `and` to `or` for Linux; C03 invert classification check; C04 return `None` |
| `describe` | D01 erase git path; D02 omit git refusal code; D03 omit confinement refusal code; D04 omit network field; D05 return `None` |

**Killed before:** E02, G01, G04, G05, G06, G08, G09, U02, P01, C01,
C04, D03, D04, D05.

**Survived before, killed after:** E01, G02, G03, G07, M01, M02, M03,
M04, M05, H01, H02, A01, A02, A03, S01, S02, S03, S04, U01, U03, U04,
U05, C02, C03, D01, D02.

**Survivors left:** none. **Equivalent mutants:** none identified.
**Untestable offline:** none among generated mutants.

## New tests

All new tests pass on the original and each fails on at least one recorded
mutant. The IDs show representative mutants each test kills.

| Function | New test | Mutants |
| --- | --- | --- |
| `HostCapabilityError.__init__` | `HostCapabilityErrorDetails.test_message_places_detail_before_code` | E01 |
| `resolve_git` | `ResolveGit.test_unusable_candidates_are_named_and_not_selected` | G02, G03 |
| `resolve_git` | `ResolveGit.test_path_fallback_returns_the_found_executable` | G07 |
| `helper_source` | `HelperSource.test_embeds_the_numeric_selftest_exit` | H01, H02 |
| `_HELPER.mount` | `HelperSource.test_mount_reports_a_failed_system_call` | M01, M02 |
| `_HELPER.mount_points` | `HelperSource.test_mount_points_reads_valid_records` | M03 |
| `_HELPER.writable` | `HelperSource.test_writable_reports_failed_and_successful_probes` | M04, M05 |
| `confined_argv` | `ConfinedArgv.test_includes_the_network_boundary_and_every_canary` | A01, A02, A03 |
| `_selftest_argv` | `SelftestArgv.test_creates_roots_and_checks_home_and_available_extra_locations` | S01, S02, S03, S04 |
| `_userns_usable` | `UserNamespaceProbe.test_requires_an_executable_unshare_without_running_a_probe` | U01 |
| `_userns_usable` | `UserNamespaceProbe.test_probe_uses_a_bounded_subprocess_and_its_exit_status` | U03, U04, U05 |
| `confinement` | `ConfinementSelectionOffline.test_linux_needs_a_successful_probe_and_rejects_non_synthetic_work` | C02, C03 |
| `describe` | `DescribeOffline.test_reports_git_path_or_its_refusal` | D01, D02 |

## Defects found

None. No mutant represented a correction to the original behavior.

## Verification

The before and after mutation runs completed without timeouts or runner
errors. Tests used an isolated temporary `HOME`. The helper function checks
extract individual definitions from the embedded source and supply invented
paths and mocked system calls; they do not claim live Linux namespace testing.
`python3 tests/test_suite.py` passed: 592 checks, 0 failures, 5 environmental
skips. `tests/test_hostenv.py` passed: 45 tests, 0 failures, 11 unavailable
Linux boundary skips.

The related `test_automatic_gate.py`, `test_delegation_audit.py`, and
`test_onboard.py` suites passed. `test_automatic_delegation_e2e.py` and
`test_local_worker.py` could not run here because their local HTTP fixtures
received `PermissionError: [Errno 1] Operation not permitted` when binding
to `127.0.0.1`; this sandbox disallows even loopback listeners. Those errors
occurred in test setup, before exercising `hostenv.py`.
