# Mutation test report: client_data_health.py

## Method

- Target: `src/agent_bridge/localq/client_data_health.py`; source SHA-256: `4773725c66e4363308c856618321e6b4d35ad1b711273553eba2a512215dd3fc`.
- Generated 143 valid single-edit AST mutants, capped below 150 and spread across every function. Edits cover comparisons, `and`/`or`, condition negation, numeric constants, return values (`None`, `False`, and empty), statement removal, `+`/`-`, and dropped raises. AST selection excludes comments, logging, and docstrings.
- Each mutant was loaded through a temporary import hook in a fresh Python process. The original production file was never written. Each focused test run had an 8-second timeout. `killed (timeout)` is counted as killed and shown separately.
- Before used the committed original `tests/test_localq_client_data_health.py` copied to a temporary test tree; after used the current test file. Both ran `python3 -m unittest discover -s <test tree> -p test_localq_client_data_health.py -q`.

## Results

| Stage | Killed | Survived | Kill rate |
| --- | ---: | ---: | ---: |
| Before new tests | 93 | 50 | 65.0% |
| After new tests | 139 | 4 | 97.2% |

Generation by type: `arithmetic` 2, `boolean` 16, `comparison` 18, `drop raise` 5, `negate condition` 21, `numeric +1` 15, `remove statement` 12, `return False` 19, `return None` 19, `return empty` 16.

Two mutants (`M004`, `M010`) timed out in both stages after changing the POSIX process-group branch. The timebox counted these as kills; it does not establish the exact failed assertion.

## New tests

Each test passed on the original module and failed when run alone against the recorded mutant shown.

| Test (`ClientDataHealthTests`) | Mutant caught |
| --- | --- |
| `test_is_sha256_rejects_nonstring_without_raising` | `M014` |
| `test_from_config_default_and_partial_group` | `M015` |
| `test_from_config_rejects_each_malformed_field` | `M060` |
| `test_kill_group_terminates_windows_process` | `M016` |
| `test_status_reaps_and_closes_on_reader_timeout` | `M021` |
| `test_status_reaps_and_closes_on_exact_output_limit` | `M001` |
| `test_status_wait_uses_remaining_time_and_closes_output` | `M053` |
| `test_status_expired_budget_and_wait_timeout_cleanup` | `M065` |
| `test_binding_reason_rejects_bad_record_shape_and_missing_wrapper` | `M058` |
| `test_check_rejects_extra_status_field` | `M043` |

## Survivors left

| Mutant | Reason |
| --- | --- |
| `M106` | Equivalent: changes only the `code == 1 and result == "not verified"` branch at line 196; every newly selected case still returns `HEALTH_UNVERIFIED` through the fallback, and a successful `code == 0, result == "verified"` case never enters this branch. |
| `M109` | Equivalent: changes only the `code == 1 and result == "not verified"` branch at line 196; every newly selected case still returns `HEALTH_UNVERIFIED` through the fallback, and a successful `code == 0, result == "verified"` case never enters this branch. |
| `M112` | Equivalent: changes only the `code == 1 and result == "not verified"` branch at line 196; every newly selected case still returns `HEALTH_UNVERIFIED` through the fallback, and a successful `code == 0, result == "verified"` case never enters this branch. |
| `M115` | Equivalent: changes only the `code == 1 and result == "not verified"` branch at line 196; every newly selected case still returns `HEALTH_UNVERIFIED` through the fallback, and a successful `code == 0, result == "verified"` case never enters this branch. |

Untestable offline: none. Defects found: none.

## Mutant ledger

`K` = killed by assertion/error, `T` = killed by timebox, `S` = survived. The four `S/S` mutants are the equivalent cases above.

