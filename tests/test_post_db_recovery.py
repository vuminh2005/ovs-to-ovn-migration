import copy
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
import yaml

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from post_db_recovery import inspect, select, record


class PostDBRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.parent=Path(self.temp.name)
        self.inventory=self.parent/'inventory';self.inventory.write_text('full original Kolla inventory')
        self.root=self.parent/'run-one';self.root.mkdir()
        self.make(self.root)

    def make(self,root):
        def put(name,value): (root/name).write_text(json.dumps(value))
        put('runtime.json',dict(phase_marker_schema_version=2,run_id=root.name,inventory=str(self.inventory),
            kolla_ansible='/venv/bin/kolla-ansible',globals='/etc/kolla/globals.yml',openrc='/etc/kolla/admin-openrc.sh',
            control_hosts=['controller'],network_hosts=['network1'],compute_hosts=['compute1','compute2'],
            ovn_cli_host='controller',db_sync_host='controller'))
        (root/'inventory.before').write_bytes(self.inventory.read_bytes())
        metrics=root/'metrics';metrics.mkdir()
        for name,value in [('phase06.end',1),('phase07.start',2),('control_plane_downtime.start',2.2),
                           ('db_migration.start',3),('db_migration.end',4)]:
            (metrics/name).write_text(str(value))
        (root/'db-sync-migrate.log').write_text('returncode: 0\n--- stdout ---\nsuccess\n')
        put('provider-associations-verification.json',{'status':'PASS'})
        put('provider-associations.after.json',[{'resource_id':'router','provider_name':'ovn'}])
        (root/'compute-port-ids.txt').write_text('port0\nport1\n')
        put('resource-counts.before.json',{'networks':6})
        (root/'ml2_conf.ini.target').write_text('[ovn]\novn_nb_connection=tcp:controller:6641\novn_sb_connection=tcp:controller:6642\n')
        from cutover_preparation import prepare
        (metrics/'control_plane_downtime.start').unlink()
        prepare(root, 'neutron:source', 120, 2, 900)
        (metrics/'control_plane_downtime.start').write_text('2.2')

    def test_successful_db_frozen_before_cutover_is_selected(self):
        value=inspect(self.root,self.inventory)
        self.assertEqual(value['runtime']['run_id'],'run-one')
        self.assertEqual(value['nb'],'tcp:controller:6641')
        self.assertEqual(select(self.parent,self.inventory),self.root)

    def test_started_cutover_or_restored_api_is_rejected(self):
        for name in ('phase08.start','phase08.end','phase09.start','phase10.start','control_plane_downtime.end','total.end'):
            marker=self.root/'metrics'/name;marker.write_text('5')
            with self.subTest(name=name),self.assertRaisesRegex(ValueError,'already began'):
                inspect(self.root,self.inventory)
            marker.unlink()

    def test_failed_or_unfinished_db_migration_is_rejected(self):
        (self.root/'db-sync-migrate.log').write_text('returncode: 1\n')
        with self.assertRaisesRegex(ValueError,'rc=0'):inspect(self.root,self.inventory)
        (self.root/'db-sync-migrate.log').write_text('returncode: 0\n')
        (self.root/'metrics/db_migration.end').unlink()
        with self.assertRaisesRegex(ValueError,'Missing'):inspect(self.root,self.inventory)

    def test_uncommitted_provider_associations_are_rejected(self):
        (self.root/'provider-associations.after.json').write_text(json.dumps([{'resource_id':'router','provider_name':'single_node'}]))
        with self.assertRaisesRegex(ValueError,'legacy'):inspect(self.root,self.inventory)

    def test_changed_inventory_cannot_resume(self):
        self.inventory.write_text('changed inventory')
        with self.assertRaisesRegex(ValueError,'changed'):inspect(self.root,self.inventory)

    def test_two_eligible_runs_are_not_silently_selected(self):
        another=self.parent/'run-two';another.mkdir();self.make(another)
        with self.assertRaisesRegex(ValueError,'found 2'):select(self.parent,self.inventory)

    def test_original_capture_and_tcp_session_are_required(self):
        cfg=dict(run='run-one',capture_enabled=True,pair_d_enabled=True,ns={'enabled':True})
        (self.root/'validation-resources.json').write_text('{}')
        (self.root/'validation-config.json').write_text(json.dumps(cfg))
        (self.root/'validation-window.json').write_text(json.dumps({'pcap_start':{'index':5}}))
        (self.root/'pair-d-baseline.json').write_text(json.dumps({'session_id':'original-session'}))
        (self.root/'pair-d-trigger.json').write_text(json.dumps({'status':'PASS','ack':{'session_id':'original-session'},'acknowledged_at':2.5}))
        for name in ('ns-baseline.json','ns-precutover.json'):
            (self.root/name).write_text(json.dumps({'status':'PASS'}))
        self.assertTrue(inspect(self.root,self.inventory)['workloads_enabled'])
        (self.root/'pair-d-trigger.json').write_text(json.dumps({'status':'PASS','ack':{'session_id':'replaced'},'acknowledged_at':2.5}))
        with self.assertRaisesRegex(ValueError,'session'):inspect(self.root,self.inventory)

    def test_recovery_record_does_not_rebase_original_timestamps(self):
        before={p.name:p.read_bytes() for p in (self.root/'metrics').iterdir()}
        record(self.root);record(self.root)
        self.assertEqual({p.name:p.read_bytes() for p in (self.root/'metrics').iterdir()},before)
        data=json.loads((self.root/'orchestration-recovery.json').read_text())
        self.assertEqual(len(data['events']),1)

    def test_entrypoint_starts_at_verification_and_phase08(self):
        plays=yaml.safe_load((ROOT/'resume-after-db.yml').read_text())
        imports=[p['import_playbook'] for p in plays if 'import_playbook' in p]
        self.assertEqual(imports,[f'playbooks/{f}' for f in ('08-cutover.yml','09-cleanup.yml','10-restore-neutron.yml',
            '11-validate.yml','12-workload-validation.yml','13-report.yml')])
        before=str(plays[:3])
        self.assertNotIn('neutron-ovn-db-sync-util',before)
        self.assertNotIn('d-arm',before)
        self.assertNotIn('freeze-start',before)
        self.assertTrue(all(p['any_errors_fatal'] for p in plays[:3]))

    def execute_bootstrap(self, frozen=True, missing_preparation=False):
        # Run the production recovery plays, stopping before real cutover imports.
        self.inventory.write_text('[control]\ncontroller ansible_connection=local\n[network]\nnetwork1 ansible_connection=local\n[compute]\ncompute1 ansible_connection=local\ncompute2 ansible_connection=local\n')
        (self.root/'inventory.before').write_bytes(self.inventory.read_bytes())
        # The fixture changes inventory before executing the production bootstrap.
        from cutover_preparation import prepare
        (self.root/'cutover-preparation.json').unlink()
        (self.root/'metrics/control_plane_downtime.start').unlink()
        prepare(self.root, 'neutron:source', 120, 2, 900)
        (self.root/'metrics/control_plane_downtime.start').write_text('2.2')
        if missing_preparation:
            (self.root/'cutover-preparation.json').unlink()
        plays=yaml.safe_load((ROOT/'resume-after-db.yml').read_text())[:3]
        harness=self.parent/'harness';harness.mkdir()
        (harness/'scripts').symlink_to(ROOT/'scripts',target_is_directory=True)
        for play in plays:
            play['become']=False
            if 'vars_files' in play:
                play['vars_files']=[str(ROOT/file) for file in play['vars_files']]
        plays.append(dict(name='Check recovered facts',hosts='localhost',gather_facts=False,tasks=[
            {'ansible.builtin.assert':{'that':["migration_run_id == 'run-one'", "ovn_nb_connection_runtime == 'tcp:controller:6641'",
               "chassis_count_runtime | int == 3", "not validation_workloads_enabled | bool"]}}]))
        playbook=harness/'recover.yml';playbook.write_text(yaml.safe_dump(plays,sort_keys=False))
        binaries=harness/'bin';binaries.mkdir()
        docker=binaries/'docker'
        docker.write_text('#!/usr/bin/env python3\nimport json,sys\n'
            'if sys.argv[1:3]==["inspect","neutron_server"]: print(json.dumps([{"State":{"Running":'+str(not frozen)+'}}]))\n'
            'elif "ovn-nbctl" in sys.argv: print("switch recovered\\nrouter recovered")\n'
            'elif "ovn-sbctl" in sys.argv: print("_uuid : recovered")\n'
            'else: raise SystemExit(9)\n')
        docker.chmod(0o700)
        env=dict(os.environ,PATH=str(binaries)+':'+os.environ['PATH'],ANSIBLE_NOCOLOR='1')
        return subprocess.run([sys.executable,'-m','ansible.cli.playbook','-i',str(self.inventory),str(playbook),
            '-e','migration_resume_run_dir='+str(self.root)],capture_output=True,text=True,env=env,timeout=60)

    def test_real_ansible_restores_runtime_and_keeps_original_db_freeze_timestamps(self):
        before={name:(self.root/'metrics'/name).read_bytes() for name in ('phase07.start','control_plane_downtime.start','db_migration.start','db_migration.end')}
        result=self.execute_bootstrap()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertTrue((self.root/'metrics/phase07.end').exists())
        self.assertFalse((self.root/'metrics/phase08.start').exists())
        self.assertEqual({name:(self.root/'metrics'/name).read_bytes() for name in before},before)

    def test_real_ansible_refuses_if_neutron_was_restarted(self):
        result=self.execute_bootstrap(frozen=False)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('Require neutron-server to remain stopped',result.stdout)
        self.assertFalse((self.root/'metrics/phase07.end').exists())
        self.assertFalse((self.root/'orchestration-recovery.json').exists())

    def test_real_ansible_rejects_missing_pre_freeze_preparation_before_live_recovery(self):
        result=self.execute_bootstrap(missing_preparation=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('Require original pre-freeze preparation',result.stdout)
        self.assertNotIn('Require neutron-server to remain stopped',result.stdout)
        self.assertFalse((self.root/'metrics/phase08.start').exists())
        self.assertEqual((self.root/'metrics/control_plane_downtime.start').read_text(),'2.2')


if __name__=='__main__':unittest.main()
