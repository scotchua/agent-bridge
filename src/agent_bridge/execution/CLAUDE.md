# Execution: hostenv and the execution lanes

Read [docs/DELEGATION-GATE.md](../../../docs/DELEGATION-GATE.md) before
touching `execution/hostenv.py` — it shares the same non-negotiable rules as
orchestration's gate: privacy is checked before capacity and nothing later
may undo it; an unreadable policy, router, or state root is a deny and never
a permissive default; and a confinement backend that cannot deny network
access or confine reads is not offered for material outside `synthetic`.

For changes here, also run tests/test_hostenv.py, tests/test_automatic_gate.py,
tests/test_delegation_audit.py and tests/test_automatic_delegation_e2e.py;
the last one drives the whole workflow and is the one that catches a component
that passes alone and fails in place.
