# Mutation report: `src/agent_bridge/orchestration/windows_wsl.py`

## Method

- Parsed the source as Python AST and made one mutation per copy, capped at 150. The 95 chosen mutants cover every top-level function.
- Mutations included comparison flips, `and`/`or` swaps, condition negation, integer increments, return replacement with `None`, statement removal, `+`/`-` swaps, and dropped raises. Docstrings, comments, and logging were excluded.
- Each mutant was executed in a fresh offline Python process with a 3-second timeout. The relevant existing suite was `tests/test_windows_wsl.py` (89 tests). The after run added `tests/test_windows_wsl_mutation.py` (26 tests). No packages or network were used.
- The original source file was never written by the runner and remained SHA-256 `a56e876fb1992e44e3a9d1721657ea63511f594ca1dc02f072439ad0ed6c2ea7`.

## Kill rates

| Phase | Killed | Generated | Kill rate |
| --- | ---: | ---: | ---: |
| Existing tests | 65 | 95 | 68.4% |
| Existing and new tests | 95 | 95 | 100.0% |

## Mutants generated

| ID | Function:line | Mutation | Before | After |
| --- | --- | --- | --- | --- |
| M001 | `_reject_unsafe_string:91` | compare | killed | killed |
| M002 | `_reject_unsafe_string:88` | boolean | survived | killed |
| M003 | `_reject_unsafe_string:84` | condition | killed | killed |
| M004 | `_reject_unsafe_string:95` | return | killed | killed |
| M005 | `_reject_unsafe_string:90` | statement | killed | killed |
| M006 | `_reject_unsafe_string:85` | raise | survived | killed |
| M007 | `_reject_unpinned_version:100` | condition | killed | killed |
| M008 | `_reject_unpinned_version:102` | return | killed | killed |
| M009 | `_reject_unpinned_version:99` | statement | survived | killed |
| M010 | `_reject_unpinned_version:101` | raise | survived | killed |
| M011 | `parse_manifest:134` | compare | killed | killed |
| M012 | `parse_manifest:143` | boolean | killed | killed |
| M013 | `parse_manifest:125` | condition | killed | killed |
| M014 | `parse_manifest:145` | number | killed | killed |
| M015 | `parse_manifest:160` | return | killed | killed |
| M016 | `parse_manifest:128` | statement | killed | killed |
| M017 | `parse_windows_build:208` | compare | survived | killed |
| M018 | `parse_windows_build:199` | boolean | killed | killed |
| M019 | `parse_windows_build:199` | condition | killed | killed |
| M020 | `parse_windows_build:206` | number | killed | killed |
| M021 | `parse_windows_build:212` | return | killed | killed |
| M022 | `parse_windows_build:202` | statement | killed | killed |
| M023 | `parse_wsl_version:230` | compare | survived | killed |
| M024 | `parse_wsl_version:221` | boolean | survived | killed |
| M025 | `parse_wsl_version:221` | condition | killed | killed |
| M026 | `parse_wsl_version:228` | number | killed | killed |
| M027 | `parse_wsl_version:236` | return | killed | killed |
| M028 | `parse_wsl_version:224` | statement | killed | killed |
| M029 | `all_prerequisites_met:253` | condition | killed | killed |
| M030 | `all_prerequisites_met:265` | return | killed | killed |
| M031 | `validate_distro_name:286` | compare | survived | killed |
| M032 | `validate_distro_name:284` | condition | killed | killed |
| M033 | `validate_distro_name:295` | return | killed | killed |
| M034 | `validate_distro_name:290` | statement | killed | killed |
| M035 | `validate_distro_name:285` | raise | survived | killed |
| M036 | `build_distro_name:301` | boolean | survived | killed |
| M037 | `build_distro_name:301` | condition | killed | killed |
| M038 | `build_distro_name:303` | return | killed | killed |
| M039 | `build_distro_name:303` | arithmetic | killed | killed |
| M040 | `build_distro_name:302` | raise | survived | killed |
| M041 | `build_guest_environment:343` | boolean | survived | killed |
| M042 | `build_guest_environment:338` | condition | killed | killed |
| M043 | `build_guest_environment:352` | return | killed | killed |
| M044 | `build_guest_environment:351` | statement | killed | killed |
| M045 | `build_guest_environment:339` | raise | survived | killed |
| M046 | `_looks_like_windows_path:392` | condition | killed | killed |
| M047 | `_looks_like_windows_path:400` | return | survived | killed |
| M048 | `_contains_host_marker:411` | compare | killed | killed |
| M049 | `_contains_host_marker:413` | boolean | survived | killed |
| M050 | `_contains_host_marker:407` | condition | killed | killed |
| M051 | `_contains_host_marker:419` | return | survived | killed |
| M052 | `_reject_host_path:423` | boolean | survived | killed |
| M053 | `_reject_host_path:423` | condition | killed | killed |
| M054 | `_reject_host_path:424` | raise | survived | killed |
| M055 | `validate_job_spec:453` | compare | killed | killed |
| M056 | `validate_job_spec:462` | boolean | survived | killed |
| M057 | `validate_job_spec:443` | condition | killed | killed |
| M058 | `validate_job_spec:466` | number | killed | killed |
| M059 | `validate_job_spec:498` | return | killed | killed |
| M060 | `validate_job_spec:446` | statement | killed | killed |
| M061 | `_reject_shell_unsafe:513` | boolean | survived | killed |
| M062 | `_reject_shell_unsafe:513` | condition | killed | killed |
| M063 | `_reject_shell_unsafe:517` | return | survived | killed |
| M064 | `_reject_shell_unsafe:514` | raise | survived | killed |
| M065 | `_reject_reserved_and_ambiguous_components:555` | compare | killed | killed |
| M066 | `_reject_reserved_and_ambiguous_components:553` | condition | survived | killed |
| M067 | `_reject_reserved_and_ambiguous_components:561` | number | killed | killed |
| M068 | `_reject_reserved_and_ambiguous_components:551` | statement | killed | killed |
| M069 | `_reject_reserved_and_ambiguous_components:556` | raise | survived | killed |
| M070 | `_validate_windows_host_path:580` | boolean | survived | killed |
| M071 | `_validate_windows_host_path:571` | condition | killed | killed |
| M072 | `_validate_windows_host_path:587` | return | survived | killed |
| M073 | `_validate_windows_host_path:570` | statement | killed | killed |
| M074 | `_validate_windows_host_path:572` | raise | survived | killed |
| M075 | `build_import_argv:599` | compare | killed | killed |
| M076 | `build_import_argv:599` | condition | killed | killed |
| M077 | `build_import_argv:591` | number | killed | killed |
| M078 | `build_import_argv:601` | return | killed | killed |
| M079 | `build_import_argv:596` | statement | killed | killed |
| M080 | `build_import_argv:600` | raise | killed | killed |
| M081 | `build_terminate_argv:614` | return | killed | killed |
| M082 | `build_terminate_argv:613` | statement | killed | killed |
| M083 | `build_unregister_argv:619` | return | killed | killed |
| M084 | `build_unregister_argv:618` | statement | survived | killed |
| M085 | `build_guest_exec_argv:639` | boolean | survived | killed |
| M086 | `build_guest_exec_argv:630` | condition | killed | killed |
| M087 | `build_guest_exec_argv:643` | return | killed | killed |
| M088 | `build_guest_exec_argv:629` | statement | survived | killed |
| M089 | `build_guest_exec_argv:631` | raise | killed | killed |
| M090 | `select_stale_instances:671` | compare | killed | killed |
| M091 | `select_stale_instances:669` | condition | killed | killed |
| M092 | `select_stale_instances:671` | number | killed | killed |
| M093 | `select_stale_instances:698` | return | killed | killed |
| M094 | `select_stale_instances:687` | statement | killed | killed |
| M095 | `select_stale_instances:691` | arithmetic | killed | killed |

