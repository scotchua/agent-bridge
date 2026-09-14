# Consultation connectivity investigation — September 6, 2026

The immediate Claude failures were real OAuth failures. The retained CLI envelopes say `Failed to authenticate: OAuth session expired and could not be refreshed`, with `is_error: true` and exit status 1. This establishes the failure stage, **not the reason the refresh failed**. No evidence establishes that having two installations caused it.

A second, independently reproduced problem complicates diagnosis: the same working Claude login is visible with normal macOS Keychain access and invisible inside the restricted Codex shell. This can create a false impression that login was lost again. It does not explain away the user's earlier check outside the restricted shell, which also reported signed out before browser authorization.

## Evidence and limits

### Failure history

The append-only ledger, read before this investigation's smoke tests, contained 11 Claude and 5 Codex `peer_auth_failure` records. These include early setup/canary records; they are not 16 independent production outages. Six early records no longer had per-job quarantine available.

Of the retained Claude authentication failures, seven contained the expired/unrefreshable OAuth message and one contained `Not logged in · Please run /login`. The two retained Codex authentication failures contained HTTP 401 Unauthorized diagnostics. No retained authentication failure inspected here was shown to be a classifier false positive.

The September 6 UTC Claude failures include:

| Job | Start (UTC) | Evidence |
| --- | --- | --- |
| `c4a019a2-7578-4aaf-8b90-01d9e05bf6f7` | 06:36:22 | OAuth expired; refresh unsuccessful |
| `5d5457fb-cfc4-4657-808d-2414d7e591be` | 14:45:24 | Same |
| `4816d548-05e3-4577-b8fb-dd6441c0c609` | 14:47:58 | Same |
| `14c90dd4-8ae2-4fbe-9039-273d89440060` | 14:51:15 | Successful after the user's browser authorization |

All four jobs used the pinned `2.1.229 (Claude Code)` and the identical effective-config SHA-256:

`4f3ffa9b26989a6abdd817081deac3cce449731ea98cfd2e6e2225a1b7a1bc6f`

The first row is September 5 at 23:36 Pacific; UTC timestamps must not be mistaken for local dates. Evidence resides in the corresponding `~/.agent-bridge/jobs/<job>/provenance.json` and failure quarantine. Quarantine remains private and was not copied into this report. No client prompts were used or sent.

### Executable and version drift

- Shell Claude: `~/.local/bin/claude`, resolving to `~/.local/share/claude/versions/2.1.260`.
- Bridge Claude: `~/.nvm/versions/node/v24.16.0/bin/claude`, resolving to the native `@anthropic-ai/claude-code/bin/claude.exe`, version 2.1.229.
- Bridge Codex: `~/.nvm/versions/node/v24.16.0/bin/codex`, version 0.153.3. It is a Node launcher (`#!/usr/bin/env node`). The shell's other Codex path also reports 0.153.3. In the investigation shell, `node` resolves to `~/.local/bin/node`, v26.8.1: placing the Codex launcher under an nvm v24.16.0 directory does not pin its Node interpreter. The bridge preserves PATH. Both live Codex jobs nevertheless passed; no failure was attributed to this runtime difference, and PATH was not changed.

The bridge selects its configured absolute executable and verifies the version at admission and again in each worker against the job's config snapshot. The recorded failures are not version-gate failures or silent selection of shell Claude. Restarting desktop applications does not update the pin or repair an expired CLI credential. Different binaries remain a maintenance concern, but switching to 2.1.260 is not an evidence-based authentication fix and was not done.

### Credential storage and subprocess context

Read-only inspection of the installed Claude executables' bundled code shows that **both versions** derive their default Keychain service from `Claude Code` plus `-credentials`, and the account from the OS user/`USER`. Executable path is not part of that identity. `CLAUDE_CONFIG_DIR` and `CLAUDE_SECURESTORAGE_CONFIG_DIR` can change the store/namespace; they are absent from this bridge's effective environment.

