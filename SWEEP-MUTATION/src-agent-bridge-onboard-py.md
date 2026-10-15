# Mutation sweep: `src/agent_bridge/onboard.py`

Source SHA-256: `2c7f7198534bbd9947538ad8c4c20aad2c2a41814e91cf4b1d37d1a741d5100e`. The source was compiled into an in-memory replacement module for each run; the source file was never rewritten.

## Method and result

- Generated **80** one-change AST mutants across **46** top-level functions (cap: 150). Operators included comparison flips, `and`/`or` swaps, condition negation, numeric increment, return replacement (`None`, empty string, `False`), statement removal, `+`/`-` swap, and dropped raises. AST selection excluded comments and docstrings; print/logging statements were excluded.
- Each mutant ran in a separate Python process with an isolated temporary `HOME` and `CODEX_HOME`, a 12-second limit, and the offline onboarding-related tests. No mutant timed out or failed to load. The module source was byte-identical before and after the sweep.
- Before: **53/80 killed (66.2%)** by 199 existing tests.
- After: **80/80 killed (100.0%)** by the same tests plus 23 new tests.
- Survivors left: **0**. Equivalent: **0**. Untestable offline: **0**. Defects found: **none**.

## Mutants

| ID | Function | Line | Change | Before | After |
| --- | --- | ---: | --- | --- | --- |
| M001 | `default_answers_path` | 37 | `return_none` | survived | killed |
| M002 | `_sha` | 42 | `return_none` | survived | killed |
| M003 | `_loopback_endpoint` | 50 | `return_none` | killed | killed |
| M004 | `validate_answers` | 167 | `return_none` | killed | killed |
| M005 | `validate_answers` | 58 | `drop_raise` | survived | killed |
| M006 | `validate_answers` | 57 | `flip_compare` | killed | killed |
| M007 | `validate_answers` | 57 | `swap_bool` | survived | killed |
| M008 | `validate_answers` | 59 | `swap_arithmetic` | killed | killed |
| M009 | `validate_answers` | 61 | `remove_statement` | killed | killed |
| M010 | `privacy_overlay` | 177 | `return_none` | killed | killed |
| M011 | `_choice` | 197 | `return_none` | killed | killed |
| M012 | `delegation_platform_blocker` | 237 | `return_none` | killed | killed |
| M013 | `questionnaire` | 303 | `return_none` | survived | killed |
| M014 | `load_answers` | 310 | `return_none` | survived | killed |
| M015 | `_peer_pin` | 326 | `return_none` | killed | killed |
| M016 | `stage` | 346 | `return_none` | killed | killed |
| M017 | `_portable_python` | 353 | `return_none` | killed | killed |
| M018 | `_command` | 357 | `return_none` | killed | killed |
| M019 | `_local_command` | 366 | `return_none` | killed | killed |
| M020 | `_windows_preflight_report` | 388 | `return_none` | killed | killed |
| M021 | `_windows_setup_summary` | 461 | `return_none` | killed | killed |
| M022 | `_windows_delegation_state` | 482 | `return_none` | killed | killed |
| M023 | `_windows_preflight_summary` | 559 | `return_none` | killed | killed |
| M024 | `_orchestration_command` | 572 | `return_none` | killed | killed |
| M025 | `plan` | 649 | `return_none` | killed | killed |
| M026 | `status` | 679 | `return_none` | killed | killed |
| M027 | `_bytes` | 688 | `return_none` | killed | killed |
| M028 | `_backup` | 697 | `return_none` | survived | killed |
| M029 | `_commit_updates` | 733 | `return_none` | survived | killed |
| M030 | `_commit_updates` | 716 | `drop_raise` | survived | killed |
| M031 | `_commit_updates` | 715 | `flip_compare` | killed | killed |
| M032 | `_commit_updates` | 717 | `remove_statement` | killed | killed |
| M033 | `_owned_file_update` | 750 | `return_none` | killed | killed |
| M034 | `_read_text` | 755 | `return_none` | killed | killed |
| M035 | `_json_registration_update` | 778 | `return_none` | killed | killed |
| M036 | `_toml_block` | 782 | `return_none` | killed | killed |
| M037 | `_toml_registration_update` | 814 | `return_none` | killed | killed |
| M038 | `_toml_registration_update` | 793 | `drop_raise` | survived | killed |
| M039 | `_toml_registration_update` | 792 | `flip_compare` | killed | killed |
| M040 | `_toml_registration_update` | 792 | `swap_bool` | survived | killed |
| M041 | `_toml_registration_update` | 792 | `increment_number` | survived | killed |
| M042 | `_toml_registration_update` | 801 | `swap_arithmetic` | killed | killed |
| M043 | `_toml_registration_update` | 789 | `remove_statement` | killed | killed |
| M044 | `_managed_text_update` | 833 | `return_none` | killed | killed |
| M045 | `_managed_text_update` | 821 | `drop_raise` | survived | killed |
| M046 | `_managed_text_update` | 820 | `flip_compare` | killed | killed |
| M047 | `_managed_text_update` | 820 | `swap_bool` | survived | killed |
| M048 | `_managed_text_update` | 820 | `increment_number` | survived | killed |
| M049 | `_managed_text_update` | 819 | `swap_arithmetic` | killed | killed |
| M050 | `_managed_text_update` | 818 | `remove_statement` | killed | killed |
| M051 | `_client_instruction` | 848 | `return_none` | killed | killed |
| M052 | `_shared_instructions` | 882 | `return_none` | killed | killed |
| M053 | `_same_directory` | 895 | `return_none` | survived | killed |
| M054 | `_paths` | 903 | `return_none` | killed | killed |
| M055 | `_host_owned_paths` | 922 | `return_none` | killed | killed |
| M056 | `_looks_managed_command` | 927 | `return_none` | survived | killed |
| M057 | `_refuse_stale_registrations` | 937 | `negate_if` | killed | killed |
| M058 | `_apply` | 953 | `negate_if` | killed | killed |
| M059 | `_apply` | 954 | `drop_raise` | survived | killed |
| M060 | `_apply` | 953 | `flip_compare` | killed | killed |
| M061 | `_apply` | 960 | `swap_bool` | survived | killed |
| M062 | `_apply` | 1095 | `increment_number` | killed | killed |
| M063 | `_apply` | 956 | `swap_arithmetic` | killed | killed |
| M064 | `_apply` | 955 | `remove_statement` | survived | killed |
| M065 | `_routing_policy_scaffold` | 1263 | `return_none` | killed | killed |
| M066 | `_install_gate` | 1321 | `return_none` | survived | killed |
| M067 | `_install_gate` | 1300 | `swap_bool` | survived | killed |
| M068 | `_install_gate` | 1278 | `remove_statement` | killed | killed |
| M069 | `apply` | 1341 | `swap_bool` | survived | killed |
| M070 | `_remove_json_registration` | 1358 | `return_none` | killed | killed |
| M071 | `_remove_toml_registration` | 1372 | `return_none` | killed | killed |
| M072 | `_remove_managed_text` | 1385 | `return_none` | survived | killed |
| M073 | `uninstall` | 1470 | `return_none` | killed | killed |
| M074 | `uninstall` | 1456 | `flip_compare` | killed | killed |
| M075 | `uninstall` | 1392 | `swap_bool` | killed | killed |
| M076 | `uninstall` | 1470 | `swap_arithmetic` | killed | killed |
| M077 | `uninstall` | 1392 | `remove_statement` | survived | killed |
| M078 | `main` | 1524 | `return_none` | survived | killed |
| M079 | `_shared_instructions` | 882 | `return_empty` | killed | killed |
| M080 | `_same_directory` | 895 | `return_false` | survived | killed |

