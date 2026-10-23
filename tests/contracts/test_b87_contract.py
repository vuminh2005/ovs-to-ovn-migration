"""Display/storage contracts, with optional early scheduling before API start."""
from pathlib import Path
import re
import unittest
import yaml
from tests.validation.test_validation import ROOT


class B87ContractTests(unittest.TestCase):
    def test_file_persistence_tasks_use_save_but_variable_assignments_do_not(self):
        for path in (ROOT/'playbooks').rglob('*.yml'):
            if path.name.startswith('reset-'): continue
            data=yaml.safe_load(path.read_text())
            for play in data:
                for task in play.get('tasks',[play] if 'hosts' not in play else []):
                    name=task.get('name','')
                    with self.subTest(file=path.name,task=name):
                        if 'ansible.builtin.set_fact' in task:
                            self.assertNotIn('[SAVE]',name)
                        if any(k in task for k in ('ansible.builtin.copy','ansible.builtin.fetch','ansible.builtin.template')):
                            self.assertIn('[SAVE]',name)

    def test_display_names_and_phase_titles_keep_the_requested_contract(self):
        names=[]
        for path in (ROOT/'playbooks').rglob('*.yml'):
            data=yaml.safe_load(path.read_text())
            for play in data:
                if re.match(r'\d\d-',path.name):
                    self.assertRegex(play['name'],r'^Phase \d\d - ')
                for task in play.get('tasks',[play] if 'hosts' not in play else []):
                    name=task.get('name',''); names.append(name)
                    self.assertNotRegex(name,r'\b(?:Pair|PAIR) [ABCD]\b')
        self.assertIn('[02] Validate workload topology and TCP test settings',names)
        self.assertIn('[07] Switch router service backend associations to OVN',names)
        self.assertIn('[08] Verify prepared OpenFlow support on br-int',names)

    def test_gateway_optimization_adds_no_full_sync_or_blocking_readiness_gate(self):
        self.assertFalse(list((ROOT/'scripts').rglob('gateway_readiness.py')))
        for name in ('playbooks/00-bootstrap.yml','playbooks/07-migrate-db.yml',
                     'playbooks/10-restore-neutron.yml','group_vars/all.yml','scripts/reporting/migration_report.py'):
            text=(ROOT/name).read_text()
            self.assertNotIn('gateway_sync',text,name)
            self.assertNotIn('gateway-readiness',text,name)
        phase = yaml.safe_load((ROOT/'playbooks/10-restore-neutron.yml').read_text())
        optional = next(p for p in phase if 'Attempt early gateway scheduling' in p['name'])
        block = next(t for t in optional['tasks'] if 'block' in t)
        self.assertTrue(block['rescue'])
        self.assertTrue(block['rescue'][0]['failed_when'] is False)
        self.assertLess(phase.index(optional), next(i for i,p in enumerate(phase)
            if 'Start Neutron API workers' in p['name']))


if __name__=='__main__': unittest.main()
