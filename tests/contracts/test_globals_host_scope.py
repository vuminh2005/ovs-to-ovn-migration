"""Execute the production assertion with vars_files and implicit localhost."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import yaml

from tests.support import ROOT


class GlobalsHostScopeTests(unittest.TestCase):
    def run_assertion(self, bad_external=False):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            globals_path=root/'globals.yml'
            # Jinja is rendered before YAML parsing; retain the double-quoted
            # representation emitted by the actual reset globals task.
            external='wrong-ex' if bad_external else "{{ 'ns-ex' if inventory_hostname in groups['network'] else 'neutron-ext' }}"
            globals_path.write_text('tunnel_interface: "{{ \'br-underlay\' if inventory_hostname in groups[\'network\'] else \'eth1\' }}"\n'
                                    'neutron_external_interface: "'+external+'"\n')
            inventory=root/'inventory.ini'
            inventory.write_text('[control]\ncontroller ansible_connection=local\n[network]\nnetwork1 ansible_connection=local\n[compute]\ncompute1 ansible_connection=local\ncompute2 ansible_connection=local\n')
            production=yaml.safe_load((ROOT/'playbooks/02-precheck.yml').read_text())
            source=next(p for p in production if p['name']=='Phase 02 - Check source hosts and OVS networking')
            task=next(t for t in source['tasks'] if t['name']=='[02] Resolve effective tunnel interface per host')
            vars_files=[str(ROOT/'group_vars/all.yml'),str(ROOT/'group_vars/ns-lab.yml')]
            plays=[dict(name='Prove play variable is absent from localhost hostvars',hosts='localhost',gather_facts=False,
                        vars_files=vars_files,tasks=[{'ansible.builtin.assert':{'that':["'kolla_globals_path' not in hostvars['localhost']"]}}]),
                   dict(name='Execute production per-host assertion',hosts='control:network:compute',gather_facts=False,
                        vars_files=vars_files,vars={**source['vars'], 'migration_external_interface': 'ns-ex'},tasks=[task])]
            playbook=root/'check.yml';playbook.write_text(yaml.safe_dump(plays,sort_keys=False))
            env=dict(os.environ,KOLLA_GLOBALS=str(globals_path),ANSIBLE_NOCOLOR='1')
            result=subprocess.run([sys.executable,'-m','ansible.cli.playbook','-i',str(inventory),str(playbook)],
                                  capture_output=True,text=True,env=env,timeout=60)
            return result.returncode,result.stdout+result.stderr

    def test_real_ansible_vars_files_are_resolved_for_each_chassis(self):
        rc,output=self.run_assertion()
        self.assertEqual(rc,0,output)
        for host in ('network1','compute1','compute2'):
            self.assertIn('ok: ['+host+']',output)
        self.assertIn('skipping: [controller]',output)

    def test_wrong_network_external_interface_is_still_rejected(self):
        rc,output=self.run_assertion(bad_external=True)
        self.assertNotEqual(rc,0)
        self.assertIn('fatal: [network1]',output)
        self.assertIn('Assertion failed',output)
        self.assertNotIn('template error',output)
        self.assertNotIn("has no attribute 'kolla_globals_path'",output)


if __name__=='__main__':unittest.main()