## New tests

All tests below pass on the original source. Each fails on at least one recorded mutant.

### `default_answers_path`

- `AnswersMutationTests.test_default_answers_path_uses_supplied_home` — kills M001.

### `_sha`

- `AnswersMutationTests.test_sha_is_canonical_sha256` — kills M002.

### `validate_answers`

- `AnswersMutationTests.test_validate_answers_rejects_wrong_and_missing_version` — kills M005, M006, M007.

### `questionnaire`

- `AnswersMutationTests.test_questionnaire_returns_validated_choices` — kills M004, M006, M008, M009, M011, M013.

### `load_answers`

- `AnswersMutationTests.test_load_answers_returns_validated_document` — kills M006, M008, M009, M014.

### `_backup`

- `CommitMutationTests.test_backup_returns_a_copy_path` — kills M027, M028.

### `_commit_updates`

- `CommitMutationTests.test_commit_returns_backup_paths` — kills M027, M028, M029, M031, M032.
- `CommitMutationTests.test_concurrent_edit_creates_no_backup` — kills M030, M031.

### `_toml_registration_update`

- `ManagedMarkerMutationTests.test_toml_rejects_orphan_end_marker` — kills M038, M039, M040, M043.
- `ManagedMarkerMutationTests.test_toml_rejects_duplicate_marker_pairs_as_damage` — kills M038, M040, M041, M043.

