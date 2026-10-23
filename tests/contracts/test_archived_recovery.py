"""Archived resume bodies must not change a run or reach a remote host."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

from tests.support import ROOT


class ArchivedRecoveryTests(unittest.TestCase):
    def test_resume_routes_and_checkpoint_helpers_are_outside_active_source(self):
        self.assertFalse((ROOT/'playbooks/recovery').exists())
        self.assertFalse((ROOT/'playbooks/06-prepared-metadata.yml').exists())
        names = {'resume-before-target.yml', 'resume-metadata-preparation.yml',
                 'resume-after-db.yml', 'resume-after-cleanup.yml', 'resume-bootstrap.yml'}
        self.assertEqual({p.name for p in (ROOT/'archive/playbooks').glob('resume-*.yml')}, names)
        for name in ('metadata_recovery', 'pre_stage_recovery', 'post_db_recovery'):
            self.assertTrue((ROOT/'archive/scripts/recovery'/(name+'.py')).exists())
            self.assertTrue((ROOT/'archive/tests'/('test_'+name+'.py')).exists())
            self.assertFalse((ROOT/'scripts/recovery'/(name+'.py')).exists())
            self.assertFalse((ROOT/'tests/recovery'/('test_'+name+'.py')).exists())

    def test_current_playbooks_never_import_archived_recovery(self):
        for path in (ROOT/'playbooks').rglob('*.yml'):
            data = yaml.safe_load(path.read_text())
            for play in data:
                if isinstance(play, dict) and 'import_playbook' in play:
                    target = (path.parent/play['import_playbook']).resolve()
                    self.assertNotIn(ROOT/'archive', target.parents, str(path))
        imports = yaml.safe_load((ROOT/'migrate-to-ovn.yml').read_text())
        self.assertEqual(sum(row['import_playbook']=='playbooks/06-target-config.yml' for row in imports), 1)

    def test_real_ansible_stops_all_archived_resume_routes_without_touching_checkpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)/'original-run'
            root.mkdir()
            sentinel = root/'evidence-kept.txt'
            sentinel.write_text('original evidence')
            env = dict(os.environ, ANSIBLE_NOCOLOR='1')
            for entry in sorted((ROOT/'archive/playbooks').glob('resume-*.yml')):
                with self.subTest(entry=entry.name):
                    result = subprocess.run([sys.executable, '-m', 'ansible.cli.playbook', '-i',
                        str(ROOT/'tests/fixtures/syntax-inventory.ini'), str(entry), '-e',
                        'migration_resume_run_dir='+str(root)], cwd=ROOT, env=env,
                        capture_output=True, text=True, timeout=30)
                    self.assertNotEqual(result.returncode, 0, result.stdout+result.stderr)
                    self.assertIn('Refuse execution of an archived migration recovery route', result.stdout)
                    self.assertIn('This recovery route is archived and unsupported', result.stdout)
                    self.assertNotIn('TASK [Load runtime checkpoint]', result.stdout)
                    self.assertNotIn('TASK [[06]', result.stdout)
                    self.assertEqual([p.name for p in root.iterdir()], [sentinel.name])
                    self.assertEqual(sentinel.read_text(), 'original evidence')


if __name__ == '__main__':
    unittest.main()
