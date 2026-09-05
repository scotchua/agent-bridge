# Audits

## verification-path-audit-2026-08-30.md

An audit of whether this project's own verification procedure can be executed,
and whether it measures what it claims. All six areas came back BROKEN, seven
findings.

Disposition, 2026-08-30:

**FIXED, commit `60b4d12`:**
- VERIFY-05. `timeout_canary` re-read `cfg.path`, which under default layering
  is `broker.json` ALONE, whose `allowed_versions` is `[]`, and `check_peer`
  skips the comparison on an empty allow-list. **The timeout canary was
  clearing its version gate by having no pin to check, not by matching one.**
  The stub's default report of `codex-cli 0.147.0` kept that invisible. It now
  builds from a deep copy of the in-memory effective config and substitutes the
  version along with the executable, so the gate stays ACTIVE.
- VERIFY-06. `--skip-timeout-canary` produced a PASS with the timeout and orphan
  control never exercised. A skipped control now reports INCOMPLETE and names
  itself. Scott ruled this forward-looking only; prior canary records were not
  re-audited.

**FIXED, 2026-09-04:**
- VERIFY-01, VERIFY-03, VERIFY-07. A non-activating `--candidate` setup mode
  emitting one complete validated effective config; the canary runner consuming
  it without re-layering; a mandatory durable results file carrying the
  effective-config hash, configured versus observed versions, controls
  requested versus executed, and the verdict; the overlay promoted only after a
  version-bound PASS. VERIFY-03 was the ordering defect: setup promoted a pin
  before re-measurement.
- VERIFY-02, VERIFY-04. `config/local.json` is a gitignored fragment with no
  `config_version`; explicit fragments now use the same defaults-plus-overlay
  build as runtime, while complete candidates are consumed verbatim.

First complete measurement after the fix, through the real layered path with no
`--config` override: PASS, timeout canary `timed_out orphans=0`. The codex pin
was then narrowed to `codex-cli 0.151.0` alone, the version actually measured.

Full session record: `~/Claude/docs/codex-session-2026-08-30-dispositions.md`
