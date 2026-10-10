"""Offline Work Item 1 tests. No OpenStack, SSH or guest services are used."""
import copy
import contextlib
import hashlib
import io
import json
import pathlib
import struct
import subprocess
import sys
import tempfile
import unittest
import uuid
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import yaml

try:
    from openstack.network.v2.security_group_rule import SecurityGroupRule
except ImportError:
    SecurityGroupRule = None

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import ew_provision as ew


class FakeCloud:
    def __init__(self, spec):
        self.spec = spec; self.rows = {k: [] for k in ('image', 'flavor', 'network', 'subnet', 'router', 'security_group', 'port', 'server', 'rule', 'keypair')}
        self.created = []; self.waits = []; self.on_wait = None
        self.image = NS(images=lambda **kw: self.list('image', **kw), create_image=self.image_create)
        self.compute = NS(flavors=lambda **kw: self.list('flavor'), create_flavor=lambda **kw: self.add('flavor', **kw, is_disabled=False, extra_specs={}),
            find_keypair=lambda name, **kw: next(iter(self.list('keypair', name=name)), None),
            create_keypair=lambda **kw: self.add('keypair', **kw), fetch_flavor_extra_specs=lambda row: row,
            servers=lambda **kw: self.list('server', **kw), create_server=self.server_create,
            get_server=lambda key: self.get('server', key), wait_for_server=self.wait)
        self.network = NS(**{plural: (lambda kind: lambda **kw: self.list(kind, **kw))(kind)
            for plural, kind in [('networks', 'network'), ('subnets', 'subnet'), ('routers', 'router'),
                                 ('security_groups', 'security_group'), ('ports', 'port'), ('security_group_rules', 'rule')]})
        for method, kind in [('create_network', 'network'), ('create_subnet', 'subnet'), ('create_router', 'router'),
                              ('create_security_group', 'security_group'), ('create_port', 'port'), ('create_security_group_rule', 'rule')]:
            setattr(self.network, method, (lambda kind: lambda **kw: self.network_create(kind, **kw))(kind))
        self.network.add_interface_to_router = self.attach
        self.network.get_port = lambda key: self.get('port', key)

    def list(self, kind, **filters):
        filters = {k: v for k, v in filters.items() if k not in ('all_projects', 'details')}
        return [r for r in self.rows[kind] if all(getattr(r, k, None) == v for k, v in filters.items())]

    def get(self, kind, key):
        return next((r for r in self.rows[kind] if r.id == key), None)

    def add(self, kind, **kw):
        row = NS(id=str(uuid.uuid4()), **kw); self.rows[kind].append(row); self.created.append((kind, row))
        return row

    def image_create(self, **kw):
        props = {k: kw.pop(k) for k in ('os_distro', 'os_version')}
        for key in ('filename', 'allow_duplicates', 'wait', 'timeout'): kw.pop(key)
        return self.add('image', **kw, status='active', hash_algo='sha256', hash_value=self.spec['image']['sha256'], properties=props)

    def network_create(self, kind, **kw):
        if kind == 'network': kw['subnet_ids'] = []
        if kind == 'router': kw['external_gateway_info'] = {}
        if kind == 'port': kw.update(device_id='', device_owner='', status='DOWN', mac_address='fa:16:3e:00:00:01', allowed_address_pairs=[])
        if kind == 'rule': kw.setdefault('remote_group_id', None)
        row = self.add(kind, **kw)
        if kind == 'subnet': self.get('network', row.network_id).subnet_ids.append(row.id)
        return row

    def attach(self, router, subnet_id):
        sub = self.get('subnet', subnet_id)
        return self.add('port', name='', network_id=sub.network_id, device_id=router, device_owner='network:router_interface',
                        fixed_ips=[dict(subnet_id=sub.id, ip_address=sub.gateway_ip)])

    def server_create(self, **kw):
        port = self.get('port', kw.pop('networks')[0]['port'])
        row = self.add('server', name=kw['name'], image={'id': kw['image_id']}, flavor={'id': kw['flavor_id']},
                       key_name=kw['key_name'], compute_host=kw['host'], availability_zone=kw['availability_zone'],
                       has_config_drive=kw['has_config_drive'], status='BUILD')
        port.device_id = row.id; port.status = 'ACTIVE'
        return row

    def wait(self, server, **kw):
        self.waits.append((server.id, kw))
        if self.on_wait: self.on_wait(server)
        server.status = 'ACTIVE'
        return server


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = pathlib.Path(self.temp.name); self.root = self.path / 'run'; self.root.mkdir()
        self.state = self.path / 'persistent'; self.state.mkdir()
        defaults = yaml.safe_load((ROOT / 'group_vars/all.yml').read_text())
        self.cfg = defaults['ew_workload_config']; self.spec = copy.deepcopy(defaults['ew_provision_spec'])
        image = self.path / 'netfix.qcow2'; image.write_bytes(b'QFI\xfb' + b'\0' * 20 + struct.pack('!Q', 10 * 1024**3))
        self.spec['image'].update(path=str(image), sha256=hashlib.sha256(image.read_bytes()).hexdigest())
        self.spec['image'].pop('sha256_file')
        public = self.path / 'ew-lab.pub'; public.write_text('ssh-ed25519 TEST')
        self.spec['keypair_public_key'] = str(public)
        self.spec['secrets'] = {}
        for kind in ('db', 'mq'):
            secret = self.path / kind; secret.write_text('a' * 48); secret.chmod(0o600)
            self.spec['secrets'][kind] = str(secret)
        self.spec['bootstrap'] = {}
        for role in ('ew-db', 'ew-queue'):
            script = self.path / (role + '.sh'); script.write_text('#!/bin/bash\nset -euo pipefail\n')
            self.spec['bootstrap'][role] = dict(path=str(script), sha256=ew.digest(script))
        (self.root / 'ew-config.json').write_text(json.dumps(self.cfg))
        (self.root / 'mtu-calculation.json').write_text(json.dumps(dict(validation_source_mtu=1400)))
        (self.root / 'ew-measurement-config.json').write_text(json.dumps(dict(tcp_ports=[18080, 18081],
            guest_key='/private/key', guest_known_hosts=str(self.path / 'known_hosts'))))
        self.cloud = FakeCloud(self.spec)
        self.p = ew.Provisioner(self.cloud, self.root, self.spec, self.state)
        self.app_vm = dict(server='app-server', port='app-port', ip='192.168.101.11')
        self.keypatch = patch.object(ew.subprocess, 'check_output', return_value='ssh-ed25519 TEST')
        self.keypatch.start(); self.addCleanup(self.keypatch.stop)

    def create(self):
        found, _ = self.p.plan(10 * 1024**3); self.p.apply(found)

    def validated(self, require_bootstrap=True):
        info = json.dumps({'format': 'qcow2', 'virtual-size': 10 * 1024**3})
        with patch.object(ew.subprocess, 'check_output', return_value=info), patch.object(ew.subprocess, 'run'):
            return ew.validated_spec(self.spec, self.cfg, str(self.path / 'backups'), require_bootstrap)

    def test_empty_cloud_creates_exact_six_and_fixed_placement(self):
        self.create()
        self.assertEqual(len(self.cloud.rows['server']), 6)
        for vm in self.cfg['servers']:
            server = self.cloud.list('server', name=vm['name'])[0]
            port = self.cloud.list('port', device_id=server.id)[0]
            self.assertEqual(server.compute_host, vm['compute_host'])
            self.assertTrue(server.has_config_drive)
            self.assertEqual(port.fixed_ips[0]['ip_address'], vm['ip'])
        self.assertEqual([n.mtu for n in self.cloud.rows['network']], [1400, 1400])
        self.assertEqual(len(self.cloud.rows['port']), 8)  # six explicit ports + router interfaces

    def test_second_run_no_creation_and_identical_uuid_checkpoint(self):
        self.create(); before = (self.state / 'resources.json').read_bytes(); count = len(self.cloud.created)
        p = ew.Provisioner(self.cloud, self.root, self.spec, self.state)
        found, missing = p.plan(10 * 1024**3); self.assertEqual(missing, [])
        p.apply(found)
        self.assertEqual(len(self.cloud.created), count)
        self.assertEqual((self.state / 'resources.json').read_bytes(), before)
        self.assertFalse(p.changed)

    def test_existing_unrestricted_rules_not_duplicated(self):
        self.create(); count = len(self.cloud.rows['rule'])
        self.assertTrue(all(r.port_range_min is None for r in self.cloud.rows['rule']))
        self.p.apply(self.p.plan(10 * 1024**3)[0])
        self.assertEqual(len(self.cloud.rows['rule']), count)

    def test_legacy_numeric_port_ranges_also_cover_required_ports(self):
        self.create()
        for rule in self.cloud.rows['rule']:
            if rule.protocol in ('tcp', 'udp'): rule.port_range_min = 1; rule.port_range_max = 65535
        self.assertTrue(self.p.rules_ready(self.cloud.rows['rule']))

    def sdk_rules(self):
        # Neutron response names, deliberately independent of rules() and the
        # evidence serializer. Real resources expose ether_type, not ethertype.
        sg = self.cloud.rows['security_group'][0]
        rows = [dict(direction='ingress', ethertype='IPv4', protocol=protocol,
                     remote_ip_prefix=net['cidr'])
                for net in self.spec['networks'].values() for protocol in ('icmp', 'tcp', 'udp')]
        rows += [dict(direction='egress', ethertype=family, protocol=None, remote_ip_prefix=None)
                 for family in ('IPv4', 'IPv6')]
        return [SecurityGroupRule(id=str(uuid.uuid4()), security_group_id=sg.id,
                    port_range_min=None, port_range_max=None, remote_group_id=None, **row) for row in rows]

    @unittest.skipIf(SecurityGroupRule is None, 'openstacksdk required for real security-group resources')
    def test_sdk_unrestricted_ingress_and_both_egress_families_are_required(self):
        self.create(); rules = self.sdk_rules()
        self.assertFalse(hasattr(rules[0], 'ethertype'))
        self.assertTrue(self.p.rules_ready(rules))
        for family in ('IPv4', 'IPv6'):
            with self.subTest(missing_egress=family):
                self.assertFalse(self.p.rules_ready([r for r in rules if not
                    (r.direction == 'egress' and r.ether_type == family)]))
        for r in rules:
            if r.protocol == 'tcp': r.port_range_min = 22; r.port_range_max = 22
        self.assertFalse(self.p.rules_ready(rules))
        rules = self.sdk_rules()
        for r in rules:
            if r.direction == 'ingress': r.ether_type = 'IPv6'
        self.assertFalse(self.p.rules_ready(rules))

    @unittest.skipIf(SecurityGroupRule is None, 'openstacksdk required for real security-group resources')
    def test_sdk_existing_unrestricted_group_reused_without_mutation(self):
        self.create(); self.cloud.rows['rule'] = self.sdk_rules()
        self.p.state['created'] = {}  # Existing group, not owned by this invocation.
        before = [r.to_dict() for r in self.cloud.rows['rule']]; count = len(self.cloud.created)
        self.cloud.network.create_security_group_rule = Mock(side_effect=AssertionError('Unexpected rule creation'))
        found, missing = self.p.plan(10 * 1024**3)
        self.assertEqual(missing, [])
        self.p.changed = False; self.p.apply(found)
        self.assertFalse(self.p.changed)
        self.cloud.network.create_security_group_rule.assert_not_called()
        self.assertEqual(len(self.cloud.created), count)
        self.assertEqual([r.to_dict() for r in self.cloud.rows['rule']], before)

    @unittest.skipIf(SecurityGroupRule is None, 'openstacksdk required for real security-group resources')
    def test_sdk_rule_creation_serialization_and_partial_retry_no_duplicates(self):
        bodies = []
        def create_rule(**attrs):
            self.assertIn('ether_type', attrs); self.assertNotIn('ethertype', attrs)
            pending = SecurityGroupRule(**attrs)
            body = pending._prepare_request(requires_id=False, prepend_key=True).body['security_group_rule']
            self.assertEqual(body['ethertype'], attrs['ether_type']); self.assertNotIn('ether_type', body)
            self.assertIsNone(body['port_range_min']); self.assertIsNone(body['port_range_max'])
            bodies.append(body)
            row = SecurityGroupRule(id=str(uuid.uuid4()), **body)
            self.cloud.rows['rule'].append(row); self.cloud.created.append(('rule', row))
            return row
        create = self.cloud.network.create_security_group_rule = Mock(side_effect=create_rule)
        self.create()
        self.assertEqual(create.call_count, 8)
        self.assertEqual({b['ethertype'] for b in bodies if b['direction'] == 'egress'}, {'IPv4', 'IPv6'})
        self.assertEqual({b['protocol'] for b in bodies if b['direction'] == 'ingress'}, {'icmp', 'tcp', 'udp'})
        # Interrupted owned-group initialization: only the missing rule is added.
        self.cloud.rows['rule'] = [r for r in self.cloud.rows['rule'] if r.ether_type != 'IPv6']
        create.reset_mock(); self.p.apply(self.p.plan(10 * 1024**3)[0])
        create.assert_called_once_with(security_group_id=self.cloud.rows['security_group'][0].id,
            direction='egress', ether_type='IPv6', protocol=None, remote_ip_prefix=None,
            port_range_min=None, port_range_max=None)
        self.assertEqual(len(self.cloud.rows['rule']), 8)
        create.reset_mock(); self.p.apply(self.p.plan(10 * 1024**3)[0]); create.assert_not_called()

    @unittest.skipIf(SecurityGroupRule is None, 'openstacksdk required for real security-group resources')
    def test_sdk_inspect_plan_preserves_neutron_ethertype_evidence(self):
        self.create(); self.cloud.rows['rule'] = self.sdk_rules()
        spec = self.path / 'spec.json'; spec.write_text(json.dumps(self.spec))
        argv = ['ew_provision.py', 'inspect', str(self.root), '--spec', str(spec),
                '--state', str(self.state), '--excluded-root', str(self.path / 'backups')]
        count = len(self.cloud.created)
        with patch.object(sys, 'argv', argv), patch.object(ew, 'validated_spec', return_value=10 * 1024**3), \
             patch('openstack.connect', return_value=self.cloud), contextlib.redirect_stdout(io.StringIO()):
            ew.main()
        evidence = json.loads((self.root / 'ew-provision-plan.json').read_text())
        self.assertEqual(evidence['missing'], [])
        rules = evidence['security_group_rules']; self.assertEqual(len(rules), 8)
        self.assertEqual([r['ethertype'] for r in rules], [r.ether_type for r in self.cloud.rows['rule']])
        for r, observed in zip(rules, self.cloud.rows['rule']):
            self.assertNotIn('ether_type', r)
            self.assertEqual(r, dict(ethertype=observed.ether_type, direction=observed.direction,
                protocol=observed.protocol, remote_ip_prefix=observed.remote_ip_prefix,
                remote_group_id=None, port_range_min=None, port_range_max=None))
        self.assertEqual(len(self.cloud.created), count)

    def test_rule_evidence_does_not_default_mismatched_sdk_attribute(self):
        with self.assertRaises(AttributeError):
            ew.security_group_rule_evidence(NS(ethertype='IPv4'))

    def test_checkpoint_precedes_active_wait_for_new_and_recovered(self):
        def check(server):
            state = json.loads((self.state / 'resources.json').read_text())
            self.assertEqual(state['resources']['server:' + server.name], server.id)
            self.assertIn('port:' + server.name, state['resources'])
        self.cloud.on_wait = check; self.create()
        self.cloud.rows['server'][0].status = 'BUILD'
        self.p.apply(self.p.plan(10 * 1024**3)[0])
        self.assertEqual(self.cloud.rows['server'][0].status, 'ACTIVE')
        self.assertTrue(all(kw == dict(status='ACTIVE', failures=['ERROR'], interval=2, wait=900) for _, kw in self.cloud.waits))

    def build_unattached(self):
        self.create()
        server = self.cloud.rows['server'][0]
        port = self.cloud.list('port', device_id=server.id)[0]
        server.status = 'BUILD'; port.device_id = ''; port.status = 'DOWN'
        return server, port

    def test_checkpointed_build_unattached_recovers_without_duplicate_resources(self):
        server, port = self.build_unattached(); count = len(self.cloud.created)
        found, missing = self.p.plan(10 * 1024**3); self.assertEqual(missing, [])
        def attach(current):
            if current.id == server.id: port.device_id = server.id; port.status = 'ACTIVE'
        self.cloud.on_wait = attach
        self.p.apply(found)
        self.assertEqual(server.status, 'ACTIVE'); self.assertEqual(port.device_id, server.id)
        self.assertEqual(len(self.cloud.created), count)

    def test_attachment_can_complete_after_nova_active(self):
        server, port = self.build_unattached(); calls = []
        def get_port(key):
            if key == port.id:
                calls.append(key)
                if len(calls) == 2: port.device_id = server.id; port.status = 'ACTIVE'
            return self.cloud.get('port', key)
        self.cloud.network.get_port = get_port
        with patch.object(ew.time, 'sleep'):
            self.p.apply(self.p.plan(10 * 1024**3)[0])
        self.assertEqual(len(calls), 2)

    def test_unattached_timeout_preserves_uuid_and_never_recreates(self):
        self.build_unattached(); count = len(self.cloud.created); before = copy.deepcopy(self.p.state['resources'])
        self.p.timeout = 1
        found, _ = self.p.plan(10 * 1024**3)
        with patch.object(ew.time, 'monotonic', side_effect=[0, 2]):
            with self.assertRaisesRegex(RuntimeError, 'attachment/ACTIVE timed out'): self.p.apply(found)
        self.assertEqual(len(self.cloud.created), count)
        self.assertEqual(self.p.state['resources'], before)

    def test_unattached_build_requires_both_exact_checkpoints(self):
        server, port = self.build_unattached()
        for kind in ('server', 'port'):
            key = kind + ':' + server.name; value = self.p.state['resources'].pop(key)
            with self.assertRaisesRegex(RuntimeError, 'not checkpointed'): self.p.plan(10 * 1024**3)
            self.p.state['resources'][key] = value
        port.device_id = 'another-server'
        with self.assertRaisesRegex(RuntimeError, 'another server'): self.p.plan(10 * 1024**3)

    def test_attachment_wait_rejects_changed_fixed_ip(self):
        server, port = self.build_unattached()
        def change(current):
            if current.id == server.id:
                port.device_id = server.id; port.status = 'ACTIVE'; port.fixed_ips[0]['ip_address'] = '192.168.101.99'
        self.cloud.on_wait = change
        with self.assertRaisesRegex(RuntimeError, 'identity changed'): self.p.apply(self.p.plan(10 * 1024**3)[0])

    def test_conflicts_fail_before_any_new_resource(self):
        self.create()
        variants = [('network', 'mtu', 1450), ('network', 'provider_network_type', 'geneve'),
            ('subnet', 'gateway_ip', '192.168.101.254'), ('subnet', 'host_routes', [{'destination': '0.0.0.0/0'}]),
            ('server', 'compute_host', 'wrong'), ('server', 'image', {'id': 'wrong'}),
            ('server', 'has_config_drive', False), ('server', 'key_name', 'wrong'),
            ('server', 'availability_zone', 'wrong'),
            ('flavor', 'ram', 1024), ('flavor', 'extra_specs', {'hw:cpu_policy': 'dedicated'}),
            ('image', 'hash_value', '0' * 64), ('image', 'properties', {'os_distro': 'wrong'}),
            ('port', 'security_group_ids', [])]
        for kind, attr, bad in variants:
            with self.subTest(kind=kind, attr=attr):
                row = next(r for r in self.cloud.rows[kind] if hasattr(r, attr))
                before = getattr(row, attr); setattr(row, attr, bad); count = len(self.cloud.created)
                with self.assertRaises(RuntimeError): self.p.plan(10 * 1024**3)
                self.assertEqual(len(self.cloud.created), count); setattr(row, attr, before)

    def test_duplicate_names_and_fixed_ip_block(self):
        self.create()
        for kind in ('server', 'network', 'image', 'subnet', 'flavor', 'security_group'):
            with self.subTest(kind=kind):
                self.cloud.rows[kind].append(copy.deepcopy(self.cloud.rows[kind][0]))
                with self.assertRaisesRegex(RuntimeError, 'duplicate'): self.p.plan(10 * 1024**3)
                self.cloud.rows[kind].pop()
        port = next(p for p in self.cloud.rows['port'] if p.name == 'ew-app-port')
        self.cloud.rows['port'].append(copy.deepcopy(port))
        with self.assertRaisesRegex(RuntimeError, 'fixed IP'): self.p.plan(10 * 1024**3)

    def test_missing_checkpointed_server_never_recreated(self):
        self.create(); self.cloud.rows['server'].pop()
        with self.assertRaisesRegex(RuntimeError, 'checkpointed resource missing'): self.p.plan(10 * 1024**3)

    def test_error_server_blocks_reuse(self):
        self.create(); self.cloud.rows['server'][0].status = 'ERROR'
        with self.assertRaisesRegex(RuntimeError, 'Nova state'): self.p.plan(10 * 1024**3)

    def test_wrong_keypair_and_incomplete_security_group_block(self):
        self.cloud.compute.find_keypair = lambda *a, **kw: NS(id='ew-key', public_key='ssh-ed25519 DIFFERENT')
        with self.assertRaisesRegex(RuntimeError, 'keypair does not match'): self.p.plan(10 * 1024**3)
        self.cloud.compute.find_keypair = lambda *a, **kw: NS(id='ew-key', public_key='ssh-ed25519 TEST')
        self.create(); self.p.state['created'] = {}; self.cloud.rows['rule'].clear()
        with self.assertRaisesRegex(RuntimeError, 'required tenant'): self.p.plan(10 * 1024**3)

    def test_verified_qcow_and_reviewed_inputs_required(self):
        self.assertEqual(self.validated(), 10 * 1024**3)
        self.spec['bootstrap'] = {}
        with self.assertRaisesRegex(RuntimeError, 'bootstrap source is missing'): self.validated()
        self.spec['secrets'] = {}
        self.assertEqual(self.validated(require_bootstrap=False), 10 * 1024**3)

    def test_hash_and_sidecar_must_match_pin(self):
        sidecar = self.path / 'checksum'; sidecar.write_text('0' * 64)
        self.spec['image']['sha256_file'] = str(sidecar)
        with self.assertRaisesRegex(RuntimeError, 'sidecar differs'): self.validated()

    def test_wrong_checksum_and_backup_location_block(self):
        self.spec['image']['sha256'] = '0' * 64
        with self.assertRaisesRegex(RuntimeError, 'SHA256 mismatch'): self.validated()
        self.spec['image']['path'] = str(self.path / 'backups' / 'netfix.qcow2')
        with self.assertRaisesRegex(RuntimeError, 'outside'): self.validated()

    def test_gateway_and_dhcp_pool_do_not_include_fixed_workload_ips(self):
        self.validated()
        net = self.spec['networks']['ew-net-a']; net['gateway_ip'] = '192.168.102.1'
        with self.assertRaisesRegex(RuntimeError, 'gateway'): self.validated()

    def test_service_handlers_are_change_aware(self):
        self.assertFalse(any(ew.service_handlers([], False).values()))
        self.assertFalse(any(ew.service_handlers(['/opt/ew-lab/probe.py'], False).values()))
        self.assertEqual(ew.service_handlers(['/opt/ew-lab/api.py'], False), {'ew-api.service': True, 'ew-worker.service': False})
        self.assertTrue(all(ew.service_handlers(['/opt/ew-lab/common.py'], False).values()))
        self.assertTrue(all(ew.service_handlers([], True).values()))

    def test_unchanged_app_neither_initializes_nor_restarts(self):
        self.p.install = Mock(return_value=[])
        def guest(vm, access, argv, data=None, timeout=None):
            if any('EW_BAKED_DEPENDENCIES' in a for a in argv): return json.dumps({'missing': []})
            if data:
                value = json.loads(data)
                self.assertFalse(any(value['services'].values()))
                return json.dumps({'actions': []})
            return json.dumps({'changed': False})
        self.p.guest = Mock(side_effect=guest)
        result = self.p.deploy_app(self.app_vm, {})
        self.assertEqual(result['service_actions'], [])
        self.assertFalse(self.p.changed)
        self.assertFalse(any(call.args[2][0] == 'bash' for call in self.p.guest.call_args_list))

    def test_changed_api_only_has_api_handler(self):
        self.p.install = Mock(return_value=['/opt/ew-lab/api.py'])
        def guest(vm, access, argv, data=None, timeout=None):
            if any('EW_BAKED_DEPENDENCIES' in a for a in argv): return json.dumps({'missing': []})
            if data:
                services = json.loads(data)['services']
                self.assertEqual(services, {name: name == 'ew-api.service' for name in services})
                return json.dumps({'actions': ['restart:ew-api.service'] if services.get('ew-api.service') else []})
            if argv[0] == 'python3' and argv[2] != 'import gunicorn,psycopg2,pika':
                return json.dumps({'changed': False})
            return ''
        self.p.guest = Mock(side_effect=guest)
        result = self.p.deploy_app(self.app_vm, {})
        self.assertEqual(result['service_actions'], ['restart:ew-api.service'])

    def test_environment_builder_preserves_credentials_and_unchanged_file(self):
        directory = self.path / 'guest-etc'; directory.mkdir()
        for name in ('db-password', 'mq-password'): (directory / name).write_text('a' * 48)
        self.p.install = Mock(return_value=[])
        def guest(vm, access, argv, data=None, timeout=None):
            if any('EW_BAKED_DEPENDENCIES' in a for a in argv): return json.dumps({'missing': []})
            if argv[:2] == ['python3', '-c'] and 'passwords=' in argv[2]:
                code = argv[2].replace('/etc/ew-lab', str(directory))
                output = subprocess.run(['python3', '-c', code, *argv[3:]], text=True, capture_output=True)
                if output.returncode: raise RuntimeError(output.stderr)
                return output.stdout
            if data: return json.dumps({'actions': []})
            return ''
        self.p.guest = Mock(side_effect=guest)
        self.p.deploy_app(self.app_vm, {})
        env = directory / 'app.env'; before = env.read_bytes(); mtime = env.stat().st_mtime_ns
        self.p.deploy_app(self.app_vm, {})
        self.assertEqual(env.read_bytes(), before); self.assertEqual(env.stat().st_mtime_ns, mtime)
        env.write_bytes(before.replace(b'a' * 48, b'b' * 48))
        with self.assertRaisesRegex(RuntimeError, 'refusing overwrite'): self.p.deploy_app(self.app_vm, {})
        self.assertIn(b'b' * 48, env.read_bytes())

    def test_console_trust_only_exact_new_owned_server_and_never_replaces(self):
        vm = {'name': 'ew-app', 'server': 'id', 'ip': '192.168.101.11'}
        with patch.object(ew.subprocess, 'run', return_value=NS(returncode=1)):
            with self.assertRaisesRegex(RuntimeError, 'reused guest needs'): self.p.trust_new_guest(vm)
        hosts = pathlib.Path(self.p.access_cfg['guest_known_hosts']); hosts.write_text('existing key\n')
        with patch.object(ew.subprocess, 'run', return_value=NS(returncode=0)):
            self.p.trust_new_guest(vm)
        self.assertEqual(hosts.read_text(), 'existing key\n')

    def test_console_public_key_block_required(self):
        import base64
        key = 'ssh-ed25519 ' + base64.b64encode(b'x' * 40).decode()
        self.assertEqual(ew.console_host_keys('-----BEGIN SSH HOST KEY KEYS-----\n' + key + '\n-----END SSH HOST KEY KEYS-----'), [key])
        for text in ('', key, '-----BEGIN SSH HOST KEY KEYS-----\n-----END SSH HOST KEY KEYS-----'):
            with self.assertRaises(RuntimeError): ew.console_host_keys(text)

    def test_install_never_overwrites_credentials_and_unchanged_writes_nothing(self):
        secret = self.path / 'existing-secret'; secret.write_bytes(b'a' * 48); secret.chmod(0o600)
        def local_guest(vm, access, argv, data=None, timeout=None):
            result = subprocess.run(argv, input=data, text=True, capture_output=True)
            if result.returncode: raise RuntimeError(result.stderr)
            return result.stdout
        self.p.guest = local_guest
        self.assertEqual(self.p.install({}, {}, {str(secret): b'a' * 48}, secrets=True), [])
        with self.assertRaisesRegex(RuntimeError, 'refusing overwrite'):
            self.p.install({}, {}, {str(secret): b'b' * 48}, secrets=True)
        self.assertEqual(secret.read_bytes(), b'a' * 48)
        secret.chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, 'not private'):
            self.p.install({}, {}, {str(secret): b'a' * 48}, secrets=True)
        target = self.path / 'code.py'; self.p.install({}, {}, {str(target): b'original'})
        mtime = target.stat().st_mtime_ns
        self.assertEqual(self.p.install({}, {}, {str(target): b'original'}), [])
        self.assertEqual(target.stat().st_mtime_ns, mtime)

    def test_normalized_credentials_retain_bytes_mode_and_mtime(self):
        def local_guest(vm, access, argv, data=None, timeout=None):
            result = subprocess.run(argv, input=data, text=True, capture_output=True)
            if result.returncode: raise RuntimeError(result.stderr)
            return result.stdout
        self.p.guest = local_guest
        secret = self.path / 'password'
        for body in (b'a' * 48, b'a' * 48 + b'\n', b'\n' + b'a' * 48 + b'\n'):
            with self.subTest(body_length=len(body)):
                secret.write_bytes(body); secret.chmod(0o600); stat = secret.stat()
                self.assertEqual(self.p.install({}, {}, {str(secret): b'a' * 48}, secrets=True), [])
                self.assertEqual(secret.read_bytes(), body); self.assertEqual(secret.stat().st_mtime_ns, stat.st_mtime_ns)
                self.assertEqual(secret.stat().st_mode, stat.st_mode)
        for body in (b'not-a-password\n', b'A' * 48, b'a' * 47):
            secret.write_bytes(body)
            with self.assertRaisesRegex(RuntimeError, 'Invalid existing credential format'):
                self.p.install({}, {}, {str(secret): b'a' * 48}, secrets=True)
        secret.write_bytes(b'a' * 48)
        with self.assertRaisesRegex(RuntimeError, 'Invalid credential format'):
            self.p.install({}, {}, {str(secret): b'bad'}, secrets=True)

    def deployment_model(self, paths, environment=False):
        """Persistent file equality survives reconstructed controller objects."""
        self.ready_transport(); self.p.ready()
        self.app_vm = json.loads((self.root / 'ew-resources.json').read_text())['servers']['ew-app']
        original_guest = self.p.guest.side_effect
        model = dict(installed=False, env_applied=not environment, init=0, restarts=[], reloads=0,
                     failures=set())
        def disk(): return json.loads(self.p.state_path.read_text())['application_deployment_pending']
        def fail(stage):
            if stage in model['failures']:
                model['failures'].remove(stage); raise RuntimeError('injected ' + stage)
        def install(vm, access, files, secrets=False, dry_run=False):
            if '/opt/ew-lab/api.py' not in files: return []
            changed = [] if model['installed'] else list(paths)
            if not dry_run:
                self.assertIn('services', disk()); model['installed'] = True
            return changed
        def guest(vm, access, argv, data=None, timeout=None):
            if any('EW_BAKED_DEPENDENCIES' in a for a in argv): return json.dumps({'missing': []})
            if argv[:2] == ['python3', '-c'] and 'passwords=' in argv[2]:
                changed = not model['env_applied']
                if argv[-1] == 'apply':
                    self.assertIn('initialize', disk()); model['env_applied'] = True
                return json.dumps({'changed': changed})
            if argv[0] == 'bash' and 'common.py' in argv[-1]:
                self.assertTrue(disk()['initialize']); model['init'] += 1; fail('initialize'); return ''
            if argv == ['systemctl', 'daemon-reload']:
                self.assertTrue(disk()['reload']); model['reloads'] += 1; fail('reload'); return ''
            if data and argv[:2] == ['python3', '-c'] and 'services' in json.loads(data):
                services = json.loads(data)['services']; actions = []
                for name, restart in services.items():
                    if restart:
                        self.assertTrue(disk()['services'][name]); model['restarts'].append(name)
                        fail(name); actions.append('restart:' + name)
                return json.dumps({'actions': actions})
            if argv[:2] == ['python3', '-']: fail('readiness')
            return original_guest(vm, access, argv, data, timeout)
        def wire():
            self.p.guest = Mock(side_effect=guest); self.p.install = Mock(side_effect=install)
            self.p.trust_new_guest = Mock(); self.p.spec['bootstrap'] = {}
        wire()
        def recover():
            tr = self.p.transport
            self.p = ew.Provisioner(self.cloud, self.root, self.spec, self.state, timeout=0, transport=tr)
            wire()
        return model, recover

    def test_initializer_failure_keeps_pending_after_environment_and_file_writes(self):
        model, recover = self.deployment_model(['/opt/ew-lab/api.py'], environment=True)
        model['failures'].add('initialize')
        with self.assertRaisesRegex(RuntimeError, 'injected'): self.p.deploy_app(self.app_vm, {})
        self.assertTrue(model['installed']); self.assertTrue(model['env_applied'])
        recover(); self.p.ready(deploy=True)
        self.assertEqual(model['init'], 2)
        self.assertNotIn('application_deployment_pending', self.p.state)

    def test_handler_failure_retries_identical_files_then_unchanged_run_is_noop(self):
        model, recover = self.deployment_model(['/opt/ew-lab/api.py'])
        model['failures'].add('ew-api.service')
        with self.assertRaisesRegex(RuntimeError, 'injected'): self.p.deploy_app(self.app_vm, {})
        self.assertTrue(model['installed']); recover()
        result = self.p.ready(deploy=True)
        self.assertEqual(model['restarts'], ['ew-api.service', 'ew-api.service'])
        self.assertEqual(model['init'], 1)
        self.assertNotIn('application_deployment_pending', self.p.state)
        result = self.p.ready(deploy=True)
        self.assertEqual(result['application_deployment']['service_actions'], [])
        self.assertEqual(result['application_deployment']['changed_files'], [])
        self.assertEqual(model['restarts'], ['ew-api.service', 'ew-api.service'])

    def test_partial_handler_completion_does_not_restart_completed_api(self):
        model, recover = self.deployment_model(['/opt/ew-lab/common.py'])
        model['failures'].add('ew-worker.service')
        with self.assertRaisesRegex(RuntimeError, 'injected'): self.p.deploy_app(self.app_vm, {})
        pending = self.p.state['application_deployment_pending']
        self.assertFalse(pending['services']['ew-api.service']); self.assertTrue(pending['services']['ew-worker.service'])
        recover(); self.p.ready(deploy=True)
        self.assertEqual(model['restarts'].count('ew-api.service'), 1)
        self.assertEqual(model['restarts'].count('ew-worker.service'), 2)

    def test_reload_failure_is_durable(self):
        model, recover = self.deployment_model(['/etc/systemd/system/ew-api.service'])
        model['failures'].add('reload')
        with self.assertRaisesRegex(RuntimeError, 'injected'): self.p.deploy_app(self.app_vm, {})
        self.assertTrue(self.p.state['application_deployment_pending']['reload'])
        recover(); self.p.ready(deploy=True)
        self.assertEqual(model['reloads'], 2); self.assertEqual(model['init'], 1)

    def test_readiness_failure_does_not_finalize_pending_or_repeat_completed_handlers(self):
        model, recover = self.deployment_model(['/opt/ew-lab/api.py'])
        model['failures'].add('readiness')
        with self.assertRaisesRegex(RuntimeError, 'injected readiness'): self.p.ready(deploy=True)
        self.assertIn('application_deployment_pending', self.p.state)
        recover(); self.p.ready(deploy=True)
        self.assertEqual(model['restarts'], ['ew-api.service'])
        self.assertNotIn('application_deployment_pending', self.p.state)

    def test_malformed_pending_state_is_not_silently_replaced(self):
        model, _ = self.deployment_model(['/opt/ew-lab/api.py'])
        self.p.state['application_deployment_pending'] = {}
        with self.assertRaisesRegex(RuntimeError, 'Malformed unfinished'): self.p.deploy_app(self.app_vm, {})
        self.assertFalse(model['installed'])

    def test_app_sources_and_initialization_preserved(self):
        common = (ew.APP / 'common.py').read_text(); setup = (ew.APP / 'setup.sh').read_text()
        self.assertIn('CREATE TABLE IF NOT EXISTS', common)
        self.assertNotIn('DROP TABLE', common); self.assertNotIn('TRUNCATE', common)
        self.assertEqual(len(ew.re.findall(r"cat > (/etc/systemd/system/[^ ]+) <<'UNIT'\n(.*?)\nUNIT", setup, ew.re.S)), 2)

    def ready_transport(self):
        self.create(); self.p.timeout = 0
        tr = Mock(); tr.access.return_value = {'host': 'network1', 'namespace': 'fixture'}
        tr.profile.side_effect = lambda vm, access: dict(server=vm['server'], boot='same-boot',
            interfaces=[dict(address='fa:16:3e:00:00:01', mtu=1400, operstate='UP',
                addr_info=[dict(family='inet', local=vm['ip'])])], routes=[dict(dst='default', gateway='192.168.101.1')])
        self.p.transport = tr
        def guest(vm, access, argv, data=None, timeout=None):
            if any('EW_BAKED_DEPENDENCIES' in a for a in argv): return json.dumps({'missing': []})
            if argv[:2] == ['cloud-init', 'status']: return json.dumps({'status': 'done', 'errors': [], 'recoverable_errors': {}})
            if argv[:2] == ['python3', '-']:
                return 'E2E_RESULT_OK client=' + argv[-1] + ' task_id=11111111-1111-4111-8111-111111111111 latency_ms=1\nWORKLOAD_E2E_OK\n'
            if argv[-1].startswith('http://'):
                return json.dumps(dict(task_id=argv[-1].rsplit('/', 1)[1], run_id='smoke', client_id='client', status='done',
                    result_sha256='a' * 64, process_count=1, created_at='original', completed_at='original'))
            return ''
        self.p.guest = Mock(side_effect=guest)

    def test_guest_identity_cloud_init_and_three_real_client_probes_required(self):
        self.ready_transport(); result = self.p.ready()
        self.assertEqual(len(result['guests']), 6); self.assertEqual(len(result['tasks']), 3)
        commands = [call.args[2] for call in self.p.guest.call_args_list]
        self.assertEqual(sum(a[:2] == ['python3', '-'] for a in commands), 3)
        self.assertEqual(sum(a[0] == 'ping' for a in commands), 9)

    def test_active_alone_and_cloud_init_errors_never_pass(self):
        self.ready_transport()
        for status in ({'status': 'running'}, {'status': 'done', 'errors': ['failed']},
                       {'status': 'done', 'recoverable_errors': {'WARN': ['failed']}}):
            self.p.guest = Mock(return_value=json.dumps(status))
            with self.assertRaisesRegex(RuntimeError, 'cloud-init readiness timed out'): self.p.ready()
        self.assertFalse((self.root / 'ew-provision-readiness.json').exists())

    def test_changed_boot_and_failed_task_completion_block_readiness(self):
        self.ready_transport(); self.p.ready()
        self.p.state['guests']['ew-app']['boot'] = 'different'
        with self.assertRaisesRegex(RuntimeError, 'readiness timed out'): self.p.ready()
        self.p.state['guests']['ew-app']['boot'] = 'same-boot'
        original = self.p.guest.side_effect
        self.p.guest.side_effect = lambda vm, access, argv, **kw: 'DEPENDENCIES_HEALTH_OK' if argv[:2] == ['python3', '-'] else original(vm, access, argv, **kw)
        with self.assertRaisesRegex(RuntimeError, 'task-completion smoke failed'): self.p.ready()

    def test_second_run_verifies_prior_task_receipts_and_changed_data_fails(self):
        self.ready_transport(); self.p.ready(); before = copy.deepcopy(self.p.state['preserved_tasks'])
        result = self.p.ready()
        self.assertEqual(result['preserved_tasks']['receipts'], before)
        self.p.state['preserved_tasks']['ew-client-a1']['result_sha256'] = 'changed'
        with self.assertRaisesRegex(RuntimeError, 'pre-existing task result changed'): self.p.ready()


