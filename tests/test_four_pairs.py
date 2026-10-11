"""Cross-compute scheduling, D ownership/gates and real TCP lifecycle regressions."""
import base64
import copy
import json
import pathlib
import socket
import subprocess
import tempfile
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import yaml
from test_three_pairs import Scenario
from test_validation import ROOT, ready_evidence, v
from tcp_guest_probe import LineReader, Probe, Stats
from tcp_validation import PairD
from validation_placement import check, create_on_target, resolve


def target(i):
    return dict(inventory_host='compute'+str(i+1), nova_host='nova'+str(i+1),
                neutron_host='ovs'+str(i+1), hypervisor='hv'+str(i+1), availability_zone='nova')


class PlacementTests(unittest.TestCase):
    def cloud(self):
        from openstack.compute.v2.service import Service
        from openstack.compute.v2.hypervisor import Hypervisor
        from openstack.network.v2.agent import Agent
        services = [Service(host='node'+str(i), zone='nova', status='enabled', state='up') for i in (1,2)]
        hypervisors = [Hypervisor(hypervisor_hostname='hv'+str(i), service={'host':'node'+str(i)},
                                 status='enabled', state='up') for i in (1,2)]
        agents = [Agent(host='node'+str(i), alive=True, admin_state_up=True) for i in (1,2)]
        cloud = NS(compute=Mock(), network=Mock())
        cloud.compute.services.return_value = services
        cloud.compute.hypervisors.return_value = hypervisors
        cloud.network.agents.return_value = agents
        return cloud

    def request(self):
        return dict(pair_hosts=['compute1','compute2'], compute_hosts=['compute1','compute2'],
                    identities={'compute1':['node1','node1.domain'], 'compute2':['node2','node2.domain']})

    def test_resolve_inventory_aliases_with_real_sdk_resource_attributes(self):
        result = resolve(self.cloud(), self.request())
        self.assertEqual(result['0']['nova_host'], 'node1')
        self.assertEqual(result['1']['hypervisor'], 'hv2')
        self.assertEqual(result['0']['inventory_host'], 'compute1')

    def test_ambiguous_or_unhealthy_hosts_fail_before_writes(self):
        for fault in ('ambiguous', 'disabled', 'dead_ovs', 'missing_hypervisor', 'same_host'):
            with self.subTest(fault=fault):
                cloud, request = self.cloud(), self.request()
                if fault == 'ambiguous':
                    cloud.compute.services.return_value.append(copy.copy(cloud.compute.services.return_value[0]))
                if fault == 'disabled':
                    cloud.compute.services.return_value[0].status = 'disabled'
                if fault == 'dead_ovs':
                    cloud.network.agents.return_value[0].is_alive = False
                if fault == 'missing_hypervisor':
                    cloud.compute.hypervisors.return_value.pop()
                if fault == 'same_host':
                    request['pair_hosts'] = ['compute1', 'compute1']
                with self.assertRaises(RuntimeError):
                    resolve(cloud, request)

    def test_host_hypervisor_and_binding_must_all_match(self):
        from openstack.compute.v2.server import Server
        from openstack.network.v2.port import Port
        server = Server(**{'OS-EXT-SRV-ATTR:host':'nova1', 'OS-EXT-SRV-ATTR:hypervisor_hostname':'hv1'})
        port = Port(**{'binding:host_id':'ovs1'})
        self.assertEqual(check(server, port, target(0))['status'], 'PASS')
        for field in ('compute_host', 'hypervisor_hostname'):
            old = getattr(server, field)
            setattr(server, field, 'wrong')
            self.assertEqual(check(server, port, target(0))['status'], 'FAIL')
            setattr(server, field, old)
        port.binding_host_id = 'ovs2'
        self.assertEqual(check(server, port, target(0))['status'], 'FAIL')

    def test_sdk_request_uses_supported_destination_body(self):
        compute = Mock()
        response = Mock(status_code=202)
        response.json.return_value = {'server': {'id':'new-server'}}
        compute.post.return_value = response
        result = create_on_target(compute, target(0), name='vm', image_id='image', flavor_id='flavor',
            networks=[{'port':'owned-port'}], metadata={'ovn_migration_run':'run'}, user_data='encoded')
        self.assertEqual(result.id, 'new-server')
        compute.post.assert_called_once_with('/servers', microversion='2.74', json={'server': {
            'name':'vm', 'imageRef':'image', 'flavorRef':'flavor', 'networks':[{'port':'owned-port'}],
            'metadata':{'ovn_migration_run':'run'}, 'user_data':'encoded',
            'host':'nova1', 'hypervisor_hostname':'hv1'}})
        compute.create_server.assert_not_called()


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.s = Scenario(self.root)
        self.obj = self.s.obj
        self.obj.cfg.update(placement_enabled=True, placement={str(i):target(i) for i in (0,1)},
            pair_d_enabled=True, tcp_old_port=18080, tcp_new_port=18081, tcp_control_port=18082,
            tcp_interval=.5, tcp_timeout=1, pair_d_network_host='network1', inventory='/inventory')
        for topology in ('pre', 'post'):
            for role in ('measure', 'existing', 'fresh'):
                for key, vm in self.obj.state[topology].get(role, {}).items():
                    dest = target(int(key))
                    vm['placement'] = dest
                    self.s.servers[vm['server']].compute_host = dest['nova_host']
                    self.s.servers[vm['server']].hypervisor_hostname = dest['hypervisor']
                    self.s.ports[vm['port']].binding_host_id = dest['neutron_host']
        self.obj.commit()

    def provision_d(self):
        self.obj.cloud.compute.servers.return_value = []
        rules = list(self.obj.cloud.network.security_group_rules.return_value)
        self.obj.cloud.network.security_group_rules.side_effect = lambda **_:rules
        def create_rule(**kw):
            rule = NS(remote_group_id=None, remote_ip_prefix=None)
            rule.__dict__.update(kw)
            rules.append(rule)
            return rule
        self.obj.cloud.network.create_security_group_rule.side_effect = create_rule
        def create_port(**kw):
            key = kw['name'][-1]
            fixed = [{'subnet_id':'pre-sub'+key, 'ip_address':'10.0.'+key+'.20'}]
            port = NS(id='tcp-port'+key, name=kw['name'], fixed_ips=fixed, security_group_ids=['pre-sg'],
                      device_id='', status='ACTIVE', binding_vif_type='ovs', binding_host_id=target(int(key))['neutron_host'])
            self.s.ports[port.id] = port
            return port
        def create_server(path, *, microversion, **request):
            self.assertEqual(path, '/servers')
            self.assertEqual(microversion, '2.74')
            kw = request['json']['server']
            key = kw['name'][-1]
            self.assertEqual(kw['host'], target(int(key))['nova_host'])
            self.assertEqual(kw['hypervisor_hostname'], target(int(key))['hypervisor'])
            checkpoint = v.read_evidence(self.root, 'validation-resources.json')
            self.assertEqual(checkpoint['pre']['tcp'][key]['port'], kw['networks'][0]['port'])
            server = NS(id='tcp'+key, status='ACTIVE', metadata=kw['metadata'],
                        compute_host=kw['host'], hypervisor_hostname=kw['hypervisor_hostname'])
            self.s.servers[server.id] = server
            self.s.ports[kw['networks'][0]['port']].device_id = server.id
            data = yaml.safe_load(base64.b64decode(kw['user_data']))
            config = json.loads(next(f['content'] for f in data['write_files'] if f['path']=='/etc/migration-probe.json'))
            self.assertEqual(config['tcp_role'], 'client' if key=='0' else 'server')
            self.assertIn(['systemctl','enable','--now','tcp-migration-probe'], data['runcmd'])
            response = Mock(status_code=202)
            response.json.return_value = {'server': {'id':server.id}}
            return response
        self.obj.cloud.network.create_port.side_effect = create_port
        self.obj.cloud.compute.post.side_effect = create_server
        self.obj.create('tcp')

    def test_d_reuses_pre_topology_and_retries_never_create_duplicates(self):
        self.provision_d()
        self.obj.create('tcp')
        self.assertEqual(self.obj.cloud.compute.post.call_count, 2)
        self.obj.cloud.compute.create_server.assert_not_called()
        self.assertEqual(self.obj.cloud.network.create_port.call_count, 2)
        self.obj.cloud.network.create_network.assert_not_called()
        self.obj.cloud.network.create_subnet.assert_not_called()
        self.obj.cloud.network.create_router.assert_not_called()
        self.assertEqual(set(self.obj.state['pre']['tcp']), {'0','1'})
        self.assertEqual(self.obj.cloud.network.create_security_group_rule.call_count, 4)

    def test_disabled_d_does_not_touch_api_or_trigger(self):
        self.obj.cfg['pair_d_enabled'] = False
        self.obj.create('tcp')
        d = PairD(self.obj)
        d.wait(); d.arm()
        self.obj.cloud.compute.create_server.assert_not_called()
        self.obj.cloud.compute.post.assert_not_called()
        self.obj.cloud.network.create_port.assert_not_called()
        self.assertEqual(d.read('pair-d-tcp.json')['status'], 'DISABLED')

    def test_recovery_of_lost_nova_reply_reuses_matching_owned_server(self):
        self.provision_d()
        self.obj.state['pre']['tcp']['0'].pop('server')
        self.obj.cloud.compute.servers.return_value = [NS(id='tcp0', name=self.obj.state['pre']['tcp']['0']['name'],
            metadata={'ovn_migration_run':'run', 'ovn_validation_role':'tcp'})]
        self.obj.create('tcp')
        self.assertEqual(self.obj.cloud.compute.post.call_count, 2)
        self.assertEqual(self.obj.state['pre']['tcp']['0']['server'], 'tcp0')

    def test_retry_cannot_change_placement(self):
        self.obj.cfg['placement']['0'] = target(1)
        with self.assertRaisesRegex(RuntimeError, 'placement'):
            self.obj.create('measure')
        self.obj.cloud.compute.create_server.assert_not_called()

    def test_rejected_first_pair_a_create_retries_saved_ports_without_duplicate_topology(self):
        from openstack.exceptions import BadRequestException
        from requests import Response
        pair = self.obj.state['pre']['measure']
        for vm in pair.values():
            self.s.servers.pop(vm.pop('server'))
            self.s.ports[vm['port']].device_id = ''
        self.obj.commit()
        self.obj.cloud.compute.servers.return_value = []
        failed = Response()
        failed.status_code = 400
        failed.headers['Content-Type'] = 'application/json'
        failed._content = json.dumps({'badRequest': {'message':'unexpected response-only hypervisor key'}}).encode()
        self.obj.cloud.compute.post.return_value = failed
        before = {k:(vm['port'],vm['network'],vm['subnet']) for k,vm in pair.items()}
        with self.assertRaises(BadRequestException):
            self.obj.create('measure')
        self.assertTrue(all('server' not in vm for vm in pair.values()))
        def accepted(path, *, microversion, **request):
            data = request['json']['server']; key = data['name'][-1]
            server = NS(id='measure'+key, status='ACTIVE', metadata=data['metadata'],
                        compute_host=data['host'], hypervisor_hostname=data['hypervisor_hostname'])
            self.s.servers[server.id] = server
            self.s.ports[data['networks'][0]['port']].device_id = server.id
            response = Mock(status_code=202)
            response.json.return_value = {'server': {'id':server.id}}
            return response
        self.obj.cloud.compute.post.side_effect = accepted
        self.obj.create('measure'); self.obj.create('measure')
        self.assertEqual(self.obj.cloud.compute.post.call_count, 3)  # one rejected, two accepted
        self.assertEqual(before, {k:(vm['port'],vm['network'],vm['subnet']) for k,vm in pair.items()})
        self.obj.cloud.network.create_port.assert_not_called()
        self.obj.cloud.network.create_network.assert_not_called()
        self.obj.cloud.network.create_router.assert_not_called()
        self.obj.cloud.compute.create_server.assert_not_called()

    def test_wrong_nova_or_binding_host_blocks_workload_audit(self):
        self.s.ports['measure-port0'].binding_host_id = 'ovs2'
        with self.assertRaisesRegex(RuntimeError, 'binding host'):
            self.obj.audit_placement(['measure','pre'])
        self.assertEqual(v.read_evidence(self.root,'workload-placement.json')['status'], 'FAIL')

    def test_cleanup_eight_vms_keeps_shared_topology_until_all_roles_deleted(self):
        self.provision_d()
        live = dict(self.s.servers)
        self.obj.cloud.compute.find_server.side_effect = lambda uuid:live.get(uuid)
        self.obj.cloud.compute.delete_server.side_effect = lambda uuid, **kw:live.pop(uuid, None)
        self.obj.cleanup('post'); self.obj.cleanup('pre'); self.obj.cleanup('pre')
        deleted = [c.args[0] for c in self.obj.cloud.compute.delete_server.call_args_list]
        self.assertEqual(len(deleted), 8)
        self.assertEqual(set(deleted), set(self.s.servers))
        self.assertEqual(self.obj.cloud.network.delete_network.call_count, 4)
        self.assertEqual(self.obj.cloud.network.delete_router.call_count, 2)

    def test_cleanup_refuses_foreign_d_owner(self):
        self.provision_d()
        self.s.servers['tcp0'].metadata['ovn_migration_run'] = 'foreign'
        self.obj.cloud.compute.find_server.side_effect = lambda uuid:self.s.servers.get(uuid)
        with self.assertRaisesRegex(RuntimeError, 'ownership'):
            self.obj.cleanup('pre')
        self.obj.cloud.compute.delete_server.assert_not_called()
        self.obj.cloud.network.delete_network.assert_not_called()

    def test_d_and_placement_are_required_only_when_enabled(self):
        ready_evidence(self.root)
        self.obj.state['historical_dual_pair'] = True
        self.obj.commit()
        v.save(self.root/'validation-config.json', self.obj.cfg)
        self.assertFalse(v.validation_ready(self.root))
        v.save(self.root/'workload-placement.json', {'status':'PASS'})
        self.assertFalse(v.validation_ready(self.root))
        v.save(self.root/'pair-d-tcp.json', {'status':'PASS'})
        self.assertTrue(v.validation_ready(self.root))
        self.obj.cfg['pair_d_enabled'] = False
        v.save(self.root/'validation-config.json', self.obj.cfg)
        v.save(self.root/'pair-d-tcp.json', {'status':'DISABLED'})
        self.assertTrue(v.validation_ready(self.root))

    def test_d_arm_requires_freeze_and_uses_no_neutron_api(self):
        self.provision_d()
        d = PairD(self.obj)
        with self.assertRaisesRegex(RuntimeError, 'freeze window'):
            d.arm()
        (self.root/'metrics').mkdir()
        (self.root/'metrics/control_plane_downtime.start').write_text('1')
        d.save('pair-d-baseline.json', {'session_id':'session'})
        d.save('pair-d-trigger-ack.json', {'status':'PASS','run':'run','armed':True,'armed_mono':2,'session_id':'session'})
        self.obj.cloud.network.get_port.side_effect = AssertionError('Neutron API is frozen')
        with patch('tcp_validation.subprocess.run', return_value=NS(returncode=0,stdout='',stderr='')) as call:
            d.arm(); d.arm()
            self.assertEqual(call.call_count, 1)
        self.assertEqual(d.read('pair-d-trigger.json')['status'], 'PASS')


