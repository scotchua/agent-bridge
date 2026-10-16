# Mutation sweep: `src/agent_bridge/localq/intake.py`

## Method and result

- Generated 132 single-change AST mutants across all 21 functions and methods; cap: 150.
- Operators: 35 statement removals, 18 condition negations, 17 `return None` replacements, 14 empty-return replacements, 14 `return False` replacements, 13 comparison flips, 9 dropped raises, 7 `and`/`or` swaps, 3 numeric increments, and 2 `+`/`-` swaps.
- Comments and docstrings were not mutated. The module has no logging calls.
- Each mutant ran in a fresh process with the modified source compiled in memory, an isolated temporary home, and an 8-second time box. The production source was never written; its SHA-256 remained `213549021cbe7cc9b70aa72c2d54db14818fd61d31a6330b48c0c0bb0d7aaf94` after every mutant.
- Relevant offline modules: `test_localq_intake.py`, `test_checkpoint.py`, `test_local_vocabulary.py`, `test_localq_client_data_health.py`, `test_extract_classify.py`, and `test_system_acceptance.py`. The before run excluded only the 14 new test methods; the after run included them.

| Measurement | Killed | Survived | Kill rate |
| --- | ---: | ---: | ---: |
| Before new tests | 99 | 33 | 75.0% |
| After new tests | 132 | 0 | 100.0% |

All 33 behavior-changing baseline survivors were killed after the new tests. Equivalent mutants: none. Untestable offline mutants: none.

## New tests by function

All tests are in `tests/test_localq_intake.py`. Each passed on the original and failed alone against the recorded mutant shown.

| Function | New test | Witness mutant |
| --- | --- | --- |
| `_migrate_checkpoint_column, _init_db` | `test_database_initialization_is_idempotent_and_durable` | M019 |
| `_init_db` | `test_database_initialization_restores_full_synchronous_mode` | M023 |
| `_init_db` | `test_database_initialization_keeps_receipts_append_only` | M028 |
| `_validated_flags` | `test_validated_flags_rejects_a_string_instead_of_a_list` | M033 |
| `checkpoint` | `test_checkpoint_rejects_invalid_task_ids_and_accepts_the_limit` | M046 |
| `checkpoint` | `test_checkpoint_rejects_an_oversized_request_key` | M049 |
| `checkpoint` | `test_checkpoint_replay_returns_the_saved_checkpoint` | M050 |
| `_validate_existing_checkpoint` | `test_checkpoint_replay_rejects_each_changed_field` | M053 |
| `get_checkpoint` | `test_get_checkpoint_returns_the_row_and_rejects_unknown_ids` | M061 |
| `_record_health_refusal` | `test_health_refusal_is_committed_to_the_audit` | M074 |
| `_validate_existing_receipt` | `test_receipt_replay_rejects_a_different_caller` | M077 |
| `_bind_checkpoint` | `test_checkpoint_binding_rejects_empty_and_missing_ids` | M085 |
| `route` | `test_route_rejects_non_text_input_and_invalid_purpose` | M093 |
| `receipt` | `test_receipt_rejects_an_unknown_id` | M103 |

## Defects found

None.

## Verification and limits

- `python3 -m unittest discover -s tests -p test_localq_intake.py -q`: 26 tests passed.
- `python3 tests/test_suite.py` in an isolated temporary home: 592 passed, 0 failed, 5 environmental skips.
- `python3 tests/test_onboard.py`: passed.
- `python3 tests/test_local_worker.py`: could not run in this sandbox. All 14 cases failed during setup because binding a `127.0.0.1` test socket returned `PermissionError: [Errno 1] Operation not permitted`. No external API was contacted.
- Production source is unchanged (`git diff -- src/agent_bridge/localq/intake.py` is empty).

## Mutant ledger

`Killed` means an assertion, exception, or test failure in the timed focused suite. No final mutant timed out.

