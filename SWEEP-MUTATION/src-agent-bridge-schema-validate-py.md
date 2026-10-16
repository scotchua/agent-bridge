# Mutation test: `src/agent_bridge/schema_validate.py`

## Method

The throwaway runner parsed the validator and compiled one altered syntax tree per child process. It never wrote the production module. It skipped comments, docstrings and logging, capped selection at 150, and generated 101 mutants across every function and the violation limit. Operators included comparison flips, `and`/`or` swaps, condition negation, a numeric increment, return replacements, statement and raise removal, and a `-`/`+` swap. Each child had an 8-second limit; none timed out.

The before run executed three focused existing `InputSecurityTests` against each mutant, then `test_contract_accepted_by_both_peers` from `tests/test_suite.py` for mutants that passed them. The after run added all 14 tests in `tests/test_schema_validate_mutations.py`. A kill means an assertion failed or the mutant raised; the original passes these tests.

## Kill rates

| Phase | Killed | Survived | Kill rate |
| --- | ---: | ---: | ---: |
| Before new tests | 50 | 51 | 50/101 = 49.5% |
| After new tests | 99 | 2 | 99/101 = 98.0% |

Forty-nine formerly surviving mutants were killed by the new tests. All 14 new tests fail on at least one recorded mutant and pass on the original.

## Verification

- `python3 tests/test_suite.py`: 593 passed, 0 failed, 5 platform skips.
- `PYTHONPATH=src python3 -m unittest discover -s tests -p test_schema_validate_mutations.py`: 14 passed.
- `PYTHONPATH=src python3 -m unittest discover -s tests -p test_input_security.py`: 9 passed.
- `tests/test_onboard.py` with an isolated temporary home: 28 passed.
- `tests/test_local_worker.py` with an isolated temporary home: 14 setup errors because this sandbox denies binding a loopback socket (`PermissionError: [Errno 1] Operation not permitted`). It could not be exercised here; this is unrelated to the validator mutants.
- The production validator remained byte-identical: SHA-256 `1eb78765943067dd945ee6cc5354fb5a8f050451c90f38c404a97fe5202f9fc3` before and after. The final working tree contains only this report and the new test file.

## Mutants by function

| Function or value | Generated | Killed before | Killed after |
| --- | ---: | ---: | ---: |
| `MAX_VIOLATIONS` | 1 | 0 | 1 |
| `assert_supported` | 14 | 7 | 14 |
| `validate` | 5 | 5 | 5 |
| `_ValidationState.__init__` | 2 | 2 | 2 |
| `_ValidationState.add` | 9 | 2 | 8 |
| `_ValidationState.full` | 4 | 0 | 4 |
| `_validate` | 66 | 34 | 65 |

## Survivors left

- M052, line 83: replacing `return False` with `return False` is identical to the original (equivalent).
- M098, line 117: flipping `t == "object"` to `t != "object"` changes only an assignment of `ok = False` in a branch where a boolean instance has already failed every non-object type check; the validation result is unchanged (equivalent).
- Untestable offline: none. Defects found: none.

## New tests

| Test name | Example mutant it kills |
| --- | --- |
| `test_accepts_supported_nested_schema` | M077 |
| `test_add_rejects_the_next_violation` | M000 |
| `test_array_boundaries_and_indexed_items` | M011 |
| `test_checks_all_supported_types` | M011 |
| `test_checks_const_and_enum_without_echoing_instance` | M011 |
| `test_default_additional_properties_are_allowed` | M006 |
| `test_initial_state_and_exact_capacity` | M000 |
| `test_numeric_boundaries_and_boolean_exclusion` | M011 |
| `test_object_required_extra_and_nested_properties` | M011 |
| `test_rejects_non_object_and_nested_schema_nodes` | M003 |
| `test_rejects_unknown_keywords_and_types` | M003 |
| `test_returns_clean_and_single_violation_lists` | M011 |
| `test_string_length_boundaries` | M014 |
| `test_truncation_marker_follows_exactly_one_hundred_errors` | M000 |

## Mutant ledger

