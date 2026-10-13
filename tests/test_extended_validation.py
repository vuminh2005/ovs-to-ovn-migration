import copy
import json
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
from extended_validation import Extended, FLOW_NAMES, quota20
from ns_probe import Client, Stats, Wire
from ns_underlay import netplan_update
from workload_validation import save, validation_ready


class ExtensionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = dict(run='test-run', prefix='validation', ew_matrix_enabled=True, ns={'enabled':False},
                       placement={'0':{'inventory_host':'compute1'}, '1':{'inventory_host':'compute2'}},
                       interval=.2, lifetime=30, timeout=2, capture_directory='/captures')
        self.a = {str(i): dict(owned=True, record_vm='measure'+str(i), network='net'+str(i), subnet='sub'+str(i),
            ip='10.231.'+str(i)+'.20', placement=self.cfg['placement'][str(i)]) for i in range(2)}
        self.v = NS(root=self.root, cfg=self.cfg, cloud=Mock(),
                    pair=lambda _: self.a, placement=Mock(return_value={'status':'PASS'}))
        self.x = Extended(self.v)
        self.x.state['matrix'] = {str(i): dict(role='ew-matrix-'+str(i), record_vm='ew-matrix-'+str(i),
            owned=True, network='net1', subnet='sub1', ip='10.231.1.'+str(30+i),
            placement=self.cfg['placement'][str(i)]) for i in range(2)}

    def test_three_added_flows_fill_all_compute_subnet_cases(self):
        expected = [(True,False), (False,True), (True,True)]
        for name, (same_compute, same_subnet) in zip(FLOW_NAMES, expected):
            source, dest = self.x.flows()[name]
            self.assertEqual(source['placement'] == dest['placement'], same_compute)
            self.assertEqual(source['subnet'] == dest['subnet'], same_subnet)

    def test_shared_measure_sources_receive_only_one_added_ping(self):
        for index in ('0','1'):
            data = {'write_files':[], 'runcmd':[]}
            self.x.decorate_measure(data, index)
            cfg = json.loads(next(f['content'] for f in data['write_files'] if f['path']=='/etc/aux-ping.json'))
            self.assertEqual(cfg['peer'], self.x.state['matrix'][index]['ip'])
            self.assertEqual(cfg['ip'], self.a[index]['ip'])
            self.assertEqual(data['runcmd'], [['systemctl','enable','--now','aux-ping']])

    def test_captures_have_separate_local_and_remote_roots_and_exact_role(self):
        roots = []
        remote = []
        for name, pair in self.x.flows().items():
            cap = self.x.capture(name, pair)
            roots.append(cap.v.root)
            remote.append(cap.v.cfg['capture_directory'])
            self.assertEqual(cap.v.pair('measure')['0'], pair[0])
            self.assertEqual(cap.v.cfg['capture_role'], pair[0].get('role','measure'))
        self.assertEqual(len(set(roots)), 3)
        self.assertEqual(len(set(remote)), 3)

    def test_existing_resource_requires_checkpoint_or_run_description(self):
        self.v.cloud.network.networks.return_value = [NS(id='foreign', name='name', description='foreign')]
        with self.assertRaisesRegex(RuntimeError, 'unowned'):
            self.x.resource({}, 'network', 'network', 'name')
        self.v.cloud.network.create_network.assert_not_called()

    def test_lost_resource_reply_recovers_exact_run_name_without_duplicate(self):
        obj = NS(id='owned', name='name', description='ovn-migration-run=test-run')
        self.v.cloud.network.networks.return_value = [obj]
        state = {}
        self.assertIs(self.x.resource(state,'network','network','name'), obj)
        self.assertEqual(state['network'], 'owned')
        self.v.cloud.network.create_network.assert_not_called()

    def test_port_fixed_ip_or_placement_change_cannot_be_adopted(self):
        self.v.cloud.network.ports.return_value = []
        self.v.cloud.network.create_port.return_value = NS(id='port', network_id='net',
            security_group_ids=['sg'], fixed_ips=[{'subnet_id':'sub','ip_address':'10.1.0.20'}])
        group = {}
        vm = self.x.port(group,'0','net','sub','role',{'host':'one'},'sg')
        self.v.cloud.network.get_port.return_value = self.v.cloud.network.create_port.return_value
        with self.assertRaisesRegex(RuntimeError, 'placement'):
            self.x.port(group,'0','net','sub','role',{'host':'two'},'sg')

    def test_restart_of_local_client_is_refused(self):
        self.x.state['processes'] = {'client': {'config':{'a':1}, 'identity':None}}
        with self.assertRaisesRegex(RuntimeError, 'restart prohibited'):
            self.x.local_process('client','client',{'a':1})

    def baseline(self):
        stats = Stats().snapshot()
        session = dict(server='srv', server_boot='boot-srv', listener='listener', connection='connection')
        stats.update(attempts=20, successes=20, consecutive_successes=20, first_session=session,
                     last_session=session, connection_attempts=1, observed_peer_ips=['10.1.0.4'])
        row = dict(boot='guest-boot', probe_id='probe', mono=100, streams={'held':copy.deepcopy(stats),'new':copy.deepcopy(stats)})
        return {'clients':{'snat':copy.deepcopy(row),'fip':copy.deepcopy(row)}}

    def test_ns_measurement_excludes_startup_failures_before_baseline(self):
        baseline = self.baseline()
        rows = copy.deepcopy(baseline['clients'])
        for row in rows.values():
            row['mono'] = 200
            for stats in row['streams'].values():
                stats.update(attempts=40, failures=2)
                stats['recovered_failure_bursts'] = [dict(start_mono=50, duration_seconds=15), dict(start_mono=120, duration_seconds=1)]
        self.x.ns_results(rows, {}, baseline)
        result = json.loads((self.root/'ns-results.json').read_text())
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['directions']['snat']['streams']['held']['longest_recovered_failure_seconds'], 1)
        self.assertEqual(result['directions']['snat']['streams']['new']['attempts'], 20)

    def test_held_closed_reports_fail_and_keeps_new_connection_metrics(self):
        baseline = self.baseline()
        rows = copy.deepcopy(baseline['clients'])
        for row in rows.values():
            row['mono'] = 200
            for stats in row['streams'].values():
                stats['attempts'] = 40
            row['streams']['held'].update(held_broken=True, failures=10, consecutive_successes=0, open_failure_since=120, terminal_error='closed')
        self.x.ns_results(rows, {}, baseline, ['held closed'])
        result = json.loads((self.root/'ns-results.json').read_text())
        self.assertEqual(result['status'], 'FAIL')
        for row in result['directions'].values():
            self.assertEqual(row['held_session_preservation'], 'FAIL')
            self.assertEqual(row['streams']['new']['status'], 'PASS')
            self.assertIsNone(row['streams']['held']['longest_recovered_failure_seconds'])

    def test_first_terminal_event_is_carried_into_results_without_changing_failure(self):
        baseline = self.baseline()
        rows = copy.deepcopy(baseline['clients'])
        event = dict(error='socket closed', exception='EOFError', operation='receive',
                     errno=None, mono=120, ts=1700000000, sequence=25)
        for row in rows.values():
            row['mono'] = 200
            for stats in row['streams'].values():
                stats['attempts'] = 40
            row['streams']['held'].update(held_broken=True, consecutive_successes=0,
                failures=10, open_failure_since=120, terminal_error='socket closed', terminal_event=event)
        self.x.ns_results(rows, {}, baseline, ['held closed'])
        result = json.loads((self.root/'ns-results.json').read_text())
        self.assertEqual(result['status'], 'FAIL')
        for row in result['directions'].values():
            self.assertEqual(row['streams']['held']['terminal_event'], event)
            self.assertTrue(row['streams']['held']['held_broken'])
            self.assertEqual(row['streams']['new']['status'], 'PASS')

    def test_historical_probe_without_terminal_event_still_builds_honest_result(self):
        baseline = self.baseline()
        rows = copy.deepcopy(baseline['clients'])
        for row in rows.values():
            row['mono'] = 200
            for stats in row['streams'].values():
                stats.pop('terminal_event')
                stats['attempts'] = 40
            row['streams']['held'].update(held_broken=True, consecutive_successes=0,
                failures=10, open_failure_since=120,
                terminal_error='held socket already closed; reconnect prohibited')
        self.x.ns_results(rows, {}, baseline, ['held closed'])
        result = json.loads((self.root/'ns-results.json').read_text())
        self.assertEqual(result['status'], 'FAIL')
        for row in result['directions'].values():
            self.assertIsNone(row['streams']['held']['terminal_event'])

    def test_guest_restart_cannot_be_reported_as_same_epoch(self):
        baseline = self.baseline()
        rows = copy.deepcopy(baseline['clients'])
        rows['snat']['boot'] = 'new-boot'
        self.x.ns_results(rows, {}, baseline)
        result = json.loads((self.root/'ns-results.json').read_text())
        self.assertEqual(result['status'], 'FAIL')
        self.assertIsNone(result['directions']['snat']['streams']['held']['attempts'])

    def test_only_instance_quota_is_changed(self):
        cloud = Mock()
        cloud.session.get_project_id.return_value = 'project'
        cloud.compute.get_quota_set.side_effect = [NS(instances=10, cores=20,ram=50000), NS(instances=20,cores=20,ram=50000)]
        quota20(cloud, self.root)
        cloud.compute.update_quota_set.assert_called_once_with('project', project_id='project', instances=20)
        self.assertEqual(json.loads((self.root/'quota-instances.json').read_text())['status'], 'PASS')

    def test_changed_cpu_quota_fails_verification(self):
        cloud = Mock()
        cloud.session.get_project_id.return_value = 'project'
        cloud.compute.get_quota_set.side_effect = [NS(instances=10,cores=20,ram=50000), NS(instances=20,cores=40,ram=50000)]
        with self.assertRaisesRegex(RuntimeError, 'only instances'):
            quota20(cloud, self.root)


