"""Exercise real compute-adapter HTTP bodies and microversion headers offline."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from keystoneauth1 import noauth, session
from openstack.compute.v2._proxy import Proxy
from openstack.connection import Connection
from openstack.exceptions import BadRequestException
from test_validation import ROOT
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


if __name__ == '__main__':
    unittest.main()
