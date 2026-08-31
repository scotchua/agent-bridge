# Verification path audit — 2026-08-30

ESCALATE — `--skip-timeout-canary` can report `PASS` while the timeout/orphan
control the suite claims to include was never exercised (`canaries/run_canaries.py:7,305-313,324,351-361`).

A reachability block, an empty matrix, a
graded-call failure, a bad continuation, or a failed timeout canary all make the
runner exit non-zero (`canaries/run_canaries.py:247-251,294-310`). The explicit
`--skip-timeout-canary` option does allow a passing report without exercising
that control; that is VERIFY-06 below.

This audit follows the shipped README and verified-behaviour instructions on
paper, then traces setup, config loading, preflight, the canary runner, and the
records they write. It does not assess `peer_home_config_present`, Codex 0.151.0,
or whether any live canary passes.

## V1 — configuration layering: BROKEN

### VERIFY-01 — an explicit canary config does not represent runtime configuration

Most severe consequence: the same named config path means different effective
configuration to normal runtime and to the canary runner. Runtime `config.load()`
reads `config/broker.json` and deep-merges `config/local.json`; passing any path
or setting `AGENT_BRIDGE_CONFIG` loads that file verbatim
(`src/agent_bridge/config.py:200-215`). The docstring calls this deterministic,
but it makes the runner deterministic with respect to an input the runtime does
not use.

- Failing command: `./canaries/run_canaries.py --config config/local.json --direction codex-to-claude --one-turn 0 --three-turn 0 --skip-timeout-canary`
- Observed behaviour in this checkout: `FileNotFoundError: [Errno 2] No such file or directory: 'config/local.json'`, because the generated, gitignored file is not shipped (`.gitignore:6`; `src/agent_bridge/config.py:206-208`). With a setup-shaped fragment in a temporary directory, the same load produced `ValueError: unsupported config_version None` at `src/agent_bridge/config.py:35-37`.
- Consequence: `--config config/local.json` cannot consume setup's output, while
  `--config config/broker.json` omits the machine pin and other local policy. A
  hand-merged full file is the only way to give `--config` the effective runtime
  settings, and the project neither creates nor validates such an artifact.
- Suggested fix: define one public effective-config operation: load committed
  defaults plus a selected overlay, validate once, and optionally write the
  merged JSON. Have runtime and canaries call it. Preserve deterministic tests by
  accepting both an explicit base and explicit overlay, not by changing the
  meaning of an explicit path.

The broker already snapshots `cfg.raw` for each admitted job specifically to
avoid losing the merge (`src/agent_bridge/broker.py:248-262`). That fix proves the
effective configuration is a useful durable artifact, but the canary entry point
does not expose the same operation.

## V2 — documented procedures: BROKEN

### VERIFY-02 — neither documented canary procedure is literal and reproducible

README's short sequence is:

1. `./bin/agent-bridge-setup`
2. `python3 tests/test_suite.py`
3. `./canaries/run_canaries.py --direction both`

The first command is dry-run by default and writes nothing
(`src/agent_bridge/setup_cmd.py:243-249`), so on a fresh checkout step 3 uses
un-pinned defaults. `config/broker.json` has `executable: null` and
`allowed_versions: []` for both peers (`config/broker.json:29-53`), and preflight
only enforces a version when that list is non-empty
(`src/agent_bridge/preflight.py:77-96`). Thus the README's literal procedure can
discover a PATH executable but cannot verify a pin.

INSTALL's longer procedure correctly adds
`./bin/agent-bridge-setup --write` (`INSTALL.md:127-137`) and then runs
`./canaries/run_canaries.py --direction both --out /tmp/canary.json`
(`INSTALL.md:155-168`). That path is executable only on the current pin; after an
upgrade it updates the pin before measurement. There is no ratification state
between “observed candidate version” and “allowed version,” so the command that
makes preflight accept the new CLI precedes the evidence meant to justify it.

The verified-behaviour document says to re-verify version-specific facts and
specifically says “Re-measure on a version bump; the canary suite is the place
for it” (`docs/verified-cli-behaviour.md:3-7,84-95`), but supplies no command or
ordering for doing so. The canary matrix also does not exercise the cited
ancestor-configuration experiment, so the stated suite is not a procedure for
re-measuring that particular claim.

- Failing command: `./canaries/run_canaries.py --config config/local.json --direction codex-to-claude --one-turn 0 --three-turn 0 --skip-timeout-canary`
- Observed error: `FileNotFoundError: [Errno 2] No such file or directory: 'config/local.json'`; a setup-shaped temporary fragment instead gives `ValueError: unsupported config_version None` (`src/agent_bridge/config.py:35-37,200-208`).
- Consequence: README can run without any version assertion; INSTALL can run
  only after trusting the upgraded pin; verified-cli-behaviour names no supported
  transition and points to a suite that does not re-run all the facts it cites.
