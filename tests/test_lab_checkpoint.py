"""Offline only: filesystem fixtures and mocked cloud/host mutations."""
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
import uuid
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
import lab_checkpoint as c
import lab_checkpoint_host as h


def host_identity(host, machine_id='8879639716ca4bc5b019ff94b3b968df'):
    return dict(hostname=host,machine_id=machine_id,product_uuid=str(uuid.uuid5(uuid.NAMESPACE_DNS,'checkpoint-fixture-'+host)))


def config(root):
    return dict(id='cold-fixture',root=str(root),roles={'controller':'control','network1':'network','network2':'network','compute1':'compute','compute2':'compute'},
                headroom_bytes=100,archive_timeout=10,guest_timeout=1,health_timeout=1,transport_timeout=1,inventory='/root/multinode',venv='/root/venv',validation_runs=[])


def archive(path,members):
    with tarfile.open(path,'w') as tar:
        for name,kind,link in members:
            info=tarfile.TarInfo(name); info.type=kind; info.linkname=link
            if kind==tarfile.REGTYPE: info.size=1; tar.addfile(info,io.BytesIO(b'x'))
            else: tar.addfile(info)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root=Path(self.tmp.name); self.path=self.root/'data.tar'
    def test_valid_archive_and_corrupt_or_empty_archive(self):
        archive(self.path,[('data',tarfile.DIRTYPE,''),('data/disk',tarfile.REGTYPE,'')])
        self.assertEqual(h.verify_archive(self.path,['/data']),2)
        for data in (b'',b'not a tar',self.path.read_bytes()[:700]):
            with self.subTest(size=len(data)):
                self.path.write_bytes(data)
                with self.assertRaises((h.Refused,tarfile.TarError)): h.verify_archive(self.path,['/data'])
    def test_unsafe_paths_links_devices_and_duplicates(self):
        bad=[('../escape',tarfile.REGTYPE,''),('/etc/passwd',tarfile.REGTYPE,''),('other',tarfile.REGTYPE,''),
             ('data/link',tarfile.SYMTYPE,'../../escape'),('data/link',tarfile.SYMTYPE,'/etc/passwd'),
             ('data/device',tarfile.CHRTYPE,''),('data/link',tarfile.LNKTYPE,'data/missing')]
        for row in bad:
            with self.subTest(row=row):
                archive(self.path,[('data',tarfile.DIRTYPE,''),row])
                with self.assertRaises(h.Refused): h.verify_archive(self.path,['/data'])
        archive(self.path,[('data',tarfile.DIRTYPE,''),('data',tarfile.DIRTYPE,'')])
        with self.assertRaises(h.Refused): h.verify_archive(self.path,['/data'])
    def test_archive_cannot_write_through_contained_symlink(self):
        archive(self.path,[('data',tarfile.DIRTYPE,''),('data/link',tarfile.SYMTYPE,'dir'),('data/link/escape',tarfile.REGTYPE,'')])
        with self.assertRaises(h.Refused): h.verify_archive(self.path,['/data'])
    def test_sparse_archive_roundtrip_numeric_modes_and_symlinks(self):
        data=self.root/'data'; data.mkdir(); disk=data/'disk'
        with disk.open('wb') as f: f.seek(16*1024*1024); f.write(b'end')
        disk.chmod(0o640); (data/'link').symlink_to('disk')
        try: os.setxattr(disk,'user.checkpoint',b'kept')
        except OSError: pass
        subprocess.run(['tar','-c','--sparse','--format=pax','--acls','--xattrs','--numeric-owner','-f',str(self.path),'-C',str(self.root),'data'],check=True)
        h.verify_archive(self.path,['/data'])
        self.assertLess(self.path.stat().st_size,1024*1024)
        stage=self.root/'stage'; stage.mkdir()
        subprocess.run(['tar','-x','--sparse','--acls','--xattrs','--numeric-owner','-f',str(self.path),'-C',str(stage)],check=True)
        self.assertEqual((stage/'data/disk').stat().st_size,disk.stat().st_size)
        self.assertEqual((stage/'data/disk').stat().st_mode&0o777,0o640)
        self.assertEqual(os.readlink(stage/'data/link'),'disk')
        try: self.assertEqual(os.getxattr(stage/'data/disk','user.checkpoint'),b'kept')
        except OSError: pass


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root=Path(self.tmp.name); self.cfg=config(self.root)
    def test_storage_classification_fails_closed(self):
        for m in (dict(Type='volume',Name='unknown'),dict(Type='bind',Source='/srv/unknown',Destination='/data',RW=True),dict(Type='nfs')):
            with self.assertRaises(h.Refused): h.classify(m)
        self.assertEqual(h.classify(dict(Type='volume',Name='mariadb')),'durable')
        self.assertEqual(h.classify(dict(Type='volume',Name='neutron_metadata_socket')),'ephemeral')
        self.assertEqual(h.classify(dict(Type='bind',Source='/run',RW=True)),'ephemeral')
    def test_journal_host_input_requires_exact_read_only_bind(self):
        mount=dict(Type='bind',Source='/var/log/journal',Destination='/var/log/journal',RW=False)
        self.assertEqual(h.classify(mount),'host-input')
        variants=[dict(mount,RW=value) for value in (True,None,0,'false')]
        variants.append({k:v for k,v in mount.items() if k!='RW'})
        variants.extend(dict(mount,Source=source) for source in ('/var/log','/var/log/journal/subdir','/var/log/journal-other','/var/log/journal/'))
        variants.extend(dict(mount,Destination=destination) for destination in ('/var/log','/journal','/var/log/journal/subdir','/var/log/journal/',''))
        for variant in variants:
            with self.subTest(mount=variant),self.assertRaises(h.Refused): h.classify(variant)
    def test_journal_discovery_retains_host_input_but_excludes_durable_roots(self):
        journal=self.root/'journal'; journal.mkdir(); (journal/'host.log').write_text('host history')
        kolla=self.root/'kolla'; kolla.mkdir()
        ovs=self.root/'ovsdb'; ovs.mkdir()
        unit=self.root/'fluentd.service'; unit.write_text('fixture')
        boot=self.root/'boot'; boot.write_text('fixture-boot')
        unit_path='/etc/systemd/system/kolla-fluentd-container.service'
        paths={'/etc/kolla':kolla,'/var/log/journal':journal,unit_path:unit,'/proc/sys/kernel/random/boot_id':boot}
        mount=dict(Type='bind',Source='/var/log/journal',Destination='/var/log/journal',RW=False)
        rows=[dict(Name='/fluentd',Id='fluentd-id',Image='source-image',State=dict(Running=True),
                   HostConfig=dict(RestartPolicy=dict(Name='always')),Mounts=[mount])]
        cfg=dict(self.cfg,role='network')
        def command(argv,timeout=120):
            if argv[:3]==['docker','image','inspect']: return '[]'
            if argv==['docker','volume','ls','-q']: return 'openvswitch_db'
            if argv[:3]==['docker','volume','inspect']:
                return json.dumps([dict(Name='openvswitch_db',Driver='local',Options=None,Mountpoint=str(ovs))])
            if argv[:2]==['systemctl','show']:
                return 'LoadState=loaded\nFragmentPath='+unit_path+'\nDropInPaths=\nActiveState=active\nUnitFileState=enabled\n'
            self.fail('Unexpected discovery command: '+repr(argv))
        with patch.object(h,'Path',side_effect=lambda p:paths.get(str(p),Path(p))),patch.object(h,'containers',return_value=rows),patch.object(h,'command',side_effect=command),patch.object(h.shutil,'which',return_value='/usr/bin/lsof'),patch.object(h,'identity',return_value=dict(machine_id='fixture')),patch.object(h,'sizes',return_value=dict(apparent_bytes=0,allocated_bytes=0)) as sizes:
            plan=h.discover(cfg)
        self.assertEqual(plan['host_inputs'],[dict(path='/var/log/journal',realpath=str(journal))])
        self.assertEqual(set(plan['roots']),{str(kolla),str(ovs),str(unit)})
        self.assertEqual(set(map(str,sizes.call_args.args[0])),set(plan['roots']))
        self.assertEqual(plan['mounts'],[dict(mount,classification='host-input')])
        self.assertEqual(plan['containers'][0]['mounts'],[mount])
    def test_apparent_not_allocated_size_and_central_copy_space(self):
        plan=dict(sizes=dict(apparent_bytes=1000,allocated_bytes=1))
        with self.assertRaises(h.Refused): h.space(plan,2000,100)
        with self.assertRaises(h.Refused): h.space(plan,2200,100,central=1000)
        self.assertEqual(h.space(plan,3100,100,central=1000),3100)
    def test_incomplete_and_corrupt_manifest_never_restore(self):
        m=dict(schema_version=1,state='INCOMPLETE',nodes={},artifacts={})
        h.save(self.root/'manifest.json',m)
        with self.assertRaises(h.Refused): c.sealed(self.root)
        m.update(state='SEALED',nodes={str(i):{} for i in range(5)},artifacts={str(i):{} for i in range(5)})
        h.save(self.root/'manifest.json',m); h.save(self.root/'seal.json',dict(manifest_sha256='wrong'))
        with self.assertRaises(h.Refused): c.sealed(self.root)
    def test_wrong_host_missing_images_and_corrupt_inputs_stop_preflight(self):
        plan=dict(identity=host_identity('old'),containers=[dict(image='source-image')],roots=[str(self.root)],host_inputs=[],volumes=[])
        with patch.object(h,'identity',return_value=host_identity('wrong')),patch.object(h,'command') as cmd:
            with self.assertRaises(h.Refused): h.node_verify(self.cfg,plan,{})
            cmd.assert_not_called()
        with patch.object(h,'identity',return_value=plan['identity']),patch.object(h,'command',side_effect=h.Refused('image missing')):
            with self.assertRaisesRegex(h.Refused,'image missing'): h.node_verify(self.cfg,plan,{})
    def test_live_writers_prevent_archive(self):
        plan=dict(services={},roots=[])
        with patch.object(h,'containers',return_value=[dict(State=dict(Running=True))]),patch.object(h,'command') as cmd:
            with self.assertRaisesRegex(h.Refused,'Live'): h.stopped(plan)
            cmd.assert_not_called()
        plan['roots']=[str(self.root)]
        with patch.object(h,'containers',return_value=[]),patch.object(h.subprocess,'run',return_value=Mock(returncode=0,stdout='999',stderr='')):
            with self.assertRaisesRegex(h.Refused,'Open files'): h.stopped(plan)
    def test_interrupted_operations_are_not_replayed(self):
        h.save(self.root/'operations.json',dict(restore=dict(status='INTENT')))
        fn=Mock()
        with self.assertRaises(h.Refused): h.journal(self.root,'restore',fn)
        fn.assert_not_called()
        with self.assertRaises(ValueError): h.journal(self.root,'archive',Mock(side_effect=ValueError()))
        self.assertEqual(json.loads((self.root/'operations.json').read_text())['archive']['status'],'FAILED')
    def test_graceful_guest_timeout_has_no_destroy_fallback(self):
        with patch.object(h,'command',return_value='running') as cmd,patch.object(h.time,'monotonic',side_effect=[0,2]):
            with self.assertRaisesRegex(h.Refused,'shutdown timeout'): h.shutdown_guests(['uuid'],1)
            self.assertTrue(any('shutdown' in args.args[0] for args in cmd.call_args_list))
            self.assertFalse(any('destroy' in args.args[0] for args in cmd.call_args_list))
    def test_forced_database_exit_invalidates_checkpoint(self):
        plan=dict(services={},containers=[dict(id='db',name='mariadb',running=True)])
        with patch.object(h,'command',side_effect=['','',json.dumps([dict(State=dict(ExitCode=137,OOMKilled=False))])]):
            with self.assertRaisesRegex(h.Refused,'cleanly'): h.quiesce(plan)
    def test_restore_scope_rejects_unrelated_or_changed_placement(self):
        original=dict(servers={'ew':dict(host='compute1',status='ACTIVE')})
        current=dict(servers=dict(original['servers'],other=dict(host='compute2')))
        with self.assertRaises(h.Refused): c.restore_scope(current,original,[])
        current=copy.deepcopy(original); current['servers']['ew']['host']='compute2'
        with self.assertRaises(h.Refused): c.restore_scope(current,original,[])
    def test_all_node_preflight_precedes_any_mutation(self):
        obj=c.Checkpoint(self.cfg,Mock(),hosts=Mock())
        obj.restore_plan=Mock(side_effect=h.Refused('one host failed verification'))
        with self.assertRaises(h.Refused): obj.restore_apply()
        obj.hosts.call.assert_not_called()
        self.assertFalse((obj.root/'restore-state.json').exists())
    def test_all_writers_quiesced_before_first_archive(self):
        obj=c.Checkpoint(self.cfg,Mock(),hosts=Mock()); obj.root.mkdir()
        nodes={h:dict(identity=dict(hostname=h)) for h in self.cfg['roles']}
        resources={'vm':dict(host='compute1',status='ACTIVE')}
        obj.shutdown(nodes,resources,'backup')
        calls=obj.hosts.call.call_args_list
        self.assertEqual(calls[0].args,('compute1','shutdown'))
        self.assertEqual(sum(call.args[1]=='quiesce' for call in calls),5)
        self.assertFalse(any(call.args[1]=='archive' for call in calls))
    def test_restore_finish_requires_all_nodes_rebooted(self):
        obj=c.Checkpoint(self.cfg,Mock(),hosts=Mock()); obj.root.mkdir()
        h.save(obj.root/'restore-state.json',dict(status='DATA_RESTORED_REBOOT_REQUIRED',pre_restore_boots={'controller':'old'}))
        obj.verify=Mock(return_value=dict(nodes={'controller':dict(identity={'machine_id':'same'})}))
        obj.hosts.call.return_value=dict(identity={'machine_id':'same'},boot='old')
        with self.assertRaisesRegex(h.Refused,'reboot'): obj.restore_finish()
        self.assertEqual(len(obj.hosts.call.call_args_list),0)  # Incomplete five-host barrier fails before discovery.
    def test_no_migration_import_or_reset_invocation(self):
        root=Path(__file__).parents[1]
        self.assertNotIn('lab-checkpoint',(root/'migrate-to-ovn.yml').read_text())
        self.assertNotIn('reset-lab-to-ovs', (root/'scripts/lab_checkpoint.py').read_text())


class AdditionalSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root=Path(self.tmp.name); self.cfg=config(self.root/'checkpoints'); Path(self.cfg['root']).mkdir(mode=0o700)
    def test_backing_chains_must_exist_and_be_contained(self):
        base=self.root/'nova'; (base/'instances/_base').mkdir(parents=True); (base/'instances/vm').mkdir()
        (base/'instances/vm/disk').write_bytes(b'root'); (base/'instances/_base/image').write_bytes(b'base')
        mounts=[dict(classification='durable',Source=str(base),Destination='/var/lib/nova')]
        chain=[dict(filename='/var/lib/nova/instances/vm/disk',format='qcow2'),dict(filename='../_base/image',format='raw')]
        rows=h.backing_files(chain,mounts,[str(base)],'uuid')
        self.assertEqual(rows[1]['host_path'],str(base/'instances/_base/image'))
        (base/'instances/_base/image').unlink()
        with self.assertRaisesRegex(h.Refused,'Missing'): h.backing_files(chain,mounts,[str(base)],'uuid')
        with self.assertRaisesRegex(h.Refused,'outside'): h.backing_files([dict(filename='/other/disk',format='raw')],mounts,[str(base)],'uuid')
    def test_unrelated_network_or_image_blocks_restore(self):
        original=dict(servers={},networks={},images={})
        for kind in ('networks','images'):
            current=copy.deepcopy(original); current[kind]['extra']={}
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(h.Refused,'Unrelated'): c.restore_scope(current,original,[])
    def test_unique_distributed_ports_on_existing_network_are_migration_residue(self):
        original=dict(servers={},networks={'tenant':{}},ports={})
        current=copy.deepcopy(original); current['ports']['distributed']=dict(device_owner='network:distributed',network_id='tenant')
        self.assertEqual(c.restore_scope(current,original,[]),{})
        current['ports']['distributed']['network_id']='unrelated'
        with self.assertRaises(h.Refused): c.restore_scope(current,original,[])
    def test_exact_validation_checkpoint_metadata_required_for_extra_guest(self):
        run=self.root/'run'; run.mkdir()
        h.save(run/'validation-config.json',dict(run='owned-run'))
        vm=dict(server='owned',port='port',fixed_ips=[dict(subnet_id='subnet',ip_address='10.0.0.1')],owned=True)
        h.save(run/'validation-resources.json',dict(schema_version=2,pre=dict(measure={'0':vm})))
        current=dict(servers={'owned':dict(metadata=dict(ovn_migration_run='owned-run',ovn_validation_role='measure'))},
                     ports={'port':dict(device_id='owned',fixed_ips=vm['fixed_ips'])})
        self.assertIn('owned',c.validation_owned([str(run)],current))
        current['servers']['owned']['metadata']['ovn_validation_role']='existing'
        with self.assertRaises(h.Refused): c.validation_owned([str(run)],current)
    def test_actual_staged_replacement_quarantines_old_data(self):
        node=h.checkpoint_path(self.cfg); node.mkdir(parents=True)
        data=self.root/'durable'; data.mkdir(); (data/'file').write_text('checkpoint')
        archive_path=node/'data.tar'
        subprocess.run(['tar','-cf',str(archive_path),'-C','/',str(data).lstrip('/')],check=True)
        (data/'file').write_text('current')
        h.save(node/'containers.private.json',[])
        plan=dict(roots=[str(data)],volumes=[],services={})
        with patch.object(h,'stopped'),patch.object(h,'containers',return_value=[]),patch.object(h,'recreate') as recreate,patch.object(h,'command',side_effect=lambda argv,timeout=120:subprocess.check_output(argv,text=True) if argv[0]=='tar' else ''):
            result=h.restore_data(self.cfg,plan)
        self.assertEqual((data/'file').read_text(),'checkpoint')
        self.assertEqual((Path(result['quarantine'])/str(data).lstrip('/')/'file').read_text(),'current')
        recreate.assert_called_once_with([])
        self.assertTrue(archive_path.exists())
    def test_partial_staging_is_not_overwritten(self):
        node=h.checkpoint_path(self.cfg); (node/'restore-stage').mkdir(parents=True)
        with patch.object(h,'stopped'),patch.object(h,'command') as cmd:
            with self.assertRaisesRegex(h.Refused,'Partial'): h.restore_data(self.cfg,dict(roots=[]))
            cmd.assert_not_called()
    def test_private_docker_recreation_uses_exact_image_and_no_restart(self):
        bind='/var/log/journal:/var/log/journal:ro'
        row=dict(Name='/source',Image='sha256:source',Config=dict(Image='mutable:tag',Env=['SECRET=private']),HostConfig=dict(RestartPolicy=dict(Name='always'),Binds=[bind]))
        connection=Mock(); connection.getresponse.return_value.status=201
        with patch.object(h,'UnixHTTP',return_value=connection): h.recreate([row])
        payload=json.loads(connection.request.call_args.kwargs['body'])
        self.assertEqual(payload['Image'],'sha256:source')
        self.assertEqual(payload['HostConfig']['RestartPolicy']['Name'],'no')
        self.assertEqual(payload['HostConfig']['Binds'],[bind])
        self.assertEqual(payload['Env'],['SECRET=private'])
    def test_controller_interrupt_retains_failed_intent_and_no_replay(self):
        obj=c.Checkpoint(self.cfg,Mock(),hosts=Mock()); obj.root.mkdir(parents=True)
        with self.assertRaises(ValueError): obj.event('restore-data-compute1',Mock(side_effect=ValueError()))
        fn=Mock()
        with self.assertRaises(h.Refused): obj.event('restore-data-compute1',fn)
        fn.assert_not_called()
        self.assertEqual(json.loads((obj.root/'controller-operations.json').read_text())['restore-data-compute1']['status'],'FAILED')
    def test_backup_seals_only_after_every_archive_and_health_check(self):
        obj=c.Checkpoint(self.cfg,Mock(),hosts=Mock()); node=dict(identity={'hostname':'controller'})
        manifest=dict(schema_version=1,id=self.cfg['id'],state='PLANNED',nodes={'controller':node},resources=dict(servers={}),artifacts={})
        obj.plan=Mock(return_value=manifest); obj.hosts.call.return_value=dict(private_sha256='private')
        obj.shutdown=Mock(); obj.collect=Mock(); obj.services=Mock()
        def healthy(*args): self.assertFalse((obj.root/'seal.json').exists())
        obj.healthy=Mock(side_effect=healthy)
        self.assertEqual(obj.create()['status'],'SEALED')
        self.assertEqual(json.loads((obj.root/'manifest.json').read_text())['state'],'SEALED')
    def test_archive_failure_cannot_seal_and_attempts_service_recovery(self):
        obj=c.Checkpoint(self.cfg,Mock(),hosts=Mock())
        manifest=dict(schema_version=1,id=self.cfg['id'],state='PLANNED',nodes={'controller':{}},resources=dict(servers={}),artifacts={})
        obj.plan=Mock(return_value=manifest); obj.hosts.call.return_value=dict(private_sha256='private')
        obj.shutdown=Mock(); obj.collect=Mock(side_effect=h.Refused('bad archive')); obj.services=Mock(); obj.healthy=Mock()
        obj.hosts.call.side_effect=lambda host,action,**kw: {} if action=='operation-state' else dict(private_sha256='private')
        with self.assertRaises(h.Refused): obj.create()
        self.assertFalse((obj.root/'seal.json').exists())
        self.assertEqual(json.loads((obj.root/'manifest.json').read_text())['state'],'INCOMPLETE')
        obj.services.assert_called_once_with(manifest,'backup-recovery')
    def test_read_only_plan_never_submits_application_tasks(self):
        source=(Path(__file__).parents[1]/'scripts/lab_checkpoint.py').read_text()
        self.assertIn("if name.startswith('ew-client-') and not initial:",source)
        self.assertIn('self.guest_health(manifest,initial=True)',source)
    def test_node_json_and_journals_are_private(self):
        path=self.root/'private.json'; h.save(path,dict(secret='fixture'))
        self.assertEqual(path.stat().st_mode&0o777,0o600)
    def test_no_confirmation_means_no_cloud_or_node_calls(self):
        cfg=dict(self.cfg,ew_workload_config={})
        with patch.object(c,'inventory_roles',return_value=self.cfg['roles']),patch.object(c.sys,'argv',['tool','restore-apply','--config','-']),patch.object(c.sys,'stdin',io.StringIO(json.dumps(cfg))),patch.object(c,'Checkpoint') as obj:
            with self.assertRaisesRegex(h.Refused,'confirmation'): c.main()
            obj.assert_not_called()

    def test_uncertain_remote_operation_blocks_backup_service_restart(self):
        obj=c.Checkpoint(self.cfg,Mock(),hosts=Mock())
        manifest=dict(schema_version=1,id=self.cfg['id'],state='PLANNED',nodes={'controller':{}},resources=dict(servers={}),artifacts={})
        obj.plan=Mock(return_value=manifest); obj.shutdown=Mock(); obj.services=Mock()
        obj.collect=Mock(side_effect=h.Refused('transport timeout'))
        obj.hosts.call.side_effect=lambda host,action,**kw: dict(archive=dict(status='INTENT')) if action=='operation-state' else dict(private_sha256='private')
        with self.assertRaises(h.Refused): obj.create()
        obj.services.assert_not_called()
        self.assertTrue((obj.root/'recovery-required.json').exists())
    def test_original_fixed_ip_change_blocks_restore(self):
        original=dict(servers={'ew':dict(host='compute1',status='ACTIVE')},ports={'port':dict(device_id='ew',network_id='net',fixed_ips=['original'],mac_address='mac')})
        current=copy.deepcopy(original); current['ports']['port']['fixed_ips']=['changed']
        with self.assertRaisesRegex(h.Refused,'port/IP/MAC'): c.restore_scope(current,original,[])


    def test_node_failure_reason_is_sanitized_without_echoing_command_secrets(self):
        hosts=c.Hosts(self.cfg)
        def response(argv,**kw):
            directory=Path(argv[-1]); h.save(directory/'controller',dict(rc=1,stdout=json.dumps(dict(status='FAILED',error='Refused',reason='Missing backing file'))))
            return Mock(returncode=2,stdout='SECRET=not-for-console',stderr='password=not-for-console')
        with patch.object(c.subprocess,'run',side_effect=response):
            with self.assertRaisesRegex(h.Refused,'controller: Missing backing file') as exc: hosts.module('controller','script','test')
        self.assertNotIn('SECRET',str(exc.exception)); self.assertNotIn('password',str(exc.exception))
    def test_backup_health_failure_is_journaled_and_cannot_seal(self):
        obj=c.Checkpoint(self.cfg,Mock(),hosts=Mock())
        manifest=dict(schema_version=1,id=self.cfg['id'],state='PLANNED',nodes={'controller':{}},resources=dict(servers={}),artifacts={})
        obj.plan=Mock(return_value=manifest); obj.hosts.call.return_value=dict(private_sha256='private')
        obj.shutdown=Mock(); obj.collect=Mock(); obj.services=Mock(); obj.healthy=Mock(side_effect=h.Refused('bad DHCP'))
        with self.assertRaises(h.Refused): obj.create()
        self.assertFalse((obj.root/'seal.json').exists())
        self.assertEqual(json.loads((obj.root/'controller-operations.json').read_text())['backup-complete-health']['status'],'FAILED')
    def test_restore_apply_executes_all_replacements_then_requires_reboots(self):
        obj=c.Checkpoint(self.cfg,Mock(),hosts=Mock())
        nodes={host:dict(boot='original',identity=dict(hostname=host)) for host in self.cfg['roles']}
        plan=dict(current=nodes,manifest=dict(nodes=nodes),resources=dict(servers={}))
        obj.restore_plan=Mock(return_value=plan); obj.shutdown=Mock()
        result=obj.restore_apply()
        self.assertEqual(result['status'],'DATA_RESTORED_REBOOT_REQUIRED')
        self.assertEqual(sum(call.args[1]=='restore' for call in obj.hosts.call.call_args_list),5)
        self.assertFalse(any(call.args[1]=='start' for call in obj.hosts.call.call_args_list))
    def test_failed_node_restore_cannot_advance_to_finish(self):
        obj=c.Checkpoint(self.cfg,Mock(),hosts=Mock())
        nodes={host:dict(boot='original',identity=dict(hostname=host)) for host in self.cfg['roles']}
        obj.restore_plan=Mock(return_value=dict(current=nodes,manifest=dict(nodes=nodes),resources=dict(servers={})))
        obj.shutdown=Mock(); obj.hosts.call.side_effect=[{},h.Refused('restore failed')]
        with self.assertRaises(h.Refused): obj.restore_apply()
        self.assertEqual(json.loads((obj.root/'restore-state.json').read_text())['status'],'INTENT')
        obj.verify=Mock(return_value={})
        with self.assertRaisesRegex(h.Refused,'Partial'): obj.restore_finish()


    def test_paused_or_building_workloads_fail_before_shutdown(self):
        for status in ('PAUSED','BUILD','ERROR','REBOOT'):
            current=dict(servers={'ew':dict(host='compute1',status=status)})
            with self.subTest(status=status):
                with self.assertRaisesRegex(h.Refused,'paused/error'): c.restore_scope(current,current,[])
    def test_host_checkpoint_root_must_be_private(self):
        path=Path(self.cfg['root']); path.chmod(0o755)
        with self.assertRaisesRegex(h.Refused,'0700'): h.checkpoint_path(self.cfg)
    def test_artifact_symlink_fails_archive_preflight(self):
        target=self.root/'real.tar'; archive(target,[('data',tarfile.DIRTYPE,'')])
        path=self.root/'alias.tar'; path.symlink_to(target)
        with self.assertRaisesRegex(h.Refused,'unsafe'): h.verify_archive(path,['/data'])


    def test_primary_glance_uses_real_kolla_container_name_and_local_volume(self):
        rows=[]
        for name,destination,volume in (('mariadb','/var/lib/mysql','mariadb'),('rabbitmq','/var/lib/rabbitmq','rabbitmq'),('glance_api','/var/lib/glance','glance'),('keystone','/etc/keystone/fernet-keys','keystone_fernet_tokens')):
            rows.append(dict(Name='/'+name,State=dict(Running=True),Mounts=[dict(Type='volume',Name=volume,Destination=destination)]))
        h.primary_storage(rows,'control')
        rows[2]['Name']='/glance'
        with self.assertRaisesRegex(h.Refused,'glance_api'): h.primary_storage(rows,'control')
        rows[2]['Name']='/glance_api'; rows[2]['Mounts']=[]
        with self.assertRaisesRegex(h.Refused,'local volume'): h.primary_storage(rows,'control')
    def test_stopped_primary_cannot_produce_an_apparently_complete_checkpoint(self):
        with self.assertRaisesRegex(h.Refused,'not running'): h.primary_storage([dict(Name='/mariadb',State=dict(Running=False))],'control')


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name); self.cfg=config(self.base/'checkpoints'); Path(self.cfg['root']).mkdir(mode=0o700)
        self.hosts=Mock(); self.factory=Mock(side_effect=AssertionError('API used before source startup'))
        self.obj=c.Checkpoint(self.cfg,None,hosts=self.hosts,cloud_factory=self.factory); self.obj.root.mkdir()
        self.nodes={host:dict(identity=host_identity(host),role=role,boot='before',domains=[],domain_states={},
                            domain_interfaces={},free_bytes=10000,sizes=dict(apparent_bytes=10)) for host,role in self.cfg['roles'].items()}
        self.nodes['compute1'].update(domains=['ew'],domain_states={'ew':'running'},domain_interfaces={'ew':[dict(port='port',mac='mac')]})
        self.resources=dict(servers={'ew':dict(id='ew',host='compute1',status='ACTIVE')},
                            ports={'port':dict(device_id='ew',network_id='net',mac_address='mac',fixed_ips=['ip'])})
        self.manifest=dict(nodes=copy.deepcopy(self.nodes),resources=self.resources)
        h.save(self.obj.root/'manifest.json',self.manifest)
        self.obj.verify=Mock(return_value=self.manifest)
        self.events=[]
        def call(host,action,**kw):
            self.events.append((host,action))
            if action=='discover-recovery': return copy.deepcopy(self.nodes[host])
            if action=='identity': return dict(identity=self.nodes[host]['identity'],boot='after')
            if action=='operation-state': return dict(restore=dict(status='COMPLETE'))
            if action=='resume-start': return dict(status='READY')
            return {}
        self.hosts.call.side_effect=call
        self.scope=self.base/'offline-scope.json'
        self.declare()
    def declare(self,journals=None):
        h.save(self.scope,dict(schema_version=1,checkpoint_manifest_sha256=h.digest(self.obj.root/'manifest.json'),
                             operator='fixture-operator',exclusive_lab_scope=True,no_unjournaled_resources=True,
                             no_concurrent_writers=True,ownership_journals=journals or {}))
        self.cfg.update(recovery_mode='offline',offline_scope_file=str(self.scope))
    def restored(self):
        self.obj.restore_apply()
        self.events.clear()
    def test_api_unavailable_preflight_discovers_all_nodes_and_preserves_evidence(self):
        result=self.obj.restore_plan()
        self.assertEqual(result['scope_verification'],'HOST_LIBVIRT_VERIFIED_API_SCOPE_OPERATOR_ATTESTED')
        self.assertEqual(len(self.events),5); self.factory.assert_not_called()
        self.assertFalse((self.obj.root/'restore-state.json').exists())
    def test_complete_offline_flow_starts_all_five_hosts_before_any_api_health(self):
        self.restored()
        for key in ('networks','subnets','routers','images','flavors','security_groups','security_group_rules','users','projects','roles','domains'):
            self.resources[key]={}
        self.manifest['original_guest_health']={'ew':dict(boot='guest-before')}
        cloud=Mock(); cloud.compute.get_server.return_value=Mock(status='ACTIVE')
        cloud.network.agents.return_value=[Mock(agent_type=kind,host=host,is_alive=True,is_admin_state_up=True)
            for host,role in self.cfg['roles'].items() for kind in
            ({'Open vSwitch agent','DHCP agent','L3 agent','Metadata agent'} if role=='network' else {'Open vSwitch agent'} if role=='compute' else set())]
        def snapshot(connection):
            self.assertIs(connection,cloud)
            self.assertEqual(sum(a=='resume-start' for _,a in self.events),5)
            self.assertEqual(sum(a=='identity' for _,a in self.events),5)
            return self.resources
        self.factory.side_effect=None; self.factory.return_value=cloud
        self.obj.guest_health=Mock(return_value={'ew':dict(boot='guest-after',status='PASS')})
        with patch.object(c,'cloud_snapshot',side_effect=snapshot) as api:
            self.assertEqual(self.obj.restore_finish()['status'],'RESTORED_HEALTHY')
        self.assertEqual(api.call_count,2); cloud.compute.wait_for_server.assert_called_once()
        self.assertEqual(json.loads((self.obj.root/'restore-complete-health.json').read_text())['status'],'PASS')
        self.factory.assert_called_once_with()
        self.assertEqual(sum(a=='restore' for _,a in self.events),0)
    def test_api_mode_rejects_unrelated_resources_before_any_mutation(self):
        self.cfg['recovery_mode']='api'; self.factory.side_effect=None
        current=copy.deepcopy(self.resources); current['servers']['unrelated']=dict(status='ACTIVE',host='compute2')
        with patch.object(c,'cloud_snapshot',return_value=current):
            with self.assertRaisesRegex(h.Refused,'Unrelated'): self.obj.restore_apply()
        self.assertFalse((self.obj.root/'restore-state.json').exists())
        self.assertTrue(all(a=='discover-recovery' for _,a in self.events))
    def test_offline_requires_explicit_private_checkpoint_bound_scope(self):
        for change in ('missing','public','wrong-seal','unasserted','wrong-journals'):
            with self.subTest(change=change):
                self.declare(); data=json.loads(self.scope.read_text())
                if change=='missing': self.scope.unlink()
                elif change=='public': self.scope.chmod(0o644)
                else:
                    if change=='wrong-seal': data['checkpoint_manifest_sha256']='wrong'
                    if change=='unasserted': data['no_unjournaled_resources']=False
                    if change=='wrong-journals': data['ownership_journals']={'unknown':'sha'}
                    h.save(self.scope,data)
                with self.assertRaises(h.Refused): self.obj.restore_apply()
                self.assertFalse((self.obj.root/'restore-state.json').exists())
                self.assertTrue(all(a=='discover-recovery' for _,a in self.events))
        self.factory.assert_not_called()
    def test_ambiguous_host_scope_blocks_all_mutation(self):
        for change in ('extra-domain','missing-domain','wrong-port','wrong-mac','wrong-host','paused'):
            original=copy.deepcopy(self.nodes)
            with self.subTest(change=change):
                node=self.nodes['compute1']
                if change=='extra-domain': node['domains'].append('unrelated')
                if change=='missing-domain': node['domains']=[]
                if change=='wrong-port': node['domain_interfaces']['ew'][0]['port']='other'
                if change=='wrong-mac': node['domain_interfaces']['ew'][0]['mac']='other'
                if change=='wrong-host': node['identity']['machine_id']='other'
                if change=='paused': node['domain_states']['ew']='paused'
                with self.assertRaises(h.Refused): self.obj.restore_apply()
                self.assertFalse((self.obj.root/'restore-state.json').exists())
                self.assertTrue(all(a=='discover-recovery' for _,a in self.events))
            self.nodes=original
        self.factory.assert_not_called()
    def test_stopped_source_and_guests_are_allowed_in_recovery(self):
        self.nodes['compute1']['domain_states']['ew']='shut off'
        plan=self.obj.restore_plan()
        self.assertEqual(plan['resources']['servers']['ew']['status'],'SHUTOFF')
        self.obj.restore_apply()
        self.assertFalse(any(a=='shutdown' for _,a in self.events))
    def test_changed_product_uuid_blocks_restore_before_any_mutation(self):
        self.nodes['controller']['identity']['product_uuid']=host_identity('replacement')['product_uuid']
        with self.assertRaisesRegex(h.Refused,'Wrong host'): self.obj.restore_apply()
        self.assertFalse((self.obj.root/'restore-state.json').exists())
        self.assertTrue(all(a=='discover-recovery' for _,a in self.events)); self.factory.assert_not_called()
    def test_changed_product_uuid_blocks_finalization_despite_new_boot(self):
        self.restored()
        self.nodes['controller']['identity']['product_uuid']=host_identity('replacement')['product_uuid']
        self.obj.healthy=Mock()
        with self.assertRaisesRegex(h.Refused,'same host'): self.obj.restore_finish()
        self.obj.healthy.assert_not_called(); self.factory.assert_not_called()
        self.assertFalse(any(a=='resume-start' for _,a in self.events))
    def test_exact_owned_validation_journals_authorize_extra_domain_only(self):
        run=self.base/'run'; run.mkdir(); self.cfg['validation_runs']=[str(run)]
        h.save(run/'validation-config.json',dict(run='run-id'))
        vm=dict(owned=True,server='validation',port='validation-port',fixed_ips=['journal-ip'])
        h.save(run/'validation-resources.json',dict(schema_version=2,pre=dict(existing={'0':vm})))
        journals={str(run/n):h.digest(run/n) for n in ('validation-config.json','validation-resources.json')}
        self.declare(journals)
        self.nodes['compute2'].update(domains=['validation'],domain_states={'validation':'running'},
                                      domain_interfaces={'validation':[dict(port='validation-port',mac='v-mac')]})
        self.assertEqual(self.obj.restore_plan()['validation_owned'],['validation'])
        vm['mac']='conflicting-journal-mac'; h.save(run/'validation-resources.json',dict(schema_version=2,pre=dict(existing={'0':vm})))
        self.declare({str(run/n):h.digest(run/n) for n in ('validation-config.json','validation-resources.json')})
        with self.assertRaisesRegex(h.Refused,'MAC differs'): self.obj.restore_plan()
        vm['owned']=False; h.save(run/'validation-resources.json',dict(schema_version=2,pre=dict(existing={'0':vm})))
        self.declare({str(run/n):h.digest(run/n) for n in ('validation-config.json','validation-resources.json')})
        with self.assertRaisesRegex(h.Refused,'ownership'): self.obj.restore_apply()
        self.assertFalse((self.obj.root/'restore-state.json').exists())
    def test_health_failure_then_finish_retry_without_data_restore(self):
        self.restored(); self.obj.healthy=Mock(side_effect=[h.Refused('temporary health failure'),dict(status='PASS')])
        with self.assertRaisesRegex(h.Refused,'temporary'): self.obj.restore_finish()
        self.assertEqual(json.loads((self.obj.root/'restore-state.json').read_text())['status'],'DATA_RESTORED_REBOOT_REQUIRED')
        self.assertEqual(self.obj.restore_finish()['status'],'RESTORED_HEALTHY')
        self.assertEqual(self.obj.healthy.call_count,2)
        self.assertFalse(any(a in ('restore','quiesce','shutdown') for _,a in self.events))
        ops=json.loads((self.obj.root/'controller-operations.json').read_text())
        self.assertEqual(ops['restore-complete-health']['attempts'][0]['status'],'FAILED')
        self.factory.assert_not_called()
    def test_completed_validation_cleanup_needs_receipt_and_no_remaining_domain(self):
        run=self.base/'run'; run.mkdir(); self.cfg['validation_runs']=[str(run)]
        h.save(run/'validation-config.json',dict(run='run-id'))
        vm=dict(owned=True,server='deleted-vm',port='deleted-port',fixed_ips=['ip'])
        h.save(run/'validation-resources.json',dict(schema_version=2,post=dict(cleaned=True,fresh={'0':vm})))
        def declare(): self.declare({str(p):h.digest(p) for p in run.iterdir()})
        declare()
        with self.assertRaisesRegex(h.Refused,'cleanup'): self.obj.restore_plan()
        h.save(run/'post-cleanup.json',dict(status='PASS',deleted=[dict(kind='server',id='deleted-vm'),dict(kind='port',id='deleted-port')]))
        declare(); self.assertEqual(self.obj.restore_plan()['validation_owned'],[])
        self.nodes['compute2'].update(domains=['deleted-vm'],domain_states={'deleted-vm':'shut off'},
                                      domain_interfaces={'deleted-vm':[dict(port='deleted-port',mac='mac')]})
        with self.assertRaisesRegex(h.Refused,'Unrelated'): self.obj.restore_apply()
        self.assertFalse((self.obj.root/'restore-state.json').exists())
    def test_inflight_controller_operation_blocks_finish_without_starts(self):
        self.restored(); path=self.obj.root/'controller-operations.json'; ops=json.loads(path.read_text())
        ops['restore-complete-start-controller']=dict(status='INTENT'); h.save(path,ops)
        self.obj.healthy=Mock()
        with self.assertRaisesRegex(h.Refused,'in-flight'): self.obj.restore_finish()
        self.obj.healthy.assert_not_called(); self.assertFalse(any(a=='resume-start' for _,a in self.events))
    def test_failed_or_inflight_node_data_restore_blocks_finish(self):
        for status in ('INTENT','FAILED','UNKNOWN'):
            with self.subTest(status=status):
                if not (self.obj.root/'restore-state.json').exists(): self.restored()
                base_call=self.hosts.call.side_effect
                def call(host,action,**kw):
                    if action=='operation-state' and host=='compute2': return dict(restore=dict(status=status))
                    return base_call(host,action,**kw)
                self.hosts.call.side_effect=call; self.events.clear(); self.obj.healthy=Mock()
                with self.assertRaisesRegex(h.Refused,'restore'): self.obj.restore_finish()
                self.obj.healthy.assert_not_called(); self.assertFalse(any(a=='resume-start' for _,a in self.events))
                self.hosts.call.side_effect=base_call
    def test_every_host_reboot_is_a_barrier_before_start(self):
        self.restored(); base_call=self.hosts.call.side_effect
        for unrebooted in self.nodes:
            def call(host,action,**kw):
                result=base_call(host,action,**kw)
                if action=='identity' and host==unrebooted: result['boot']='before'
                return result
            self.hosts.call.side_effect=call; self.events.clear(); self.obj.healthy=Mock()
            with self.assertRaisesRegex(h.Refused,'reboot'): self.obj.restore_finish()
            self.obj.healthy.assert_not_called(); self.assertFalse(any(a=='resume-start' for _,a in self.events))
        self.hosts.call.side_effect=base_call
    def test_incomplete_reboot_list_cannot_finish(self):
        self.restored(); path=self.obj.root/'restore-state.json'; state=json.loads(path.read_text())
        state['pre_restore_boots'].pop('compute2'); h.save(path,state); self.obj.healthy=Mock()
        with self.assertRaisesRegex(h.Refused,'barrier'): self.obj.restore_finish()
        self.obj.healthy.assert_not_called(); self.assertEqual(self.events,[])
    def test_verify_does_not_initialize_sdk(self):
        obj=c.Checkpoint(self.cfg,None,hosts=Mock(),cloud_factory=self.factory)
        self.assertIsNone(obj._cloud); self.factory.assert_not_called()


class RecoveryHostTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root=Path(self.tmp.name)
        self.plan=dict(containers=[dict(name='mariadb',image='source',mounts=[],running=True,restart=dict(Name='always'))],
                       services={'kolla-mariadb-container.service':dict(FragmentPath='/etc/systemd/system/kolla-mariadb-container.service',DropInPaths='',UnitFileState='enabled')})
        self.rows=[dict(Name='/mariadb',Image='source',Mounts=[],Config=dict(Labels=dict(kolla_version='18.8.1')),
                        HostConfig=dict(RestartPolicy=dict(Name='always'),NetworkMode='host'),State=dict(Running=True))]
        self.props='LoadState=loaded\nFragmentPath=/etc/systemd/system/kolla-mariadb-container.service\nDropInPaths=\nActiveState=active\nUnitFileState=enabled\n'
    def test_completed_start_is_reconciled_and_skipped(self):
        h.save(self.root/'operations.json',{'restore-complete-start':dict(status='COMPLETE')})
        with patch.object(h,'containers',return_value=self.rows),patch.object(h,'command',return_value=self.props) as cmd,patch.object(h,'start') as start:
            self.assertEqual(h.resume_start(self.root,self.plan)['status'],'READY')
            start.assert_not_called()
            self.assertTrue(all(args.args[0][:2]==['systemctl','show'] for args in cmd.call_args_list))
    def test_completed_but_stopped_service_is_restarted_after_reconciliation(self):
        h.save(self.root/'operations.json',{'restore-complete-start':dict(status='COMPLETE')})
        with patch.object(h,'start_state',side_effect=[dict(status='NOT_READY'),dict(status='READY')]),patch.object(h,'start',return_value=dict(status='STARTED')) as start:
            h.resume_start(self.root,self.plan); start.assert_called_once_with(self.plan)
        self.assertEqual(json.loads((self.root/'operations.json').read_text())['restore-complete-start-attempt-2']['status'],'COMPLETE')
    def test_failed_start_can_retry_only_after_reconciliation(self):
        h.save(self.root/'operations.json',{'restore-complete-start':dict(status='FAILED')})
        with patch.object(h,'start_state',side_effect=[dict(status='NOT_READY'),dict(status='READY')]),patch.object(h,'start',return_value={}) as start:
            h.resume_start(self.root,self.plan); start.assert_called_once()
    def test_node_inflight_or_unknown_operation_never_retried(self):
        for status in ('INTENT','UNKNOWN'):
            h.save(self.root/'operations.json',{'restore-complete-start':dict(status=status)})
            with patch.object(h,'start_state') as reconcile,patch.object(h,'start') as start:
                with self.assertRaisesRegex(h.Refused,'in-flight'): h.resume_start(self.root,self.plan)
                reconcile.assert_not_called(); start.assert_not_called()
    def test_wrong_container_image_mount_or_unknown_unit_state_blocks_retry(self):
        for change in ('image','mount','extra','activating','masked','fragment'):
            rows=copy.deepcopy(self.rows); props=self.props
            if change=='image': rows[0]['Image']='wrong'
            if change=='mount': rows[0]['Mounts']=[dict(Source='/other')]
            if change=='extra': rows.append(dict(rows[0],Name='/unrelated'))
            if change=='activating': props=props.replace('ActiveState=active','ActiveState=activating')
            if change=='masked': props=props.replace('UnitFileState=enabled','UnitFileState=masked')
            if change=='fragment': props=props.replace('/etc/systemd/system/','/other/')
            with self.subTest(change=change),patch.object(h,'containers',return_value=rows),patch.object(h,'command',return_value=props),patch.object(h,'start') as start:
                with self.assertRaises(h.Refused): h.resume_start(self.root,self.plan)
                start.assert_not_called()
    def test_primary_containers_may_be_stopped_only_for_recovery(self):
        rows=[dict(Name='/'+name,State=dict(Running=False),Mounts=[dict(Type='volume',Name=volume,Destination=path)])
              for name,path,volume in (('mariadb','/var/lib/mysql','mariadb'),('rabbitmq','/var/lib/rabbitmq','rabbitmq'),
                                      ('glance_api','/var/lib/glance','glance'),('keystone','/etc/keystone/fernet-keys','keystone_fernet_tokens'))]
        h.primary_storage(rows,'control',recovery=True)
        with self.assertRaises(h.Refused): h.primary_storage(rows,'control')
        rows[0]['Mounts']=[]
        with self.assertRaises(h.Refused): h.primary_storage(rows,'control',recovery=True)
    def test_stopped_libvirt_xml_and_port_identity_are_read_without_commands(self):
        root=self.root/'qemu'; root.mkdir()
        port='f2cbbef0-6af0-445a-b727-193702eaa0bf'; mac='fa:16:3e:ba:39:0b'
        xml=f'<domain><uuid>ew</uuid><devices><interface><mac address="{mac}"/><virtualport><parameters interfaceid="{port}"/></virtualport></interface></devices></domain>'
        (root/'instance.xml').write_text(xml)
        mounts=[dict(classification='durable',Source=str(self.root),Destination='/etc/libvirt')]
        with patch.object(h,'command') as cmd:
            domains=h.offline_domains(mounts); self.assertEqual(h.domain_ports(domains['ew'],'ew'),[dict(port=port,mac=mac)]); cmd.assert_not_called()
        (root/'instance.xml').unlink(); (root/'instance.xml').symlink_to(self.root/'missing')
        with self.assertRaises(h.Refused): h.offline_domains(mounts)
    def test_xml_missing_port_or_duplicate_uuid_is_ambiguous(self):
        import xml.etree.ElementTree as ET
        for xml in ('<domain><uuid>other</uuid></domain>','<domain><uuid>ew</uuid><devices><interface><mac address="mac"/></interface></devices></domain>'):
            with self.assertRaises(h.Refused): h.domain_ports(ET.fromstring(xml),'ew')
    def test_host_qemu_processes_must_have_unique_known_uuid(self):
        proc=self.root/'proc'; proc.mkdir(); pid=proc/'100'; pid.mkdir()
        (pid/'cmdline').write_bytes(b'/usr/bin/qemu-system-x86_64\0-uuid\0aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa\0')
        real_path=Path
        with patch.object(h,'Path',side_effect=lambda p:proc if str(p)=='/proc' else real_path(p)):
            self.assertEqual(h.qemu_domains(),{'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'})
            (pid/'cmdline').write_bytes(b'/usr/bin/qemu-system-x86_64\0')
            with self.assertRaisesRegex(h.Refused,'Unidentified'): h.qemu_domains()


