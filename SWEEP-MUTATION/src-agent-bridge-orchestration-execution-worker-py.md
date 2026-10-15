# Mutation report: `src/agent_bridge/orchestration/execution_worker.py`

## Method and result

The runner compiled each single-edit mutant in an in-memory module and ran the relevant unittest module in a separate Python process with a 12-second timeout. It used no packages, external services, or live inference. Mutations were selected from executable statements across every function, including the nested callbacks in `run`. Comments, logging, and docstrings were excluded. The source file was never written; its SHA-256 remained `c0cdd515d73df80d45b079fceb123a663e89a99f541773a191a4c7381d8086b2` after both sweeps.

| Phase | Tests | Mutants killed | Kill rate | Survived |
| --- | ---: | ---: | ---: | ---: |
| Existing worker tests | 23 | 12 / 38 | 31.6% | 26 |
| Existing and new worker tests | 34 | 38 / 38 | 100% | 0 |

No eligible arithmetic `+` or `-` operator occurs in the executable code, so that mutation category generated no case. The sweep did include condition flips, `or` to `and`, a numeric increment, replacement return values, removed statements, and a dropped exception raise. `run_once_flip` was killed by its 12-second timeout; the other after-phase kills were test failures or errors.

## Mutants generated

| Function | Mutant | Single change | Before | After |
| --- | --- | --- | --- | --- |
| `WorkerLock.__init__` | `init_fd_zero` | `self._fd: int \| None = None → self._fd: int \| None = 0` | survived | killed |
| `WorkerLock.__init__` | `init_platform_flip` | `platform if platform is not None else host_platform → platform if platform is None else host_platform` | killed | killed |
| `WorkerLock.__enter__` | `enter_no_mkdir` | `self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True) → pass` | killed | killed |
| `WorkerLock.__enter__` | `enter_mode_plus_one` | `fd = os.open(self.path, flags, 0o600) → fd = os.open(self.path, flags, 0o601)` | survived | killed |
| `WorkerLock.__enter__` | `enter_no_verify` | `self._verify_queue_root(fd) → pass` | killed | killed |
| `WorkerLock.__enter__` | `enter_no_lock` | `release.__enter__() → pass` | killed | killed |
| `WorkerLock.__enter__` | `enter_no_seek` | `os.lseek(fd, 0, os.SEEK_SET) → pass` | survived | killed |
| `WorkerLock.__enter__` | `enter_no_truncate` | `os.ftruncate(fd, written) → pass` | survived | killed |
| `WorkerLock.__enter__` | `enter_no_fsync` | `os.fsync(fd) → pass` | survived | killed |
| `WorkerLock.__enter__` | `enter_no_fd_save` | `self._fd = fd → pass` | survived | killed |
| `WorkerLock.__enter__` | `enter_return_none` | Return `None` instead of `self`. | survived | killed |
| `WorkerLock._verify_queue_root` | `verify_missing_flip` | `if verify is None: → if verify is not None:` | killed | killed |
| `WorkerLock._verify_queue_root` | `verify_no_raise` | Drop the `QueueNotPrivate` raise when verification is unavailable. | killed | killed |
| `WorkerLock._verify_queue_root` | `verify_no_call` | `verified, _evidence = verify(str(self.path.parent), str(self.path)) → verified, _evidence = True, {}` | killed | killed |
| `WorkerLock._verify_queue_root` | `verify_result_flip` | `if not verified: → if verified:` | killed | killed |
| `WorkerLock.__exit__` | `exit_fd_flip` | `if self._fd is not None: → if self._fd is None:` | survived | killed |
| `WorkerLock.__exit__` | `exit_no_unlock` | `self._release.__exit__(None, None, None) → pass` | survived | killed |
| `WorkerLock.__exit__` | `exit_no_close` | `os.close(self._fd) → pass` | survived | killed |
| `WorkerLock.__exit__` | `exit_no_reset` | `self._fd = None → pass` | survived | killed |
| `select_executor` | `select_os_flip` | `if name == "nt": → if name != "nt":` | killed | killed |
| `select_executor` | `select_windows_missing_flip` | `if cfg.windows_wsl_runtime_root is None: → if cfg.windows_wsl_runtime_root is not None:` | killed | killed |
| `select_executor` | `select_posix_any_all` | `if any(value is None for value in required): → if all(value is None for value in required):` | survived | killed |
| `select_executor` | `select_review_parent_plus_one` | `Path(__file__).resolve().parents[1] / "execution" → Path(__file__).resolve().parents[2] / "execution"` | survived | killed |
| `_configured_queue` | `configured_no_recovery` | `recover_interrupted=True, → recover_interrupted=False,` | survived | killed |
| `_configured_queue` | `configured_no_queue` | `return ExecutionQueue( → return None if True else ExecutionQueue(` | killed | killed |
| `run` | `run_interval_boundary` | `if interval <= 0: → if interval < 0:` | survived | killed |
| `run.request_stop` | `run_no_stop` | `stop = True → stop = False` | survived | killed |
| `run` | `run_identity_and` | `identity = worker_id or f → identity = worker_id and f` | survived | killed |
| `run.run_checked_once` | `run_no_changed` | `changed = set(status["changed"]) → changed = set()` | survived | killed |
| `run.run_checked_once` | `run_no_attention` | `drift.report_attention(None, status) → pass` | survived | killed |
| `run.run_checked_once` | `run_codex_flip` | `if "codex" in changed: → if "codex" not in changed:` | survived | killed |
| `run.run_checked_once` | `run_claude_flip` | `if "claude" in changed: → if "claude" not in changed:` | survived | killed |
| `run.run_checked_once` | `run_no_holds` | `queue.hold_dependencies(holds) → pass` | survived | killed |
| `run.run_checked_once` | `run_no_dispatch` | `return queue.run_once(identity) → return None` | survived | killed |
| `run` | `run_once_flip` | `if once: → if not once:` | killed | killed |
| `run` | `run_no_term_restore` | `signal.signal(signal.SIGTERM, previous_term) → pass` | survived | killed |
| `main` | `main_return_none` | Return `None` instead of the result of `run`. | survived | killed |
| `main` | `main_error_zero` | `return 2 → return 0` | survived | killed |

