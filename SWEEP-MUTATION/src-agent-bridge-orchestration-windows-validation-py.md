# Mutation sweep: `src/agent_bridge/orchestration/windows_validation.py`

## Method and rates

- 51 single-site mutants across every executable function and method; at most one edit per run.
- Edits cover comparison flips, `and`/`or` swaps, condition negation, numeric changes by one, empty/`None`/`False` returns, statement removal, `+`/`-` swap, and removal of one exception raise. Comments, logging, and docstrings were excluded.
- Each mutant was loaded from a temporary copy in a fresh Python process, with a 12-second timeout, and exercised by `test_windows_validation` and `test_windows_setup`. The original source was never rewritten.
- Baseline: **41/51 killed (80.4%)**, 10 survived.
- After new tests: **50/51 killed (98.0%)**, 1 survived.
- Source SHA-256 before and after: `199494304c3d958d8fc38f2ebae118cbe8d42c3377d15ac148de8ed40c66e693` (byte-identical).

## Mutants generated

| ID | Function | Line | Single edit | Before | After |
| --- | --- | ---: | --- | --- | --- |
| M01 | `validate_observation` | 149 | if not isinstance(observation, Observation): -> if isinstance(observation, Observation): | killed | killed |
| M02 | `validate_observation` | 151 | if observation.status not in STATUSES: -> if observation.status in STATUSES: | killed | killed |
| M03 | `validate_observation` | 153 | if not _REASON_RE.match(observation.reason): -> if _REASON_RE.match(observation.reason): | killed | killed |
| M04 | `validate_observation` | 156 | if not isinstance(key, str) or not _FACT_TOKEN_RE.match(key): -> if not isinstance(key, str) and not _FACT_TOKEN_RE.match(key): | survived | killed |
| M05 | `validate_observation` | 160 | if isinstance(value, str) and _FACT_TOKEN_RE.match(value): -> if isinstance(value, str) or _FACT_TOKEN_RE.match(value): | killed | killed |
| M06 | `validate_observation` | 152 | raise ValidationError(f"{check_id}: unknown status") -> pass | killed | killed |
| M07 | `validate_observation` | 163 | return observation -> return None | killed | killed |
| M08 | `_validate_suite` | 277 | if check.id in seen: -> if check.id not in seen: | killed | killed |
| M09 | `_validate_suite` | 280 | if required not in seen: -> if required in seen: | killed | killed |
| M10 | `_validate_suite` | 283 | seen.add(check.id) -> pass | killed | killed |
| M11 | `run_checks` | 307 | missing = [name for name in check.requires if name not in passed] ↵         if missing: -> missing = [name for name in check.requires if name not in passed] ↵         if not missing: | killed | killed |
| M12 | `run_checks` | 314 | if runner is None: -> if runner is not None: | killed | killed |
| M13 | `run_checks` | 331 | if observation.status == PASS: -> if observation.status != PASS: | killed | killed |
| M14 | `run_checks` | 332 | passed.add(check.id) -> pass | killed | killed |
| M15 | `_row` | 343 | "proves": check.proves if observation.status == PASS else "", -> "proves": check.proves if observation.status != PASS else "", | killed | killed |
| M16 | `_row` | 342 | "facts": dict(observation.facts), -> "facts": {}, | killed | killed |
| M17 | `verdict` | 359 | if row is None or row.get("status") != PASS: -> if row is None and row.get("status") != PASS: | killed | killed |
| M18 | `verdict` | 361 | return VERDICT_READY -> return None | killed | killed |
| M19 | `build_report` | 378 | "not_proven": list(NOT_PROVEN), -> "not_proven": [], | killed | killed |
| M20 | `build_report` | 381 | "suite": list(check.id for check in checks), -> "suite": [], | survived | killed |
| M21 | `parse_report` | 391 | if set(raw) != REPORT_KEYS: -> if set(raw) == REPORT_KEYS: | killed | killed |
| M22 | `parse_report` | 398 | if not isinstance(rows, list): -> if isinstance(rows, list): | killed | killed |
| M23 | `parse_report` | 403 | if row.get("status") not in STATUSES: -> if row.get("status") in STATUSES: | killed | killed |
| M24 | `parse_report` | 408 | if verdict(rows) != raw["verdict"]: -> if verdict(rows) == raw["verdict"]: | killed | killed |
| M25 | `render` | 416 | return json.dumps(report, indent=2, sort_keys=True) + "\n" -> return json.dumps(report, indent=2, sort_keys=True) - "\n" | killed | killed |
| M26 | `platform_runner` | 434 | if name.lower() not in {"windows", "win32"}: -> if name.lower() in {"windows", "win32"}: | killed | killed |
| M27 | `windows_build_runner` | 444 | completed = run(["C:\\Windows\\System32\\cmd.exe", "/c", "ver"]) ↵         if getattr(completed, "returncode", 1) != 0: -> completed = run(["C:\\Windows\\System32\\cmd.exe", "/c", "ver"]) ↵         if getattr(completed, "returncode", 1) == 0: | killed | killed |
| M28 | `wsl_version_runner` | 465 | result = ww.parse_wsl_version(_text(completed)) ↵         if not result.passed: -> result = ww.parse_wsl_version(_text(completed)) ↵         if result.passed: | killed | killed |
| M29 | `_build_number` | 479 | return int(parts[-1]) if parts and parts[-1].isdigit() else 0 -> return int(parts[-1]) if parts or parts[-1].isdigit() else 0 | survived | killed |
| M30 | `_text` | 494 | if b"\x00" in raw[:64]: -> if b"\x00" not in raw[:64]: | killed | killed |
| M31 | `features_runner` | 511 | if str(collected.get(name) or "").lower() != "enabled"] ↵         if missing: -> if str(collected.get(name) or "").lower() != "enabled"] ↵         if not missing: | killed | killed |
| M32 | `reboot_runner` | 524 | if pending(): -> if not pending(): | killed | killed |
| M33 | `runtime_root_runner` | 547 | return Observation(PASS, "runtime_root_private") -> return Observation(False, "runtime_root_private") | survived | killed |
| M34 | `image_runner` | 569 | if observed != expected: -> if observed == expected: | killed | killed |
| M35 | `parse_distro_list` | 591 | if len(fields) < 3: -> if len(fields) <= 3: | killed | killed |
| M36 | `parse_distro_list` | 594 | if name.upper() == "NAME": -> if name.upper() != "NAME": | killed | killed |
| M37 | `distro_runner` | 610 | if version.strip() != "2": -> if version.strip() == "2": | killed | killed |
| M38 | `GuestRoundTrip.__init__` | 631 | self._ran = False -> self._ran = True | killed | killed |
| M39 | `GuestRoundTrip.result` | 635 | self._ran = True -> pass | killed | killed |
| M40 | `GuestRoundTrip.guest_runner_check` | 641 | if getattr(result, "canaries_passed", False): -> if not getattr(result, "canaries_passed", False): | killed | killed |
| M41 | `GuestRoundTrip.canaries_check` | 650 | "count": len(getattr(result, "timings", {}) or {}) -> "count": len(getattr(result, "timings", {}) or {}) + 1 | survived | killed |
| M42 | `GuestRoundTrip.egress_check` | 656 | result = self.result() ↵         if not getattr(result, "canaries_passed", False): ↵             return Observation(FAIL, "egress_policy_unproven") -> result = self.result() ↵         if getattr(result, "canaries_passed", False): ↵             return Observation(FAIL, "egress_policy_unproven") | killed | killed |
| M43 | `GuestRoundTrip.round_trip_check` | 663 | if not getattr(result, "ok", False): -> if getattr(result, "ok", False): | killed | killed |
| M44 | `_short` | 677 | return token[:MAX_FACT_CHARS] -> return token[:MAX_FACT_CHARS + 1] | survived | killed |
| M45 | `default_runners` | 709 | if rootfs_path is not None and manifest is not None: -> if rootfs_path is not None or manifest is not None: | survived | killed |
| M46 | `default_runners` | 713 | if run_job is not None: -> if run_job is None: | survived | killed |
| M47 | `parse_report` | 410 | return dict(raw) -> return {} | killed | killed |
| M48 | `_text` | 496 | return raw.decode("utf-8", "replace") -> return raw.decode("utf-8", "replace") + "" | survived | survived |
| M49 | `_short` | 677 | return token[:MAX_FACT_CHARS] -> return token[:MAX_FACT_CHARS - 1] | survived | killed |
| M50 | `distro_runner` | 607 | if entry is None: -> if entry is not None: | killed | killed |
| M51 | `parse_distro_list` | 591 | if len(fields) < 3: -> if len(fields) < 4: | killed | killed |

