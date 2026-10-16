# `src/agent_bridge/worker.py` mutation sweep

The source SHA-256 was `d059b427607c3c4489501d426dec868d078f772cdfa29aa1ced1719d2858bc43` before and after the sweep. A temporary runner copied the Python package, schema, config, fake peer executables, and relevant tests into a temporary directory. It changed only the copied `worker.py`, restored that copy after each run, and imposed a 90-second timeout per mutant. No packages, network calls, real peers, or client data were used.

**Generated:** 18 single-change mutants across all five functions (`_quarantine`, `_run_peer`, `_cleared_prompt`, `execute`, and `main`). The changes included comparison flips, `and`/`or` replacement, condition negation, a numeric constant changed by one, a `None` return, statement removal, `+`/`-` replacement, and a dropped raise. Comments, logging, and docstrings were excluded.

**Before:** 6/18 killed (33.3%). **After:** 18/18 killed (100%). Each mutant ran one relevant existing `test_suite.py` function before the added tests. The after run repeated that function and the new test group for the mutated function. A kill means a nonzero test exit; a timeout also counts as a kill. `M1` never executed its job and was caught by the existing happy path's 30-second wait.

| ID | Function | Mutation | Existing test | Before | After |
| --- | --- | --- | --- | --- | --- |
| Q1 | `_quarantine` | negate stdout presence | `test_schema_enforcement` | survived | killed |
| Q2 | `_quarantine` | negate stderr presence | `test_schema_enforcement` | survived | killed |
| Q3 | `_quarantine` | negate last-message presence | `test_schema_enforcement` | survived | killed |
| P1 | `_run_peer` | `peer == "claude"` to `!=` | `test_happy_path_both_directions` | survived | killed |
| P2 | `_run_peer` | invert Codex resume selection | `test_continuation_uses_exact_session_id` | killed | killed |
| C1 | `_cleared_prompt` | `or` to `and` on handoff pin | `test_happy_path_both_directions` | survived | killed |
| C2 | `_cleared_prompt` | verified bytes return to `None` | `test_happy_path_both_directions` | survived | killed |
| C3 | `_cleared_prompt` | drop changed-contract raise | `test_happy_path_both_directions` | survived | killed |
| E1 | `execute` | attempt cap 2 to 1 | `test_corrective_retry_and_no_identical_retry` | killed | killed |
| E2 | `execute` | resume/session `and` to `or` | `test_happy_path_both_directions` | killed | killed |
| E3 | `execute` | prompt budget `>` to `>=` | `test_input_validation` | survived | killed |
| E4 | `execute` | attempt cap `>=` to `>` | `test_corrective_retry_and_no_identical_retry` | killed | killed |
| E5 | `execute` | negate deterministic category check | `test_corrective_retry_and_no_identical_retry` | killed | killed |
| E6 | `execute` | exit value to `None` | `test_happy_path_both_directions` | survived | killed |
| E7 | `execute` | admission elapsed `-` to `+` | `test_happy_path_both_directions` | survived | killed |
| E8 | `execute` | prompt cap `>` to `<` | `test_happy_path_both_directions` | killed | killed |
| E9 | `execute` | remove argv record write | `test_happy_path_both_directions` | survived | killed |
| M1 | `main` | return from `execute` to `None` | `test_happy_path_both_directions` | killed | killed |

## New tests

- `test_worker_quarantine_mutations`: catches Q1–Q3 by checking all three quarantined byte streams.
- `test_worker_run_peer_mutations`: catches P1 by checking backend selection and UTF-8 prompt conversion.
- `test_worker_cleared_prompt_mutations`: catches C1–C3 by checking the required pin, verified bytes, and changed schema snapshot.
- `test_worker_execute_mutations`: catches E3, E6, E7, and E9 at the exact prompt budget boundary and in the job's return code, elapsed stage, and argv record.
- `test_worker_main_mutations`: catches M1 by checking the delegated exit code and job directory.

All five test groups pass on the original code. Each was also run independently against a recorded in-memory mutant and failed: Q1, P1, C1, E3, and M1 respectively. The temporary runner and machine-readable scratch results were deleted after the sweep.

## Survivors left

None. Equivalent: none. Untestable offline: none. Defect: none.

## Defects found

None.

## Verification

`python3 tests/test_suite.py` passed with 605 checks, 0 failures, and 5 documented platform skips during the sweep. `python3 tests/test_onboard.py` passed (28 tests). `python3 tests/test_local_worker.py` could not run its 14 synthetic HTTP tests because this sandbox denied binding to `127.0.0.1` with `PermissionError: [Errno 1] Operation not permitted`.
