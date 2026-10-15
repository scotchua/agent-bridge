# Mutation test: `src/agent_bridge/chat/server.py`

## Method and results

The throwaway runner generated 31 single-edit mutants across `create_server`, `round_caller`, `participants`, `Handler.send`, and `Handler.handle_request`. It changed one copy of the module in memory per isolated Python process, ran each process with an 8-second limit, and checked that the production file was byte-identical afterward. Mutations covered comparison flips, `and`/`or` swaps, condition negations, numeric off-by-one, return replacement, statement removal, `+`/`-` swap, and a dropped raise. Comments, docstrings, and logging were excluded. `Handler.log_message` is logging only and was excluded. No packages or network access were used.

- Before: **12/31 killed (38.7%)**. Baseline: `test_chat_http.HTTPPolicyTests.test_room_history_classification_restricts_recipient_before_save`.

- After: **31/31 killed (100.0%)**. Baseline plus the new offline `test_chat_server_mutation` tests.

| Mutant | Before | After |
| --- | --- | --- |
| `policy_fallback_and` | killed | killed |
| `participant_fallback_and` | killed | killed |
| `daemon_statement_removed` | survived | killed |
| `round_token_condition_negated` | killed | killed |
| `round_caller_none` | survived | killed |
| `legacy_caller_none` | survived | killed |
| `cache_threshold_ge` | survived | killed |
| `cache_age_plus` | survived | killed |
| `cache_result_none` | survived | killed |
| `adapter_condition_negated` | survived | killed |
| `json_type_comparison_flipped` | survived | killed |
| `nosniff_header_removed` | survived | killed |
| `host_comparison_flipped` | killed | killed |
| `static_and_or` | survived | killed |
| `api_condition_negated` | killed | killed |
| `authorization_and_or` | killed | killed |
| `peer_client_condition_negated` | survived | killed |
| `post_origin_comparison_flipped` | killed | killed |
| `body_limit_minus_one` | survived | killed |
| `json_content_type_flipped` | killed | killed |
| `json_object_condition_negated` | killed | killed |
| `round_path_length_flipped` | survived | killed |
| `caller_binding_flipped` | survived | killed |
| `approval_keys_flipped` | survived | killed |
| `approval_raise_dropped` | survived | killed |
| `recipient_list_condition_negated` | killed | killed |
| `adapter_none_condition_negated` | killed | killed |
| `ready_comparison_flipped` | killed | killed |
| `delete_confirmation_negated` | survived | killed |
| `delete_round_statement_removed` | survived | killed |
| `reset_target_condition_negated` | survived | killed |

## Survivors left

- Equivalent: none.
- Untestable offline: none among generated mutants.
- Defect: none.

## New tests

### `CreateServerTests`

- `CreateServerTests.test_loopback_binding_and_daemon_threads`
- `CreateServerTests.test_round_participants_take_precedence`

### `RoundCallerTests`

- `RoundCallerTests.test_dictionary_token_binds_caller`
- `RoundCallerTests.test_legacy_token_can_access_peer_status`

### `ParticipantsTests`

- `ParticipantsTests.test_status_uses_adapter_and_unavailable_fallback`
- `ParticipantsTests.test_participant_status_is_cached_until_expiry`

### `SendTests`

- `SendTests.test_json_response_has_security_headers_and_bytes`

### `HandleRequestTests`

- `HandleRequestTests.test_host_and_origin_are_checked`
- `HandleRequestTests.test_static_path_and_unknown_path`
- `HandleRequestTests.test_post_requires_valid_size_type_and_object`
- `HandleRequestTests.test_post_accepts_body_at_size_limit`
- `HandleRequestTests.test_round_path_and_approval_body_are_validated`
- `HandleRequestTests.test_room_actions_reject_bad_recipients_and_unready_peer`
- `HandleRequestTests.test_delete_requires_literal_true_and_resets_known_target`

## Execution limits

The existing `HTTPTests` and `PeerHTTPTests` require binding a loopback socket; this sandbox returns `PermissionError: [Errno 1] Operation not permitted` at setup. The baseline therefore uses the existing handler-level policy test, and the added tests exercise handlers directly with an in-memory server. The before rate describes that runnable baseline, not every existing HTTP test. `tests/test_local_worker.py` has the same socket restriction.

Every new test was run alone against a selected recorded mutant and failed; all 14 pass on the original. `python3 tests/test_suite.py` passed with 593 checks, 0 failures, and 5 environmental skips. `python3 tests/test_onboard.py` passed 28 tests. `python3 tests/test_local_worker.py` could not run its 14 tests because its mock server could not bind a loopback socket.
