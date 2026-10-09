import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from test_validation import v


class Conflict(Exception):
    status_code = 409


class ReadinessTests(unittest.TestCase):
    def obj(self, root):
        obj=v.Validation.__new__(v.Validation)
        obj.root=root; obj.path=root/'validation-resources.json'
        obj.cfg=dict(timeout=30,run='run',prefix='validation',image='image',flavor='flavor',interval=.2,lifetime=100)
        pair={'security_group':'sg','router':'router'}
        for i in range(2):
            pair[str(i)]=dict(network='net'+str(i),subnet='sub'+str(i),port='port'+str(i),
                              interface=True,ip='10.0.0.'+str(i+2),server='vm'+str(i))
        obj.state={'pre':pair}
        compute=Mock(); network=Mock(); image=Mock()
        image.find_image.return_value=SimpleNamespace(id='image')
        compute.find_flavor.return_value=SimpleNamespace(id='flavor',disk=8,ram=1024)
        network.security_group_rules.return_value=[SimpleNamespace(direction='ingress',protocol='icmp')]
        obj.cloud=SimpleNamespace(compute=compute,network=network,image=image)
        return obj

    def test_new_server_uuid_saved_before_build_wait(self):
        with tempfile.TemporaryDirectory() as d:
            obj=self.obj(pathlib.Path(d)); del obj.state['pre']['0']['server']
            obj.cloud.compute.servers.return_value=[]
            obj.cloud.compute.create_server.return_value=SimpleNamespace(id='new-vm',status='BUILD')
            obj.cloud.compute.get_server.side_effect=lambda uuid: SimpleNamespace(id=uuid,status='BUILD' if uuid=='new-vm' else 'ACTIVE')
            def wait(server, **kwargs):
                self.assertEqual(v.read_evidence(obj.root,'validation-resources.json')['pre']['0']['server'],'new-vm')
                self.assertEqual(kwargs,dict(status='ACTIVE',failures=['ERROR'],wait=30,interval=2))
                return SimpleNamespace(id=server.id,status='ACTIVE')
            obj.cloud.compute.wait_for_server.side_effect=wait
            obj.create('pre')
            self.assertEqual(obj.cloud.compute.wait_for_server.call_count,2)
            obj.cloud.compute.get_server_console_output.assert_not_called()

    def test_recovered_build_waits_without_duplicate(self):
        with tempfile.TemporaryDirectory() as d:
            obj=self.obj(pathlib.Path(d))
            obj.cloud.compute.get_server.return_value=SimpleNamespace(id='vm0',status='BUILD')
            obj.cloud.compute.wait_for_server.return_value=SimpleNamespace(id='vm0',status='ACTIVE')
            obj.create('pre')
            self.assertEqual(obj.cloud.compute.wait_for_server.call_count,2)
            obj.cloud.compute.create_server.assert_not_called()

    def test_active_server(self):
        with tempfile.TemporaryDirectory() as d:
            obj=self.obj(pathlib.Path(d)); server=SimpleNamespace(id='vm0',status='ACTIVE')
            obj.cloud.compute.get_server.return_value=server
            obj.cloud.compute.wait_for_server.return_value=server
            self.assertIs(obj.wait_active('vm0'),server)

    def test_error_server_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            obj=self.obj(pathlib.Path(d))
            obj.cloud.compute.get_server.return_value=SimpleNamespace(id='vm0',status='ERROR',fault={'message':'No valid host'})
            with self.assertRaisesRegex(RuntimeError,'ERROR.*No valid host'): obj.create('pre')
            self.assertEqual(obj.state['pre']['0']['server'],'vm0')
            obj.cloud.compute.wait_for_server.assert_not_called()
            obj.cloud.compute.delete_server.assert_not_called()

    def test_transient_console_conflict_retries(self):
        with tempfile.TemporaryDirectory() as d:
            obj=self.obj(pathlib.Path(d))
            text='OVN_MIGRATION_JSON {"run":"run","vm":"pre0","kind":"packet","seq":1,"boot":"boot","ts":1,"success":true}'
            obj.cloud.compute.get_server_console_output.side_effect=[Conflict('Instance vm0 is not ready'),{'output':text},{'output':''}]
            obj.cloud.compute.get_server.return_value=SimpleNamespace(status='ACTIVE')
            with patch.object(v.time,'sleep') as sleep:
                rows=obj.collect('pre')
            self.assertEqual(len(rows['0']),1); sleep.assert_called_once()

    def test_console_timeout(self):
        with tempfile.TemporaryDirectory() as d:
            obj=self.obj(pathlib.Path(d))
            obj.cloud.compute.get_server_console_output.side_effect=Conflict('Instance vm0 is not ready')
            obj.cloud.compute.get_server.return_value=SimpleNamespace(status='ACTIVE')
            with patch.object(v.time,'monotonic',side_effect=[0,0,31]),patch.object(v.time,'sleep'):
                with self.assertRaisesRegex(TimeoutError,'remained not ready'): obj.collect('pre')

    def test_permanent_console_errors_not_retried(self):
        for exc in (PermissionError('Authentication failed'),Conflict('Instance is locked'),FileNotFoundError('server missing')):
            with self.subTest(error=exc), tempfile.TemporaryDirectory() as d:
                obj=self.obj(pathlib.Path(d)); obj.cloud.compute.get_server_console_output.side_effect=exc
                with self.assertRaises(type(exc)): obj.collect('pre')
                self.assertEqual(obj.cloud.compute.get_server_console_output.call_count,1)

    def test_missing_checkpointed_server_not_recreated(self):
        with tempfile.TemporaryDirectory() as d:
            obj=self.obj(pathlib.Path(d)); obj.cloud.compute.get_server.side_effect=FileNotFoundError('vm0 missing')
            with self.assertRaises(FileNotFoundError): obj.create('pre')
            obj.cloud.compute.create_server.assert_not_called()


if __name__=='__main__': unittest.main()
