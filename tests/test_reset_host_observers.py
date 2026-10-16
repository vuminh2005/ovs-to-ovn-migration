"""Reset acts on live processes, independently of old reports and Jinja configs."""
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
import yaml

from test_validation import ROOT
import reset_host_observers as r


class LiveResetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)/'observers'; self.base.mkdir()
        self.children=[]; self.addCleanup(self.cleanup)

    def cleanup(self):
        for proc in self.children:
            if proc.poll() is None: proc.kill()
            proc.wait(timeout=3)

    def child(self,run,*,base=None,ignore=False):
        root=(base or self.base)/run/'controller-core'; root.mkdir(parents=True)
        script=root/'migration_diagnostics.py'
        script.write_text('import signal,time,sys\nfrom pathlib import Path\n'
            +'signal.signal(signal.SIGTERM,'+('signal.SIG_IGN' if ignore else 'lambda *_:sys.exit(0)')+')\n'
            +'Path(sys.argv[2],"ready").touch()\nwhile True:time.sleep(.01)\n')
        proc=subprocess.Popen([sys.executable,str(script),'run',str(root)])
        self.children.append(proc)
        deadline=time.monotonic()+3
        while not (root/'ready').exists() and time.monotonic()<deadline:time.sleep(.01)
        self.assertTrue((root/'ready').exists())
        return proc,root

    def mapped_stop(self,timeout=1):
        # This executor exposes host /proc inside its child PID namespace.
        # Adapt only the pidfd boundary; production matching/identity/signal/wait run unchanged.
        real_open=os.pidfd_open
        def mapped(pid):
            line=next(line for line in Path(f'/proc/{pid}/status').read_text().splitlines() if line.startswith('NSpid:'))
            return real_open(int(line.split()[-1]))
        with patch.object(r.os,'pidfd_open',side_effect=mapped):
            return r.stop_observers(self.base,timeout)

    def test_old_run_results_and_archives_are_not_read_or_rewritten(self):
        old=self.base/'old-run/controller-core'; old.mkdir(parents=True)
        files={old/'state.json':'not valid JSON',old/'config.json':'unrendered {{ Jinja }}',
               old/'migration-report.txt':'historical failure'}
        for path,text in files.items():path.write_text(text)
        value=self.mapped_stop()
        self.assertEqual(value['status'],'PASS'); self.assertEqual(value['signaled'],[])
        self.assertEqual({p:p.read_text() for p in files},files)

    def test_stops_both_live_runs_in_one_pass_and_preserves_foreign_prefix(self):
        one,_=self.child('old-live-run'); two,_=self.child('new-live-run')
        foreign,_=self.child('foreign-run',base=Path(str(self.base)+'-foreign'))
        value=self.mapped_stop()
        self.assertEqual(value['status'],'PASS',value)
        self.assertEqual({x['run'] for x in value['stopped']},{'old-live-run','new-live-run'})
        one.wait(timeout=2); two.wait(timeout=2); self.assertIsNone(foreign.poll())
        self.assertEqual(self.mapped_stop()['signaled'],[])

    def test_live_observer_ignoring_term_blocks_with_exact_remaining_identity(self):
        proc,_=self.child('stuck',ignore=True)
        value=self.mapped_stop(timeout=.05)
        self.assertEqual(value['status'],'FAIL'); self.assertEqual(len(value['remaining']),1)
        self.assertEqual(value['remaining'][0]['run'],'stuck')
        self.assertEqual(value['stopped'],[]); self.assertIsNone(proc.poll())

    def test_changed_command_is_never_signaled(self):
        proc,_=self.child('changed')
        original=r.invocation; counts={}
        def changing(pid,base):
            value=original(pid,base)
            if value:
                counts[pid]=counts.get(pid,0)+1
                if counts[pid]>=3:return None
            return value
        with patch.object(r,'invocation',side_effect=changing):value=self.mapped_stop()
        self.assertEqual(value['status'],'FAIL'); self.assertEqual(value['signaled'],[])
        self.assertEqual(len(value['remaining']),1); self.assertIsNone(proc.poll())

    def test_rejects_relative_or_overbroad_directory(self):
        for root in ('/','relative','/var/lib/../'):
            with self.subTest(root=root),self.assertRaises(ValueError):r.stop_observers(Path(root))


