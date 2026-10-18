"""Real child playbooks must find run_file outside the project working directory."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_validation import ROOT
import dataplane_capture as capture_module
from dataplane_capture import Capture
from run_layout import initialize, run_path


class FilterDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name)
        self.inventory=self.home/'inventory.ini'
        self.inventory.write_text('[all]\nlocalhost ansible_connection=local ansible_become=false '
            'ansible_python_interpreter='+sys.executable+'\n')
        config=self.home/'isolated.cfg'; config.write_text('[defaults]\n')
        empty=self.home/'empty-plugins'; empty.mkdir()
        self.env=dict(os.environ,ANSIBLE_CONFIG=str(config),
            ANSIBLE_FILTER_PLUGINS=str(empty),ANSIBLE_NOCOLOR='1')
        self.env.pop('PYTHONPATH',None)

    def root(self, organized):
        path=self.home/('organized' if organized else 'legacy'); path.mkdir()
        if organized: initialize(path)
        return path

    def play(self, name, task, variables):
        result=subprocess.run([sys.executable,'-m','ansible.cli.playbook',
            '-i',str(self.inventory),str(ROOT/'playbooks'/name),
            '--start-at-task',task,'-e',json.dumps(variables),'-e','ansible_become=false'],
            cwd=self.home,env=self.env,capture_output=True,text=True,timeout=40)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_capture_state_and_original_files_are_saved_without_project_config(self):
        for organized in (False,True):
            with self.subTest(organized=organized):
                root=self.root(organized)
                remote=self.home/('remote-'+root.name); remote.mkdir()
                originals={name:('original '+name).encode() for name in
                    ('measure0.pcap','tcpdump.log','capture-state.json','supervisor.log')}
                for name,data in originals.items(): (remote/name).write_bytes(data)
                state=dict(status='STOPPED',tap='lo')
                self.play('dataplane-capture-tasks.yml',
                    '[GROUP A] [SAVE] Save capture process state',dict(
                        capture_root=str(root),capture_action='stop',
                        capture_checkpoint=dict(directory=str(remote),compute_host='localhost'),
                        capture_operation=dict(stdout=json.dumps(state))))
                self.assertEqual(json.loads(run_path(root,'capture-remote-state.json').read_text()),state)
                for name,data in originals.items():
                    self.assertEqual(run_path(root,name).read_bytes(),data)
                    self.assertEqual((remote/name).read_bytes(),data)

    def test_group_d_acknowledgement_is_saved_without_project_config(self):
        for organized in (False,True):
            with self.subTest(organized=organized):
                root=self.root(organized); value=dict(status='ARMED',run=root.name)
                filename='pair-d-trigger-ack.arm.json'
                self.play('pair-d-trigger-tasks.yml',
                    '[GROUP D] [SAVE] Save Group D arm acknowledgement',dict(
                        pair_d_root=str(root),pair_d_ack_filename=filename,
                        pair_d_arm=dict(stdout=json.dumps(value))))
                self.assertEqual(json.loads(run_path(root,filename).read_text()),value)

    def test_capture_transport_resolves_and_saves_state_from_an_unrelated_directory(self):
        # Exercise the real Python -> Ansible -> staged Python chain. The fake
        # OVS/libvirt commands expose the existing loopback interface. A fake
        # tcpdump writes an empty PCAP and waits for SIGINT; it sends no traffic.
        # This test runtime has no Linux /proc identities, so the test interpreter
        # substitutes a stable identity for the live local child processes.
        binary=self.home/'bin'; binary.mkdir()
        ovs=binary/'ovs-vsctl'
        ovs.write_text('#!'+sys.executable+'\nimport json,sys\n'
            'if "iface-to-br" in sys.argv: print("br-int")\n'
            'else: print(json.dumps({"headings":["name","external_ids"],'
            '"data":[["lo",["map",[["iface-id","port"],["vm-uuid","server"]]]]]}))\n')
        docker=binary/'docker'
        docker.write_text('#!'+sys.executable+'\nprint('+repr(
            '<domain><uuid>server</uuid><devices><interface><target dev="lo"/>'
            '<virtualport><parameters interfaceid="port"/></virtualport>'
            '</interface></devices></domain>')+')\n')
        tcpdump=binary/'tcpdump'
        tcpdump.write_text('#!'+sys.executable+'\n'
            'import signal,struct,sys,time\nfrom pathlib import Path\n'
            'def stop(*args):\n'
            ' print("0 packets dropped by kernel",file=sys.stderr,flush=True)\n'
            ' raise SystemExit(0)\n'
            'signal.signal(signal.SIGINT,stop)\n'
            'Path(sys.argv[sys.argv.index("-w")+1]).write_bytes('
            'b"\\xd4\\xc3\\xb2\\xa1"+struct.pack("<HHIIII",2,4,0,0,65535,1))\n'
            'while True: time.sleep(.1)\n')
        interpreter=binary/'python3'
        interpreter.write_text('#!'+sys.executable+'\n'
            'import importlib.util,os,sys\n'
            'path=sys.argv.pop(1)\n'
            'sys.path.insert(0,os.path.dirname(path))\n'
            'spec=importlib.util.spec_from_file_location("test_capture_agent",path)\n'
            'module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)\n'
            'def identity(pid):\n'
            ' try: os.kill(pid,0)\n'
            ' except (OSError,TypeError): return None\n'
            ' return {"pid":pid,"start_ticks":"fixture","boot":"fixture"}\n'
            'module.identity=identity; sys.executable=__file__; module.main()\n')
        for path in (ovs,docker,tcpdump,interpreter): path.chmod(0o700)
        env=dict(self.env,PATH=str(binary)+os.pathsep+str(Path(sys.executable).parent)+
            os.pathsep+os.environ['PATH'])
        for organized in (False,True):
            with self.subTest(organized=organized):
                root=self.root(organized); remote=self.home/('remote-'+root.name)
                capture=Capture(SimpleNamespace(root=root,cfg=dict(inventory=str(self.inventory),timeout=40)))
                checkpoint=dict(run=root.name,compute_host='localhost',directory=str(remote),
                    server='server',port='port',source_ip='10.0.0.2',peer_ip='10.0.1.2',interval=.2,
                    path=str(remote/'measure0.pcap'))
                previous=Path.cwd()
                try:
                    os.chdir(self.home)
                    with patch.dict(os.environ,env,clear=True):
                        state=capture.transport('resolve',checkpoint)
                        self.assertEqual(state,dict(status='RESOLVED',tap='lo',integration_bridge='br-int'))
                        self.assertEqual(json.loads(run_path(root,'capture-remote-state.json').read_text()),state)
                        self.assertIn('failed=0',run_path(root,'capture-transport-resolve.log').read_text())
                        self.assertTrue((remote/'dataplane_capture.py').exists())
                        self.assertTrue((remote/'run_layout.py').exists())
                        self.assertFalse((remote/'capture-state.json').exists())
                        if organized:
                            cache=remote/'__pycache__'; cache.mkdir(exist_ok=True)
                            (cache/'run_layout.cpython-312.pyc').write_bytes(b'old helper cache')
                        checkpoint.update(tap='lo',integration_bridge='br-int',allow_create=True)
                        failed=False
                        try:
                            started=capture.transport('start',checkpoint)
                            self.assertEqual(started['status'],'RUNNING')
                            reused=capture.transport('start',dict(checkpoint,allow_create=False))
                            for key in ('supervisor','tcpdump','started_at','tap','path'):
                                self.assertEqual(reused[key],started[key])
                        except Exception as exc:
                            failed=True
                            details='\n'.join(name+': '+(remote/name).read_text() for name in
                                ('capture-state.json','supervisor.log','tcpdump.log') if (remote/name).exists())
                            raise AssertionError(str(exc)+'\n'+details) from exc
                        finally:
                            journal=remote/'capture-state.json'
                            if journal.exists() and json.loads(journal.read_text()).get('status')=='RUNNING':
                                try:
                                    stopped=capture.transport('stop',checkpoint)
                                except RuntimeError:
                                    if not failed: raise
                                else:
                                    self.assertEqual(stopped['status'],'STOPPED')
                                    self.assertEqual(stopped['dropped_packets'],0)
                        for name in ('measure0.pcap','tcpdump.log','capture-state.json','supervisor.log'):
                            self.assertEqual(run_path(root,name).read_bytes(),(remote/name).read_bytes())
                        capture.transport('remove',checkpoint)
                        self.assertFalse(remote.exists())
                        self.assertTrue(run_path(root,'measure0.pcap').exists())
                finally:
                    os.chdir(previous)

    def test_capture_start_still_refuses_unrelated_files_and_helper_symlinks(self):
        for foreign in ('foreign.json','cache-file','cache-symlink','helper-symlink'):
            with self.subTest(foreign=foreign):
                root=self.home/foreign; root.mkdir()
                (root/'dataplane_capture.py').write_text('staged helper')
                if foreign=='foreign.json': (root/foreign).write_text('keep')
                elif foreign=='cache-file':
                    cache=root/'__pycache__'; cache.mkdir(); (cache/'foreign.pyc').write_text('keep')
                elif foreign=='cache-symlink': (root/'__pycache__').symlink_to(self.home,target_is_directory=True)
                else: (root/'run_layout.py').symlink_to(ROOT/'scripts/run_layout.py')
                with patch.object(capture_module.subprocess,'Popen') as launch:
                    with self.assertRaisesRegex(RuntimeError,'non-owned'):
                        capture_module.agent_start(root,dict(run='run',server='server',port='port'))
                    launch.assert_not_called()


if __name__=='__main__': unittest.main()
