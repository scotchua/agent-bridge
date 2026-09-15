# Agent-guided installation and repository work

If the user asks to set up this bridge on their computer, read
[docs/SETUP-WITH-AN-AGENT.md](docs/SETUP-WITH-AN-AGENT.md) and follow it.
Do the work for the user: inspect prerequisites, ask the small set of required
choices in plain language, stage a plan, run authorized checks, install and
verify. Do not merely tell them to paste commands into another assistant.

The user chooses privacy rules and whether to connect an existing local model.
Never copy the author's account permissions, home paths, credentials, model
names, or live configuration. “Same connection” means the same supported
consultation tools and workflow, not identical account access or automatic
sharing of all conversations. Consultations are not arbitrary remote execution.

Before live verification, explain that it invokes both providers and consumes
allowance or incurs charges according to their accounts. Obtain authorization
unless the user has already authorized those calls. Do not enable paid fallback,
download a model or copy credentials as an installation shortcut.

Keep the existing candidate -> full canary PASS -> promotion gate. Choose
privacy/model settings before canaries; changed settings invalidate the result.
Do not bypass an isolation, version, authentication or privacy failure. Report
what is installed separately from what has been verified. Do not claim live
Windows testing from mocked platform paths or macOS tests.

Automatic delegation, if the user has it on, is enforced by a host hook that
refuses edits without a routing receipt. Read
[docs/DELEGATION-GATE.md](docs/DELEGATION-GATE.md) before touching
`orchestration/{gate,autoroute,autodecide,audit}.py` or
`execution/hostenv.py`. Three rules there are not negotiable: privacy is
checked before capacity and nothing later may undo it; an unreadable policy,
router or state root is a deny and never a permissive default; and a
confinement backend that cannot deny network access or confine reads is not
offered for material outside `synthetic`. Do not describe an instruction file
as enforcement, and do not widen what a receipt claims.

Describe the routing policy to the user as theirs. A repository with no entry
is retained and never dispatched; that default is the safe one. Ask which
repositories to classify one at a time and never classify one on their behalf.

For code changes: preserve existing work, use standard-library Python 3.11+
and portable paths, and exercise tests/test_onboard.py and
tests/test_local_worker.py plus the existing offline suite when relevant.
For anything touching routing, the gate or either execution lane, also run
tests/test_hostenv.py, tests/test_automatic_gate.py,
tests/test_delegation_audit.py and tests/test_automatic_delegation_e2e.py;
the last one drives the whole workflow and is the one that catches a component
that passes alone and fails in place. Do not change the current user's actual
assistant configuration while testing; use an isolated temporary home. No real
inference is needed for offline tests.
