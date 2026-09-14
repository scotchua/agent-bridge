"""The suite's own shape, checked by the suite.

One test, for one failure mode that is invisible in the place people look
for it. ``unittest.main()`` runs at the point it is written, so a class
defined after that block is never collected by ``python3 tests/whatever.py``.
Under pytest the whole module is imported first and every class runs, so the
file looks green in CI and silently skips tests when someone runs it
directly. Six files had drifted this way, all from appending a class to the
end of a file that already ended with the block.

This is deliberately a source check rather than a runtime one. The runtime
cannot see it: by the time anything executes, the classes that would be
skipped are either collected or the process has already exited.
"""

from __future__ import annotations

import ast
from pathlib import Path
import unittest

TESTS = Path(__file__).resolve().parent


def _main_guard_line(tree: ast.Module) -> int | None:
    """The line of the ``if __name__ == "__main__":`` statement, if present."""

    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and test.left.id == "__name__"):
            return node.lineno
    return None


class MainGuardPlacementTests(unittest.TestCase):

    def test_no_test_class_is_defined_after_unittest_main(self):
        offenders = []
        for path in sorted(TESTS.glob("test_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            guard = _main_guard_line(tree)
            if guard is None:
                continue
            for node in tree.body:
                if (isinstance(node, (ast.ClassDef, ast.FunctionDef))
                        and node.lineno > guard):
                    offenders.append(f"{path.name}:{node.lineno} {node.name}")
        self.assertEqual(
            offenders, [],
            "defined after unittest.main(), so running the file directly "
            "skips them without saying so")


if __name__ == "__main__":
    unittest.main()
