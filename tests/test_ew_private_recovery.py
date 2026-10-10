"""No SSH or credential recovery executes: fake trusted sources and local temp files."""
import contextlib
import copy
import importlib.util
import json
import os
import pathlib
import stat
import sys
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock,patch

import yaml
ROOT=pathlib.Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT/'scripts'))
import ew_bootstrap_access as access
import ew_bootstrap_verify as verify
import ew_recover_credentials as recovery
import ew_provision as provision
spec=importlib.util.spec_from_file_location('private_adapter',ROOT/'workloads/ew-bootstrap/adapter.py')
adapter=importlib.util.module_from_spec(spec); spec.loader.exec_module(adapter)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=pathlib.Path(self.temp.name); self.private=self.root/'private'
        self.observed={name:dict(vm=dict(server=name+'-uuid',port=name+'-port',ip='192.168.101.11'),
            access={},mac='fa:16:3e:00:00:01',boot='same-boot') for name in ('ew-app','ew-db','ew-queue')}
        self.transport=Mock(); self.transport.guest_argv.return_value=['ssh','verified-guest']
        self.values={'ew-app':dict(db='a'*48,mq='b'*48),'ew-db':dict(db='a'*48),'ew-queue':dict(mq='b'*48)}
        self.transport.run.side_effect=lambda *args,**kw: json.dumps(self.values[('ew-app','ew-db','ew-queue')[(self.transport.run.call_count-1)%3]])
        self.stack=contextlib.ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(recovery.os,'geteuid',return_value=0))
        self.stack.enter_context(patch.object(recovery,'private_tree'))
        self.stack.enter_context(patch.object(recovery,'sources',return_value=(self.transport,self.observed)))
        self.recheck=self.stack.enter_context(patch.object(recovery,'recheck'))
        # Fake only ownership; bytes/modes/mtime/no-replace behavior use real files.
        original=pathlib.Path.lstat
        def lstat(p,*args,**kw):
            s=original(p,*args,**kw)
            return NS(st_mode=s.st_mode,st_uid=0,st_gid=0,st_nlink=s.st_nlink)
        self.stack.enter_context(patch.object(pathlib.Path,'lstat',lstat))

    def recover(self): return recovery.recover(self.root,'reference.json',self.private,Mock(),self.transport)

    def test_matching_guest_values_saved_privately_and_unchanged_retry_preserves_bytes(self):
        result=self.recover(); self.assertTrue(result['changed']); self.assertNotIn('a'*48,json.dumps(result))
        mapping=yaml.safe_load((self.private/'ew-provision-inputs.yml').read_text())
        self.assertEqual(mapping,dict(ew_provision_secret_files={k:str(self.private/(k+'-password')) for k in ('db','mq')}))
        files=list(self.private.iterdir()); before={p:(p.read_bytes(),p.stat().st_mtime_ns) for p in files}
        self.assertTrue(all(stat.S_IMODE(p.stat().st_mode)==0o600 for p in files))
        self.assertEqual(stat.S_IMODE(self.private.stat().st_mode),0o700)
        self.assertFalse(self.recover()['changed']); self.assertEqual({p:(p.read_bytes(),p.stat().st_mtime_ns) for p in files},before)
        self.assertEqual(self.recheck.call_count,6)
        for call in self.transport.run.call_args_list:
            self.assertNotIn('a'*48,repr(call.args)); self.assertNotIn('b'*48,repr(call.args))

    def test_corresponding_guest_mismatch_and_format_fail_before_any_write(self):
        for value in ('c'*48,'INVALID'):
            self.values['ew-db']['db']=value
            with self.subTest(value=value),self.assertRaises(RuntimeError): self.recover()
            self.assertFalse(self.private.exists())

    def test_existing_matching_newline_keeps_bytes_permissions_mtime(self):
        self.private.mkdir(mode=0o700); p=self.private/'db-password'; p.write_text('a'*48+'\n'); p.chmod(0o600)
        before=p.stat().st_mtime_ns
        self.recover(); self.assertEqual(p.read_bytes(),('a'*48+'\n').encode()); self.assertEqual(p.stat().st_mtime_ns,before)

    def test_differing_or_non_private_output_refused_without_overwrite(self):
        self.private.mkdir(mode=0o700); p=self.private/'mq-password'; p.write_text('c'*48); p.chmod(0o600)
        with self.assertRaisesRegex(RuntimeError,'Differing'): self.recover()
        self.assertFalse((self.private/'db-password').exists()); self.assertEqual(p.read_text(),'c'*48)
        p.write_text('b'*48); p.chmod(0o644)
        with self.assertRaisesRegex(RuntimeError,'Unsafe existing'): self.recover()
        self.assertEqual(p.stat().st_mode & 0o777,0o644)

    def test_interruption_after_one_atomic_write_recovers_without_replacement(self):
        original=recovery.atomic_private; counter=0
        def interrupted(*args,**kwargs):
            nonlocal counter
            counter+=1
            if counter==2: raise RuntimeError('injected interruption')
            return original(*args,**kwargs)
        with patch.object(recovery,'atomic_private',side_effect=interrupted),self.assertRaises(RuntimeError): self.recover()
        p=self.private/'db-password'; before=(p.read_bytes(),p.stat().st_mtime_ns)
        self.recover(); self.assertEqual((p.read_bytes(),p.stat().st_mtime_ns),before)
        self.assertTrue((self.private/'ew-provision-inputs.yml').is_file())

    def test_interruption_after_atomic_link_publication_is_retryable(self):
        self.private.mkdir(mode=0o700)
        temporary=self.private/'private-temporary'; temporary.write_text('a'*48); temporary.chmod(0o600)
        target=self.private/'db-password'; os.link(temporary,target)
        before=(target.read_bytes(),target.stat().st_mtime_ns)
        self.recover()
        self.assertEqual((target.read_bytes(),target.stat().st_mtime_ns),before)
        self.assertTrue(temporary.is_file())  # no unrelated temporary file deletion

    def test_source_recheck_failure_blocks_all_local_writes(self):
        self.recheck.side_effect=RuntimeError('changed guest boot')
        with self.assertRaisesRegex(RuntimeError,'changed guest boot'): self.recover()
        self.assertFalse(self.private.exists())

    def test_password_parser_rejects_format_symlink_owner_and_mode(self):
        # Exercise actual adapter parser instead of reconstructing its rules.
        p=self.root/'password'; p.write_text('a'*48+'\n'); p.chmod(0o600)
        self.assertEqual(adapter.password(p),'a'*48)
        p.write_text('A'*48)
        with self.assertRaises(adapter.Refused): adapter.password(p)
        p.write_text('a'*48); p.chmod(0o644)
        with self.assertRaises(adapter.Refused): adapter.password(p)
        p.chmod(0o600); link=self.root/'symlink'; link.symlink_to(p)
        with self.assertRaises(adapter.Refused): adapter.password(link)
        with patch.object(pathlib.Path,'lstat',return_value=NS(st_mode=stat.S_IFREG|0o600,st_uid=1000,st_gid=0,st_nlink=1)),self.assertRaises(adapter.Refused): adapter.password(p)

    def test_recovery_playbook_does_not_include_guest_deployment_or_migration(self):
        for entry in ('ew-recover-credentials.yml','ew-bootstrap-check.yml'):
            plays=yaml.safe_load((ROOT/entry).read_text()); self.assertEqual(len(plays),1)
            self.assertFalse(any('import_playbook' in p for p in plays))
            source=(ROOT/entry).read_text()
            for forbidden in ('ew_provision.py','ew_workload.py','reset-lab','migrate-to-ovn','baseline'):
                self.assertNotIn(forbidden,source)
        self.assertIn('no_log: true',(ROOT/'ew-recover-credentials.yml').read_text())
        reset=(ROOT/'reset-lab-to-ovs.yml').read_text()
        self.assertNotIn('/root/ew-private',reset)
        self.assertIn('reset-ew-lab.yml',reset)
        # The reviewed reset now rejects evidence deletion, including overrides.
        workflow=(ROOT/'scripts/reset_workflow.py').read_text()
        self.assertIn("require(not spec['delete_backups']",workflow)


