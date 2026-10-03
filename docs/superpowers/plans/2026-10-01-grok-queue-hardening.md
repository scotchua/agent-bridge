# Grok Queue Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make PR #20's optional Grok queue safe, explicitly opt-in, lifecycle-clean, and ready for Scott's final review.

**Architecture:** Keep `RoomStore` responsible for room state and add a narrow dispatcher cleanup hook for any provider job cancelled by room deletion. Keep Grok as a private local file queue: the helper reads one job and writes a reply through standard input; Agent Bridge never calls xAI or Grok directly.

**Tech Stack:** Python 3, `unittest`, SQLite, local JSON queue files, existing browser JavaScript tests.

**Spec:** `docs/superpowers/specs/2026-10-01-grok-queue-hardening-design.md`

## Global Constraints

- Grok remains optional and only appears after `--grok-state-dir` is explicitly supplied and ready at launch.
- Agent Bridge must make no direct xAI/Grok API calls and must never send unattended replies.
- Grok's queue directory must be private and outside the Agent Room state directory.
- The helper's documented and manifest-declared commands must exactly match its actual interface.
- Do not invent the Bot product/version/tool list; Scott supplies those factual fields before PR review.

## Review Focus

- A ready optional provider must not silently be selected in a new room; test the default preferences with Grok and Hermes registered.
- A queue request claimed by two concurrent readers must have exactly one winner; test `receive()` under the queue lock.
- An expired response must not turn back into a completed response; test with controlled time.
- Deleting a room while a Grok request is active must cancel its queue record; test the server's delete route through the dispatcher.
- The local helper must reject obsolete file-path input and accept only UTF-8 standard input; test CLI parsing and manifest command validation.

---

### Task 1: Make optional providers opt-in by default

**Files:**
- Modify: `src/agent_bridge/chat/storage.py:preferences`
- Test: `tests/test_chat_discussion.py`

**Interfaces:**
- Consumes: `RoomStore.participants: tuple[str, ...]`
- Produces: `RoomStore.preferences(room_id) -> dict` whose new-room participant list contains only built-in providers.

- [ ] **Step 1: Write the failing default-preferences test**

