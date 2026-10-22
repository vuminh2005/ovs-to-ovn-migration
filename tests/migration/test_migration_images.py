"""Real Ansible policy/placement tests; Docker and registry are modeled."""
import hashlib
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
sys.path.insert(0, str(ROOT/'scripts'))
from prepare_migration_images import prepare_tree, kolla_tree, run

FAKE_MODULE = '''from ansible.module_utils.basic import AnsibleModule
import hashlib,json,os
from pathlib import Path
m=AnsibleModule(argument_spec=dict(action=dict(type='str'),image=dict(type='str'),common_options=dict(type='dict')))
image=m.params['image'];cache=Path(os.environ['IMAGE_CACHE']);path=cache/hashlib.sha256(image.encode()).hexdigest()
if m.params['action']=='ensure_image' and path.exists():m.exit_json(changed=False)
with open(cache/'pulls.log','a') as output:output.write(image+'\\n')
if os.environ.get('IMAGE_REGISTRY_MISSING')=='1':m.fail_json(msg='no such manifest: '+image)
path.write_text(image);m.exit_json(changed=True)
'''


class MigrationImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.parent=Path(self.tmp.name)
        self.binary=self.parent/'venv/bin/kolla-ansible';self.binary.parent.mkdir(parents=True)
        self.source=self.binary.parent.parent/'share/kolla-ansible/ansible';self.source.mkdir(parents=True)
        self.cache=self.parent/'cache';self.cache.mkdir()
        self.root=self.parent/'run';self.root.mkdir();(self.root/'runtime.json').write_text('{}')
        self.inventory=self.parent/'inventory'
        self.inventory.write_text('[control]\ncontroller ansible_connection=local\n[network]\nnetwork1 ansible_connection=local\n[compute]\ncompute1 ansible_connection=local\ncompute2 ansible_connection=local ovn_controller_image_full=mirror/ovn-controller:approved\n'
            '[ovn-northd:children]\ncontrol\n[ovn-nb-db:children]\ncontrol\n[ovn-sb-db:children]\ncontrol\n'
            '[ovn-controller:children]\nnetwork\ncompute\n[neutron-server:children]\ncontrol\n'
            '[neutron-ovn-metadata-agent:children]\nnetwork\ncompute\n')
        self.globals=self.parent/'globals.yml';self.globals.write_text('openstack_tag: 2024.1-ubuntu-noble\n')
        (self.source/'group_vars').mkdir()
        (self.source/'group_vars/all.yml').write_text('kolla_container_engine: docker\ndocker_common_options: {}\nservice_images_pull_retries: 0\nservice_images_pull_delay: 0\n')
        (self.source/'filter_plugins').mkdir()
        (self.source/'filter_plugins/services.py').write_text('from jinja2 import pass_context\n'
            '@pass_context\ndef selected(context,services):return {k:v for k,v in services.items() if v["enabled"] and v["group"] in context["group_names"]}\n'
            'class FilterModule:\n def filters(self):return {"select_services_enabled_and_mapped_to_host":selected}\n')
        (self.source/'library').mkdir();(self.source/'library/kolla_container.py').write_text(FAKE_MODULE)
        roles = [('ovn-db','ovn_db',['ovn-northd','ovn-nb-db','ovn-sb-db']),
                 ('ovn-controller','ovn_controller',['ovn-controller']),
                 ('neutron','neutron',['neutron-server','neutron-ovn-metadata-agent'])]
        plays=[];self.images=[]
        for role,prefix,services in roles:
            directory=self.source/'roles'/role
            for child in ('tasks','vars','defaults'):(directory/child).mkdir(parents=True)
            (directory/'vars/main.yml').write_text(yaml.safe_dump(dict(project_name=prefix)))
            defaults={prefix+'_services':{service:dict(enabled=True,group=service,image='{{ '+service.replace('-','_')+'_image_full }}') for service in services}}
            for service in services:
                defaults[service.replace('-','_')+'_image_full']='quay.io/openstack.kolla/'+service+':{{ openstack_tag }}'
                self.images.append('quay.io/openstack.kolla/'+service+':2024.1-ubuntu-noble')
            (directory/'defaults/main.yml').write_text(yaml.safe_dump(defaults))
            (directory/'tasks/main.yml').write_text('- include_role:\n    name: service-images-pull\n')
            plays.append(dict(hosts=':'.join(services),gather_facts=False,roles=[dict(role=role,tags=[role])]))
        (self.source/'site.yml').write_text(yaml.safe_dump(plays))
        pull=self.source/'roles/service-images-pull/tasks';pull.mkdir(parents=True)
        (pull/'main.yml').write_bytes((ROOT/'tests/fixtures/kolla-18.8/pull-images.yml').read_bytes())
        self.original={p.relative_to(self.source):p.read_bytes() for p in self.source.rglob('*') if p.is_file()}
        self.binary.write_text('#!'+sys.executable+'\nimport os,subprocess,sys\na=sys.argv[1:];p=a[a.index("--playbook")+1];i=a[a.index("-i")+1]\n'
            'cmd=[sys.executable,"-m","ansible.cli.playbook","-i",i,p,"--tags",a[a.index("--tags")+1],"-e","@"+a[a.index("--configdir")+1]+"/globals.yml"]\n'
            'for j,v in enumerate(a):\n if v=="-e":cmd.extend(["-e",a[j+1]])\n'
            'raise SystemExit(subprocess.call(cmd))\n')
        self.binary.chmod(0o700)
        docker=self.binary.parent/'docker'
        docker.write_text('#!'+sys.executable+'\nimport hashlib,json,os,sys\nfrom pathlib import Path\n'
            'image=sys.argv[-1];path=Path(os.environ["IMAGE_CACHE"])/hashlib.sha256(image.encode()).hexdigest()\n'
            'if not path.exists():raise SystemExit(1)\n'
            'print(json.dumps(dict(id="sha256:"+path.name,repo_tags=[image],repo_digests=[image+"@sha256:test"],architecture="amd64",os="linux")))\n')
        docker.chmod(0o700)
        config=self.parent/'ansible.cfg';config.write_text('[defaults]\nhost_key_checking=False\n')
        self.env=dict(IMAGE_CACHE=str(self.cache),PATH=str(self.binary.parent)+':'+os.environ['PATH'],
                      ANSIBLE_CONFIG=str(config),ANSIBLE_NOCOLOR='1')

    def cache_all(self):
        for image in self.images+['mirror/ovn-controller:approved']:
            (self.cache/hashlib.sha256(image.encode()).hexdigest()).write_text(image)

    def execute(self, registry_missing=True):
        with patch.dict(os.environ,dict(self.env,IMAGE_REGISTRY_MISSING='1' if registry_missing else '0')):
            return run(self.root,self.binary,self.inventory,self.globals)

    def test_cached_images_survive_registry_404_and_host_override_is_preserved(self):
        self.cache_all()
        rc=self.execute()
        evidence=next((self.root/'image-preflight').iterdir())
        log=(evidence/'kolla-images.log').read_text()
        self.assertEqual(rc,0,log)
        self.assertFalse((self.cache/'pulls.log').exists())
        self.assertEqual(len(list(evidence.glob('*.ovn_controller.json'))),3)
        self.assertEqual(json.loads((evidence/'compute2.ovn_controller.json').read_text())[0]['repo_tags'],['mirror/ovn-controller:approved'])
        self.assertEqual({p.relative_to(self.source):p.read_bytes() for p in self.source.rglob('*') if p.is_file()},self.original)

    def test_missing_controller_is_pulled_only_when_needed(self):
        self.cache_all()
        image='mirror/ovn-controller:approved'
        (self.cache/hashlib.sha256(image.encode()).hexdigest()).unlink()
        rc=self.execute(registry_missing=False)
        evidence=next((self.root/'image-preflight').iterdir())
        self.assertEqual(rc,0,(evidence/'kolla-images.log').read_text())
        self.assertEqual((self.cache/'pulls.log').read_text().splitlines(),[image])

    def test_missing_image_with_registry_404_fails_with_evidence(self):
        self.cache_all()
        image='mirror/ovn-controller:approved'
        (self.cache/hashlib.sha256(image.encode()).hexdigest()).unlink()
        self.assertNotEqual(self.execute(),0)
        evidence=next((self.root/'image-preflight').iterdir())
        self.assertEqual(json.loads((evidence/'result.json').read_text())['status'],'FAIL')
        self.assertIn('no such manifest',(evidence/'kolla-images.log').read_text())
        self.assertEqual((self.cache/'pulls.log').read_text().splitlines(),[image])

    def test_unknown_installed_task_layout_is_rejected_without_editing_installation(self):
        path=self.source/'roles/service-images-pull/tasks/main.yml';path.write_text('[]')
        with self.assertRaisesRegex(ValueError,'Unsupported Kolla'):
            prepare_tree(self.source,self.parent/'copy')
        self.assertEqual(path.read_text(),'[]')
        self.assertEqual(kolla_tree(self.binary),self.source)


if __name__=='__main__':unittest.main()