| ID | Line | Function or value | Mutation | Before | After |
| --- | ---: | --- | --- | --- | --- |
| M000 | 61 | `MAX_VIOLATIONS` | constant | survived | killed |
| M001 | 38 | `assert_supported` | negate | killed | killed |
| M002 | 41 | `assert_supported` | negate | killed | killed |
| M003 | 43 | `assert_supported` | negate | survived | killed |
| M004 | 49 | `assert_supported` | negate | killed | killed |
| M005 | 52 | `assert_supported` | negate | killed | killed |
| M006 | 68 | `validate` | negate | killed | killed |
| M007 | 70 | `validate` | return | killed | killed |
| M008 | 70 | `validate` | return | killed | killed |
| M009 | 70 | `validate` | return | killed | killed |
| M010 | 100 | `_validate` | negate | killed | killed |
| M011 | 105 | `_validate` | negate | survived | killed |
| M012 | 123 | `_validate` | negate | killed | killed |
| M013 | 125 | `_validate` | negate | killed | killed |
| M014 | 129 | `_validate` | negate | survived | killed |
| M015 | 135 | `_validate` | negate | survived | killed |
| M016 | 141 | `_validate` | negate | killed | killed |
| M017 | 158 | `_validate` | negate | killed | killed |
| M018 | 39 | `assert_supported` | drop_raise | survived | killed |
| M019 | 42 | `assert_supported` | drop_raise | survived | killed |
| M020 | 45 | `assert_supported` | negate | killed | killed |
| M021 | 53 | `assert_supported` | drop_raise | survived | killed |
| M022 | 69 | `validate` | remove | killed | killed |
| M023 | 81 | `_ValidationState.add` | negate | killed | killed |
| M024 | 84 | `_ValidationState.add` | remove | killed | killed |
| M025 | 85 | `_ValidationState.add` | return | survived | killed |
| M026 | 85 | `_ValidationState.add` | return | survived | killed |
| M027 | 85 | `_ValidationState.add` | return | survived | killed |
| M028 | 89 | `_ValidationState.full` | return | survived | killed |
| M029 | 89 | `_ValidationState.full` | return | survived | killed |
| M030 | 89 | `_ValidationState.full` | return | survived | killed |
| M031 | 106 | `_validate` | negate | killed | killed |
| M032 | 119 | `_validate` | negate | killed | killed |
| M033 | 123 | `_validate` | boolean | killed | killed |
| M034 | 124 | `_validate` | remove | survived | killed |
| M035 | 125 | `_validate` | boolean | killed | killed |
| M036 | 127 | `_validate` | remove | survived | killed |
| M037 | 130 | `_validate` | negate | killed | killed |
| M038 | 132 | `_validate` | negate | killed | killed |
| M039 | 135 | `_validate` | boolean | survived | killed |
| M040 | 136 | `_validate` | negate | survived | killed |
| M041 | 138 | `_validate` | negate | survived | killed |
| M042 | 142 | `_validate` | negate | killed | killed |
| M043 | 144 | `_validate` | negate | killed | killed |
| M044 | 151 | `_validate` | negate | survived | killed |
| M045 | 166 | `_validate` | negate | killed | killed |
| M046 | 40 | `assert_supported` | arithmetic | killed | killed |
| M047 | 45 | `assert_supported` | boolean | survived | killed |
| M048 | 46 | `assert_supported` | drop_raise | survived | killed |
| M049 | 81 | `_ValidationState.add` | compare | survived | killed |
| M050 | 83 | `_ValidationState.add` | return | survived | killed |
| M051 | 83 | `_ValidationState.add` | return | survived | killed |
| M052 | 83 | `_ValidationState.add` | return | survived | survived |
| M053 | 89 | `_ValidationState.full` | compare | survived | killed |
| M054 | 106 | `_validate` | compare | killed | killed |
| M055 | 108 | `_validate` | negate | killed | killed |
| M056 | 120 | `_validate` | remove | survived | killed |
| M057 | 123 | `_validate` | compare | survived | killed |
| M058 | 130 | `_validate` | boolean | killed | killed |
| M059 | 131 | `_validate` | remove | survived | killed |
| M060 | 132 | `_validate` | boolean | killed | killed |
| M061 | 133 | `_validate` | remove | survived | killed |
| M062 | 136 | `_validate` | boolean | survived | killed |
| M063 | 137 | `_validate` | remove | survived | killed |
| M064 | 138 | `_validate` | boolean | survived | killed |
| M065 | 139 | `_validate` | remove | survived | killed |
| M066 | 142 | `_validate` | boolean | killed | killed |
| M067 | 143 | `_validate` | remove | survived | killed |
| M068 | 144 | `_validate` | boolean | survived | killed |
| M069 | 145 | `_validate` | remove | killed | killed |
| M070 | 159 | `_validate` | boolean | killed | killed |
| M071 | 160 | `_validate` | boolean | killed | killed |
| M072 | 161 | `_validate` | negate | survived | killed |
| M073 | 164 | `_validate` | negate | survived | killed |
| M074 | 176 | `_validate` | negate | survived | killed |
| M075 | 179 | `_validate` | negate | killed | killed |
| M076 | 45 | `assert_supported` | boolean | killed | killed |
| M077 | 47 | `assert_supported` | boolean | survived | killed |
| M078 | 107 | `_validate` | boolean | survived | killed |
| M079 | 108 | `_validate` | compare | killed | killed |
| M080 | 110 | `_validate` | negate | killed | killed |
| M081 | 130 | `_validate` | compare | survived | killed |
| M082 | 132 | `_validate` | compare | survived | killed |
| M083 | 136 | `_validate` | compare | survived | killed |
| M084 | 138 | `_validate` | compare | survived | killed |
| M085 | 142 | `_validate` | compare | survived | killed |
| M086 | 144 | `_validate` | compare | survived | killed |
| M087 | 153 | `_validate` | negate | survived | killed |
| M088 | 165 | `_validate` | remove | killed | killed |
| M089 | 168 | `_validate` | negate | killed | killed |
| M090 | 171 | `_validate` | negate | killed | killed |
| M091 | 109 | `_validate` | boolean | survived | killed |
| M092 | 110 | `_validate` | compare | killed | killed |
| M093 | 112 | `_validate` | negate | killed | killed |
| M094 | 174 | `_validate` | remove | killed | killed |
| M095 | 112 | `_validate` | compare | killed | killed |
| M096 | 117 | `_validate` | negate | killed | killed |
| M097 | 117 | `_validate` | boolean | killed | killed |
| M098 | 117 | `_validate` | compare | survived | survived |
| M099 | 77 | `_ValidationState.__init__` | initial_state | killed | killed |
| M100 | 78 | `_ValidationState.__init__` | initial_state | killed | killed |
