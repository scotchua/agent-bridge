# Mutation test report: `src/agent_bridge/execution/codex_task.py`

- Source SHA-256 before and after: `fc11a1b3909761c579802cd24b144a0f8c7c53bf78fdc58f5aa6dbd25f901d27` (byte-identical).
- Generated: **32 mutants**, one executable edit in every top-level function; no edits in comments, logging, or docstrings. This is below the 150-mutant cap.
- **Before:** 13/32 killed (**40.625%**); 19/32 survived.
- **After:** 32/32 killed (**100%**); 0/32 survived.
- Each mutant ran in a fresh Python subprocess with a 30-second timeout. The modified module was executed from a temporary copy under `SWEEP-MUTATION`; the production file was never written. The copy was removed after each run.
- Existing offline test selection: `CodexSourceIntegrityTests`, `ErrorDetailTests`, focused `CodexTaskTests` for classification, verification policy, environment, timeout and cleanup, `SpawnDiagnostics.test_a_missing_program_is_named`, `ResolveGit.test_finds_the_git_this_host_actually_has`, and two `SourceIntegrityRegressionTests` covering Codex. This selection passed on the original.
- After pass used that same selection plus `tests.test_codex_task_mutation`. Each new test also passed on the original and failed when run alone against its listed mutant.

| Mutant | Function | Edit | Before | After | New test |
|---|---|---|---|---|---|
| M01 | `_with_error_detail` | `[:ERROR_MESSAGE_DETAIL_LIMIT] -> [:ERROR_MESSAGE_DETAIL_LIMIT+1]` | killed | killed | `—` |
| M02 | `_git_bin` | `if _GIT_BIN is None: -> if _GIT_BIN is not None:` | killed | killed | `—` |
| M03 | `_run` | `if r.timed_out: -> if not r.timed_out:` | killed | killed | `—` |
| M04 | `_spawn_detail` | `if not os.path.exists(program): -> if os.path.exists(program):` | killed | killed | `—` |
| M05 | `_env` | `return e -> return None` | killed | killed | `—` |
| M06 | `_git` | `if r.returncode: -> if not r.returncode:` | killed | killed | `—` |
| M07 | `_git_argv` | `return [str(_git_bin()) -> return [None` | killed | killed | `—` |
| M08 | `_sha` | `return hashlib.sha256(path.read_bytes()).hexdigest() -> return None` | survived | killed | `ShaTests.test_hashes_file_bytes` |
| M09 | `_relevant_paths` | `if kind=="1": -> if kind!="1":` | killed | killed | `—` |
| M10 | `_content_snapshot` | `len(paths)>MAX_SNAPSHOT_FILES -> len(paths)>=MAX_SNAPSHOT_FILES` | survived | killed | `ContentSnapshotTests.test_file_count_at_limit_is_covered` |
| M11 | `_hash_regular_file` | `opened.st_size>MAX_SNAPSHOT_FILE_BYTES -> opened.st_size>=MAX_SNAPSHOT_FILE_BYTES` | survived | killed | `HashRegularFileTests.test_file_size_at_limit_is_hashed` |
| M12 | `_source_state` | `"content":_content_snapshot(repo,env) -> "content":None` | killed | killed | `—` |
| M13 | `_require_confinement` | `try: return hostenv.confinement(classification) -> try: return None` | survived | killed | `RequireConfinementTests.test_returns_selected_backend` |
| M14 | `_atomic_json` | `json.dump(value,f,indent=2,sort_keys=True);  -> ` | survived | killed | `AtomicJsonTests.test_writes_receipt_value` |
| M15 | `_verify_argv` | `if not commands: return [] -> if commands: return []` | killed | killed | `—` |
| M16 | `_auth` | `r.returncode==0 and "logged in using chatgpt" -> r.returncode!=0 and "logged in using chatgpt"` | survived | killed | `AuthTests.test_accepts_successful_chatgpt_status` |
| M17 | `_command` | `if model and model!="default": -> if model or model!="default":` | survived | killed | `CommandTests.test_default_model_does_not_add_override` |
| M18 | `_parse_events` | `if etype=="thread.started": -> if etype!="thread.started":` | survived | killed | `ParseEventsTests.test_thread_started_yields_thread_id` |
| M19 | `_is_cache_artifact` | `if rel.endswith(".pyc"): return True -> if rel.endswith(".pyc"): return False` | survived | killed | `CacheArtifactTests.test_generated_bytecode_is_cache` |
| M20 | `_unlink_beneath` | `if stat.S_ISREG(os.lstat(name,dir_fd=fd).st_mode): -> if not stat.S_ISREG(os.lstat(name,dir_fd=fd).st_mode):` | survived | killed | `UnlinkBeneathTests.test_unlinks_regular_file_only` |
| M21 | `_drop_caches` | `if not _is_cache_artifact(rel): continue -> if _is_cache_artifact(rel): continue` | survived | killed | `DropCachesTests.test_removes_only_reported_cache_artifact` |
| M22 | `_patch` | `if not r.stdout: raise TaskError -> if r.stdout: raise TaskError` | survived | killed | `PatchTests.test_returns_nonempty_diff` |
| M23 | `_remove` | `if not tree.exists(): return True -> if tree.exists(): return True` | killed | killed | `—` |
| M24 | `_pinned_python` | `command[0] in ("python","python3") -> command[0] not in ("python","python3")` | survived | killed | `PinnedPythonTests.test_pins_python3_verification` |
| M25 | `_sandboxed` | `if backend.name==hostenv.LINUX_USERNS: -> if backend.name!=hostenv.LINUX_USERNS:` | survived | killed | `SandboxedTests.test_non_linux_backend_selects_sandbox_exec` |
| M26 | `_netns_confined` | `if result.returncode==hostenv.CONFINEMENT_SELFTEST_EXIT: -> if result.returncode!=hostenv.CONFINEMENT_SELFTEST_EXIT:` | survived | killed | `NetnsConfinedTests.test_successful_canary_check_returns_verification_result` |
| M27 | `_confinement_canaries` | `if text.startswith("gitdir: "): -> if not text.startswith("gitdir: "):` | survived | killed | `ConfinementCanariesTests.test_linked_worktree_adds_common_repository_parent` |
| M28 | `_sandbox_exec_confined` | `if command[0]=="git": command=_git_argv(*command[1:]) -> if command[0]!="git": command=_git_argv(*command[1:])` | survived | killed | `SandboxExecConfinedTests.test_git_verification_uses_pinned_git_argv` |
| M29 | `_assert_no_ancestor_contamination` | `preflight.assert_workspace_clean(str(job)) -> pass` | survived | killed | `AncestorContaminationTests.test_checks_ancestor_instructions` |
| M30 | `run_task` | `return _run_admitted(codex_bin=admission.exec_path,admission=admission,**kwargs) -> return None` | killed | killed | `—` |
| M31 | `_run_admitted` | `if classification not in ALLOWED_CLASSIFICATIONS and not ( -> if classification not in ALLOWED_CLASSIFICATIONS or not (` | survived | killed | `RunAdmittedTests.test_synthetic_classification_reaches_path_validation` |
| M32 | `main` | `return 0 if ok else 3 -> return 3 if ok else 0` | killed | killed | `—` |

## Survivors left

None. Equivalent: 0; untestable offline: 0; defects: 0.

## Defects found

None.

## Verification and host limits

- `python3 tests/test_suite.py`: 593 passed, 0 failed, 5 environment skips.
- Focused original test selection plus 19 new tests: 48 passed, 1 skipped. The skipped source-integrity case requires a device fixture unavailable on this host.
- `python3 -B -m unittest tests.test_onboard -q`: 28 passed.
- The broader `tests.test_codex_task` class has 7 baseline failures here because `sandbox-exec` returns `sandbox_apply: Operation not permitted`. It was excluded from the mutant kill denominator.
- `tests.test_local_worker` has 14 baseline errors because this sandbox refuses its loopback socket bind. No live inference or network call was made by the mutation tests.
