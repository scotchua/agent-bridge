# Contributing

agent-bridge is experimental. Bug reports, setup feedback and focused changes
are welcome, especially from people trying the guided setup on a fresh machine.
Fresh testers are part of learning what needs improvement; a particular number
of testers is not a release gate.

Start with an issue for behavior changes or larger proposals. For security
problems, follow [SECURITY.md](SECURITY.md) and do not disclose details in a
public issue.

## Making a change

- Keep the implementation compatible with Python 3.11 and newer and prefer the
  standard library.
- Preserve platform-specific safety behavior. Native Windows, macOS and Linux
  use different process, locking and permission implementations.
- Use portable paths. Do not assume the contributor's home directory, CLI
  location, account, model or configuration.
- Keep fixtures synthetic. Never commit real prompts, peer responses,
  credentials, tokens, provider configuration, account identifiers or local
  bridge state.
- Do not make real provider calls from automated tests. The offline suite uses
  stand-in CLIs; live canaries require signed-in accounts, consume allowance or
  money, and must remain an explicit human-authorized step.

Run the checks relevant to your change. For changes that affect the broker,
storage, process handling or platform behavior, run:

```text
python3 tests/test_suite.py
```

For guided setup or local-worker changes, also run:

```text
python3 -m unittest discover -s tests -p "test_onboard.py"
python3 -m unittest discover -s tests -p "test_local_worker.py"
```

Use your platform's Python command if it differs. A pull request should explain
the trigger, the resulting behavior, the checks run and any platform or live
path that was not verified. Do not include raw logs unless you have inspected
and reduced them to synthetic, non-sensitive excerpts.

By submitting a contribution, you agree that it is licensed under the
[Apache License 2.0](LICENSE).