class NetplanTests(unittest.TestCase):
    def test_bridge_retains_mac_ip_and_does_not_modify_management(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            eth0 = {'dhcp4':True, 'mtu':1500}
            (root/'50-cloud-init.yaml').write_text(yaml.safe_dump({'network':{'version':2,'ethernets':{'eth0':eth0,'eth1':{'dhcp4':True}}}}))
            netplan_update(root, {'address':'10.1.0.90/24','mtu':1450}, 'fa:16:3e:00:00:01')
            old = yaml.safe_load((root/'50-cloud-init.yaml').read_text())
            new = yaml.safe_load((root/'90-ns-underlay.yaml').read_text())
            self.assertEqual(old['network']['ethernets'], {'eth0':eth0})
            bridge = new['network']['bridges']['br-underlay']
            self.assertEqual(bridge['addresses'], ['10.1.0.90/24'])
            self.assertEqual(bridge['macaddress'], 'fa:16:3e:00:00:01')
            self.assertEqual(bridge['mtu'], 1450)
            netplan_update(root, {'address':'10.1.0.90/24','mtu':1450}, 'fa:16:3e:00:00:01')
            self.assertEqual(yaml.safe_load((root/'90-ns-underlay.yaml').read_text()), new)

    def test_wildcard_covering_eth1_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'net.yaml').write_text(yaml.safe_dump({'network':{'ethernets':{'all':{'match':{'name':'eth*'},'dhcp4':True}}}}))
            with self.assertRaisesRegex(RuntimeError, 'wildcard'):
                netplan_update(root, {'address':'10.1.0.90/24','mtu':1450}, 'fa:16:3e:00:00:01')


