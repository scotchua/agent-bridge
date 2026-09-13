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

For code changes: preserve existing work, use standard-library Python 3.11+
and portable paths, and exercise tests/test_onboard.py and
tests/test_local_worker.py plus the existing offline suite when relevant.
Do not change the current user's actual assistant configuration while testing;
use an isolated temporary home. No real inference is needed for offline tests.
