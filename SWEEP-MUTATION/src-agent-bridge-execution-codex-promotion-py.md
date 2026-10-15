# Mutation sweep: `src/agent_bridge/execution/codex_promotion.py`

## Method and result

The source was read in full. Each AST mutant was compiled into an imported module in a fresh Python process; no production file was edited. The offline `tests/test_codex_promotion.py` suite ran once per mutant with an eight-second limit. The pre-change suite had 53 tests; the expanded suite has 83. The selected 150 mutants cover all 24 functions and methods. Comments, docstrings, and logging calls were excluded. Mutations include comparison and Boolean changes, condition negation, integer increments, return replacement with None, an empty dict, or False, arithmetic swaps, raise removal, and deletion of five ordinary statements.

| Measure | Before new tests | After new tests |
| --- | ---: | ---: |
| Killed | 109/150 (72.7%) | 150/150 (100.0%) |
| Survived | 41 | 0 |

Source SHA-256 before and after: `8502dd70d03c3e65fc99b4e3476a6ad7b81aeed0b0a5d8a2dfbc4b3da337e755`. The final bytes match `HEAD:src/agent_bridge/execution/codex_promotion.py`.

## Survivors left

None. Equivalent: none among the selected mutants. Untestable offline: none. Defects found: none. All 41 first-pass survivors are killed by the added tests.

## New test evidence

Each test below passed on the original source and failed when its listed mutant ran alone. Tests are grouped by the function containing that mutant.

### `_check_gate`

- `Admission.test_gate_transaction_id_is_bounded_in_refusal` — M103

### `_file_sha256`

- `DigestDetects.test_file_hash_fallback_flags_remain_read_only` — M011
- `DigestDetects.test_file_hash_refuses_an_inode_changed_since_stat` — M063

### `_is_hex64`

- `DigestVector.test_hex_digest_requires_length_and_hex_digits` — M036

### `_owned_private`

- `StateFileBoundaries.test_ownership_check_refuses_another_uid` — M066

### `_read_state_json`

- `StateFileBoundaries.test_state_reader_accepts_exact_size_limit` — M110
- `StateFileBoundaries.test_state_reader_fallback_flags_remain_read_only` — M015
- `StateFileBoundaries.test_state_reader_refuses_growth_during_read` — M132
- `StateFileBoundaries.test_state_reader_refuses_oversized_stat_even_if_read_is_short` — M115
- `StateFileBoundaries.test_state_reader_requires_a_regular_file` — M082
- `StateFileBoundaries.test_state_reader_requires_private_permissions` — M148

### `_reject_floats`

- `DigestVector.test_canonical_bytes_rejects_floats_and_non_string_keys` — M030

### `_slot_package_version`

- `Admission.test_malformed_slot_package_is_refused` — M106
- `Admission.test_missing_slot_package_is_refused` — M021
- `Admission.test_slot_package_accepts_exact_size_limit` — M072
- `Admission.test_symlinked_slot_package_is_refused` — M057

### `_verify_node_file`

- `Admission.test_missing_recorded_node_is_refused` — M022

### `_verify_record`

- `Admission.test_record_with_an_unknown_promoter_is_refused` — M141
- `Admission.test_record_with_another_schema_version_is_refused` — M116

### `_verify_task_node`

- `Admission.test_promoted_task_requires_a_node_on_path` — M042

### `account_home`

- `Admission.test_account_home_fallback_uses_path_home` — M026

### `admit`

- `Admission.test_absent_file_lock_support_is_refused` — M089
- `Admission.test_existing_lock_is_opened_read_only` — M139
- `Admission.test_fresh_directory_and_lock_modes_ignore_umask` — M099
- `Admission.test_lock_fallback_flag_remains_read_only` — M135
- `Admission.test_parent_creation_error_is_wrapped` — M107
- `Admission.test_promotion_directory_creation_error_is_wrapped` — M118
- `Admission.test_promotion_directory_must_be_a_directory` — M127
- `Admission.test_relative_codex_path_is_refused` — M060

