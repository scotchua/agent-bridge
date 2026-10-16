# Mutation test: `src/agent_bridge/platform/windows_acl.py`

## Method

- Mutated an in-memory copy of the source, one edit per run. The production file was never written; its bytes were checked against the original after each sweep.
- Ran the 38 existing offline codec and judgment tests from `tests/test_windows_acl_security.py` in a fresh Python process for each mutant, with a 12-second timeout. The after sweep also ran the 11 new tests in `tests/test_windows_acl_mutations.py` (49 tests total per mutant).
- Mutations covered every function and property in the module. Operators included comparison flips, boolean swaps and negations, numeric changes, return replacements, statement removal, arithmetic swap, and dropped raise. Comments, logging, and docstrings were excluded.

## Kill rates

| Stage | Killed | Generated | Kill rate |
| --- | ---: | ---: | ---: |
| Before new tests | 36 | 51 | 70.6% |
| After new tests | 48 | 51 | 94.1% |

The 3 remaining survivors are equivalent for valid inputs; all 48 behavior-changing mutants were killed after the new tests. Baseline runs passed with zero failures.

## Mutants

| ID | Function | Mutation | Before | After |
| --- | --- | --- | --- | --- |
| M01 | `parse_sid` | short header boundary | survived | killed |
| M02 | `parse_sid` | revision equality | killed | killed |
| M03 | `parse_sid` | maximum sub-authorities | survived | killed |
| M04 | `parse_sid` | large authority boundary | survived | killed |
| M05 | `encode_sid` | field count boundary | survived | killed |
| M06 | `encode_sid` | field guard conjunction | survived | killed |
| M07 | `encode_sid` | authority upper bound | killed | killed |
| M08 | `encode_sid` | sub-authority upper bound | killed | killed |
| M09 | `canonical_sid` | empty return | killed | killed |
| M10 | `acl_size` | short header boundary | killed | killed |
| M11 | `acl_size` | minimum size boundary | survived | killed |
| M12 | `parse_acl` | short ACL header | survived | killed |
| M13 | `parse_acl` | supported revisions | killed | killed |
| M14 | `parse_acl` | ACE header boundary | survived | killed |
| M15 | `parse_acl` | ACE size guard conjunction | survived | killed |
| M16 | `parse_acl` | SID length boundary | killed | killed |
| M17 | `parse_acl` | drop ACE append | killed | killed |
| M18 | `owner_only_aces` | directory condition | killed | killed |
| M19 | `build_owner_only_acl` | ACE size plus-minus | killed | killed |
| M20 | `build_owner_only_acl` | empty return | killed | killed |
| M21 | `parse_security_descriptor` | short descriptor boundary | killed | killed |
| M22 | `parse_security_descriptor` | self-relative condition | killed | killed |
| M23 | `parse_security_descriptor` | owner header boundary | survived | killed |
| M24 | `parse_security_descriptor` | DACL header boundary | survived | killed |
| M25 | `parse_security_descriptor` | DACL guard conjunction | survived | killed |
| M26 | `build_owner_only_descriptor` | DACL offset plus one | killed | killed |
| M27 | `build_owner_only_descriptor` | redundant None branch | survived | survived |
| M28 | `is_directory` | empty return | killed | killed |
| M29 | `is_reparse_point` | empty return | killed | killed |
| M30 | `parse_directory_listing` | record header boundary | survived | survived |
| M31 | `parse_directory_listing` | name guard conjunction | killed | killed |
| M32 | `parse_directory_listing` | chain minimum boundary | killed | killed |
| M33 | `parse_directory_listing` | chain alignment plus-minus | killed | killed |
| M34 | `parse_directory_listing` | drop duplicate tracking | killed | killed |
| M35 | `accepted_owners` | default owner condition | killed | killed |
| M36 | `accepted_owners` | administrator equality | killed | killed |
| M37 | `map_generic_rights` | generic mask condition | killed | killed |
| M38 | `map_generic_rights` | mapping union to subtraction | killed | killed |
| M39 | `grants_effective_access` | required equality | killed | killed |
| M40 | `encode_object_name` | maximum name boundary | survived | survived |
| M41 | `encode_object_name` | empty return | killed | killed |
| M42 | `judge_security` | owner comparison | killed | killed |
| M43 | `judge_security` | NULL DACL condition | killed | killed |
| M44 | `judge_security` | drop foreign principal finding | killed | killed |
| M45 | `judge_security` | drop inherited ACE finding | killed | killed |
| M46 | `judge_security` | inherit-only condition | killed | killed |
| M47 | `judge_security` | empty unknown-caller result | killed | killed |
| M48 | `is_exactly_owner_only` | judgment comparison | killed | killed |
| M49 | `is_exactly_owner_only` | exact-match conjunction | killed | killed |
| M50 | `encode_object_name` | drop oversize raise | killed | killed |
| M51 | `map_generic_rights` | None return | killed | killed |

## New tests and recorded mutant kills

| Test | Mutants it kills |
| --- | --- |
| `ParseSidBoundaryTests.test_zero_and_maximum_sub_authority_counts` | M01, M02, M03, M05 |
| `ParseSidBoundaryTests.test_authority_at_the_hex_boundary` | M01, M02, M04 |
| `EncodeSidBoundaryTests.test_sid_without_sub_authorities_is_valid` | M05 |
| `EncodeSidBoundaryTests.test_incomplete_sid_raises_value_error` | M06 |
| `AclSizeBoundaryTests.test_header_only_acl_has_a_valid_size` | M10, M11 |
| `ParseAclBoundaryTests.test_header_only_acl_is_empty` | M12, M13 |
| `ParseAclBoundaryTests.test_header_only_uninterpreted_ace_is_retained` | M13, M14 |
| `ParseAclBoundaryTests.test_zero_length_ace_is_refused` | M15 |
| `ParseSecurityDescriptorBoundaryTests.test_owner_sid_ending_at_buffer_boundary` | M01, M02, M05, M22, M23 |
| `ParseSecurityDescriptorBoundaryTests.test_empty_dacl_ending_at_buffer_boundary` | M10, M11, M12, M13, M22, M24 |
| `ParseSecurityDescriptorBoundaryTests.test_dacl_offset_is_ignored_without_present_bit` | M22, M25 |

## Survivors left

- **M27 (`build_owner_only_descriptor`) — equivalent.** `None or packed_descriptor` returns the same packed descriptor for every input.
- **M30 (`parse_directory_listing`) — equivalent for the stated contract.** A record ending exactly at the 68-byte header has an empty name and is rejected either at the record bound or the component-name check; both raise `ValueError`.
- **M40 (`encode_object_name`) — equivalent.** UTF-16-LE byte lengths are even, while `UNICODE_STRING_MAX_BYTES` is 65533. Changing `>` to `>=` cannot change a result.

## Defects found

None.

## Verification

- `python3 -m unittest tests/test_windows_acl_security.py tests/test_windows_acl_mutations.py`: passed (88 tests, 4 native-Windows skips).
- `python3 tests/test_suite.py`: passed (593 checks, 0 failed, 5 environmental skips).
- `python3 tests/test_onboard.py`: passed (28 tests). `python3 tests/test_local_worker.py`: blocked in setup by this sandbox denying loopback socket bind (`PermissionError: [Errno 1]`); no assertions ran.
