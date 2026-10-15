# Mutation test: `windows_rootfs.py`

Source SHA-256 before and after: `4520460f4ec6d74eef889b291643162a30b3e0f69cc6cbeb1dd8b8cbf4047b80`. The production module was loaded from a temporary mutant file in each child process; its worktree bytes were never replaced.

## Method and result

The runner generated one AST guided change at a time within each function, skipped comments and docstrings, compiled the mutant, and ran `tests/test_windows_rootfs.py` offline in a fresh subprocess with a five second timeout. The focused baseline had 97 passing tests; after additions it has 110. All 24 functions received at least one mutant. There were no timeouts.

| Stage | Killed | Generated | Kill rate |
| --- | ---: | ---: | ---: |
| Before | 126 | 150 | 84.0% |
| After | 146 | 150 | 97.3% |

## New tests

All tests below are in `tests/test_windows_rootfs.py` under `MutationGapTests`. Each passes on the original and was also run individually against the first listed mutant, where it failed.

| Test | Mutants killed |
| --- | --- |
| `test_is_sha256_hex_rejects_non_strings` | M001, M025 |
| `test_validate_recipe_rejects_an_empty_base_image` | M079, M121 |
| `test_base_reference_validates_the_recipe` | M006 |
| `test_build_argv_validates_the_recipe` | M008 |
| `test_export_argv_rejects_an_unknown_engine` | M033 |
| `test_output_names_validates_the_recipe` | M010 |
| `test_normalise_member_clears_pax_headers` | M099 |
| `test_normalise_member_preserves_execute_permission` | M134 |
| `test_sha256_path_hashes_the_whole_file` | M055, M071, M086, M100 |
| `test_build_manifest_validates_the_recipe` | M015 |
| `test_build_sidecar_validates_the_recipe_and_runner_hash` | M016, M088 |
| `test_build_versions_file_validates_recipe_and_tool_hash` | M017, M149 |
| `test_verify_manifest_trust_rejects_unknown_architecture` | M105 |

## Survivors left

- M007: Equivalent: dockerfile calls base_reference, which validates the same recipe before returning.
- M029: Equivalent: split("=", 1)[0] and split("=", 2)[0] return the same package name.
- M120: Equivalent: replacement is literally the existing return None.
- M126: Equivalent for constructed manifests: validate_recipe and the rootfs hash check establish all constraints parse_manifest rechecks; the produced key set and schema version are fixed.

## Defects found

None.

## Verification

`python3 tests/test_suite.py` passed with 592 checks passed, 0 failed and 5 environmental skips. The focused test file passed all 110 tests. `tests/test_onboard.py`, `tests/test_hostenv.py`, `tests/test_automatic_gate.py` and `tests/test_delegation_audit.py` also passed. Optional `tests/test_local_worker.py` and `tests/test_automatic_delegation_e2e.py` could not bind their loopback test servers in this sandbox (`PermissionError`); no external service was contacted.

## Generated mutants

Each row identifies one single change. `K` means killed; `S` means survived.

