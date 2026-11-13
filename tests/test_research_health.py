import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from research_center.health import data_catalog, workspace_health
from research_center.store import RunStore


class WorkspaceHealthTests(unittest.TestCase):
    def test_filename_inventory_does_not_read_any_parquet_or_heldout_files(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);store=RunStore(root/'store');data=root/'data';data.mkdir()
            for name in ('stock_1m_2024-01_batch1.parquet','stock_1m_2025-12_batch1.parquet','stock_1m_2026-01_batch1.parquet'):
                (data/name).write_bytes(b'not a parquet')
            with patch('research_center.health.DATA_ROOT',data),patch.object(Path,'read_bytes',side_effect=AssertionError('must not open data')):
                catalog=data_catalog(store)
                self.assertEqual([row['month'] for row in catalog['months']],['2024-01','2025-12'])
                self.assertFalse(catalog['coverage_verified']);self.assertFalse(catalog['paper_admission'])
                self.assertEqual(workspace_health(store)['status'],'ready')

    def test_missing_data_is_actionable_and_examples_are_observed_only(self):
        with tempfile.TemporaryDirectory() as root:
            store=RunStore(Path(root)/'store')
            config={'symbol':'000001.SZSE','start':'2024-01-02','end':'2024-01-05'}
            r=store.create(config);store.update(r['run_id'],status='failed')
            with patch('research_center.health.DATA_ROOT',Path(root)/'missing'):
                self.assertEqual(workspace_health(store)['status'],'needs_attention')
                self.assertEqual(data_catalog(store)['examples'],[])
                store.update(r['run_id'],status='completed')
                self.assertEqual(data_catalog(store)['examples'][0]['source_run_id'],r['run_id'])
