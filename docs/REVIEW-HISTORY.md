# What four rounds of adversarial review found

This bridge was reviewed by having each model attack the other's work, four
rounds, 40 findings. This is the write-up, kept because the findings transfer
even if the code does not.

Two things are worth knowing before the list.

**Individually correct fixes broke each other, twice.** Both times the result
was worse than either original bug, and both times every test passed. That is
the failure mode this document exists to warn about.

**Five tests were asserting bugs as correct behaviour.** Found by the reviewer,
not by me. A suite that certifies the defect is worse than no suite, because it
converts "we have not checked" into "we have checked and it is fine".

## The design was wrong in four ways before any of this

Measured against the real CLIs, contradicting what the original design assumed:

1. `codex exec resume` supports neither a sandbox flag nor a working-directory
   flag, so both have to be supplied another way.
2. Codex's session identifier is a thread id on a specific event, not the
   session field the design named.
3. Claude's result envelope reports success even on a failed run. A different
   field is the only trustworthy signal. Keying on the obvious one silently
   accepts failures as good answers.
4. Prompts go on standard input. Passing them as an argument as well silently
   duplicates the question.

Detail in [verified-cli-behaviour.md](verified-cli-behaviour.md).

## The one that cost real money

The two models enforce output schemas differently. One accepts a schema with
optional fields; the other routes it through a stricter validator that requires
every field to be listed as required. So a single shared schema worked perfectly
in one direction and failed **one hundred percent** of calls in the other.

Thirteen live calls were spent discovering something a five-line static check
would have caught. That check now exists and runs before any live call.

The general lesson: when two systems must both accept the same artefact, test
the artefact against both statically, before spending anything on a round trip.

## The concurrency findings, in the order they were found

Each round closed the previous gap and revealed the next one. All five turned out
to be windows in a single lifecycle.

| Round | The gap |
|---|---|
| 1 | A worker could invoke a model without holding the conversation it was speaking for |
| 2 | The fix protected the polling path and left the admission path unguarded |
| 3 | The uncertainty test counted how many processes were killed, and the safety record was written after the destructive action rather than before |
| 4 | Evidence of an in-flight call was recorded after starting the call, so a crash in between left an invisible orphan |
| 5 | Evidence was discarded when the local process ended, before the outcome had been durably saved |

Patching them one at a time converged slowly. What ended it was taking the
reviewer's own framing: instead of closing a fifth window, make the evidence
last for the **whole** operation, from before anything starts until after the
result is durably recorded. Asked afterwards whether a sixth window existed, the
reviewer could not name one.

The transferable version: **if you are fixing a sequence of ever-narrower
windows, you are enumerating instances of a class you have not named yet.** Name
the class.

## Where two correct fixes collided

Round 2 established that a finished record must never be overwritten, so a stale
process could not replace a real error with a wrong one. Correct.

Round 3 added a rule that a worker must publish its process id and then confirm
it still owned the conversation before calling a model. Also correct.

Together they were broken. If a job was marked finished while it waited, the
worker's attempt to publish its id was silently ignored, because finished
records are immutable. No id was published, the ownership check still passed
because the claim was intact, and a second worker then saw a finished job and
took the conversation. Two workers, one model session.

The fix is small: check that the write you depended on actually happened. The
lesson is not small. **A fix that makes something unwritable can silently break
any later fix that depends on writing it**, and neither change looks wrong on
its own.

## A defect no test suite would have found

Both models were asked only to name a failure mode and a minimum fix. Both
answered well, and both filled in a "disagreements" field arguing against
exponential backoff, which nobody had proposed.

The cause was two of my own decisions interacting. The instructions told the
model that "a consultation that only agrees is worthless", and the response
format made the disagreement field mandatory. Between them, the model was
cornered into producing a disagreement whether or not one existed.

For a tool whose entire purpose is honest second opinions, a manufactured
objection is the worst possible output: it is exactly what someone would act on.

The fix scopes disagreement to a position the caller actually stated, names the
empty case, forbids arguing against a position nobody took, and says plainly
that agreeing is a legitimate answer. Verified in both directions with two
prompts: one that states no position, which must now produce an empty list, and
one that states a wrong position and asks for confirmation, which must still
produce a real disagreement.

That second test is the important one. **Over-suppression would have produced
empty lists too, and would have looked like a success while destroying the point
of the tool.**

This survived three review rounds and 308 automated tests. It was only visible
when a real model answered a real question and a person read the answer
critically.

## The tests that were certifying bugs

All found by the reviewer.

- One required that a process which had *lost* a conversation could still modify
  it. That was the bug, written down as the expected result.
- One checked that bad output was quarantined, and passed because unrelated
  bytes happened to be present. The actual bad content was never saved.
- One proved a dangerous path was refused, using an input that was rejected
  earlier for a different reason, so the dangerous path was never exercised.
- Two asserted behaviour that a later fix deliberately reversed, and had not been
  updated.

What helps: assert on the **reason** something happened, not only the outcome.
One patch in this project silently failed to apply, leaving safe behaviour with a
misleading explanation. Only a reason-string assertion caught it.

## Things accepted rather than fixed

Named as accepted risks, not solved problems. The reviewer confirmed they do not
change its final verdict.

- The consulted Codex process can read the filesystem. Its sandbox restricts
  writing. The instruction not to read is a rule, not a boundary.
- Codex's own built-in skills are present in the isolated directory and could not
  be disabled in the version this was built against. Inventoried per job, not
  proven inert.
- A process that deliberately detaches itself survives being killed.
- A conversation interrupted mid-call is held indefinitely until a person looks
  at it. There is no timeout by design, because a timeout would convert a known
  unknown into a silent assumption.

## Method notes

The fourth round was conducted **through the bridge itself**, one continued
conversation, seven consultations. It found the collision that three earlier
rounds and 308 tests had missed.

Rounds worked better when the request named the specific claims to attack and
gave the reviewer permission to say a section was clean. Asking "review this"
produces findings whether or not any exist; asking "here is the claim, here is
the code, try to falsify it, and tell me plainly if you cannot" produces
verdicts. Two of the most useful answers received were "correct" and "I cannot
identify one".