class TCPTests(unittest.TestCase):
    def probe(self, mode):
        listener = socket.socket()
        listener.bind(('127.0.0.1',0))
        listener.listen()
        listener.settimeout(.05)
        stop = threading.Event()
        def handle(sock):
            wire = Wire(sock)
            connection = str(sock.getpeername()[1])
            with sock:
                while not stop.is_set():
                    try:
                        row=wire.line(time.monotonic()+1)
                        if mode == 'close': return
                        if mode == 'reset':
                            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
                            return
                        if mode == 'delay' and row['seq']==2: time.sleep(.12)
                        row.update(server='expected',server_boot='boot',listener='listener',connection=connection,
                                   peer='127.0.0.2' if mode=='wrong-source' else '127.0.0.1')
                        sock.sendall((json.dumps(row)+'\n').encode())
                    except (OSError,EOFError,ValueError): return
        def server():
            while not stop.is_set():
                try:
                    sock,_=listener.accept()
                    threading.Thread(target=handle,args=(sock,),daemon=True).start()
                except socket.timeout: pass
                except OSError: return
        thread=threading.Thread(target=server,daemon=True);thread.start()
        cfg=dict(run='r',source='127.0.0.1',target='127.0.0.1',port=listener.getsockname()[1],
                 expected_peer='127.0.0.1',expected_server='expected',timeout=.08,interval=.05)
        client=Client(cfg)
        workers=[threading.Thread(target=client.stream,args=(name,),daemon=True) for name in ('held','new')]
        for worker in workers: worker.start()
        time.sleep(.65)
        client.stop.set()
        for worker in workers: worker.join(2)
        stop.set();listener.close();thread.join(1)
        return {name:stats.snapshot() for name,stats in client.stats.items()}

    def test_normal_held_is_one_socket_and_new_is_multiple(self):
        result=self.probe('normal')
        self.assertEqual(result['held']['connection_attempts'],1)
        self.assertGreater(result['new']['connection_attempts'],3)
        self.assertEqual(result['held']['failures'],0)
        self.assertEqual(result['held']['first_session'],result['held']['last_session'])

    def test_timeout_recovers_on_same_held_socket(self):
        result=self.probe('delay')
        self.assertEqual(result['held']['connection_attempts'],1)
        self.assertFalse(result['held']['held_broken'])
        self.assertGreater(result['held']['failures'],0)
        self.assertIsNone(result['held']['open_failure_since'])
        self.assertGreater(result['held']['longest_recovered_failure_seconds'],0)
        self.assertIsNone(result['held']['terminal_event'])

    def test_closed_held_socket_never_reconnects(self):
        result=self.probe('close')
        self.assertEqual(result['held']['connection_attempts'],1)
        self.assertTrue(result['held']['held_broken'])
        self.assertGreater(result['new']['connection_attempts'],3)

    def test_closed_held_socket_keeps_first_eof_after_repeated_failures(self):
        result = self.probe('close')['held']
        self.assertGreater(result['failures'], 2)
        self.assertEqual(result['connection_attempts'], 1)
        self.assertEqual(result['terminal_error'], 'socket closed')
        event = result['terminal_event']
        self.assertEqual(event['exception'], 'EOFError')
        self.assertEqual(event['operation'], 'receive')
        self.assertEqual(event['sequence'], 1)
        self.assertIsNone(event['errno'])
        self.assertGreater(event['mono'], 0)
        self.assertGreater(event['ts'], 0)
        self.assertEqual(event['socket_local'][0], '127.0.0.1')
        self.assertEqual(event['socket_peer'][0], '127.0.0.1')

    def test_tcp_reset_records_original_os_error_and_never_reconnects_held(self):
        result = self.probe('reset')['held']
        self.assertGreater(result['failures'], 2)
        self.assertEqual(result['connection_attempts'], 1)
        self.assertTrue(result['held_broken'])
        event = result['terminal_event']
        self.assertIn(event['exception'], ('ConnectionResetError', 'BrokenPipeError'))
        self.assertIn(event['operation'], ('receive', 'send'))
        self.assertGreater(event['errno'], 0)
        self.assertEqual(event['error'], result['terminal_error'])
        self.assertNotIn('reconnect prohibited', result['terminal_error'])

    def test_unexpected_source_is_not_success(self):
        result=self.probe('wrong-source')
        self.assertEqual(result['held']['successes'],0)
        self.assertEqual(result['new']['successes'],0)
        self.assertEqual(result['held']['terminal_event']['operation'], 'validate_echo')


if __name__ == '__main__':
    unittest.main()
