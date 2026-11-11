"""Keep tests off the real operator settings file.

Importing this module points the runner state home at an empty temporary
directory, so a test never reads the machine's real
``operator-settings.json`` and never depends on what the operator chose.
A test that exercises the file sets its own state home.

Not a test module: the name deliberately fails the ``test_*.py`` pattern.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile

STATE_ENV = "CODEX_JOB_RUNNER_STATE"
# Suppress desktop alerts in tests, including subprocesses that inherit this environment.
os.environ["AGENT_BRIDGE_NO_DESKTOP_ALERTS"] = "1"

if STATE_ENV not in os.environ:
    _root = tempfile.mkdtemp(prefix="ab-operator-state-")
    atexit.register(shutil.rmtree, _root, ignore_errors=True)
    os.environ[STATE_ENV] = os.path.join(_root, "state")
