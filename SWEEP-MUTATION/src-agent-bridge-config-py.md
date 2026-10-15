# Mutation sweep: `src/agent_bridge/config.py`

## Method and result

- Generated 68 single-edit mutants across all 38 functions (maximum allowed: 150). Edits cover comparison flips, `and`/`or` swaps, condition negation, numeric `+1`, return replacement with `None`, empty and `False`, statement removal, `+`/`-` swap, and dropped raises. Comments, logging and docstrings were excluded.
- Each mutant ran in a separate Python process with a temporary copy compiled into the imported `agent_bridge.config` module. The production source file was never written. Each run had a 12-second timeout and a temporary `HOME`; no packages or network access were used.
- Before: `tests/test_client_parity.py` and, when the mutant survived, `tests/test_onboard.py`. After: the 43 before-survivors were run against `tests/test_config_mutation.py`; the 25 earlier kills retain their status.
- **Before kill rate: 25/68 (36.8%). After kill rate: 68/68 (100.0%).** No timeout occurred.
- Original file SHA-256 before and after: `ebf26c79b70a03f7e7fa51ff793d6146567a97cf526c9bc6f3fa1804aa1ac539`.

## Mutants generated

| ID | Function | Line | Mutation | Before | After |
| --- | --- | ---: | --- | --- | --- |
| M001 | `canonical_uuid` | 44 | negate condition | survived | killed |
| M002 | `canonical_uuid` | 50 | negate condition | survived | killed |
| M003 | `_deep_merge` | 343 | remove statement | killed | killed |
| M004 | `_deep_merge` | 349 | return replacement | killed | killed |
| M005 | `merge` | 354 | return replacement | killed | killed |
| M006 | `effective_config_sha256` | 359 | remove statement | killed | killed |
| M007 | `effective_config_sha256` | 360 | return replacement | survived | killed |
| M008 | `_is_complete` | 368 | return replacement | killed | killed |
| M009 | `load_effective` | 373 | remove statement | killed | killed |
| M010 | `load_effective` | 374 | negate condition | killed | killed |
| M011 | `build_effective` | 382 | remove statement | killed | killed |
| M012 | `build_effective` | 383 | remove statement | killed | killed |
| M013 | `local_config_path` | 393 | return replacement | killed | killed |
| M014 | `load` | 403 | remove statement | killed | killed |
| M015 | `load` | 404 | negate condition | killed | killed |
| M016 | `__init__` | 57 | remove statement | killed | killed |
| M017 | `__init__` | 58 | remove statement | survived | killed |
| M018 | `contract_version` | 77 | return replacement | survived | killed |
| M019 | `state_root` | 81 | return replacement | survived | killed |
| M020 | `schema_path` | 85 | remove statement | survived | killed |
| M021 | `schema_path` | 86 | negate condition | survived | killed |
| M022 | `limit` | 92 | return replacement | killed | killed |
| M023 | `limit` | 94 | drop raise | survived | killed |
| M024 | `retention_days` | 97 | return replacement | survived | killed |
| M025 | `prompt_budget` | 100 | remove statement | killed | killed |
| M026 | `prompt_budget` | 101 | remove statement | killed | killed |
| M027 | `request_timeout` | 107 | remove statement | survived | killed |
| M028 | `request_timeout` | 108 | negate condition | survived | killed |
| M029 | `allowed_classifications` | 114 | return replacement | killed | killed |
| M030 | `client_derived_routes` | 119 | remove statement | killed | killed |
| M031 | `client_derived_routes` | 120 | negate condition | killed | killed |
| M032 | `peer_reasoning_effort` | 140 | remove statement | survived | killed |
| M033 | `peer_reasoning_effort` | 141 | negate condition | survived | killed |
| M034 | `peer_allowed_classifications` | 158 | remove statement | killed | killed |
| M035 | `peer_allowed_classifications` | 159 | negate condition | killed | killed |
| M036 | `refused_classifications` | 171 | return replacement | killed | killed |
| M037 | `_local_first` | 174 | remove statement | survived | killed |
| M038 | `_local_first` | 175 | negate condition | survived | killed |
| M039 | `local_first_enabled` | 191 | remove statement | survived | killed |
| M040 | `local_first_enabled` | 192 | negate condition | survived | killed |
| M041 | `local_first_min_bytes` | 204 | remove statement | survived | killed |
| M042 | `local_first_min_bytes` | 205 | negate condition | survived | killed |
| M043 | `local_first_queue_root` | 225 | remove statement | survived | killed |
| M044 | `local_first_queue_root` | 226 | negate condition | survived | killed |
| M045 | `local_first_receipt_max_age_seconds` | 253 | remove statement | survived | killed |
| M046 | `local_first_receipt_max_age_seconds` | 254 | negate condition | survived | killed |
| M047 | `peer` | 259 | negate condition | killed | killed |
| M048 | `peer` | 261 | return replacement | killed | killed |
| M049 | `peer_extra_env` | 273 | remove statement | survived | killed |
| M050 | `peer_extra_env` | 274 | negate condition | survived | killed |
| M051 | `poll_interval` | 287 | return replacement | survived | killed |
| M052 | `python_executable` | 292 | remove statement | survived | killed |
| M053 | `python_executable` | 293 | return replacement | survived | killed |
| M054 | `state` | 297 | return replacement | survived | killed |
| M055 | `_state_identifier_path` | 302 | remove statement | survived | killed |
| M056 | `_state_identifier_path` | 303 | remove statement | survived | killed |
| M057 | `ledger_path` | 316 | return replacement | survived | killed |
| M058 | `job_dir` | 319 | return replacement | survived | killed |
| M059 | `conversation_path` | 322 | return replacement | survived | killed |
| M060 | `workspace` | 325 | return replacement | survived | killed |
| M061 | `load_schema` | 330 | remove statement | survived | killed |
| M062 | `load_schema` | 331 | negate condition | survived | killed |
| M063 | `canonical_uuid` | 50 | comparison | survived | killed |
| M064 | `_deep_merge` | 345 | and/or | survived | killed |
| M065 | `effective_config_sha256` | 359 | numeric +1 | survived | killed |
| M066 | `effective_config_sha256` | 359 | +/- | killed | killed |
| M067 | `canonical_uuid` | 52 | empty return | survived | killed |
| M068 | `canonical_uuid` | 52 | false return | survived | killed |