The pinned implementation reads Keychain first and can fall back to `~/.claude/.credentials.json`. Both are currently present. A metadata-only comparison on September 6 found:

- Keychain: readable with normal host access, access and refresh tokens present, Team subscription, access-token expiry **22:49:54 UTC September 6**.
- Fallback file: access and refresh tokens present, access-token expiry **21:59:37 UTC September 4**, no subscription type reported.
- The two stores' access and refresh tokens differed. Values and token hashes were never emitted or saved by the investigation.

An expired access token alone is not proof of an invalid refresh token. The fallback file's age and divergence show why context matters; they do not prove when or why it was written, whether its refresh token is valid, or that it caused every historical failure. Neither store was edited, deleted, copied, or synchronized.

The paired status check used both executables in an empty temporary cwd with (a) inherited environment and (b) the bridge's actual environment allowlist:

| Execution context | Pinned Claude | Shell Claude |
| --- | --- | --- |
| Restricted shell, either environment | `loggedIn: false` | `loggedIn: false` |
| Normal host/Keychain access, either environment | `loggedIn: true`, Team | `loggedIn: true`, Team |

Thus credential visibility changes with OS execution context, while changing the tested environment allowlist does not change the result. A `loggedIn: false` from the restricted shell cannot by itself establish account logout. Environment scrubbing cannot remove an inherited OS sandbox. Conversely, a working MCP consultation is not equivalent to a subprocess launched inside that shell: they can have different OS restrictions.

The bridge preserves `HOME`, `USER`, `LOGNAME` and essential runtime variables, drops inherited API/OAuth keys and customization variables, and sets the Codex peer's isolated `CODEX_HOME`. No configured extra environment overrides were present. Isolation settings, working-directory checks, API-key exclusions, spend ceilings and retries were preserved.

Codex's `~/.agent-bridge/codex-home/auth.json` is a separate regular file, not a symlink to `~/.codex/auth.json`. Both report ChatGPT auth mode but had different last-refresh timestamps (September 3 versus August 28). The isolated-home `codex login status` reports ChatGPT login. Its separate credential lifecycle explains why desktop/default-home status cannot establish bridge status; it does not explain the recent Claude refresh failures.

