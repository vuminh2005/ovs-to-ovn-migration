"""Exercise the actual topology shell check with output larger than pipe buffers."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import yaml

ROOT=Path(__file__).resolve().parents[1]


class TopologyPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        directory=Path(self.temp.name)
        docker=directory/'docker'
        docker.write_text('''#!/usr/bin/env python3
import os, signal, sys
signal.signal(signal.SIGPIPE, signal.SIG_DFL)
case=os.environ.get('TOPOLOGY_CASE','valid')
if 'ovn-nbctl' in sys.argv:
    first=b'switch abc\\n' if case!='no-nb' else b'empty logical database\\n'
    os.write(1,first)
    for i in range(2048):os.write(1,b'x'*8191+b'\\n')
    if case=='nb-error':sys.exit(7)
elif 'ovn-sbctl' in sys.argv:
    os.write(1,b'_uuid : datapath\\n' if case!='no-sb' else b'\\n')
else:sys.exit(9)
''')
        docker.chmod(0o700)
        self.env=dict(os.environ,PATH=str(directory)+':'+os.environ['PATH'])
        plays=yaml.safe_load((ROOT/'playbooks/07-migrate-db.yml').read_text())
        task=next(t for p in plays for t in p['tasks'] if t['name']=='[07] Check OVN logical topology and datapaths')
        self.shell=task['ansible.builtin.shell'].replace("{{ hostvars['localhost']['ovn_nb_connection_runtime'] }}",'tcp:controller:6641').replace("{{ hostvars['localhost']['ovn_sb_connection_runtime'] }}",'tcp:controller:6642')

    def run_check(self,case='valid',original=False):
        shell=self.shell.replace("grep -E 'switch|router' >/dev/null","grep -Eq 'switch|router'") if original else self.shell
        return subprocess.run(['bash','-c',shell],env=dict(self.env,TOPOLOGY_CASE=case),capture_output=True,text=True,timeout=20)

    def test_original_quiet_reader_reproduces_rc141(self):
        self.assertEqual(self.run_check(original=True).returncode,141)

    def test_fixed_reader_consumes_large_topology_and_passes(self):
        self.assertEqual(self.run_check().returncode,0)

    def test_real_nb_failure_is_not_ignored(self):
        self.assertEqual(self.run_check('nb-error').returncode,7)

    def test_missing_logical_topology_or_datapaths_is_rejected(self):
        for case in ('no-nb','no-sb'):
            with self.subTest(case=case):self.assertNotEqual(self.run_check(case).returncode,0)


if __name__=='__main__':unittest.main()
