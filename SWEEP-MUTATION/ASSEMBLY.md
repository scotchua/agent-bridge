# Round 2 mutation patch assembly

Base commit: `d0617cf94611a86ba510e4e63449d380caad2aa9`.

| Module | Patch job ID | Status | Reason |
| --- | --- | --- | --- |
| `src/agent_bridge/orchestration/gate.py` | `20261013T233834Z-975dde` | Applied | Applied cleanly. |
| `src/agent_bridge/orchestration/guest_runner.py` | `20261013T235715Z-167dad` | Applied | Applied cleanly. |
| `src/agent_bridge/orchestration/windows_wsl_runtime.py` | `20261014T001132Z-8a3d6d` | Applied | Applied cleanly. |
| `src/agent_bridge/onboard.py` | `20261014T002030Z-166eac` | Applied | Applied cleanly. |
| `src/agent_bridge/orchestration/windows_delegation.py` | `20261014T002943Z-88a37c` | Applied | Applied cleanly. |
| `src/agent_bridge/platform/windows.py` | `20261014T004004Z-e6b9c2` | Applied | Applied cleanly. |
| `src/agent_bridge/orchestration/windows_provision_driver.py` | `20261014T005546Z-24a0f5` | Applied | Applied cleanly. |
| `src/agent_bridge/orchestration/localfirst.py` | `20261014T010639Z-056560` | Applied | Applied cleanly. |
| `src/agent_bridge/orchestration/mcp.py` | `20261014T011428Z-ebb22f` | Applied | Applied cleanly. |
| `src/agent_bridge/drift.py` | `20261014T012304Z-c6f8da` | Applied | Applied cleanly. |
| `src/agent_bridge/broker.py` | `20261014T013152Z-a7b46a` | Applied | Applied cleanly. |
| `src/agent_bridge/orchestration/autoroute.py` | `20261014T014353Z-594d04` | Applied | Applied cleanly. |
| `src/agent_bridge/orchestration/windows_wsl_provision.py` | `20261014T015033Z-222eb5` | Applied | Applied cleanly. |
| `src/agent_bridge/orchestration/windows_rootfs.py` | `20261014T020033Z-302ee0` | Applied | Applied cleanly. |
| `src/agent_bridge/execution/codex_task.py` | `20261014T020816Z-dcd39c` | Applied | Applied cleanly. |
| `src/agent_bridge/execution/claude_task.py` | `20261014T021739Z-cbdb6c` | Applied | Applied cleanly. |
| `src/agent_bridge/localq/spool.py` | `20261014T024044Z-101271` | Applied | Applied cleanly. |
| `src/agent_bridge/orchestration/execution_queue.py` | `20261014T025025Z-972fbd` | Applied | Applied cleanly. |
| `src/agent_bridge/orchestration/windows_validation.py` | `20261014T030118Z-f543c3` | Applied | Applied cleanly. |
| `src/agent_bridge/orchestration/windows_wsl.py` | `20261014T031238Z-6ef398` | Applied | Applied cleanly. |
| `src/agent_bridge/registry.py` | `20261014T032053Z-d0620b` | Applied | Applied cleanly. |
| `src/agent_bridge/orchestration/audit.py` | `20261014T032942Z-e7e0b9` | Applied | Applied cleanly. |
| `src/agent_bridge/orchestration/windows_auth.py` | `20261014T034024Z-6e2f7e` | Applied | Applied cleanly. |
| `src/agent_bridge/orchestration/delegation_verify.py` | `20261014T034909Z-5e2a75` | Applied | Applied cleanly. |
| `src/agent_bridge/localq/intake.py` | `20261014T035815Z-d207fd` | Applied | Applied cleanly. |
| `src/agent_bridge/windows_setup.py` | `20261014T041230Z-ed4c59` | Applied | Applied cleanly. |
| `src/agent_bridge/orchestration/autodecide.py` | `20261014T041938Z-5c6c42` | Applied | Applied cleanly. |
| `src/agent_bridge/orchestration/windows_privacy.py` | `20261014T042720Z-309e28` | Applied | Applied cleanly. |
| `src/agent_bridge/admin.py` | `20261014T043438Z-18687f` | Applied | Applied cleanly. |
| `src/agent_bridge/platform/windows_acl.py` | `20261014T044046Z-80bb4d` | Applied | Applied cleanly. |
| `src/agent_bridge/upgrade.py` | `20261014T044701Z-ab45e4` | Applied | Applied cleanly. |
| `src/agent_bridge/worker.py` | `20261014T045253Z-939f78` | Applied | Applied; merged worker calls with the registry suite registration. |
| `src/agent_bridge/execution/codex_review.py` | `20261014T050257Z-596b99` | Applied | Applied cleanly. |
| `src/agent_bridge/orchestration/delegation.py` | `20261014T051549Z-7a5ed2` | Applied | Applied cleanly. |
| `src/agent_bridge/localq/gemma_child.py` | `20261014T052235Z-33580b` | Applied | Applied cleanly. |
| `src/agent_bridge/mcp_server.py` | `20261014T053109Z-dfd66b` | Applied | Applied cleanly. |
| `src/agent_bridge/orchestration/windows_evidence.py` | `20261014T053918Z-114ef3` | Applied | Applied cleanly. |
| `src/agent_bridge/capacity_router.py` | `20261014T054724Z-4acdec` | Applied | Applied cleanly. |
| `src/agent_bridge/orchestration/windows_preflight.py` | `20261014T055545Z-505014` | Applied | Applied cleanly. |
| `src/agent_bridge/execution/hostenv.py` | `20261014T060532Z-1d8ac9` | Applied | Applied cleanly. |
| `src/agent_bridge/execution/codex_promotion.py` | `20261014T061407Z-496aaa` | Applied | Applied cleanly. |
| `src/agent_bridge/config.py` | `20261014T063220Z-ba639f` | Applied | Applied cleanly. |
| `src/agent_bridge/orchestration/windows_activation.py` | `20261014T064148Z-3308b3` | Applied | Applied cleanly. |
| `src/agent_bridge/orchestration/output_router.py` | `20261014T065128Z-2662e5` | Applied | Applied cleanly. |
| `src/agent_bridge/execution/claude_config.py` | `20261014T070143Z-99c2c3` | Applied | Applied cleanly. |
| `src/agent_bridge/platform/posix.py` | `20261014T071138Z-1afc6e` | Applied | Applied cleanly. |
| `src/agent_bridge/local_worker.py` | `20261014T072647Z-40e39b` | Applied | Applied cleanly. |
| `src/agent_bridge/runner.py` | `20261014T073545Z-c86cc3` | Applied | Applied cleanly. |
| `src/agent_bridge/orchestration/config.py` | `20261014T074706Z-ebe41b` | Applied | Applied cleanly. |
| `src/agent_bridge/preflight.py` | `20261014T075632Z-6ba5ca` | Applied | Applied cleanly. |
| `src/agent_bridge/errors.py` | `20261014T080705Z-d9c8f6` | Applied | Applied cleanly. |
| `src/agent_bridge/backends/codex_backend.py` | `20261014T081416Z-c91e66` | Applied | Applied; merged Codex backend registration with registry and worker calls. |
| `src/agent_bridge/backends/claude_backend.py` | `20261014T082549Z-e9354c` | Applied | Applied cleanly. |
| `src/agent_bridge/orchestration/signing.py` | `20261014T083610Z-6452a6` | Applied | Applied cleanly. |
| `src/agent_bridge/orchestration/execution_worker.py` | `20261014T084607Z-161be4` | Applied | Applied cleanly. |
| `src/agent_bridge/store.py` | `20261014T090130Z-ebafd1` | Applied | Applied cleanly. |
| `src/agent_bridge/chat/rounds.py` | `20261014T091202Z-dd35be` | Applied | Applied cleanly. |
| `src/agent_bridge/chat/storage.py` | `20261014T092120Z-b1ace3` | Applied | Applied cleanly. |
| `src/agent_bridge/orchestration/server.py` | `20261014T093228Z-b4e9b1` | Applied | Applied cleanly. |
| `src/agent_bridge/chat/grok.py` | `20261014T094230Z-af9a1f` | Applied | Applied cleanly. |
| `src/agent_bridge/chat/hermes.py` | `20261014T095239Z-6c0101` | Applied | Applied cleanly. |
| `src/agent_bridge/localq/client_data_health.py` | `20261014T100435Z-c3b1df` | Applied | Applied cleanly. |
| `src/agent_bridge/schema_validate.py` | `20261014T102543Z-658f82` | Applied | Applied cleanly. |
| `src/agent_bridge/chat/server.py` | `20261014T103437Z-50e685` | Applied | Applied cleanly. |
| `src/agent_bridge/health.py` | `20261014T104236Z-12a2f1` | Applied | Applied; retained imports required by both health test patches. |
| `src/agent_bridge/peer_mcp.py` | `20261014T105043Z-59eeaa` | Applied | Applied cleanly. |
| `src/agent_bridge/localq/runtime.py` | `20261014T105906Z-c1d2f8` | Applied | Applied cleanly. |
| `src/agent_bridge/chat/__main__.py` | `20261014T110559Z-cff93c` | Applied | Applied cleanly. |
| `src/agent_bridge/localq/server.py` | `20261014T111524Z-20b364` | Applied | Applied cleanly. |
| `src/agent_bridge/backends/base.py` | `20261014T112512Z-3353c7` | Applied | Applied cleanly. |
| `src/agent_bridge/chat/dispatch.py` | `20261014T113102Z-c898e2` | Applied | Applied cleanly. |
| `src/agent_bridge/platform/base.py` | `20261014T114305Z-842ef5` | Applied | Applied cleanly. |
| `src/agent_bridge/localq/backend_select.py` | `20261014T115058Z-eb3dc9` | Applied | Applied cleanly. |
| `src/agent_bridge/localq/service.py` | `20261014T115758Z-ccd294` | Applied | Applied cleanly. |
| `src/agent_bridge/localq/worker_child.py` | `20261014T120653Z-303553` | Applied | Applied cleanly. |
| `src/agent_bridge/execution/verify_policy.py` | `20261014T121339Z-eb2ad1` | Applied | Applied cleanly. |
| `src/agent_bridge/envelope.py` | `20261014T122033Z-aa7da5` | Applied | Applied cleanly. |
| `src/agent_bridge/localq/mcp.py` | `20261014T122724Z-f743c8` | Applied | Applied cleanly. |
| `src/agent_bridge/token_usage.py` | `20261014T123417Z-f1f91e` | Applied | Applied cleanly. |
| `src/agent_bridge/provenance.py` | `20261014T124126Z-c592cf` | Applied | Applied cleanly. |
| `src/agent_bridge/__init__.py` | `20261014T134937Z-352464` | Applied | Applied cleanly. |
| `src/agent_bridge/chat/policy.py` | `20261014T135536Z-89763b` | Applied | Applied cleanly. |
| `src/agent_bridge/chat/adapters.py` | `20261014T140312Z-18c4ae` | Applied | Applied cleanly. |
| `src/agent_bridge/receipts.py` | `20261014T141748Z-e5d683` | Applied | Applied; merged receipt registration with registry, Codex backend, and worker calls. |

## Verification

- Final suite command: `python3 tests/test_suite.py`
- Final suite result: 610 passed, 0 failed, 5 environmental skips.
- Baseline suite before patches: 593 passed, 0 failed, 5 environmental skips.
- Patch-touched test files: 81 passed when run individually with isolated temporary homes and `PYTHONPATH=src` where required.
- Production files changed: none.
- Patches skipped: 0 of 84.
