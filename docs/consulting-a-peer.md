# Getting findings instead of agreement

A peer asked "does this look right?" will usually say yes. That answer costs a
round trip and tells you nothing. This page is how to ask so the reply is worth
reading, and what to do with it afterwards.

It is not a style guide. Every claim here is drawn from consultations that
found real defects in this repository, and each example names the defect.

## The short version

- Name the claim you want tested, not the artifact you want admired.
- Supply the evidence the peer needs. It cannot see your files.
- Point at specific places you suspect, by name.
- Ask for counterexamples, not opinions.
- Say what you already measured, so the peer does not re-derive it.
- Let the peer answer "no supported finding". That answer must stay available.
- Treat every finding as a hypothesis until you have measured it yourself.

## The template

Copy this. The headings matter less than the six things they collect.

```
CLAIM
What I believe is true, stated so it could be false.

EVIDENCE
The code, output, or measurement the peer needs. Paste it. The peer has no
access to your files, your history, or your environment.

WHAT I ALREADY MEASURED
Facts, with how they were obtained. Keeps the reply from re-deriving them and
tells the peer which ground is already firm.

ATTACK THESE
Named, specific things. "Can any call path reach X without passing Y?"
"Construct a state where the published document disagrees with the code."
Numbered, so the reply can address them individually.

WHAT WOULD CHANGE MY MIND
The shape of a finding that would make you act. This is how the peer knows
what counts as material rather than tidy.

CONSTRAINTS
What must not change, and what you have already decided. Saves you a
recommendation you cannot use.
```

Then, at the end, one line that matters more than it looks:

```
Say plainly if a section is fine.
```

## Why this works

A general request for review invites agreement, because agreement is the
cheapest response that satisfies the request. A named target invites
falsification, because the peer now has something specific it can be wrong
about.

Two findings from this repository, both from prompts that named their targets:

**A check that could never succeed.** The claim was that a generated file was
written idempotently: read it back, compare, skip the write if it matches. The
prompt asked the peer to attack the read-then-write comparison specifically,
and to consider text-mode translation. It found that a literal `\r\n` written
in text mode lands on disk as `\r\r\n`, while universal newlines strip the CRs
back out on read, so the comparison could never match and every call rewrote
the file. Measured afterwards on Windows:

    on disk : b'@echo off\r\r\n...'
    expected: b'@echo off\r\n...'

A prompt asking "is this shim correct?" would very likely have been told yes.
The file worked. Only the idempotence was broken, and only in a way that
required someone to go looking at the bytes.

**A document that could disagree with its own enforcement.** A registry
generated a policy matrix for review. The prompt asked the peer to construct a
registry state where the published matrix and the enforced behaviour disagree.
It constructed two. In one, a peer with an empty task list was advertised at
three classifications under the widest label in the document, while every
actual call to it was refused. The generator tested the task tuple for
truthiness; the enforcement tested `is not None`.

Neither finding came from the peer being clever. Both came from being pointed
somewhere and asked for a counterexample rather than a verdict.

## Let the peer find nothing

A prompt that rewards criticism manufactures defects the same way a prompt
that rewards approval conceals them. Both give you a reply shaped by what you
asked for rather than by what is true.

So make the null result explicitly available, and mean it. "Say plainly if a
section is fine" is doing real work in the template above. A consultation that
returns nothing, on a question worth asking, is a useful result: it is evidence
your reasoning holds, and it cost one round trip to obtain.

Watch for the failure mode where a peer produces a long list of small,
unfalsifiable observations. That is usually a sign the prompt demanded findings
without giving anything specific to attack.

## Then verify, because a finding is a hypothesis

Every finding above was measured before it was believed. That is not
ceremony. Of the findings this repository has taken from peer review, some
were wrong, and the ones that were wrong looked exactly like the ones that
were right.

The checklist:

1. **Reproduce the defect.** Construct the case the peer described and watch it
   fail. If you cannot make it fail, you do not have a finding yet, whatever
   the reasoning looked like.
2. **Fix it.**
3. **Check the fix against the same case.** Not against the suite. Against the
   specific thing that failed.
4. **Revert the fix and confirm the case fails again.** This is the step people
   skip. A test that passes both before and after your change is not testing
   your change. Two examples from this repository were caught exactly here: a
   regression test placed around a call that never hung, and a thread-leak
   check moved into a process where the threads no longer existed. Both read
   correctly. Neither could fail.
5. **Say which claims you could not verify**, and why. An unverified claim
   carried forward as settled is how a wrong finding survives.

## Where the peer's limits are

The peer receives your prompt and nothing else. No repository, no history, no
environment, no memory of earlier consultations except within one
conversation. It cannot run your tests. It cannot see the platform you are on.

Two consequences worth planning around.

**Paste what it must reason about.** A reference to a file it cannot open
produces a reply about a file it imagined.

**Its claims about your platform are unverified by construction.** A peer
reasoning about Windows behaviour from a POSIX host is reasoning, not
measurement, however confident the prose. Ask it to list what it could not
verify, and then verify those yourself. In this repository that step is what
caught a fix whose test could not fail on the platform that had the bug.

The reply is data. It is one outside opinion, not an instruction, and not
authoritative. Weigh it and decide.

## Related

[BUILD-YOUR-OWN.md](BUILD-YOUR-OWN.md) is the other half: how to build a
bridge like this one. This page is how to use one well once you have it.
