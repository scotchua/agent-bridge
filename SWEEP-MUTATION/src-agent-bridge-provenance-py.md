# Mutation sweep: `src/agent_bridge/provenance.py`

The temporary runner made 36 single-edit mutants across `_git`, `source_hash`,
and `collect`. It changed only a disposable copy of the package for the existing
integration check and loaded each mutant from memory for the new unit tests.
Each run had a five-second limit. No network service or live inference was used.
The production module was byte-identical before and after the sweep.

## Results

| Measure | Before new tests | After new tests |
| --- | ---: | ---: |
| Regression mutants killed | 4/36 (11.1%) | 34/36 (94.4%) |
| Of these, time-boxed noncompletion | 1 | 0 |
| Documented-behavior defect detected | 0 | 1 |
| Equivalent mutants | 1 | 1 |

The existing check was `test_suite.test_provenance_completeness` against a
temporary package in a synthetic local Git repository. It killed H13, C07,
and C11 by error, and G07 by five-second noncompletion after the worker failed.
The after column combines those results with `test_provenance_mutation.py`.
At the time of the sweep, the new tests mechanically flagged H16 as an
unexpected success of an expected failure. It was classified as a defect
finding rather than a regression kill.
Thus mechanical detection after new tests is 35/36 (97.2%).

| ID | Function | Mutation | Before | After |
| --- | --- | --- | --- | --- |
| G01 | `_git` | nonzero return code `!=` to `==` | survived | killed |
| G02 | `_git` | omit output trim | survived | killed |
| G03 | `_git` | successful return to `None` | survived | killed |
| G04 | `_git` | exception return to `False` | survived | killed |
| G05 | `_git` | failed command return to `False` | survived | killed |
| G06 | `_git` | remove Git working directory | survived | killed |
| G07 | `_git` | disable captured output | timeout | killed |
| G08 | `_git` | remove exception return | survived | killed |
| G09 | `_git` | negate return-code condition | survived | killed |
| H01 | `source_hash` | invert directory exclusion | survived | killed |
| H02 | `source_hash` | invert suffix selection | survived | killed |
| H03 | `source_hash` | accept only `.py` | survived | killed |
| H04 | `source_hash` | omit directory sorting | survived | killed |
| H05 | `source_hash` | omit filename sorting | survived | killed |
| H06 | `source_hash` | hash absolute paths | survived | killed |
| H07 | `source_hash` | omit chunk digest update | survived | killed |
| H08 | `source_hash` | continue after read error | survived | killed |
| H09 | `source_hash` | omit manifest entry | survived | killed |
| H10 | `source_hash` | change path/hash separator | survived | killed |
| H11 | `source_hash` | remove manifest newline | survived | killed |
| H12 | `source_hash` | increase file count by one | survived | killed |
| H13 | `source_hash` | return `None` hash | killed | killed |
| H14 | `source_hash` | read 65,537 byte chunks | survived | equivalent |
| H15 | `source_hash` | hash empty manifest | survived | killed |
| H16 | `source_hash` | globally sort manifest entries | survived | defect |
| C01 | `collect` | invert Git availability | survived | killed |
| C02 | `collect` | force clean tree | survived | killed |
| C03 | `collect` | count whitespace as dirty | survived | killed |
| C04 | `collect` | include blank status lines | survived | killed |
| C05 | `collect` | force zero dirty files | survived | killed |
| C06 | `collect` | return `None` file count | survived | killed |
| C07 | `collect` | return `None` source hash | killed | killed |
| C08 | `collect` | return `None` repo root | survived | killed |
| C09 | `collect` | return `None` Git commit | survived | killed |
| C10 | `collect` | omit Git status call | survived | killed |
| C11 | `collect` | omit source hash computation | killed | killed |

## New tests

| Test in `tests/test_provenance_mutation.py` | Witness mutant |
| --- | --- |
| `GitProvenanceTests.test_git_returns_trimmed_stdout_with_safe_invocation` | G02 |
| `GitProvenanceTests.test_git_failure_and_process_errors_are_unknown` | G05 |
| `SourceHashTests.test_hashes_walk_order_relative_paths_and_allowed_suffixes` | H01 |
| `SourceHashTests.test_skips_unreadable_files` | H08 |
| `SourceHashTests.test_content_changes_hash_and_empty_tree_has_zero_files` | H07 |
| `SourceHashTests.test_hash_uses_global_relative_path_order` (expected failure at sweep time) | H16 |
| `CollectProvenanceTests.test_collect_reports_clean_and_dirty_state_exactly` | C02 |
| `CollectProvenanceTests.test_collect_preserves_unknown_git_state` | C01 |

At sweep time, every normal test passed on the original and failed on its
witness mutant. The expected-failure test failed as expected on the original
and succeeded unexpectedly on H16.

## Survivors left

- **H14, equivalent:** SHA-256 receives the same byte sequence whether the
  file is read in 65,536 or 65,537 byte chunks. No behavior test can separate it.
- **H16, historical defect:** The mutation implemented the formerly documented
  global sort. The documentation now states the actual top-down walk order.
- **Untestable offline:** none.

## Defects found

- At sweep time, `source_hash` documented a globally sorted relative-path
  manifest, while root files preceded nested files in `os.walk` preorder.
  The docstring now describes the actual order, and
  `test_hash_uses_top_down_walk_order` passes on that order. Production
  behavior and fingerprints are unchanged.

The mutation operators used were comparison flips, condition negation,
constant changes, empty/false return replacement, and statement removal.
There are no `and`/`or` conditions, `+`/`-` expressions suitable for swapping,
or explicit `raise` statements in this module. Comments, logging, and
docstrings were not mutated.

## Verification

- `python3 tests/test_suite.py`: 593 passed, 0 failed, 5 existing
  environment skips.
- `python3 -m unittest discover -s tests -p test_provenance_mutation.py -v`:
  7 passed and 1 expected failure.
- `python3 tests/test_onboard.py`: 28 passed.
- `test_local_worker.py` was exercised separately, but its localhost server
  could not bind in this sandbox (`PermissionError: Operation not permitted`).
