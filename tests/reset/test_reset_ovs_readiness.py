"""Source OVS readiness must precede additive OpenFlow and workload creation."""
import copy
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
import reset_ovs_readiness as readiness


class Clock:
    def __init__(self):
        self.now = 0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def inspect_rows(stopped=None):
    return [dict(Name='/'+name, RestartCount=0, State=dict(
        Status='exited' if name == stopped else 'running', Running=name != stopped,
        Restarting=False, ExitCode=1 if name == stopped else 0),
        Config=dict(Env=['PASSWORD=DO_NOT_COPY'])) for name in readiness.CONTAINERS]


class OVSReadinessTests(unittest.TestCase):
    def test_ready_sample_is_read_only_and_does_not_export_container_environment(self):
        responses = [subprocess.CompletedProcess([], 0, json.dumps(inspect_rows()), ''),
                     subprocess.CompletedProcess([], 0, '', '')]
        with patch.object(readiness, 'command', side_effect=responses) as command:
            value = readiness.sample(100)
        self.assertTrue(value['ready'])
        self.assertEqual(len(command.call_args_list), 2)
        self.assertIn('br-exists', command.call_args_list[1].args[0])
        self.assertNotIn('DO_NOT_COPY', json.dumps(value))

    def test_stopped_agent_cannot_pass_even_if_a_stale_bridge_exists(self):
        response = subprocess.CompletedProcess([], 0, json.dumps(inspect_rows('neutron_openvswitch_agent')), '')
        with patch.object(readiness, 'command', return_value=response) as command:
            value = readiness.sample(100)
        self.assertFalse(value['ready'])
        self.assertEqual(value['containers']['neutron_openvswitch_agent']['status'], 'exited')
        self.assertEqual(command.call_count, 1)

    def test_delayed_bridge_is_polled_without_restarting_or_creating_anything(self):
        clock = Clock()
        with patch.object(readiness, 'time', clock), patch.object(readiness, 'sample', side_effect=[
                dict(ready=False, reason='bridge absent', containers={}),
                dict(ready=True, reason='ready', containers={})]):
            value = readiness.wait(timeout=10, interval=2)
        self.assertEqual(value['status'], 'PASS')
        self.assertEqual(value['attempts'], 2)
        self.assertEqual(value['elapsed_seconds'], 2)

    def test_missing_bridge_hits_deadline_and_never_reports_pass(self):
        clock = Clock()
        with patch.object(readiness, 'time', clock), patch.object(readiness, 'sample', return_value=
                dict(ready=False, reason='bridge absent', containers={})):
            value = readiness.wait(timeout=5, interval=2)
        self.assertEqual(value['status'], 'FAIL')
        self.assertEqual(value['elapsed_seconds'], 5)
        self.assertEqual(value['attempts'], 3)

    def test_hung_docker_command_is_bounded_by_remaining_deadline(self):
        clock = Clock()
        def hung(args, **kwargs):
            self.assertLessEqual(kwargs['timeout'], 3)
            clock.sleep(kwargs['timeout'])
            raise subprocess.TimeoutExpired(args, kwargs['timeout'])
        with patch.object(readiness, 'time', clock), patch.object(readiness.subprocess, 'run', side_effect=hung):
            value = readiness.wait(timeout=3, interval=2)
        self.assertEqual(value['status'], 'FAIL')
        self.assertEqual(value['attempts'], 1)
        self.assertEqual(value['elapsed_seconds'], 3)


DOCKER = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
host=os.environ['OVS_FIXTURE_HOST']
root=Path(os.environ['OVS_FIXTURE_ROOT'])
path=root/(host+'.json')
state=json.loads(path.read_text())
args=sys.argv[1:]
with (root/(host+'.calls')).open('a') as output:output.write(json.dumps(args)+'\\n')
def save():path.write_text(json.dumps(state))
if args[:2]==['container','inspect']:
    state['inspects']+=1;save()
    print(json.dumps([dict(Name='/'+name,RestartCount=0,State=dict(Status='running',Running=True,Restarting=False,ExitCode=0)) for name in args[2:]]))
elif args[0]=='inspect':print('fixture container running')
elif args[0]=='logs':print('FIXTURE_PRIVATE_STARTUP_LOG')
elif args[:3]==['exec','openvswitch_vswitchd','ovs-vsctl']:
    available=not state['missing'] and state['inspects']>=state['delay']
    if 'br-exists' in args:raise SystemExit(0 if available else 2)
    if 'show' in args:print('fixture OVS diagnostics')
    elif 'list' in args:
        if not available:raise SystemExit(9)
        print(json.dumps(dict(headings=['_uuid','name','protocols'],data=[[['uuid',host+'-bridge'],'br-int',['set',state['protocols']]]])))
    elif 'set' in args:
        if not available:raise SystemExit(9)
        state['protocols']=args[-1].split('=',1)[1].split(',');save()
    else:raise SystemExit(8)
