# Mutation hardening: `src/agent_bridge/localq/spool.py`

## Method

42 one-change mutants were generated from executable code, with at least one in each concrete function or method. The changes include comparison flips, a Boolean swap, a negated condition, a numeric boundary change, `None`/`False` returns, a removed statement, a `+`/`-` swap, and a dropped raise. Comments, logging, and docstrings were excluded. Each mutant was written to a temporary copy and imported through a subprocess finder; the production module was never edited. Each subprocess had a 25-second limit. Existing tests were `test_localq`, `test_localq_intake`, plus `test_checkpoint` for blob mutants and `test_localq_client_data_health` for health and process-group mutants. All baseline selections passed against the original module.

## Kill rates

| Stage | Killed | Generated | Kill rate |
| --- | ---: | ---: | ---: |
| Existing tests | 27 | 42 | 64.3% |
| Existing plus new tests | 42 | 42 | 100.0% |

## Mutants

| ID | Function / line | Change | Before | After / new killer |
| --- | --- | --- | --- | --- |
| M01 | `QueueCaps.__post_init__:94` | `value <= 0 => value < 0` | killed | killed (existing tests) |
| M02 | `_fixed_child_detail:152` | `return True => return False` | killed | killed (existing tests) |
| M03 | `_fixed_child_detail:154` | `< 16 => <= 16` | survived | killed (test_fixed_child_detail_rejects_document_index_sixteen) |
| M04 | `_ChildFailure.__init__:173` | ` + _child_failure_suffix =>  - _child_failure_suffix` | killed | killed (existing tests) |
| M05 | `_failure_text:183` | `else "backend_failure" => else None` | survived | killed (test_failure_text_hides_unrecognized_backend_exception) |
| M06 | `FakeBackend.__init__:190` | `self.fn = fn or => self.fn = fn and` | killed | killed (existing tests) |
| M07 | `FakeBackend.run:195` | `return self.fn(payload) => return None` | killed | killed (existing tests) |
| M08 | `_child_failure_suffix:209` | `<= FAILURE_TEXT_LIMIT => < FAILURE_TEXT_LIMIT` | survived | killed (test_child_failure_suffix_keeps_recognized_detail_at_text_limit) |
| M09 | `SubprocessBackend.__init__:224` | `raise ValueError => pass  # raise ValueError` | survived | killed (test_constructor_rejects_empty_command) |
| M10 | `SubprocessBackend.run:248` | `proc.returncode != 0 => proc.returncode == 0` | killed | killed (existing tests) |
| M11 | `SubprocessBackend._terminate_group:276` | `os.killpg(proc.pid, sig) => pass` | killed | killed (existing tests) |
| M12 | `SubprocessBackend.cancel:287` | `return True => return False` | survived | killed (test_cancel_reports_that_a_live_child_was_signalled) |
| M13 | `LocalQueue.__init__:319` | `self._init_db() => pass` | killed | killed (existing tests) |
| M14 | `LocalQueue._connect:324` | `return conn => return None` | killed | killed (existing tests) |
| M15 | `LocalQueue._db:330` | `yield db => yield None` | killed | killed (existing tests) |
| M16 | `LocalQueue._init_db:338` | `db.execute("""CREATE TABLE => db.execute("""CREATE INDEX` | killed | killed (existing tests) |
| M17 | `LocalQueue._normalized:352` | `sort_keys=True => sort_keys=False` | killed | killed (existing tests) |
| M18 | `LocalQueue._blob:382` | `return digest => return None` | killed | killed (existing tests) |
| M19 | `LocalQueue._existing_blob_digest:393` | `return hashlib.sha256(target.read_bytes()).hexdigest() => return None` | killed | killed (existing tests) |
| M20 | `LocalQueue._read_blob:400` | `return json.loads => return None  # json.loads` | killed | killed (existing tests) |
| M21 | `LocalQueue._admit:410` | `age > self.caps.sample_max_age_seconds => age < self.caps.sample_max_age_seconds` | killed | killed (existing tests) |
| M22 | `LocalQueue._admit:410` | `age < -1 => age <= -1` | survived | killed (test_private_admit_accepts_sample_exactly_one_second_ahead) |
| M23 | `LocalQueue.admit:433` | `return self._admit(priority) => return None` | survived | killed (test_admit_returns_the_validated_resource_snapshot) |
| M24 | `LocalQueue.submit:438` | `task_type not in MECHANICAL_TASKS => task_type in MECHANICAL_TASKS` | killed | killed (existing tests) |
| M25 | `LocalQueue.submit:465` | `> self.caps.max_input_bytes => >= self.caps.max_input_bytes` | survived | killed (test_submit_accepts_payload_exactly_at_configured_byte_cap) |
| M26 | `LocalQueue._client_data_health_refusal:515` | `self.backend_id != "gemma_certified" => self.backend_id == "gemma_certified"` | killed | killed (existing tests) |
| M27 | `LocalQueue._get:533` | `return row => return None` | killed | killed (existing tests) |
| M28 | `LocalQueue._public:540` | `"deduplicated": deduplicated => "deduplicated": False` | killed | killed (existing tests) |
| M29 | `LocalQueue.status:546` | `return self._public => return None  # self._public` | killed | killed (existing tests) |
| M30 | `LocalQueue.hold_dependency:552` | `if dependency: => if not dependency:` | survived | killed (test_hold_dependency_blocks_then_releases_queued_work) |
| M31 | `LocalQueue.result:564` | `not in TERMINAL => in TERMINAL` | killed | killed (existing tests) |
| M32 | `LocalQueue.cancel:576` | `row["status"] == "queued" => row["status"] != "queued"` | killed | killed (existing tests) |
| M33 | `LocalQueue.feedback:591` | `outcome not in => outcome in` | killed | killed (existing tests) |
| M34 | `LocalQueue._recover_locked:611` | `return cur.rowcount => return None` | survived | killed (test_recover_locked_counts_only_lease_that_has_passed) |
| M35 | `LocalQueue._recover_locked:609` | `lease_until < ? => lease_until <= ?` | survived | killed (test_recover_locked_counts_only_lease_that_has_passed) |
| M36 | `LocalQueue._acquire:618` | `return True => return False` | killed | killed (existing tests) |
| M37 | `LocalQueue._acquire:619` | `row["lease_until"] < now => row["lease_until"] <= now` | survived | killed (test_acquire_preserves_other_owner_at_exact_lease_deadline) |
| M38 | `LocalQueue.run_once:636` | `return self._run_once_under_lock(owner) => return None` | killed | killed (existing tests) |
| M39 | `LocalQueue._run_once_under_lock:644` | `self.backend is None => self.backend is not None` | killed | killed (existing tests) |
| M40 | `LocalQueue._run_once_under_lock:654` | `expires_at < ? => expires_at <= ?` | survived | killed (test_run_once_executes_bulk_job_at_exact_expiry_deadline) |
| M41 | `LocalQueue.state_report:728` | `"schema": 1 => "schema": 2` | survived | killed (test_state_report_declares_schema_version_one) |
| M42 | `LocalQueue._admit:415` | `snapshot.cpu_idle_ratio >= self.caps.min_cpu_idle_ratio => snapshot.cpu_idle_ratio > self.caps.min_cpu_idle_ratio` | survived | killed (test_private_admit_accepts_measured_idle_at_exact_threshold) |

