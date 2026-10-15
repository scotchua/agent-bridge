# Mutation sweep: `src/agent_bridge/peer_mcp.py`

## Method and result

The temporary standard-library runner generated **30 single-change mutants** from executable source only. It covered `RoomClient.__init__`, `registered_participants`, `_request`, `NoRedirect.redirect_request`, `prepare`, `status`, `read`, `schema`, `PeerServer.__init__`, `handle`, `call_tool`, and `main`. Mutations used comparison flips, `and`/`or` swaps, negated conditions, numeric changes by one, empty or `None` returns, one removed statement, `+`/`-` swap, and dropped raises. Comments, logging, and docstrings were excluded. The source was compiled into an in-memory module for each subprocess run; the production file was never edited. Every mutant run had a 12-second limit; none timed out.

The denominator is the 30 generated mutants. The before phase ran `test_peer_mcp.MCPTests` (18 inherited and direct tests). The after phase also ran `test_peer_mcp_mutation.PeerMCPMutationTests` (15 new tests).

| Phase | Killed | Survived | Kill rate |
| --- | ---: | ---: | ---: |
| Before new tests | 7 | 23 | 23.3% |
| After new tests | 29 | 1 | 96.7% |

The remaining mutant is equivalent for JSON tool requests, so all 29 behavior-changing mutants were killed. The final size-limit assertion was rerun individually against M09 and changed its after result from survived to killed. No survivor was classified as untestable offline or as a defect.

## Mutants

| ID | Function | Change | Before | After |
| --- | --- | --- | --- | --- |
| M01 | `RoomClient.__init__` | Empty participant fallback | Survived | Killed |
| M02 | `registered_participants` | Remove private-directory verification | Survived | Killed |
| M03 | `registered_participants` | Change uniqueness `==` to `!=` | Survived | Killed |
| M04 | `registered_participants` | Return `None` | Survived | Killed |
| M05 | `_request` | Exclude port 65535 | Survived | Killed |
| M06 | `_request` | Negate token-map type condition | Survived | Killed |
| M07 | `_request` | Drop invalid-runtime raise | Survived | Killed |
| M08 | `_request` | Swap URL `+` for `-` | Survived | Killed |
| M09 | `_request` | Read 2,000,002 rather than 2,000,001 bytes | Survived | Killed |
| M10 | `_request` | Reject response at exactly 2,000,000 bytes | Survived | Killed |
| M11 | `_request` | Return `None` instead of decoded JSON | Survived | Killed |
| M12 | `prepare` | Return `None` | Survived | Killed |
| M13 | `status` | Return `None` | Survived | Killed |
| M14 | `read` | Swap `or` for `and` in ID validation | Survived | Killed |
| M15 | `read` | Drop invalid-ID raise | Survived | Killed |
| M16 | `read` | Return `None` | Survived | Killed |
| M17 | `schema` | Allow additional properties | Survived | Killed |
| M18 | `PeerServer.__init__` | Negate caller-membership check | Killed | Killed |
| M19 | `PeerServer.__init__` | Include caller in target list | Killed | Killed |
| M20 | `PeerServer.__init__` | Raise minimum target capacity from 1 to 2 | Survived | Killed |
| M21 | `handle` | Swap `and` for `or` in initialize dispatch | Killed | Killed |
| M22 | `handle` | Return `None` for other methods | Killed | Killed |
| M23 | `call_tool` | Swap `or` for `and` in unknown-tool guard | Survived | Survived: equivalent |
| M24 | `call_tool` | Swap `or` for `and` in argument-shape guard | Killed | Killed |
| M25 | `call_tool` | Negate result error condition | Killed | Killed |
| M26 | `call_tool` | Return `None` from exception handler | Killed | Killed |
| M27 | `main` | Flip MCP-action comparison | Survived | Killed |
| M28 | `main` | Reject input at exactly 65,536 characters | Survived | Killed |
| M29 | `main` | Return `None` instead of exit status | Survived | Killed |
| M30 | `NoRedirect.redirect_request` | Return `False` instead of `None` | Survived | Killed |

M09 changes the number of bytes awaited before the size check, so the size-limit test now checks the bounded read amount. M23 is equivalent for JSON tool requests because the later `self.tools[name]` lookup raises on an unknown name and the existing broad exception handler produces the same error content.

## New tests

Each test passed on the original and was run individually against at least the recorded mutant shown here; each individual mutant run failed.

| Function group | Test name | Mutant killed in individual run |
| --- | --- | --- |
| `RoomClient.__init__` | `test_init_uses_registered_fallback` | M01 |
| `registered_participants` | `test_registered_participants_verifies_directory_and_returns_runtime_list` | M02 |
| `registered_participants` | `test_registered_participants_rejects_duplicates` | M03 |
| `_request` / `NoRedirect.redirect_request` | `test_request_uses_local_origin_token_and_parses_response` | M06, M30 |
| `_request` | `test_request_rejects_invalid_runtime_before_transport` | M07 |
| `_request` | `test_request_accepts_upper_port_boundary` | M05 |
| `_request` | `test_request_accepts_response_at_size_limit` | M09, M10 |
| `prepare` / `status` | `test_prepare_and_status_forward_to_request` | M12 |
| `read` | `test_read_rejects_noncanonical_round_id` | M14 |
| `read` | `test_read_forwards_canonical_round_id` | M16 |
| `schema` | `test_schema_disallows_extra_properties` | M17 |
| `PeerServer.__init__` | `test_peer_server_limits_targets_to_other_participants` | M20 |
| `main` | `test_main_status_uses_tool_handler` | M27 |
| `main` | `test_main_accepts_input_at_size_limit` | M28 |
| `main` | `test_main_returns_error_exit_status` | M29 |

## Defects found

None.

## Verification and limits

- `python3 -m unittest discover -s tests -p 'test_peer_mcp_mutation.py' -q`: 15 passed.
- `python3 -m unittest discover -s tests -p 'test_peer_mcp.py' -q`: 32 passed in discovery (including imported inherited tests).
- `python3 tests/test_suite.py`: 593 passed, 0 failed, 5 platform skips.
- `python3 tests/test_onboard.py`: passed with its temporary home.
- `tests/test_peer_http.py` and `tests/test_local_worker.py` were attempted but cannot bind `127.0.0.1` in this sandbox (`PermissionError: [Errno 1] Operation not permitted`); their failures are environmental. The mutation denominator therefore uses the runnable MCP tests and the new mocked tests. No network or API call was made by the mutation runner or new tests.
- A byte comparison with the committed `peer_mcp.py` and `git diff --exit-code` confirmed the production source is unchanged. The temporary mutation runner was removed after the sweep.