### `tree_digest`

- `DigestDetects.test_file_root_is_refused_as_a_slot` — M033

## Mutant ledger

| ID | Function:line | Change | Before | After |
| --- | --- | --- | --- | --- |
| M001 | `receipt:111` | return → None | killed | killed |
| M002 | `check_reported_version:121` | negate → condition | killed | killed |
| M003 | `managed_home:127` | return → None | killed | killed |
| M004 | `account_home:131` | negate → condition | killed | killed |
| M005 | `default_promotion_dir:153` | bool → Or | killed | killed |
| M006 | `_check_home:160` | negate → condition | killed | killed |
| M007 | `canonical_bytes:169` | return → None | killed | killed |
| M008 | `record_id_of:172` | return → None | killed | killed |
| M009 | `_reject_floats:175` | negate → condition | killed | killed |
| M010 | `_digest_mode:186` | return → None | killed | killed |
| M011 | `_file_sha256:189` | number → 1 | survived | killed |
| M012 | `walk:224` | negate → condition | killed | killed |
| M013 | `tree_digest:238` | negate → condition | killed | killed |
| M014 | `_owned_private:249` | negate → condition | killed | killed |
| M015 | `_read_state_json:253` | number → 1 | survived | killed |
| M016 | `_float_refused:271` | drop_raise → pass | killed | killed |
| M017 | `_is_hex64:274` | return → None | killed | killed |
| M018 | `_check_gate:283` | drop_raise → pass | killed | killed |
| M019 | `_verify_record:292` | return → None | killed | killed |
| M020 | `_check_launcher_set:342` | negate → condition | killed | killed |
| M021 | `_slot_package_version:357` | drop_raise → pass | survived | killed |
| M022 | `_verify_node_file:367` | drop_raise → pass | survived | killed |
| M023 | `_verify_task_node:376` | negate → condition | killed | killed |
| M024 | `admit:389` | bool → And | killed | killed |
| M025 | `check_reported_version:121` | bool → Or | killed | killed |
| M026 | `account_home:131` | return → None | survived | killed |
| M027 | `default_promotion_dir:154` | return → None | killed | killed |
| M028 | `_check_home:160` | compare → Eq | killed | killed |
| M029 | `record_id_of:172` | compare → Eq | killed | killed |
| M030 | `_reject_floats:175` | drop_raise → pass | survived | killed |
| M031 | `_file_sha256:189` | number → 1 | survived | killed |
| M032 | `walk:226` | negate → condition | killed | killed |
| M033 | `tree_digest:238` | drop_raise → pass | survived | killed |
| M034 | `_owned_private:249` | bool → Or | killed | killed |
| M035 | `_read_state_json:253` | number → 1 | survived | killed |
| M036 | `_is_hex64:274` | bool → Or | survived | killed |
| M037 | `_check_gate:284` | negate → condition | killed | killed |
| M038 | `_verify_record:294` | negate → condition | killed | killed |
| M039 | `_check_launcher_set:342` | bool → And | killed | killed |
| M040 | `_slot_package_version:358` | negate → condition | killed | killed |
| M041 | `_verify_node_file:368` | negate → condition | killed | killed |
| M042 | `_verify_task_node:376` | drop_raise → pass | survived | killed |
| M043 | `admit:390` | negate → condition | killed | killed |
| M044 | `check_reported_version:121` | compare → NotEq | killed | killed |
| M045 | `account_home:132` | return → None | killed | killed |
| M046 | `_check_home:161` | drop_raise → pass | killed | killed |
| M047 | `_reject_floats:176` | negate → condition | survived | killed |
| M048 | `_file_sha256:193` | negate → condition | killed | killed |
| M049 | `walk:226` | drop_raise → pass | killed | killed |
| M050 | `tree_digest:240` | number → 2 | killed | killed |
| M051 | `_owned_private:249` | compare → Eq | killed | killed |
| M052 | `_read_state_json:255` | drop_raise → pass | killed | killed |
| M053 | `_is_hex64:274` | compare → NotEq | killed | killed |
| M054 | `_check_gate:284` | bool → And | killed | killed |
| M055 | `_verify_record:294` | bool → And | killed | killed |
| M056 | `_check_launcher_set:342` | compare → Eq | killed | killed |
| M057 | `_slot_package_version:358` | bool → And | survived | killed |
| M058 | `_verify_node_file:368` | bool → And | killed | killed |
| M059 | `_verify_task_node:377` | negate → condition | killed | killed |
| M060 | `admit:390` | drop_raise → pass | survived | killed |
| M061 | `check_reported_version:121` | compare → Eq | killed | killed |
| M062 | `_reject_floats:178` | negate → condition | killed | killed |
| M063 | `_file_sha256:193` | bool → And | survived | killed |
| M064 | `walk:228` | negate → condition | killed | killed |
| M065 | `tree_digest:242` | arithmetic → Sub | killed | killed |
| M066 | `_owned_private:249` | drop_raise → pass | survived | killed |
| M067 | `_read_state_json:258` | negate → condition | killed | killed |
| M068 | `_is_hex64:274` | number → 65 | killed | killed |
| M069 | `_check_gate:284` | compare → Eq | killed | killed |
| M070 | `_verify_record:294` | compare → Eq | killed | killed |
| M071 | `_check_launcher_set:342` | compare → Eq | killed | killed |
| M072 | `_slot_package_version:358` | compare → GtE | survived | killed |
| M073 | `_verify_node_file:368` | compare → Eq | killed | killed |
| M074 | `_verify_task_node:377` | compare → Eq | killed | killed |
| M075 | `admit:391` | negate → condition | killed | killed |
| M076 | `check_reported_version:122` | drop_raise → pass | killed | killed |
| M077 | `_reject_floats:178` | drop_raise → pass | survived | killed |
| M078 | `_file_sha256:193` | compare → Eq | killed | killed |
| M079 | `walk:228` | bool → And | killed | killed |
| M080 | `tree_digest:243` | return → None | killed | killed |
| M081 | `_owned_private:250` | negate → condition | killed | killed |
| M082 | `_read_state_json:258` | drop_raise → pass | survived | killed |
| M083 | `_check_gate:285` | drop_raise → pass | killed | killed |
| M084 | `_verify_record:294` | drop_raise → pass | killed | killed |
| M085 | `_check_launcher_set:343` | drop_raise → pass | killed | killed |
| M086 | `_slot_package_version:359` | drop_raise → pass | survived | killed |
| M087 | `_verify_node_file:369` | drop_raise → pass | killed | killed |
| M088 | `_verify_task_node:378` | drop_raise → pass | killed | killed |
| M089 | `admit:391` | drop_raise → pass | survived | killed |
| M090 | `_reject_floats:180` | negate → condition | killed | killed |
| M091 | `_file_sha256:194` | drop_raise → pass | survived | killed |
| M092 | `walk:228` | compare → Eq | killed | killed |
| M093 | `_owned_private:250` | drop_raise → pass | killed | killed |
| M094 | `_read_state_json:259` | number → 64 | survived | killed |
| M095 | `_check_gate:286` | drop_raise → pass | killed | killed |
| M096 | `_verify_record:295` | negate → condition | killed | killed |
| M097 | `_check_launcher_set:346` | negate → condition | killed | killed |
| M098 | `_slot_package_version:360` | return → None | killed | killed |
| M099 | `admit:393` | number → 449 | survived | killed |
| M100 | `_file_sha256:196` | negate → condition | killed | killed |
| M101 | `walk:229` | drop_raise → pass | killed | killed |
| M102 | `_read_state_json:260` | negate → condition | killed | killed |
| M103 | `_check_gate:286` | number → 65 | survived | killed |
| M104 | `_verify_record:295` | compare → Eq | killed | killed |
| M105 | `_check_launcher_set:346` | compare → Eq | killed | killed |
| M106 | `_slot_package_version:362` | drop_raise → pass | survived | killed |
| M107 | `admit:394` | drop_raise → pass | survived | killed |
| M108 | `_file_sha256:197` | return → None | killed | killed |
| M109 | `walk:231` | negate → condition | killed | killed |
| M110 | `_read_state_json:260` | compare → GtE | survived | killed |
| M111 | `_verify_record:295` | number → 2 | killed | killed |
| M112 | `_check_launcher_set:346` | drop_raise → pass | killed | killed |
| M113 | `admit:395` | number → 449 | survived | killed |
| M114 | `walk:233` | negate → condition | killed | killed |
| M115 | `_read_state_json:260` | drop_raise → pass | survived | killed |
| M116 | `_verify_record:295` | drop_raise → pass | survived | killed |
| M117 | `_check_launcher_set:347` | negate → condition | killed | killed |
| M118 | `admit:397` | drop_raise → pass | survived | killed |
| M119 | `walk:236` | drop_raise → pass | killed | killed |
| M120 | `_read_state_json:262` | negate → condition | killed | killed |
| M121 | `_verify_record:296` | negate → condition | killed | killed |
| M122 | `_check_launcher_set:347` | compare → Eq | killed | killed |
| M123 | `admit:399` | negate → condition | killed | killed |
| M124 | `_read_state_json:264` | negate → condition | killed | killed |
| M125 | `_verify_record:296` | bool → And | killed | killed |
| M126 | `_check_launcher_set:347` | drop_raise → pass | killed | killed |
| M127 | `admit:399` | drop_raise → pass | survived | killed |
| M128 | `_read_state_json:264` | compare → GtE | survived | killed |
| M129 | `_verify_record:296` | compare → Eq | killed | killed |
| M130 | `_check_launcher_set:349` | negate → condition | killed | killed |
| M131 | `admit:400` | number → 64 | killed | killed |
| M132 | `_read_state_json:264` | drop_raise → pass | survived | killed |
| M133 | `_verify_record:297` | drop_raise → pass | killed | killed |
| M134 | `_check_launcher_set:349` | bool → And | killed | killed |
| M135 | `admit:405` | number → 1 | survived | killed |
| M136 | `_read_state_json:267` | return → None | killed | killed |
| M137 | `_verify_record:298` | negate → condition | killed | killed |
| M138 | `_check_launcher_set:349` | compare → Eq | killed | killed |
| M139 | `admit:405` | number → 1 | survived | killed |
| M140 | `_read_state_json:268` | drop_raise → pass | killed | killed |
| M141 | `_verify_record:298` | drop_raise → pass | survived | killed |
| M142 | `_check_launcher_set:349` | compare → Eq | killed | killed |
| M143 | `admit:406` | number → 385 | survived | killed |
| M144 | `receipt:111` | return_empty → {} | killed | killed |
| M145 | `_is_hex64:274` | return_false → False | killed | killed |
| M146 | `canonical_bytes:168` | remove_statement → pass | survived | killed |
| M147 | `tree_digest:239` | remove_statement → pass | killed | killed |
| M148 | `_read_state_json:259` | remove_statement → pass | survived | killed |
| M149 | `_verify_record:311` | remove_statement → pass | killed | killed |
| M150 | `admit:392` | remove_statement → pass | killed | killed |

## Verification

- `python3 tests/test_codex_promotion.py`: 83 passed.
- `python3 tests/test_suite.py`: 593 passed, 0 failed, 5 platform skips.
- `python3 tests/test_onboard.py`: 28 passed.
- `python3 tests/test_local_worker.py`: 14 setup errors because the sandbox denied a `127.0.0.1` server bind (`PermissionError: [Errno 1] Operation not permitted`); no remote API was called.