| ID | Function:line | Operator | Before | After |
| --- | --- | --- | --- | --- |
| M001 | `accepted_values:80` | negate | killed | killed |
| M002 | `accepted_values:80` | comparison | killed | killed |
| M003 | `accepted_values:81` | return_none | killed | killed |
| M004 | `accepted_values:82` | negate | killed | killed |
| M005 | `accepted_values:83` | return_none | killed | killed |
| M006 | `accepted_values:84` | negate | killed | killed |
| M007 | `accepted_values:85` | return_none | killed | killed |
| M008 | `accepted_values:86` | negate | killed | killed |
| M009 | `byte_threshold:124` | return_none | killed | killed |
| M010 | `__init__:132` | remove_statement | killed | killed |
| M011 | `__init__:133` | remove_statement | killed | killed |
| M012 | `__init__:134` | remove_statement | killed | killed |
| M013 | `_connect:137` | remove_statement | killed | killed |
| M014 | `_connect:139` | return_none | killed | killed |
| M015 | `_connect:138` | remove_statement | killed | killed |
| M016 | `_db:143` | remove_statement | killed | killed |
| M017 | `_db:147` | remove_statement | killed | killed |
| M018 | `_migrate_checkpoint_column:166` | remove_statement | killed | killed |
| M019 | `_migrate_checkpoint_column:166` | constant | killed | killed |
| M020 | `_migrate_checkpoint_column:167` | negate | killed | killed |
| M021 | `_migrate_checkpoint_column:168` | remove_statement | killed | killed |
| M022 | `_init_db:172` | remove_statement | survived | killed |
| M023 | `_init_db:173` | remove_statement | survived | killed |
| M024 | `_init_db:174` | remove_statement | killed | killed |
| M025 | `_init_db:187` | remove_statement | killed | killed |
| M026 | `_init_db:188` | remove_statement | killed | killed |
| M027 | `_init_db:190` | remove_statement | killed | killed |
| M028 | `_init_db:193` | remove_statement | survived | killed |
| M029 | `_init_db:196` | remove_statement | killed | killed |
| M030 | `_normalized:220` | return_none | killed | killed |
| M031 | `_validated_flags:224` | remove_statement | killed | killed |
| M032 | `_validated_flags:225` | negate | killed | killed |
| M033 | `_validated_flags:225` | boolean | survived | killed |
| M034 | `_validated_flags:226` | drop_raise | survived | killed |
| M035 | `_validated_flags:227` | return_none | killed | killed |
| M036 | `_classify_metrics:240` | negate | killed | killed |
| M037 | `_classify_metrics:241` | return_none | killed | killed |
| M038 | `_classify_metrics:242` | remove_statement | killed | killed |
| M039 | `_classify_metrics:242` | arithmetic | killed | killed |
| M040 | `_classify_metrics:250` | boolean | killed | killed |
| M041 | `_classify_metrics:250` | comparison | killed | killed |
| M042 | `_classify_metrics:243` | negate | killed | killed |
| M043 | `_classify_metrics:244` | return_none | killed | killed |
| M044 | `checkpoint:285` | negate | killed | killed |
| M045 | `checkpoint:285` | boolean | survived | killed |
| M046 | `checkpoint:285` | comparison | survived | killed |
| M047 | `checkpoint:286` | drop_raise | survived | killed |
| M048 | `checkpoint:297` | remove_statement | killed | killed |
| M049 | `checkpoint:316` | constant | survived | killed |
| M050 | `checkpoint:325` | return_none | survived | killed |
| M051 | `checkpoint:287` | negate | killed | killed |
| M052 | `_validate_existing_checkpoint:352` | remove_statement | survived | killed |
| M053 | `_validate_existing_checkpoint:353` | boolean | survived | killed |
| M054 | `_validate_existing_checkpoint:353` | comparison | survived | killed |
| M055 | `_validate_existing_checkpoint:358` | negate | survived | killed |
| M056 | `_validate_existing_checkpoint:359` | drop_raise | survived | killed |
| M057 | `_validate_existing_checkpoint:354` | comparison | survived | killed |
| M058 | `_validate_existing_checkpoint:355` | comparison | survived | killed |
| M059 | `_validate_existing_checkpoint:356` | comparison | survived | killed |
| M060 | `_public_checkpoint:362` | return_none | killed | killed |
| M061 | `get_checkpoint:373` | remove_statement | survived | killed |
| M062 | `get_checkpoint:374` | negate | survived | killed |
| M063 | `get_checkpoint:375` | drop_raise | survived | killed |
| M064 | `get_checkpoint:376` | return_none | survived | killed |
| M065 | `audit:381` | remove_statement | killed | killed |
| M066 | `audit:399` | negate | killed | killed |
| M067 | `audit:405` | return_none | killed | killed |
| M068 | `audit:382` | remove_statement | killed | killed |
| M069 | `audit:383` | remove_statement | killed | killed |
| M070 | `audit:389` | remove_statement | killed | killed |
| M071 | `audit:393` | remove_statement | killed | killed |
| M072 | `audit:395` | remove_statement | killed | killed |
| M073 | `_record_health_refusal:413` | remove_statement | killed | killed |
| M074 | `_record_health_refusal:414` | remove_statement | killed | killed |
| M075 | `_record_health_refusal:417` | remove_statement | killed | killed |
| M076 | `_validate_existing_receipt:431` | remove_statement | killed | killed |
| M077 | `_validate_existing_receipt:432` | boolean | survived | killed |
| M078 | `_validate_existing_receipt:432` | comparison | killed | killed |
| M079 | `_validate_existing_receipt:440` | negate | killed | killed |
| M080 | `_validate_existing_receipt:441` | drop_raise | killed | killed |
| M081 | `_validate_existing_receipt:433` | comparison | killed | killed |
| M082 | `_validate_existing_receipt:434` | comparison | killed | killed |
| M083 | `_validate_existing_receipt:435` | comparison | killed | killed |
| M084 | `_bind_checkpoint:455` | negate | killed | killed |
| M085 | `_bind_checkpoint:455` | boolean | survived | killed |
| M086 | `_bind_checkpoint:456` | drop_raise | survived | killed |
| M087 | `_bind_checkpoint:458` | remove_statement | killed | killed |
| M088 | `_bind_checkpoint:461` | comparison | killed | killed |
| M089 | `_bind_checkpoint:468` | return_none | killed | killed |
| M090 | `_bind_checkpoint:459` | negate | killed | killed |
| M091 | `_bind_checkpoint:460` | drop_raise | survived | killed |
| M092 | `route:475` | negate | killed | killed |
| M093 | `route:476` | drop_raise | survived | killed |
| M094 | `route:477` | boolean | survived | killed |
| M095 | `route:486` | remove_statement | killed | killed |
| M096 | `route:487` | comparison | killed | killed |
| M097 | `route:506` | constant | killed | killed |
| M098 | `route:523` | arithmetic | killed | killed |
| M099 | `route:550` | return_none | killed | killed |
| M100 | `_public:602` | return_none | killed | killed |
| M101 | `receipt:616` | remove_statement | killed | killed |
| M102 | `receipt:617` | negate | killed | killed |
| M103 | `receipt:618` | drop_raise | survived | killed |
| M104 | `receipt:619` | return_none | killed | killed |
| M105 | `accepted_values:81` | return_empty | killed | killed |
| M106 | `accepted_values:81` | return_false | killed | killed |
| M107 | `byte_threshold:124` | return_empty | killed | killed |
| M108 | `byte_threshold:124` | return_false | killed | killed |
| M109 | `_connect:139` | return_empty | killed | killed |
| M110 | `_connect:139` | return_false | killed | killed |
| M111 | `_normalized:220` | return_empty | killed | killed |
| M112 | `_normalized:220` | return_false | killed | killed |
| M113 | `_validated_flags:227` | return_empty | killed | killed |
| M114 | `_validated_flags:227` | return_false | killed | killed |
| M115 | `_classify_metrics:241` | return_empty | killed | killed |
| M116 | `_classify_metrics:241` | return_false | killed | killed |
| M117 | `checkpoint:325` | return_empty | survived | killed |
| M118 | `checkpoint:325` | return_false | survived | killed |
| M119 | `_public_checkpoint:362` | return_empty | killed | killed |
| M120 | `_public_checkpoint:362` | return_false | killed | killed |
| M121 | `get_checkpoint:376` | return_empty | survived | killed |
| M122 | `get_checkpoint:376` | return_false | survived | killed |
| M123 | `audit:405` | return_empty | killed | killed |
| M124 | `audit:405` | return_false | killed | killed |
| M125 | `_bind_checkpoint:468` | return_empty | killed | killed |
| M126 | `_bind_checkpoint:468` | return_false | killed | killed |
| M127 | `route:550` | return_empty | killed | killed |
| M128 | `route:550` | return_false | killed | killed |
| M129 | `_public:602` | return_empty | killed | killed |
| M130 | `_public:602` | return_false | killed | killed |
| M131 | `receipt:619` | return_empty | killed | killed |
| M132 | `receipt:619` | return_false | killed | killed |

Survivors left: none.