| ID | Function | Line | Mutation | Before | After |
| --- | --- | ---: | --- | :---: | :---: |
| M001 | `is_sha256_hex` | 76 | replace return | S | K |
| M002 | `validate_recipe` | 134 | negate condition | K | K |
| M003 | `_require_download_integrity` | 174 | negate condition | K | K |
| M004 | `_reject_placeholders` | 211 | negate condition | K | K |
| M005 | `_require_runtime_packages` | 222 | remove statement | K | K |
| M006 | `base_reference` | 232 | remove statement | S | K |
| M007 | `dockerfile` | 251 | remove statement | S | S |
| M008 | `build_argv` | 321 | remove statement | S | K |
| M009 | `export_argv` | 348 | negate condition | K | K |
| M010 | `output_names` | 360 | remove statement | S | K |
| M011 | `normalise_member` | 383 | remove statement | K | K |
| M012 | `normalised_members` | 396 | replace return | K | K |
| M013 | `normalise_tar` | 411 | remove statement | K | K |
| M014 | `sha256_path` | 420 | remove statement | K | K |
| M015 | `build_manifest` | 443 | remove statement | S | K |
| M016 | `build_sidecar` | 471 | remove statement | S | K |
| M017 | `build_versions_file` | 515 | remove statement | S | K |
| M018 | `verify_image` | 553 | remove statement | K | K |
| M019 | `canonical_manifest_bytes` | 585 | replace return | K | K |
| M020 | `manifest_digest` | 592 | replace return | K | K |
| M021 | `signed` | 627 | replace return | K | K |
| M022 | `verify_manifest_trust` | 657 | remove statement | K | K |
| M023 | `release_blockers` | 712 | remove statement | K | K |
| M024 | `host_architecture_for` | 741 | remove statement | K | K |
| M025 | `is_sha256_hex` | 76 | boolean swap | S | K |
| M026 | `validate_recipe` | 135 | drop raise | K | K |
| M027 | `_require_download_integrity` | 174 | remove negation | K | K |
| M028 | `_reject_placeholders` | 211 | comparison | K | K |
| M029 | `_require_runtime_packages` | 222 | numeric +1 | S | S |
| M030 | `base_reference` | 233 | replace return | K | K |
| M031 | `dockerfile` | 252 | negate condition | K | K |
| M032 | `build_argv` | 322 | negate condition | K | K |
| M033 | `export_argv` | 349 | drop raise | S | K |
| M034 | `output_names` | 361 | replace return | K | K |
| M035 | `normalise_member` | 384 | remove statement | K | K |
| M036 | `normalise_tar` | 414 | remove statement | K | K |
| M037 | `sha256_path` | 423 | remove statement | K | K |
| M038 | `build_manifest` | 444 | negate condition | K | K |
| M039 | `build_sidecar` | 472 | negate condition | K | K |
| M040 | `build_versions_file` | 516 | remove statement | K | K |
| M041 | `verify_image` | 555 | replace return | K | K |
| M042 | `signed` | 627 | boolean swap | K | K |
| M043 | `verify_manifest_trust` | 658 | negate condition | K | K |
| M044 | `release_blockers` | 713 | negate condition | K | K |
| M045 | `host_architecture_for` | 741 | boolean swap | K | K |
| M046 | `validate_recipe` | 135 | arithmetic swap | K | K |
| M047 | `_require_download_integrity` | 174 | boolean swap | K | K |
| M048 | `_reject_placeholders` | 212 | drop raise | K | K |
| M049 | `_require_runtime_packages` | 222 | numeric +1 | K | K |
| M050 | `dockerfile` | 252 | remove negation | K | K |
| M051 | `build_argv` | 323 | drop raise | K | K |
| M052 | `export_argv` | 350 | replace return | K | K |
| M053 | `normalise_member` | 385 | remove statement | K | K |
| M054 | `normalise_tar` | 415 | remove statement | K | K |
| M055 | `sha256_path` | 424 | negate condition | S | K |
| M056 | `build_manifest` | 444 | remove negation | K | K |
| M057 | `build_sidecar` | 472 | remove negation | K | K |
| M058 | `build_versions_file` | 517 | negate condition | K | K |
| M059 | `verify_image` | 556 | negate condition | K | K |
| M060 | `verify_manifest_trust` | 658 | remove negation | K | K |
| M061 | `release_blockers` | 713 | remove negation | K | K |
| M062 | `host_architecture_for` | 742 | negate condition | K | K |
| M063 | `validate_recipe` | 136 | negate condition | K | K |
| M064 | `_require_download_integrity` | 175 | drop raise | K | K |
| M065 | `_reject_placeholders` | 216 | negate condition | K | K |
| M066 | `_require_runtime_packages` | 223 | remove statement | K | K |
| M067 | `dockerfile` | 252 | boolean swap | K | K |
| M068 | `build_argv` | 324 | negate condition | K | K |
| M069 | `normalise_member` | 386 | remove statement | K | K |
| M070 | `normalise_tar` | 416 | replace return | K | K |
| M071 | `sha256_path` | 424 | remove negation | S | K |
| M072 | `build_manifest` | 444 | boolean swap | K | K |
| M073 | `build_sidecar` | 472 | boolean swap | K | K |
| M074 | `build_versions_file` | 517 | comparison | K | K |
| M075 | `verify_image` | 556 | remove negation | K | K |
| M076 | `verify_manifest_trust` | 659 | replace return | K | K |
| M077 | `release_blockers` | 714 | remove statement | K | K |
| M078 | `host_architecture_for` | 743 | replace return | K | K |
| M079 | `validate_recipe` | 136 | boolean swap | S | K |
| M080 | `_require_download_integrity` | 182 | remove statement | K | K |
| M081 | `_reject_placeholders` | 217 | drop raise | K | K |
| M082 | `_require_runtime_packages` | 224 | negate condition | K | K |
| M083 | `dockerfile` | 253 | drop raise | K | K |
| M084 | `build_argv` | 324 | boolean swap | K | K |
| M085 | `normalise_member` | 387 | remove statement | K | K |
| M086 | `sha256_path` | 426 | remove statement | S | K |
| M087 | `build_manifest` | 445 | drop raise | K | K |
| M088 | `build_sidecar` | 473 | drop raise | S | K |
| M089 | `build_versions_file` | 518 | drop raise | K | K |
| M090 | `verify_image` | 556 | boolean swap | K | K |
| M091 | `verify_manifest_trust` | 660 | negate condition | K | K |
| M092 | `release_blockers` | 716 | remove statement | K | K |
| M093 | `host_architecture_for` | 744 | negate condition | K | K |
| M094 | `validate_recipe` | 136 | remove negation | K | K |
| M095 | `_require_download_integrity` | 183 | negate condition | K | K |
| M096 | `_require_runtime_packages` | 225 | drop raise | K | K |
| M097 | `dockerfile` | 258 | remove statement | K | K |
| M098 | `build_argv` | 324 | remove negation | K | K |
| M099 | `normalise_member` | 388 | remove statement | S | K |
| M100 | `sha256_path` | 427 | replace return | S | K |
| M101 | `build_manifest` | 446 | remove statement | K | K |
| M102 | `build_sidecar` | 475 | negate condition | K | K |
| M103 | `build_versions_file` | 518 | arithmetic swap | K | K |
| M104 | `verify_image` | 557 | replace return | K | K |
| M105 | `verify_manifest_trust` | 661 | replace return | S | K |
| M106 | `release_blockers` | 718 | negate condition | K | K |
| M107 | `host_architecture_for` | 745 | replace return | K | K |
| M108 | `validate_recipe` | 136 | remove negation | K | K |
| M109 | `_require_download_integrity` | 183 | remove negation | K | K |
| M110 | `_require_runtime_packages` | 226 | arithmetic swap | K | K |
| M111 | `dockerfile` | 259 | remove statement | K | K |
| M112 | `build_argv` | 325 | drop raise | K | K |
| M113 | `normalise_member` | 389 | remove statement | K | K |
| M114 | `build_manifest` | 447 | numeric +1 | K | K |
| M115 | `build_sidecar` | 475 | remove negation | K | K |
| M116 | `build_versions_file` | 519 | remove statement | K | K |
| M117 | `verify_image` | 558 | negate condition | K | K |
| M118 | `verify_manifest_trust` | 662 | remove statement | K | K |
| M119 | `release_blockers` | 719 | remove statement | K | K |
| M120 | `host_architecture_for` | 746 | replace return | S | S |
| M121 | `validate_recipe` | 137 | drop raise | S | K |
| M122 | `_require_download_integrity` | 183 | boolean swap | K | K |
| M123 | `dockerfile` | 264 | replace return | K | K |
| M124 | `build_argv` | 326 | replace return | K | K |
| M125 | `normalise_member` | 389 | numeric +1 | K | K |
| M126 | `build_manifest` | 455 | remove statement | S | S |
| M127 | `build_sidecar` | 475 | boolean swap | K | K |
| M128 | `build_versions_file` | 521 | negate condition | K | K |
| M129 | `verify_image` | 558 | comparison | K | K |
| M130 | `verify_manifest_trust` | 663 | boolean swap | K | K |
| M131 | `release_blockers` | 721 | negate condition | K | K |
| M132 | `validate_recipe` | 138 | negate condition | K | K |
| M133 | `_require_download_integrity` | 184 | drop raise | K | K |
| M134 | `normalise_member` | 389 | numeric +1 | S | K |
| M135 | `build_manifest` | 456 | replace return | K | K |
| M136 | `build_sidecar` | 476 | drop raise | K | K |
| M137 | `build_versions_file` | 521 | remove negation | K | K |
| M138 | `verify_image` | 559 | replace return | K | K |
| M139 | `verify_manifest_trust` | 663 | comparison | K | K |
| M140 | `release_blockers` | 721 | remove negation | K | K |
| M141 | `validate_recipe` | 138 | boolean swap | K | K |
| M142 | `normalise_member` | 390 | replace return | K | K |
| M143 | `build_sidecar` | 477 | replace return | K | K |
| M144 | `build_versions_file` | 521 | boolean swap | K | K |
| M145 | `verify_image` | 560 | negate condition | K | K |
| M146 | `verify_manifest_trust` | 664 | boolean swap | K | K |
| M147 | `release_blockers` | 722 | remove statement | K | K |
| M148 | `validate_recipe` | 141 | drop raise | K | K |
| M149 | `build_versions_file` | 522 | drop raise | S | K |
| M150 | `verify_image` | 561 | remove statement | K | K |
