# Mutation sweep: `src/agent_bridge/receipts.py`

## Method and result

- Source SHA-256 before and after: `c6e1672f88c8de7ad401fc0783093b21d69931fa6c9b73b65d2c4060d17b04ff` (byte-identical).
- Generated 32 single-change mutants across the sole function, `build`; comments and the module docstring were excluded. Changes cover branch conditions, `or` to `and`, numeric constants, return values, field values, one removed raise, and the attempt projection. The module contains no `<`, `<=`, `>`, `>=`, `==`, `!=`, or arithmetic `+`/`-` operator suitable for those operator swaps.
- Each mutant was compiled from an in-memory copy of the source. The relevant existing test was `ReplayOverlapTests.test_native_structured_exhaustion_keeps_diagnostic_and_adds_receipt`; the added direct tests ran against the same mutant. Each child process had a 15-second timeout and an isolated temporary home. No production file was written.
- **Before:** 0/32 killed (0%). **After:** 32/32 killed (100%). The before rate describes this bounded in-process probe, not a claim about every integration test: the selected existing test runs its worker in a separate process and therefore cannot observe an in-memory mutation.

## Mutants

| ID | Mutation | Existing test | After added tests |
| --- | --- | --- | --- |
| 1 | timeout branch misses timeout | survived | killed |
| 2 | auth branch misses auth | survived | killed |
| 3 | corrective branch excluded | survived | killed |
| 4 | exhausted branch excluded | survived | killed |
| 5 | corrective or becomes and | survived | killed |
| 6 | timeout outcome changed | survived | killed |
| 7 | timeout action changed | survived | killed |
| 8 | auth outcome changed | survived | killed |
| 9 | auth action changed | survived | killed |
| 10 | corrective outcome changed | survived | killed |
| 11 | corrective action changed | survived | killed |
| 12 | receipt version incremented | survived | killed |
| 13 | category from outcome | survived | killed |
| 14 | prompt hash empty | survived | killed |
| 15 | config hash empty | survived | killed |
| 16 | attempt count incremented | survived | killed |
| 17 | attempts empty | survived | killed |
| 18 | attempt projection drops duration | survived | killed |
| 19 | stages empty | survived | killed |
| 20 | negative elapsed unclamped | survived | killed |
| 21 | elapsed precision reduced | survived | killed |
| 22 | elapsed floor incremented | survived | killed |
| 23 | budget empty | survived | killed |
| 24 | next action empty | survived | killed |
| 25 | allowed actions empty | survived | killed |
| 26 | authentication negated | survived | killed |
| 27 | approval enabled | survived | killed |
| 28 | question list empty | survived | killed |
| 29 | return None | survived | killed |
| 30 | return empty | survived | killed |
| 31 | return False | survived | killed |
| 32 | drop unsupported raise | survived | killed |

## New tests

- `test_build_timeout_identity_and_actions`
- `test_build_auth_failure_requires_authentication`
- `test_build_corrective_and_exhausted_categories`
- `test_build_hashes_budget_and_verbatim_question`
- `test_build_attempts_and_stages`
- `test_build_elapsed_is_clamped_and_rounded`
- `test_build_rejects_unsupported_category`

Every listed test failed on at least one recorded mutant and passed on the original module. The seven tests are also invoked by `python3 tests/test_suite.py`.

| Test | Example mutant killed |
| --- | --- |
| `test_build_timeout_identity_and_actions` | 6, timeout outcome changed |
| `test_build_auth_failure_requires_authentication` | 8, auth outcome changed |
| `test_build_corrective_and_exhausted_categories` | 5, corrective `or` becomes `and` |
| `test_build_hashes_budget_and_verbatim_question` | 14, prompt hash empty |
| `test_build_attempts_and_stages` | 17, attempts empty |
| `test_build_elapsed_is_clamped_and_rounded` | 20, negative elapsed unclamped |
| `test_build_rejects_unsupported_category` | 32, drop unsupported raise |

## Survivors left

None. Equivalent: none. Untestable offline: none. Defect: none.

## Defects found

None.

## Verification

- `PYTHONPATH=src python3 -m unittest -q tests/test_receipts_mutation.py` passed (7 tests).
- `python3 tests/test_suite.py` passed: 594 checks, 0 failures, 5 documented environmental skips; it ran all seven new tests.
- `python3 -m unittest -q tests.test_onboard` passed: 28 tests.
- `tests.test_local_worker` could not bind its synthetic loopback HTTP server in this sandbox (14 setup errors with `PermissionError`); it does not exercise this receipt builder.
