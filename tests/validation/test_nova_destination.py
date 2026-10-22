"""Exercise real compute-adapter HTTP bodies and microversion headers offline."""
import base64
import gzip
import json
from pathlib import Path
import random
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import yaml
from http.server import BaseHTTPRequestHandler, HTTPServer

from keystoneauth1 import noauth, session
from openstack.compute.v2._proxy import Proxy
from openstack.connection import Connection
from openstack.exceptions import BadRequestException
from tests.validation.test_validation import ROOT
from _paths import script_path
from extended_validation import Extended
from validation_placement import create_on_target


class NovaDestinationHTTPTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.response_status = 202
        self.response_body = {'server': {'id':'vm-id'}}
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.requests.append((self.path, dict(self.headers), body))
                payload = owner.response_body
                allowed = {'name','imageRef','flavorRef','networks','metadata','user_data','host','hypervisor_hostname'}
                if set(body.get('server', {})) - allowed:
                    self.send_response(400)
                    payload = {'badRequest': {'message':'Additional properties are not allowed'}}
                elif len(body['server']['user_data']) > 65535:
                    self.send_response(400)
                    payload = {'badRequest': {'message':'user_data is too long'}}
                else:
                    self.send_response(owner.response_status)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())
        self.http = HTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, kwargs={'poll_interval':.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.thread.join)
        self.addCleanup(self.http.shutdown)
        endpoint = f'http://127.0.0.1:{self.http.server_port}/v2.1'
        self.connection = Connection(session=session.Session(auth=noauth.NoAuth(endpoint=endpoint)), force_ipv4=True)
        self.compute = Proxy(session=self.connection.session,
                             endpoint_override=endpoint, service_type='compute')
        self.attributes = dict(name='owned-vm', image_id='image-id', flavor_id='flavor-id',
            networks=[{'port':'owned-port'}], metadata={'ovn_migration_run':'run','ovn_validation_role':'measure'},
            user_data='cloud-init-data')
        self.target = {'nova_host':'compute1', 'hypervisor':'compute1'}

    def test_host_and_hypervisor_reach_nova_as_request_keys_with_274(self):
        result = create_on_target(self.compute, self.target, **self.attributes)
        self.assertEqual(result.id, 'vm-id')
        path, headers, body = self.requests[0]
        self.assertEqual(path, '/v2.1/servers')
        self.assertEqual(headers['OpenStack-API-Version'], 'compute 2.74')
        self.assertEqual(headers['X-OpenStack-Nova-API-Version'], '2.74')
        self.assertEqual(body['server']['host'], 'compute1')
        self.assertEqual(body['server']['hypervisor_hostname'], 'compute1')
        self.assertEqual(body['server']['imageRef'], 'image-id')
        self.assertEqual(body['server']['flavorRef'], 'flavor-id')
        self.assertEqual(body['server']['networks'], [{'port':'owned-port'}])
        self.assertEqual(body['server']['metadata'], self.attributes['metadata'])
        self.assertEqual(body['server']['user_data'], 'cloud-init-data')
        self.assertFalse(any(key.startswith('OS-EXT-SRV-ATTR:') for key in body['server']))
        self.assertEqual(len(self.requests), 1)

    def test_nova_rejection_is_not_retried_without_a_destination(self):
        self.response_status = 400
        self.response_body = {'badRequest': {'message':'invalid destination'}}
        with self.assertRaises(BadRequestException):
            create_on_target(self.compute, self.target, **self.attributes)
        self.assertEqual(len(self.requests), 1)

    def test_missing_id_preserves_uncertain_request_without_second_post(self):
        self.response_body = {'server': {}}
        with self.assertRaisesRegex(RuntimeError, 'no server ID'):
            create_on_target(self.compute, self.target, **self.attributes)
        self.assertEqual(len(self.requests), 1)

    def test_invalid_target_fails_without_http_request(self):
        with self.assertRaisesRegex(RuntimeError, 'destination'):
            create_on_target(self.compute, {'nova_host':'compute1'}, **self.attributes)
        self.assertEqual(self.requests, [])

    def test_user_data_boundary_is_measured_after_base64_encoding(self):
        for size in (49149, 49150):
            raw = b'#cloud-config\n'+b'x'*(size-len(b'#cloud-config\n'))
            encoded = base64.b64encode(raw).decode()
            create_on_target(self.compute, self.target, **dict(self.attributes, user_data=encoded))
            wire = self.requests[-1][2]['server']['user_data']
            self.assertLessEqual(len(wire), 65535)
            if size == 49149:
                self.assertEqual(len(encoded), 65532)
                self.assertEqual(wire, encoded)
            else:
                self.assertEqual(len(encoded), 65536)
                self.assertEqual(gzip.decompress(base64.b64decode(wire)), raw)

    def test_real_ns_guest_payload_reaches_nova_with_every_file_and_command_intact(self):
        for action, export in (('client', True), ('serve', False)):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as folder:
                cfg = dict(run='20261018-164925', port=18180, new_port=18181,
                    control_port=18182, control_token='0'*32, source='10.233.0.11',
                    target='10.1.0.187', interval=.5, timeout=2, lifetime=86400,
                    label='snat', expected_peer='10.1.0.4', expected_server='external-endpoint',
                    icmp_enabled=True, icmp_export_enabled=export,
                    icmp_output='/var/lib/ovn-migration-icmp/20261018-164925/snat-icmp-sender.jsonl')
                vm = dict(name='owned-ns-'+action, role='ns-'+action, port='owned-port', placement=self.target)
                validation = SimpleNamespace(root=Path(folder), cloud=SimpleNamespace(compute=self.compute),
                    cfg=dict(run=cfg['run'], image='image-id', flavor='flavor-id'),
                    wait_active=Mock(), wait_placement=Mock())
                extended = Extended(validation)
                with patch.object(self.compute, 'servers', return_value=[]), \
                        patch.object(extended, 'server', wraps=extended.server) as boot:
                    extended.add_ns_guest(vm, action, cfg)
                original = b'#cloud-config\n'+yaml.safe_dump(boot.call_args.args[1]).encode()
                wire = self.requests[-1][2]['server']['user_data']
                raw = base64.b64decode(wire, validate=True)
                self.assertLessEqual(len(wire), 65535)
                if export:
                    self.assertGreater(len(base64.b64encode(original)), 65535)
                    raw = gzip.decompress(raw)
                self.assertEqual(raw, original)
                files = {f['path']: f for f in yaml.safe_load(raw)['write_files']}
                for guest, source in (('ns-probe.py','ns_probe.py'), ('ns_icmp.py','ns_icmp.py')):
                    self.assertEqual(files['/usr/local/bin/'+guest]['content'],
                                     script_path(source).read_text())
                if export:
                    for name in ('ns_log_transfer.py','run_layout.py'):
                        self.assertEqual(files['/usr/local/bin/'+name]['content'],
                                         script_path(name).read_text())
                self.assertEqual(json.loads(files['/etc/ns-probe.json']['content']), cfg)
                self.assertEqual(vm['server'], 'vm-id')
                validation.wait_active.assert_called_once_with('vm-id')

    def test_already_gzipped_user_data_is_not_wrapped_again(self):
        original = b'#cloud-config\n'+b'# repeated config line\n'*4000
        encoded = base64.b64encode(gzip.compress(original, mtime=0)).decode()
        create_on_target(self.compute, self.target, **dict(self.attributes, user_data=encoded))
        wire = self.requests[-1][2]['server']['user_data']
        self.assertEqual(wire, encoded)
        self.assertEqual(gzip.decompress(base64.b64decode(wire)), original)

    def test_uncompressible_oversized_data_fails_before_post_without_exposing_it(self):
        encoded = base64.b64encode(random.Random(0).randbytes(65000)).decode()
        with self.assertRaisesRegex(RuntimeError, '65535.*gzip') as caught:
            create_on_target(self.compute, self.target, **dict(self.attributes, user_data=encoded))
        self.assertNotIn(encoded, str(caught.exception))
        self.assertEqual(self.requests, [])

    def test_invalid_oversized_base64_fails_before_post(self):
        with self.assertRaisesRegex(RuntimeError, 'base64'):
            create_on_target(self.compute, self.target, **dict(self.attributes, user_data='!'*65536))
        self.assertEqual(self.requests, [])


if __name__ == '__main__':
    unittest.main()
