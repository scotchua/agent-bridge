# Grok Queue Hardening Design

## Goal

Make the optional Grok queue safe, explicitly opt-in, and reviewable so PR #20 can proceed to Scott's final review. Agent Bridge itself must continue to make no xAI or Grok API call.

## Scope

This design addresses every open item from Scott's September 30 review that belongs in PR #20:

1. New rooms must select only built-in providers by default. Optional `hermes` and `grok` providers appear only after a person selects them.
2. The CLI helper's default queue must be outside the default room state root, matching the launcher's separation rule.
3. The manifest and documentation must declare the complete local execution boundary. The supported reply path will be standard input only; the `--file` variant will be removed so the Bot does not need undeclared file-write access.
4. Queue state transitions (`poll`, `read`, `receive`, and `respond`) must use the existing private `queue.lock` so a completion cannot be lost or read during another transition.
5. Room deletion and scheduled queue retention must remove expired or orphaned queue records safely, with tests for both paths.
6. Grok must not receive peer-round tokens or MCP-peer treatment. Its readiness is documented as launch-time opt-in; it is not represented as a continuously connected peer.
7. Tests must cover default selection, expiry with controlled time, record validation, JavaScript recipient selection, and the supported reply boundary.

## Explicit Non-Goals

- No direct xAI or Grok API call.
- No unattended reply or automatic send.
- No claim that a check-in proves continuous Bot availability.
- No invented product/version. The manifest remains operator-supplied; the PR body will identify the actual product, version, and approved tools once Brooks supplies them.

## Architecture

The room store remains the authority for saved participant preferences. It will compute defaults from the configured participant list while excluding optional providers by identifier.

The Grok adapter remains a private file queue. A single lock wraps every record load, state mutation, completion read, and cleanup decision. The helper exposes only `next` and stdin-backed `reply`; the manifest, docs, and CLI match this exact boundary.

Room lifecycle code will notify the Grok adapter when a room is deleted so queue records associated with that room are removed. A small adapter cleanup entry point will remove only expired terminal records and safely reject malformed records.

## Error Handling and Safety

- Any invalid, expired, or missing record fails closed and is quarantined or refused without a reply.
- Optional providers never become default recipients merely because they are ready.
- Cleanup holds `queue.lock` and deletes only records it can validate as terminal and past retention.
- Removing `--file` avoids a hidden local file-writing capability.

## Validation

- Focused Python tests for the Grok adapter, room storage, room management, and launcher.
- JavaScript pending-recipient test for Grok opt-in behavior.
- The complete chat test suite locally, followed by all six GitHub CI legs after the PR update.

## Product Information Needed Before Merge

The installation operator must supply the exact Grok Bot product name, version, and the complete approved tool list. Until then the queue stays a draft-only optional integration and the PR remains in draft.
