# Data-flow matrix: agent-bridge

Generated from `src/agent_bridge/routes.py`. Do not edit by hand; edit
the registry and regenerate, so what is approved and what is enforced
stay the same thing.

**Client-derived gate: SHUT.** While shut, no destination may receive
tax return information or any client-derived material, whatever a peer's
own ceiling says. Opening it requires both lifting the gate and raising a
peer's ceiling, in a reviewed change.

## What each destination is

**claude** (cloud) — ceiling `internal`, general consultation

> Runs with customizations, MCP servers and built-in tools disabled, in an empty working directory, under a hard spend ceiling. Sent only your question. Third-party API: never send client-derived material.

**codex** (cloud) — ceiling `internal`, general consultation

> Runs with no user config and no rules files, in an empty working directory, sandboxed against writes. The sandbox restricts writes, not reads, so treat the prompt itself as the confidentiality boundary. Third-party API: never send client-derived material.

**local** (local) — ceiling `internal`, tasks: summarize, triage, classify, extract, redact, route certificate required

> Runs on this machine via Ollama and makes no network call, so nothing leaves the hardware. It is a small model restricted to five certified tasks. It can silently omit content while returning well-formed output: treat every reply as a draft to be checked, never as a complete account of the input.

## Permitted flows

| From | To | Leaves this machine | May receive | For | Certificate |
|---|---|---|---|---|---|
| claude | codex | **yes** | public, synthetic, internal | (general consultation) | n/a |
| claude | local | no | public, synthetic, internal | summarize, triage, classify, extract, redact | required |
| codex | claude | **yes** | public, synthetic, internal | (general consultation) | n/a |
| codex | local | no | public, synthetic, internal | summarize, triage, classify, extract, redact | required |

Any pair absent from this table is denied. Absence is the default.

## What counsel is being asked

Nothing in the table above sends client-derived material anywhere; the
gate is shut and every ceiling sits at `internal`. Two questions decide
whether that can change, and neither is answered:

1. Is transmission to a third-party LLM API a disclosure of tax return
   information under IRC 7216, and if so does any exception apply? A
   negative answer permanently fixes the two cloud rows at `internal`.
2. Does execution on the firm's own hardware, with no network call,
   fall outside 7216's disclosure concept entirely? If yes, the `local`
   rows could carry client-derived material that the cloud rows never
   can, which is the whole privacy argument for running locally.

A third question if de-identification is contemplated: does removing
names remove identifiability when figures, dates and jurisdictions
remain? Inside a two-office practice, a deliverable carrying exact
revenue and a Ketchikan borough filing may still identify its client.

## What is enforced regardless of the answers

- Consultation only. No peer can edit files, run commands, or start a
  consultation of its own.
- Replies are schema-validated; raw peer text is quarantined and never
  reaches a caller.
- Every exchange is ledgered.
- The local peer is restricted to certified tasks and cannot be asked an
  open question, because it can omit content while returning well-formed
  output.
