"""Test-only launcher for codex_task.py under an isolated ``HOME``.

Execution-lane admission (codex_promotion.py) refuses any process whose
``HOME`` is not the account's home, so a production worker with a stray
``HOME`` never gets an empty promotion state of its own. Tests that run the
lane as a subprocess under a temporary ``HOME`` point their harness at this
file instead: it treats that ``HOME`` as the account home, so admission locks
and reads the test's own promotion directory, then runs the real script.
Nothing in production refers to this file.
"""
import runpy
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "src" / "agent_bridge" / "execution" / "codex_task.py"
sys.path.insert(0, str(SCRIPT.parents[2]))

from agent_bridge.execution import codex_promotion  # noqa: E402

codex_promotion.account_home = Path.home
sys.argv[0] = str(SCRIPT)
runpy.run_path(str(SCRIPT), run_name="__main__")
