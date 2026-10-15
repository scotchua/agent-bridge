# Mutation sweep: `src/agent_bridge/chat/hermes.py`

The throwaway runner generated 53 single-edit AST mutants across every function and the nested `drain` function. It flipped comparisons, swapped `and`/`or`, negated conditions, incremented numeric constants, returned `None`, an empty dictionary, or `False`, removed statements, swapped `+`/`-`, and dropped a raise. It skipped comments, logging, and docstrings. Each mutant ran in a separate Python process against an in-memory replacement of the module with an 8-second limit and an isolated temporary home. The source file was never written and was checked byte-for-byte after each pass.

| Pass | Assertion kills | Timeout kills | Total killed | Kill rate | Survived |
| --- | ---: | ---: | ---: | ---: | ---: |
| Existing Hermes tests | 28 | 1 | 29 / 53 | 54.7% | 24 |
| Existing plus new tests | 52 | 1 | 53 / 53 | 100.0% | 0 |

The timeout kill is mutant M37, which negates the stream-end condition in `drain` and hangs. Assertion-only kill rates are 52.8% before and 98.1% after. The before pass selected the existing `HermesTests` and `SelectedHermesTests` classes; the after pass added `HermesMutationTests`.

## Mutants generated

| ID | Function | Line | Mutation | Before | After |
| --- | --- | ---: | --- | --- | --- |
| M01 | `child_environment` | 35 | return none | killed | killed |
| M02 | `decode_result` | 43 | flip comparison | killed | killed |
| M03 | `HermesAdapter` | 56 | increment number | survived | killed |
| M04 | `HermesAdapter.__init__` | 59 | remove statement | killed | killed |
| M05 | `HermesAdapter._cleanup_stale_jobs` | 68 | flip comparison | survived | killed |
| M06 | `HermesAdapter.fingerprint` | 75 | negate condition | killed | killed |
| M07 | `HermesAdapter.status` | 89 | return none | killed | killed |
| M08 | `HermesAdapter._terminate_process_group` | 96 | flip comparison | survived | killed |
| M09 | `HermesAdapter._run_process` | 107 | flip comparison | killed | killed |
| M10 | `HermesAdapter._run_process.drain` | 121 | flip comparison | survived | killed |
| M11 | `HermesAdapter._call` | 161 | flip comparison | killed | killed |
| M12 | `HermesAdapter.start` | 187 | return none | killed | killed |
| M13 | `HermesAdapter.start_selected` | 190 | return none | killed | killed |
| M14 | `HermesAdapter.continue_` | 193 | return none | killed | killed |
| M15 | `HermesAdapter.poll` | 197 | return none | survived | killed |
| M16 | `HermesAdapter.read` | 200 | flip comparison | killed | killed |
| M17 | `child_environment` | 34 | remove statement | killed | killed |
| M18 | `decode_result` | 47 | flip comparison | killed | killed |
| M19 | `HermesAdapter` | 56 | remove statement | survived | killed |
| M20 | `HermesAdapter.__init__` | 60 | remove statement | killed | killed |
| M21 | `HermesAdapter._cleanup_stale_jobs` | 68 | swap boolean | survived | killed |
| M22 | `HermesAdapter.fingerprint` | 81 | return none | survived | killed |
| M23 | `HermesAdapter.status` | 90 | return none | killed | killed |
| M24 | `HermesAdapter._terminate_process_group` | 94 | negate condition | survived | killed |
| M25 | `HermesAdapter._run_process` | 111 | flip comparison | killed | killed |
| M26 | `HermesAdapter._run_process.drain` | 116 | negate condition | killed | killed |
| M27 | `HermesAdapter._call` | 164 | flip comparison | survived | killed |
| M28 | `HermesAdapter.poll` | 196 | remove statement | survived | killed |
| M29 | `HermesAdapter.read` | 200 | negate condition | killed | killed |
| M30 | `decode_result` | 47 | flip comparison | killed | killed |
| M31 | `HermesAdapter.__init__` | 61 | remove statement | survived | killed |
| M32 | `HermesAdapter._cleanup_stale_jobs` | 65 | negate condition | survived | killed |
| M33 | `HermesAdapter.fingerprint` | 72 | remove statement | survived | killed |
| M34 | `HermesAdapter.status` | 85 | remove statement | killed | killed |
| M35 | `HermesAdapter._terminate_process_group` | 96 | negate condition | survived | killed |
| M36 | `HermesAdapter._run_process` | 142 | flip comparison | survived | killed |
| M37 | `HermesAdapter._run_process.drain` | 118 | negate condition | timeout | timeout |
| M38 | `HermesAdapter._call` | 164 | swap boolean | survived | killed |
| M39 | `HermesAdapter.read` | 205 | return none | killed | killed |
| M40 | `decode_result` | 47 | flip comparison | killed | killed |
| M41 | `HermesAdapter._cleanup_stale_jobs` | 68 | negate condition | survived | killed |
| M42 | `HermesAdapter.fingerprint` | 73 | remove statement | killed | killed |
| M43 | `HermesAdapter.status` | 86 | remove statement | survived | killed |
| M44 | `HermesAdapter._terminate_process_group` | 99 | increment number | survived | killed |
| M45 | `HermesAdapter._run_process` | 136 | negate condition | killed | killed |
| M46 | `HermesAdapter._run_process.drain` | 121 | negate condition | killed | killed |
| M47 | `HermesAdapter._call` | 166 | swap boolean | killed | killed |
| M48 | `HermesAdapter.read` | 202 | remove statement | killed | killed |
| M49 | `HermesAdapter._cleanup_stale_jobs` | 64 | swap arithmetic | survived | killed |
| M50 | `decode_result` | 48 | drop raise | killed | killed |
| M51 | `HermesAdapter.status` | 87 | remove statement | survived | killed |
| M52 | `HermesAdapter.poll` | 197 | return empty | survived | killed |
| M53 | `HermesAdapter.poll` | 197 | return false | survived | killed |

## New tests

Every test below passes on the original and fails on the listed recorded mutant. The tests are grouped by the function they exercise in `HermesMutationTests`.

| Test | Example mutant killed |
| --- | --- |
| `test_child_environment_uses_default_source` | M01 |
| `test_timeout_contract` | M03 |
| `test_cleanup_stale_jobs_preserves_cutoff_and_recent_jobs` | M05 |
| `test_constructor_removes_stale_jobs` | M31 |
| `test_fingerprint_hashes_launcher_and_implementation` | M22 |
| `test_status_removes_stale_jobs` | M51 |
| `test_status_requires_private_directory_verification` | M43 |
| `test_terminate_process_group_skips_finished_and_kills_running` | M08 |
| `test_run_process_accepts_exact_output_limit` | M10 |
| `test_run_process_rejects_zero_remaining_time` | M36 |
| `test_call_rejects_empty_and_oversized_prompts_before_dispatch` | M27 |
| `test_poll_validates_job_id_and_reports_completion` | M15 |

## Survivors left

None. Equivalent: none; untestable offline: none; defect: none.

## Defects found

None. No expected-failure tests were needed.

## Verification

- `PYTHONPATH=src:tests python3 -m unittest -q test_chat_hermes test_peer_hermes`: 62 tests passed.
- `python3 tests/test_suite.py`: 593 passed, 0 failed, 5 environmental skips.
- `python3 tests/test_onboard.py`: passed.
- `python3 tests/test_local_worker.py`: 14 setup errors because the sandbox refused binding `127.0.0.1`; this is outside the Hermes mutation target.
- `git diff` confirms no production file changed.
