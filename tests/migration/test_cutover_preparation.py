"""Immutable pre-freeze inputs, additive OVS changes and owned reset cleanup."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
import shutil
import yaml
from unittest.mock import patch

from tests.validation.test_validation import ROOT
import cutover_preparation as c
import openflow_prepare as o


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name)
        self.root = self.parent/'run-one'; self.root.mkdir()
        inventory = self.parent/'inventory'; inventory.write_text('original inventory')
        (self.root/'inventory.before').write_bytes(inventory.read_bytes())
        (self.root/'compute-port-ids.txt').write_text('port-a\nport-b\n')
        (self.root/'runtime.json').write_text(json.dumps(dict(run_id='run-one', inventory=str(inventory),
            ovn_cli_host='central', db_sync_host='control')))

    def prepare(self):
        return c.prepare(self.root, 'neutron:source', 120, 2, 900)

    def test_timeout_and_repeated_preparation_preserve_checkpoint_bytes(self):
        value = self.prepare()
        self.assertEqual(value['binding_timeout'],124)
        before = (self.root/'cutover-preparation.json').read_bytes()
        self.assertEqual(self.prepare(), value)
        self.assertEqual(before,(self.root/'cutover-preparation.json').read_bytes())
        with self.assertRaisesRegex(ValueError,'overwrite'):
            c.prepare(self.root,'other-image',120,2,900)
        self.assertEqual(before,(self.root/'cutover-preparation.json').read_bytes())

    def test_preparation_never_rebases_frozen_run_but_verification_is_read_only(self):
        self.prepare(); (self.root/'metrics').mkdir()
        marker = self.root/'metrics/control_plane_downtime.start'; marker.write_text('100.25')
        with self.assertRaisesRegex(ValueError,'before Neutron freeze'): self.prepare()
        c.verify(self.root)
        self.assertEqual(marker.read_text(),'100.25')

    def test_changed_port_list_inventory_and_target_host_are_rejected(self):
        self.prepare()
        for name, replacement in [('compute-port-ids.txt',b'new-port\n'),
                ('inventory.before',b'foreign inventory'),
                ('runtime.json',json.dumps(dict(run_id='run-one',inventory=str(self.parent/'inventory'),
                    ovn_cli_host='foreign',db_sync_host='control')).encode())]:
            path=self.root/name; old=path.read_bytes(); path.write_bytes(replacement)
            with self.subTest(name=name),self.assertRaises(ValueError): c.verify(self.root)
            path.write_bytes(old)

    def test_original_host_topology_is_part_of_the_immutable_preparation(self):
        topology = self.root/'host-topology.json'
        topology.write_text('{"node-a":{"system_id":"original"}}')
        value = self.prepare()
        self.assertEqual(value['topology_sha256'], hashlib.sha256(topology.read_bytes()).hexdigest())
        topology.write_text('{"node-a":{"system_id":"replacement"}}')
        with self.assertRaisesRegex(ValueError, 'does not match'):
            c.verify(self.root)
        topology.unlink()
        with self.assertRaisesRegex(ValueError, 'does not match'):
            c.verify(self.root)

    def owned(self, run):
        path=self.parent/('ovs-to-ovn-preparation-'+run); path.mkdir()
        (path/'owner.json').write_text(json.dumps(dict(run=run,directory=str(path),
            inventory_sha256=hashlib.sha256(b'inventory').hexdigest())))
        (path/'provider_associations.py').write_text('owned staged helper')
        return path

    def test_reset_is_idempotent_and_preserves_unrelated_evidence(self):
        owned=self.owned('old-run'); evidence=self.parent/'migration-report.txt'; evidence.write_text('keep')
        value=c.cleanup(self.parent)
        self.assertEqual(value['removed'][0]['path'],str(owned)); self.assertFalse(owned.exists())
        self.assertEqual(evidence.read_text(),'keep'); self.assertEqual(c.cleanup(self.parent)['removed'],[])

    def test_reset_validates_every_owner_before_any_removal(self):
        owned=self.owned('a'); foreign=self.parent/'ovs-to-ovn-preparation-z'; foreign.mkdir()
        with self.assertRaises(FileNotFoundError): c.cleanup(self.parent)
        self.assertTrue(owned.exists()); self.assertTrue(foreign.exists())
        foreign.rmdir(); foreign.symlink_to(owned,target_is_directory=True)
        with self.assertRaisesRegex(ValueError,'Unsafe'): c.cleanup(self.parent)
        self.assertTrue(owned.exists())

    def test_reset_cleanup_runs_as_an_isolated_ansible_script_on_all_four_hosts(self):
        # Use the production play: script transfers only cutover_preparation.py,
        # without its controller-side sibling modules. Each local host alias gets
        # a private cleanup parent instead of touching the real /tmp directories.
        harness=self.parent/'harness'; harness.mkdir()
        (harness/'scripts').symlink_to(ROOT/'scripts',target_is_directory=True)
        snapshots=self.parent/'snapshot'; snapshots.mkdir()
        inventory=harness/'inventory.ini'
        inventory.write_text('[all:vars]\nansible_connection=local\n'
            'ansible_python_interpreter='+sys.executable+'\n'
            '[control]\ncontroller\n[network]\nnetwork1\n'
            '[compute]\ncompute1\ncompute2\n')
        hosts=('controller','network1','compute1','compute2')
        for host in hosts:
            parent=self.parent/host; parent.mkdir()
            owned=parent/('ovs-to-ovn-preparation-'+host); owned.mkdir()
            (owned/'owner.json').write_text(json.dumps(dict(run=host,
                directory=str(owned),inventory_sha256=hashlib.sha256(b'inventory').hexdigest())))
            (parent/'migration-report.txt').write_text('keep')
        production=next(p for p in yaml.safe_load((ROOT/'reset-lab-to-ovs.yml').read_text())
            if p['name']=='Remove only owned preparation directories after successful Kolla destroy')
        production['become']=False
        production['vars_files']=[str(ROOT/'group_vars/reset.yml')]
        production['tasks'][0]['ansible.builtin.script']=(
            'scripts/migration/cutover_preparation.py cleanup '+str(self.parent)+'/{{ inventory_hostname }}')
        plays=[dict(hosts='localhost',gather_facts=False,tasks=[
            {'ansible.builtin.set_fact':dict(reset_run_dir=str(snapshots))}]),production]
        play=harness/'reset-cleanup.yml'; play.write_text(yaml.safe_dump(plays,sort_keys=False))
        env=dict(os.environ,ANSIBLE_NOCOLOR='1'); env.pop('PYTHONPATH',None)
        for expected_count in (1,0):
            result=subprocess.run([sys.executable,'-m','ansible.cli.playbook','-i',str(inventory),str(play)],
                cwd=harness,env=env,capture_output=True,text=True,timeout=50)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            for host in hosts:
                value=json.loads((snapshots/('preparation-cleanup-'+host+'.json')).read_text())
                self.assertEqual(value['status'],'PASS')
                self.assertEqual(len(value['removed']),expected_count)
                self.assertFalse((self.parent/host/('ovs-to-ovn-preparation-'+host)).exists())
                self.assertEqual((self.parent/host/'migration-report.txt').read_text(),'keep')


class OpenFlowTests(unittest.TestCase):
    def fake(self, protocols):
        self.protocols=list(protocols); self.calls=[]
        def run(args):
            self.calls.append(args)
            if args[0]=='ovs-vsctl' and 'list' in args:
                return json.dumps(dict(headings=['_uuid','name','protocols'],
                    data=[[['uuid','original-bridge'],'br-int',['set',self.protocols]]]))
            if args[:4]==['ovs-vsctl','set','Bridge','br-int']:
                self.protocols=args[4].split('=',1)[1].split(','); return ''
            if args==['ovs-ofctl','-O','OpenFlow15','show','br-int']: return 'OF15 reply'
            raise AssertionError(args)
        return run

    def test_additive_preparation_preserves_legacy_protocols_and_bridge(self):
        with patch.object(o,'run',side_effect=self.fake(['OpenFlow10','OpenFlow13'])):
            value=o.prepare('OpenFlow13,OpenFlow15')
            self.assertTrue(value['changed']); self.assertIn('OpenFlow10',self.protocols)
            self.assertEqual(value['before']['uuid'],value['after']['uuid'])
            self.assertFalse(o.prepare('OpenFlow13,OpenFlow15')['changed'])
            self.assertFalse(o.prepare('OpenFlow13,OpenFlow15',check=True)['changed'])
        self.assertEqual(sum('set' in args for args in self.calls),1)
        self.assertFalse(any(set(args)&{'del-flows','del-br','del-port','set-controller'} for args in self.calls))

    def test_cutover_check_never_mutates_missing_protocols(self):
        with patch.object(o,'run',side_effect=self.fake(['OpenFlow13'])):
            with self.assertRaisesRegex(ValueError,'before Neutron freeze'):
                o.prepare('OpenFlow13,OpenFlow15',check=True)
        self.assertFalse(any('set' in args for args in self.calls))

    def test_empty_protocol_set_preserves_default_openflow10(self):
        with patch.object(o,'run',side_effect=self.fake([])):
            o.prepare('OpenFlow15')
            self.assertEqual(self.protocols,['OpenFlow10','OpenFlow15'])

    def test_real_ansible_include_runs_idempotently_and_persists_bridge_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); binary=root/'bin'; binary.mkdir()
            state=root/'ovs.json'; calls=root/'calls.jsonl'
            state.write_text(json.dumps(['OpenFlow10','OpenFlow13']))
            docker=binary/'docker'
            docker.write_text('#!'+sys.executable+'\n'+
                'import json,os,sys\nfrom pathlib import Path\n'
                'a=sys.argv[3:]; p=Path(os.environ["TEST_OVS_STATE"])\n'
                'with open(os.environ["TEST_OVS_CALLS"],"a") as f:f.write(json.dumps(a)+"\\n")\n'
                'if sys.argv[1:3]==["inspect","neutron_server"]:print("neutron:source")\n'
                'elif "list" in a:print(json.dumps({"headings":["_uuid","name","protocols"],'
                '"data":[[["uuid","original"],"br-int",["set",json.loads(p.read_text())]]]}))\n'
                'elif "set" in a:p.write_text(json.dumps(a[-1].split("=",1)[1].split(",")))\n'
                'elif a[:3]==["ovs-ofctl","-O","OpenFlow15"]:print("OF15 reply")\n'
                'else:raise SystemExit(8)\n')
            docker.chmod(0o700)
            play=root/'harness.yml'
            play.write_text('''- hosts: localhost
  connection: local
  gather_facts: false
  vars:
    openflow_protocols: OpenFlow13,OpenFlow15
    openflow_evidence_directory: '''+str(root)+'''
  tasks:
    - include_tasks: '''+str(ROOT/'playbooks/tasks/openflow-preparation-tasks.yml')+'''
    - include_tasks: '''+str(ROOT/'playbooks/tasks/openflow-preparation-tasks.yml')+'''
      vars:
        openflow_check_only: true
        openflow_evidence_stage: cutover-check
''')
            env=dict(os.environ,PATH=str(binary)+os.pathsep+os.environ['PATH'],
                TEST_OVS_STATE=str(state),TEST_OVS_CALLS=str(calls),ANSIBLE_NOCOLOR='1')
            result=subprocess.run([sys.executable,'-m','ansible.cli.playbook','-i','localhost,',str(play)],
                env=env,capture_output=True,text=True,timeout=40)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertTrue(json.loads((root/'openflow-preparation-localhost.json').read_text())['changed'])
            self.assertFalse(json.loads((root/'openflow-cutover-check-localhost.json').read_text())['changed'])
            self.assertEqual(sum('set' in json.loads(line) for line in calls.read_text().splitlines()),1)

            # Execute the actual new preparation plays, including image templating,
            # delegated facts, private helper/port staging, and per-host OF evidence.
            run=root/('run-'+uuid.uuid4().hex); run.mkdir()
            inventory=root/'inventory.ini'
            inventory.write_text('[control]\ncontroller ansible_connection=local ansible_python_interpreter='+sys.executable+
                '\n[network]\nnetwork1 ansible_connection=local ansible_python_interpreter='+sys.executable+
                '\n[compute]\ncompute1 ansible_connection=local ansible_python_interpreter='+sys.executable+'\n')
            (run/'inventory.before').write_bytes(inventory.read_bytes())
            (run/'compute-port-ids.txt').write_text('old-port-0\nold-port-1\n')
            (run/'runtime.json').write_text(json.dumps(dict(run_id=run.name,inventory=str(inventory),
                ovn_cli_host='controller',db_sync_host='controller')))
            harness=root/'harness'; (harness/'playbooks').mkdir(parents=True)
            (harness/'scripts').symlink_to(ROOT/'scripts',target_is_directory=True)
            source=yaml.safe_load((ROOT/'playbooks/07-migrate-db.yml').read_text())
            plays=[p for p in source if p['name'] in (
                'Phase 07 - Prepare database migration inputs before API freeze',
                'Phase 07 - Prepare existing VM port checkpoint',
                'Phase 07 - Prepare OpenFlow support before API freeze')]
            self.assertEqual(len(plays),3)
            for production in plays:
                production['become']=False
                production['vars_files']=[str(ROOT/'group_vars/all.yml')]
                for task in production['tasks']:
                    if 'ansible.builtin.include_tasks' in task:
                        task['ansible.builtin.include_tasks']=str(ROOT/'playbooks/tasks/openflow-preparation-tasks.yml')
            plays.insert(0,dict(hosts='localhost',gather_facts=False,tasks=[{'ansible.builtin.set_fact':
                dict(migration_run_dir=str(run),ovn_cli_host_runtime='controller')}]))
            stage=harness/'playbooks/stage.yml'; stage.write_text(yaml.safe_dump(plays,sort_keys=False))
            remote=Path('/tmp')/('ovs-to-ovn-preparation-'+run.name)
            try:
                result=subprocess.run([sys.executable,'-m','ansible.cli.playbook','-i',str(inventory),str(stage)],
                    env=env,capture_output=True,text=True,timeout=50)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                prepared=c.verify(run)
                self.assertEqual(prepared['neutron_image'],'neutron:source')
                self.assertEqual(Path(prepared['ports_path']).read_bytes(),(run/'compute-port-ids.txt').read_bytes())
                self.assertEqual((remote/'provider_associations.py').read_text().strip(),
                                 (ROOT/'scripts/migration/provider_associations.py').read_text().strip())
                self.assertEqual(len(list(run.glob('openflow-preparation-*.json'))),2)
                self.assertFalse((run/'metrics/control_plane_downtime.start').exists())
            finally:
                if remote.exists(): shutil.rmtree(remote)


if __name__=='__main__': unittest.main()
