"""Recovered startup errors may pass; live missing/stuck quorum queues must not."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import reset_rabbitmq_health as health


QUEUES = 'name\ttype\tstate\tconsumers\nconductor\tquorum\trunning\t5\nscheduler\tquorum\trunning\t3\ncompute\tquorum\trunning\t2\nreply_uuid\tclassic\trunning\t1\n'
QUORUM = '''Status of quorum queue scheduler on node rabbit@controller ...
┌───────────────────┬────────────┬────────────┐
│ Node Name         │ Raft State │ Membership │
├───────────────────┼────────────┼────────────┤
│ rabbit@controller │ leader     │ voter      │
└───────────────────┴────────────┴────────────┘
'''
CAPTURED_QUORUM = json.loads((ROOT/'tests/fixtures/rabbitmq-quorum-terminal.json').read_text())


class Clock:
    def __init__(self):
        self.now = 0
    def monotonic(self):
        return self.now
    def sleep(self, duration):
        self.now += duration


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = self.root/'rabbit.log'
        self.log.write_text('old cluster_not_formed already_started\n')
        self.evidence = self.root/'health.json'

    def run_wait(self, events=None, prepare=None):
        clock = Clock()
        calls = []
        def snap(*args):
            index = len(calls)
            calls.append(index)
            if prepare:
                prepare(index)
            issues = events[index] if events and index < len(events) else []
            return dict(issues=list(issues), queues=health.parse_queues(QUEUES), checks={})
        with patch.object(health.time,'monotonic',clock.monotonic), patch.object(health.time,'sleep',clock.sleep), patch.object(health,'snapshot',side_effect=snap):
            return health.wait(self.evidence,self.log,timeout=20,stable_seconds=5,interval=2)

    def test_recovered_startup_errors_do_not_fail_healthy_live_state(self):
        result = self.run_wait()
        self.assertEqual(result['status'],'PASS')
        self.assertGreaterEqual(result['samples'][-1]['stable_seconds'],5)
        self.assertEqual(self.log.read_text(),'old cluster_not_formed already_started\n')
        self.assertEqual(json.loads(self.evidence.read_text()),result)
        self.assertEqual(self.evidence.stat().st_mode & 0o777,0o600)

    def test_recovery_must_be_stable_not_just_one_good_sample(self):
        result = self.run_wait([['scheduler absent'],[],['scheduler down'],[]])
        self.assertEqual(result['status'],'PASS')
        self.assertEqual(result['elapsed_seconds'],12)
        self.assertEqual(result['samples'][2]['stable_seconds'],0)

    def test_persistent_live_failure_is_saved_as_fail(self):
        result = self.run_wait([['scheduler absent']]*100)
        self.assertEqual(result['status'],'FAIL')
        self.assertEqual(result['elapsed_seconds'],20)
        self.assertEqual(json.loads(self.evidence.read_text())['status'],'FAIL')

    def test_new_raft_errors_reset_stability_even_if_current_sample_is_healthy(self):
        def error(index):
            if index==2:
                with self.log.open('a') as stream:
                    stream.write('new failed to form a new cluster scheduler\n')
        result = self.run_wait(prepare=error)
        self.assertEqual(result['status'],'PASS')
        self.assertEqual(result['elapsed_seconds'],12)
        self.assertIn('New quorum/Raft errors',result['samples'][2]['issues'][0])

    def test_continuing_new_raft_errors_never_pass(self):
        def error(index):
            with self.log.open('a') as stream:
                stream.write('new cluster_not_formed\n')
        self.assertEqual(self.run_wait(prepare=error)['status'],'FAIL')

    def test_missing_log_cannot_be_treated_as_quiet(self):
        self.log.unlink()
        self.assertEqual(self.run_wait()['status'],'FAIL')

    def test_log_rotation_resets_observation_instead_of_reusing_old_window(self):
        def rotate(index):
            if index==2:
                self.log.rename(self.root/'rabbit.log.1')
                self.log.write_text('new file\n')
        result = self.run_wait(prepare=rotate)
        self.assertEqual(result['status'],'PASS')
        self.assertEqual(result['elapsed_seconds'],12)
        self.assertEqual(result['samples'][2]['log']['continuity'],'ROTATED_OR_TRUNCATED')

    def test_old_pass_is_overwritten_before_an_interrupted_retry(self):
        health.save(self.evidence,{'status':'PASS'})
        with patch.object(health,'snapshot',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                health.wait(self.evidence,self.log,timeout=20,stable_seconds=5)
        self.assertEqual(json.loads(self.evidence.read_text())['status'],'RUNNING')

    def test_live_snapshot_requires_all_quorum_queues_and_required_consumers(self):
        def command(args,*_):
            return dict(rc=0,stdout=QUEUES if 'list_queues' in args else QUORUM,stderr='')
        with patch.object(health,'command',side_effect=command):
            row = health.snapshot(100,15,'rabbit@controller')
        self.assertFalse(row['issues'])
        for listing in (QUEUES.replace('scheduler\tquorum\trunning\t3\n',''),
                        QUEUES.replace('scheduler\tquorum\trunning\t3','scheduler\tquorum\trunning\t0'),
                        QUEUES.replace('compute\tquorum\trunning','compute\tquorum\tdown'),
                        QUEUES.replace('conductor\tquorum','conductor\tclassic')):
            self.assertTrue(health.queue_issues(health.parse_queues(listing)))

    def test_malformed_cli_output_does_not_pass(self):
        for output in ('','Error: not found','scheduler quorum running 3\nscheduler quorum running 3',
                       'scheduler quorum down NaN'):
            with self.subTest(output=output),self.assertRaises(ValueError):
                health.parse_queues(output)
        for output in ('',QUORUM.replace('leader','follower'),QUORUM.replace('rabbit@controller','rabbit@old-controller')):
            with self.subTest(output=output),self.assertRaises(ValueError):
                health.parse_quorum(output,'rabbit@controller')

    def test_real_captured_ansi_dec_member_rows_are_recognized(self):
        for name,output in CAPTURED_QUORUM.items():
            with self.subTest(capture=name):
                self.assertIn('\x1b(0x\x1b(B',output)
                self.assertEqual(health.parse_quorum(output,'rabbit@controller'),[
                    {'node':'rabbit@controller','state':'leader','membership':'voter'}])

    def test_ansi_styles_and_ascii_rules_keep_same_validation(self):
        output=QUORUM.replace('│','|').replace('leader','\x1b[1;32mleader\x1b[0m')
        self.assertEqual(health.parse_quorum(output,'rabbit@controller')[0]['state'],'leader')
        self.assertEqual(health.terminal_text('rabbit@extra-x'),'rabbit@extra-x')

    def test_captured_output_cannot_hide_a_follower_wrong_node_or_extra_member(self):
        for output in CAPTURED_QUORUM.values():
            for bad in (output.replace('leader','follower'),output.replace('voter','promotable'),
                        output.replace('rabbit@controller','rabbit@other'),output+output):
                with self.subTest(output=bad[:40]),self.assertRaises(ValueError):
                    health.parse_quorum(bad,'rabbit@controller')

    def test_deadline_caps_subprocess_and_cli_failure_is_retained(self):
        with patch.object(health.time,'monotonic',return_value=98), patch.object(health.subprocess,'run',return_value=subprocess.CompletedProcess([],7,'','failure')) as run:
            result=health.command(['rabbitmqctl','cluster_status'],100,15)
        self.assertEqual(run.call_args.kwargs['timeout'],2)
        self.assertEqual(result['rc'],7)
        self.assertEqual(result['stderr'],'failure')
        with patch.object(health.time,'monotonic',return_value=101),patch.object(health.subprocess,'run') as run:
            self.assertEqual(health.command([],100,15)['rc'],124)
            run.assert_not_called()
        with patch.object(health.subprocess,'run',side_effect=subprocess.TimeoutExpired('docker',1)):
            result=health.command([],health.time.monotonic()+10,1)
            self.assertEqual(result['rc'],124)
            self.assertEqual(result['timeout_scope'],'command')
        with patch.object(health.time,'monotonic',return_value=99.5),patch.object(health.subprocess,'run',side_effect=subprocess.TimeoutExpired('docker',.5)):
            result=health.command([],100,15)
            self.assertEqual(result['timeout_scope'],'overall')
            self.assertEqual(result['timeout_seconds'],.5)

    def test_invalid_timings_do_not_enter_polling_loop(self):
        for kwargs in ({'timeout':5,'stable_seconds':5},{'interval':0},{'timeout':float('nan')}):
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):
                health.wait(self.evidence,self.log,**kwargs)

    def test_storage_check_cannot_hide_a_failed_docker_daemon(self):
        plays=yaml.safe_load((ROOT/'reset-lab-to-ovs.yml').read_text())
        tasks=next(p['tasks'] for p in plays if p['name']=='Verify Kolla runtime residue after destroy')
        task=next(t for t in tasks if t['name']=='Check for residual RabbitMQ Docker volumes')
        shim=self.root/'docker'
        for rc,volumes in ((0,'kolla_logs\nrabbitmq\n'),(0,'kolla_logs\n'),(7,'')):
            shim.write_text('#!'+sys.executable+'\nimport sys\nprint('+repr(volumes)+',end="")\nsys.exit('+str(rc)+')\n')
            shim.chmod(0o700)
            env=dict(os.environ,PATH=str(self.root)+os.pathsep+os.environ['PATH'])
            result=subprocess.run(['/bin/bash','-c',task['ansible.builtin.shell']],
                                  env=env,capture_output=True,text=True)
            self.assertEqual(result.returncode,rc)
            self.assertEqual('rabbitmq' in result.stdout,'rabbitmq' in volumes)


@unittest.skipUnless(importlib.util.find_spec('ansible'), 'requires ansible-core')
class PlaybookIntegrationTests(unittest.TestCase):
    def test_real_ansible_accepts_recovered_startup_and_rejects_failed_deploy(self):
        plays=yaml.safe_load((ROOT/'reset-lab-to-ovs.yml').read_text())
        deploy=next(p for p in plays if p['name']=='Deploy clean source ML2 OVS cloud')
        names=('Wait for stable live RabbitMQ queues and a quiet Raft window',
               'Check new RabbitMQ log lines for quorum Raft failures',
               'Preserve deploy-time quorum errors as diagnostic evidence',
               'Fail when deploy or RabbitMQ validation failed')
        tasks=[copy.deepcopy(t) for t in deploy['tasks'] if t['name'] in names]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/'scripts').symlink_to(ROOT/'scripts',target_is_directory=True)
            log=root/'rabbit.log';log.write_text('earlier cluster_not_formed\n')
            shim=root/'docker'
            shim.write_text('#!'+sys.executable+'\nimport sys\nargs=sys.argv\n'
                +'print('+repr(QUEUES)+' if "list_queues" in args else '+repr(CAPTURED_QUORUM['sample1_conductor'])+')\n')
            shim.chmod(0o700)
            env=dict(os.environ,PATH=str(root)+os.pathsep+os.environ['PATH'],ANSIBLE_CONFIG=str(ROOT/'ansible.cfg'))
            for deploy_rc in (0,2):
                run=root/str(deploy_rc);run.mkdir()
                play=[dict(name='Production RabbitMQ gate',hosts='localhost',gather_facts=False,
                    vars=dict(reset_run_dir=str(run),reset_rabbitmq_log_path=str(log),
                        reset_rabbitmq_log_offset={'stdout':'0'},reset_deploy={'rc':deploy_rc},
                        reset_rabbitmq_health_timeout=15,reset_rabbitmq_stable_seconds=1,
                        ansible_python_interpreter=sys.executable),tasks=tasks)]
                path=root/'test.yml';path.write_text(yaml.safe_dump(play,sort_keys=False))
                result=subprocess.run([sys.executable,'-m','ansible.cli.playbook','-i','localhost,','-c','local',str(path)],
                    env=env,cwd=ROOT,capture_output=True,text=True,timeout=25)
                self.assertEqual(result.returncode==0,deploy_rc==0,result.stdout+result.stderr)
                self.assertEqual(json.loads((run/'rabbitmq-health.json').read_text())['status'],'PASS')
                self.assertIn('earlier cluster_not_formed',(run/'rabbitmq-deploy-raft-errors.log').read_text())


if __name__=='__main__':unittest.main()