class TCPWireTests(unittest.TestCase):
    def ports(self):
        sockets = [socket.socket() for _ in range(3)]
        try:
            for s in sockets: s.bind(('127.0.0.1', 0))
            return [s.getsockname()[1] for s in sockets]
        finally:
            for s in sockets: s.close()

    def wait_for(self, predicate):
        deadline = time.monotonic()+4
        while time.monotonic()<deadline:
            if predicate(): return
            time.sleep(.02)
        self.fail('TCP socket condition did not converge')

    def test_real_sockets_hold_session_and_open_new_listener_only_on_arm(self):
        old, new, control = self.ports()
        cfg = dict(run='test', peer='127.0.0.1', server_id='server', tcp_old_port=old,
                   tcp_new_port=new, tcp_control_port=control, tcp_timeout=.15, tcp_interval=.03)
        server = Probe(dict(cfg,tcp_role='server'), lambda _:None, bind='127.0.0.1')
        client = Probe(dict(cfg,tcp_role='client'), lambda _:None, bind='127.0.0.1')
        server.start(); client.start()
        try:
            self.wait_for(lambda:client.snapshot()['total']['held']['successes'] >= 5)
            first = client.snapshot()['session_id']
            with socket.create_connection(('127.0.0.1', control), .2) as stream:
                stream.sendall(b'CHECK test\n')
                reply = json.loads(LineReader(stream).read())
                self.assertTrue(reply['ready'])
                self.assertFalse(reply['armed'])
                self.assertEqual(reply['session_id'], first)
            with self.assertRaises(OSError): socket.create_connection(('127.0.0.1', new), .1)
            ack = client.arm()
            self.wait_for(lambda:all(r['consecutive_successes'] >= 5 for r in client.snapshot()['streams'].values()))
            snapshot = client.snapshot()
            self.assertEqual(snapshot['session_id'], first)
            self.assertFalse(snapshot['held_broken'])
            self.assertTrue(server.snapshot()['listener_opened'])
            self.assertEqual(client.arm()['armed_mono'], ack['armed_mono'])
            self.assertEqual(client.snapshot()['arm_attempts'], 1)
        finally:
            client.close(); server.close()

    def test_partial_response_survives_timeout_on_same_socket(self):
        sender, receiver = socket.socketpair()
        try:
            receiver.settimeout(.02)
            reader = LineReader(receiver)
            sender.sendall(b'{"echo":')
            with self.assertRaises(socket.timeout): reader.read()
            sender.sendall(b'"ok"}\n')
            self.assertEqual(json.loads(reader.read()), {'echo':'ok'})
        finally:
            sender.close(); receiver.close()

    def test_held_peer_close_never_reconnects(self):
        peer = Mock()
        peer.recv.return_value = b''
        probe = Probe(dict(run='run',peer='127.0.0.1',tcp_old_port=1,tcp_timeout=.1,tcp_interval=.01),lambda _:None)
        probe.held_established = True
        count = 0
        def record(*_):
            nonlocal count
            count += 1
            if count == 3: probe.stop.set()
        probe.record = record
        with patch('tcp_guest_probe.socket.create_connection', return_value=peer) as connection:
            probe.held()
        self.assertTrue(probe.held_broken)
        self.assertEqual(connection.call_count, 1)

    def test_failure_duration_uses_elapsed_monotonic_time(self):
        stats = Stats()
        stats.record(False, 10, 11)
        stats.record(False, 12, 13)
        stats.record(True, 14, 14.5)
        self.assertEqual(stats.value['longest_recovered_failure_seconds'], 4.5)
        self.assertEqual(stats.value['failures'], 2)
        self.assertIsNone(stats.value['open_failure_since'])


class SummaryFenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.pair = {k:dict(server='tcp'+k,port='p'+k,fixed_ips=[k],network='n'+k,subnet='s'+k) for k in ('0','1')}
        self.obj = NS(root=self.root, cfg=dict(pair_d_enabled=True, timeout=.001,tcp_interval=.5,tcp_timeout=1),
                      pair=lambda _:self.pair)
        self.d = PairD(self.obj)
        self.d.identities = Mock(return_value={k:dict(identity='PASS',placement={'status':'PASS'}) for k in ('0','1')})
        stats = dict(attempts=9,successes=6,failures=3,consecutive_successes=6,open_failure_since=None,
                     longest_recovered_failure_seconds=1.2)
        self.current = {k:[dict(kind='tcp',server_id='tcp'+k,boot='boot'+k,mono=10,armed=True,armed_mono=5,
                               held_established=True,held_broken=False,session_id='session',listener_opened=True,
                               total={s:copy.deepcopy(stats) for s in ('held','new_old','new_listener')},
                               streams={s:copy.deepcopy(stats) for s in ('held','new_old','new_listener')})] for k in ('0','1')}
        self.counter = 0
        def collect(*_, **kw):
            self.counter += 1
            value = copy.deepcopy(self.current)
            for rows in value.values():
                if rows:
                    rows[0]['mono'] += self.counter
            return value
        self.obj.collect = collect
        self.d.save('pair-d-baseline.json', dict(boots={'0':'boot0','1':'boot1'},session_id='session',
                                               resources=copy.deepcopy(self.pair)))
        self.d.save('pair-d-trigger.json', dict(status='PASS',acknowledged_at=2,
                    ack=dict(session_id='session',armed_mono=5)))
        (self.root/'metrics').mkdir()
        (self.root/'metrics/control_plane_downtime.start').write_text('1')
        (self.root/'metrics/control_plane_downtime.end').write_text('3')

    def test_fresh_same_boot_same_session_proves_all_three_recoveries(self):
        self.d.wait(post=True)
        result = self.d.read('pair-d-tcp.json')
        self.assertEqual(result['status'], 'PASS')
        self.assertTrue(result['listener_opened_during_freeze'])
        self.assertEqual(set(result['streams']), {'held','new_old','new_listener'})
        self.assertTrue(all(r['status']=='PASS' for r in result['streams'].values()))
        self.assertAlmostEqual(result['streams']['held']['failed_probe_percent'], 100/3)

    def test_invalid_evidence_never_passes(self):
        for fault in ('reboot','new_session','broken','changed_uuid','stale','unarmed','missing_trigger',
                      'late_listener','wrong_binding','missing_server','unrecovered'):
            with self.subTest(fault=fault):
                self.setUp()
                if fault=='reboot': self.current['0'][0]['boot']='new-boot'
                if fault=='new_session': self.current['0'][0]['session_id']='new-session'
                if fault=='broken': self.current['0'][0]['held_broken']=True
                if fault=='changed_uuid': self.pair['0']['port']='new-port'
                if fault=='stale': self.obj.collect=lambda *_,**kw:copy.deepcopy(self.current)
                if fault=='unarmed': self.current['0'][0]['armed']=False
                if fault=='missing_trigger': (self.root/'pair-d-trigger.json').unlink()
                if fault=='late_listener': self.d.save('pair-d-trigger.json', dict(status='PASS',acknowledged_at=4,ack=dict(session_id='session',armed_mono=5)))
                if fault=='wrong_binding': self.d.identities.return_value['0']['placement']['status']='FAIL'
                if fault=='missing_server': self.current['1']=[]
                if fault=='unrecovered': self.current['0'][0]['streams']['new_listener']['open_failure_since']=8
                with patch('tcp_validation.time.sleep'), self.assertRaises(RuntimeError):
                    self.d.wait(post=True)
                self.assertNotEqual(self.d.read('pair-d-tcp.json')['status'],'PASS')

    def test_partially_available_initial_console_latches_each_guest(self):
        (self.root/'pair-d-baseline.json').unlink()
        for rows in self.current.values():
            rows[0].update(armed=False, listener_opened=False)
        normal = self.obj.collect
        self.obj.cfg['timeout'] = 1
        def partial(*args,**kw):
            value = normal(*args,**kw)
            if self.counter == 1: value['1']=[]
            return value
        self.obj.collect = partial
        with patch('tcp_validation.time.sleep'):
            self.d.wait()
        self.assertEqual(self.d.read('pair-d-readiness.json')['status'], 'PASS')
        self.assertEqual(set(self.d.read('pair-d-baseline.json')['resources']), {'0','1'})