## New tests

- `ConfiguredQueueMutationTests.test_configured_queue_recovers_after_lock_acquisition` — fails on `configured_no_recovery`.
- `MainMutationTests.test_main_returns_error_status` — fails on `main_error_zero`.
- `MainMutationTests.test_main_returns_run_status` — fails on `main_return_none`.
- `RunMutationTests.test_once_reports_drift_holds_and_dispatches_under_worker_id` — fails on `run_identity_and`.
- `RunMutationTests.test_stop_signal_ends_loop_and_restores_handlers` — fails on `run_no_stop`.
- `RunMutationTests.test_zero_interval_is_refused_before_loading_configuration` — fails on `run_interval_boundary`.
- `SelectExecutorMutationTests.test_one_missing_posix_executable_refuses` — fails on `select_posix_any_all`.
- `SelectExecutorMutationTests.test_posix_review_script_comes_from_the_package` — fails on `select_review_parent_plus_one`.
- `WorkerLockMutationTests.test_enter_returns_the_lock_and_exit_releases_and_closes` — fails on `exit_no_unlock`.
- `WorkerLockMutationTests.test_enter_uses_private_mode_and_commits_pid_record` — fails on `enter_mode_plus_one`.
- `WorkerLockMutationTests.test_new_lock_has_no_descriptor_to_close` — fails on `init_fd_zero`.

## Survivors and defects

Survivors left: none. Equivalent mutants: none. Untestable-offline mutants: none. Defects found: none; no expected-failure tests were needed.

## Verification

- `python3 -m unittest discover -s tests -p "test_execution_worker*.py"`: 34 tests passed on the original source.
- `python3 tests/test_suite.py`: 593 passed, 0 failed, 5 environmental skips.
- `python3 -m unittest tests.test_onboard tests.test_hostenv tests.test_automatic_gate tests.test_delegation_audit`: 269 ran, 0 failures, 11 skipped.
- Additional onboarding, local-worker, host-environment, gate, audit, and end-to-end unittest modules were attempted. Their combined run had 56 setup errors because this sandbox denied binding a loopback HTTP server (`PermissionError: [Errno 1]`); those errors are unrelated to the worker mutations. No external network request was made.
