"""Real SDK GET/PUT serialization against a local Nova quota endpoint."""

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
from extended_validation import quota20


class QuotaHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.requests = []
        self.quotas = dict(instances=10, cores=20, ram=51200)
        self.reject = False
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
                if owner.reject:
                    return self.answer(403, {'forbidden':{'message':'not permitted'}})
                if body != {'quota_set':{'instances':20}} or self.path != '/v2.1/os-quota-sets/project-id':
                    return self.answer(400, {'badRequest':{'message':'invalid quota update'}})
                owner.quotas['instances']=20
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

    def test_only_instances_reaches_the_correct_project_url(self):
        quota20(self.cloud,self.root)
        self.assertEqual(self.requests,[('GET','/v2.1/os-quota-sets/project-id',None),
            ('PUT','/v2.1/os-quota-sets/project-id',{'quota_set':{'instances':20}}),
            ('GET','/v2.1/os-quota-sets/project-id',None)])
        evidence=json.loads((self.root/'quota-instances.json').read_text())
        self.assertEqual(evidence['status'],'PASS')
        self.assertEqual(evidence['after'],dict(instances=20,cores=20,ram=51200))

    def test_permission_failure_is_not_retried_or_marked_pass(self):
        self.reject=True
        with self.assertRaises(ForbiddenException):quota20(self.cloud,self.root)
        self.assertEqual(sum(method=='PUT' for method,_,_ in self.requests),1)
        self.assertFalse((self.root/'quota-instances.json').exists())


if __name__=='__main__':unittest.main()
