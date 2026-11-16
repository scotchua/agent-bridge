# Gemma receipt gate repair — proposed, not installed

Scott authorized Gemma ownership reconciliation and preparation of an
independently reviewed gate repair on November 15, 2026. Login remains his
responsibility. This proposal does not change routing policy, gate state,
credentials, source/model settings, nightly admission, or launch authority.

## Observed issue

The accepted PR105 fix resolves simple shell destinations and task ownership.
The Gemma command envelope begins with `exec /usr/bin/env -i`, followed by
HOME/PATH/LANG/PYTHONPATH and an absolute native Python interpreter with
`-B -c`. That envelope remains opaque to the gate. Its fallback interprets
HOME as a possible mutation operand and words in quoted peer prose as tree
operations. PYTHONPATH dependencies can also look like repository targets.
Separately, four superseded Gemma stages were still owned with expired leases,
and the current canonical task stage had no matching ledger row.

The ownership issue has been reconciled through supported administrative and
stage tools. That is separate from this code proposal and does not establish
that any denied record save or nightly launch is now ready.

## Proposed narrow behavior

Recognize only the complete clean environment envelope plus a bounded,
literal-only Python body creating new text files. The receipt-only invocation
requires `-I -S -B -c`, the exact canonical path of the Python interpreter
running the gate itself, and `LANG=en_US.UTF-8`. Alternative symlink spellings
are refused, as are pyvenv.cfg or ._pth files beside that canonical binary or
in its parent directory. These flags apply only to this proposed
receipt workflow, not the native nightly runner, its imports or its approved
command. The old `-B -c` saver remains outside this recognized form.
Each statement must be:

```python
with open('/absolute/new-receipt.json', 'x', encoding='utf-8') as receipt:
    receipt.write('literal content')
```

At most 16 such statements and 128 KiB of command text are accepted. Tokens must
have canonical POSIX `shlex.quote` spelling, separated only by spaces/tabs;
other quote spellings, Unicode separators and double-quoted escape sequences
do not qualify. This closes the shell/shlex mismatch found in independent
round0 review. Every actual
destination still passes existing realpath/protected-path checks and current
task ownership checks. Repository destinations still require their routing
receipts. Missing/expired/mismatched ownership and unavailable state remain
denials. Quoted content, environment values, and the interpreter path are not
mutation targets for this completely recognized form.

No code is evaluated by the parser. Imports, reads, assignments, arbitrary
calls, computed paths, overwrite/append, extra arguments, shell chaining,
redirection and substitutions do not gain precision. An attempted new isolated
receipt form that fails recognition is denied as `literal_receipt_unresolved`,
not sent through the legacy read-only fast path. Other command formats retain
their existing conservative judgment. No automatic fallback to this writer
is provided; this is not a general Python permit.
Paths and content must encode strictly as UTF-8; their literal spelling must
occupy one physical source line. Escaped newlines are supported, but multiline
literal spellings are refused so Python 3.14 command dedenting cannot alter
their contents.

The original complex record-saving script, with locks, JSON processing and
atomic replacement, is **not** proven safe by this recognizer. It must not be
retried or silently rewritten as a workaround. Using the proposed form for
the blocked receipt saves requires the independent reviewer/operator to
accept that workflow change after reviewing this exact artifact. Canonical
task updates remain subject to their lock/revision protocol; this writer is
for new immutable receipts, not replacing task.json. A future broader Python
analyzer would be a separate security change.

## Protections and limits

Exclusive creation refuses existing paths. It is not atomic publication of a
multi-file bundle: failures can leave a partial new receipt, so callers must
retain the actual exit, reopen/hash/validate each artifact, and never interpret
a partial file as a completed review or successful install. A bundle's last
statement must create a completion manifest containing the expected hashes
of all prior files; after execution each hash must still be checked. A
manifest's existence alone is not independent review evidence. No live command
uses this proposal during preparation.

This remains a command-text heuristic, not an execution sandbox. The receipt
process uses the gate's exact canonical interpreter path, isolated startup and no site
processing: supplied PYTHONPATH, user site, sitecustomize and .pth startup
hooks are inert. This is a path binding, not a binary hash attestation; it
does not eliminate replacement races or compromise of the gate interpreter
installation (including a user-writable interpreter/standard-library tree).
The symlink/pyvenv.cfg issue from round1 is a distinct startup-path risk,
not a binary race; this correction refuses those alternative launch paths.
The shell's own trusted invocation/alias assumptions remain. The independent
review must assess these limits. Round0 was NOT ACCEPT and is retained with
its actual provenance; it is not an endorsement of this corrected candidate.

## Review and release conditions

Review the parser, gate integration and tests together, including shell quote
boundaries, second-target writes, symlinks, nested repositories, dynamic code,
credential/protected destinations, expired leases and live ownership.
Run the required offline gate, host-environment, audit, end-to-end, onboard
and local-worker tests with isolated fixture state. The synthetic writer
roundtrip only creates a temporary test receipt and verifies exclusive create;
it is not a live Gemma save or successful nightly launch.

No changes are installed in the active checkout. Independent other-provider
review is in progress; a real consultation succeeded after login was restored.
After acceptance and authorized installation,
recheck the exact installed gate and supported receipt workflow before saving
the pending native-repair evidence. Only then may the separately reviewed
three-file backup repair be installed together, frozen/reviewed, reconciled
against the immutable historical evidence and used to prepare a fresh dated
night plan. Old consumed launch commands remain unusable throughout.
