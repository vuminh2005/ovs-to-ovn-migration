"""An included script is resolved from its task file in both reset and phase plays."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

from tests.support import ROOT


class IncludedHelperPathTests(unittest.TestCase):
    def test_openflow_helper_is_copied_and_runs_from_root_and_phase_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root/'bin'
            binary.mkdir()
            bridge = dict(headings=['_uuid', 'name', 'protocols'], data=[[
                ['uuid', 'existing-bridge'], 'br-int', ['set', ['OpenFlow10', 'OpenFlow13', 'OpenFlow15']]]])
            docker = binary/'docker'
            docker.write_text('#!'+sys.executable+'\nimport json,sys\n'
                'if "list" in sys.argv: print('+repr(json.dumps(bridge))+')\n'
                'elif "ovs-ofctl" in sys.argv: print("OF15 ready")\n'
                'else: raise SystemExit("unexpected command")\n')
            docker.chmod(0o700)
            env = dict(os.environ, PATH=str(binary)+os.pathsep+os.environ['PATH'])
            for name in ('reset-test.yml', 'playbooks/phase-test.yml'):
                with self.subTest(entry=name):
                    entry = root/name
                    entry.parent.mkdir(parents=True, exist_ok=True)
                    evidence = root/entry.stem
                    evidence.mkdir()
                    entry.write_text(yaml.safe_dump([dict(hosts='localhost', connection='local',
                        gather_facts=False, vars=dict(ansible_python_interpreter=sys.executable,
                            openflow_protocols='OpenFlow10,OpenFlow13,OpenFlow15', openflow_check_only=True,
                            openflow_evidence_directory=str(evidence)),
                        tasks=[{'ansible.builtin.include_tasks': str(ROOT/'playbooks/tasks/openflow-preparation-tasks.yml')}])],
                        sort_keys=False))
                    result = subprocess.run([sys.executable, '-m', 'ansible.cli.playbook', '-i',
                        'localhost,', str(entry)], cwd=root, env=env, capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
                    saved = json.loads((evidence/'openflow-preparation-localhost.json').read_text())
                    self.assertEqual(saved['status'], 'PASS')
                    self.assertFalse(saved['changed'])
                    self.assertEqual(saved['after']['uuid'], ['uuid', 'existing-bridge'])


if __name__ == '__main__':
    unittest.main()