Official documentation supports the storage/context distinction: [Claude credential management](https://code.claude.com/docs/en/authentication) documents macOS Keychain, file fallback and configuration-directory scoping. [Codex authentication](https://learn.chatgpt.com/docs/auth) documents file storage under `CODEX_HOME` and automatic token refresh during use. Local observations and installed implementation were used for version-specific conclusions.

### Refresh behavior: what is still unknown

The pinned Claude code contains refresh-token locking, cache invalidation, and an `invalid_grant` path that conditionally clears the matching access/refresh token on disk. This demonstrates mechanisms, not that they executed during this incident. The available bridge envelopes do not contain the refresh HTTP response, Keychain error code, lock contention history, or credential-write history. A targeted search for fixed refresh/Keychain failure diagnostics in the available Claude debug/telemetry files did not recover that missing evidence.

Token revocation, concurrent refresh/write behavior, intermittent Keychain access, stale fallback selection, network failure during refresh, and an upstream/version-specific defect remain hypotheses. No refresh was forced, token expired artificially, account logged out, or active app restarted to test them. The current successful consultations occurred before the new access token's expiry; they do **not** test the next refresh boundary.

## Implemented corrections

1. **`agent-bridge-admin health [--peer claude|codex] [--json]`** reports the configured path, realpath, observed/allowed versions, competing PATH installation, actual credential context and CLI-reported local login visibility. It uses an empty cwd, scrubbed environment, bounded capture and timeout. It honors version and isolated-home guards, never launches a login or inference request, never prints raw auth output, and explicitly distinguishes cached login visibility from network/refresh validity. JSON carries an observation timestamp and effective-config hash. The command itself does not read credential contents.
2. **Setup authentication checks** no longer declare Codex authenticated merely because `auth.json` exists, or treat arbitrary successful/unparseable output as proof. Claude missing/malformed status is unknown rather than implicitly signed out. Setup probes now use scrubbed environments and bounded execution too.
3. **Classification corrections:** removed bare `unauthorized` and bare `codex login` matches; failed-process/error-envelope evidence is required. Successful Codex runs with recovered error diagnostics are not mislabeled as auth failures. Nested `turn.failed.error.message` is recognized. Claude cannot be accepted as successful after a nonzero process exit. Timeout, output cap, session identity and quarantine safeguards remain in place.
4. **Future evidence and recovery:** failures record a closed reason such as `oauth_refresh_failed`, its diagnostic channel, executable realpath and non-secret credential context. These do not claim to know the upstream cause. The caller hint directs an operator to health checks and the correct context before changing login. Deterministic auth failures remain non-retryable; no automatic login, fallback to API billing, or credential synchronization was added.

The classifier and setup defects were reproduced with synthetic fixtures. They are real corrections, but neither is proven to have caused the historical Claude outage. The durable improvement delivered here is diagnosis and recovery guidance; a permanent authentication repair has not been established.

## Verification

- Targeted suite: 14 synthetic tests, including actual retained error wording, malformed status, secret redaction, expired-file false health, pin/home guards, scrubbed context, recovered errors, nested failures and nonzero exits.
- Full offline suite: restricted execution passed 520 checks with 5 process-enumeration checks explicitly skipped. Host-visible rerun: **538 passed, 0 failed, 0 skipped**, including all process-cleanup safeguards. The 14-test targeted suite also ran as part of both full runs. Final addition of the accepted Claude `--safe-mode --setting-sources ""` status flags was then verified by rerunning the targeted suite and normal-host health; the full suite was not repeated for that argv-only change. See [validation summary](connectivity-validation-2026-09-06.json).
- Live forward path: the already-connected Codex MCP integration; start `9532e721-7f37-4e04-a772-cb2d0a3c6e10`, continuation `ab5dcebd-ed58-402e-9383-c90b44c64238`.
- Live reverse path: real stdio MCP server launched as `--caller claude`, matching the `codex-peer` registration observed in `~/.claude.json`; start `d0448e08-66ca-4c09-a8d1-46ebe25eeac3`, continuation `99993dfd-d820-42f2-aadd-cba8d2a89a36`. This validates the MCP/backend path, not a click in Claude Desktop's UI.
- All four jobs completed on the first attempt with the existing pins/config hash, validated contracts, correct marker recall and stable session IDs. Both test conversations were closed and audit records retained.
- New worker provenance fields were verified in the live records. Existing MCP servers launch workers from current source, so backend changes applied without restarting apps. An already-running MCP server can retain an older in-memory hint until its normal next launch; the standalone health command is available immediately.

## Operator recovery procedure

Use the short [bridge health runbook](../bridge-health.md). Preserve evidence **before** any new browser authorization. If the problem recurs after a successful normal-context login, the missing evidence is the CLI refresh/storage failure around the incident, not another restart. Review logs locally and share only allowlisted metadata with support; never attach credential files or raw transcripts.

A future pin upgrade should follow the existing candidate, canary and promotion procedure, with explicit authorization. Do not remove the old installation, replace its pin, delete the fallback file, rewrite Keychain entries, copy Codex auth files or relax isolation as an unverified repair.

## Follow-up: implementation review failures

The operator supplied two additional failed review job IDs after the initial investigation. Neither establishes another authentication loss, and neither returned an independent implementation review. This investigation inspected saved metadata and the short CLI error diagnostic; it did not resend the implementation source or review prompts.

### Full review: `330fd2a8-b37f-4e87-abf9-cbbc2f8ca850`

The job submitted 45,879 prompt characters to the same pinned Claude 2.1.229, model alias `opus`, effort `xhigh`, 300-second timeout, 5-second termination grace and configured $2 per-call budget. Both attempts timed out, after 302.676 and 302.692 seconds. Each recorded exit 143, SIGTERM sent to its process group, and no SIGKILL. Total job time was about 605 seconds.

No CLI output explaining the delay was retained in bridge quarantine; the worker log was empty. Metadata from the two matching Claude session transcripts shows one assistant thinking block per attempt (14:57:52 UTC and 15:03:49 UTC), with no final answer or StructuredOutput calls recorded. This confirms model activity during both attempts. The timeout establishes that the bridge's time limit was reached, not how time was divided between reasoning and server/network waiting. The larger prompt and high effort may contribute to latency, but that is a hypothesis. There is no evidence here that the budget was exceeded, authentication failed, or a review finished. Timeout and retry limits were not changed.

### Smaller review: `4791da17-eb5b-4dc9-8849-c900c83b831f`

This 9,147-character request used the identical configuration hash and peer settings. Both attempts exited 1, after 138.886 and 98.174 seconds; neither timed out. Both attempt records contain:

- `envelope_subtype: error_max_structured_output_retries`
- `envelope_is_error: true`
- `terminal_reason: structured_output_retry_exhausted`
- `num_turns: 6`

The retained second-attempt envelope has no `result` and reports: `Failed to provide valid structured output after 5 attempts`. The first attempt's raw envelope was not retained, but its subtype and terminal reason were recorded in provenance. Model-usage/cost fields were present. The final envelope's cost is not a trustworthy total for the entire two-attempt job.

Metadata and tool-error diagnostics from the two exact matching Claude session transcripts narrow this further. Each attempt made five `StructuredOutput` calls and received five error results. Attempt one had three JSON parsing failures and two schema violations; attempt two had one JSON parsing failure and four schema violations. One diagnostic describes invalid JSON with a missing value; another identifies missing required contract fields. No raw review content or thinking text is reproduced here.

This establishes that the structured-output tool was available and that Claude failed to supply valid arguments. It does not establish why those malformed/incomplete arguments were generated, whether the underlying review reasoning was correct, or whether a version-specific CLI/model interaction contributed. It supplies no basis for enabling other tools or weakening isolation. Successful short canaries demonstrate that the contract can work; they do not prove it works reliably for substantive implementation reviews.

### Narrow correction

The bridge previously treated this specific terminal condition as generic `peer_nonzero_exit`, eligible for an identical outer retry. That obscured an actionable diagnostic and repeated the CLI's own exhausted correction loop. The backend now recognizes the exact error-envelope subtype/terminal reason as `peer_structured_output_exhausted`, with a constant explanatory hint and no automatic bridge retry. Normal schema-validation failures retain their existing corrective retry; unknown exits retain their existing handling. No raw peer errors are returned to callers.

Synthetic regressions reproduce the observed envelope (including the missing `result`), verify that a successful envelope cannot trigger the new category, and run a real fake-peer worker to verify one attempt, quarantine, caller messaging and retry refusal. The review source was not resubmitted, and no additional live model calls were made for this follow-up.

The local prototype and parent-repository test totals provided by the operator are not independent review approval and were not reverified by this bridge investigation. The review remains incomplete.


For compatibility with already-running MCP servers, worker status keeps the
existing `peer_output_schema_invalid` category and adds the closed
`diagnostic_category: peer_structured_output_exhausted`. Updated MCP readers
return the specific category and hint; older readers still report a recognizable
schema error rather than `internal_error`. The worker's no-retry decision and
specific provenance apply immediately. The detailed caller hint becomes available
when that MCP server next starts normally; no active app was restarted.


Final follow-up validation: **17 targeted tests passed**; the complete host-visible
offline suite passed **538 checks, 0 failures, 0 skips** after the compatibility
change. Existing effective configuration remained identical. Historical jobs,
credentials, limits, billing configuration and active app processes were not
modified. This fixes diagnosis and the redundant retry, not the model's ability
to complete the requested substantive review.
