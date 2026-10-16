# Mutation probe: `windows_auth.py`

The source file was read in full, and each mutant was compiled into the imported
module in a separate offline Python process. The source file was never edited.
Each run used a five-second timeout and the existing `test_windows_auth` module;
the second pass also used `test_windows_auth_mutation`. The runner checked that
the source bytes matched its initial snapshot after the pass and was deleted.

| Measure | Existing tests | With new tests |
| --- | ---: | ---: |
| Mutants generated | 39 | 39 |
| Killed | 25 | 39 |
| Survived | 14 | 0 |
| Kill rate | 64.1% | 100% |

One small source change was made per process. The set covers every function,
including the nested DPAPI call and methods. Mutations touch executable code
only, using comparison flips, condition negations, `and`/`or` swaps, numeric
changes by one, altered return values, a removed statement, a `+`/`-` swap,
and a dropped exception raise. None touch comments, logging, or docstrings.

| ID | Function and mutation | Before | After |
| --- | --- | --- | --- |
| M01 | `AuthError.__init__`: reason becomes `None` | killed | killed |
| M02 | `_entropy`: omit provider bytes | killed | killed |
| M03 | `dpapi_protector`: flip Windows comparison | survived | killed |
| M04 | `dpapi_protector._call`: negate call result check | survived | killed |
| M05 | `_Dpapi.protect`: return `None` | survived | killed |
| M06 | `_Dpapi.unprotect`: return `None` | survived | killed |
| M07 | `_resolve_protector`: negate supplied-protector check | killed | killed |
| M08 | `Enrolment.as_record`: increment schema version | killed | killed |
| M09 | `auth_root`: alter directory name | survived | killed |
| M10 | `record_path`: alter record suffix | survived | killed |
| M11 | `secret_path`: alter secret suffix | survived | killed |
| M12 | `lock_path`: alter lock suffix | survived | killed |
| M13 | `_provider`: swap `or` for `and` | killed | killed |
| M14 | `_validate_token`: negate capsule check | killed | killed |
| M15 | `enrol`: negate consent check | killed | killed |
| M16 | `enrol`: flip protected-size comparison | killed | killed |
| M17 | `enrol`: treat zero timestamp as absent | survived | killed |
| M18 | `_write_enrolment`: flip identity-error comparison | killed | killed |
| M19 | `_existing_secret`: flip absence-error comparison | killed | killed |
| M20 | `_roll_back_secret`: return `True` on identity error | killed | killed |
| M21 | `read_enrolment`: negate record-existence check | killed | killed |
| M22 | `read_enrolment`: swap schema guard `or` for `and` | survived | killed |
| M23 | `read_enrolment`: swap consent guard `or` for `and` | survived | killed |
| M24 | `load_capsule`: negate enrolment check | killed | killed |
| M25 | `load_capsule`: flip ciphertext-size comparison | killed | killed |
| M26 | `load_capsule`: swap secret-schema guard `or` for `and` | survived | killed |
| M27 | `revoke`: negate root-existence check | killed | killed |
| M28 | `_remove_enrolment_files`: raise for an absent file | survived | killed |
| M29 | `enrolment_state`: flip Windows comparison | killed | killed |
| M30 | `enrolment_state`: negate closed-lane check | killed | killed |
| M31 | `enrolment_states`: negate lane-presence check | killed | killed |
| M32 | `redact`: negate capsule check | killed | killed |
| M33 | `redact`: negate token check | killed | killed |
| M34 | `redact`: return an empty replacement | killed | killed |
| M35 | `enrolment_state`: swap `+` for `-` in next step | killed | killed |
| M36 | `AuthError.__init__`: remove reason assignment | killed | killed |
| M37 | `enrol`: drop consent exception raise | killed | killed |
| M38 | `dpapi_protector._call`: change UI flag from `0x1` to `0x2` | survived | killed |
| M39 | `revoke`: return `False` | killed | killed |

## New tests

Each name below passed on the original module and failed on at least the
recorded mutant shown. Tests are grouped by the function they exercise.

| Function | Test name | Mutant killed |
| --- | --- | --- |
| `auth_root` | `AuthPathTests.test_auth_root_uses_the_auth_directory` | M09 |
| `record_path` | `AuthPathTests.test_record_path_uses_the_enrolment_suffix` | M10 |
| `secret_path` | `AuthPathTests.test_secret_path_uses_the_secret_suffix` | M11 |
| `lock_path` | `AuthPathTests.test_lock_path_uses_the_lock_suffix` | M12 |
| `enrol` | `EnrolBoundaryTests.test_zero_timestamp_is_preserved` | M17 |
| `read_enrolment` | `ReadEnrolmentBoundaryTests.test_wrong_record_schema_is_unreadable` | M22 |
| `read_enrolment` | `ReadEnrolmentBoundaryTests.test_empty_consent_statement_is_unreadable` | M23 |
| `load_capsule` | `LoadCapsuleBoundaryTests.test_wrong_secret_schema_is_unreadable` | M26 |
| `_remove_enrolment_files` | `RevokeBoundaryTests.test_missing_secret_does_not_prevent_record_removal` | M28 |
| `dpapi_protector` | `DpapiBoundaryTests.test_windows_branch_creates_a_protector` | M03 |
| `dpapi_protector._call` | `DpapiBoundaryTests.test_failed_dpapi_call_raises_fixed_error` | M04 |
| `_Dpapi.protect` | `DpapiBoundaryTests.test_protect_returns_dpapi_output` | M05, M38 |
| `_Dpapi.unprotect` | `DpapiBoundaryTests.test_unprotect_returns_dpapi_output` | M06 |

## Survivors left

None. There were no equivalent mutants, no mutants left untestable offline,
and no mutant that represented a correction to the original code.

## Defects found

None in this probe.

The DPAPI tests use a stand-in `ctypes.windll` to exercise Windows-only
branches. They do not establish behavior on a live Windows host.

Verification: `python3 tests/test_suite.py` passed with 592 checks, 0 failures,
and 5 environmental skips in an isolated temporary home.
The targeted Windows auth tests passed (73 tests), as did the provision driver
(68 tests) and onboarding tests (28 tests). `tests/test_local_worker.py` could
not start its loopback HTTP fixture in this sandbox: all 14 setups failed at
`ThreadingHTTPServer(("127.0.0.1", 0), ...)` with `PermissionError` before the
test bodies ran.
`tests/test_hostenv.py` passed (32 tests, 11 skips),
`tests/test_automatic_gate.py` passed (169 tests), and
`tests/test_delegation_audit.py` passed (40 tests).
`tests/test_automatic_delegation_e2e.py` had the same sandbox restriction:
its 42 setups could not bind the loopback model-service fixture.
