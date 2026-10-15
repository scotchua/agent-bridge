# Mutation sweep: `src/agent_bridge/errors.py`

The offline sweep generated 27 one-change mutants, with an eight-second limit for each test run. Each mutant was written to a temporary `errors.py` and loaded as `agent_bridge.errors` in a fresh Python process. The source file in the worktree was never written. Its SHA-256 before and after the sweep was `658b56d859f3a2bf10d95e52f2f193d1ffdc583f45948066a0c3822a973108f7`.

The before run used the existing `test_health.ClassificationTests.test_diagnostic_compatibility_is_closed_and_category_bound`. The after run used that test plus all nine tests in `tests/test_errors.py`. A failing assertion or import was a kill. No run timed out.

| Result | Killed | Generated | Kill rate |
| --- | ---: | ---: | ---: |
| Before | 11 | 27 | 40.7% |
| After | 27 | 27 | 100% |

| Mutant | Change | Before | After |
| --- | --- | --- | --- |
| M01 | Reverse base-category equality | killed | killed |
| M02 | Reverse diagnostic equality | killed | killed |
| M03 | Change diagnostic `and` to `or` | killed | killed |
| M04 | Read diagnostic instead of base category | killed | killed |
| M05 | Return `None` for promoted diagnostic | survived | killed |
| M06 | Change status fallback `or` to `and` | killed | killed |
| M07 | Return `None` for unknown category | killed | killed |
| M08 | Return `None` for ordinary status | killed | killed |
| M09 | Return `None` from `hint` | survived | killed |
| M10 | Return a nonempty fallback hint | survived | killed |
| M11 | Empty the nonzero-exit hint | survived | killed |
| M12 | Require both retry sets (`and`) | survived | killed |
| M13 | Negate transient membership | survived | killed |
| M14 | Negate corrective membership | survived | killed |
| M15 | Return `False` from `is_retryable` | survived | killed |
| M16 | Negate the `BrokerError` type check | survived | killed |
| M17 | Drop the invalid-category `TypeError` raise | survived | killed |
| M18 | Remove the base exception initializer | survived | killed |
| M19 | Remove the `category` assignment | survived | killed |
| M20 | Change the peer-timeout wire value | killed | killed |
| M21 | Remove peer timeout from deterministic categories | killed | killed |
| M22 | Add peer timeout to a second retry class | killed | killed |
| M23 | Increase the overlap threshold from one to two | survived | killed |
| M24 | Drop the unclassified-category raise | survived | killed |
| M25 | Drop the overlapping-category raise | survived | killed |
| M26 | Lower the overlap threshold to zero (`>= 1`) | killed | killed |
| M27 | Return an empty string from `hint` | survived | killed |

New tests, grouped by behavior:

- `CategoryFromStatusTests.test_diagnostic_requires_matching_base_and_code` killed M05.
- `CategoryFromStatusTests.test_known_missing_and_unknown_categories` killed M06.
- `HintTests.test_known_hints_are_closed_and_unknown_is_empty` killed M09.
- `RetryTests.test_retry_classes_have_distinct_decisions` killed M12.
- `BrokerErrorTests.test_valid_category_is_preserved_in_exception` killed M18.
- `BrokerErrorTests.test_non_category_is_rejected_with_type_error` killed M17.
- `ImportGuardTests.test_unclassified_category_fails_import` killed M24.
- `ImportGuardTests.test_overlapping_category_fails_import` killed M25.
- `VocabularyTests.test_peer_timeout_has_stable_wire_value` killed M20.

Each named test was also run alone against its named mutant and failed; all nine passed on the original module. The import-guard tests inject one invalid retry-class assignment into a source copy, then check that importing it raises.

No `+` or `-` arithmetic occurs in executable code in this module, so an arithmetic swap was not applicable. The sweep covered the executable comparisons (`==`, `>`), Boolean operators, condition negation, numeric threshold, return replacements, statement removal, and dropped raises. Comments and docstrings were excluded.

Survivors left: none. Equivalent mutants: none. Untestable offline: none. Defects found: none.

Verification: `python3 tests/test_suite.py` passed with 593 checks, zero failures, and five environmental skips. `test_errors.py` passed all nine tests, and `test_onboard.py` passed all 28 tests. The separate `test_local_worker.py` run could not bind its loopback HTTP fixture in this sandbox (`PermissionError: Operation not permitted`); it does not exercise this module directly.
