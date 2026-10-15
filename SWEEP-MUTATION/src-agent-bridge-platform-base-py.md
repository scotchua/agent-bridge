# Mutation sweep: `src/agent_bridge/platform/base.py`

The source is a `NamedTuple` result and a `Protocol` interface. It has no
comparisons, Boolean expressions, numeric constants, arithmetic, explicit
returns, or exception raises in executable code, so those requested mutation
operators had no eligible sites. The sweep used one-statement removal and
single contract edits instead. Comments, logging, and docstrings were not
mutated. All 15 `Platform` methods were covered by a removal mutant.

The throwaway runner loaded one changed copy of the module from a temporary
file under its normal import name for each subprocess. It ran
`test_suite.test_platform_boundary` and
`test_windows_stream_security` with a 15-second limit per mutant. The after
pass also ran `test_platform_base_contract`. No mutant timed out or required
network access. The source file was checked byte-for-byte after each sweep;
its SHA-256 was
`da82aa91f9ebab211f5ad90fc5ab171ea24cd86f4b4827939ee8d8cc759e4761`.

| Measure | Before | After |
| --- | ---: | ---: |
| Mutants generated | 24 | 24 |
| Killed | 1 | 23 |
| Survived | 23 | 1 |
| Raw kill rate | 1/24 (4.17%) | 23/24 (95.83%) |
| Kill rate excluding the equivalent mutant | 1/23 (4.35%) | 23/23 (100%) |

## Mutants and new tests

Each new test below passes on the original module and fails on its listed
mutant; all 22 one-test/one-mutant pairs were also run independently.
`field_remove_cap_exceeded` was already killed by
`test_suite.test_platform_boundary`.

| Mutant | Before | After | New test |
| --- | --- | --- | --- |
| `field_type_timed_out` | survived | killed | `StreamReadResultContractTests.test_timed_out_is_boolean` |
| `field_remove_cap_exceeded` | killed | killed | none needed |
| `field_type_cap_exceeded` | survived | killed | `StreamReadResultContractTests.test_cap_exceeded_is_boolean` |
| `field_type_descendant_held_pipes` | survived | killed | `StreamReadResultContractTests.test_descendant_held_pipes_is_boolean` |
| `protocol_to_object` | survived | killed | `PlatformContractTests.test_platform_is_a_protocol` |
| `attribute_type_name` | survived | killed | `PlatformContractTests.test_name_type` |
| `attribute_type_supports_owner_only_permissions` | survived | killed | `PlatformContractTests.test_owner_only_support_type` |
| `remove_set_owner_only_umask` | survived | killed | `PlatformContractTests.test_set_owner_only_umask` |
| `equivalent_explicit_none` | survived | survived | equivalent; the protocol stub returns `None` either way |
| `remove_enforce_owner_only_file` | survived | killed | `PlatformContractTests.test_enforce_owner_only_file` |
| `remove_verify_owner_only_path` | survived | killed | `PlatformContractTests.test_verify_owner_only_path` |
| `remove_lock_exclusive` | survived | killed | `PlatformContractTests.test_lock_exclusive` |
| `remove_lock_contextmanager` | survived | killed | `PlatformContractTests.test_lock_exclusive_is_context_manager` |
| `remove_spawn_isolated` | survived | killed | `PlatformContractTests.test_spawn_isolated` |
| `remove_isolated_process_group` | survived | killed | `PlatformContractTests.test_isolated_process_group` |
| `remove_terminate_process_tree` | survived | killed | `PlatformContractTests.test_terminate_process_tree` |
| `remove_process_liveness` | survived | killed | `PlatformContractTests.test_process_liveness` |
| `remove_process_alive` | survived | killed | `PlatformContractTests.test_process_alive` |
| `remove_process_tree_alive` | survived | killed | `PlatformContractTests.test_process_tree_alive` |
| `remove_process_group_members` | survived | killed | `PlatformContractTests.test_process_group_members` |
| `remove_process_identity` | survived | killed | `PlatformContractTests.test_process_identity` |
| `remove_process_group_identity` | survived | killed | `PlatformContractTests.test_process_group_identity` |
| `remove_is_own_process_group` | survived | killed | `PlatformContractTests.test_is_own_process_group` |
| `remove_read_streams_with_caps` | survived | killed | `PlatformContractTests.test_read_streams_with_caps` |

## Survivors left

`equivalent_explicit_none` replaces the ellipsis in
`set_owner_only_umask` with an explicit `return None`. Calling the unmodified
stub also returns `None`, and the protocol member and signature remain the
same. There are no offline-untestable survivors.

## Defects found

None. No expected failures were added.

## Verification

`python3 -m unittest discover -s tests -p test_platform_base_contract.py -q`:
22 tests passed. `python3 tests/test_suite.py`: 592 passed, 0 failed, 5
environmental skips, using an isolated temporary home.
`python3 tests/test_onboard.py` passed using that isolated home.
`python3 tests/test_local_worker.py` was attempted there, but all 14 tests
errored when the sandbox refused their local socket binds with
`PermissionError: [Errno 1] Operation not permitted`.