## New tests

Every test below passed on the original module and failed when run alone against the listed recorded mutant. The test names are grouped by the config function they exercise.

| Test | Mutant killed |
| --- | --- |
| `test_canonical_uuid_rejects_noncanonical_identifiers` | M001 |
| `test_deep_merge_preserves_inputs_and_nested_defaults` | M003 |
| `test_effective_config_sha256_uses_canonical_json` | M006 |
| `test_load_effective_requires_a_complete_document` | M009 |
| `test_init_rejects_missing_keys_and_peers` | M016 |
| `test_contract_version_returns_configured_value` | M018 |
| `test_state_root_expands_home` | M019 |
| `test_schema_path_resolves_relative_and_absolute_paths` | M020 |
| `test_limit_rejects_missing_or_noninteger_values` | M023 |
| `test_retention_days_returns_configured_days` | M024 |
| `test_prompt_budget_respects_configured_cap` | M025 |
| `test_request_timeout_checks_finite_value_and_floor` | M027 |
| `test_allowed_classifications_returns_tuple` | M029 |
| `test_client_derived_routes_rejects_bad_opt_in` | M030 |
| `test_peer_reasoning_effort_validates_level` | M032 |
| `test_peer_allowed_classifications_respects_override` | M034 |
| `test_refused_classifications_returns_tuple` | M036 |
| `test_local_first_object_is_validated` | M037 |
| `test_local_first_enabled_requires_boolean` | M039 |
| `test_local_first_min_bytes_requires_nonnegative_integer` | M041 |
| `test_local_first_queue_root_requires_absolute_path` | M043 |
| `test_local_first_receipt_age_requires_nonnegative_integer` | M045 |
| `test_peer_extra_env_rejects_credentials` | M049 |
| `test_poll_interval_returns_configured_seconds` | M051 |
| `test_python_executable_uses_configured_path` | M052 |
| `test_state_joins_root_and_components` | M054 |
| `test_state_identifier_path_rejects_bad_id` | M055 |
| `test_ledger_path_uses_state_root` | M057 |
| `test_job_dir_uses_canonical_identifier` | M058 |
| `test_conversation_path_uses_json_suffix` | M059 |
| `test_workspace_joins_peer_and_conversation` | M060 |
| `test_load_schema_reads_blob_and_reports_hash` | M061 |

## Survivors left

- Equivalent: none.
- Untestable offline: none.
- Defect: none.

## Defects found

None. No expected-failure tests were needed.

## Verification

- `python3 tests/test_config_mutation.py`: 32 passed.
- `python3 tests/test_onboard.py`: 28 passed.
- `python3 tests/test_suite.py`: 592 passed, 0 failed, 5 environmental skips.
- `python3 tests/test_local_worker.py`: blocked by the sandbox before test logic because binding its localhost test server raised `PermissionError: [Errno 1] Operation not permitted` in all 14 cases. No external service was contacted.
