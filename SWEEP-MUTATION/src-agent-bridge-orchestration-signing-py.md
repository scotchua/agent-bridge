# Mutation test: `src/agent_bridge/orchestration/signing.py`

The source file was read in full and mutated only as an AST compiled in a child
process. The on-disk file was never replaced. Each child ran the focused
`tests/test_signing.py` suite offline with an eight-second timeout. The baseline
used its 20 existing tests; the final pass used all 29 tests. The source SHA-256
was `1965a247595a119d2eee8121977e803267a8a937548d4247ccb195818eaa473e`
before and after both passes.

## Results

| Function | Generated | Killed before | Killed after |
| --- | ---: | ---: | ---: |
| `_modp_inv` | 5 | 5 | 5 |
| `_recover_x` | 22 | 16 | 21 |
| `_point_add` | 16 | 16 | 16 |
| `_point_mul` | 13 | 13 | 13 |
| `_point_equal` | 17 | 15 | 17 |
| `_decompress` | 20 | 16 | 18 |
| `_sha512_modq` | 3 | 3 | 3 |
| `_is_small_order` | 3 | 3 | 3 |
| `verify` | 22 | 14 | 22 |
| `verify_hex` | 7 | 5 | 7 |
| **Total** | **128** | **106 (82.81%)** | **125 (97.66%)** |

There were 8 comparison flips, 1 Boolean swap, 16 condition negations, 13
numeric increments, 55 return substitutions (`None`, `False`, or an empty
tuple), 23 removed statements, and 12 addition/subtraction swaps. The source
has no exception raise to drop. Comments, docstrings, and logging were not
mutation targets. Two `_point_mul` mutants (049 and 052) failed by reaching the
eight-second timeout; these count as killed because they made the test run
nonterminating. Across the 125 behavior-changing mutants, the kill rate rose
from 106/125 (84.8%) to 125/125 (100%).

## New tests and mutants they kill

| Test in `tests/test_signing.py` | Newly killed mutant IDs |
| --- | --- |
| `PointEqualMutationTests.test_unequal_points_return_exactly_false` | 035, 106 |
| `RecoverXMutationTests.test_field_modulus_is_not_a_canonical_y` | 065, 094 |
| `RecoverXMutationTests.test_zero_x_cannot_have_odd_sign` | 096 |
| `RecoverXMutationTests.test_y_without_a_curve_point_returns_none` | 098, 099 |
| `DecompressMutationTests.test_wrong_length_returns_none` | 109, 110 |
| `VerifyMutationTests.test_a_valid_signature_with_trailing_zero_byte_is_rejected` | 084, 085 |
| `VerifyMutationTests.test_a_wrong_length_returns_exactly_false` | 085, 119 |
| `VerifyMutationTests.test_each_malformed_component_returns_exactly_false` | 120, 121, 122, 123, 124 |
| `VerifyHexMutationTests.test_invalid_hex_returns_exactly_false` | 089, 127 |

Each listed test passed on the original source and failed on at least one
listed mutant. The final three survivors are equivalent mutations: 008 and
011 replace `_decompress`'s existing `return None` with `return None`, and 066
does the same in `_recover_x`. No behavior-changing or offline-untestable
survivors remain.

## Defects found

None.

## Verification

`python3 tests/test_signing.py` passed with 29 tests. The final requested
`python3 tests/test_suite.py` run passed with 593 checks and 5 environmental
skips. `test_onboard.py` passed with 28 tests, `test_release_signing.py` with
39, and `test_windows_rootfs.py` with 97. `test_local_worker.py` could not run
in this sandbox: all 14 cases failed during setup because binding its local
`127.0.0.1` fake HTTP server raised `PermissionError: [Errno 1] Operation not
permitted`. No real inference or external API was used.
