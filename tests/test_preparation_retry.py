"""First-create recovery must retain its run and never repeat a started cutover."""
import copy
import json
import pathlib
import tempfile
import unittest
import yaml
from test_validation import ROOT
from phase_schema import preparation_retry_check


class PreparationRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)/'run'
        (self.root/'metrics').mkdir(parents=True)
        self.runtime = {'phase_marker_schema_version':2, 'run_id':'run', 'inventory':'/inventory'}
        self.cfg = {'run':'run', 'inventory':'/inventory', 'placement_enabled':True,
                    'placement':{'0':{},'1':{}}}
        self.state = {'schema_version':2,'pre':{'measure':{
            '0':{'owned':True,'port':'port0'},'1':{'owned':True,'port':'port1'}}}}
        self.write()
        for phase in range(4):
            (self.root/'metrics'/f'phase{phase:02d}.end').write_text('100')
        (self.root/'metrics/phase04.start').write_text('101')

    def write(self):
        for name, value in [('runtime.json',self.runtime),('validation-config.json',self.cfg),
                            ('validation-resources.json',self.state)]:
            (self.root/name).write_text(json.dumps(value))

    def test_rejected_first_create_can_reuse_original_run_and_ports_read_only(self):
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        preparation_retry_check(self.root, '/inventory')
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_any_staging_target_freeze_or_completion_marker_blocks_retry(self):
        markers = ['phase04.end','phase05.start','phase06.start','phase07.start','phase08.end',
                   'phase13.end','control_plane_downtime.start','db_migration.start','total.end']
        for marker in markers:
            with self.subTest(marker=marker):
                path = self.root/'metrics'/marker
                path.write_text('')  # existence blocks retry even with a damaged timestamp
                with self.assertRaises(ValueError):
                    preparation_retry_check(self.root, '/inventory')
                path.unlink()

    def test_vm_creation_cleanup_or_later_pairs_cannot_use_first_create_retry(self):
        for fault in ('server','unowned','missing_port','cleanup','post','existing','tcp','legacy'):
            with self.subTest(fault=fault):
                state = copy.deepcopy(self.state)
                if fault == 'server': state['pre']['measure']['0']['server']='server0'
                if fault == 'unowned': state['pre']['measure']['0']['owned']=False
                if fault == 'missing_port': state['pre']['measure']['0'].pop('port')
                if fault == 'cleanup': state['pre']['cleanup_started']=True
                if fault in ('existing','tcp'): state['pre'][fault]={'0':{}}
                if fault == 'post': state['post']={'fresh':{}}
                if fault == 'legacy': state['historical_dual_pair']=True
                (self.root/'validation-resources.json').write_text(json.dumps(state))
                with self.assertRaises(ValueError):
                    preparation_retry_check(self.root, '/inventory')
        self.write()

    def test_wrong_run_inventory_schema_or_missing_prerequisite_markers_blocks_retry(self):
        for fault in ('run','inventory','schema','phase03','phase04'):
            with self.subTest(fault=fault):
                self.write()
                if fault == 'run':
                    (self.root/'validation-config.json').write_text(json.dumps(dict(self.cfg,run='other')))
                if fault == 'inventory':
                    (self.root/'runtime.json').write_text(json.dumps(dict(self.runtime,inventory='/other')))
                if fault == 'schema':
                    (self.root/'runtime.json').write_text(json.dumps(dict(self.runtime,phase_marker_schema_version=1)))
                marker = self.root/'metrics'/('phase03.end' if fault=='phase03' else 'phase04.start')
                if fault in ('phase03','phase04'): marker.unlink()
                with self.assertRaises(ValueError):
                    preparation_retry_check(self.root, '/inventory')
                if fault in ('phase03','phase04'): marker.write_text('100')

    def test_retry_entrypoint_rechecks_source_then_reuses_phase04_without_new_run(self):
        plays = yaml.safe_load((ROOT/'retry-validation-preparation.yml').read_text())
        imports = [p['import_playbook'] for p in plays if 'import_playbook' in p]
        self.assertEqual(imports, ['playbooks/02-precheck.yml'] + [
            'playbooks/'+filename for filename in ('04-validation-workloads.yml','05-stage-ovn-db.yml',
            '06-target-config.yml','07-migrate-db.yml','08-cutover.yml','09-cleanup.yml',
            '10-restore-neutron.yml','11-validate.yml','12-workload-validation.yml','13-report.yml')])
        tasks = yaml.safe_load((ROOT/'playbooks/04-validation-workloads.yml').read_text())[0]['tasks']
        task = next(t for t in tasks if t['name']=='Save non-secret guest validation configuration')
        self.assertIn('not (validation_retry_preparation_runtime | default(false) | bool)', task['when'])
        restore = next(t['ansible.builtin.set_fact'] for t in plays[0]['tasks']
                       if t['name']=='Restore the original run paths settings and prerequisites')
        self.assertEqual(restore['migration_run_dir'], '{{ migration_resume_run_dir }}')
        self.assertEqual(restore['migration_run_id'], '{{ validation_retry_runtime.run_id }}')
        self.assertTrue(restore['validation_retry_preparation_runtime'])


if __name__ == '__main__':
    unittest.main()
