"""Busy controller ports fail before VM creation and retain the real startup error."""
import errno
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import yaml

from tests.validation.test_validation import ROOT
from extended_validation import Extended
from ns_endpoint import check
from run_layout import initialize, run_path


class EndpointTests(unittest.TestCase):
    def test_occupied_listener_fails_and_closed_listener_releases_the_same_port(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0)); listener.listen()
            port=listener.getsockname()[1]
            result=check('127.0.0.1',port)
            self.assertEqual(result['status'],'FAIL')
            self.assertEqual(result['errno'],errno.EADDRINUSE)
        self.assertEqual(check('127.0.0.1',port)['status'],'PASS')

    def test_actual_phase02_task_blocks_on_busy_port_and_passes_after_release(self):
        source=yaml.safe_load((ROOT/'playbooks/02-precheck.yml').read_text())
        task=next(t for p in source for t in p.get('tasks',[])
            if t['name']=='[02] Check controller N-S listener port is free')
        with tempfile.TemporaryDirectory() as folder,socket.socket() as listener:
            root=Path(folder); listener.bind(('127.0.0.1',0)); listener.listen()
            port=listener.getsockname()[1]
            # Keep playbook_dir aligned with the production ../scripts path.
            (root/'playbooks').mkdir(); (root/'scripts').symlink_to(ROOT/'scripts',target_is_directory=True)
            play=root/'playbooks/preflight.yml'
            play.write_text(yaml.safe_dump([dict(hosts='localhost',connection='local',gather_facts=False,
                vars=dict(ns_endpoint_ip='127.0.0.1',ns_tcp_port=port,validation_ns_enabled=True),tasks=[task])],sort_keys=False))
            command=[sys.executable,'-m','ansible.cli.playbook','-i','localhost,',str(play)]
            failed=subprocess.run(command,capture_output=True,text=True,timeout=30)
            self.assertNotEqual(failed.returncode,0)
            self.assertIn('Address already in use',failed.stdout+failed.stderr)
            listener.close()
            ready=subprocess.run(command,capture_output=True,text=True,timeout=30)
            self.assertEqual(ready.returncode,0,ready.stdout+ready.stderr)

    def test_reset_checks_port_release_after_probe_cleanup_and_before_destroy(self):
        plays=yaml.safe_load((ROOT/'reset-lab-to-ovs.yml').read_text())
        tasks=next(p['tasks'] for p in plays if p['name']=='Destroy the current Kolla deployment from the deployment host')
        names=[t['name'] for t in tasks]
        before=names.index('Stop owned controller N-S probes before Kolla destroy')
        guard=names.index('Verify the controller N-S listener port was released before Kolla destroy')
        self.assertLess(before,guard)
        self.assertLess(guard,names.index('Run kolla-ansible destroy'))


class LocalStartupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'run-one'; initialize(self.root)
        self.children=[]
        real_popen=subprocess.Popen
        def launch(*args,**kwargs):
            process=real_popen(*args,**kwargs); self.children.append(process); return process
        def cleanup():
            for process in self.children:
                if process.poll() is None:
                    process.terminate()
                    try: process.wait(2)
                    except subprocess.TimeoutExpired: process.kill()
                process.wait()
        self.addCleanup(cleanup)
        self.launch=patch('extended_validation.subprocess.Popen',side_effect=launch)
        self.launch.start(); self.addCleanup(self.launch.stop)
        identity=patch('extended_validation.identity',side_effect=lambda pid:
            dict(pid=pid,start_ticks='fixture',boot='fixture'))
        identity.start(); self.addCleanup(identity.stop)
        self.x=Extended(SimpleNamespace(root=self.root,cfg=dict(run=self.root.name,timeout=5),cloud=None))

    def config(self,port):
        return dict(run=self.root.name,bind='127.0.0.1',port=port,server='external-endpoint',
            lifetime=30,console=False,output=str(self.root/'ns/snat-server.jsonl'))

    def test_real_probe_bind_failure_is_reported_and_checkpoint_is_failed(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0)); listener.listen()
            with self.assertRaisesRegex(RuntimeError,'Address already in use'):
                self.x.local_process('snat-server','serve',self.config(listener.getsockname()[1]))
        state=json.loads(run_path(self.root,'extended-resources.json').read_text())
        cp=state['processes']['snat-server']
        self.assertEqual(cp['status'],'FAILED'); self.assertNotEqual(cp['returncode'],0)
        self.assertIn('Address already in use',(self.root/'ns/snat-server.log').read_text())
        self.assertFalse((self.root/'ns/snat-server.jsonl').exists())

    def fake_probe(self,emit=True):
        folder=self.root.parent/'helpers'; folder.mkdir()
        (folder/'ns_probe.py').write_text('import json,signal,socket,sys,time\nfrom pathlib import Path\n'
            'cfg=json.loads(Path(sys.argv[2]).read_text())\n'
            'signal.signal(signal.SIGTERM,lambda *args: sys.exit(0))\n'
            'sock=socket.socket(); sock.bind((cfg["bind"],cfg["port"])); sock.listen()\n'+
            ('Path(cfg["output"]).write_text("ready")\n' if emit else '')+
            'while True: time.sleep(.05)\n')
        return patch('extended_validation.script_path',return_value=folder/'ns_probe.py')

    def test_success_is_ready_before_running_and_reuse_does_not_launch_a_replacement(self):
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1',0)); port=reservation.getsockname()[1]
        cfg=self.config(port)
        with self.fake_probe(): self.x.local_process('snat-server','serve',cfg)
        cp=self.x.state['processes']['snat-server']
        self.assertEqual(cp['status'],'RUNNING')
        self.assertTrue(Path(cfg['output']).exists())
        with socket.socket() as other:
            with self.assertRaises(OSError): other.bind(('127.0.0.1',port))
        with patch('extended_validation.alive',return_value=True):
            self.x.local_process('snat-server','serve',cfg)
        self.assertEqual(len(self.children),1)

    def test_readiness_timeout_retains_process_identity_for_reset_and_refuses_relaunch(self):
        self.x.cfg['timeout']=.2
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1',0)); port=reservation.getsockname()[1]
        cfg=self.config(port)
        with self.fake_probe(emit=False):
            with self.assertRaisesRegex(RuntimeError,'readiness timed out'):
                self.x.local_process('snat-server','serve',cfg)
        cp=self.x.state['processes']['snat-server']
        self.assertEqual(cp['status'],'FAILED')
        self.assertEqual(cp['identity']['pid'],self.children[0].pid)
        self.assertIsNone(self.children[0].poll())
        with patch('extended_validation.alive',return_value=True):
            with self.assertRaisesRegex(RuntimeError,'restart prohibited'):
                self.x.local_process('snat-server','serve',cfg)
        self.assertEqual(len(self.children),1)


if __name__=='__main__': unittest.main()
