import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
from pre_stage_recovery import inspect, select, record


class PreStageRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.parent = Path(self.tmp.name)
        self.inventory = self.parent/'inventory'
        self.inventory.write_text('[control]\ncontroller ansible_connection=local\n[network]\nnetwork1 ansible_connection=local\n[compute]\ncompute1 ansible_connection=local\ncompute2 ansible_connection=local\n')
        self.globals = self.parent/'globals.yml'
        self.globals.write_text('neutron_plugin_agent: openvswitch\nopenstack_release: "2024.1"\n')
        self.root = self.parent/'run-one'
        self.make(self.root)

    def put(self, root, name, value):
        (root/name).write_text(json.dumps(value))

    def make(self, root):
        root.mkdir()
        self.put(root, 'runtime.json', dict(phase_marker_schema_version=2, run_id=root.name,
            inventory=str(self.inventory), globals=str(self.globals), openrc='/etc/kolla/admin-openrc.sh',
            kolla_ansible='/venv/bin/kolla-ansible', control_hosts=['controller'], network_hosts=['network1'],
            compute_hosts=['compute1','compute2'], ovn_cli_host='controller', db_sync_host='controller'))
        (root/'inventory.before').write_bytes(self.inventory.read_bytes())
        (root/'metrics').mkdir()
        (root/'metrics/total.start').write_text('1')
        for phase in range(5):
            for edge, offset in [('start',0),('end',1)]:
                (root/'metrics'/f'phase{phase:02d}.{edge}').write_text(str(1+phase*2+offset))
        (root/'metrics/phase05.start').write_text('12')
        self.put(root, 'resource-counts.before.json', {'networks': 3})

    def workloads(self):
        cfg = dict(run=self.root.name, inventory=str(self.inventory), capture_enabled=True,
            pair_d_enabled=True, target_mtu=1392, allow_pre_cutover_guest_reboot=False,
            allow_post_cutover_guest_reboot=True, post_cutover_timeout=180, ns={'enabled':True},
            placement={str(i):{'inventory_host':'compute'+str(i+1)} for i in range(2)})
        self.put(self.root, 'validation-config.json', cfg)
        self.put(self.root, 'validation-resources.json', {'pre': {'tcp': {'original': 'session'}}})
        self.put(self.root, 'validation-window.json', {'pcap_start': {'index':7}})
        self.put(self.root, 'pair-d-baseline.json', {'session_id':'original'})
        self.put(self.root, 'ns-baseline.json', {'status':'PASS'})
        self.put(self.root, 'initial-workload-checks.json', {str(i): {key:'PASS' for key in
            ('identity','active','bound','dhcp','connectivity','metadata','mtu')} for i in range(2)})

    def test_select_and_record_keep_original_run_and_baselines(self):
        self.workloads()
        before = {p.relative_to(self.root):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(select(self.parent, self.inventory), self.root)
        record(self.root)
        self.assertEqual(before, {name:(self.root/name).read_bytes() for name in before})
        self.assertTrue(inspect(self.root,self.inventory)['workloads_enabled'])

    def test_later_phases_and_target_side_effects_cannot_resume(self):
        for name in ('metrics/phase05.end','metrics/phase06.start','metrics/phase07.start',
                     'metrics/db_migration.start','metrics/control_plane_downtime.start',
                     'metrics/total.end','network-mtu-migration.tsv','ml2_conf.ini.target',
                     'db-sync-migrate.log','pair-d-trigger.json','migration-report.json'):
            with self.subTest(name=name):
                path=self.root/name;path.write_text('13')
                with self.assertRaises(ValueError):inspect(self.root,self.inventory)
                path.unlink()

    def test_changed_inventory_incomplete_phase_and_ambiguous_selection_fail(self):
        self.inventory.write_text('changed')
        with self.assertRaisesRegex(ValueError,'Inventory changed'):inspect(self.root,self.inventory)
        self.inventory.write_bytes((self.root/'inventory.before').read_bytes())
        other=self.parent/'run-two';self.make(other)
        with self.assertRaisesRegex(ValueError,'found 2'):select(self.parent,self.inventory)
        (self.root/'metrics/phase04.end').unlink()
        with self.assertRaisesRegex(ValueError,'Completed'):inspect(self.root,self.inventory)

    def test_lost_baselines_or_started_cleanup_are_rejected(self):
        self.workloads()
        for filename, value in [('ns-baseline.json',{'status':'FAIL'}),
                                ('pair-d-baseline.json',{}),('validation-window.json',{}),
                                ('validation-resources.json',{'pre':{'cleanup_started':True}})]:
            original=(self.root/filename).read_bytes()
            self.put(self.root,filename,value)
            with self.subTest(filename=filename),self.assertRaises(ValueError):inspect(self.root,self.inventory)
            (self.root/filename).write_bytes(original)

    def execute(self, source_ovs=True):
        self.workloads()
        plays=yaml.safe_load((ROOT/'resume-before-target.yml').read_text())[:3]
        harness=self.parent/'harness';harness.mkdir()
        (harness/'scripts').symlink_to(ROOT/'scripts',target_is_directory=True)
        for play in plays:
            play['become']=False
            if 'vars_files' in play:play['vars_files']=[str(ROOT/file) for file in play['vars_files']]
        plays.append(dict(name='Check original settings',hosts='localhost',gather_facts=False,tasks=[
            {'ansible.builtin.assert':{'that':[
                "migration_run_dir == migration_resume_run_dir", "migration_run_id == 'run-one'",
                "validation_workloads_enabled | bool", "not validation_allow_pre_cutover_guest_reboot | bool",
                "chassis_count_runtime | int == 3", "target_geneve_mtu | int == 1392"]}}]))
        playbook=harness/'recover.yml';playbook.write_text(yaml.safe_dump(plays,sort_keys=False))
        binaries=harness/'bin';binaries.mkdir()
        live_config=harness/'ml2_conf.ini'
        live_config.write_text('[ml2]\nmechanism_drivers='+('openvswitch,l2population' if source_ovs else 'ovn')+'\ntenant_network_types=vxlan\n')
        docker=binaries/'docker'
        docker.write_text('#!/usr/bin/env python3\nimport json,sys\n'
            'if sys.argv[1]=="inspect":print(json.dumps([{"State":{"Running":True}}]))\n'
            'elif sys.argv[1:3]==["exec","neutron_server"]:exec(sys.argv[5].replace("/etc/neutron/plugins/ml2/ml2_conf.ini",'+repr(str(live_config))+'))\n'
            'elif "br-exists" in sys.argv:pass\n'
            'else:raise SystemExit(9)\n')
        docker.chmod(0o700)
        env=dict(os.environ,PATH=str(binaries)+':'+os.environ['PATH'],ANSIBLE_NOCOLOR='1')
        return subprocess.run([sys.executable,'-m','ansible.cli.playbook','-i',str(self.inventory),str(playbook),
            '-e','migration_resume_run_dir='+str(self.root)],capture_output=True,text=True,env=env,timeout=60)

    def test_real_ansible_recovers_without_changing_timers_or_workloads(self):
        self.workloads()
        before={p.relative_to(self.root):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result=self.execute()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(before,{name:(self.root/name).read_bytes() for name in before})
        self.assertTrue((self.root/'orchestration-recovery.json').is_file())

    def test_real_ansible_rejects_target_live_configuration_before_staging(self):
        result=self.execute(source_ovs=False)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('Require the live Neutron API process and OVS mechanism',result.stdout)
        self.assertFalse((self.root/'orchestration-recovery.json').exists())


if __name__=='__main__':unittest.main()
