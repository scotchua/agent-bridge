# Orchestration: routing, gate, autodecide, audit

Automatic delegation, if the user has it on, is enforced by a host hook that
refuses edits without a routing receipt. Read
[docs/DELEGATION-GATE.md](../../../docs/DELEGATION-GATE.md) before touching
`orchestration/{gate,autoroute,autodecide,audit}.py`. Three rules there are
not negotiable: privacy is checked before capacity and nothing later may undo
it; an unreadable policy, router, or state root is a deny and never a
permissive default; and a confinement backend that cannot deny network access
or confine reads is not offered for material outside `synthetic`. Do not
describe an instruction file as enforcement, and do not widen what a receipt
claims.

Describe the routing policy to the user as theirs. A repository with no entry
is retained and never dispatched; that default is the safe one. Ask which
repositories to classify one at a time and never classify one on their behalf.

For changes here, also run tests/test_hostenv.py, tests/test_automatic_gate.py,
tests/test_delegation_audit.py and tests/test_automatic_delegation_e2e.py;
the last one drives the whole workflow and is the one that catches a component
that passes alone and fails in place.