class AccessTests(unittest.TestCase):
    def test_private_directory_rejects_backup_source_and_traversal_paths(self):
        for path in ('/tmp/private','/root','/root/ovs-to-ovn-backup/private',
                     '/root/ovs-to-ovn-migration/private','/root/.ssh/private',
                     '/root/ew-private/../ovs-to-ovn-backup'):
            with self.subTest(path=path),self.assertRaises(RuntimeError): recovery.private_tree(path)

    def test_exact_reviewed_identity_and_boot_required_before_secret_reads(self):
        with tempfile.TemporaryDirectory() as d:
            root=pathlib.Path(d)
            for name in ('ew-config.json','ew-measurement-config.json'): (root/name).write_text('{}')
            guests={n:dict(server=n,port=n+'-port',ip='192.168.101.11',network=n+'-net') for n in ('ew-app','ew-db','ew-queue')}
            catalog=dict(servers=guests)
            reference=dict(guests={n:dict(v,boot='expected',network_type='vxlan') for n,v in guests.items()})
            path=root/'reference.json'; path.write_text(json.dumps(reference))
            cloud=Mock(); cloud.network.get_network.return_value=NS(provider_network_type='vxlan')
            cloud.network.get_port.return_value=NS(mac_address='fa:16:3e:00:00:01')
            tr=Mock(); tr.access.return_value={}
            tr.profile.side_effect=lambda vm,_:dict(server=vm['server'],boot='expected',
                interfaces=[dict(address='fa:16:3e:00:00:01',operstate='UP',addr_info=[dict(local=vm['ip'],family='inet')])],
                routes=[dict(dst='default',gateway='192.168.101.1')])
            with patch.object(access,'resolve_ew',return_value=catalog):
                _,observed=access.sources(root,path,cloud,tr); self.assertEqual(len(observed),3)
                for key,value in [('server','foreign'),('port','foreign'),('ip','192.168.101.99'),('boot','foreign')]:
                    old=reference['guests']['ew-app'][key]; reference['guests']['ew-app'][key]=value
                    path.write_text(json.dumps(reference))
                    with self.subTest(key=key),self.assertRaises(RuntimeError): access.sources(root,path,cloud,tr)
                    reference['guests']['ew-app'][key]=old
            tr.run.assert_not_called(); tr.install.assert_not_called()

    def test_read_only_validation_streams_check_and_does_not_deploy(self):
        with tempfile.TemporaryDirectory() as d:
            observed={n:dict(vm={},access={}) for n in ('ew-app','ew-db','ew-queue')}
            tr=Mock(); tr.guest_argv.return_value=['ssh','verified']; tr.run.return_value='{"status":"PASS"}'
            with patch.object(verify,'sources',return_value=(tr,observed)),patch.object(verify,'recheck'):
                result=verify.verify(pathlib.Path(d),'reference',Mock())
                self.assertEqual(result['status'],'PASS')
                self.assertTrue(all('apply' not in c.args[0][-1] for c in tr.run.call_args_list))
                self.assertEqual(sum('check' in c.args[0][-1] for c in tr.run.call_args_list),2)
                tr.install.assert_not_called(); tr.operation.assert_not_called()
                tr.run.return_value='{"status":"CONFLICT"}'
                self.assertEqual(verify.verify(pathlib.Path(d),'reference',Mock())['status'],'NOT_READY')


