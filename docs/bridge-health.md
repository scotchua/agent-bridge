# When a consultation stops working

Run this in a normal macOS Terminal:

```sh
bin/agent-bridge-admin health
```

This checks both pinned CLIs and their local login visibility. It does not sign you in, change configuration, or send a model request. Exit 0 means both local checks passed; it does not guarantee the network or next token refresh will work. Exit 1 means a check failed or could not establish status.

| Health result | Next step |
| --- | --- |
| Both peers signed in | Try one short synthetic consultation. If it fails, retain the job ID and ask the bridge maintainer to inspect that failure. |
| Claude signed out only in a restricted Codex shell | Run the same check in normal Terminal. If it passes there, investigate the failed launcher's Keychain access; do not replace a working login. |
| Claude signed out in normal Terminal too | Ask the operator to review the recent failure before authorizing login with the **bridge-pinned** executable. Signing into Claude Desktop is not this check. |
| Codex signed out | Review the isolated `CODEX_HOME` shown by health. A working desktop/default-home login does not establish this peer's status. |
| Version mismatch or missing executable | Have the maintainer verify a candidate through the existing canary/promotion process. Do not clear the allowlist or rerun setup to silently select a different CLI. |
| Different PATH CLI | This is a maintenance warning. The bridge keeps its tested pin. It is not proof of an authentication failure. |
| Structured-output retries exhausted | Claude failed to produce the required response format after its own retries. Ask the maintainer to inspect the CLI/schema interaction; do not log in again or repeatedly resend the same review. No review was completed. |
| Peer timeout | The configured time limit was reached. Keep the job ID; do not assume this is an authentication failure or raise limits without review. |
| Unknown | The CLI did not report recognizable status, timed out, or could not be checked. Do not treat unknown as logged out. |

For a maintainer, capture non-secret context immediately after a failure:

```sh
bin/agent-bridge-admin health --json
bin/agent-bridge-admin ledger --tail 10
```

Keep the failing job ID, timestamps and health output. Health JSON provides the exact pinned login argv for an operator **if login is subsequently authorized**, plus the environment context it needs. Codex login must use the displayed isolated `CODEX_HOME`. Do not execute a bare shell-default login and assume it targets the bridge.

Ask the maintainer to compare the job's `peer_auth_failure_reason`, attempt notes, credential context, CLI version and config hash. `oauth_refresh_failed` identifies the failure stage; it does not establish revocation, Keychain failure, or concurrent refresh as the cause. Older jobs lack the new fields and require careful local inspection of quarantine. Do not paste raw quarantine, credentials, tokens, email addresses or client prompts into a ticket.

Repeated browser logins and app restarts are not the long-term repair procedure. On September 6 we reproduced a context-dependent Keychain visibility problem and confirmed earlier OAuth refresh failures, but could not establish the original refresh failure's underlying cause. See the [investigation and test record](audits/connectivity-investigation-2026-09-06.md).
