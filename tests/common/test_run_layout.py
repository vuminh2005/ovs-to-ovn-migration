"""Organized files share one resolver; historical runs stay byte-for-byte flat."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from tests.validation.test_validation import ROOT, v
from run_layout import initialize, run_path, version
from phase_schema import mark, schema_version, resume_eligible
from cutover_preparation import prepare, verify


class LayoutTests(unittest.TestCase):
    def test_reports_remain_discoverable_and_evidence_is_not_duplicated(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);initialize(root)
            expected={'runtime.json':'config','inventory.before':'backups',
                'compute-port-ids.txt':'resources','validation-resources.json':'checkpoints',
                'measure0.pcap':'captures','collector.log':'logs','post-workload-checks.json':'evidence'}
            for name,directory in expected.items():
                path=run_path(root,name);path.write_text('original')
                self.assertEqual(path.parent,root/directory)
                self.assertFalse((root/name).exists())
            for name in ('migration-report.json','migration-report.txt'):
                self.assertEqual(run_path(root,name),root/name)
            self.assertEqual(run_path(root,'metrics/phase08.end'),root/'metrics/phase08.end')
            self.assertEqual(version(root),1)

    def test_legacy_resume_keeps_original_locations_and_cannot_be_relocated(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            original=b'{"phase_marker_schema_version":2}'
            (root/'runtime.json').write_bytes(original)
            self.assertEqual(run_path(root,'runtime.json'),root/'runtime.json')
            self.assertEqual(schema_version(root),2)
            with self.assertRaises(ValueError):initialize(root)
            self.assertEqual((root/'runtime.json').read_bytes(),original)
            self.assertFalse((root/'run-layout.json').exists())

    def test_unknown_layout_never_falls_back_to_a_stale_flat_checkpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'run-layout.json').write_text('{"schema_version":99}')
            (root/'runtime.json').write_text('{}')
            with self.assertRaises(ValueError):run_path(root,'runtime.json')

    def test_phase_markers_and_immutable_cutover_checkpoint_use_organized_files(self):
        with tempfile.TemporaryDirectory() as folder:
            parent=Path(folder);root=parent/'run-one';initialize(root)
            inventory=parent/'inventory';inventory.write_text('full inventory')
            run_path(root,'inventory.before').write_bytes(inventory.read_bytes())
            run_path(root,'runtime.json').write_text(json.dumps(dict(run_id='run-one',phase_marker_schema_version=2,
                inventory=str(inventory),ovn_cli_host='controller',db_sync_host='controller')))
            run_path(root,'compute-port-ids.txt').write_text('vm-port-1\nvm-port-2\n')
            checkpoint=prepare(root,'neutron:source',10,2,30)
            self.assertEqual(verify(root),checkpoint)
            mark(root,8,'start',10);mark(root,8,'end',11)
            self.assertTrue(resume_eligible(root))
            self.assertEqual(schema_version(root),2)
            self.assertFalse((root/'cutover-preparation.json').exists())
            run_path(root,'compute-port-ids.txt').write_text('changed-port\n')
            with self.assertRaises(ValueError):verify(root)

    def test_report_rebuild_reads_layout_and_keeps_original_timer_values(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);initialize(root)
            v.save(run_path(root,'runtime.json'),dict(phase_marker_schema_version=2))
            for name,value in [('total.start',10),('total.end',110),('db_migration.start',25),('db_migration.end',35)]:
                (root/'metrics'/name).write_text(str(value))
            v.save(run_path(root,'resource-counts.before.json'),dict(networks=4))
            backup=dict(enabled=True,volume='mariadb_backup',locations=[dict(host='controller',mountpoint='/volume/backup')])
            v.save(run_path(root,'mariadb-backup.json'),backup)
            subprocess.run([sys.executable,str(ROOT/'scripts/reporting/migration_report.py'),str(root),'run-one','inventory'],
                           check=True,capture_output=True,text=True)
            report=json.loads((root/'migration-report.json').read_text())
            self.assertEqual(report['total_duration_seconds'],100)
            self.assertEqual(report['db_migration_seconds'],10)
            self.assertEqual(report['resources_before'],{'networks':4})
            self.assertEqual(report['run_storage']['layout_schema_version'],1)
            self.assertEqual(report['run_storage']['mariadb_backup'],backup)
            self.assertNotIn('gateway_readiness',report)
            self.assertTrue((root/'migration-report.txt').exists())
            self.assertEqual({p.name for p in root.iterdir() if p.is_file()},
                {'run-layout.json','migration-report.json','migration-report.txt'})


if __name__=='__main__':unittest.main()