### `_managed_text_update`

- `ManagedMarkerMutationTests.test_managed_text_rejects_orphan_end_marker` — kills M045, M046, M047, M049, M050.
- `ManagedMarkerMutationTests.test_managed_text_rejects_duplicate_marker_pairs_as_damage` — kills M045, M047, M048, M049, M050.

### `_remove_managed_text`

- `ManagedMarkerMutationTests.test_remove_managed_text_returns_new_bytes` — kills M034, M046, M049, M050, M072.

### `_same_directory`

- `PathAndApplyMutationTests.test_same_directory_returns_boolean_for_equal_paths` — kills M053, M080.

### `_looks_managed_command`

- `PathAndApplyMutationTests.test_unmanaged_command_returns_false` — kills M056.

### `_apply`

- `PathAndApplyMutationTests.test_apply_rejects_other_checkout_before_reading_plan` — kills M058, M059, M060.
- `PathAndApplyMutationTests.test_apply_rejects_mismatched_answers_before_candidate_inspection` — kills M058, M060, M061, M063.
- `PathAndApplyMutationTests.test_apply_canonicalizes_checkout_root_for_registrations` — kills M004, M006, M008, M009, M010, M017, M018, M031, M032, M034, M035, M036, M039, M043, M046, M049, M050, M051, M052, M054, M055, M057, M058, M060, M063, M064.

### `apply`

- `PathAndApplyMutationTests.test_apply_lock_uses_explicit_home` — kills M069.

### `_install_gate`

- `GateAndUninstallMutationTests.test_install_gate_returns_failure_report` — kills M066.
- `GateAndUninstallMutationTests.test_install_gate_handles_non_object_existing_policy` — kills M067, M068.

### `uninstall`

- `GateAndUninstallMutationTests.test_uninstall_defaults_to_isolated_home` — kills M004, M006, M008, M009, M034, M054, M073, M075, M076, M077.

### `main`

- `GateAndUninstallMutationTests.test_main_reports_missing_answers_as_failure` — kills M014, M078.

## Defects found

None.

## Scope and limits

- Subprocesses started by some existing tests load the original module; the mutant was exercised by the in-process tests. The new focused tests call the mutant module directly.
- The sweep samples 80 mutations; it is evidence for these mistakes, not exhaustive proof of all possible mistakes.

## Verification

- `python3 tests/test_suite.py` with an isolated temporary home: 592 passed, 0 failed, 5 environmental skips.
- `python3 -m unittest test_onboard test_onboard_mutation -q` with an isolated temporary home: 51 passed.
- `test_local_worker.py` was attempted with an isolated temporary home, but all 14 tests failed during fixture setup because this sandbox denied binding `127.0.0.1`; no HTTP request was made.
