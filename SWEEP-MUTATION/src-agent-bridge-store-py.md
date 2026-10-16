# Mutation sweep: `src/agent_bridge/store.py`

The sweep generated 27 single-edit mutants across all 14 functions. It loaded each
mutated source into a fresh Python subprocess in memory; `store.py` was never
rewritten. Each run had an eight-second timeout. Mutations targeted executable
statements only, including comparisons, Boolean operators, conditions, constants,
return values, statement removal, addition/subtraction, and an exception raise.

The before run used the five existing tests in `tests/test_store.py`. The after
run used those tests plus the 18 tests in `tests/test_store_mutation.py`.

| Measure | Before | After |
| --- | ---: | ---: |
| Generated | 27 | 27 |
| Killed | 4 | 26 |
| Kill rate | 14.8% (4/27) | 96.3% (26/27) |
| Survived | 23 | 1 |

| Function | Mutant ID and edit | Before | After |
| --- | --- | --- | --- |
| `replace` | `replace_and`: `or` to `and` | survived | killed |
| `replace` | `replace_no_raise`: drop `raise` | survived | killed |
| `set_umask` | `umask_no_call`: remove platform call | survived | killed |
| `secure_mkdir` | `mkdir_no_chmod`: remove created-directory chmod | survived | killed |
| `secure_mkdir` | `mkdir_wrong_root_check`: `==` to `!=` | survived | killed |
| `secure_mkdir` | `mkdir_wrong_existing_mode`: `!=` to `==` | survived | killed |
| `secure_mkdir` | `mkdir_return_none`: return `None` | survived | killed |
| `atomic_write_bytes` | `write_negate_owner`: negate owner-only condition | killed | killed |
| `atomic_write_bytes` | `write_no_replace`: remove replace call | killed | killed |
| `atomic_write_json` | `json_no_newline`: omit trailing newline | survived | killed |
| `atomic_write_json` | `json_subtract`: `+` to `-` | survived | killed |
| `read_json` | `read_no_bom`: decode as plain UTF-8 | killed | killed |
| `read_json_atomic` | `atomic_read_none`: return `None` | survived | killed |
| `read_json_atomic` | `atomic_read_gt`: `>=` to `>` | survived | killed |
| `read_json_atomic` | `atomic_read_grace_plus`: grace 1 to 2 seconds | survived | killed |
| `read_json_or_none` | `optional_read_none`: return `None` on valid JSON | killed | killed |
| `read_json_or_none` | `optional_read_no_value_error`: stop catching `ValueError` | survived | killed |
| `file_lock` | `lock_no_close`: remove descriptor close | survived | killed |
| `append_ledger` | `ledger_no_fsync`: remove fsync | survived | killed |
| `append_ledger` | `ledger_no_newline`: omit record delimiter | survived | killed |
| `sha256_bytes` | `hash_bytes_none`: return `None` | survived | killed |
| `sha256_bytes` | `hash_bytes_empty`: return empty string | survived | killed |
| `sha256_text` | `hash_text_none`: return `None` | survived | killed |
| `sha256_text` | `hash_text_false`: return `False` | survived | killed |
| `sha256_file` | `hash_file_no_update`: omit digest update | survived | killed |
| `sha256_file` | `hash_file_chunk_plus`: read 65,537 instead of 65,536 bytes | survived | survived |
| `utc_now` | `utc_none`: return `None` | survived | killed |

## New tests

Each test below passed on the original source and failed individually on at
least the named mutant. They are grouped by function in the new test file.

| Function | Test method | Mutant caught |
| --- | --- | --- |
| `replace` | `ReplaceTests.test_permission_error_on_posix_is_raised_without_retry` | `replace_and`, `replace_no_raise` |
| `set_umask` | `SetUmaskTests.test_set_umask_calls_platform` | `umask_no_call` |
| `secure_mkdir` | `SecureMkdirTests.test_creates_all_missing_parents` | `mkdir_wrong_root_check` |
| `secure_mkdir` | `SecureMkdirTests.test_existing_leaf_is_chmodded_owner_only` | `mkdir_wrong_existing_mode` |
| `secure_mkdir` | `SecureMkdirTests.test_directory_created_by_racer_is_chmodded` | `mkdir_no_chmod` |
| `secure_mkdir` | `SecureMkdirTests.test_returns_absolute_path` | `mkdir_return_none` |
| `atomic_write_json` | `AtomicWriteJsonTests.test_json_write_ends_with_one_newline` | `json_no_newline`, `json_subtract` |
| `read_json_atomic` | `ReadJsonAtomicTests.test_returns_decoded_document` | `atomic_read_none` |
| `read_json_atomic` | `ReadJsonAtomicTests.test_raises_at_grace_deadline` | `atomic_read_gt` |
| `read_json_atomic` | `ReadJsonAtomicTests.test_grace_is_one_second` | `atomic_read_grace_plus` |
| `read_json_or_none` | `ReadJsonOrNoneTests.test_malformed_document_returns_none` | `optional_read_no_value_error` |
| `file_lock` | `FileLockTests.test_lock_descriptor_is_closed` | `lock_no_close` |
| `append_ledger` | `AppendLedgerTests.test_record_is_fsynced` | `ledger_no_fsync` |
| `append_ledger` | `AppendLedgerTests.test_records_are_newline_delimited` | `ledger_no_newline` |
| `sha256_bytes` | `Sha256BytesTests.test_hashes_bytes` | `hash_bytes_none`, `hash_bytes_empty` |
| `sha256_text` | `Sha256TextTests.test_hashes_utf8_text` | `hash_text_none`, `hash_text_false` |
| `sha256_file` | `Sha256FileTests.test_hashes_file_content` | `hash_file_no_update` |
| `utc_now` | `UtcNowTests.test_timestamp_has_utc_timezone_and_milliseconds` | `utc_none` |

## Survivors left

`hash_file_chunk_plus` is equivalent for regular files: SHA-256 receives the
same byte sequence regardless of whether the read chunks are 65,536 or 65,537
bytes. No behavior-changing survivors, offline-untestable survivors, or defects
were found.

## Verification

- `python3 -m unittest discover -s tests -p 'test_store*.py'`: 23 passed.
- `python3 tests/test_suite.py`: 593 passed, 0 failed, 5 environmental skips.
- `python3 tests/test_onboard.py`: 28 passed.
- `python3 tests/test_local_worker.py`: blocked by this sandbox's denial of a
  loopback test-server bind (`PermissionError` in test setup); 14 setup errors.
- The production source remained byte-identical throughout the sweep.

## Defects found

None.
