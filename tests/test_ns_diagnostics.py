"""Diagnostic evidence must preserve TCP tuples and never alter NAT/probes."""
import copy
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import ns_diagnostics as diag
from ns_probe import Client, PREFIX, serve


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = dict(run='run-one', kind='endpoint', endpoint_ip='10.1.0.187', snat_ip='10.1.0.4',
                        fip_ip='10.1.0.5', guest_ip='10.233.0.11', tcp_port=18180, interface='eth1',
                        lifetime=60, directory=str(self.root), allow_create=True)
        self.state = dict(status='RUNNING', config=copy.deepcopy(self.cfg), interface='eth1', started_at=100,
                          supervisor={'pid':1}, tcpdump={'pid':2})

    def save_state(self, state=None):
        diag.save(self.root/'tcp-capture-state.json', state or self.state)

    def test_endpoint_filter_includes_only_approved_ns_tcp_tuples(self):
        self.assertEqual(diag.capture_filter(self.cfg),
            'tcp and port 18180 and host 10.1.0.187 and (host 10.1.0.4 or host 10.1.0.5)')

    def test_guest_filter_checks_full_uuids_and_source(self):
        cfg = dict(self.cfg, kind='guest-snat', server='11111111-1111-4111-8111-111111111111',
                   port='22222222-2222-4222-8222-222222222222')
        self.assertTrue(diag.capture_filter(cfg).endswith('(host 10.233.0.11)'))
        cfg['port'] = 'tap-guessed'
        with self.assertRaises(ValueError): diag.capture_filter(cfg)

    def test_unsafe_values_are_rejected_before_commands(self):
        for key, bad in [('interface','eth1;reboot'), ('endpoint_ip','10.1.0.187;echo'),
                         ('tcp_port',0), ('run','../previous'), ('lifetime',0)]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                diag.checked_config(dict(self.cfg, **{key:bad}))

    def test_endpoint_interface_must_own_exact_ip(self):
        data = json.dumps([{'addr_info':[{'local':'10.1.0.99'}]}])
        with patch.object(diag.subprocess, 'check_output', return_value=data), self.assertRaisesRegex(RuntimeError, 'not assigned'):
            diag.capture_interface(self.cfg)

    def test_running_agent_is_reused_without_duplicate_launch(self):
        self.save_state()
        with patch.object(diag, 'alive', return_value=True), patch.object(diag.subprocess, 'Popen') as launch:
            result = diag.agent_start(self.root, self.cfg)
        self.assertEqual(result['tcpdump'], {'pid':2})
        launch.assert_not_called()

    def test_lost_or_stopped_agent_never_restarts(self):
        for status, live in [('RUNNING',False), ('STOPPED',True), ('START_INTENT',True)]:
            self.save_state(dict(self.state, status=status))
            with patch.object(diag, 'alive', return_value=live), patch.object(diag.subprocess, 'Popen') as launch:
                with self.assertRaisesRegex(RuntimeError, 'replacement prohibited'):
                    diag.agent_start(self.root, self.cfg)
            launch.assert_not_called()

    def test_orphaned_pcap_is_never_overwritten(self):
        (self.root/'tcp.pcap').write_bytes(b'old evidence')
        with self.assertRaisesRegex(RuntimeError, 'not be overwritten'):
            diag.agent_start(self.root, self.cfg)
        self.assertEqual((self.root/'tcp.pcap').read_bytes(), b'old evidence')

    def test_actual_helper_import_cache_does_not_block_startup(self):
        for name in ('ns_diagnostics.py','dataplane_capture.py'):
            (self.root/name).write_bytes((ROOT/'scripts'/name).read_bytes())
        program = '''import json, pathlib, sys
root=pathlib.Path(sys.argv[1]);sys.path.insert(0,str(root))
sys.dont_write_bytecode=False
import dataplane_capture
assert (root/'__pycache__').is_dir(), 'test must reproduce the legacy import cache'
import ns_diagnostics as d
cfg=json.loads(sys.argv[2])
d.capture_interface=lambda cfg: 'eth1'
d.subprocess.run=lambda *args, **kwargs: None
def launch(*args, **kwargs):
 state=d.read_state(root)
 state.update(status='RUNNING',supervisor={'pid':1},tcpdump={'pid':2})
 d.save(root/'tcp-capture-state.json',state)
d.subprocess.Popen=launch
print(json.dumps(d.agent_start(root,cfg)))
'''
        proc=subprocess.run([sys.executable,'-c',program,str(self.root),json.dumps(self.cfg)],capture_output=True,text=True)
        self.assertEqual(proc.returncode,0,proc.stderr)
        self.assertEqual(json.loads(proc.stdout)['status'],'RUNNING')

    def test_cache_allowance_never_hides_capture_evidence(self):
        cache=self.root/'__pycache__';cache.mkdir()
        (cache/'tcp.pcap').write_bytes(b'evidence')
        with self.assertRaisesRegex(RuntimeError,'must not be overwritten'):
            diag.agent_start(self.root,self.cfg)
        self.assertEqual((cache/'tcp.pcap').read_bytes(),b'evidence')

    def test_inspection_proves_no_launch_only_with_no_capture_artifacts(self):
        (self.root/'dataplane_capture.py').write_text('helper')
        self.assertEqual(diag.agent_inspect(self.root,self.cfg)['status'],'NOT_STARTED')
        (self.root/'supervisor.log').write_text('possible launch')
        with self.assertRaisesRegex(RuntimeError,'must be preserved'):
            diag.agent_inspect(self.root,self.cfg)

    def test_stop_rejects_wrong_owner_without_stop_request(self):
        self.save_state()
        with self.assertRaisesRegex(RuntimeError, 'ownership'):
            diag.agent_snapshot(self.root, dict(self.cfg, run='another-run'), stop=True)
        self.assertFalse((self.root/'stop-request').exists())

    def test_stop_signals_supervisor_by_owned_request_and_preserves_pcap(self):
        self.save_state()
        raw = b'pcap evidence'
        (self.root/'tcp.pcap').write_bytes(raw)
        def stopped(_):
            self.assertTrue((self.root/'stop-request').exists())
            self.save_state(dict(self.state, status='STOPPED'))
        with patch.object(diag, 'alive', return_value=True), patch.object(diag.time, 'sleep', side_effect=stopped):
            result = diag.agent_snapshot(self.root, self.cfg, stop=True)
        self.assertEqual(result['status'], 'STOPPED')
        self.assertEqual((self.root/'tcp.pcap').read_bytes(), raw)

    def test_conntrack_snapshot_is_readonly_and_keeps_matching_zone_and_nat_port(self):
        matching = 'tcp,orig=(src=10.233.0.11,dst=10.1.0.187,sport=37529,dport=18180),reply=(src=10.1.0.187,dst=10.1.0.4,sport=18180,dport=35728),zone=1'
        unrelated = 'tcp,orig=(src=10.233.0.12,dst=10.1.0.187,sport=40000,dport=18180),zone=3'
        def run(args, **kwargs):
            out = matching+'\n'+unrelated if 'dpctl/dump-conntrack' in args else '{}'
            return NS(returncode=0, stdout=out, stderr='')
        cfg = dict(router='33333333-3333-4333-8333-333333333333', guest_ip='10.233.0.11', endpoint_ip='10.1.0.187', tcp_port=18180)
        with patch.object(diag.subprocess, 'run', side_effect=run) as commands:
            result = diag.conntrack_snapshot(cfg)
        self.assertEqual(result['ovs']['stdout'], matching)
        self.assertFalse(result['conntrack_transfer'])
        for call in commands.call_args_list:
            args = call.args[0]
            self.assertNotIn('-F', args)
            self.assertNotIn('dpctl/flush-conntrack', args)
        self.assertIn('qrouter-'+cfg['router'], commands.call_args_list[0].args[0])

    def test_missing_legacy_container_is_unavailable_without_erasing_ovn_data(self):
        cfg = dict(router='33333333-3333-4333-8333-333333333333', guest_ip='10.233.0.11', endpoint_ip='10.1.0.187', tcp_port=18180)
        with patch.object(diag.subprocess, 'run', return_value=NS(returncode=1, stdout='', stderr='container not running')):
            result = diag.conntrack_snapshot(cfg)
        self.assertEqual(result['legacy']['status'], 'UNAVAILABLE')
        self.assertEqual(result['ovs']['status'], 'UNAVAILABLE')

    def test_conntrack_cli_runs_without_companion_capture_module(self):
        script = self.root/'ns_diagnostics.py'
        script.write_text((ROOT/'scripts/ns_diagnostics.py').read_text())
        cfg = dict(router='33333333-3333-4333-8333-333333333333', guest_ip='10.233.0.11', endpoint_ip='10.1.0.187', tcp_port=18180)
        proc = subprocess.run([sys.executable, str(script), 'conntrack', '/tmp', json.dumps(cfg)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)['legacy']['status'], 'UNAVAILABLE')

    def controller(self):
        cfg = dict(run='run-one', inventory='/inventory', capture_directory='/captures', lifetime=60, timeout=2,
                   pair_d_network_host='network1', ns=dict(enabled=True, diagnostics_enabled=True, capture_interface='eth1',
                   endpoint_ip='10.1.0.187', snat_ip='10.1.0.4', fip_ip='10.1.0.5', outbound_ip='10.233.0.11',
                   inbound_ip='10.233.0.20', port=18180))
        x = NS(root=self.root, cfg=cfg, state={}, audit=Mock())
        return diag.Diagnostics(x)

    def test_transport_failure_retains_launch_intent_and_refuses_duplicate(self):
        d = self.controller()
        with patch.object(d, 'transport', side_effect=RuntimeError('lost reply')):
            with self.assertRaisesRegex(RuntimeError, 'lost reply'): d.start('endpoint')
            with self.assertRaisesRegex(RuntimeError, 'Ambiguous'): d.start('endpoint')
        self.assertTrue(d.state['captures']['endpoint']['launch_attempted'])

    def test_recovery_preserves_failed_intent_and_rearms_only_proven_unstarted_capture(self):
        d=self.controller()
        config=dict(self.cfg,directory=str(self.root/'ns-diagnostics/endpoint-agent'))
        d.state['captures']={'endpoint':dict(kind='endpoint',host='localhost',status='START_INTENT',
            config=config,launch_attempted=True)}
        with patch.object(d,'transport',return_value=dict(status='NOT_STARTED',config=config)):
            d.recover_unstarted_endpoint()
        self.assertFalse(d.state['captures']['endpoint']['launch_attempted'])
        saved=json.loads((self.root/'ns-diagnostics-launch-recovery.json').read_text())
        self.assertTrue(saved['before']['launch_attempted'])

    def test_recovery_never_rearms_existing_launch_evidence(self):
        d=self.controller()
        config=dict(self.cfg,directory=str(self.root/'ns-diagnostics/endpoint-agent'))
        d.state['captures']={'endpoint':dict(kind='endpoint',host='localhost',status='START_INTENT',
            config=config,launch_attempted=True)}
        with patch.object(d,'transport',return_value=dict(status='FAILED',config=config)):
            with self.assertRaisesRegex(RuntimeError,'launch evidence'): d.recover_unstarted_endpoint()
        self.assertTrue(d.state['captures']['endpoint']['launch_attempted'])

    def test_finish_attempts_all_captures_even_if_first_fails(self):
        d = self.controller()
        d.state['captures'] = {kind:dict(kind=kind, host='host', status='RUNNING') for kind in ('endpoint','guest-snat','guest-fip')}
        responses = [RuntimeError('first failure'), dict(status='STOPPED', dropped_packets=0, captured_packets=3),
                     dict(status='STOPPED', dropped_packets=0, captured_packets=2)]
        with patch.object(d, 'transport', side_effect=responses) as transport:
            errors = d.finish()
        self.assertEqual(transport.call_count, 3)
        self.assertTrue(errors)
        self.assertEqual(json.loads((self.root/'ns-diagnostics.json').read_text())['status'], 'UNAVAILABLE')

    def test_clean_finish_preserves_metadata_and_marks_capture_complete(self):
        d = self.controller()
        d.state['captures'] = {kind:dict(kind=kind, host='host', status='RUNNING') for kind in ('endpoint','guest-snat','guest-fip')}
        with patch.object(d, 'transport', return_value=dict(status='STOPPED', dropped_packets=0, captured_packets=5)):
            self.assertEqual(d.finish(), [])
        result = json.loads((self.root/'ns-diagnostics.json').read_text())
        self.assertEqual(result['status'], 'PASS')
        self.assertFalse(result['conntrack_transfer'])

    def test_sender_refresh_and_missing_log_are_retained_as_diagnostic_failure(self):
        d=self.controller()
        d.cfg['ns'].update(icmp_enabled=True,icmp_sender_logging_enabled=True)
        d.state['captures']={kind:dict(kind=kind,host='host',status='RUNNING')
            for kind in ('endpoint','guest-snat','guest-fip')}
        d.x.ns_rows=Mock()
        outcomes={'snat':{'sender_log':{'status':'UNAVAILABLE','reason':'console gap'}},
                  'fip':{'sender_log':{'status':'PASS'}}}
        with patch.object(d,'transport',return_value=dict(status='STOPPED',dropped_packets=0,captured_packets=5)), \
             patch('ns_icmp_evidence.finalize',return_value=outcomes):
            errors=d.finish()
        d.x.ns_rows.assert_called_once()
        self.assertIn('snat/ICMP sender log: console gap',errors)
        self.assertEqual(json.loads((self.root/'ns-diagnostics.json').read_text())['status'],'UNAVAILABLE')

    def test_pre_freeze_rejects_missing_or_replaced_capture(self):
        d = self.controller()
        with self.assertRaisesRegex(RuntimeError, 'missing before freeze'): d.ready()
        d.state['captures'] = {kind:dict(kind=kind, host='host', remote=dict(self.state))
            for kind in ('endpoint','guest-snat','guest-fip')}
        with patch.object(d, 'transport', return_value=dict(self.state, tcpdump={'pid':99})):
            with self.assertRaisesRegex(RuntimeError, 'identity changed'): d.ready()

    def test_conntrack_records_all_network_hosts_and_keeps_partial_failure(self):
        d = self.controller()
        d.x.state = {'ns':{'router':'33333333-3333-4333-8333-333333333333'}}
        d.cfg['ns']['network_hosts'] = ['network1','network2']
        with patch.object(d, 'transport', side_effect=[RuntimeError('unreachable'), {'ovs':{'status':'PASS'}}]) as transport:
            d.snapshot_conntrack('post-cutover')
        result = json.loads((self.root/'ns-conntrack-post-cutover.json').read_text())
        self.assertEqual([r['inventory_host'] for r in result['hosts']], ['network1','network2'])
        self.assertEqual(result['hosts'][0]['status'], 'UNAVAILABLE')
        self.assertEqual(result['hosts'][1]['ovs']['status'], 'PASS')
        self.assertEqual(transport.call_count, 2)

    def test_failed_live_capture_blocks_reset(self):
        self.save_state(dict(self.state, status='FAILED'))
        with patch.object(diag, 'alive', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'still has a live process'):
                diag.agent_snapshot(self.root, self.cfg, stop=True)

    def test_supervisor_stops_real_owned_child_and_archives_stats(self):
        # Fake only the packet producer; exercise real Popen and SIGINT/wait.
        script = self.root/'packet-producer.py'
        script.write_text('''import signal, struct, sys, time
from pathlib import Path
running=True
def stop(*_):
 global running
 running=False
signal.signal(signal.SIGINT,stop)
Path(sys.argv[1]).write_bytes(struct.pack('<IHHIIII',0xa1b2c3d4,2,4,0,0,65535,1))
while running: time.sleep(.01)
sys.stderr.write('5 packets captured\\n0 packets dropped by kernel\\n')
''')
        self.save_state(dict(self.state, status='START_INTENT', path=str(self.root/'tcp.pcap'), filter=diag.capture_filter(self.cfg)))
        original = subprocess.Popen
        children = []
        def producer(args, **kwargs):
            self.assertEqual(args[0], 'tcpdump')
            self.assertIn('-s', args)
            child = original([sys.executable,str(script),str(self.root/'tcp.pcap')], **kwargs)
            children.append(child)
            return child
        def request_stop():
            deadline=time.monotonic()+3
            while time.monotonic()<deadline:
                if diag.read_state(self.root)['status']=='RUNNING':
                    (self.root/'stop-request').touch()
                    return
                time.sleep(.01)
        stopper=threading.Thread(target=request_stop,daemon=True);stopper.start()
        with patch.object(diag.subprocess,'Popen',side_effect=producer), patch.object(diag.signal,'signal'):
            diag.supervise(self.root)
        stopper.join(1)
        self.assertEqual(diag.read_state(self.root)['status'],'STOPPED')
        self.assertEqual(diag.read_state(self.root)['captured_packets'],5)
        self.assertEqual(diag.read_state(self.root)['dropped_packets'],0)
        self.assertEqual(children[0].poll(),0)

    def test_reset_preserves_archived_stopped_capture_without_remote_access(self):
        backups = self.root/'backups'
        root = backups/'run-one'
        root.mkdir(parents=True)
        cfg = dict(run='run-one', inventory='/inventory')
        diag.save(root/'validation-config.json', cfg)
        cp = dict(kind='endpoint', host='localhost', config=self.cfg, status='STOPPED', remote=dict(self.state, status='STOPPED'))
        diag.save(root/'ns-diagnostics-checkpoint.json', dict(schema=1, run='run-one', captures={'endpoint':cp}))
        archive = root/'ns-diagnostics/endpoint'
        archive.mkdir(parents=True)
        (archive/'tcp.pcap').write_bytes(b'raw pcap retained')
        diag.save(archive/'tcp-capture-state.json', cp['remote'])
        with patch.object(diag.Diagnostics, 'transport') as transport:
            diag.reset_stop(backups, self.root/'reset-evidence.json')
        transport.assert_not_called()
        self.assertEqual((archive/'tcp.pcap').read_bytes(), b'raw pcap retained')


