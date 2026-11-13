"""Verify the rolling command boundary without reading market data."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research_center.cli import main


TEMPLATE = Path(__file__).resolve().parents[1] / 'research_center/templates/rolling_validation.json'


class RollingCLITest(unittest.TestCase):
    def invoke(self, action, store):
        output = io.StringIO()
        errors = io.StringIO()
        argv = ['research', '--store', str(store), 'rolling', action, '--file', str(TEMPLATE)]
        with patch('sys.argv', argv), redirect_stdout(output), redirect_stderr(errors):
            status = main()
        return status, json.loads(output.getvalue()), errors.getvalue()

    def test_preview_is_read_only_and_exposes_all_windows(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Path(temporary) / 'uncreated'
            status, plan, errors = self.invoke('plan', store)
            self.assertEqual(status, 0)
            self.assertEqual(errors, '')
            self.assertFalse(plan['paper_admission'])
            self.assertEqual(
                [(case['config']['start'], case['config']['end']) for case in plan['cases']],
                [('2024-01-02', '2024-01-03'), ('2024-01-04', '2024-01-04'),
                 ('2024-01-03', '2024-01-04'), ('2024-01-05', '2024-01-05')],
            )
            self.assertFalse(store.exists())

    def test_execution_keeps_progress_separate_from_final_receipt(self):
        def execute(store, spec, *, on_event):
            self.assertEqual(len(spec['folds']), 2)
            on_event({'type': 'planned', 'experiment_id': 'owned-rolling-test'})
            return {'status': 'interrupted', 'experiment_id': 'owned-rolling-test'}

        with tempfile.TemporaryDirectory() as temporary, patch(
            'research_center.rolling.execute_rolling', side_effect=execute
        ):
            status, receipt, progress = self.invoke('run', Path(temporary) / 'store')
        self.assertEqual(status, 1)
        self.assertEqual(receipt['status'], 'interrupted')
        self.assertEqual(json.loads(progress)['experiment_id'], receipt['experiment_id'])
