# Mutation sweep: windows_privacy.py

The source was imported and exercised offline. The temporary runner compiled one
modified source string at a time into a subprocess module; it never wrote to the
production file. Each subprocess had a 12-second limit. The selected 26 mutants
cover every function in this module, including `PrivacyError.__init__` and the
returned secure writer. Comments, logging and docstrings were excluded.

| Measure | Existing `test_windows_privacy.py` | With `test_windows_privacy_mutation.py` |
| --- | ---: | ---: |
| Generated mutants | 26 | 26 |
| Killed | 13 | 26 |
| Survived | 13 | 0 |
| Kill rate | 50% | 100% |

Each row is one independently executed mutation. `K` means killed and `S`
means survived. Mutations were made in memory and the source SHA-256 was checked
after every run: `a3860dfa0021c1cab32dc78156ec34f117bf584b5e6f1cb56375b2a9e6782ae1`.

| ID | Function | Mutation | Before | After |
| --- | --- | --- | :---: | :---: |
| M01 | `PrivacyError.__init__` | reason becomes `None` | K | K |
| M02 | `_reparse_tag` | return zero | S | K |
| M03 | `assert_no_reparse_ancestors` | boundary `or` becomes `and` | K | K |
| M04 | `assert_no_reparse_ancestors` | link `or` becomes `and` | K | K |
| M05 | `require_private_directory` | negate missing verifier check | K | K |
| M06 | `require_private_directory` | negate verified check | K | K |
| M07 | `_observe_owner_only` | negate observer availability | K | K |
| M08 | `_examine_before_open` | negate regular file check | K | K |
| M09 | `_same_object` | `==` becomes `!=` | K | K |
| M10 | `require_private_file` | negate identity check | K | K |
| M11 | `read_private_file` | size `>` becomes `>=` | S | K |
| M12 | `read_private_file` | postread mtime comparison gains one | K | K |
| M13 | `_verify_descriptor_acl` | owner `!=` becomes `==` | K | K |
| M14 | `_verify_descriptor_acl` | mode mask loses one bit | S | K |
| M15 | `_descriptor_path` | return fallback instead of resolved path | S | K |
| M16 | `_read_all` | remaining `+ 1` becomes `- 1` | K | K |
| M17 | `_read_all` | payload size `>` becomes `>=` | S | K |
| M18 | `exclusive_lock` | negate successful lock check | S | K |
| M19 | `exclusive_lock` | deadline `+` becomes `-` | S | K |
| M20 | `_try_lock` | successful flock returns `False` | S | K |
| M21 | `_unlock` | remove flock unlock | S | K |
| M22 | `file_identity` | `lstat` becomes `stat` | S | K |
| M23 | `atomic_private_write` | identity `!=` becomes `==` | K | K |
| M24 | `atomic_private_write` | remove `fsync` statement | S | K |
| M25 | `platform_secure_writer` | remove enforcement call | S | K |
| M26 | `_descriptor_path` | drop resolver exception raise | S | K |

## New tests and mutants killed

Every test below passed on the original and failed when run alone against its
listed mutant. Tests are grouped by the function they exercise.

| Function | New test | Mutant |
| --- | --- | --- |
| `_reparse_tag` | `test_reparse_tag_is_preserved` | M02 |
| `read_private_file`, `_read_all` | `test_file_exactly_at_limit_is_read` | M11, M17 |
| `_verify_descriptor_acl` | `test_other_execute_bit_is_refused_before_platform_check` | M14 |
| `_descriptor_path` | `test_descriptor_resolver_supplies_the_checked_path` | M15 |
| `_descriptor_path` | `test_resolver_failure_is_refused` | M26 |
| `exclusive_lock` | `test_available_lock_enters_context_at_zero_timeout` | M18 |
| `exclusive_lock` | `test_positive_timeout_allows_a_second_attempt` | M19 |
| `_try_lock` | `test_flock_success_is_reported` | M20 |
| `_unlock` | `test_unlock_releases_the_handle_before_close` | M21 |
| `file_identity` | `test_symlink_identity_is_its_own_name` | M22 |
| `atomic_private_write` | `test_new_record_is_synced_before_replace` | M24 |
| `platform_secure_writer` | `test_writer_enforces_on_its_descriptor` | M25 |

## Survivors left

None. Equivalent mutants: none. Mutants untestable offline: none.

## Defects found

None. No expected failures were added.

## Verification

`test_windows_privacy.py`, `test_windows_privacy_mutation.py` and
`test_onboard.py` passed under an isolated temporary home. The requested
`python3 tests/test_suite.py` passed with 592 checks, zero failures and five
documented environmental skips. `test_windows_semantics.py` passed 51 tests
with three platform skips. `test_local_worker.py`
could not bind its loopback test server in this OS sandbox (`PermissionError:
[Errno 1] Operation not permitted`); its 14 setup errors do not involve the
mutated module. These runs do not claim live Windows ACL validation.
