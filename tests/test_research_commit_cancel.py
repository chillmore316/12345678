"""Synthetic concurrency checks: cancellation and publication have one winner."""
import json
from types import SimpleNamespace
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from research_center import runner
from research_center.store import RunStore


class CommitCancellationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = RunStore(Path(self.temp.name) / 'runs')
        self.record = self.store.create({'strategy': 'buy_hold', 'initial_cash': 5000})
        self.run_id = self.record['run_id']
        self.directory = self.store.directory(self.run_id)
        (self.directory / 'provenance.json').write_text(json.dumps({'source_hashes': {}}))
        for target in ('resource.setrlimit', 'signal.signal', 'signal.alarm'):
            mock = patch('research_center.runner.' + target)
            mock.start()
            self.addCleanup(mock.stop)
        for target, value in [('research_center.registry.run_backtest', {'metrics': {}}),
                              ('research_center.analytics.build_analytics', {}),
                              ('research_center.data_evidence.assess_data', {})]:
            mock = patch(target, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)

    def test_active_pid_must_match_research_store_and_run(self):
        active = self.store.update(self.run_id, status='running', pid=999999)
        command = f"S python -m research_center.cli --store {self.store.root} _worker {self.run_id}"
        with patch('research_center.runner.os.waitpid', side_effect=ChildProcessError), \
             patch('research_center.runner.os.kill'), \
             patch('research_center.runner.subprocess.run') as ps:
            ps.return_value = SimpleNamespace(stdout=command, returncode=0)
            self.assertEqual(runner.refresh_status(self.store, active)['status'], 'running')
            ps.return_value = SimpleNamespace(stdout='', returncode=1)
            self.assertEqual(runner.refresh_status(self.store, active)['status'], 'running')
            self.assertEqual(self.store.get(self.run_id)['status'], 'running')

            ps.return_value = SimpleNamespace(
                stdout=command.replace(str(self.store.root), '/different research store'), returncode=0,
            )
            self.assertEqual(runner.refresh_status(self.store, active)['status'], 'interrupted')

    def test_cancel_accepted_during_analytics_prevents_result_publication(self):
        def analytics(*_args):
            record = runner.cancel(self.store, self.run_id)
            self.assertEqual(record['status'], 'cancelling')
            return {}
        with patch('research_center.analytics.build_analytics', side_effect=analytics):
            runner.execute(self.store, self.run_id)
        self.assertEqual(self.store.get(self.run_id)['status'], 'cancelled')
        self.assertFalse((self.directory / 'result.json').exists())
        self.assertFalse(list(self.directory.glob('*.csv')))

    def test_commit_wins_before_later_cancel_can_be_acknowledged(self):
        save_started = threading.Event()
        cancel_attempted = threading.Event()
        cancellation = []
        errors = []
        def cancel_thread():
            try:
                if not save_started.wait(2):
                    raise AssertionError('save did not begin')
                cancellation.append(runner.cancel(self.store, self.run_id))
            except BaseException as exc:
                errors.append(exc)
        original_save = self.store.save_result
        def save(*args):
            save_started.set()
            self.assertTrue(cancel_attempted.wait(2))
            original_save(*args)
        thread = threading.Thread(target=cancel_thread, daemon=True)
        real_flock = runner.fcntl.flock
        def observed_flock(fd, operation):
            if threading.current_thread() is thread and operation == runner.fcntl.LOCK_EX:
                # Prove actual kernel lock contention, rather than merely
                # announcing that the cancellation thread is about to call.
                with self.assertRaises(BlockingIOError):
                    real_flock(fd, operation | runner.fcntl.LOCK_NB)
                cancel_attempted.set()
            return real_flock(fd, operation)
        thread.start()
        with patch.object(self.store, 'save_result', side_effect=save), \
             patch('research_center.runner.fcntl.flock', side_effect=observed_flock):
            runner.execute(self.store, self.run_id)
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(cancellation[0]['status'], 'completed')
        self.assertFalse((self.directory / 'cancel').exists())
        self.assertTrue((self.directory / 'result.json').exists())

    def test_commit_error_is_failed_before_late_cancel_and_hides_remnants(self):
        original_save = self.store.save_result
        def save(*args):
            original_save(*args)
            raise OSError('owned injected artifact failure')
        with patch.object(self.store, 'save_result', side_effect=save):
            runner.execute(self.store, self.run_id)
        self.assertEqual(self.store.get(self.run_id)['status'], 'failed')
        self.assertEqual(runner.cancel(self.store, self.run_id)['status'], 'failed')
        self.assertFalse((self.directory / 'cancel').exists())
        self.assertFalse((self.directory / 'result.json').exists())
        self.assertFalse(list(self.directory.glob('*.csv')))
        # Even undeletable remnants would not be exposed as a result.
        (self.directory / 'result.json').write_text('{"equity":[{"equity":999}]}')
        self.assertNotIn('equity', self.store.detail(self.run_id))
        with self.assertRaises(ValueError):
            self.store.csv_bytes(self.run_id, 'equity')

    def test_running_reader_never_exposes_partial_result_or_csv(self):
        self.store.update(self.run_id, status='running')
        (self.directory / 'result.json').write_text('{"equity":[{"equity":999}]}')
        (self.directory / 'equity.csv').write_text('equity\n999\n')
        self.assertNotIn('equity', self.store.detail(self.run_id))
        with self.assertRaises(ValueError):
            self.store.csv_bytes(self.run_id, 'equity')

    def test_failure_to_write_marker_still_never_exposes_failed_artifacts(self):
        original_save = self.store.save_result
        real_touch = Path.touch
        def save(*args):
            original_save(*args)
            raise OSError('owned injected save failure')
        def touch(path, *args, **kwargs):
            if path.name == 'artifact-commit-failed':
                raise PermissionError('owned injected marker failure')
            return real_touch(path, *args, **kwargs)
        with patch.object(self.store, 'save_result', side_effect=save), patch.object(Path, 'touch', touch):
            runner.execute(self.store, self.run_id)
        self.assertEqual(self.store.get(self.run_id)['status'], 'failed')
        self.assertNotIn('equity', self.store.detail(self.run_id))
        with self.assertRaises(ValueError):
            self.store.csv_bytes(self.run_id, 'equity')

    def test_transient_post_save_record_read_failure_never_exposes_report(self):
        original_save = self.store.save_result
        original_get = self.store.get
        saved = [False]
        read_failed = [False]
        def save(*args):
            original_save(*args)
            saved[0] = True
            raise OSError('owned injected save failure')
        def get(*args):
            if saved[0] and not read_failed[0]:
                read_failed[0] = True
                raise OSError('owned injected transient record-read failure')
            return original_get(*args)
        with patch.object(self.store, 'save_result', side_effect=save), patch.object(self.store, 'get', side_effect=get):
            runner.execute(self.store, self.run_id)
        self.assertTrue(read_failed[0])
        self.assertEqual(self.store.get(self.run_id)['status'], 'failed')
        self.assertNotIn('equity', self.store.detail(self.run_id))
        with self.assertRaises(ValueError):
            self.store.csv_bytes(self.run_id, 'equity')


if __name__ == '__main__':
    unittest.main()