class IntegrationTests(unittest.TestCase):
    def test_reconstructed_check_apply_dispatch_and_conflict_never_applies(self):
        obj=object.__new__(provision.Provisioner); obj.changed=False; obj.timeout=30
        source=ROOT/'workloads/ew-bootstrap/adapter.py'
        obj.spec=dict(bootstrap={'ew-db':dict(path=str(source),sha256=provision.digest(source),interpreter='python3')})
        obj.install=Mock(); obj.guest=Mock(side_effect=['{"status":"CHANGE_REQUIRED"}','{"status":"PASS","changed":true}','{"status":"PASS"}'])
        obj.bootstrap('ew-db',{},{}); self.assertTrue(obj.changed)
        argv=[call.args[2] for call in obj.guest.call_args_list]
        self.assertEqual(argv,[['python3','/opt/ew-provision/bootstrap.py',mode,'ew-db'] for mode in ('check','apply','check')])
        obj.guest=Mock(return_value='{"status":"CONFLICT","reason":"Existing credentials conflict"}')
        with self.assertRaisesRegex(RuntimeError,'Existing credentials conflict'): obj.bootstrap('ew-db',{}, {})
        self.assertEqual(obj.guest.call_count,1)

    def test_legacy_bash_interface_accepts_successful_empty_apply_stdout(self):
        obj=object.__new__(provision.Provisioner); obj.changed=False; obj.timeout=30
        source=ROOT/'workloads/ew-bootstrap/controller-validation.sh'
        obj.spec=dict(bootstrap={'ew-db':dict(path=str(source),sha256=provision.digest(source))})
        obj.install=Mock(); obj.guest=Mock(side_effect=['{"status":"CHANGE_REQUIRED"}','','{"status":"PASS"}'])
        obj.bootstrap('ew-db',{},{}); self.assertTrue(obj.changed)
        self.assertEqual(obj.guest.call_args_list[1].args[2],['bash','/opt/ew-provision/bootstrap.sh','apply'])

    def test_adapter_source_change_is_rejected_again_before_guest_install(self):
        obj=object.__new__(provision.Provisioner); obj.changed=False
        obj.spec=dict(bootstrap={'ew-db':dict(path=str(ROOT/'workloads/ew-bootstrap/adapter.py'),sha256='0'*64)})
        obj.install=Mock()
        with self.assertRaisesRegex(RuntimeError,'changed since preflight'): obj.bootstrap('ew-db',{}, {})
        obj.install.assert_not_called()

    def test_missing_baked_application_module_is_clear_without_mutation(self):
        obj=object.__new__(provision.Provisioner); obj.guest=Mock(return_value='{"missing":["psycopg2"]}')
        with self.assertRaisesRegex(RuntimeError,'Missing baked guest dependencies.*psycopg2'): obj.baked_dependencies({}, {}, ['psycopg2'])


if __name__=='__main__': unittest.main()