- Suggested fix: document one ordered upgrade workflow: discover the candidate,
  emit an effective candidate config without activating it, run and save the
  canaries plus named version-specific probes against it, review the evidence,
  then promote the candidate pin.

## V3 — version pin end to end: BROKEN

### VERIFY-03 — setup promotes the new version before re-measurement

Setup obtains each executable's `--version` output and directly builds
`allowed_versions: [observed version]` (`src/agent_bridge/setup_cmd.py:98-105,228-244`).
Preflight later refuses only when an allowed list exists and the observed value
is absent (`src/agent_bridge/preflight.py:83-90`). There is no candidate-pin
command, canary ratification command, or promotion step.

- Failing command in the documented upgrade order:
  `./bin/agent-bridge-setup --write && ./canaries/run_canaries.py --direction both --out /tmp/canary.json`
- Observed behaviour from code: setup prints `Wrote .../config/local.json` and
  replaces the allow-list with the newly observed version before the canary
  process starts (`src/agent_bridge/setup_cmd.py:231-247`). If canaries then fail,
  runtime nonetheless accepts the new version because preflight sees that new
  value in `allowed_versions` (`src/agent_bridge/preflight.py:88-96`).
- Consequence: the supported upgrade path cannot maintain “refused until
  ratified.” It conflates discovering a version, authorizing it, and selecting it
  for measurement.
- Suggested fix: have setup emit a complete candidate effective config (or an
  overlay plus explicit base) without changing active `local.json`; run canaries
  against the candidate; promote it only after a successful, saved result.

## V4 — `config/local.json` contract: BROKEN

### VERIFY-04 — the fragment contract is implementation-only and easy to misuse

Setup deliberately creates only `{"peers": ...}` (`src/agent_bridge/setup_cmd.py:228-244`).
README calls `config/local.json` “your machine, written by setup” and shows users
editing fragment-shaped objects (`README.md:101-104,153-162,227-239`), but neither
README nor verified-cli-behaviour calls it an overlay/fragment, says it is invalid
alone, or identifies `config/broker.json` as the only shipped full config.

- Failing command: `./canaries/run_canaries.py --config /tmp/setup-shaped-local.json --direction codex-to-claude --one-turn 0 --three-turn 0 --skip-timeout-canary`
- Observed error from the equivalent temporary fragment check:
  `ValueError: unsupported config_version None` (`src/agent_bridge/config.py:35-37`).
  The next required-key checks would also reject the fragment
  (`src/agent_bridge/config.py:38-43`).
- Consequence: a reasonable user treats a file called “config” as a valid
  `--config` argument and fails before canaries start. Copying `config_version`
  into it merely advances to more missing keys; manually filling those keys
  duplicates defaults and risks drift.
- Suggested fix: document `local.json` as a deep-merge overlay wherever it is
  introduced, call the CLI option `--overlay` if that is what it accepts, and
  provide an `effective-config` command rather than asking users to infer merge
  semantics.

## V5 — canary refusal and degradation: BROKEN

### VERIFY-05 — timeout substitution starts from the wrong file

`main()` loads the effective/default config into `cfg`, but invokes timeout
canaries with `cfg.path` (`canaries/run_canaries.py:328,351-356`). Under normal
runtime loading `cfg.path` remains `config/broker.json`, even when `cfg.raw`
contains the local merge (`src/agent_bridge/config.py:209-215`). The timeout
canary then reopens that path directly, substitutes the stub, writes it, and
loads the temporary full config (`canaries/run_canaries.py:171-185`). It has
therefore dropped all local pins and policy before testing timeout behavior.

- Failing command: `./canaries/run_canaries.py --direction both`
- Observed behaviour from code for a setup-managed machine: live calls use the
  merged `cfg`, but timeout calls use `json.load(open(cfg.path))`; with the normal
  `cfg.path == config/broker.json`, its peer `allowed_versions` are empty
  (`config/broker.json:29-53`) rather than correctly pinned. Conversely, a
  hand-merged config retaining a real peer's allowed version gets its executable
  replaced by a fake whose version string does not match, producing
  `preflight_version_mismatch` from `broker.start` at
  `canaries/run_canaries.py:174-185` and `src/agent_bridge/preflight.py:83-90`.
- Consequence: the timeout canary either silently disables the version control
  by losing the pin or fails before exercising timeout because the stub is
  checked against the real CLI's pin. It does not test timeout under the same
  effective configuration as the live matrix.
- Suggested fix: deep-copy `cfg.raw`, replace the stub, and replace that peer's
  allowed version with the fake's declared version (or narrowly bypass only the
  version check for the controlled stub). Record both substitutions.

### VERIFY-06 — explicit skipping can produce PASS with no timeout evidence

The runner exposes `--skip-timeout-canary` (`canaries/run_canaries.py:316-325`).
When set, the timeout list stays empty, and `report()` has no required-control
manifest or warning; it can print `PASS` after grading only live rows
(`canaries/run_canaries.py:254-313,351-361`).

