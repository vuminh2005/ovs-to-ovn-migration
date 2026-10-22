"""Explicit instance-quota changes preserve other limits and never lower quota."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock

import yaml

from tests.support import ROOT
from project_quota import ensure_instance_quota


class QuotaPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cloud = Mock()
        self.cloud.session.get_project_id.return_value = 'project'

    def test_default_target_changes_only_instances_and_verifies(self):
        self.cloud.compute.get_quota_set.side_effect = [NS(instances=10, cores=20, ram=51200),
                                                        NS(instances=20, cores=20, ram=51200)]
        result = ensure_instance_quota(self.cloud, self.root)
        self.cloud.compute.update_quota_set.assert_called_once_with('project', project_id='project', instances=20)
        self.assertEqual(result['status'], 'PASS'); self.assertTrue(result['changed'])
        self.assertEqual(result['after'], dict(instances=20, cores=20, ram=51200))
        path = self.root/'quota-instances.json'
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(path.read_text()), result)

    def test_custom_target_is_used(self):
        self.cloud.compute.get_quota_set.side_effect = [NS(instances=20, cores=20, ram=51200),
                                                        NS(instances=30, cores=20, ram=51200)]
        ensure_instance_quota(self.cloud, self.root, 30)
        self.cloud.compute.update_quota_set.assert_called_once_with('project', project_id='project', instances=30)

    def test_sufficient_higher_and_unlimited_quotas_are_not_updated(self):
        for instances in (20, 30, -1):
            with self.subTest(instances=instances):
                self.cloud.compute.get_quota_set.return_value = NS(instances=instances, cores=20, ram=51200)
                result = ensure_instance_quota(self.cloud, self.root)
                self.assertFalse(result['changed'])
                self.assertEqual(result['before'], result['after'])
                self.cloud.compute.update_quota_set.assert_not_called()

    def test_invalid_target_or_missing_project_never_updates(self):
        for target in (0, -1, True, 20.5, '20'):
            with self.subTest(target=target), self.assertRaises(ValueError):
                ensure_instance_quota(self.cloud, self.root, target)
        self.cloud.compute.get_quota_set.assert_not_called()
        self.cloud.session.get_project_id.return_value = None
        with self.assertRaisesRegex(RuntimeError, 'project-scoped'):
            ensure_instance_quota(self.cloud, self.root)
        self.cloud.compute.update_quota_set.assert_not_called()

    def test_changed_cores_or_ram_and_too_small_result_fail_verification(self):
        for after in (NS(instances=20, cores=21, ram=51200), NS(instances=20, cores=20, ram=51201),
                      NS(instances=19, cores=20, ram=51200)):
            with self.subTest(after=after):
                self.cloud.compute.get_quota_set.side_effect = [NS(instances=10, cores=20, ram=51200), after]
                with self.assertRaisesRegex(RuntimeError, 'verification failed'):
                    ensure_instance_quota(self.cloud, self.root)
                self.assertEqual(json.loads((self.root/'quota-instances.json').read_text())['status'], 'FAIL')

    def test_rejected_update_is_not_retried_or_marked_pass(self):
        self.cloud.compute.get_quota_set.return_value = NS(instances=10, cores=20, ram=51200)
        self.cloud.compute.update_quota_set.side_effect = PermissionError('fixture')
        with self.assertRaises(PermissionError):
            ensure_instance_quota(self.cloud, self.root)
        self.cloud.compute.update_quota_set.assert_called_once()
        evidence = json.loads((self.root/'quota-instances.json').read_text())
        self.assertEqual(evidence['status'], 'FAIL'); self.assertTrue(evidence['update_attempted'])


FAKE_SDK = '''import json,os
from pathlib import Path
from types import SimpleNamespace as NS
path=Path(os.environ['QUOTA_FIXTURE_STATE'])
def write(method,project,attrs=None):
 data=json.loads(path.read_text());data['calls'].append([method,project,attrs])
 if method=='PUT':
  assert set(attrs)=={'project_id','instances'} and attrs['project_id']==project
  data['quota']['instances']=attrs['instances']
 path.write_text(json.dumps(data));return NS(**data['quota'])
class Compute:
 def get_quota_set(self,project):return write('GET',project)
 def update_quota_set(self,project,**attrs):return write('PUT',project,attrs)
def connect():return NS(session=NS(get_project_id=lambda:'project'),compute=Compute())
'''


class QuotaAnsibleTests(unittest.TestCase):
    def test_actual_reset_and_migration_tasks_default_off_and_apply_requested_limits(self):
        reset = yaml.safe_load((ROOT/'reset-lab-to-ovs.yml').read_text())
        migration = yaml.safe_load((ROOT/'playbooks/04-validation-workloads.yml').read_text())
        entries = [
            ('reset.yml', next(t for p in reset for t in p.get('tasks', [])
                              if t['name']=='Increase instance quota only when explicitly enabled')),
            ('playbooks/phase.yml', next(t for p in migration for t in p.get('tasks', [])
                              if t['name']=='[04] Increase instance quota only when explicitly enabled'))]
        cases = [({}, 10), ({'increase_quota':True}, 20),
                 ({'increase_quota':True, 'instance_quota_limit':30}, 30)]
        for entry_name, task in entries:
            for extra, expected in cases:
                with self.subTest(entry=entry_name, extra=extra), tempfile.TemporaryDirectory() as directory:
                    home = Path(directory); scripts = home/'scripts/common'; scripts.mkdir(parents=True)
                    for name in ('project_quota.py','run_layout.py'):
                        shutil.copyfile(ROOT/'scripts/common'/name, scripts/name)
                    (scripts/'openstack.py').write_text(FAKE_SDK)
                    tasks = home/'playbooks/tasks'; tasks.parent.mkdir(exist_ok=True)
                    tasks.symlink_to(ROOT/'playbooks/tasks', target_is_directory=True)
                    state = home/'state.json'
                    state.write_text(json.dumps(dict(quota=dict(instances=10, cores=20, ram=51200),calls=[])))
                    openrc = home/'openrc'; openrc.write_text('export QUOTA_FIXTURE_STATE='+str(state)+'\n')
                    activate = home/'venv/bin/activate'; activate.parent.mkdir(parents=True); activate.touch()
                    evidence = home/'run'; evidence.mkdir()
                    entry = home/entry_name
                    entry.write_text(yaml.safe_dump([dict(hosts='localhost',connection='local',gather_facts=False,
                        vars_files=[str(ROOT/'group_vars/quota.yml')],
                        vars=dict(reset_kolla_venv=str(home/'venv'),reset_run_dir=str(evidence),
                                  migration_run_dir=str(evidence),openrc_path=str(openrc)),
                        tasks=[copy.deepcopy(task)])],sort_keys=False))
                    variables = dict(extra,quota_openrc=str(openrc),ansible_python_interpreter=sys.executable)
                    result = subprocess.run([sys.executable,'-m','ansible.cli.playbook','-i','localhost,',
                        str(entry),'-e',json.dumps(variables)],cwd=home,env=dict(os.environ,ANSIBLE_NOCOLOR='1'),
                        capture_output=True,text=True,timeout=30)
                    self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                    observed = json.loads(state.read_text())
                    self.assertEqual(observed['quota'],dict(instances=expected,cores=20,ram=51200))
                    self.assertEqual(sum(call[0]=='PUT' for call in observed['calls']), int(bool(extra)))
                    self.assertEqual((evidence/'quota-instances.json').exists(), bool(extra))


if __name__=='__main__': unittest.main()
