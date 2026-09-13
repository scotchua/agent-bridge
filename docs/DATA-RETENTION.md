# Data retention

agent-bridge keeps its runtime state as plaintext files under
`~/.agent-bridge` by default. The active `config/local.json` remains in the
checkout, canary output goes to the durable path you choose, and setup may make
owner-only backups beside provider configuration files that it changes.

The operating system protects files written by the bridge: POSIX directories
and files are restricted to the owner, and native Windows uses owner-only ACL
checks. This is access control, not encryption at rest. Anyone who can act as
your account, an administrator or the system may be able to read the files.

Local state can include consultation prompts and validated responses, captured
peer output and errors, job status and provenance, provider conversation or
session identifiers, isolated peer workspaces, setup answers, configuration
snapshots, the isolated Codex home and provider CLI authentication state, and an
append-only exchange ledger. The ledger stores audit metadata and
prompt/response hashes rather than the full validated prompt and response, but
it can still reveal operational details such as when a consultation ran, which
peer received it, its classification and its outcome. Never share the state
directory, config, canary output or backups as troubleshooting attachments.

## The 30-day defaults

The default configuration makes job payloads and conversation records eligible
for cleanup after 30 days. It does not run cleanup on a schedule and does not
delete anything automatically.

From the checkout, preview eligible files first. On macOS/Linux:

```text
./bin/agent-bridge-admin cleanup
```

After reviewing the exact paths, apply that cleanup explicitly:

```text
./bin/agent-bridge-admin cleanup --apply
```

On Windows, use `.\bin\agent-bridge-admin.cmd cleanup` for the preview and
`.\bin\agent-bridge-admin.cmd cleanup --apply` after reviewing it.

Cleanup is limited to eligible job directories, conversation records and their
recorded workspaces inside the configured state root. The append-only ledger is
never deleted or truncated by this command, regardless of the retention
settings. Changing the day values in `config/local.json` changes eligibility;
it still does not schedule cleanup.

## Uninstall and provider records

The guided uninstaller removes bridge registrations and managed instruction
blocks that it can identify. It retains local consultation history, the shared
instruction file, setup records, backups and bridge configuration for
inspection or reuse. Review [the removal procedure](SETUP-WITH-AN-AGENT.md#removing-the-setup)
before applying it.

Claude, Codex and an optional Ollama service may keep their own conversation,
account or service records. Those histories are controlled by their respective
products and account policies. Local cleanup and bridge uninstall do not delete
provider-side history.