## Survivors left

- None. Equivalent mutants: none. Untestable offline: none.

## Defects found

- None.

## New tests and the originally surviving mutants they kill

- `test_build_non_string_raises_contract_error`: M036, M040
- `test_guest_exec_rejects_host_marker_in_supplied_job`: M085
- `test_guest_exec_rejects_unsafe_name`: M088
- `test_host_marker_detects_slash_unc_path`: M049
- `test_host_marker_returns_false_for_guest_path`: M051
- `test_reject_host_path_reports_empty_path`: M052, M054
- `test_rejects_non_letter_drive`: M070
- `test_rejects_non_mapping_with_contract_error`: M045
- `test_rejects_non_sequence_command_with_contract_error`: M056
- `test_rejects_non_string_allowlisted_value`: M041
- `test_rejects_trailing_dot_component`: M066, M069
- `test_rejects_traversal_in_absolute_path`: M074
- `test_shell_unsafe_rejects_non_string_with_contract_error`: M061, M064
- `test_shell_unsafe_returns_valid_value`: M063
- `test_unpinned_version_rejects_embedded_traversal`: M002, M009
- `test_unpinned_version_rejects_non_numeric_prefix`: M010
- `test_unregister_rejects_unsafe_name`: M084
- `test_unsafe_string_rejects_non_string_with_contract_error`: M006
- `test_unsafe_string_rejects_single_path_separator`: M002
- `test_validate_accepts_maximum_length_suffix`: M031
- `test_validate_non_string_raises_contract_error`: M035
- `test_windows_build_at_minimum_passes`: M017
- `test_windows_host_path_returns_valid_value`: M072
- `test_windows_path_detector_returns_false_for_guest_path`: M047
- `test_wsl_version_at_minimum_passes`: M023
- `test_wsl_version_non_string_fails_closed`: M024

## Verification

- `python3 tests/test_windows_wsl.py`: 89 passed.
- `python3 tests/test_windows_wsl_mutation.py`: 26 passed.
- `python3 tests/test_suite.py`: 593 passed, 5 environmental skips.
- `tests/test_onboard.py`, `tests/test_hostenv.py`, `tests/test_automatic_gate.py`, and `tests/test_delegation_audit.py` passed.
- `tests/test_local_worker.py` and `tests/test_automatic_delegation_e2e.py` could not bind their local loopback test servers in this sandbox (`PermissionError: [Errno 1] Operation not permitted`). No external service was contacted.
