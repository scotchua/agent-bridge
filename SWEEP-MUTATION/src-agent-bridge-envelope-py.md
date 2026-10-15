# Mutation sweep: `src/agent_bridge/envelope.py`

## Method

Thirty-five single-change mutants were made from an in-memory copy of the source and written only to a temporary file. The original module was executed with each copy in a separate subprocess; the source file itself was never edited. Each subprocess had an eight-second limit. Mutants were confined to executable code and covered all three functions. There were no comparison or boolean operators in those functions to flip or swap. The relevant existing direct test was `InputSecurityTests.test_unknown_key_never_reaches_corrective_prompt`; the after pass also ran the new tests in `test_envelope_mutation.py`. This focused mutation run does not count indirect checks in the full integration suite as kills.

## Results

| Phase | Killed | Generated | Kill rate |
| --- | ---: | ---: | ---: |
| Before new tests | 3 | 35 | 8.6% |
| After new tests | 35 | 35 | 100.0% |

## Mutants

| Function | Mutation | Before | After |
| --- | --- | --- | --- |
| `build_initial` | `initial.return_none` | survived | killed |
| `build_initial` | `initial.return_empty` | survived | killed |
| `build_initial` | `initial.no_preamble` | survived | killed |
| `build_initial` | `initial.no_version` | survived | killed |
| `build_initial` | `initial.no_schema` | survived | killed |
| `build_initial` | `initial.no_prompt` | survived | killed |
| `build_initial` | `initial.no_begin_marker` | survived | killed |
| `build_initial` | `initial.no_end_marker` | survived | killed |
| `build_initial` | `initial.indent_plus` | survived | killed |
| `build_initial` | `initial.unsorted_schema` | survived | killed |
| `build_continuation` | `continuation.return_none` | survived | killed |
| `build_continuation` | `continuation.return_false` | survived | killed |
| `build_continuation` | `continuation.no_rules` | survived | killed |
| `build_continuation` | `continuation.no_version` | survived | killed |
| `build_continuation` | `continuation.no_prompt` | survived | killed |
| `build_continuation` | `continuation.no_begin_marker` | survived | killed |
| `build_continuation` | `continuation.no_end_marker` | survived | killed |
| `build_corrective` | `corrective.return_none` | killed | killed |
| `build_corrective` | `corrective.return_empty` | survived | killed |
| `build_corrective` | `corrective.max_violations_minus` | survived | killed |
| `build_corrective` | `corrective.max_violations_plus` | survived | killed |
| `build_corrective` | `corrective.max_chars_minus` | survived | killed |
| `build_corrective` | `corrective.max_chars_plus` | survived | killed |
| `build_corrective` | `corrective.negate_type_check` | killed | killed |
| `build_corrective` | `corrective.drop_raise` | survived | killed |
| `build_corrective` | `corrective.drop_append` | survived | killed |
| `build_corrective` | `corrective.no_bullet` | survived | killed |
| `build_corrective` | `corrective.plus_to_minus` | killed | killed |
| `build_corrective` | `corrective.negate_empty_check` | survived | killed |
| `build_corrective` | `corrective.drop_fallback` | survived | killed |
| `build_corrective` | `corrective.no_code` | survived | killed |
| `build_corrective` | `corrective.no_violations` | survived | killed |
| `build_corrective` | `corrective.no_version` | survived | killed |
| `build_corrective` | `corrective.unsorted_schema` | survived | killed |
| `build_corrective` | `corrective.indent_plus` | survived | killed |

## New tests

- `test_envelope_mutation.ContinuationPromptTests.test_continuation_delimits_the_follow_up_question` — fails on `continuation.return_none`, `continuation.return_false`, `continuation.no_prompt`, `continuation.no_begin_marker`, `continuation.no_end_marker`.
- `test_envelope_mutation.ContinuationPromptTests.test_continuation_keeps_rules_version_and_question` — fails on `continuation.return_none`, `continuation.return_false`, `continuation.no_rules`, `continuation.no_version`, `continuation.no_prompt`.
- `test_envelope_mutation.CorrectivePromptTests.test_corrective_explains_absent_violations` — fails on `corrective.return_none`, `corrective.return_empty`, `corrective.negate_empty_check`, `corrective.drop_fallback`, `corrective.no_violations`.
- `test_envelope_mutation.CorrectivePromptTests.test_corrective_keeps_bulleted_violations` — fails on `corrective.return_none`, `corrective.return_empty`, `corrective.negate_type_check`, `corrective.drop_append`, `corrective.no_bullet`, `corrective.plus_to_minus`, `corrective.negate_empty_check`, `corrective.no_violations`.
- `test_envelope_mutation.CorrectivePromptTests.test_corrective_limits_each_violation_to_240_characters` — fails on `corrective.return_none`, `corrective.return_empty`, `corrective.max_chars_minus`, `corrective.max_chars_plus`, `corrective.negate_type_check`, `corrective.drop_append`, `corrective.no_bullet`, `corrective.plus_to_minus`, `corrective.negate_empty_check`, `corrective.no_violations`.
- `test_envelope_mutation.CorrectivePromptTests.test_corrective_limits_violation_count_to_twelve` — fails on `corrective.return_none`, `corrective.return_empty`, `corrective.max_violations_minus`, `corrective.max_violations_plus`, `corrective.negate_type_check`, `corrective.drop_append`, `corrective.no_bullet`, `corrective.plus_to_minus`, `corrective.negate_empty_check`, `corrective.no_violations`.
- `test_envelope_mutation.CorrectivePromptTests.test_corrective_preserves_code_version_and_sorted_schema` — fails on `corrective.return_none`, `corrective.return_empty`, `corrective.negate_type_check`, `corrective.plus_to_minus`, `corrective.no_code`, `corrective.no_version`, `corrective.unsorted_schema`, `corrective.indent_plus`.
- `test_envelope_mutation.CorrectivePromptTests.test_corrective_rejects_non_string_violations_with_validator_error` — fails on `corrective.negate_type_check`, `corrective.drop_raise`.
- `test_envelope_mutation.InitialPromptTests.test_initial_delimits_the_question` — fails on `initial.return_none`, `initial.return_empty`, `initial.no_prompt`, `initial.no_begin_marker`, `initial.no_end_marker`.
- `test_envelope_mutation.InitialPromptTests.test_initial_keeps_preamble_version_and_question` — fails on `initial.return_none`, `initial.return_empty`, `initial.no_preamble`, `initial.no_version`, `initial.no_prompt`.
- `test_envelope_mutation.InitialPromptTests.test_initial_serializes_schema_with_stable_indentation_and_order` — fails on `initial.return_none`, `initial.return_empty`, `initial.no_schema`, `initial.indent_plus`, `initial.unsorted_schema`.

## Survivors left

None. Equivalent: none. Untestable offline: none. Defect: none.

## Defects found

None.

## Verification

- All 11 new tests pass on the original module.
- The mutation runner checked that the source bytes were unchanged after each pass.
- `python3 tests/test_onboard.py`: passed (28 tests).
- `python3 tests/test_local_worker.py`: blocked by sandbox denial of a loopback server bind in setup (14 errors).
- `python3 tests/test_suite.py`: passed (593 checks, 0 failed, 5 environmental skips).
