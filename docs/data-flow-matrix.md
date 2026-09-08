# Data-flow matrix: agent-bridge

Generated from `src/agent_bridge/routes.py`. Do not edit by hand; edit
the registry and regenerate, so what is approved and what is enforced
stay the same thing.

**Client-derived gate: SHUT.** While shut, no destination may receive
tax return information or any client-derived material, whatever a peer's
own ceiling says. Opening it requires both lifting the gate and raising a
peer's ceiling, in a reviewed change.

**Status: the registry describes, it does not yet enforce.** Seven call
sites still resolve peers directly and never consult it. Until one
dispatch gateway owns backend access and every call passes through it,
the flows below are intended policy, not a boundary the code imposes.
Wiring those sites is a separate reviewed change. Read this document as
what is being proposed for approval, not as a description of what runs.

## What each destination is

**claude** (cloud). Ceiling `internal`, general consultation

> Runs with customizations, MCP servers and built-in tools disabled, in an empty working directory, under a hard spend ceiling. Sent only your question. Third-party API: never send client-derived material.

**codex** (cloud). Ceiling `internal`, general consultation

> Runs with no user config and no rules files, in an empty working directory, sandboxed against writes. The sandbox restricts writes, not reads, so treat the prompt itself as the confidentiality boundary. Third-party API: never send client-derived material.

**local** (local). Ceiling `internal`, tasks: summarize, triage, classify, extract, redact, route certificate required

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

Framing by Scott Edwards, CPA, 2026-09-08, assuming the inputs are tax return
information obtained in a return-preparation engagement.

**Cloud destinations: treat this as a disclosure.** Reg. 301.7216-1 defines
disclosure broadly as making tax return information known to any person in any
manner, and tax return information includes both client-furnished information
and preparer-derived computations, worksheets and workpapers. So the default is
not "cloud is capped because no disclosure occurs". It is **cloud may receive
only non-client-derived material unless a specific exception applies or the
taxpayer has given a Reg. 301.7216-3 consent.**

A third-party technology provider is not automatically prohibited.
Non-substantive processing, software and equipment services can be permissible
subject to the regulatory conditions, including limiting the disclosure to what
is necessary and giving written notice of the 7216 and 6713 obligations where
required. But a provider making substantive determinations or giving tax advice
affecting liability requires taxpayer consent first.

**Local destinations: more plausibly internal use than disclosure.** Inference
on the firm's own hardware, with no network call and no access by anyone
outside the same U.S. tax return preparer, fits Reg. 301.7216-2, which permits
an officer, employee or member of the same U.S. preparer to use or disclose
return information internally to assist in preparing the return or providing
auxiliary services. **That is why a local ceiling can sit above a cloud
ceiling.** It is conditional, not automatic.

### The three questions

Deliberately not phrased as "is the API a disclosure", which the regulation
makes a hard position to hold.

1. Does any specific IRC § 7216 / Reg. § 301.7216-2 exception apply to the
   contemplated cloud API use? If not, must the cloud ceiling remain below
   client-derived tax return information absent taxpayer consent under
   Reg. § 301.7216-3?
2. Does the architecture genuinely keep local inference inside the same U.S.
   tax return preparer, with no disclosure to a separate person or non-U.S.
   personnel, and only for permitted return-preparation, auxiliary-service,
   or other authorized uses?
3. If de-identification is later relied on, does removing names and direct
   identifiers sufficiently remove identifiability where amounts, dates,
   jurisdictions, entity facts, or filing details may still point to a
   specific client?

### Conditions the local answer depends on

The five local internal-use conditions are stated as deployment claims, not
test-proven facts. Raising a ceiling requires re-reading and affirming those
claims. The code can enforce the registry; it cannot prove the legal
predicates behind the registry.

- access to the model and its inputs stays within the same U.S. tax return preparer
- logging and stored artifacts stay within the same U.S. preparer
- model operation stays within the same U.S. preparer (no hosted inference, no telemetry carrying return information)
- administration of the machine stays within the same U.S. preparer
- the use is return preparation, an auxiliary service, or another use permitted under IRC 7216 and Reg. 301.7216-1 through -3

## What is enforced regardless of the answers

- Consultation only. No peer can edit files, run commands, or start a
  consultation of its own.
- Replies are schema-validated. Raw peer text is quarantined and never reaches
  a caller.
- Every exchange is ledgered.
- The local peer is restricted to certified tasks and cannot be asked an open
  question, because it can omit content while returning well-formed output.

### Why the task envelope has two independent justifications

The five certified local tasks (summarize, triage, classify, extract, redact)
are all non-substantive processing. They were chosen for a capability reason:
the firm measured a local model returning 59 or 60 lines from a 61-line input
on every run, while looking perfect on an 11-line fixture, so a reply can be
well-formed and quietly incomplete.

They also land on the permissive side of the regulatory line, which separates
non-substantive processing from substantive determinations and tax advice
affecting liability. The two arguments are independent and agree. The envelope
is worth keeping even if one of them later changes.
