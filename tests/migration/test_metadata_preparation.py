"""Run the generated preparation play through real Ansible, with modeled Docker."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

from tests.support import ROOT
from prepare_metadata import prepare_tree, run


API = '''from ansible.module_utils.basic import AnsibleModule
def generate_module():
    m=AnsibleModule(argument_spec=dict(action=dict(type='str'),common_options=dict(type='dict'),
      name=dict(type='str'),image=dict(type='str'),volumes=dict(type='list'),dimensions=dict(type='dict'),
      privileged=dict(type='bool'),healthcheck=dict(type='dict'),labels=dict(type='dict')))
    p=m.params; common=p.pop('common_options'); p.update(common)
    p.update(container_engine='docker',detach=True,restart_policy='unless-stopped')
    return m
'''

SYSTEMD = '''class SystemdWorker:
    def __init__(self,path): self.path=path
    def enable(self): (self.path.parent/'ILLEGAL-ENABLE').write_text('started too early')
    def create_unit_file(self): self.enable(); return True
'''

DOCKER = '''import copy,json,hashlib
from pathlib import Path
from ansible.module_utils.kolla_systemd_worker import SystemdWorker
class DockerWorker:
    def __init__(self,module):
        self.params=module.params; self.changed=False
        config=Path(self.params['volumes'][0].split(':')[0])
        self.path=config.parents[2]/'state'/(config.parent.name+'.json')
        self.path.parent.mkdir(exist_ok=True)
        self.systemd=SystemdWorker(self.path); self.dc=self
    def check_container(self): return json.loads(self.path.read_text()) if self.path.exists() else None
    def check_image(self): return {'Id':'sha256:approved'}
    def inspect_container(self,name): return self.check_container()
    def check_container_differs(self):
        d=self.check_container()
        return d['Config']['Labels']!=self.params['labels'] or d['State']['Status']!=self.params['state']
    def build_container_options(self): return copy.deepcopy(self.params)
    def create_container(self):
        p=self.build_container_options(); self.changed=True
        d=dict(Id='id-'+self.path.stem,Image='sha256:approved',State=dict(Status='created',Running=False),
           Config=dict(Labels=p['labels'],Image=p['image']),
           HostConfig=dict(Binds=p['volumes'],RestartPolicy=dict(Name='no')),native_options=p)
        self.path.write_text(json.dumps(d)); self.systemd.create_unit_file()
    def remove_container(self): self.path.unlink()
'''


class MetadataPreparationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.parent = Path(tmp.name)
        self.binary = self.parent/'venv/bin/kolla-ansible'
        self.binary.parent.mkdir(parents=True)
        self.source = self.binary.parent.parent/'share/kolla-ansible/ansible'
        for name in ('library','module_utils','group_vars','roles/neutron/handlers',
                     'roles/neutron/defaults','roles/neutron/tasks','roles/service-images-pull/tasks'):
            (self.source/name).mkdir(parents=True)
        (self.source/'library/kolla_container.py').write_text(API)
        (self.source/'module_utils/kolla_docker_worker.py').write_text(DOCKER)
        (self.source/'module_utils/kolla_systemd_worker.py').write_text(SYSTEMD)
        (self.source/'roles/service-images-pull/tasks/main.yml').write_text('[]\n')
        (self.source/'roles/neutron/handlers/main.yml').write_bytes(
            (ROOT/'tests/fixtures/kolla-18.8/metadata-handler.yml').read_bytes())
        (self.source/'roles/neutron/tasks/main.yml').write_text('- include_tasks: "{{ kolla_action }}.yml"\n')
        config = [
            dict(name='Create native metadata config directory',
                **{'ansible.builtin.file': dict(path='{{ node_config_directory }}/neutron-ovn-metadata-agent',state='directory')}),
            dict(name='Generate native metadata config',
                **{'ansible.builtin.copy':dict(dest='{{ node_config_directory }}/neutron-ovn-metadata-agent/config.json',
                    content='{"command":"neutron-ovn-metadata-agent"}')},
                notify='Restart neutron-ovn-metadata-agent container')]
        (self.source/'roles/neutron/tasks/config.yml').write_text(yaml.safe_dump(config))
        self.root = self.parent/'run-one'
        self.root.mkdir()
        self.inventory = self.parent/'inventory'
        self.globals = self.parent/'globals.yml'
        self.globals.write_text('neutron_plugin_agent: ovn\n')
        variables = dict(kolla_container_engine='docker',docker_common_options={},
            node_config_directory=str(self.parent/'configs')+'/{{ inventory_hostname }}',
            neutron_ovn_metadata_agent_image_full='quay.io/metadata:2024.1',
            neutron_services={'neutron-ovn-metadata-agent':dict(enabled=True,
                container_name='neutron_ovn_metadata_agent',image='{{ neutron_ovn_metadata_agent_image_full }}',
                privileged=True,volumes=['{{ node_config_directory }}/neutron-ovn-metadata-agent/:/var/lib/kolla/config_files/:ro',
                    'custom-volume:/extra:ro'],dimensions={'mem_limit':'1G'},healthcheck={'test':['CMD','true']})})
        (self.source/'group_vars/all.yml').write_text(yaml.safe_dump(variables))
        (self.source/'site.yml').write_text(yaml.safe_dump([dict(hosts='network:compute',
            gather_facts=False,roles=[dict(role='neutron',tags=['neutron'])])]))
        self.binary.write_text('#!'+sys.executable+'\nimport subprocess,sys\na=sys.argv[1:]\n'
            'cmd=[sys.executable,"-m","ansible.cli.playbook","-i",a[a.index("-i")+1],a[a.index("--playbook")+1],'
            '"--limit",a[a.index("--limit")+1],"--tags",a[a.index("--tags")+1],"-e","kolla_action=config",'
            '"-e","@"+a[a.index("--configdir")+1]+"/globals.yml"]\n'
            'for j,v in enumerate(a):\n if v=="-e":cmd.extend(["-e",a[j+1]])\n'
            'raise SystemExit(subprocess.call(cmd))\n')
        self.binary.chmod(0o700)
        systemctl = self.binary.parent/'systemctl'
        systemctl.write_text('#!'+sys.executable+'\nimport sys\n'
            'action=sys.argv[1]\n'
            'if action=="is-active":print("inactive");raise SystemExit(3)\n'
            'if action=="is-enabled":print("disabled");raise SystemExit(1)\n'
            'if action!="disable":raise SystemExit("Unexpected systemd mutation: "+action)\n')
        systemctl.chmod(0o700)
        ansible_config = self.parent/'ansible.cfg'
        ansible_config.write_text('[defaults]\nhost_key_checking=False\n')
        self.env = dict(PATH=str(self.binary.parent)+':'+os.environ['PATH'],
                        ANSIBLE_CONFIG=str(ansible_config),ANSIBLE_NOCOLOR='1')

    def inventory_for(self, computes):
        self.inventory.write_text('[control]\ncontroller ansible_connection=local\n[network]\nnetwork1 ansible_connection=local\n'
            '[compute]\n'+'\n'.join('compute'+str(i)+' ansible_connection=local'+
                (' neutron_ovn_metadata_agent_image_full=mirror/metadata:approved' if i==2 else '')
                for i in range(1, computes+1))+
            '\n[neutron-ovn-metadata-agent:children]\nnetwork\ncompute\n')
        (self.root/'runtime.json').write_text(json.dumps(dict(run_id=self.root.name,inventory=str(self.inventory))))
        (self.root/'inventory.before').write_bytes(self.inventory.read_bytes())

    def execute(self):
        with patch.dict(os.environ,self.env):
            return run(self.root,self.binary,self.inventory,self.globals)

    def test_real_ansible_stages_all_metadata_hosts_and_preserves_host_overrides(self):
        self.inventory_for(2)
        original = {p.relative_to(self.source):p.read_bytes() for p in self.source.rglob('*') if p.is_file()}
        value = self.execute()
        self.assertEqual(set(value['hosts']), {'network1','compute1','compute2'})
        for host in value['hosts']:
            actual = json.loads((self.parent/'state'/(host+'.json')).read_text())
            self.assertEqual(actual['State']['Status'],'created')
            self.assertEqual(actual['native_options']['dimensions'],dict(mem_limit='1G'))
            self.assertIn('custom-volume:/extra:ro',actual['HostConfig']['Binds'])
        override = json.loads((self.parent/'state/compute2.json').read_text())
        self.assertEqual(override['Config']['Image'],'mirror/metadata:approved')
        self.assertFalse((self.parent/'state/ILLEGAL-ENABLE').exists())
        self.assertEqual({p.relative_to(self.source):p.read_bytes() for p in self.source.rglob('*') if p.is_file()},original)
        self.assertEqual(self.execute()['hosts'],value['hosts'])

    def test_preparation_is_blocked_after_freeze_without_invoking_kolla(self):
        self.inventory_for(2)
        (self.root/'metrics').mkdir()
        (self.root/'metrics/control_plane_downtime.start').touch()
        with self.assertRaisesRegex(ValueError,'after Neutron freeze'):
            self.execute()
        self.assertFalse((self.parent/'state').exists())

    def test_twenty_node_inventory_has_no_single_network_or_compute_assumption(self):
        self.inventory_for(18)
        value = self.execute()
        self.assertEqual(len(value['hosts']),19)
        self.assertIn('compute18',value['hosts'])
        self.assertNotIn('controller',value['hosts'])
        self.assertFalse((self.parent/'state/ILLEGAL-ENABLE').exists())

    def test_unsupported_native_handler_fails_before_hosts_are_touched(self):
        path=self.source/'roles/neutron/handlers/main.yml'
        path.write_text('[]\n')
        with self.assertRaisesRegex(ValueError,'Unsupported installed Kolla metadata handler'):
            prepare_tree(self.source,self.parent/'copy')
        self.assertEqual(path.read_text(),'[]\n')

    def test_preparation_failure_aborts_later_control_host_plays(self):
        self.inventory_for(2)
        source = yaml.safe_load((ROOT/'playbooks/06-target-config.yml').read_text())[0]
        sentinel = self.parent/'NEUTRON-MUST-NOT-STOP'
        entry = self.parent/'failure-gate.yml'
        entry.write_text(yaml.safe_dump([
            dict(name=source['name'],hosts=source['hosts'],gather_facts=False,
                 any_errors_fatal=source.get('any_errors_fatal',False),
                 tasks=[{'ansible.builtin.command':{'argv':['/bin/false']}}]),
            dict(name='Model later API freeze',hosts='control',gather_facts=False,
                 tasks=[{'ansible.builtin.file':{'path':str(sentinel),'state':'touch'}}])]))
        with patch.dict(os.environ,self.env):
            result = subprocess.run([sys.executable,'-m','ansible.cli.playbook','-i',str(self.inventory),str(entry)],
                                    capture_output=True,text=True,timeout=30)
        self.assertNotEqual(result.returncode,0)
        self.assertFalse(sentinel.exists(),result.stdout+result.stderr)


if __name__ == '__main__':
    unittest.main()