- Failing command (demonstrated by calling the same report function without
  peers): `PYTHONDONTWRITEBYTECODE=1 python3 -c '<load run_canaries.py; call report with one valid row and an empty timeout list>'`
- Observed output: `calls graded 1`, `contract-valid rate 1/1 (100%)`, final
  `PASS`, and `report exit: 0`; no timeout line or “not verified” warning
  (`canaries/run_canaries.py:305-313`).
- Consequence: a saved console transcript can say PASS while containing no
  timeout/process-orphan measurement. This triggers the audit's escalation
  condition: the suite-level PASS overstates the controls actually exercised.
- Suggested fix: print `PARTIAL/FAIL` and a named missing-control row whenever a
  canary is skipped, or require a separate `--allow-partial` mode whose output
  and exit status cannot be mistaken for full-suite evidence.

Reachability itself is sound against silent clean results: environment failures
populate `blocked`, skip the matrix with an explicit message, and force exit 1
(`canaries/run_canaries.py:219-251,330-342,301-304`). A direction with zero
graded rows likewise says `NOTHING WAS TESTED` and fails
(`canaries/run_canaries.py:294-298`).

## V6 — durable, version-bound evidence: BROKEN

### VERIFY-07 — canary output is optional and omits the measured versions

The runner writes results only when `--out` is supplied; otherwise it prints and
exits (`canaries/run_canaries.py:325,358-361`). The README canary command omits
`--out` (`README.md:39-47`). INSTALL uses `/tmp/canary.json`, an explicitly
non-durable location, and provides no preservation or review step
(`INSTALL.md:155-168`). Even when written, the JSON contains only `rows`,
`timeouts`, and `blocked`; row construction records job outcomes and session
details but no configured or observed CLI version (`canaries/run_canaries.py:85-115,358-360`).
Per-job provenance does record `peer_observed_version`
(`src/agent_bridge/worker.py:288-328`), but `_record()` reads provenance and
discards that field, and a timeout result records neither the fake nor original
version (`canaries/run_canaries.py:195-207`).

- Failing command: `./canaries/run_canaries.py --direction both`
- Observed behaviour from code: no results file is written because `args.out` is
  false (`canaries/run_canaries.py:358-360`). With the documented INSTALL command,
  the only aggregate artifact is `/tmp/canary.json`, and its schema has no
  version/config identity.
- Consequence: afterward, nobody can prove which executable versions, effective
  config, requested matrix, skipped controls, or runner revision produced a PASS.
  “Re-measure on upgrade” is not auditable as a before/after claim.
- Suggested fix: require a durable output path and include timestamp, repository
  revision, runner arguments, effective-config hash (and optionally redacted
  snapshot), each peer's executable and configured/observed version, expected and
  executed control counts, blocked/skipped controls, rows, timeout results, exit
  verdict, and per-job IDs.

## Commands actually run

All executed checks were local and read-only with respect to repository and user
configuration. Temporary files were created only inside Python-managed temporary
directories and removed automatically. No network call was made and no peer job
was started.

- `pwd` and repository-local `rg`, `find`, `nl`, and `git status --porcelain`
  commands to locate and read code/docs.
- `PYTHONDONTWRITEBYTECODE=1 python3` importing `agent_bridge.config`, loading
  `config/local.json`, `config/broker.json`, and the default config. The absent
  ignored local file produced `FileNotFoundError`; broker/default loads succeeded.
- `PYTHONDONTWRITEBYTECODE=1 ./canaries/run_canaries.py --config config/local.json --direction codex-to-claude --one-turn 0 --three-turn 0 --skip-timeout-canary`.
  Loading failed before reachability, dispatch, or any peer invocation.
- `PYTHONDONTWRITEBYTECODE=1 python3` created a setup-shaped fragment in a
  temporary directory. Explicit loading produced
  `ValueError: unsupported config_version None`; substituting it as the default
  local overlay produced an effective config with `config_version: 1` and the
  fragment's executable/version pin.
- `PYTHONDONTWRITEBYTECODE=1 python3` imported the canary module and called
  `report()` with one synthetic valid row, no timeouts, and no blocked direction.
  It printed `PASS` and returned 0. This called neither `main()` nor a peer.
- Final verification: `git status --porcelain`.

## Minimum change that makes re-measurement executable

Add a non-activating setup mode that writes a complete, validated candidate
effective config (defaults plus the proposed local overlay). Make the canary
runner accept that artifact without re-layering, derive timeout configs from its
in-memory `cfg.raw`, replace the fake peer's version pin along with its
executable, and require a durable result file containing the effective-config
hash, configured and observed peer versions, requested/executed controls, and
verdict. Document this exact order: emit candidate; run all version-specific
probes and canaries against candidate; review a version-bound PASS artifact; then
atomically promote only its overlay to `config/local.json`. Until promotion,
preflight continues refusing the upgraded runtime CLI.
