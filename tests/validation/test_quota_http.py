"""Capacity checks read existing Nova quotas and never issue quota writes."""

from tests.support import ROOT
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.parse import urlsplit
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

from keystoneauth1 import noauth, session
from openstack.compute.v2._proxy import Proxy
from openstack.connection import Connection
from openstack.exceptions import ForbiddenException

sys.path.insert(0, str(ROOT/'scripts'))
from extended_validation import Extended
from project_quota import ensure_instance_quota
from unittest.mock import Mock


class QuotaHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.requests = []
        self.quotas = dict(instances=20, cores=20, ram=51200)
        self.reject_updates = False
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def answer(self, status, payload):
                self.send_response(status)
                self.send_header('Content-Type','application/json')
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())
            def do_GET(self):
                path=urlsplit(self.path).path
                owner.requests.append(('GET', path, None))
                if path != '/v2.1/os-quota-sets/project-id':
                    return self.answer(404, {})
                self.answer(200, {'quota_set':dict(owner.quotas, id='project-id')})
            def do_PUT(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.requests.append(('PUT', self.path, body))
                if owner.reject_updates:
                    return self.answer(403, {'forbidden':{'message':'quota writes prohibited'}})
                if self.path != '/v2.1/os-quota-sets/project-id' or set(body) != {'quota_set'} or set(body['quota_set']) != {'instances'}:
                    return self.answer(400, {'badRequest':{'message':'only instance quota may change'}})
                owner.quotas['instances'] = body['quota_set']['instances']
                self.answer(200, {'quota_set':dict(owner.quotas, id='project-id')})
        self.http=HTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.http.serve_forever,kwargs={'poll_interval':.01},daemon=True)
        self.thread.start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.thread.join)
        self.addCleanup(self.http.shutdown)
        endpoint=f'http://127.0.0.1:{self.http.server_port}/v2.1'
        self.connection=Connection(session=session.Session(auth=noauth.NoAuth(endpoint=endpoint)),force_ipv4=True)
        proxy=Proxy(session=self.connection.session,
                    endpoint_override=endpoint, service_type='compute',default_microversion='2.1')
        self.cloud=SimpleNamespace(compute=proxy,session=SimpleNamespace(get_project_id=lambda:'project-id'))

    def capacity(self):
        compute = Mock(wraps=self.cloud.compute)
        compute.find_flavor.return_value = SimpleNamespace(id='flavor', vcpus=1, ram=1024, disk=8)
        compute.servers.return_value = [SimpleNamespace(metadata={}, compute_host=host)
                                        for host in ('compute1', 'compute2')]
        compute.get_limits.return_value = SimpleNamespace(absolute=SimpleNamespace(
            total_cores_used=2, total_ram_used=2048))
        compute.hypervisors.return_value = [SimpleNamespace(name=host,
            memory_size=64000, memory_used=1024, local_disk_size=1000, local_disk_used=8)
            for host in ('compute1', 'compute2')]
        cfg = dict(run='run-one', flavor='flavor', ew_matrix_enabled=True,
            ns={'enabled':True}, pair_d_enabled=True,
            placement={str(i):dict(inventory_host=host, nova_host=host, hypervisor=host)
                       for i, host in enumerate(('compute1', 'compute2'))})
        return Extended(SimpleNamespace(root=self.root, cfg=cfg,
            cloud=SimpleNamespace(compute=compute, session=self.cloud.session))), compute

    def test_sufficient_existing_quota_is_read_once_without_any_put(self):
        original = dict(self.quotas)
        workload, compute = self.capacity()
        workload.capacity()
        self.assertEqual(self.requests, [('GET', '/v2.1/os-quota-sets/project-id', None)])
        compute.update_quota_set.assert_not_called()
        self.assertEqual(self.quotas, original)
        evidence = json.loads((self.root/'workload-capacity.json').read_text())
        self.assertEqual(evidence['status'], 'PASS')
        self.assertEqual(evidence['checks']['instances']['projected'], 14)
        self.assertFalse((self.root/'quota-instances.json').exists())

    def test_insufficient_quota_blocks_without_changing_the_limit(self):
        self.quotas['instances'] = 10
        workload, compute = self.capacity()
        with self.assertRaisesRegex(RuntimeError, 'Insufficient quota'):
            workload.capacity()
        self.assertEqual(self.requests, [('GET', '/v2.1/os-quota-sets/project-id', None)])
        compute.update_quota_set.assert_not_called()
        self.assertEqual(self.quotas['instances'], 10)
        evidence = json.loads((self.root/'workload-capacity.json').read_text())
        self.assertEqual(evidence['checks']['instances']['status'], 'FAIL')
        self.assertFalse((self.root/'quota-instances.json').exists())

    def test_opt_in_helper_sends_only_instances_to_the_authenticated_project(self):
        self.quotas['instances'] = 10
        result = ensure_instance_quota(self.cloud, self.root)
        self.assertEqual(self.requests, [('GET','/v2.1/os-quota-sets/project-id',None),
            ('PUT','/v2.1/os-quota-sets/project-id',{'quota_set':{'instances':20}}),
            ('GET','/v2.1/os-quota-sets/project-id',None)])
        self.assertTrue(result['changed'])
        self.assertEqual(result['after'],dict(instances=20,cores=20,ram=51200))

    def test_opt_in_permission_failure_is_not_retried_or_marked_pass(self):
        self.quotas['instances'] = 10; self.reject_updates = True
        with self.assertRaises(ForbiddenException):
            ensure_instance_quota(self.cloud, self.root)
        self.assertEqual(sum(method=='PUT' for method,_,_ in self.requests),1)
        self.assertEqual(json.loads((self.root/'quota-instances.json').read_text())['status'],'FAIL')


if __name__=='__main__':unittest.main()
