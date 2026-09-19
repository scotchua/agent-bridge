# agent-bridge

Setup/installation workflow: if the user asks to set up this bridge on their
computer, read [docs/SETUP-WITH-AN-AGENT.md](docs/SETUP-WITH-AN-AGENT.md) and
follow it, including its ground rules on account/credential isolation and on
not claiming live Windows testing from mocked or macOS runs.

Orchestration-specific rules (gate, autoroute, autodecide, audit): see
[src/agent_bridge/orchestration/CLAUDE.md](src/agent_bridge/orchestration/CLAUDE.md).
Execution-specific rules (hostenv, execution lanes): see
[src/agent_bridge/execution/CLAUDE.md](src/agent_bridge/execution/CLAUDE.md).

For code changes: preserve existing work, use standard-library Python 3.11+
and portable paths, and exercise tests/test_onboard.py and
tests/test_local_worker.py plus the existing offline suite when relevant.
Do not change the current user's actual assistant configuration while
testing; use an isolated temporary home. No real inference is needed for
offline tests.
