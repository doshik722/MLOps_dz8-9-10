"""Дополнительные проверки атомарности, восстановления и идентичности выпуска."""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lab
import release


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'workspace'
        lab.initialize(self.root)
        self.manifest = lab.read(self.root / 'manifest-B.json')
        self.report = lab.read(self.root / 'report-B.json')

    def test_failed_fsync_preserves_pointer_and_removes_temp(self):
        target = self.root / 'accepted.json'
        before = target.read_bytes()
        with patch('os.fsync', side_effect=OSError('injected fsync failure')):
            with self.assertRaises(OSError):
                release.publish(self.manifest, self.report, target)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_crash_after_pointer_recovers_one_event_in_new_process(self):
        script = Path(__file__).resolve().parents[1] / 'lab.py'
        first = subprocess.run([sys.executable, str(script), '--workspace', str(self.root), 'run',
                                '--fail-after-publish'], capture_output=True, timeout=30)
        self.assertNotEqual(first.returncode, 0)
        self.assertTrue((self.root / 'pending-publication.json').exists())
        for _ in range(2):
            result = subprocess.run([sys.executable, str(script), '--workspace', str(self.root), 'run'],
                                    capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / 'pending-publication.json').exists())
        self.assertEqual(len(release.read_events(self.root)), 1)
        self.assertEqual(lab.read(self.root / 'accepted.json')['model_id'], 'fixture-B')

    def test_file_tampering_rejected_before_publication(self):
        before = (self.root / 'accepted.json').read_bytes()
        (self.root / 'model-B.txt').write_text('Изменённые веса STUB', encoding='utf-8')
        with self.assertRaises(ValueError):
            release.journaled_publish(self.manifest, self.report, self.root)
        self.assertEqual((self.root / 'accepted.json').read_bytes(), before)

    def test_release_identity_includes_runtime_and_evaluation(self):
        first = release.journaled_publish(self.manifest, self.report, self.root)['release_id']
        manifest, report = copy.deepcopy(self.manifest), copy.deepcopy(self.report)
        manifest['runtime']['dtype'] = 'float16'
        manifest['runtime_sha256'] = release.sha256(release.canonical(manifest['runtime']))
        report['runtime_sha256'] = manifest['runtime_sha256']
        second = release.journaled_publish(manifest, report, self.root)['release_id']
        self.assertNotEqual(first, second)
        report['evaluation_run_id'] = 'fixture-new-evaluation'
        third = release.journaled_publish(manifest, report, self.root)['release_id']
        self.assertNotEqual(second, third)
        self.assertEqual(len(release.read_events(self.root)), 3)

    def test_nan_and_foreign_report_preserve_pointer(self):
        before = (self.root / 'accepted.json').read_bytes()
        for changes in [{'metrics': {'accuracy': float('nan'), 'format_rate': 1}},
                        {'model_id': 'fixture-A'}, {'passed': 'true'}]:
            with self.subTest(changes=str(changes)):
                with self.assertRaises(ValueError):
                    release.journaled_publish(self.manifest, {**self.report, **changes}, self.root)
                self.assertEqual((self.root / 'accepted.json').read_bytes(), before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
