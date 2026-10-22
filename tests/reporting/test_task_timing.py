"""Exercise the callback through real Ansible, including child processes/resume."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import yaml


from tests.support import ROOT
HAS_ANSIBLE = importlib.util.find_spec('ansible') is not None


@unittest.skipUnless(HAS_ANSIBLE, 'requires ansible-core for callback integration')
class TaskTimingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.run = self.work / 'backup' / 'run'
        self.env = {k:v for k,v in os.environ.items()
                    if not k.startswith('OVS_TO_OVN_TASK_TIMING_')}
        self.env.update(ANSIBLE_CONFIG=str(ROOT/'ansible.cfg'),
                        ANSIBLE_NOCOLOR='1', ANSIBLE_HOST_KEY_CHECKING='False')
        self.inventory = self.work/'inventory.ini'
        self.inventory.write_text('[local]\nalpha ansible_connection=local\nbeta ansible_connection=local\n'
            '[offline]\nabsent ansible_connection=ssh ansible_host=127.0.0.1 ansible_port=1 ansible_ssh_timeout=1\n'
            '[all:vars]\nansible_python_interpreter=' + sys.executable + '\n')

    def write_play(self, name, plays):
        path = self.work/name
        path.write_text(yaml.safe_dump(plays, sort_keys=False))
        return path

    def command(self, play):
        return [sys.executable, '-m', 'ansible.cli.playbook', '-i', str(self.inventory), str(play)]

    def read_reports(self):
        return [json.loads(p.read_text()) for p in (self.run/'task-timings').glob('*.json')]

    def test_hosts_statuses_privacy_nested_playbook_and_resume(self):
        child = self.write_play('child.yml', [dict(name='Child', hosts='localhost', gather_facts=False,
            tasks=[{'name':'Child task', 'ansible.builtin.debug':{'msg':'secret-child-output'}}])])
        included = self.work/'included.yml'
        included.write_text(yaml.safe_dump([
            {'name':'Skipped task','ansible.builtin.debug':{'msg':'unused'}, 'when':False},
            {'name':'Loop task','ansible.builtin.debug':{'msg':'{{ item }}'},
             'loop':['secret-loop-one','secret-loop-two']},
        ], sort_keys=False))
        main = self.write_play('main.yml', [dict(name='Main', hosts='local', gather_facts=False,
            become=True, tasks=[
                {'name':'Buffered bootstrap', 'ansible.builtin.debug':{'msg':'secret-before-root'}},
                {'name':'Resolve run', 'ansible.builtin.set_fact':{'migration_run_dir':str(self.run)}},
                {'name':'Protected secret-name', 'ansible.builtin.debug':{'msg':'secret-no-log'}, 'no_log':True},
                # An unnamed command must not use get_name() (which includes argv).
                {'ansible.builtin.command':{'argv':[sys.executable,'-c',"print('secret-command-argument')"]}},
                {'name':'Parallel execution', 'ansible.builtin.command':{'argv':[
                    sys.executable,'-c','import time; time.sleep(0.15)']}},
                {'name':'Include tasks','ansible.builtin.include_tasks':str(included)},
                {'name':'Delegated task','ansible.builtin.debug':{'msg':'secret-delegate-output'}, 'delegate_to':'localhost'},
                {'name':'Notify handler','ansible.builtin.debug':{'msg':'notice'},'changed_when':True, 'notify':'Handler task'},
                {'name':'Flush handlers','ansible.builtin.meta':'flush_handlers'},
                {'name':'Child invocation','ansible.builtin.command':{'argv':[
                    sys.executable,'-m','ansible.cli.playbook','-i','localhost,','-c','local',str(child)],
                    'chdir':str(self.work)},'run_once':True},
                {'name':'Ignored failure','ansible.builtin.command':{'argv':[sys.executable,'-c','raise SystemExit(9)']},'ignore_errors':True},
                {'name':'Fatal task','ansible.builtin.fail':{'msg':'secret-fatal-output'},'when':"inventory_hostname == 'beta'"},
            ], handlers=[{'name':'Handler task','ansible.builtin.debug':{'msg':'secret-handler-output'}}]),
            dict(name='Unreachable host',hosts='offline',gather_facts=False,
                 tasks=[{'name':'Unreachable task','ansible.builtin.ping':{}}])])
        for _ in range(2):  # Same run, distinct attempts; nothing is overwritten.
            completed = subprocess.run(self.command(main), cwd=ROOT, env=self.env,
                capture_output=True, text=True, timeout=45)
            self.assertNotEqual(completed.returncode, 0, completed.stdout)
            self.assertNotIn('Failure using method', completed.stderr)
            self.assertNotIn('Task timing could not', completed.stderr)
        reports = self.read_reports()
        self.assertEqual(len(reports), 4, completed.stdout + completed.stderr)
        parents = [r for r in reports if not r['parent_invocation_id']]
        children = [r for r in reports if r['parent_invocation_id']]
        self.assertEqual(len(parents), 2)
        self.assertEqual(len(children), 2)
        self.assertEqual({r['parent_invocation_id'] for r in children},
                         {r['invocation_id'] for r in parents})
        for report in parents:
            rows = report['tasks']
            self.assertFalse(report['persistence_errors'])
            self.assertEqual({r['host'] for r in rows if r['task_name']=='Buffered bootstrap'}, {'alpha','beta'})
            self.assertEqual({r['status'] for r in rows if r['task_name']=='Skipped task'}, {'SKIPPED'})
            failed = [r for r in rows if r['task_name']=='Ignored failure']
            self.assertEqual(len(failed), 2)
            self.assertTrue(all(r['status']=='FAILED' and r['ignored'] for r in failed))
            self.assertTrue(any(r['status']=='FAILED' and not r['ignored'] for r in rows))
            self.assertTrue(any(r['status']=='UNREACHABLE' for r in rows))
            self.assertTrue(any(r['handler'] and r['task_name']=='Handler task' for r in rows))
            self.assertTrue(any(r.get('delegated_to')=='localhost' for r in rows))
            self.assertEqual(len([r for r in rows if r['task_name']=='Loop task']), 2)
            for row in rows:
                self.assertIsNotNone(row['started_at'], row)
                self.assertIsNotNone(row['ended_at'], row)
                self.assertGreaterEqual(row['duration_seconds'], 0)
                self.assertEqual(row['duration_seconds'], row['ended_at']['monotonic']-row['started_at']['monotonic'])
                self.assertIn('+00:00', row['started_at']['utc'])
        saved = '\n'.join(p.read_text() for p in (self.run/'task-timings').iterdir())
        for secret in ('secret-before-root', 'Protected secret-name', 'secret-no-log',
                       'secret-command-argument', 'secret-child-output', 'secret-fatal-output',
                       'secret-delegate-output', 'secret-loop-one', 'secret-loop-two', 'secret-handler-output'):
            self.assertNotIn(secret, saved)
        for report in reports:
            journal = self.run/'task-timings'/(report['invocation_id']+'.jsonl')
            events = [json.loads(line) for line in journal.read_text().splitlines()]
            self.assertEqual(events[0]['event'], 'invocation_start')
            self.assertEqual(events[-1]['event'], 'invocation_end')
            self.assertEqual(len([e for e in events if e['event']=='task_end']), len(report['tasks']))
            self.assertEqual(journal.stat().st_mode & 0o777, 0o600)

    def test_killed_invocation_keeps_task_start_without_fabricating_end(self):
        play = self.write_play('interrupted.yml',[dict(name='Interrupted',hosts='alpha',gather_facts=False,
            tasks=[{'name':'Resolve run','ansible.builtin.set_fact':{'migration_run_dir':str(self.run)}},
                   {'name':'In progress','ansible.builtin.command':{'argv':[
                       sys.executable,'-c','import time; time.sleep(30)']}}])])
        process = subprocess.Popen(self.command(play), cwd=ROOT, env=self.env,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
        try:
            deadline = time.monotonic()+15
            found = False
            while time.monotonic() < deadline and process.poll() is None:
                for path in (self.run/'task-timings').glob('*.jsonl'):
                    events = [json.loads(line) for line in path.read_text().splitlines()]
                    found = any(e['event']=='task_start' and e['task_name']=='In progress' for e in events)
                if found:
                    break
                time.sleep(.05)
            self.assertTrue(found, 'callback did not persist the task start')
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
            process.communicate(timeout=10)
        self.assertEqual(self.read_reports(), [])
        events = [json.loads(line) for path in (self.run/'task-timings').glob('*.jsonl')
                  for line in path.read_text().splitlines()]
        self.assertFalse(any(e['event']=='task_end' and e['task_name']=='In progress' for e in events))


if __name__ == '__main__':
    unittest.main()
