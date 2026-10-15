"""Collector lifecycle with real processes, rotation, schema adaptation and failure evidence."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from test_validation import ROOT
import migration_diagnostics as d
from diagnostics_control import archived_stopped, summarize


class CollectorProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root/'observer'
        self.remote.mkdir()
        shutil.copy2(ROOT/'scripts/migration_diagnostics.py', self.remote/'migration_diagnostics.py')
        binary = self.root/'bin'
        binary.mkdir()
        fake = binary/'docker'
        fake.write_text('#!'+sys.executable+'\nimport signal,sys,time\n'
            'if len(sys.argv)>1 and sys.argv[1]=="events":\n'
            ' print("{\\"event\\":\\"start\\"}",flush=True)\n'
            ' while True: time.sleep(.1)\n'
            'raise SystemExit(0)\n')
        fake.chmod(0o700)
        self.env = dict(os.environ, PATH=str(binary)+os.pathsep+os.environ['PATH'])
        self.cfg = dict(run='run-one', host='controller', role='core', lifetime=60,
            max_bytes=1048576, chassis=False, network=False, control=False, ns_enabled=False)
        self.script = self.remote/'migration_diagnostics.py'
        self.started = False
        self.addCleanup(self.stop)

    def cli(self, action, *extra, check=True):
        result = subprocess.run([sys.executable, str(self.script), action, str(self.remote), *extra],
            capture_output=True, text=True, env=self.env, timeout=30)
        if check:
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            return json.loads(result.stdout)
        return result

    def stop(self):
        if self.started:
            self.cli('stop', check=False)

    def test_start_check_snapshot_stop_is_idempotent_and_preserves_unrelated_process(self):
        sentinel = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'])
        try:
            first = self.cli('start', '--config', json.dumps(self.cfg))
            self.started = True
            again = self.cli('start', '--config', json.dumps(self.cfg))
            self.assertEqual(first['identity'], again['identity'])
            self.cli('snapshot', '--label', 'pre-freeze')
            time.sleep(.6)
            self.assertIsNone(sentinel.poll())
            final = self.cli('stop')
            self.assertEqual(final['status'], 'STOPPED', final)
            self.assertTrue(final['stopped'])
            self.assertFalse(d.alive(first['identity']))
            self.assertTrue(all(v['rc'] is not None for v in final['jobs'].values()))
            self.assertEqual(self.cli('stop')['identity'], final['identity'])
            self.assertIn('observer-start', (self.remote/'events.jsonl').read_text())
            self.assertIsNone(sentinel.poll())
        finally:
            sentinel.terminate()
            sentinel.wait(timeout=3)

    def test_changed_owner_or_unjournaled_evidence_cannot_be_overwritten(self):
        (self.remote/'events.jsonl').write_text('old evidence')
        result = self.cli('start', '--config', json.dumps(self.cfg), check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.remote/'events.jsonl').read_text(), 'old evidence')


class CollectorEvidenceTests(unittest.TestCase):
    def test_log_rotation_preserves_unread_tail_and_distinguishes_new_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            log = root/'neutron-server.log'
            log.write_text('before-run\n')
            observer = d.Observer(root, dict(run='r', host='controller'))
            with patch.object(d.glob, 'glob', return_value=[str(log)]):
                observer.tail_logs()
            log.write_text('before-run\nstartup-sync\n')
            rotated = root/'neutron-server.log.1'
            log.rename(rotated)
            log.write_text('gateway-scheduled\n')
            with patch.object(d.glob, 'glob', return_value=[str(rotated),str(log)]):
                observer.tail_logs()
            events = [json.loads(s) for s in observer.events.read_text().splitlines()]
            text = ''.join(e.get('text','') for e in events)
            self.assertEqual(text.count('startup-sync'), 1)
            self.assertEqual(text.count('gateway-scheduled'), 1)
            self.assertNotIn('before-run', text)

    def test_database_monitor_selects_only_tables_and_columns_in_runtime_schema(self):
        schema = dict(tables={
            'Logical_Router_Port': dict(columns={'name':{}, 'gateway_chassis':{}}),
            'Gateway_Chassis': dict(columns={'name':{}, 'chassis_name':{}, 'priority':{}}),
            'Port_Binding': dict(columns={'logical_port':{}, 'chassis':{}, 'up':{}})})
        with patch.object(d, 'command', return_value=dict(status='PASS', stdout=json.dumps(schema))):
            jobs, _ = d.database_jobs(dict(nb='tcp:127.0.0.1:6641',sb='tcp:127.0.0.1:6642'))
        lrp = jobs['nb-Logical_Router_Port']['args']
        self.assertIn('gateway_chassis', lrp[-1])
        self.assertNotIn('ha_chassis_group', lrp[-1])
        for value in jobs.values():
            self.assertIn('monitor', value['args'])
            self.assertNotIn('transact', value['args'])

    def test_archived_stop_needs_original_config_and_no_remote_access(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            entry = dict(host='network1',role='core',config={'run':'r'})
            dest = root/'diagnostics/network1/core'
            dest.mkdir(parents=True)
            d.save(dest/'state.json',dict(stopped=True,config=entry['config']))
            self.assertTrue(archived_stopped(root,entry))
            self.assertFalse(archived_stopped(root,dict(entry,config={'run':'other'})))

    def test_missing_central_archive_or_dead_observer_cannot_be_reported_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            d.save(root/'diagnostics-config.json', dict(max_bytes=1048576,lifetime=60))
            checkpoints = root/'diagnostics/checkpoints'
            checkpoints.mkdir(parents=True)
            d.save(checkpoints/'controller-core.json', dict(host='controller', role='core'))
            value = summarize(root)
            self.assertEqual(value['status'], 'UNAVAILABLE')
            self.assertEqual(value['collectors'][0]['status'], 'UNAVAILABLE')


class AnsibleCollectorTests(unittest.TestCase):
    def test_coordinator_stages_stops_and_fetches_a_local_inventory_host(self):
        from diagnostics_control import operate
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)/'run'
            root.mkdir()
            inventory = root/'inventory.ini'
            inventory.write_text('[control]\nalpha ansible_connection=local ansible_python_interpreter='+sys.executable+'\n')
            binary = root/'bin'
            binary.mkdir()
            fake = binary/'docker'
            fake.write_text('#!'+sys.executable+'\nimport sys,time\n'
                'if len(sys.argv)>1 and sys.argv[1]=="events":\n'
                ' print("ready",flush=True)\n'
                ' while True: time.sleep(.1)\n')
            fake.chmod(0o700)
            cfg = dict(enabled=True, directory=str(root/'remote'), lifetime=60, max_bytes=1048576,
                       ns_enabled=False, tunnel_interface='eth1', external_interface='ns-ex')
            d.save(root/'diagnostics-config.json',cfg)
            d.save(root/'runtime.json',dict(run_id='run', inventory=str(inventory), control_hosts=['alpha'],
                network_hosts=[],compute_hosts=[],ovn_cli_host='alpha'))
            env = dict(os.environ, PATH=str(binary)+os.pathsep+str(Path(sys.executable).parent)+os.pathsep+os.environ['PATH'],
                ANSIBLE_HOST_KEY_CHECKING='False', ANSIBLE_NOCOLOR='1')
            with patch.dict(os.environ,env):
                try:
                    operate(root,'start',role='core')
                    operate(root,'check')
                    operate(root,'snapshot',label='pre-freeze')
                finally:
                    operate(root,'stop')
                initial = json.loads((root/'diagnostics/alpha/core/state.json').read_text())
                try:
                    operate(root,'start',role='core')  # A preparation retry keeps the original segment.
                finally:
                    operate(root,'stop')
            entry = json.loads((root/'diagnostics/checkpoints/alpha-core.json').read_text())
            self.assertEqual(len(entry['segments']),1)
            self.assertTrue(entry['segments'][0]['gap_before_cutover'])
            current = json.loads((root/entry['archive_directory']/'state.json').read_text())
            self.assertNotEqual(current['identity'],initial['identity'])
            self.assertEqual(current['status'],'STOPPED')
            state = json.loads((root/'diagnostics/alpha/core/state.json').read_text())
            self.assertEqual(state['status'],'STOPPED',state)
            self.assertTrue((root/'diagnostics/alpha-core.tar.gz').exists())
            self.assertTrue((root/'diagnostics/alpha/core/events.jsonl').exists())


if __name__ == '__main__':
    unittest.main()
