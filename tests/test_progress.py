from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, get_ident
import unittest

from index_model import IndexBuilder, IndexMetadata
from index_pipeline import Pipeline, Step
from progress import LoggingProgressReporter, NO_PROGRESS
from snapshot_steps import CollectorCheckpointStore, PrepareSnapshotStep, RunInputs
from pipeline_config import PipelineConfig
from sqlite_model_store import SQLiteModelStore


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.lines = []
        self.reporter = LoggingProgressReporter(clock=self.clock, emit=self.lines.append)
        self.reporter.start_step(2, 5, 'Collector')

    def test_throttling_warmup_and_completion(self):
        p = self.reporter
        p.phase('collecting', total=10, unit='files', detail='a.cpp')
        initial = len(self.lines)
        self.clock.now = 2
        p.update(1)
        p.tick()
        self.assertEqual(len(self.lines), initial)
        self.clock.now = 5
        p.update(2)
        p.tick()
        self.assertIn('remaining unknown', self.lines[-1])
        self.clock.now = 6
        p.update(3, detail='b.cpp')
        self.clock.now = 10
        p.tick()
        self.assertIn('3/10 files | elapsed 10s | remaining ~10s | b.cpp', self.lines[-1])
        self.clock.now = 20
        p.update(10)
        self.assertIn('10/10 files', self.lines[-1])
        self.assertIn('remaining 0s', self.lines[-1])
        self.assertFalse(any('Completed' in line for line in self.lines))
        p.finish_step()
        self.assertIn('total elapsed 20.0s', self.lines[-1])

    def test_exhausted_estimate_becomes_unknown_until_new_completion(self):
        p = self.reporter
        p.phase('compiling', total=4, unit='contexts')
        for n in range(1, 4):
            self.clock.now = n * 2
            p.update(n)
        self.clock.now = 10
        p.tick()
        self.assertIn('remaining unknown', self.lines[-1])
        p.update(4)
        self.assertIn('remaining 0s', self.lines[-1])

    def test_phase_resets_estimate_and_detail_updates_are_not_samples(self):
        p = self.reporter
        p.phase('compiling', total=10, unit='contexts')
        for n in range(1, 4):
            self.clock.now = n * 2
            p.update(n)
        p.phase('merging')
        self.assertIn('remaining unknown', self.lines[-1])
        self.assertNotIn('contexts', self.lines[-1])
        p.phase('collecting', total=4, unit='files')
        self.clock.now = 12
        p.update(1)
        for _ in range(5):
            p.update(1, detail='still processing\nsecond file')
        p.tick()
        self.assertIn('remaining unknown', self.lines[-1])
        self.assertIn('still processing second file', self.lines[-1])

    def test_zero_work_bytes_and_invalid_counters(self):
        p = self.reporter
        p.phase('collecting', total=0, unit='files')
        self.assertIn('0/0 files', self.lines[-1])
        p.phase('importing', total=2 * 1048576, unit='bytes')
        p.update(2 * 1048576)
        self.assertIn('2.0/2.0 MiB', self.lines[-1])
        for completed in (-1, 1, 3 * 1048576):
            with self.assertRaises(ValueError):
                p.update(completed)
        for interval in (0, -1, float('inf'), float('nan')):
            with self.assertRaises(ValueError):
                LoggingProgressReporter(interval)

    def test_finished_and_failed_steps_stop_heartbeats(self):
        for end in (self.reporter.finish_step, self.reporter.fail_step):
            self.reporter.start_step(1, 1, 'Test')
            self.reporter.phase('working')
            end()
            count = len(self.lines)
            self.clock.now += 20
            self.reporter.tick()
            self.assertEqual(len(self.lines), count)

    def test_heartbeat_observes_state_and_joins_on_interruption(self):
        owner = get_ident()
        observed = Event()
        def emit(line):
            if get_ident() != owner:
                self.assertIn('waiting for compiler', line)
                observed.set()
        reporter = LoggingProgressReporter(0.01, clock=self.clock, emit=emit)
        with self.assertRaises(KeyboardInterrupt):
            with reporter:
                thread = reporter._thread
                reporter.start_step(1, 1, 'AST')
                reporter.phase('waiting for compiler')
                self.clock.now = 10
                self.assertTrue(observed.wait(2), 'heartbeat did not run')
                raise KeyboardInterrupt
        self.assertFalse(thread.is_alive())
        self.assertIsNone(reporter._thread)

    def test_pipeline_saves_before_completion_and_reports_failure(self):
        b = IndexBuilder()
        b.set_metadata(IndexMetadata(project_root='/example'))
        class Identity(Step):
            def run(inner, model):
                self.clock.now = 3
                return model
        class Store:
            def save(inner, model):
                self.assertFalse(any('Completed' in line for line in self.lines))
                self.clock.now = 8
        Pipeline([Identity()], store=Store(), progress=self.reporter).run(b.build())
        self.assertIn('total elapsed 8.0s', self.lines[-1])
        class BrokenStore:
            def save(inner, model):
                raise RuntimeError('save failed')
        self.lines.clear()
        with self.assertLogs('index_pipeline', level='ERROR'), self.assertRaisesRegex(RuntimeError, 'save failed'):
            Pipeline([Identity()], store=BrokenStore(), progress=self.reporter).run(b.build())
        self.assertIn('Failed or interrupted', self.lines[-1])
        self.assertFalse(any('Completed' in line for line in self.lines))

    def test_database_phases_and_skipped_preparation_checkpoint(self):
        with TemporaryDirectory() as directory:
            b = IndexBuilder()
            b.set_metadata(IndexMetadata(project_root=directory))
            preparation = PrepareSnapshotStep(RunInputs(PipelineConfig(Path(directory), ())))
            seed = preparation.run(b.build())
            store = SQLiteModelStore(Path(directory) / 'out.sqlite3', progress=self.reporter)
            checkpoint = CollectorCheckpointStore(store, preparation)
            checkpoint.save(seed)
            self.assertFalse(any('database' in line for line in self.lines))
            self.assertFalse(store.path.exists())
            checkpoint.save(b.build())
            phases = ('validating database snapshot', 'opening database', 'writing database',
                      'checking database integrity', 'committing database')
            self.assertEqual([line.split(' | ')[1] for line in self.lines if ' | ' in line], list(phases))

    def test_noop_reporter_needs_no_thread_or_model(self):
        with NO_PROGRESS:
            NO_PROGRESS.start_step(1, 1, 'test')
            NO_PROGRESS.phase('work')
            NO_PROGRESS.update(1)
            NO_PROGRESS.finish_step()

    def test_large_finite_interval_can_be_stopped(self):
        with LoggingProgressReporter(1e100, emit=self.lines.append) as reporter:
            thread = reporter._thread
            reporter.start_step(1, 1, 'test')
            reporter.phase('waiting')
        self.assertFalse(thread.is_alive())


if __name__ == '__main__':
    unittest.main()
