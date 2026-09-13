"""Synthetic regression checks for promotion evidence; never calls providers."""
import copy
import importlib.util
import io
import contextlib
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from agent_bridge import config, setup_cmd
spec = importlib.util.spec_from_file_location('security_canaries', ROOT / 'canaries/run_canaries.py')
canaries = importlib.util.module_from_spec(spec)
spec.loader.exec_module(canaries)


def evidence():
    rows = []
    for direction in ('codex->claude', 'claude->codex'):
        for kind in ([f'one-turn {i}' for i in range(1, 11)] +
                     [f'3-turn {i} t{t}' for i in range(1, 4) for t in range(1, 4)] +
                     ['schema-pressure']):
            rows.append(dict(direction=direction, kind=kind, status='complete',
                             contract_valid=True, acceptable=True,
                             peer_session_id='synthetic-session',
                             session_id_as_intended=True))
    return dict(verification_profile=setup_cmd.CANARY_VERIFICATION_PROFILE, controls_requested=dict(setup_cmd.MINIMUM_CANARY_CONTROLS),
                controls_executed=dict(setup_cmd.MINIMUM_CANARY_CONTROLS),
                rows=rows, timeouts=[dict(direction=d, status='timed_out', orphans=0)
                for d in ('codex->claude', 'claude->codex')], skipped_controls=[], blocked={})


class CanarySecurityTests(unittest.TestCase):
    def test_full_synthetic_controls_accepted(self):
        setup_cmd.validate_canary_evidence(evidence())

    def test_incomplete_and_inconsistent_controls_rejected(self):
        variants = []
        d = evidence(); d['controls_requested'] = {'timeout_canaries': 2}; d['controls_executed'] = dict(d['controls_requested']); variants.append(d)
        d = evidence(); d['rows'] = [r for r in d['rows'] if not r['kind'].startswith('3-turn')]; variants.append(d)
        d = evidence(); d['rows'][0]['status'] = 'timed_out'; variants.append(d)
        d = evidence(); d['rows'][11]['session_id_as_intended'] = False; variants.append(d)
        d = evidence(); d['rows'][11]['peer_session_id'] = 'wrong-session'; variants.append(d)
        d = evidence(); d['timeouts'][0]['cleanup_errors'] = ['synthetic cleanup error']; variants.append(d)
        d = evidence(); d['timeouts'][0]['orphans'] = 1; variants.append(d)
        d = evidence(); d['controls_requested']['timeout_canaries'] = True; variants.append(d)
        d = evidence(); d['rows'][-1]['acceptable'] = False; variants.append(d)
        d = evidence(); d['rows'][0] = copy.deepcopy(d['rows'][1]); variants.append(d)
        d = evidence(); d['rows'] = [r for r in d['rows'] if r['direction'] != 'claude->codex']; variants.append(d)
        d = evidence(); d['rows'][-1]['status'] = 'timed_out'; variants.append(d)
        d = evidence(); d.pop('verification_profile'); variants.append(d)
        d = evidence(); d['controls_requested']['one_turn_calls'] = -1; variants.append(d)
        for i, candidate in enumerate(variants):
            with self.subTest(i=i), self.assertRaises(ValueError):
                setup_cmd.validate_canary_evidence(candidate)

    def test_abbreviated_run_is_not_promotion_pass(self):
        counts = canaries.requested_controls(['codex', 'claude'], 1, 0)
        self.assertEqual(canaries.result_verdict(0, False, counts, counts), 'INCOMPLETE')

    def test_requested_identity_is_not_observed_identity(self):
        prov = {'peer_session_id': 'wanted', 'attempts': [{'notes': {}}]}
        self.assertFalse(canaries.continuation_identity_matches(prov, 'codex', 'wanted'))
        prov['attempts'][0]['notes']['thread_id_honoured'] = None
        self.assertFalse(canaries.continuation_identity_matches(prov, 'codex', 'wanted'))
        prov['attempts'][0]['notes']['thread_id_honoured'] = True
        self.assertTrue(canaries.continuation_identity_matches(prov, 'codex', 'wanted'))
        prov['peer_session_id'] = 'other'
        self.assertFalse(canaries.continuation_identity_matches(prov, 'codex', 'wanted'))

    def test_schema_pressure_timeout_fails_report(self):
        row = dict(direction='codex->claude', kind='schema-pressure',
                   status='timed_out', error_category='peer_timeout', acceptable=False)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertNotEqual(canaries.report([row], []), 0)

if __name__ == '__main__':
    unittest.main()