class YAMLIntegrationTests(unittest.TestCase):
    def test_default_enabled_and_no_lab_uuid(self):
        defaults = yaml.safe_load((ROOT/'group_vars/all.yml').read_text())
        self.assertTrue(defaults['validation_pair_d_enabled'])
        self.assertEqual(defaults['target_geneve_mtu'], 1392)
        self.assertTrue(defaults['validation_allow_pre_cutover_guest_reboot'])
        self.assertTrue(defaults['validation_allow_post_cutover_guest_reboot'])
        self.assertIn("default('', true)", defaults['validation_image'])
        self.assertIn("default('', true)", defaults['validation_flavor'])
        self.assertFalse((ROOT/'migration-lab.yml').exists())

    def test_trigger_is_between_freeze_and_db_migration(self):
        plays = yaml.safe_load((ROOT/'playbooks/07-migrate-db.yml').read_text())
        freeze = next(i for i,p in enumerate(plays) if any('ansible.builtin.systemd_service' in t for t in p['tasks']))
        trigger = next(i for i,p in enumerate(plays) if any('d-arm ' in t.get('ansible.builtin.shell','') for t in p['tasks']))
        migrate = next(i for i,p in enumerate(plays) if 'provider_association_container_command' in p.get('vars',{}))
        self.assertLess(freeze, trigger); self.assertLess(trigger, migrate)
        self.assertTrue(plays[trigger]['any_errors_fatal'])


if __name__ == '__main__':
    unittest.main()