elif args[:3]==['exec','openvswitch_vswitchd','ovs-ofctl']:print('fixture OpenFlow15 bridge')
else:raise SystemExit(7)
'''


class OVSReadinessAnsibleTests(unittest.TestCase):
    def execute(self, missing=False):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        home = Path(self.temp.name)
        (home/'scripts').symlink_to(ROOT/'scripts', target_is_directory=True)
        (home/'playbooks').symlink_to(ROOT/'playbooks', target_is_directory=True)
        (home/'bin').mkdir()
        docker = home/'bin/docker'
        docker.write_text(DOCKER)
        docker.chmod(0o700)
        inventory = home/'inventory'
        inventory.write_text('[all:vars]\nansible_connection=local\nansible_python_interpreter='+sys.executable+
            '\n[control]\ncontroller\n[network]\nnetwork1\n[compute]\ncompute1\ncompute2\n')
        hosts = ('network1', 'compute1', 'compute2')
        for host in hosts:
            (home/(host+'.json')).write_text(json.dumps(dict(inspects=0, protocols=['OpenFlow10'],
                missing=missing and host=='network1', delay=2 if host=='compute1' else 1)))
        evidence = home/'evidence'
        evidence.mkdir()
        source = next(p for p in yaml.safe_load((ROOT/'reset-lab-to-ovs.yml').read_text())
                      if p['name']=='Verify source ML2 OVS service containers')
        source = copy.deepcopy(source)
        source['become'] = False
        source['vars_files'] = [str(ROOT/file) for file in source['vars_files']]
        source['environment'] = dict(PATH=str(home/'bin')+':'+os.environ['PATH'],
            OVS_FIXTURE_HOST='{{ inventory_hostname }}', OVS_FIXTURE_ROOT=str(home))
        # Run actual readiness/rescue and OpenFlow tasks, without unrelated live config checks.
        source['tasks'] = source['tasks'][:2]
        source['tasks'][0]['block'][0]['ansible.builtin.script'] = (
            'scripts/reset/reset_ovs_readiness.py --timeout '+('1' if missing else '5')+' --interval .02')
        reached = home/'WORKLOAD-MUST-NOT-START'
        entry = home/'verify.yml'
        entry.write_text(yaml.safe_dump([
            dict(hosts='localhost',gather_facts=False,tasks=[{'ansible.builtin.set_fact':dict(reset_run_dir=str(evidence))}]),
            source,
            dict(hosts='localhost',gather_facts=False,tasks=[{'ansible.builtin.file':dict(path=str(reached),state='touch')}])
        ], sort_keys=False))
        result = subprocess.run([sys.executable, '-m', 'ansible.cli.playbook', '-i', str(inventory), str(entry)],
            cwd=home, env=dict(os.environ, ANSIBLE_NOCOLOR='1'), capture_output=True, text=True, timeout=40)
        return home, evidence, reached, result

    def test_actual_reset_waits_on_all_hosts_before_adding_openflow(self):
        home, evidence, reached, result = self.execute()
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertTrue(reached.exists())
        for host in ('network1', 'compute1', 'compute2'):
            ready = json.loads((evidence/('ovs-readiness-'+host+'.json')).read_text())
            self.assertEqual(ready['status'], 'PASS')
            protocols = json.loads((home/(host+'.json')).read_text())['protocols']
            self.assertIn('OpenFlow15', protocols)
            calls = [json.loads(row) for row in (home/(host+'.calls')).read_text().splitlines()]
            self.assertFalse(any('add-br' in args or 'del-br' in args for args in calls))
            self.assertLess(next(i for i,args in enumerate(calls) if 'br-exists' in args),
                            next(i for i,args in enumerate(calls) if 'set' in args))
        delayed = json.loads((evidence/'ovs-readiness-compute1.json').read_text())
        self.assertGreaterEqual(delayed['attempts'], 2)

    def test_actual_reset_archives_failure_and_blocks_openflow_and_later_workloads(self):
        home, evidence, reached, result = self.execute(missing=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Stop reset when the source OVS agent or br-int is not ready', result.stdout)
        self.assertFalse(reached.exists())
        self.assertNotIn('FIXTURE_PRIVATE_STARTUP_LOG', result.stdout)
        diagnostics = evidence/'ovs-readiness-network1.log'
        self.assertIn('FIXTURE_PRIVATE_STARTUP_LOG', diagnostics.read_text())
        self.assertEqual(diagnostics.stat().st_mode & 0o777, 0o600)
        for host in ('network1', 'compute1', 'compute2'):
            calls = [json.loads(row) for row in (home/(host+'.calls')).read_text().splitlines()]
            self.assertFalse(any('set' in args for args in calls), host)


if __name__ == '__main__':
    unittest.main()
