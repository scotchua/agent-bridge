"""Regression tests for hostile-working-directory Python imports."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class LauncherSecurityTests(unittest.TestCase):
    def test_launchers_ignore_cwd_package_and_inherited_pythonpath(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            hostile = Path(temporary)
            marker = hostile / "hostile-imported"
            package = hostile / "agent_bridge"
            package.mkdir()
            (package / "__init__.py").write_text(
                "from pathlib import Path\nimport os\n"
                "Path(os.environ['LAUNCHER_SECURITY_MARKER']).write_text('executed')\n",
                encoding="utf-8",
            )
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(hostile)
            environment["LAUNCHER_SECURITY_MARKER"] = str(marker)

            for name in ("agent-bridge-mcp", "agent-bridge-admin", "agent-bridge-setup",
                         "agent-bridge-gate-hook"):
                with self.subTest(launcher=name):
                    launcher = ROOT / "bin" / (name + (".cmd" if os.name == "nt" else ""))
                    argv = ([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(launcher), "--help"]
                            if os.name == "nt" else [str(launcher), "--help"])
                    completed = subprocess.run(
                        argv, cwd=hostile,
                        env=environment, capture_output=True, timeout=20,
                    )
                    self.assertFalse(marker.exists(), completed.stdout.decode())
                    self.assertEqual(completed.returncode, 0, completed.stderr.decode())

    def test_all_launchers_set_only_repo_pythonpath_and_enable_safe_path(self) -> None:
        # agent-bridge-gate-hook is the launcher the delegation-first gate depends
        # on, and it was in neither of these tuples, so neither the hostile-cwd
        # execution test nor this lint covered it. The "Security regression
        # tests" CI step runs this file on all three runners, which makes this
        # the cheapest Windows coverage the gate launcher can have.
        for name in ("agent-bridge-admin", "agent-bridge-mcp", "agent-bridge-setup",
                     "agent-bridge-gate-hook"):
            with self.subTest(launcher=name, platform="posix"):
                text = (ROOT / "bin" / name).read_text(encoding="utf-8")
                self.assertIn('export PYTHONPATH="$REPO/src"', text)
                self.assertNotIn("$PYTHONPATH", text)
                self.assertIn('PY=${AGENT_BRIDGE_PYTHON:-python3}', text)
                self.assertIn('exec "$PY" -P -m ', text)
            with self.subTest(launcher=name, platform="windows"):
                text = (ROOT / "bin" / f"{name}.cmd").read_text(encoding="utf-8")
                self.assertIn('set "PYTHONPATH=%REPO%\\src"', text)
                self.assertNotIn("%PYTHONPATH%", text)
                self.assertIn('if not defined AGENT_BRIDGE_PYTHON set "AGENT_BRIDGE_PYTHON=python"', text)
                self.assertIn('"%AGENT_BRIDGE_PYTHON%" -P -m ', text)


if __name__ == "__main__":
    unittest.main()