## New tests

Each named test passes on the original module and fails when run alone against its listed mutant. All are in `tests/test_windows_validation.py`.

| Mutant | Test |
| --- | --- |
| M04 | `ValidateObservationMutationTests.test_a_string_fact_key_with_spaces_is_refused` |
| M20 | `BuildReportMutationTests.test_envelope_names_the_supplied_suite` |
| M29 | `BuildNumberMutationTests.test_an_unparseable_build_number_is_zero` |
| M33 | `RuntimeRootMutationTests.test_a_private_root_is_reported_as_passing` |
| M41 | `CanariesCheckMutationTests.test_passed_canaries_report_the_observed_count` |
| M44, M49 | `ShortMutationTests.test_a_token_keeps_exactly_the_first_64_characters` |
| M45 | `DefaultRunnersMutationTests.test_image_check_requires_both_path_and_manifest` |
| M46 | `DefaultRunnersMutationTests.test_guest_checks_require_a_job_runner` |

## Survivors left

- **M48 (`_text`, line 496): equivalent.** Appending `""` to the decoded string cannot change its value. No test was added.
- Untestable offline: none.
- Defects found: none.

## Verification

- `python3 tests/test_windows_validation.py -q`: 69 tests passed.
- `python3 tests/test_suite.py` with an isolated temporary home: 592 passed, 0 failed, 5 platform skips.
- `tests/test_onboard.py`, `tests/test_hostenv.py`, `tests/test_automatic_gate.py`, and `tests/test_delegation_audit.py` passed with an isolated temporary home.
- `tests/test_local_worker.py` and `tests/test_automatic_delegation_e2e.py` could not start their local fake HTTP servers: the sandbox denies loopback socket binding (`PermissionError: [Errno 1] Operation not permitted`).