Create a `RoomStore` with `('claude', 'codex', 'hermes', 'grok')`; assert a new room returns lead `claude` and participants `['codex']`.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_chat_discussion.DiscussionTests.test_new_room_discussion_includes_registered_providers -v`

Expected: FAIL because optional registered providers are selected automatically.

- [ ] **Step 3: Restrict `RoomStore.preferences` defaults to built-in peers**

Return only members of `PARTICIPANTS` other than the lead when no saved preferences exist. Preserve saved selections and all explicit user choices.

- [ ] **Step 4: Run focused tests**

Run: `python -m unittest tests.test_chat_discussion -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agent_bridge/chat/storage.py tests/test_chat_discussion.py
git commit -m "fix: keep optional peers out of new room defaults"
```

### Task 2: Tighten the private Grok helper contract

**Files:**
- Modify: `grok_room.py`
- Modify: `src/agent_bridge/chat/grok.py`
- Modify: `docs/GROK-ROOM-SETUP.md`
- Test: `tests/test_chat_grok.py`

**Interfaces:**
- Consumes: `grok_room.py reply --queue-dir <path> <job-id>` and UTF-8 text from standard input.
- Produces: `ALLOWED_COMMANDS` and the documentation with the identical two-command interface.

- [ ] **Step 1: Write failing helper-interface tests**

Assert the helper's default queue path is `~/.agent-bridge/grok` (outside `~/.agent-bridge/chat`), `reply` has no `--file` argument, and standard input is passed once to `respond`.

- [ ] **Step 2: Run focused helper tests**

Run: `python -m unittest tests.test_chat_grok -v`

Expected: FAIL because `--file` is still accepted and the default is inside the room root.

- [ ] **Step 3: Remove file input and move the default queue root**

Set the helper default to `Path.home() / '.agent-bridge' / 'grok'`. Remove `--file` parsing, file reads, and cleanup; always read bounded UTF-8 reply text from `sys.stdin`.

- [ ] **Step 4: Update the setup guide**

Replace every `--file` example with a standard-input example. State that `--grok-state-dir` is a launch-time opt-in, readiness is checked at launch and before each queued request, and a heartbeat is not continuous-availability proof.

- [ ] **Step 5: Run focused tests**

Run: `python -m unittest tests.test_chat_grok tests.test_chat_launch -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add grok_room.py src/agent_bridge/chat/grok.py docs/GROK-ROOM-SETUP.md tests/test_chat_grok.py
git commit -m "fix: narrow Grok helper to standard input replies"
```

### Task 3: Make queue operations atomic and validate time boundaries

**Files:**
- Modify: `src/agent_bridge/chat/grok.py:poll,read`
- Test: `tests/test_chat_grok.py`

**Interfaces:**
- Consumes: `GrokAdapter.poll(job_id)` and `GrokAdapter.read(job_id)`.
- Produces: queue-lock-protected state transitions and one-time reply retrieval.

- [ ] **Step 1: Write failing race and expiry tests**

Add a concurrent `receive(root, wait=0)` test that asserts exactly one caller gets a queued job. Add controlled-time tests showing `poll` turns queued/running work into `timed_out` exactly at expiry and `read` refuses non-complete records without deleting them.

- [ ] **Step 2: Run the new tests**

Run: `python -m unittest tests.test_chat_grok.GrokTests -v`

Expected: FAIL because `poll` and `read` access the queue without its lock.

- [ ] **Step 3: Lock every record mutation and read-delete operation**

Wrap `poll` and `read` in `store.file_lock(str(self.root / 'queue.lock'))`. Re-load records inside the lock, write timeout status inside the same lock, and delete a completed record only while holding it.

- [ ] **Step 4: Run focused tests**

Run: `python -m unittest tests.test_chat_grok -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agent_bridge/chat/grok.py tests/test_chat_grok.py
git commit -m "fix: serialize Grok queue state transitions"
```

### Task 4: Cancel external queue work when a room is deleted

**Files:**
- Modify: `src/agent_bridge/chat/dispatch.py`
- Modify: `src/agent_bridge/chat/server.py`
- Test: `tests/test_chat_management.py`
- Test: `tests/test_chat_grok.py`

**Interfaces:**
- Consumes: `RoomStore.snapshot(room_id)['jobs']` before deletion and adapters with optional `cancel(job_id)`.
- Produces: `Dispatcher.cancel_room(room_id) -> dict` that cancels active adapter jobs before `RoomStore.delete_room` removes their IDs.

- [ ] **Step 1: Write a failing room-deletion queue cleanup test**

Start a Grok-backed job, delete its room through the authenticated server route, then assert its queue file is cancelled or absent and cannot later be completed.

- [ ] **Step 2: Run the test**

Run: `python -m unittest tests.test_chat_management -v`

Expected: FAIL because deleting room database rows leaves the Grok queue record active.

- [ ] **Step 3: Add a narrow dispatcher cancellation hook**

Implement `Dispatcher.cancel_room(room_id)` to snapshot queued/running jobs, invoke an adapter's `cancel(job_id)` when available, then call `store.stop(room_id)`. Invoke it in the server delete route before `store.delete_room(room_id)`. Preserve deletion when adapter cleanup fails.

- [ ] **Step 4: Run management and Grok tests**

Run: `python -m unittest tests.test_chat_management tests.test_chat_grok -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agent_bridge/chat/dispatch.py src/agent_bridge/chat/server.py tests/test_chat_management.py tests/test_chat_grok.py
git commit -m "fix: cancel provider queue work when deleting rooms"
```

### Task 5: Prevent Grok from acting as an MCP peer

**Files:**
- Modify: `src/agent_bridge/chat/__main__.py`
- Modify: `tests/test_chat_launch.py`
- Modify: `docs/GROK-ROOM-SETUP.md`

**Interfaces:**
- Consumes: `build_app(..., grok_state_dir=...)`.
- Produces: peer-round participant/token collections limited to providers with an MCP peer interface.

- [ ] **Step 1: Write the failing launch test**

Launch with an explicitly configured ready Grok queue and assert Grok remains visible to the human room UI but is absent from `server.rounds_token` and `server.peer_rounds.participants`.

- [ ] **Step 2: Run the focused test**

Run: `python -m unittest tests.test_chat_launch -v`

Expected: FAIL because every ready provider receives a peer-round token.

- [ ] **Step 3: Separate UI participants from peer-round callers**

Build `PeerRounds` and `rounds_token` only from adapters that expose the supported peer client interface. Continue to include Grok in `RoomStore` and the human status endpoint.

- [ ] **Step 4: Run focused tests**

Run: `python -m unittest tests.test_chat_launch tests.test_peer_http tests.test_peer_rounds -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agent_bridge/chat/__main__.py tests/test_chat_launch.py docs/GROK-ROOM-SETUP.md
git commit -m "fix: exclude Grok from MCP peer rounds"
```

### Task 6: Validate the complete PR and prepare the review handoff

**Files:**
- Modify: PR description only after Scott supplies factual Bot product/version/tool information.

- [ ] **Step 1: Run the focused Grok and chat suites**

Run: `python -m unittest tests.test_chat_grok tests.test_chat_launch tests.test_chat_management tests.test_chat_discussion tests.test_peer_http tests.test_peer_rounds -v`

Expected: PASS.

- [ ] **Step 2: Run browser JavaScript validation and the repository chat suite**

Run the repository's existing JavaScript test command, then `python tests/test_chat_suite.py` (or its documented equivalent).

Expected: PASS with no unrelated changes.

- [ ] **Step 3: Inspect the final diff and GitHub checks**

Run: `git diff upstream/main...HEAD --check` and the approved GitHub PR-check command.

Expected: no whitespace errors and every required check passing.

- [ ] **Step 4: Update the PR description only with factual Bot details supplied by Scott**

Add the exact approved product name, version, tools, and launch-time limitation. Keep the PR a draft until that information and Scott's final review are complete.
