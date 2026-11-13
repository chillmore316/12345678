"""Exercise the batch entry point without launching workers or reading market data."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research_center.cli import main


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / 'research_center/templates/parameter_batch.json'


class BatchCLITest(unittest.TestCase):
    def invoke(self, action, store, path):
        output = io.StringIO()
        errors = io.StringIO()
        args = ['research', '--store', str(store), 'batch', action, '--file', str(path)]
        with patch('sys.argv', args), redirect_stdout(output), redirect_stderr(errors):
            status = main()
        return status, json.loads(output.getvalue()), errors.getvalue()

    def test_plan_previews_two_cases_without_creating_store(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Path(temporary) / 'missing-store'
            status, output, errors = self.invoke('plan', store, TEMPLATE)
            self.assertEqual(status, 0)
            self.assertEqual(errors, '')
            self.assertTrue(output['valid'])
            self.assertFalse(output['paper_admission'])
            self.assertEqual([case['config']['slippage_bps'] for case in output['cases']], [5, 15])
            self.assertFalse(store.exists())

    def test_invalid_file_does_not_create_store(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = root / 'missing-store'
            invalid = root / 'duplicate.json'
            invalid.write_text('{"base": {}, "base": {}}')
            with self.assertRaises(SystemExit) as error:
                self.invoke('run', store, invalid)
            self.assertEqual(error.exception.code, 2)
            self.assertFalse(store.exists())

    def test_run_streams_receipt_events_separately_and_returns_failure_code(self):
        def execute(store, spec, *, on_event):
            self.assertEqual(len(spec['cases']), 2)
            on_event({'type': 'planned', 'experiment_id': 'owned-test'})
            return {'status': 'failed', 'experiment_id': 'owned-test'}

        with tempfile.TemporaryDirectory() as temporary, patch(
            'research_center.batch.execute_batch', side_effect=execute
        ):
            status, output, errors = self.invoke('run', Path(temporary) / 'store', TEMPLATE)
        self.assertEqual(status, 1)
        self.assertEqual(output['status'], 'failed')
        self.assertEqual(json.loads(errors)['experiment_id'], output['experiment_id'])