class NativePortTests(unittest.TestCase):
    domain='b162d212-73bc-4427-81ae-702f70cf7d30'
    port='f2cbbef0-6af0-445a-b727-193702eaa0bf'
    mac='fa:16:3e:ba:39:0b'
    tap='tapf2cbbef0-6a'
    def setUp(self):
        import xml.etree.ElementTree as ET
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root=Path(self.tmp.name)
        self.xml_text=f'<domain><uuid>{self.domain}</uuid><devices><interface type="ethernet"><mac address="{self.mac}"/><target dev="{self.tap}"/></interface><disk><source file="/var/lib/nova/instances/{self.domain}/disk"/></disk></devices></domain>'
        self.xml=ET.fromstring(self.xml_text)
        self.ovs=[dict(name=self.tap,external_ids={'iface-id':self.port,'attached-mac':self.mac})]
        self.cfg=dict(config(self.root/'checkpoints'),role='compute')
        self.volumes=[]; self.paths={}; self.rows=[]; self.running=False; self.inactive=self.xml_text
        for name in ('nova_compute','libvirtd','nova_libvirt_qemu','openvswitch_db'):
            path=self.root/name; path.mkdir()
            self.volumes.append(dict(Name=name,Driver='local',Options=None,Mountpoint=str(path)))
        qemu=self.root/'nova_libvirt_qemu'; (qemu/'instance.xml').write_text(self.xml_text)
        disk=self.root/'nova_compute/instances'/self.domain/'disk'; disk.parent.mkdir(parents=True); disk.write_bytes(b'fixture')
        self.database=self.root/'openvswitch_db/current-database'; self.database.write_bytes(b'OVSDB JSON fixture')
        for name,mounts in (('nova_libvirt',[('nova_compute','/var/lib/nova'),('libvirtd','/var/lib/libvirt'),('nova_libvirt_qemu','/etc/libvirt/qemu')]),('openvswitch_db',[('openvswitch_db','/var/lib/openvswitch')])):
            unit=f'/etc/systemd/system/kolla-{name}-container.service'; path=self.root/(name+'.service'); path.write_text('fixture'); self.paths[unit]=path
            self.rows.append(dict(Name='/'+name,Id=name,Image='source-image',State=dict(Running=False),HostConfig=dict(RestartPolicy=dict(Name='always')),
                                  Mounts=[dict(Type='volume',Name=v,Source=str(self.root/v),Destination=d) for v,d in mounts]))
        kolla=self.root/'kolla'; kolla.mkdir(); self.paths['/etc/kolla']=kolla
        boot=self.root/'boot'; boot.write_text('boot'); self.paths['/proc/sys/kernel/random/boot_id']=boot
    def command(self,argv,timeout=120):
        if argv[:3]==['docker','image','inspect']: return '[]'
        if argv==['docker','volume','ls','-q']: return '\n'.join(v['Name'] for v in self.volumes)
        if argv[:3]==['docker','volume','inspect']: return json.dumps(self.volumes)
        if argv[:2]==['systemctl','show']:
            return 'LoadState=loaded\nFragmentPath=/etc/systemd/system/'+argv[2]+'\nDropInPaths=\nActiveState=inactive\nUnitFileState=disabled\n'
        if argv[:2]==['/usr/bin/ovsdb-tool','db-name']: return 'Open_vSwitch'
        if argv[:2]==['/usr/bin/ovsdb-tool','query']:
            self.assertEqual(argv[2],str(self.database))
            self.assertEqual(json.loads(argv[3]),['Open_vSwitch',dict(op='select',table='Interface',where=[],columns=['name','external_ids'])])
            return json.dumps([dict(rows=[dict(name=r['name'],external_ids=['map',list(map(list,r['external_ids'].items()))]) for r in self.ovs])])
        if self.running and argv[:3]==['docker','exec','nova_libvirt']:
            if argv[3:5]==['virsh','list']: return self.domain
            if argv[3:5]==['virsh','dumpxml']: return self.inactive if '--inactive' in argv else self.xml_text
            if argv[3:5]==['virsh','domstate']: return 'running'
            if argv[3:5]==['virsh','dominfo']: return 'Autostart: disable'
            if argv[3:5]==['qemu-img','info']: return json.dumps([dict(filename=f'/var/lib/nova/instances/{self.domain}/disk',format='raw')])
        self.fail('Unexpected command/service startup: '+repr(argv))
    def discover(self,missing_tool=False):
        with patch.object(h,'Path',side_effect=lambda p:self.paths.get(str(p),Path(p))),patch.object(h,'containers',return_value=self.rows),patch.object(h,'command',side_effect=self.command) as cmd,patch.object(h.shutil,'which',side_effect=lambda name:None if missing_tool and name=='ovsdb-tool' else '/usr/bin/'+name),patch.object(h,'identity',return_value=host_identity('compute1')),patch.object(h,'qemu_domains',return_value={self.domain} if self.running else set()),patch.object(c,'cloud_snapshot',side_effect=AssertionError('Offline discovery used an API')) as api:
            plan=h.discover(self.cfg,recovery=True)
        api.assert_not_called()
        self.calls=cmd.call_args_list
        return plan
    def test_observed_native_layout_uses_full_current_ovs_uuid_without_vm_id(self):
        self.assertEqual(h.domain_ports(self.xml,self.domain,self.ovs),[dict(port=self.port,mac=self.mac)])
        self.assertNotIn('vm-id',self.ovs[0]['external_ids'])
        # A full UUID comes only from OVS, even when its TAP prefix differs.
        self.ovs[0]['external_ids']['iface-id']='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        self.assertEqual(h.domain_ports(self.xml,self.domain,self.ovs)[0]['port'],'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa')
    def test_explicit_xml_id_is_supported_and_current_ovs_conflicts_refuse(self):
        import xml.etree.ElementTree as ET
        xml=ET.fromstring(self.xml_text.replace('<target',f'<virtualport><parameters interfaceid="{self.port}"/></virtualport><target'))
        self.assertEqual(h.domain_ports(xml,self.domain),[dict(port=self.port,mac=self.mac)])
        self.assertEqual(h.domain_ports(xml,self.domain,self.ovs),[dict(port=self.port,mac=self.mac)])
        wrong=copy.deepcopy(self.ovs); wrong[0]['external_ids']['iface-id']='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        with self.assertRaisesRegex(h.Refused,'conflicts'): h.domain_ports(xml,self.domain,wrong)
    def test_native_wrong_mac_uuid_duplicate_or_missing_evidence_refuses(self):
        for change in ('mac','missing-mac','short-uuid','invalid-uuid','missing-uuid','duplicate-target','duplicate-port','missing-target','missing-evidence'):
            ovs=copy.deepcopy(self.ovs)
            if change=='mac': ovs[0]['external_ids']['attached-mac']='fa:16:3e:00:00:00'
            if change=='missing-mac': ovs[0]['external_ids'].pop('attached-mac')
            if change=='short-uuid': ovs[0]['external_ids']['iface-id']='f2cbbef0-6a'
            if change=='invalid-uuid': ovs[0]['external_ids']['iface-id']='z'*36
            if change=='missing-uuid': ovs[0]['external_ids'].pop('iface-id')
            if change=='duplicate-target': ovs.append(copy.deepcopy(ovs[0]))
            if change=='duplicate-port': ovs.append(dict(ovs[0],name='another-tap'))
            if change=='missing-target': ovs[0]['name']='another-tap'
            if change=='missing-evidence': ovs=None
            with self.subTest(change=change),self.assertRaises(h.Refused): h.domain_ports(self.xml,self.domain,ovs)
    def test_stopped_services_discovery_reads_current_persistent_ovsdb_without_startup(self):
        plan=self.discover()
        self.assertEqual(plan['domain_interfaces'],{self.domain:[dict(port=self.port,mac=self.mac)]})
        self.assertEqual(plan['domain_states'],{self.domain:'shut off'})
        self.assertEqual(plan['ovs_interface_evidence']['database'],str(self.database))
        self.assertFalse(any('exec' in c.args[0] or 'start' in c.args[0] or 'run' in c.args[0] or 'transact' in c.args[0] for c in self.calls))
        self.assertEqual(self.database.read_bytes(),b'OVSDB JSON fixture')
    def test_stopped_service_missing_tools_or_current_evidence_blocks_replacement(self):
        for change in ('tool','database','mapping'):
            with self.subTest(change=change):
                if change=='database': self.database.unlink()
                if change=='mapping': self.database.write_bytes(b'OVSDB JSON fixture'); self.ovs=[]
                obj=c.Checkpoint(self.cfg,Mock(),hosts=Mock()); obj.verify=Mock(return_value={})
                obj.hosts.call.side_effect=lambda *a,**kw:self.discover(missing_tool=change=='tool')
                with self.assertRaises(h.Refused): obj.restore_apply()
                self.assertFalse((obj.root/'restore-state.json').exists())
                self.assertTrue(all(c.args[1]=='discover-recovery' for c in obj.hosts.call.call_args_list))
    def test_running_active_and_inactive_xml_must_resolve_same_current_ports(self):
        self.running=True; self.rows[0]['State']['Running']=True
        self.assertEqual(self.discover()['domain_interfaces'][self.domain],[dict(port=self.port,mac=self.mac)])
        self.inactive=self.xml_text.replace(self.mac,'fa:16:3e:00:00:00')
        with self.assertRaisesRegex(h.Refused,'MAC'): self.discover()
        self.inactive=self.xml_text.replace(self.tap,'missing-tap')
        with self.assertRaisesRegex(h.Refused,'OVS Interface'): self.discover()
    def test_duplicate_current_database_is_ambiguous(self):
        (self.database.parent/'second-database').write_bytes(b'OVSDB JSON fixture')
        with self.assertRaisesRegex(h.Refused,'ambiguous'): self.discover()
    def test_two_domains_cannot_claim_one_current_ovs_port(self):
        other=self.xml_text.replace('<uuid>'+self.domain+'</uuid>','<uuid>aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa</uuid>')
        (self.root/'nova_libvirt_qemu/other.xml').write_text(other)
        with self.assertRaisesRegex(h.Refused,'Duplicate Neutron port'): self.discover()
    def test_malformed_ovs_json_is_not_port_evidence(self):
        for output in ('invalid','[]','[{"error":"failed"}]','[{"rows":[{"name":"tap","external_ids":["map",[["iface-id","one"],["iface-id","two"]]]}]}]'):
            with self.subTest(output=output),patch.object(h.shutil,'which',return_value='/usr/bin/ovsdb-tool'),patch.object(h,'command',side_effect=['Open_vSwitch',output]):
                with self.assertRaisesRegex(h.Refused,'Malformed'): h.ovs_interfaces([],self.volumes)
    def test_real_read_only_query_of_synthetic_standalone_database(self):
        import hashlib
        if not h.shutil.which('ovsdb-tool'): self.skipTest('Host ovsdb-tool unavailable for temporary-file query test')
        # Construct only a private temporary fixture; never create/transact a
        # database through OVS tooling or contact a running service.
        schema=dict(name='Open_vSwitch',version='1.0.0',tables={'Interface':dict(isRoot=True,columns={
            'name':dict(type='string'),'external_ids':dict(type=dict(key='string',value='string',min=0,max='unlimited'))})})
        transaction={'Interface':{'11111111-1111-1111-1111-111111111111':dict(name=self.tap,external_ids=['map',list(map(list,self.ovs[0]['external_ids'].items()))])}}
        records=[]
        for value in (schema,transaction):
            payload=(json.dumps(value,separators=(',',':'))+'\n').encode()
            records.append(f'OVSDB JSON {len(payload)} {hashlib.sha1(payload).hexdigest()}\n'.encode()+payload)
        self.database.write_bytes(b''.join(records)); before=h.digest(self.database)
        evidence=h.ovs_interfaces([],self.volumes)
        self.assertEqual(evidence['interfaces'],self.ovs)
        self.assertEqual(h.domain_ports(self.xml,self.domain,evidence['interfaces']),[dict(port=self.port,mac=self.mac)])
        self.assertEqual(h.digest(self.database),before)
    def test_source_creation_cross_checks_full_port_and_mac_against_api_catalog(self):
        cfg=config(self.root/'checkpoints'); node=self.discover()
        nodes={host:dict(identity=host_identity(host),domains=[],domain_states={},domain_interfaces={},containers=[],sizes=dict(apparent_bytes=1),free_bytes=10000) for host in cfg['roles']}
        nodes['compute1'].update(node); nodes['compute1']['identity']=host_identity('compute1')
        catalog=dict(servers={'ew':dict(server=self.domain,port=self.port,mac=self.mac,actual_host='compute1')})
        obj=c.Checkpoint(cfg,Mock(),hosts=Mock()); obj.hosts.call.side_effect=lambda host,*a,**kw:nodes[host]; obj.guest_health=Mock(return_value={})
        nodes['compute1']['domain_states'][self.domain]='running'
        with patch.object(c,'cloud_snapshot',return_value={}),patch.object(c,'ew_catalog',return_value=catalog):
            self.assertEqual(obj.plan()['ew'],catalog)
            for field,value in (('port','aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'),('mac','fa:16:3e:00:00:00')):
                old=catalog['servers']['ew'][field]; catalog['servers']['ew'][field]=value
                with self.subTest(field=field),self.assertRaisesRegex(h.Refused,'API catalog'): obj.plan()
                catalog['servers']['ew'][field]=old


class ProductIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root=Path(self.tmp.name)
        self.cfg=config(self.root/'checkpoints')
    def identity(self, product):
        def read(path):
            if str(path)=='/etc/machine-id': return '8879639716ca4bc5b019ff94b3b968df\n'
            self.assertEqual(str(path),'/sys/class/dmi/id/product_uuid')
            if product is None: raise FileNotFoundError('fixture missing DMI')
            return product
        with patch('platform.freedesktop_os_release',return_value=dict(ID='ubuntu',VERSION_ID='24.04')),patch.object(h.Path,'read_text',autospec=True,side_effect=read),patch.object(h.socket,'gethostname',return_value='controller'):
            return h.identity()
    def test_identity_canonicalizes_product_uuid_and_keeps_machine_id_hostname(self):
        expected=host_identity('controller')
        self.assertEqual(self.identity(expected['product_uuid'].upper()+'\n'),expected)
        self.assertEqual(set(expected),{'product_uuid','machine_id','hostname'})  # Boot ID remains separate.
    def test_missing_malformed_zero_and_ff_product_uuids_refuse(self):
        for product in (None,'','not-a-uuid','a'*32,'00000000-0000-0000-0000-000000000000','ffffffff-ffff-ffff-ffff-ffffffffffff','FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF'):
            with self.subTest(product=product),self.assertRaisesRegex(h.Refused,'product_uuid'): self.identity(product)
    def test_five_clones_require_distinct_valid_product_uuids_in_planning(self):
        nodes={host:dict(identity=host_identity(host),domains=[],domain_states={},containers=[],sizes=dict(apparent_bytes=1),free_bytes=10000) for host in self.cfg['roles']}
        obj=c.Checkpoint(self.cfg,Mock(),hosts=Mock()); obj.hosts.call.side_effect=lambda host,*a,**kw:nodes[host]; obj.guest_health=Mock(return_value={})
        with patch.object(c,'cloud_snapshot',return_value={}),patch.object(c,'ew_catalog',return_value=dict(servers={})):
            self.assertEqual(len({p['identity']['machine_id'] for p in obj.plan()['nodes'].values()}),1)
            self.assertEqual(len({p['identity']['product_uuid'] for p in nodes.values()}),5)
            original=nodes['compute1']['identity']['product_uuid']
            for value in (nodes['controller']['identity']['product_uuid'],None,'bad-uuid'):
                nodes['compute1']['identity']['product_uuid']=value
                with self.subTest(product=value),self.assertRaisesRegex(h.Refused,'product_uuid'): obj.plan()
            nodes['compute1']['identity']['product_uuid']=original
    def test_changed_product_uuid_blocks_node_and_controller_verify(self):
        original=host_identity('controller'); changed=dict(original,product_uuid=host_identity('replacement')['product_uuid'])
        plan=dict(identity=original,roots=[])
        with patch.object(h,'identity',return_value=changed),patch.object(h,'command') as command:
            with self.assertRaisesRegex(h.Refused,'Wrong host identity'): h.node_verify(self.cfg,plan,{})
            obj=c.Checkpoint(self.cfg,Mock(),hosts=Mock())
            manifest=dict(nodes={'controller':plan},artifacts={'controller':dict(sha256='sha',private_sha256='sha')})
            obj.cfg=dict(self.cfg,roles={'controller':'control'})
            obj.hosts.call.side_effect=lambda host,action,**kw:h.node_verify(self.cfg,kw['plan'],kw['artifact'])
            with patch.object(c,'sealed',return_value=manifest),patch.object(c,'digest',return_value='sha'),patch.object(c,'verify_archive'):
                with self.assertRaisesRegex(h.Refused,'Wrong host identity'): obj.verify()
            command.assert_not_called()
    def test_old_or_duplicate_identity_manifest_is_not_upgraded(self):
        root=h.checkpoint_path(self.cfg); root.mkdir(parents=True)
        nodes={host:dict(identity=host_identity(host)) for host in self.cfg['roles']}
        manifest=dict(schema_version=1,state='SEALED',nodes=nodes,artifacts={host:{} for host in nodes})
        def write():
            h.save(root/'manifest.json',manifest); h.save(root/'seal.json',dict(manifest_sha256=h.digest(root/'manifest.json')))
        write(); self.assertEqual(c.sealed(root),manifest)
        nodes['compute1']['identity'].pop('product_uuid'); write(); before=h.digest(root/'manifest.json')
        with self.assertRaisesRegex(h.Refused,'product_uuid'): c.sealed(root)
        self.assertEqual(h.digest(root/'manifest.json'),before)
        nodes['compute1']['identity']['product_uuid']=nodes['controller']['identity']['product_uuid']; write()
        with self.assertRaisesRegex(h.Refused,'distinct'): c.sealed(root)


if __name__=='__main__': unittest.main()
