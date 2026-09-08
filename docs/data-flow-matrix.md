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
claims.

The registry can enforce approved routing. It cannot prove the legal
predicates behind that routing: same-preparer status, U.S.-only access, no
third-party disclosure, permitted purpose, and non-identifiability.

- every access surface stays within the same U.S. tax return preparer: prompts, outputs, logs, model state, telemetry, backups, admin consoles and support channels
- no access by personnel outside the United States, because Reg. 301.7216-2 requires consent for disclosure to non-U.S. personnel even within the same firm
- model operation stays within the same U.S. preparer: no hosted inference, no telemetry or crash reporting carrying return information
- administration of the machine stays within the same U.S. preparer
- the use is return preparation, an auxiliary service, or another use permitted under IRC 7216 and Reg. 301.7216-1 through -3

## The firm's expected answers

Position of Scott Edwards, CPA, 2026-09-08, from Thomson Reuters Checkpoint
research. A draft for licensed review, not final authority. Counsel is being
asked to confirm or correct each one.

**1. Cloud rows.** Treat transmission of client-derived tax return information
to a third-party LLM API as a **disclosure**, unless counsel identifies a
specific IRC 7216 / Reg. 301.7216-2 exception or the firm obtains valid
taxpayer consent under Reg. 301.7216-3. Reg. 301.7216-1 defines disclosure
broadly as making tax return information known to any person in any manner,
and tax return information includes client-furnished information as well as
preparer-derived computations, worksheets and workpapers. Absent an applicable
exception or consent, **the cloud ceiling stays below client-derived tax
return information.** That is the ceiling the registry ships with.

**2. Local rows.** Local inference can support a higher ceiling **only if** the
deployment truly remains inside the same U.S. tax return preparer and is used
only for permitted return-preparation, auxiliary-service or other authorized
purposes. Reg. 301.7216-2 permits use or disclosure among officers, employees
or members of the same U.S. preparer for those purposes, but disclosure to
personnel outside the United States requires consent.

This makes the question factual as much as legal: who can reach prompts,
outputs, logs, model state, telemetry, backups, admin consoles and support
channels. Those are the surfaces the conditions above enumerate, and none of
them is something this repository can verify.

**3. De-identification.** Removing names and direct identifiers is **not
enough** if the remaining facts can indirectly identify the client. Reg.
301.7216-2(o) requires anonymized or statistical information to be in a form
that cannot be associated with, or otherwise identify, directly or indirectly,
a particular taxpayer. Exact amounts, dates, jurisdictions, entity facts,
filing details and unique high-net-worth transactions can each function as a
cell-of-one identifier without a name attached.

Consequence for this registry: **redaction is a task, not a downgrade.** The
local peer may be asked to redact. Its output does not thereby become a lower
classification, and there is deliberately no de-identified tier in the
classification ladder. Adding one would be a consequential change requiring
its own approval, not a convenience.

### The one-line version, and the two places it is too short

Working summary: **local work kept entirely inside the firm does not require a
7216 consent; sending client tax return information to either cloud API does.**
That is the right instinct for day-to-day routing. Two refinements before it
becomes the rule anyone quotes.

**Local is not outside 7216, it is permitted under it.** IRC 7216 governs USE
as well as disclosure. Internal use is permitted because Reg. 301.7216-2
allows officers, employees and members of the same U.S. preparer to use return
information for return preparation and auxiliary services, not because the
statute stops at the firm door. The purpose limit therefore still binds: using
client return information locally for something other than that engagement,
training or fine-tuning a model on it being the obvious temptation, is a
different use and may require consent even though nothing left the building.

**Cloud may not always need a consent.** Consent under Reg. 301.7216-3 is one
route. An applicable exception is the other, and whether a cloud LLM provider
can sit inside one as a provider of auxiliary services, subject to the
conditions and to the non-U.S. limits, is exactly open question 1. Treating
consent as the only path would be safe but would also concede the question
before counsel answers it.

Sources: Thomson Reuters Checkpoint, Key Issue 33J; RIA ¶ V-3316; ¶ V-3310.3;
¶ S-6207. Primary authority: IRC 7216, IRC 6713, Reg. 301.7216-1, -2, -2(o),
-3.

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
