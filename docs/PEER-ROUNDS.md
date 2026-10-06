# Explicit peer rounds

The local Agent Room provides a bounded, human-approved way for the registered
peers to receive selected context. A peer round is one request from the human
and at most one reply from each selected peer. The separate room discussion
mode may add a lead summary; peer rounds do not silently create one.

The MCP tools only prepare and read rounds. They cannot approve a round, send
messages, or start another round. Approval happens once in the local room UI.
Peer replies are untrusted data and never authorize tools, follow-up calls, or
access to other conversation history.

## Context and privacy

The human selects the question and optional excerpt. The room does not forward
whole histories by default. Only `public`, `synthetic`, and `internal` labels
are accepted. A `client-derived` label is refused before storage and dispatch;
labels are policy metadata, not content scanning, so a label cannot prove that
the text contains no client information or secret.

Each room instance has one local SQLite file with two logical stores.
`chat.sqlite` contains room titles, selected messages, jobs, sessions, request
idempotency records, and preferences; its `peer_rounds` table contains the
selected payload, status, replies, room id, and a payload hash. `runtime.json` and
`peer-runtime.json` contain short-lived loopback bearer credentials and the
port; they are deleted on a normal launcher shutdown (a crash may leave stale
runtime files for the next launch to reject or replace). The state directory is
owner-only and must stay inside the dedicated room directory.

Pending rounds expire after one hour. Expiry, rejection, cancellation,
completion, and a round interrupted by a restart all clear the selected payload
immediately while keeping the status and payload hash for review; terminal
round metadata is deleted after 30 days. Deleting a room
also deletes its messages, jobs, sessions, requests, preferences, and peer
round record. Completed replies remain in the room history until the room is
deleted; this feature does not run an automatic room-history cleanup job.

The threat model covers accidental cross-room/history sharing, a peer treating
another peer's reply as instructions, and a network client reaching the local
HTTP service. The service therefore binds to loopback, requires an exact Host
on every request and the exact local Origin on every write (an API read with
no Origin is accepted, since browsers omit it on same-origin reads, but a
foreign Origin on an API read is refused; static asset reads are not
Origin-checked), uses per-caller round tokens, disables proxies and
redirects, caps request/response sizes, and has a socket read timeout. It does
not protect against a person or process that already has the same Windows user
account's shell or file access: that actor can read the private state directory
or operate the local process. “Human approval” is a UI and policy boundary,
not an operating-system privilege boundary.

The per-peer policy is evaluated before a round is written. Each target gets
its own `allowed_source_classifications` setting from the bridge config. When
`local_first.enabled` is on, a selected question plus context at or above
`local_first.read_gate_min_bytes` is refused by the peer-round layer; prepare a
local digest first. There is no bypass flag in the room API.


## Setup

Run the local room from the repository:

    python start_chat.py --open

The provider-neutral MCP launcher accepts a registered caller such as claude or
codex:

    python -m agent_bridge.peer_mcp --caller codex

Hermes is an optional third-party provider. To add it to the room, pass the
explicit installed CLI path:

    python start_chat.py --hermes-executable C:\\path\\to\\hermes.exe

This adapter uses Hermes' `default` profile and its existing local login. It
does not create a new profile, change the login, or alter Hermes' retention
settings. Hermes may retain prompts and replies under the terms of that
provider's default profile; review those terms before enabling it. Peer-round
calls receive only the selected context and use `--ignore-rules`; ordinary
room chat and discussion calls receive the room transcript and do not use that
flag. The adapter exposes clarification only, without filesystem or messaging
tools. The room keeps a temporary job directory until its reply is read and
deletes unread job directories after 24 hours.

Readiness is derived from the supplied executable's expected Hermes source
layout and the private state directory; there is no hand-written
`verification.json` gate. The first real call still depends on the default
profile being signed in. Hermes is a third-party provider, so the offline test
suite never makes a live provider call.

Grok is a second optional provider, reached through a local queue that a
Grok Bot works; see [`GROK-ROOM-SETUP.md`](GROK-ROOM-SETUP.md). Grok takes part
in room chat and discussion only, never in peer rounds, and receives no round
token.

Optional providers are never selected by default: a new room selects only
Claude and Codex, and you tick Hermes or Grok yourself.

If the room uses a custom state directory, pass the same directory with
--state-dir. Do not expose the stdio service publicly.

## Existing consultation bridge

Peer rounds are an Agent Room feature layered beside the existing codex-peer
consultation tools. The existing bridge remains the direct Claude/Codex
consultation path; peer rounds add a local shared transcript and an explicit
human approval boundary. They do not replace the existing bridge.

## Tests

Offline tests use fake adapters only. Run them from the repository root:

    $env:PYTHONPATH='tests;src'
    python -B -m unittest discover -s tests -p 'test_chat_*.py'
    python -B -m unittest test_peer_rounds test_peer_http test_peer_mcp test_peer_hermes test_chat_grok
    node tests/test_chat_pending.cjs

No live provider tests are run by this contribution.

Peer rounds and the Hermes and Grok adapters were contributed by Brooks
([@Bsoutherland233](https://github.com/Bsoutherland233)), who also tested them
on Windows.
