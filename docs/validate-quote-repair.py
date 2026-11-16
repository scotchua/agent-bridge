"""Offline validation only; isolated home, no real provider or model calls."""
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests')]
from agent_bridge.orchestration import literal_records as new

old = types.ModuleType('old_literal_records')
exec(subprocess.check_output(['git', 'show', '6e7e99b:src/agent_bridge/orchestration/literal_records.py'], cwd=ROOT), old.__dict__)
corpus = []
with tempfile.TemporaryDirectory(prefix='gemma-quote-tests-') as home:
    os.environ.update(HOME=home, USERPROFILE=home)
    for text in ('plain', "a'b", '$X', '`id`', '\t', '\n', '', 'café', "'12$'", 'x' * 132000):
        body = f"with open('/tmp/new.json', 'x', encoding='utf-8') as receipt:\n    receipt.write({text!r})\n"
        cmd = shlex.join(['exec', '/usr/bin/env', '-i', f'HOME={home}', 'PATH=/usr/bin:/bin',
                          'LANG=en_US.UTF-8', 'PYTHONPATH=/tmp', os.path.realpath(sys.executable),
                          '-I', '-S', '-B', '-c', body])
        corpus.extend([cmd, cmd.replace(' -I ', '\t-I ', 1), cmd.replace(' -I ', '\n-I ', 1),
                       cmd + '; echo x', cmd.replace("'x'", "'w'")])
    corpus.extend(['', 'echo "$X"', 'echo `id`', 'echo $(id)', 'echo \ud800', None])
    for command in corpus:
        assert old.targets(command) == new.targets(command), repr(command)
    print(json.dumps({'receipt_parity_cases': len(corpus), 'passed': True}), flush=True)
    names = ['test_automatic_gate.py', 'test_hostenv.py', 'test_delegation_audit.py',
             'test_automatic_delegation_e2e.py', 'test_onboard.py', 'test_local_worker.py']
    suite = unittest.TestSuite()
    for name in names:
        suite.addTests(unittest.defaultTestLoader.discover(str(ROOT / 'tests'), pattern=name))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    print(json.dumps({'run': result.testsRun, 'failures': len(result.failures),
                      'errors': len(result.errors), 'skipped': len(result.skipped)}), flush=True)
    raise SystemExit(not result.wasSuccessful())