## New tests

- `test_acquire_preserves_other_owner_at_exact_lease_deadline`
- `test_admit_returns_the_validated_resource_snapshot`
- `test_cancel_reports_that_a_live_child_was_signalled`
- `test_child_failure_suffix_keeps_recognized_detail_at_text_limit`
- `test_constructor_rejects_empty_command`
- `test_failure_text_hides_unrecognized_backend_exception`
- `test_fixed_child_detail_rejects_document_index_sixteen`
- `test_hold_dependency_blocks_then_releases_queued_work`
- `test_private_admit_accepts_measured_idle_at_exact_threshold`
- `test_private_admit_accepts_sample_exactly_one_second_ahead`
- `test_recover_locked_counts_only_lease_that_has_passed`
- `test_run_once_executes_bulk_job_at_exact_expiry_deadline`
- `test_state_report_declares_schema_version_one`
- `test_submit_accepts_payload_exactly_at_configured_byte_cap`

## Survivors left

None. Equivalent: 0; untestable offline: 0; defects: 0.

## Defects found

None.

## Verification

- Original module SHA-256 before and after mutation: `000e0c3d4c019e9e4c08e2803cb8501b32f993ff91663af93869ab6126ef9d5c`.
- `python3 tests/test_spool_mutation.py`: 14 tests passed.
- Original selected suites: 35 queue/intake, 22 checkpoint, and 15 client-data-health tests passed.
- `python3 tests/test_onboard.py`: 28 tests passed using temporary homes.
- `python3 tests/test_local_worker.py`: 14 setup errors because the sandbox denies binding even `127.0.0.1` (`PermissionError: [Errno 1]`); no application assertion ran.
- `python3 tests/test_suite.py`: 593 passed, 0 failed, 5 environmental skips.