class ResetAnsibleTests(unittest.TestCase):
    def test_real_parent_reset_tasks_record_nested_success_and_failure_before_destroy(self):
        plays=yaml.safe_load((ROOT/'reset-lab-to-ovs.yml').read_text())
        production=next(p for p in plays if p['name']=='Destroy the current Kolla deployment from the deployment host')
        names=[t['name'] for t in production['tasks']]
        rows=production['tasks'][names.index('Stop live host observers before Kolla destroy'):
            names.index('Require current observer shutdown before Kolla destroy')+1]
        for valid in (True,False):
            with self.subTest(valid=valid),tempfile.TemporaryDirectory() as folder:
                root=Path(folder); evidence=root/'evidence'; evidence.mkdir()
                for name in ('playbooks','scripts','group_vars'):(root/name).symlink_to(ROOT/name,target_is_directory=True)
                inventory=root/'inventory.ini'
                inventory.write_text('[control]\ncontroller ansible_connection=local ansible_python_interpreter='+sys.executable+'\n')
                harness=root/'reset-test.yml'
                harness.write_text(yaml.safe_dump([dict(hosts='localhost',gather_facts=False,
                    vars=dict(reset_kolla_inventory=str(inventory),reset_run_dir=str(evidence),
                        reset_diagnostics_directory=str(root/'observers') if valid else 'relative-not-allowed'),tasks=rows)],sort_keys=False))
                result=subprocess.run([sys.executable,'-m','ansible.cli.playbook','-i',str(inventory),str(harness)],
                    capture_output=True,text=True,timeout=40)
                self.assertEqual(result.returncode==0,valid,result.stdout+result.stderr)
                self.assertTrue((evidence/'host-observers-stop.log').exists())
                self.assertTrue((evidence/'host-observers-controller.json').exists())
                if not valid:self.assertIn('Require current observer shutdown before Kolla destroy',result.stdout)

    def test_production_helper_play_needs_no_migration_run_or_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); evidence=root/'reset'; evidence.mkdir()
            base=root/'observers'
            inventory=root/'inventory.ini'
            inventory.write_text('[control]\ncontroller ansible_connection=local ansible_python_interpreter='+sys.executable+'\n')
            extra=root/'vars.json'
            extra.write_text(json.dumps(dict(reset_observer_evidence_dir=str(evidence),reset_diagnostics_directory=str(base))))
            result=subprocess.run([sys.executable,'-m','ansible.cli.playbook','-i',str(inventory),
                str(ROOT/'playbooks/reset-host-observers.yml'),'-e','@'+str(extra)],capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            value=json.loads((evidence/'host-observers-controller.json').read_text())
            state=json.loads(value['stdout'])
            self.assertEqual(state['status'],'PASS'); self.assertEqual(state['signaled'],[])
            self.assertFalse(base.exists())

    def test_reset_uses_live_inventory_cleanup_before_destroy_and_preserves_transport_errors(self):
        plays=yaml.safe_load((ROOT/'reset-lab-to-ovs.yml').read_text())
        play=next(p for p in plays if p['name']=='Destroy the current Kolla deployment from the deployment host')
        tasks=play['tasks']; names=[t['name'] for t in tasks]
        stop=names.index('Stop live host observers before Kolla destroy')
        gate=names.index('Require current observer shutdown before Kolla destroy')
        destroy=names.index('Run kolla-ansible destroy')
        self.assertLess(stop,gate); self.assertLess(gate,destroy)
        self.assertNotIn('diagnostics_control.py',str(tasks[stop]))
        self.assertNotIn('reset_backup_root_runtime',str(tasks[stop]))
        self.assertIn('reset_kolla_inventory',str(tasks[stop]))
        self.assertEqual(tasks[gate]['ansible.builtin.assert']['that'],'reset_host_observers.rc == 0')
        self.assertIn('host-observers-stop.log',str(tasks[gate]))


if __name__=='__main__':unittest.main()