class RealTCPPortEvidenceTests(unittest.TestCase):
    def test_server_echo_and_connection_log_identify_same_held_tuple(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory)/'server.jsonl'
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1',0))
                port = reservation.getsockname()[1]
            cfg = dict(run='test-run', bind='127.0.0.1', port=port, server='endpoint',
                       lifetime=.5, console=False, output=str(log))
            server = threading.Thread(target=serve, args=(cfg,), daemon=True)
            server.start()
            deadline = time.monotonic()+2
            while not log.exists() and time.monotonic() < deadline: time.sleep(.01)
            client = Client(dict(run='test-run', source='127.0.0.1', target='127.0.0.1', port=port,
                                 expected_peer='127.0.0.1', expected_server='endpoint', timeout=.1, interval=.02))
            worker = threading.Thread(target=client.stream, args=('held',), daemon=True)
            worker.start()
            deadline = time.monotonic()+1
            while client.stats['held'].snapshot()['successes'] < 3 and time.monotonic() < deadline: time.sleep(.01)
            client.stop.set(); worker.join(1); server.join(2)
            held = client.stats['held'].snapshot()
            self.assertGreaterEqual(held['successes'],3)
            self.assertEqual(held['connection_attempts'],1)
            self.assertEqual(held['first_local_endpoint'],held['first_peer_endpoint'])
            self.assertEqual(held['first_local_endpoint'],held['last_local_endpoint'])
            rows = [json.loads(line.split(PREFIX,1)[1]) for line in log.read_text().splitlines()]
            opened = next(r for r in rows if r['kind']=='connection-open')
            self.assertEqual(opened['connection'], held['first_session']['connection'])
            self.assertEqual(opened['peer'], held['first_peer_endpoint'])
            self.assertEqual(opened['local'],['127.0.0.1',port])


if __name__ == '__main__':
    unittest.main()
