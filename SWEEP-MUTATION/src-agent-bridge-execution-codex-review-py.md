# Mutation sweep: `src/agent_bridge/execution/codex_review.py`

The offline sweep generated 35 single-change mutants across all 17 functions. Each mutant was compiled from an in-memory copy and imported in a separate Python process. The existing `tests/test_codex_review.py` suite ran with a 15-second limit per mutant; Git and the fake Codex executable stayed local. The production module was never written, and its SHA-256 remained `36f7c0651a84b71a24f06205d6ad90e03bc0bcacf63a4e1429a07391bb2ebe40`.

| Result | Killed | Survived | Kill rate |
| --- | ---: | ---: | ---: |
| Before new tests | 28 | 7 | 80.0% |
| After new tests | 35 | 0 | 100.0% |

The last three operators (M33–M35) were checked before the new tests by loading the original test module from `HEAD`; the other 32 were checked before the tests were added. All 35 were checked after the tests were added. No run timed out.

| ID | Function:line | Single mutation | Before | After |
| --- | --- | --- | --- | --- |
| M01 | `_json_sha:47` | Return `None` | survived | killed |
| M02 | `_git:51` | Return `None` | killed | killed |
| M03 | `_blob:57` | Flip exit-code equality | killed | killed |
| M04 | `compute_range:73` | Negate empty path guard | killed | killed |
| M05 | `_review_command:92` | Negate effort guard | survived | killed |
| M06 | `_review_command:94` | Change model `and` to `or` | killed | killed |
| M07 | `build_prompt:111` | Return `None` | killed | killed |
| M08 | `validate_findings:136` | Change minimum line from 1 to 0 | survived | killed |
| M09 | `validate_findings:142` | Negate context type guard | killed | killed |
| M10 | `validate_findings:144` | Change path/context `and` to `or` | killed | killed |
| M11 | `validate_findings:147` | Change verdict/severity `and` to `or` | killed | killed |
| M12 | `_one_json:159` | Negate trailing-data guard | killed | killed |
| M13 | `_one_json:161` | Return `None` | killed | killed |
| M14 | `_observed:170` | Return `"unverified"` instead of event value | survived | killed |
| M15 | `_remove:181` | Negate worktree absence result | killed | killed |
| M16 | `run_review:193` | Flip provider equality | killed | killed |
| M17 | `run_review:210` | Change empty/large brief `or` to `and` | survived | killed |
| M18 | `run_review:260` | Negate initial clean-worktree guard | killed | killed |
| M19 | `run_review:276` | Negate output-empty guard | killed | killed |
| M20 | `run_review:299` | Negate cleanup guard | killed | killed |
| M21 | `_load_json:307` | Negate path type guard | killed | killed |
| M22 | `validate_receipt:313` | Flip receipt status comparison | killed | killed |
| M23 | `validate_receipt:321` | Change digest length from 64 to 63 | killed | killed |
| M24 | `_receipt_from_location:328` | Flip exclusive receipt-input comparison | killed | killed |
| M25 | `_receipt_from_location:331` | Stop rejecting `/` in job IDs | survived | killed |
| M26 | `_verify_evidence:346` | Flip evidence comparison | killed | killed |
| M27 | `_verify_evidence:348` | Flip tree comparison | killed | killed |
| M28 | `review_verify:373` | Flip requested-head comparison | killed | killed |
| M29 | `review_verify:391` | Negate duplicate-disposition guard | killed | killed |
| M30 | `review_verify:400` | Flip fixed disposition comparison | killed | killed |
| M31 | `review_verify:408` | Flip chain-base comparison | killed | killed |
| M32 | `main:456` | Return 1 instead of 0 | survived | killed |
| M33 | `validate_findings:146` | Remove append of a validated finding | killed | killed |
| M34 | `validate_findings:130` | Swap set subtraction for addition | killed | killed |
| M35 | `review_verify:406` | Drop incomplete-disposition raise | killed | killed |

## New tests

Tests are grouped by the function they exercise in `tests/test_codex_review.py`. Each passed on the original and failed on its corresponding previously surviving mutant:

| Function | Test | Mutant killed |
| --- | --- | --- |
| `_json_sha` | `test_json_sha_uses_canonical_json` | M01 |
| `_review_command` | `test_review_command_omits_unrequested_reasoning_effort` | M05 |
| `validate_findings` | `test_validate_findings_rejects_zero_line` | M08 |
| `_observed` | `test_observed_prefers_latest_nonempty_metadata` | M14 |
| `run_review` | `test_run_review_refuses_empty_brief_before_range_resolution` | M17 |
| `_receipt_from_location` | `test_receipt_location_rejects_job_id_with_separator` | M25 |
| `main` | `test_main_returns_success_for_verified_review` | M32 |

## Survivors left

None. Equivalent mutants: none. Untestable offline: none.

## Defects found

None.

## Verification

- `python3 -m unittest tests/test_codex_review.py -q`: 23 tests passed.
- `python3 tests/test_suite.py`: 593 passed, 0 failed, 5 platform skips.
- `tests/test_onboard.py`, `tests/test_hostenv.py`, `tests/test_automatic_gate.py`, and `tests/test_delegation_audit.py`: passed.
- `tests/test_local_worker.py` and `tests/test_automatic_delegation_e2e.py`: blocked in fixture setup because the sandbox denies binding a loopback HTTP server (`PermissionError: [Errno 1] Operation not permitted`).