class OrderingTests(unittest.TestCase):
    def test_work_item_entrypoints_preserve_migration_order(self):
        workflow = yaml.safe_load((ROOT / 'ew-migrate.yml').read_text())
        imports = [p['ansible.builtin.import_playbook'] for p in workflow if 'ansible.builtin.import_playbook' in p]
        self.assertEqual(imports, ['ew-provision.yml', 'ew-baseline.yml', 'migrate-to-ovn.yml'])
        migration = yaml.safe_load((ROOT / 'migrate-to-ovn.yml').read_text())
        names = [p.get('ansible.builtin.import_playbook', p.get('import_playbook')) for p in migration]
        self.assertEqual([pathlib.Path(n).name[:2] for n in names], [f'{n:02}' for n in range(14)])

    def test_provisioning_has_no_listener_activation_or_reboot_cleanup(self):
        source = (ROOT / 'scripts/ew_provision.py').read_text()
        for forbidden in ('reboot_server(', 'delete_server(', 'delete_port(', 'activate_tcp', '.bind(', '.listen('):
            self.assertNotIn(forbidden, source)
        freeze = (ROOT / 'playbooks/07-migrate-db.yml').read_text()
        self.assertIn('tcp-activate', freeze)

    def test_provision_readiness_failure_blocks_baseline(self):
        plays = yaml.safe_load((ROOT / 'ew-provision.yml').read_text())
        work = plays[-1]
        self.assertTrue(work['any_errors_fatal'])
        task = next(t for t in work['tasks'] if 'ansible.builtin.shell' in t)
        self.assertNotIn('ignore_errors', task); self.assertNotIn('failed_when', task)
        self.assertIn('set -euo pipefail', task['ansible.builtin.shell'])
        workflow = yaml.safe_load((ROOT / 'ew-migrate.yml').read_text())
        assertions = workflow[0]['tasks'][1]['ansible.builtin.assert']['that']
        self.assertEqual(assertions, ['ew_workloads_enabled | bool', "ew_provision_action == 'apply'"])


if __name__ == '__main__': unittest.main()
