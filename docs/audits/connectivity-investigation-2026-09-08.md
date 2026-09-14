# Claude bridge authentication recurrence — September 8 UTC / September 7 Pacific

## Finding

The default macOS Keychain credential is readable but its Claude OAuth access and refresh tokens are empty, with `expiresAt: 0`. A separate fallback file has nonempty tokens and a future access-token expiry. The bridge-pinned Claude 2.1.229 prefers any non-null Keychain object over that file, including an object containing empty tokens. This establishes why the current bridge cannot use the apparently newer fallback credential. File expiry alone does not prove server acceptance.

This is more specific than the September 6 finding, when both stores still contained tokens. Credential values, hashes, account identifiers and raw private logs were not emitted or saved.

## Evidence

- Failed request `f9ccbbf7-73e0-41f7-9c7a-26b06b94f5f9`, September 8 01:39:03–01:39:04 UTC: `peer_auth_failure`, reason `oauth_refresh_failed`, sourced from the CLI error envelope.
- Health reported signed out both in the restricted shell and with approved normal host access. Keychain read itself returned success with normal host access. Thus this observation is not just the previously reproduced sandbox visibility problem.
- Keychain service `Claude Code-credentials`, normal user account: OAuth object present; access and refresh tokens empty; expiry zero; Team subscription metadata retained.
- Keychain modification time: September 7 **16:02:00 UTC / 09:02:00 Pacific**. This falls inside failed bridge job `c6b2bc87-1579-496a-bdcb-bd8295d7e577` (16:01:59.817–16:02:04.190 UTC), which also reported `oauth_refresh_failed`. Timing strongly implicates that invocation but does not identify the writer conclusively.
- Fallback `~/.claude/.credentials.json`: nonempty access and refresh tokens; modified September 8 00:38:27.834 UTC; access-token expiry September 8 08:38:27.834 UTC. It is not the old September 4 fallback recorded in the prior investigation. No credential was submitted to a server to validate it.
- Current bridge pin remains 2.1.229; standalone Claude and two observed Desktop engine processes use 2.1.260. Current process presence is not historical evidence of concurrency at the write time.

## Installed implementation

Read-only inspection of printable code embedded in the pinned executable establishes:

1. `kku` wraps Keychain with plaintext fallback. Both synchronous and asynchronous reads return the primary object whenever it is non-null; they do not check whether its OAuth tokens are empty before returning it.
2. `Lvr` marks a refresh token dead and uses a storage mutation to blank `refreshToken` and `accessToken` and zero `expiresAt`, preserving other fields. It first checks that the stored refresh token still equals the failed token. Its event name identifies the invalid-grant path.

Consequently the empty state matches an implemented invalid-grant cleanup path. This code includes a concurrent-token equality guard; it would be inaccurate to claim there is no concurrency protection. Available evidence does not establish why the token became invalid, whether a race defeated that protection, or whether another process performed the write.

Fixed-marker scans found no refresh/keychain diagnostics in the available CLI telemetry. Desktop logs contain older invalid-grant markers, but these have not been linked to this exact event or credential family. No raw logs were copied into this report.

## External corroboration and limits

[Anthropic issue 88583](https://github.com/anthropics/claude-code/issues/88583) remains open and reports the same blank-token/zero-expiry pattern with 2.1.229 and concurrent Desktop sessions. Its proposed race explanation is a reporter's hypothesis, not a confirmed cause on this machine.

[Official authentication documentation](https://code.claude.com/docs/en/authentication) documents separate Keychain entries when `CLAUDE_CONFIG_DIR` differs and a one-year `setup-token` option for scripts. The bridge intentionally strips inherited OAuth/API token variables; using setup-token would require a reviewed credential-loading design, not putting a secret in its JSON configuration.

The upstream changelog contains earlier concurrency fixes, including 2.1.126 and 2.1.133, both older than this pin. Those entries do not establish that switching to 2.1.260 fixes this recurrence.

## Recommended repair and validation

Give the bridge a dedicated Claude configuration directory and a fresh, separately authorized subscription login, so Desktop and ordinary CLI sessions do not write the same Keychain entry. Preserve the existing bridge tool/customization isolation and run the candidate/canary/promotion workflow. This is a containment measure, not proof that every upstream refresh defect is eliminated. Merely copying existing tokens would not establish an independent login and should not be used.

After browser authorization, validate a short synthetic start and continuation, then validate again after natural token expiry with metadata-only before/after observations. Do not claim a durable repair from a pre-expiry success. An isolated configuration still needs refresh behavior validated under bridge concurrency.

No active configuration, credentials, executable pins, running sessions or billing settings were changed. No login, forced refresh or repeated failed consultation was attempted. A browser authorization step is needed for the proposed independent login. Investigation completed; service recovery and refresh-boundary validation remain outstanding.

## Authorized repair completed — September 8, 03:43 UTC

The user subsequently authorized implementing the separate login and restoring service. Created `~/.agent-bridge/claude-home` with owner-only directory permissions and completed browser authorization using the existing pinned Claude executable with that `CLAUDE_CONFIG_DIR`. Health confirmed the Team subscription login in this separate context; no fallback credential file was present there.

Staged a candidate changing only `peers.claude.extra_env.CLAUDE_CONFIG_DIR`. The active configuration was checked again before promotion to ensure no intervening changes would be overwritten. Previous local and effective configurations were retained privately alongside the candidate.

Verification:

- Authentication-related tests: 17 passed.
- Full restricted offline suite: 520 passed, 0 failed, 5 process-enumeration checks skipped by the sandbox.
- Full live matrix with normal host access: PASS; 20 standalone calls, 18 conversation turns across six conversations, two schema-pressure calls, and two reachability probes. All 40 graded model calls succeeded on their first attempt; all continuations preserved the expected session identity.
- Both controlled timeout tests passed with zero orphan processes; no live controls were skipped.
- The original Parallels question succeeded under the candidate login: job `4ce258cd-2a17-42ed-a17e-6deba840cc16`.
- Promoted through the existing verified promotion function. Active effective hash: `b1f225b2d617f8d777d9fee194a418aeecf64e3d9246c2ecbace3658d73a61b1`.
- A fresh stdio MCP server loaded the promoted configuration and successfully completed `claude_start`, `claude_poll`, `claude_read`, and `claude_close`: job `8346d154-a525-4040-97c2-8f5ae6cd4139`. Claude confirmed receipt and returned a valid response contract.

Private validation artifacts and rollback copies are in `.claude-codex-relay/auth-repair-20260908/`. No existing shared credential was copied, deleted or overwritten by the repair; executable pins, billing and tool/customization isolation remain unchanged.

Service is restored for newly launched bridge servers. The already-running Codex MCP server holds its old configuration in memory and needs a normal reconnect/app restart. Computer-use access to the Codex app was refused, and no alternative UI-control mechanism or process termination was used. The user must perform that final client reload when convenient.

Natural expiry/refresh-boundary validation remains outstanding. Pre-expiry successes prove current connectivity and conversation continuity, not permanent elimination of upstream refresh defects. No recurring monitor was scheduled.
