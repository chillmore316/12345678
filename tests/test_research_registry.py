from __future__ import annotations
import tempfile
import importlib.metadata
import unittest
from unittest.mock import patch
from research_center.registry import catalog, normalize_config, run_backtest, strategy_version
from research_center.store import RunStore
from research_center.runner import submit


def config(**values):
    return {'strategy': 'buy_hold', 'symbol': '000001.SZSE', 'start': '2024-01-02',
            'end': '2024-01-05', 'initial_cash': 100000, **values}


class StrategyRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = RunStore(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_default_backend_and_source_derived_version(self):
        normalized = normalize_config(config(), self.store)
        self.assertEqual(normalized['backend'], 'reference')
        self.assertEqual(normalized['strategy_version'], strategy_version('buy_hold'))
        self.assertEqual(len(normalized['experiment_id']), 32)
        self.assertNotEqual(strategy_version('buy_hold'), strategy_version('sma_cross'))
        self.assertEqual(normalize_config(normalized, self.store), normalized)

    def test_unknown_backend_strategy_version_and_native_sma_fail_closed(self):
        for request in (config(backend='dynamic_module'), config(strategy_version='old-untrusted'),
                        config(backend='vnpy', strategy='sma_cross')):
            with self.subTest(request=request), self.assertRaises(ValueError):
                normalize_config(request, self.store)
        with patch('research_center.registry.backend_available', return_value=False):
            with self.assertRaisesRegex(ValueError, '不可用'):
                normalize_config(config(backend='vnpy'), self.store)

    def test_child_keeps_series_and_does_not_mutate_parent(self):
        parent_config = normalize_config(config(label='首轮'), self.store)
        parent = self.store.create(parent_config)
        self.store.update(parent['run_id'], status='completed')
        before = self.store.get(parent['run_id'])
        child = normalize_config(config(parent_run_id=parent['run_id'], params={'target_fraction': .3},
                                        label='参数对照'), self.store)
        self.assertEqual(child['experiment_id'], parent_config['experiment_id'])
        self.assertEqual(child['parent_run_id'], parent['run_id'])
        self.assertEqual(self.store.get(parent['run_id']), before)
        with self.assertRaisesRegex(ValueError, '不一致'):
            normalize_config(config(parent_run_id=parent['run_id'], experiment_id='different'), self.store)

    def test_legacy_parent_has_stable_series_without_rewriting_history(self):
        parent = self.store.create(config())
        self.store.update(parent['run_id'], status='completed')
        child = normalize_config(config(parent_run_id=parent['run_id']), self.store)
        self.assertEqual(child['experiment_id'], parent['run_id'])
        self.assertIsNone(self.store.get(parent['run_id'])['experiment_id'])
        with self.assertRaisesRegex(ValueError, '同一策略'):
            normalize_config(config(strategy='sma_cross', parent_run_id=parent['run_id']), self.store)

    def test_catalog_never_imports_native_runtime_or_executes_strategy(self):
        with patch.dict('sys.modules', {'vnpy': None}):
            listing = catalog()
        self.assertEqual({s['id'] for s in listing['strategies']}, {'buy_hold', 'sma_cross'})
        self.assertFalse(listing['paper_admission'])
        self.assertEqual(listing['limits']['end'], '2025-12-31')

    def test_registered_reference_dispatch_and_backend_identity(self):
        normalized = normalize_config(config(), self.store)
        with patch('research_center.engine.run_backtest', return_value={'metrics': {}}) as execute:
            result = run_backtest(normalized)
        execute.assert_called_once_with(normalized, progress=None, cancelled=None)
        self.assertEqual(result['backend'], 'reference')
        self.assertEqual(result['strategy_version'], normalized['strategy_version'])

    def test_preparation_failure_is_persisted_without_launching_worker(self):
        with patch('research_center.runner.importlib.metadata.version',
                   side_effect=importlib.metadata.PackageNotFoundError('test dependency')), \
             patch('research_center.runner.subprocess.Popen') as launch:
            with self.assertRaises(importlib.metadata.PackageNotFoundError):
                submit(self.store, config())
        launch.assert_not_called()
        failed = self.store.list()[0]
        self.assertEqual(failed['status'], 'failed')
        self.assertIn('test dependency', self.store.detail(failed['run_id'])['logs'])


if __name__ == '__main__':
    unittest.main()