| ID | Function:line | Edit | Before | After |
| --- | --- | --- | --- | --- |
| `M001` | `<module>:27` | numeric +1: `4096 -> 4097` | S | K |
| `M002` | `_is_sha256:32` | return None: `isinstance(value, str) and _SHA256.fullmatch(value) is not None -> None` | K | K |
| `M003` | `from_config:59` | negate condition: `all(value is None for value in values) -> not (all(value is None for value in values))` | K | K |
| `M004` | `_kill_group:89` | negate condition: `os.name != "posix" -> not (os.name != "posix")` | T | T |
| `M005` | `_status:116` | remove statement: `reader.start() -> pass` | K | K |
| `M006` | `_binding_reason:150` | return None: `HEALTH_BINDING_MISMATCH -> None` | K | K |
| `M007` | `check:181` | negate condition: `status is None -> not (status is None)` | K | K |
| `M008` | `_is_sha256:32` | return False: `isinstance(value, str) and _SHA256.fullmatch(value) is not None -> False` | K | K |
| `M009` | `from_config:61` | negate condition: `any(value is None for value in values) -> not (any(value is None for value in values))` | K | K |
| `M010` | `_kill_group:89` | comparison: `os.name != "posix" -> (os.name == "posix")` | T | T |
| `M011` | `_status:117` | remove statement: `reader.join(self.timeout_seconds) -> pass` | K | K |
| `M012` | `_binding_reason:150` | return False: `HEALTH_BINDING_MISMATCH -> False` | K | K |
| `M013` | `check:182` | return None: `HEALTH_UNVERIFIED -> None` | K | K |
| `M014` | `_is_sha256:32` | boolean: `isinstance(value, str) and _SHA256.fullmatch(value) is not None -> ((isinstance(value, str)) or (_SHA256.fullmatch(value) is not None))` | S | K |
| `M015` | `from_config:67` | negate condition: `values[0] is None and values[1] is None and values[3] is None and values[2] == 30.0 -> not (values[0] is None and values[1] is None and values[3] is None and values[2] =...` | S | K |
| `M016` | `_kill_group:90` | remove statement: `proc.kill() -> pass` | S | K |
| `M017` | `_status:118` | negate condition: `reader.is_alive() -> not (reader.is_alive())` | K | K |
| `M018` | `_binding_reason:150` | return empty: `HEALTH_BINDING_MISMATCH -> ''` | K | K |
| `M019` | `check:182` | return False: `HEALTH_UNVERIFIED -> False` | K | K |
| `M020` | `from_config:67` | boolean: `values[0] is None and values[1] is None and values[3] is None and values[2] == 30.0 -> ((values[0] is None) or (values[1] is None) or (values[3] is None) or (values[2] =...` | S | K |
| `M021` | `_status:120` | remove statement: `proc.wait() -> pass` | S | K |
| `M022` | `_binding_reason:151` | negate condition: `hashlib.sha256(record_bytes).hexdigest() != reported_hash -> not (hashlib.sha256(record_bytes).hexdigest() != reported_hash)` | K | K |
| `M023` | `check:182` | return empty: `HEALTH_UNVERIFIED -> ''` | K | K |
| `M024` | `from_config:67` | numeric +1: `0 -> 1` | S | K |
| `M025` | `_status:121` | remove statement: `reader.join() -> pass` | S | K |
| `M026` | `_binding_reason:151` | comparison: `hashlib.sha256(record_bytes).hexdigest() != reported_hash -> (hashlib.sha256(record_bytes).hexdigest() == reported_hash)` | K | K |
| `M027` | `check:187` | return None: `HEALTH_UNVERIFIED -> None` | K | K |
| `M028` | `from_config:67` | numeric +1: `1 -> 2` | S | K |
| `M029` | `_status:122` | remove statement: `proc.stdout.close() -> pass` | S | K |
| `M030` | `_binding_reason:152` | return None: `HEALTH_BINDING_MISMATCH -> None` | K | K |
| `M031` | `check:187` | return False: `HEALTH_UNVERIFIED -> False` | K | K |
| `M032` | `from_config:67` | numeric +1: `3 -> 4` | S | K |
| `M033` | `_status:124` | numeric +1: `0 -> 1` | K | K |
| `M034` | `_binding_reason:152` | return False: `HEALTH_BINDING_MISMATCH -> False` | K | K |
| `M035` | `check:187` | return empty: `HEALTH_UNVERIFIED -> ''` | K | K |
| `M036` | `from_config:68` | comparison: `values[2] == 30.0 -> (values[2] != 30.0)` | S | K |
| `M037` | `_status:129` | negate condition: `len(output) >= _STATUS_LIMIT -> not (len(output) >= _STATUS_LIMIT)` | K | K |
| `M038` | `_binding_reason:152` | return empty: `HEALTH_BINDING_MISMATCH -> ''` | K | K |
| `M039` | `check:188` | negate condition: `not isinstance(value, dict) or set(value) != {"schema", "result", "record_sha256"}... -> not (not isinstance(value, dict) or set(value) != {"schema", "result", "record_sha...` | K | K |
| `M040` | `from_config:68` | numeric +1: `2 -> 3` | S | K |
| `M041` | `_status:129` | comparison: `len(output) >= _STATUS_LIMIT -> (len(output) > _STATUS_LIMIT)` | S | K |
| `M042` | `_binding_reason:156` | return None: `HEALTH_BINDING_MISMATCH -> None` | K | K |
| `M043` | `check:188` | boolean: `not isinstance(value, dict) or set(value) != {"schema", "result", "record_sha256"}... -> ((not isinstance(value, dict)) and (set(value) != {"schema", "result", "record_sha...` | S | K |
| `M044` | `from_config:68` | numeric +1: `30.0 -> 31.0` | S | K |
| `M045` | `_status:131` | remove statement: `proc.wait() -> pass` | S | K |
| `M046` | `_binding_reason:156` | return False: `HEALTH_BINDING_MISMATCH -> False` | K | K |
| `M047` | `check:188` | comparison: `set(value) != {"schema", "result", "record_sha256"} -> (set(value) == {"schema", "result", "record_sha256"})` | K | K |
| `M048` | `from_config:70` | drop raise: `raise ValueError("client_data_health_configuration_incomplete") -> pass` | S | K |
| `M049` | `_status:132` | remove statement: `proc.stdout.close() -> pass` | S | K |
| `M050` | `_binding_reason:156` | return empty: `HEALTH_BINDING_MISMATCH -> ''` | K | K |
| `M051` | `check:189` | comparison: `value["schema"] != 1 -> (value["schema"] == 1)` | K | K |
| `M052` | `from_config:72` | negate condition: `not isinstance(command, (tuple, list)) or not command or not all(isinstance(part, ... -> not (not isinstance(command, (tuple, list)) or not command or not all(isinstance(p...` | K | K |
| `M053` | `_status:134` | arithmetic: `self.timeout_seconds - (time.monotonic() - started) -> (self.timeout_seconds + time.monotonic() - started)` | S | K |
| `M054` | `_binding_reason:157` | negate condition: `not isinstance(record, dict) or record.get("state") != "verified" or record.get("f... -> not (not isinstance(record, dict) or record.get("state") != "verified" or record.g...` | K | K |
| `M055` | `check:189` | numeric +1: `1 -> 2` | K | K |
| `M056` | `from_config:72` | boolean: `not isinstance(command, (tuple, list)) or not command or not all(isinstance(part, ... -> ((not isinstance(command, (tuple, list))) and (not command) and (not all(isinstanc...` | K | K |
| `M057` | `_status:134` | arithmetic: `time.monotonic() - started -> (time.monotonic() + started)` | S | K |
| `M058` | `_binding_reason:157` | boolean: `not isinstance(record, dict) or record.get("state") != "verified" or record.get("f... -> ((not isinstance(record, dict)) and (record.get("state") != "verified") and (recor...` | S | K |
| `M059` | `check:191` | boolean: `value["record_sha256"] is not None and not _is_sha256(value["record_sha256"]) -> ((value["record_sha256"] is not None) or (not _is_sha256(value["record_sha256"])))` | K | K |
| `M060` | `from_config:73` | boolean: `isinstance(part, str) and part -> ((isinstance(part, str)) or (part))` | S | K |
| `M061` | `_status:136` | remove statement: `proc.wait(timeout=max(0.0, remaining)) -> pass` | K | K |
| `M062` | `_binding_reason:157` | comparison: `record.get("state") != "verified" -> (record.get("state") == "verified")` | K | K |
| `M063` | `check:192` | return None: `HEALTH_UNVERIFIED -> None` | S | K |
| `M064` | `from_config:74` | numeric +1: `0 -> 1` | K | K |
| `M065` | `_status:136` | numeric +1: `0.0 -> 1.0` | S | K |
| `M066` | `_binding_reason:159` | return None: `HEALTH_BINDING_MISMATCH -> None` | K | K |
| `M067` | `check:192` | return False: `HEALTH_UNVERIFIED -> False` | S | K |
| `M068` | `from_config:74` | comparison: `tuple(command[-2:]) != ("status", "--json") -> (tuple(command[-2:]) == ("status", "--json"))` | K | K |
| `M069` | `_status:139` | remove statement: `proc.wait() -> pass` | S | K |
| `M070` | `_binding_reason:159` | return False: `HEALTH_BINDING_MISMATCH -> False` | K | K |
| `M071` | `check:192` | return empty: `HEALTH_UNVERIFIED -> ''` | S | K |
| `M072` | `from_config:74` | numeric +1: `2 -> 3` | K | K |
| `M073` | `_status:140` | remove statement: `proc.stdout.close() -> pass` | S | K |
| `M074` | `_binding_reason:159` | return empty: `HEALTH_BINDING_MISMATCH -> ''` | K | K |
| `M075` | `check:194` | negate condition: `code == 2 and result == "failed" -> not (code == 2 and result == "failed")` | K | K |
| `M076` | `from_config:75` | drop raise: `raise ValueError("client_data_health_command_invalid") -> pass` | K | K |
| `M077` | `_status:143` | remove statement: `proc.stdout.close() -> pass` | S | K |
| `M078` | `_binding_reason:161` | negate condition: `not isinstance(hashes, dict) or set(hashes) != set(_WRAPPER_FILES) -> not (not isinstance(hashes, dict) or set(hashes) != set(_WRAPPER_FILES))` | K | K |
| `M079` | `check:194` | boolean: `code == 2 and result == "failed" -> ((code == 2) or (result == "failed"))` | K | K |
| `M080` | `from_config:76` | negate condition: `not isinstance(path, str) or not path -> not (not isinstance(path, str) or not path)` | K | K |
| `M081` | `_status:144` | return None: `code, output -> None` | K | K |
| `M082` | `_binding_reason:161` | boolean: `not isinstance(hashes, dict) or set(hashes) != set(_WRAPPER_FILES) -> ((not isinstance(hashes, dict)) and (set(hashes) != set(_WRAPPER_FILES)))` | S | K |
| `M083` | `check:194` | comparison: `code == 2 -> (code != 2)` | K | K |
| `M084` | `from_config:76` | boolean: `not isinstance(path, str) or not path -> ((not isinstance(path, str)) and (not path))` | S | K |
| `M085` | `_status:144` | return False: `code, output -> False` | K | K |
| `M086` | `_binding_reason:161` | comparison: `set(hashes) != set(_WRAPPER_FILES) -> (set(hashes) == set(_WRAPPER_FILES))` | K | K |
| `M087` | `check:194` | numeric +1: `2 -> 3` | K | K |
| `M088` | `from_config:77` | drop raise: `raise ValueError("client_data_health_path_invalid") -> pass` | S | K |
| `M089` | `_status:144` | return empty: `code, output -> ()` | K | K |
| `M090` | `_binding_reason:162` | return None: `HEALTH_BINDING_MISMATCH -> None` | S | K |
| `M091` | `check:194` | comparison: `result == "failed" -> (result != "failed")` | K | K |
| `M092` | `from_config:78` | negate condition: `isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0... -> not (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout...` | K | K |
| `M093` | `_binding_reason:162` | return False: `HEALTH_BINDING_MISMATCH -> False` | S | K |
| `M094` | `check:195` | return None: `HEALTH_FAILED -> None` | K | K |
| `M095` | `from_config:78` | boolean: `isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0... -> ((isinstance(timeout, bool)) and (not isinstance(timeout, (int, float))) and (time...` | K | K |
| `M096` | `_binding_reason:162` | return empty: `HEALTH_BINDING_MISMATCH -> ''` | S | K |
| `M097` | `check:195` | return False: `HEALTH_FAILED -> False` | K | K |
| `M098` | `from_config:79` | comparison: `timeout <= 0 -> (timeout < 0)` | S | K |
| `M099` | `_binding_reason:163` | negate condition: `not isinstance(self.delegate_executable, Path) or not _is_sha256(self.delegate_sha... -> not (not isinstance(self.delegate_executable, Path) or not _is_sha256(self.delegat...` | K | K |
| `M100` | `check:195` | return empty: `HEALTH_FAILED -> ''` | K | K |
| `M101` | `from_config:79` | numeric +1: `0 -> 1` | K | K |
| `M102` | `_binding_reason:163` | boolean: `not isinstance(self.delegate_executable, Path) or not _is_sha256(self.delegate_sha... -> ((not isinstance(self.delegate_executable, Path)) and (not _is_sha256(self.delegat...` | S | K |
| `M103` | `check:196` | negate condition: `code == 1 and result == "not verified" -> not (code == 1 and result == "not verified")` | K | K |
| `M104` | `from_config:79` | boolean: `isinstance(timeout, float) and not math.isfinite(timeout) -> ((isinstance(timeout, float)) or (not math.isfinite(timeout)))` | K | K |
| `M105` | `_binding_reason:164` | return None: `HEALTH_BINDING_MISMATCH -> None` | S | K |
| `M106` | `check:196` | boolean: `code == 1 and result == "not verified" -> ((code == 1) or (result == "not verified"))` | S | S |
| `M107` | `from_config:80` | drop raise: `raise ValueError("client_data_health_timeout_seconds_invalid") -> pass` | K | K |
| `M108` | `_binding_reason:164` | return False: `HEALTH_BINDING_MISMATCH -> False` | S | K |
| `M109` | `check:196` | comparison: `code == 1 -> (code != 1)` | S | S |
| `M110` | `from_config:81` | negate condition: `not isinstance(record, Path) or not record.is_absolute() -> not (not isinstance(record, Path) or not record.is_absolute())` | K | K |
| `M111` | `_binding_reason:164` | return empty: `HEALTH_BINDING_MISMATCH -> ''` | S | K |
| `M112` | `check:196` | numeric +1: `1 -> 2` | S | S |
| `M113` | `from_config:81` | boolean: `not isinstance(record, Path) or not record.is_absolute() -> ((not isinstance(record, Path)) and (not record.is_absolute()))` | S | K |
| `M114` | `_binding_reason:171` | return None: `HEALTH_BINDING_MISMATCH -> None` | S | K |
| `M115` | `check:196` | comparison: `result == "not verified" -> (result != "not verified")` | S | S |
| `M116` | `from_config:82` | drop raise: `raise ValueError("client_data_health_record_invalid") -> pass` | S | K |
| `M117` | `_binding_reason:171` | return False: `HEALTH_BINDING_MISMATCH -> False` | S | K |
| `M118` | `check:197` | return None: `HEALTH_UNVERIFIED -> None` | K | K |
| `M119` | `from_config:83` | return None: `cls(command=tuple(command), path=path, timeout_seconds=float(timeout), record=reco... -> None` | K | K |
| `M120` | `_binding_reason:171` | return empty: `HEALTH_BINDING_MISMATCH -> ''` | S | K |
| `M121` | `check:197` | return False: `HEALTH_UNVERIFIED -> False` | K | K |
| `M122` | `from_config:83` | return False: `cls(command=tuple(command), path=path, timeout_seconds=float(timeout), record=reco... -> False` | K | K |
| `M123` | `_binding_reason:172` | negate condition: `any(hashes.get(name) != actual[name] for name in _WRAPPER_FILES) -> not (any(hashes.get(name) != actual[name] for name in _WRAPPER_FILES))` | K | K |
| `M124` | `check:197` | return empty: `HEALTH_UNVERIFIED -> ''` | K | K |
| `M125` | `_binding_reason:172` | comparison: `hashes.get(name) != actual[name] -> (hashes.get(name) == actual[name])` | K | K |
| `M126` | `check:198` | negate condition: `code != 0 or result != "verified" or not _is_sha256(record_hash) -> not (code != 0 or result != "verified" or not _is_sha256(record_hash))` | K | K |
| `M127` | `_binding_reason:173` | return None: `HEALTH_BINDING_MISMATCH -> None` | K | K |
| `M128` | `check:198` | boolean: `code != 0 or result != "verified" or not _is_sha256(record_hash) -> ((code != 0) and (result != "verified") and (not _is_sha256(record_hash)))` | K | K |
| `M129` | `_binding_reason:173` | return False: `HEALTH_BINDING_MISMATCH -> False` | K | K |
| `M130` | `check:198` | comparison: `code != 0 -> (code == 0)` | K | K |
| `M131` | `_binding_reason:173` | return empty: `HEALTH_BINDING_MISMATCH -> ''` | K | K |
| `M132` | `check:198` | numeric +1: `0 -> 1` | K | K |
| `M133` | `_binding_reason:174` | negate condition: `hashes["delegate.py"] != self.delegate_sha256 -> not (hashes["delegate.py"] != self.delegate_sha256)` | K | K |
| `M134` | `check:198` | comparison: `result != "verified" -> (result == "verified")` | K | K |
| `M135` | `_binding_reason:174` | comparison: `hashes["delegate.py"] != self.delegate_sha256 -> (hashes["delegate.py"] == self.delegate_sha256)` | K | K |
| `M136` | `check:199` | return None: `HEALTH_UNVERIFIED -> None` | K | K |
| `M137` | `_binding_reason:175` | return None: `HEALTH_BINDING_MISMATCH -> None` | K | K |
| `M138` | `check:199` | return False: `HEALTH_UNVERIFIED -> False` | K | K |
| `M139` | `_binding_reason:175` | return False: `HEALTH_BINDING_MISMATCH -> False` | K | K |
| `M140` | `check:199` | return empty: `HEALTH_UNVERIFIED -> ''` | K | K |
| `M141` | `_binding_reason:175` | return empty: `HEALTH_BINDING_MISMATCH -> ''` | K | K |
| `M142` | `check:200` | return None: `self._binding_reason(record_hash) -> None` | K | K |
| `M143` | `check:200` | return False: `self._binding_reason(record_hash) -> False` | K | K |

## Verification

- Focused tests: 25 passed on the original module.
- Each of the ten new tests failed against its listed mutant when run individually.
- `python3 tests/test_suite.py` under an isolated temporary home: 592 passed, 0 failed, 5 environmental skips.
- `python3 tests/test_onboard.py` under an isolated temporary home: 28 passed.
- `python3 tests/test_local_worker.py` was attempted under an isolated temporary home; all 14 cases were blocked at setup because the sandbox denied loopback socket binding (`PermissionError: [Errno 1]`). No external API was contacted.
- The target module remained byte-identical throughout mutation runs; no production file was changed.
