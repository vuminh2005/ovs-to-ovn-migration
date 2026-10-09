"""Offline EW lifecycle, identity, transport, raw coverage and application regressions."""
import base64
import copy
import hashlib
import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import yaml
from test_validation import ROOT, v, ready_evidence
from test_mtu_and_ew import inputs
import mtu_plan
import ew_workload as ew
import ew_transport as transport
import ew_api_observer as api

sys.path.insert(0,str(ew.METRICS))
import agent
import runner


def uid(name): return str(uuid.uuid5(uuid.NAMESPACE_DNS,name))


def fixture(root,mode='migration'):
    defaults=yaml.safe_load((ROOT/'group_vars/all.yml').read_text())
    definitions=defaults['ew_workload_config']
    cfg=dict(enabled=True,inventory='fixture.ini',namespace_hosts=['network1','network2','compute1','compute2'],
        guest_key='/existing/guest.key',guest_known_hosts='/existing/guest.hosts',host_key='/existing/host.key',
        host_known_hosts='/existing/host.hosts',guest_user='ubuntu',transport_timeout=20,readiness_timeout=120,
        collection_timeout=300,maximum_lifetime=3600,drain=30,interval=1,api_interval=2,recovery_seconds=30,
        baseline_seconds=120,max_rate=1,slo=10,stable_samples=5,maximum_collection_bytes=10000000)
    catalog=dict(ownership='external-existing-never-validation-owned',configuration=definitions,router=uid('router'),servers={})
    servers={}; ports={}; networks={}; profiles={}
    for row in definitions['servers']:
        name=row['name']; net=uid(row['network']); server=uid(name); port=uid(name+'port')
        fixed=[dict(subnet_id=uid(row['network']+'subnet'),ip_address=row['ip'])]
        vm=dict(server=server,port=port,network=net,ip=row['ip'],fixed_ips=fixed,actual_host=row['compute_host'],owned=False)
        catalog['servers'][name]=vm
        mac='02:00:00:00:00:'+str(len(profiles)+10)
        servers[server]=NS(id=server,status='ACTIVE',compute_host=row['compute_host'])
        ports[port]=NS(id=port,network_id=net,device_id=server,status='ACTIVE',fixed_ips=fixed,mac_address=mac)
        networks[net]=NS(id=net,mtu=1400,provider_network_type='vxlan')
        profiles[name]=dict(server=server,boot='boot-'+name,utc='2026-10-09T00:00:00+00:00',mono=100,
            interfaces=[dict(address=mac,operstate='UP',mtu=1400,ifname='ens3',addr_info=[dict(local=row['ip'],family='inet')])],
            routes=[dict(dst='default',gateway='192.168.0.1')])
    cloud=NS(compute=Mock(),network=Mock())
    cloud.compute.get_server.side_effect=servers.get; cloud.network.get_port.side_effect=ports.get
    cloud.network.get_network.side_effect=networks.get
    tr=Mock(); tr.access.return_value=dict(host='network1',namespace='qrouter-'+catalog['router'])
    tr.profile.side_effect=lambda vm,access:copy.deepcopy(profiles[next(n for n,v in catalog['servers'].items() if v['server']==vm['server'])])
    tr.operation.return_value=dict(e2e=True,dependencies=True)
    v.save(root/'ew-measurement-config.json',cfg); v.save(root/'ew-resources.json',catalog)
    v.save(root/'mtu-calculation.json',mtu_plan.calculate(inputs()))
    v.save(root/'network-mtu-plan.json',dict(networks=[dict(network=n,source_mtu=1400,target_mtu=1392) for n in networks]))
    obj=ew.Workload(root,cloud,tr,mode)
    guests={name:dict(boot=profiles[name]['boot'],source_mtu=1400,target_mtu=1392,
        mac=ports[vm['port']].mac_address) for name,vm in catalog['servers'].items()}
    session=dict(run_id='ew-fixture',status='RUNNING',guests=guests,runners={},launch={},readiness={})
    obj.state['sessions'][mode]=session
    session['runners']={n:obj.runner_config(n,session) for n in ew.ACTORS}; obj.commit()
    return NS(obj=obj,cfg=cfg,catalog=catalog,cloud=cloud,tr=tr,servers=servers,ports=ports,networks=networks,profiles=profiles)


def raw_actor(directory,cfg,failed=(),open_loss=False,gap=False,integrity=False,unresolved=False):
    directory.mkdir(parents=True,exist_ok=True)
    cfg=copy.deepcopy(cfg)
    v.save(directory/'config.json',cfg)
    with patch.object(runner.time,'monotonic',return_value=0): recorder=runner.Recorder(directory,[p['name'] for p in cfg['probes']])
    with patch.object(runner.time,'monotonic',return_value=0):
        recorder.event('run_start',run_id=cfg['run_id'])
        if cfg['tasks']:
            recorder.event('created',task_id='one'); recorder.event('accepted',task_id='one')
            if integrity: recorder.event('integrity_error',task_id='one')
            elif unresolved: recorder.event('unresolved',task_id='one')
            else: recorder.event('completed',task_id='one',latency_ms=2)
    for i in range(8):
        with patch.object(runner.time,'monotonic',return_value=i+(10 if gap and i>=4 else 0)):
            for probe in cfg['probes']:
                ok=not (i in failed or (open_loss and i>=5))
                if probe.get('source_boundary') and cfg['mode']=='migration': ok=False
                recorder.event(probe['name'],ok=ok)
    end=18 if gap else 8
    with patch.object(runner.time,'monotonic',return_value=end):
        recorder.event('run_end',probe_threads_stopped=True)
        stats=dict(accepted=1,done=1,processed=1,pending=0)
        recorder.finish(dict(cfg,guest_boot=cfg['boot']),stats if cfg['tasks'] else {},None)
    v.save(directory/'state.json',dict(status='COMPLETE',config=cfg))


class GuestLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root=pathlib.Path(self.tmp.name)
        f=fixture(self.root); self.cfg=f.obj.session()['runners']['ew-client-a1']
        self.cfg['output']=str(self.root/self.cfg['run_id'])
        self.patch=patch.object(agent,'ROOT',self.root); self.patch.start(); self.addCleanup(self.patch.stop)
        self.profile_patch=patch.object(agent,'profile',return_value=dict(boot=self.cfg['boot'])); self.profile_patch.start(); self.addCleanup(self.profile_patch.stop)

    def requested(self,status='START_REQUESTED'):
        directory=self.root/self.cfg['run_id']; directory.mkdir(exist_ok=True)
        v.save(directory/'state.json',dict(status=status,config=self.cfg,pid=123))
        return directory

    def test_launch_intent_precedes_systemd_and_stop_timeout_exceeds_drain(self):
        def cmd(argv,**kwargs):
            if argv[0]=='systemd-run':
                self.assertEqual(json.loads((self.root/self.cfg['run_id']/'state.json').read_text())['status'],'START_REQUESTED')
                self.assertEqual(json.loads((self.root/'active.json').read_text()),self.cfg)
                self.assertIn('--property=TimeoutStopSec=60',argv)
                self.assertIn('--property=RuntimeMaxSec=3660',argv)
            return ''
        with patch.object(agent,'command',side_effect=cmd),patch.object(agent.pwd,'getpwnam',return_value=NS(pw_uid=os.getuid(),pw_gid=os.getgid())):
            self.assertEqual(agent.start(self.cfg)['status'],'START_REQUESTED')

    def test_resume_running_unit_never_launches_a_duplicate(self):
        self.requested('RUNNING')
        with patch.object(agent,'command',return_value='ActiveState=active\nSubState=running\nMainPID=123\n') as cmd:
            self.assertEqual(agent.start(self.cfg)['status'],'RUNNING')
            self.assertFalse(any(c.args[0][0]=='systemd-run' for c in cmd.call_args_list))

    def test_crash_after_intent_is_ambiguous_and_cannot_restart(self):
        self.requested()
        with patch.object(agent,'command',return_value='ActiveState=inactive\nMainPID=0\n') as cmd:
            with self.assertRaisesRegex(RuntimeError,'ambiguous'): agent.start(self.cfg)
            self.assertFalse(any(c.args[0][0]=='systemd-run' for c in cmd.call_args_list))

    def test_exited_runner_without_summary_is_distinct_from_network_loss(self):
        self.requested('RUNNING')
        with patch.object(agent,'command',return_value='ActiveState=active\nSubState=exited\nMainPID=0\n'):
            self.assertEqual(agent.status(self.cfg)['status'],'CRASHED_OR_AMBIGUOUS')

    def test_changed_main_pid_cannot_be_used_as_runner(self):
        self.requested('RUNNING')
        with patch.object(agent,'command',return_value='ActiveState=active\nSubState=running\nMainPID=999\n'):
            self.assertEqual(agent.status(self.cfg)['status'],'CRASHED_OR_AMBIGUOUS')

    def test_global_slot_prevents_overlapping_runs(self):
        self.requested('RUNNING'); v.save(self.root/'active.json',self.cfg)
        other=dict(self.cfg,run_id='other',output=str(self.root/'other'))
        with patch.object(agent,'command',return_value='ActiveState=active\nSubState=running\nMainPID=123\n'):
            with self.assertRaisesRegex(RuntimeError,'already owns'): agent.start(other)
            self.assertFalse((self.root/'other').exists())

    def test_durable_stop_request_and_sigterm_target_only_measurement_unit(self):
        directory=self.requested('RUNNING')
        with patch.object(agent,'status',return_value=dict(status='RUNNING')),patch.object(agent,'command') as cmd:
            agent.stop(self.cfg)
            self.assertTrue((directory/'stop.request').exists())
            self.assertEqual(cmd.call_args.args[0],['systemctl','kill','--kill-who=main','--signal=SIGTERM',agent.unit(self.cfg)])

    def test_completed_stop_and_drain_are_idempotent(self):
        with patch.object(agent,'status',return_value=dict(status='COMPLETE')),patch.object(agent,'command') as cmd:
            agent.stop(self.cfg); self.assertEqual(agent.drain(self.cfg,1)['status'],'COMPLETE'); cmd.assert_not_called()

    def test_drain_timeout_retains_guest_state(self):
        directory=self.requested('RUNNING'); before=(directory/'state.json').read_bytes()
        with patch.object(agent,'status',return_value=dict(status='RUNNING')),patch.object(agent.time,'monotonic',side_effect=[0,0,2]),patch.object(agent.time,'sleep'):
            with self.assertRaises(TimeoutError): agent.drain(self.cfg,1)
        self.assertEqual((directory/'state.json').read_bytes(),before)

    def test_collection_is_bounded_allowlisted_and_preserves_raw_on_failure(self):
        directory=self.requested(); (directory/'events.jsonl').write_text('x'*100)
        (directory/'private-key').write_text('DO_NOT_ARCHIVE')
        with self.assertRaisesRegex(RuntimeError,'byte bound'): agent.collect(self.cfg,10)
        result=agent.collect(self.cfg,100000)
        self.assertEqual(result['status'],'INCOMPLETE'); self.assertNotIn('private-key',result['files'])
        self.assertEqual(base64.b64decode(result['files']['events.jsonl']['base64']),b'x'*100)

    def test_collection_rejects_symlinks_instead_of_archiving_unrelated_secrets(self):
        directory=self.requested(); secret=self.root/'unrelated-secret'; secret.write_text('DO_NOT_ARCHIVE')
        (directory/'events.jsonl').symlink_to(secret)
        with self.assertRaisesRegex(RuntimeError,'symlink'): agent.collect(self.cfg,100000)
        self.assertEqual(secret.read_text(),'DO_NOT_ARCHIVE')

    def diagnostic_fixture(self, text, code=0, maximum=100000, delay=0):
        directory=self.requested('COMPLETE')
        v.save(directory/'state.json',dict(status='COMPLETE',requested_utc='2026-10-09T00:00:00+00:00',
            started_utc='2026-10-09T00:01:00+00:00',finished_utc='2026-10-09T00:02:00+00:00'))
        popen=subprocess.Popen
        def local_fixture(argv,**kwargs):
            self.assertIn('--since=2026-10-09 00:01:00 UTC',argv)
            self.assertIn('--until=2026-10-09 00:02:00 UTC',argv)
            self.assertNotIn('-n',argv)  # no arbitrary journal tail
            script='import sys,time; sys.stdout.write('+repr(text)+'); sys.stdout.flush(); time.sleep('+str(delay)+'); sys.exit('+str(code)+')'
            return popen([sys.executable,'-c',script],**kwargs)  # harmless pipe fixture, never journalctl
        with patch.object(agent.subprocess,'Popen',side_effect=local_fixture):
            return agent.diagnostics(self.cfg,.05 if delay else 2,maximum)

    def test_diagnostics_are_run_scoped_and_exclude_secret_fields(self):
        row=dict(__REALTIME_TIMESTAMP='123',MESSAGE=json.dumps(dict(event='completed',task_id='one',password='DO_NOT_ARCHIVE')))
        result=self.diagnostic_fixture(json.dumps(row)+'\n')
        self.assertEqual(result['status'],'COMPLETE')
        self.assertEqual(result['events'][0]['message'],dict(event='completed',task_id='one'))
        self.assertNotIn('DO_NOT_ARCHIVE',json.dumps(result))

    def test_failed_or_bounded_diagnostics_are_not_complete(self):
        self.assertEqual(self.diagnostic_fixture('',code=1)['status'],'UNAVAILABLE')
        self.assertEqual(self.diagnostic_fixture('x'*10000,maximum=10)['status'],'TRUNCATED_OR_TIMED_OUT')
        self.assertEqual(self.diagnostic_fixture('',delay=2)['status'],'TRUNCATED_OR_TIMED_OUT')

    def test_boot_change_prohibits_lifecycle_reuse(self):
        self.requested('RUNNING')
        with patch.object(agent,'profile',return_value=dict(boot='another')):
            with self.assertRaisesRegex(RuntimeError,'boot changed'): agent.status(self.cfg)

    def test_nonfinite_lifetime_and_unsafe_paths_are_rejected(self):
        for change in (dict(maximum_lifetime=float('inf')),dict(drain=float('nan')),dict(output='/etc'),dict(run_id='../x')):
            with self.subTest(change=change),self.assertRaises(RuntimeError): agent.validate_config(dict(self.cfg,**change))


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root=pathlib.Path(self.tmp.name); self.f=fixture(self.root)

    def test_five_exact_flows_have_correct_placement_and_mtu_payloads(self):
        pairs=set()
        for cfg in self.f.obj.session()['runners'].values():
            for probe in cfg['probes']:
                if probe['type'] in ('live','dependency'): continue
                pairs.add(probe['name'].split('.')[0])
                if probe['type']=='df': self.assertEqual(probe['payload'],1372 if probe['source_boundary'] else 1364)
                if probe['type']=='ping': self.assertEqual(probe['payload'],56)
        self.assertEqual(pairs,{'ew-client-a1->ew-app','ew-client-a2->ew-app','ew-client-b->ew-app','ew-app->ew-queue','ew-app->ew-db'})
        app=self.f.obj.session()['runners']['ew-app']['probes'][0]['placement']
        self.assertTrue(app['same_compute']); self.assertFalse(app['same_subnet'])

    def test_source_identity_or_port_or_placement_changes_fail_without_cloud_writes(self):
        for fault in ('server','port','ip','host'):
            with self.subTest(fault=fault):
                f=fixture(self.root); vm=f.catalog['servers']['ew-app']
                if fault=='server': f.servers[vm['server']].id=uid('changed')
                elif fault=='port': f.ports[vm['port']].device_id=uid('changed')
                elif fault=='ip': f.ports[vm['port']].fixed_ips=[]
                else: f.servers[vm['server']].compute_host='compute2'
                with self.assertRaises(RuntimeError): f.obj.cloud_check('initial')
                f.cloud.compute.reboot_server.assert_not_called(); f.cloud.compute.delete_server.assert_not_called()
                f.cloud.network.update_network.assert_not_called()

    def test_pre_freeze_ew_target_mtu_is_not_silently_remediated(self):
        f=self.f
        for net in f.networks.values(): net.mtu=1392
        with self.assertRaisesRegex(RuntimeError,'prepare guest'): f.obj.readiness('pre-freeze')
        self.assertEqual(f.obj.session()['readiness']['pre-freeze']['status'],'FAIL')
        f.cloud.compute.reboot_server.assert_not_called(); f.tr.operation.assert_not_called()

    def test_real_e2e_readiness_not_health_alone(self):
        self.f.tr.operation.return_value=dict(e2e=False,dependencies=True)
        with self.assertRaisesRegex(RuntimeError,'end-to-end'): self.f.obj.readiness('initial')
        self.assertEqual(self.f.obj.session()['readiness']['initial']['tasks']['ew-client-a1']['status'],'FAIL')
        self.assertIn('task',self.f.tr.operation.call_args.args[2])

    def test_all_six_boots_and_interfaces_checked_before_readiness_pass(self):
        result=self.f.obj.readiness('initial')
        self.assertEqual(result['status'],'PASS'); self.assertEqual(len(result['guests']),6)
        self.assertEqual(set(result['tasks']),set(ew.ACTORS[:3]))

    def test_recovery_checks_target_geneve_and_original_boot(self):
        for net in self.f.networks.values(): net.mtu=1392; net.provider_network_type='geneve'
        for profile in self.f.profiles.values(): profile['interfaces'][0]['mtu']=1392
        self.f.profiles['ew-db']['boot']='rebooted'
        with self.assertRaisesRegex(RuntimeError,'reboot'): self.f.obj.readiness('recovery')

    def test_readiness_retry_retains_prior_task_ids_and_diagnostics(self):
        first=self.f.obj.readiness('initial'); self.f.obj.readiness('initial')
        self.assertEqual(self.f.obj.session()['readiness_attempts'][0],first)
        self.assertNotEqual(first['tasks']['ew-client-a1']['task']['task_id'],self.f.obj.session()['readiness']['initial']['tasks']['ew-client-a1']['task']['task_id'])

    def test_partial_startup_does_not_duplicate_ambiguous_actor(self):
        obj=self.f.obj; obj.session()['launch']['ew-client-a1']=dict(status='START_REQUESTED'); obj.commit()
        self.f.tr.operation.return_value=dict(status='ABSENT')
        with self.assertRaisesRegex(RuntimeError,'not automatically relaunched'): obj.start()
        self.assertTrue(all(call.args[2]['action']!='start' for call in self.f.tr.operation.call_args_list))

    def test_controller_launch_intent_is_saved_before_guest_start(self):
        obj=self.f.obj
        def operation(vm,access,payload,timeout):
            if payload['action']=='start':
                name=payload['config']['client_id']
                self.assertEqual(json.loads(obj.path.read_text())['sessions']['migration']['launch'][name]['status'],'START_REQUESTED')
                return dict(status='RUNNING')
            return dict(e2e=True,dependencies=True)
        self.f.tr.operation.side_effect=operation
        (obj.base/'api-events.jsonl').write_text('{}\n{}\n{}\n')
        with patch.object(obj,'observer_start'),patch.object(obj,'coverage',return_value={n:dict(last_event_seq=10) for n in ew.ACTORS}):
            obj.start()
        self.assertEqual(set(obj.session()['launch']),set(ew.ACTORS))
        self.assertEqual(obj.session()['status'],'RUNNING')

    def test_start_retry_preserves_first_coverage_without_duplicate_guest_launch(self):
        obj=self.f.obj; session=obj.session()
        session['launch']={n:dict(status='RUNNING') for n in ew.ACTORS}
        session['initial_coverage']={n:10 for n in ew.ACTORS}; session['initial_api_samples']=3
        self.f.tr.operation.return_value=dict(status='RUNNING')
        (obj.base/'api-events.jsonl').write_text('{}\n'*6)
        with patch.object(obj,'readiness'),patch.object(obj,'observer_start'),patch.object(obj,'coverage',return_value={n:dict(last_event_seq=200) for n in ew.ACTORS}):
            obj.start()
        self.assertEqual(session['initial_coverage'],{n:10 for n in ew.ACTORS})
        self.assertEqual(session['initial_api_samples'],3)
        self.assertTrue(all(c.args[2]['action']!='start' for c in self.f.tr.operation.call_args_list))

    def test_collection_transport_failure_is_incomplete_not_application_outage(self):
        self.f.tr.access.side_effect=RuntimeError('Management unavailable')
        with patch.object(self.f.obj,'observer_stop'):
            result=self.f.obj.finish()
        self.assertEqual(result['status'],'INCOMPLETE')
        report=ew.report_evidence(self.root)
        self.assertEqual(report['status'],'UNAVAILABLE')
        self.assertTrue(all(a['coverage']=='UNAVAILABLE' for a in report['sessions']['migration']['actors'].values()))

    def test_api_observer_ambiguous_start_never_duplicates_process(self):
        (self.f.obj.base/'api-start-intent.json').write_text('{}')
        with patch.object(ew.subprocess,'Popen') as popen:
            with self.assertRaisesRegex(RuntimeError,'ambiguous'): self.f.obj.observer_start()
            popen.assert_not_called()

    def test_existing_live_api_observer_reused_without_cloud_call(self):
        boot=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        v.save(self.f.obj.base/'api-state.json',dict(status='RUNNING',pid=1,process_identity='same',boot=boot,
            configuration=self.f.obj.observer_config()))
        with patch.object(ew,'process_identity',return_value='same'),patch.object(ew.subprocess,'Popen') as popen:
            self.f.obj.observer_start(); popen.assert_not_called()

    def test_old_successes_cannot_satisfy_fresh_recovery_fence(self):
        cfg=dict(probes=[dict(name='small',interval=1)])
        status=dict(status='RUNNING',current_mono=20,progress={'small':dict(seq=105,mono=19,successes=10,success_sequences=[97,98,99,100,105])})
        self.assertFalse(ew.stable(status,cfg,100,5))
        status['progress']['small']['success_sequences']=[101,102,103,104,105]
        self.assertTrue(ew.stable(status,cfg,100,5))
        status['progress']['small']['mono']=float('nan'); self.assertFalse(ew.stable(status,cfg,100,5))

    def test_stale_api_events_cannot_satisfy_pre_freeze_coverage(self):
        obj=self.f.obj; cfg=self.f.cfg
        v.save(obj.base/'api-state.json',dict(status='RUNNING',pid=123,process_identity='same',
            boot=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),configuration=obj.observer_config()))
        (obj.base/'api-events.jsonl').write_text(''.join(json.dumps(dict(ok=True,mono=1))+'\n' for _ in range(3)))
        obj.guest_call=Mock(return_value=dict(status='RUNNING'))
        with patch.object(ew,'process_identity',return_value='same'),patch.object(ew,'stable',return_value=True),\
                patch.object(ew.time,'monotonic',side_effect=[100,100,100,100,221,221]),patch.object(ew.time,'sleep'):
            with self.assertRaisesRegex(TimeoutError,'coverage'): obj.coverage('source')


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root=pathlib.Path(self.tmp.name); self.f=fixture(self.root)
        self.t=transport.Transport(self.f.cfg,self.f.catalog,inventory={'_meta':{'hostvars':{}}},cloud=self.f.cloud)
        self.vm=self.f.catalog['servers']['ew-app']

    def test_source_router_is_exact_catalog_uuid_without_hardcoded_host(self):
        ns='qrouter-'+self.f.catalog['router']
        self.t.host=Mock(side_effect=lambda host,argv:ns+' (id: 1)\n' if argv==['ip','netns','list'] else '')
        found=self.t.namespace(self.vm,'source')
        self.assertEqual(found['namespace'],ns); self.assertEqual(found['host'],'network1')
        self.assertIn(self.vm['ip'],self.t.host.call_args.args[1])

    def test_post_ovn_namespace_requires_exact_network_and_metadata_mac_ip(self):
        ns='ovnmeta-'+self.vm['network']; subnet=self.vm['fixed_ips'][0]['subnet_id']
        port=NS(id=uid('metadata'),mac_address='02:00:00:00:00:99',fixed_ips=[dict(subnet_id=subnet,ip_address='192.168.101.3')])
        self.f.cloud.network.ports.return_value=[port]
        def host(h,argv):
            if argv==['ip','netns','list']: return 'ovnmeta-'+uid('wrong-network')+'\n' if h=='network1' else ns+'\n'
            if argv[-2:]==['-j','address']: return json.dumps([dict(address=port.mac_address,addr_info=[dict(local='192.168.101.3')])])
            return ''
        self.t.host=Mock(side_effect=host)
        access=self.t.namespace(self.vm,'ovn')
        self.assertEqual(access['host'],'network2'); self.assertEqual(access['metadata_port'],port.id)
        self.f.cloud.network.ports.assert_called_once_with(network_id=self.vm['network'],device_owner='network:distributed')

    def test_missing_ambiguous_or_wrong_subnet_metadata_cannot_enable_transport(self):
        for ports in ([],[NS(fixed_ips=[])]):
            self.f.cloud.network.ports.return_value=ports
            with self.assertRaisesRegex(RuntimeError,'Missing/ambiguous'): self.t.namespace(self.vm,'ovn')
        subnet=self.vm['fixed_ips'][0]['subnet_id']
        p=NS(fixed_ips=[dict(subnet_id=subnet,ip_address='192.168.101.2')])
        self.f.cloud.network.ports.return_value=[p,p]
        with self.assertRaisesRegex(RuntimeError,'Missing/ambiguous'): self.t.namespace(self.vm,'ovn')

    def test_no_namespace_connectivity_produces_collection_failure_not_new_ports(self):
        self.t.host=Mock(side_effect=RuntimeError('Host down'))
        with self.assertRaisesRegex(RuntimeError,'local measurement evidence is retained'): self.t.namespace(self.vm,'source')
        self.f.cloud.network.create_port.assert_not_called()

    def test_guest_identity_boot_mac_mtu_and_route_are_independently_required(self):
        original=self.f.profiles['ew-app']
        for fault in ('server','boot','mac','mtu','route'):
            with self.subTest(fault=fault):
                profile=copy.deepcopy(original)
                if fault=='server': profile['server']=uid('other')
                elif fault=='boot': profile['boot']='other'
                elif fault=='mac': profile['interfaces'][0]['address']='other'
                elif fault=='mtu': profile['interfaces'][0]['mtu']=1450
                else: profile['routes']=[]
                with self.assertRaises(RuntimeError): transport.verify_profile(profile,self.vm,original['boot'],1400,original['interfaces'][0]['address'])

    def test_strict_host_keys_and_key_contents_are_never_read_or_archived(self):
        argv=self.t.guest_argv(self.vm,dict(host='network1',namespace='qrouter-'+self.f.catalog['router']))
        self.assertIn('StrictHostKeyChecking=yes',argv)
        self.assertNotIn('StrictHostKeyChecking=accept-new',argv)
        self.assertIn(self.f.cfg['guest_key'],argv)
        self.assertTrue(any('ProxyCommand=' in a and self.vm['ip'] in a for a in argv))

    def test_transport_process_has_a_total_deadline(self):
        self.t.deadline=time.monotonic()-1
        with patch.object(transport.subprocess,'run') as run:
            with self.assertRaises(TimeoutError): self.t.run(['ssh','fixture'])
            run.assert_not_called()

    def test_namespace_proxy_idle_bound_allows_bounded_guest_drain_to_finish(self):
        self.t.run=Mock(return_value='{}')
        self.t.operation(self.vm,dict(host='network1',namespace='qrouter-'+self.f.catalog['router']),
            dict(action='drain'),55)
        self.assertTrue(any('nc -w 55' in arg for arg in self.t.run.call_args.args[0]))
        self.assertEqual(self.t.run.call_args.args[2],55)

    def test_installed_helpers_remain_readable_under_restrictive_umask(self):
        import shlex
        self.t.run=Mock(return_value='')
        self.t.install(self.vm,dict(direct=self.vm['ip']))
        code=shlex.split(self.t.run.call_args.args[0][-1])[-1]
        destination=self.root/'installed'; code=code.replace('/opt/ew-load',str(destination))
        code='import os; os.umask(0o077)\n'+code
        data=json.dumps({n:base64.b64encode(b'# harmless fixture\n').decode() for n in ('agent.py','runner.py')})
        subprocess.run([sys.executable,'-c',code],input=data,text=True,check=True)
        self.assertEqual(destination.stat().st_mode & 0o777,0o755)
        self.assertEqual((destination/'runner.py').stat().st_mode & 0o777,0o644)


class ReportingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root=pathlib.Path(self.tmp.name); self.f=fixture(self.root)
        self.cfg=self.f.obj.session()['runners']['ew-client-a1']; self.actor=self.root/'actor'

    def complete(self):
        obj=self.f.obj; obj.readiness('initial')
        for net in self.f.networks.values(): net.mtu=1392
        for profile in self.f.profiles.values(): profile['interfaces'][0]['mtu']=1392
        obj.readiness('pre-freeze')
        for net in self.f.networks.values(): net.provider_network_type='geneve'
        obj.readiness('recovery'); obj.session()['readiness']['recovery']['observation_status']='PASS'
        session=obj.session(); session.update(status='COMPLETE',initial_api_samples=3,
            initial_coverage={n:10 for n in ew.ACTORS},pre_freeze_coverage={n:20 for n in ew.ACTORS},
            collection={n:dict(status='COMPLETE') for n in ew.ACTORS})
        for name,cfg in session['runners'].items(): raw_actor(obj.base/name,cfg)
        v.save(obj.base/'api-state.json',dict(status='COMPLETE',samples=8,started=0,ended=15,configuration=obj.observer_config()))
        (obj.base/'api-events.jsonl').write_text(''.join(json.dumps(dict(seq=i+1,mono=2*i,utc=str(i),ok=True))+'\n' for i in range(8)))
        obj.commit(); return session

    def test_complete_evidence_can_pass_and_reports_all_flows(self):
        self.complete(); result=ew.report_evidence(self.root)
        self.assertEqual(result['status'],'PASS')
        self.assertEqual(set(result['sessions']['migration']['actors']),set(ew.ACTORS))

    def test_missing_initial_or_fresh_readiness_cannot_pass_even_with_complete_raw_data(self):
        session=self.complete()
        for field in ('initial_coverage','pre_freeze_coverage','initial_api_samples'):
            with self.subTest(field=field):
                old=session.pop(field); self.f.obj.commit()
                self.assertEqual(ew.report_evidence(self.root)['status'],'UNAVAILABLE')
                session[field]=old
        session['readiness']['recovery'].pop('observation_status'); self.f.obj.commit()
        self.assertEqual(ew.report_evidence(self.root)['status'],'UNAVAILABLE')

    def test_empty_pass_readiness_objects_do_not_prove_guest_workloads(self):
        session=self.complete(); session['readiness']['pre-freeze']=dict(status='PASS'); self.f.obj.commit()
        self.assertEqual(ew.report_evidence(self.root)['status'],'UNAVAILABLE')

    def test_collected_configuration_cannot_be_rebased_to_a_different_vm(self):
        self.complete(); path=self.f.obj.base/'ew-app'/'config.json'; cfg=json.loads(path.read_text()); cfg['server']=uid('other'); v.save(path,cfg)
        self.assertEqual(ew.report_evidence(self.root)['status'],'UNAVAILABLE')

    def test_completed_collection_is_idempotent_without_transport(self):
        self.complete(); self.f.tr.reset_mock(); self.f.obj.finish()
        self.f.tr.operation.assert_not_called(); self.f.tr.access.assert_not_called()

    def test_malformed_optional_evidence_remains_reportable(self):
        for path in ('ew-lifecycle.json','ew-measurement-config.json'):
            with self.subTest(path=path):
                before=(self.root/path).read_bytes(); (self.root/path).write_text('{invalid')
                self.assertEqual(ew.report_evidence(self.root)['status'],'UNAVAILABLE')
                (self.root/path).write_bytes(before)

    def test_malformed_nested_readiness_and_collection_cannot_pass_or_break_report(self):
        session=self.complete()
        for field in ('readiness','collection','runners'):
            with self.subTest(field=field):
                old=session[field]; session[field]=None; self.f.obj.commit()
                self.assertEqual(ew.report_evidence(self.root)['status'],'UNAVAILABLE')
                session[field]=old

    def test_coverage_anchors_must_be_inside_collected_event_history(self):
        session=self.complete(); session['pre_freeze_coverage']['ew-app']=1000000; self.f.obj.commit()
        self.assertEqual(ew.report_evidence(self.root)['status'],'UNAVAILABLE')
        session['pre_freeze_coverage']['ew-app']=session['initial_coverage']['ew-app']; self.f.obj.commit()
        self.assertEqual(ew.report_evidence(self.root)['status'],'UNAVAILABLE')

    def test_recovered_observed_loss_retains_outage_and_migration_acceptance(self):
        raw_actor(self.actor,self.cfg,failed=(1,2))
        result=ew.metrics.assess(self.actor,'migration')
        self.assertEqual(result['status'],'PASS'); self.assertEqual(result['baseline_acceptance'],'FAIL')
        small=next(p for n,p in result['probes'].items() if n.endswith('.small'))
        self.assertEqual(small['failures'],2); self.assertEqual(small['windows'][0]['observed_outage_seconds'],2)
        self.assertIn('attempted',result['rates_per_second'])

    def test_source_boundary_loss_is_diagnostic_during_migration(self):
        raw_actor(self.actor,self.cfg)
        result=ew.metrics.assess(self.actor,'migration')
        self.assertEqual(result['status'],'PASS')
        boundary=next(p for n,p in result['probes'].items() if 'source-boundary' in n)
        self.assertEqual(boundary['failures'],8); self.assertFalse(boundary['recovered'])

    def test_unrecovered_required_probe_failure_fails_without_invented_outage(self):
        raw_actor(self.actor,self.cfg,open_loss=True)
        result=ew.metrics.assess(self.actor,'migration')
        self.assertEqual(result['status'],'FAIL'); self.assertEqual(result['recovery'],'FAIL')
        small=next(p for n,p in result['probes'].items() if n.endswith('.small'))
        self.assertIsNone(small['windows'][-1]['observed_outage_seconds'])

    def test_capture_gap_is_unavailable_not_counted_as_a_connectivity_failure(self):
        raw_actor(self.actor,self.cfg,gap=True)
        result=ew.metrics.assess(self.actor,'migration')
        self.assertEqual(result['coverage'],'UNAVAILABLE'); self.assertNotIn('probes',result)

    def test_missing_raw_sequence_cannot_pass(self):
        raw_actor(self.actor,self.cfg); path=self.actor/'events.jsonl'; lines=path.read_text().splitlines(); lines.pop(3); path.write_text('\n'.join(lines)+'\n')
        self.assertEqual(ew.metrics.assess(self.actor,'migration')['status'],'UNAVAILABLE')

    def test_missing_summary_or_changed_boot_cannot_pass(self):
        raw_actor(self.actor,self.cfg); path=self.actor/'summary.json'; summary=json.loads(path.read_text()); summary['guest_boot']='other'; path.write_text(json.dumps(summary))
        self.assertEqual(ew.metrics.assess(self.actor,'migration')['status'],'UNAVAILABLE')
        path.unlink(); self.assertEqual(ew.metrics.assess(self.actor,'migration')['status'],'UNAVAILABLE')

    def test_corruption_and_unresolved_tasks_fail_independently_of_connectivity(self):
        for fault in ('integrity','unresolved'):
            with self.subTest(fault=fault):
                path=self.root/fault; raw_actor(path,self.cfg,**{fault:True})
                result=ew.metrics.assess(path,'migration'); self.assertEqual(result['status'],'FAIL')
                self.assertEqual(result['recovery'],'PASS')

    def test_server_processed_count_mismatch_fails(self):
        raw_actor(self.actor,self.cfg); path=self.actor/'summary.json'; summary=json.loads(path.read_text()); summary['server_stats']['processed']=2; path.write_text(json.dumps(summary))
        self.assertEqual(ew.metrics.assess(self.actor,'migration')['task_reconciliation'],'FAIL')

    def test_disabled_is_not_tested_and_enabled_missing_evidence_unavailable(self):
        self.assertEqual(ew.report_evidence(self.root)['status'],'UNAVAILABLE')
        (self.root/'ew-measurement-config.json').unlink()
        self.assertEqual(ew.report_evidence(self.root),dict(status='NOT TESTED',enabled=False))

    def test_api_failure_burst_is_separate_monotonic_sampled_metric(self):
        directory=self.root/'api'; directory.mkdir()
        v.save(directory/'api-state.json',dict(status='COMPLETE',samples=4,started=10,ended=17))
        rows=[dict(seq=i+1,mono=10+2*i,utc=str(i),ok=i not in (1,2)) for i in range(4)]
        (directory/'api-events.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
        result=ew.observer_report(directory,self.f.cfg)
        self.assertEqual(result['status'],'PASS'); self.assertEqual(result['windows'][0]['observed_seconds'],4)
        rows[2]['mono']=100; (directory/'api-events.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
        self.assertEqual(ew.observer_report(directory,self.f.cfg)['status'],'UNAVAILABLE')

    def test_api_observer_instrumentation_failure_is_not_neutron_downtime(self):
        self.complete(); path=self.f.obj.base/'api-events.jsonl'
        rows=[json.loads(l) for l in path.read_text().splitlines()]
        rows[2].update(ok=False,error='SampleProcessFailure',observer_error=True)
        path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        result=ew.observer_report(self.f.obj.base,self.f.cfg)
        self.assertEqual(result['status'],'UNAVAILABLE'); self.assertNotIn('windows',result)

    def test_api_events_from_a_different_run_cannot_prove_this_runs_coverage(self):
        self.complete(); path=self.f.obj.base/'api-state.json'; state=json.loads(path.read_text())
        state['configuration']['run_id']='another-run'; v.save(path,state)
        self.assertEqual(ew.report_evidence(self.root)['status'],'UNAVAILABLE')

    def test_new_enabled_run_missing_config_is_unavailable_and_disabled_is_not_tested(self):
        (self.root/'ew-measurement-config.json').unlink()
        v.save(self.root/'runtime.json',dict(ew_measurement_schema_version=1,ew_workloads_enabled=True))
        self.assertEqual(ew.report_evidence(self.root)['status'],'UNAVAILABLE')
        v.save(self.root/'ew-measurement-config.json',dict(enabled=False))
        self.assertEqual(ew.report_evidence(self.root)['status'],'NOT TESTED')

    def test_migration_report_ew_failure_does_not_contaminate_pair_a_pcap(self):
        ready_evidence(self.root)
        subprocess.run([sys.executable,str(ROOT/'scripts/migration_report.py'),str(self.root),'test','fixture'],check=True,stdout=subprocess.DEVNULL)
        result=json.loads((self.root/'migration-report.json').read_text())
        self.assertEqual(result['result'],'MIGRATED_VALIDATION_INCOMPLETE')
        self.assertEqual(result['dataplane_probe']['packet_loss_percent'],0)
        self.assertEqual(result['dataplane_probe']['evidence_source'],'compute-tap-pcap')
        self.assertEqual(result['east_west_workload']['status'],'UNAVAILABLE')

    def test_full_ew_pass_preserves_success_and_pair_a_packet_source(self):
        self.complete(); ready_evidence(self.root)
        for name in ('pre-cleanup.json','post-cleanup.json'): v.save(self.root/name,dict(status='PASS'))
        subprocess.run([sys.executable,str(ROOT/'scripts/migration_report.py'),str(self.root),'test','fixture'],check=True,stdout=subprocess.DEVNULL)
        report=json.loads((self.root/'migration-report.json').read_text())
        self.assertEqual(report['result'],'SUCCESS'); self.assertEqual(report['east_west_workload']['status'],'PASS')
        self.assertEqual(report['dataplane_probe']['evidence_source'],'compute-tap-pcap')
        self.assertIn('Existing East-West application measurement: PASS',(self.root/'migration-report.txt').read_text())


class RunnerBoundTests(unittest.TestCase):
    def test_corruption_and_repeated_processing_have_distinct_raw_evidence(self):
        task=dict(task_id='one',payload='payload',run_id='r',client_id='c')
        for corrupt,count in ((True,1),(False,2)):
            with self.subTest(corrupt=corrupt,count=count),tempfile.TemporaryDirectory() as d:
                recorder=runner.Recorder(d)
                client=Mock(); client.request.return_value=(200,dict(job=dict(task_id='one',status='done',
                    result_sha256='bad' if corrupt else hashlib.sha256(b'payload').hexdigest(),process_count=count)),None)
                self.assertFalse(runner.run_task(client,task,recorder,lambda:time.monotonic()+1,10))
                recorder.stream.close(); row=json.loads((pathlib.Path(d)/'events.jsonl').read_text().splitlines()[-1])
                self.assertEqual(row['payload_corruption'],corrupt); self.assertEqual(row['process_count_invalid'],count!=1)
                self.assertEqual(row['process_count'],count)

    def test_probe_thread_crash_is_incomplete_not_measured_connectivity_loss(self):
        with tempfile.TemporaryDirectory() as d:
            root=pathlib.Path(d); f=fixture(root); cfg=f.obj.session()['runners']['ew-app']
            cfg.update(output=str(root/'run'),boot=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                maximum_lifetime=.15,duration=0,drain=.05,max_rate=100,
                probes=[dict(name='small',type='ping',host='127.0.0.1',port=1,interval=.01)])
            directory=pathlib.Path(cfg['output']); directory.mkdir(); v.save(directory/'state.json',dict(status='START_REQUESTED',config=cfg))
            v.save(directory/'config.json',cfg)
            with patch.object(sys,'argv',['runner.py','--config',str(directory/'config.json')]),patch.object(runner.signal,'signal'),patch.object(runner,'probe',side_effect=RuntimeError('fixture')):
                self.assertEqual(runner.managed_main(),0)
            rows=[json.loads(l) for l in (directory/'events.jsonl').read_text().splitlines()]
            self.assertTrue(any(r['kind']=='measurement_thread_crash' for r in rows))
            self.assertFalse(rows[-1]['probe_threads_stopped'])
            self.assertEqual(ew.metrics.assess(directory,'migration')['coverage'],'UNAVAILABLE')

    def test_small_ping_and_target_df_use_distinct_argv(self):
        with patch.object(runner.subprocess,'run',return_value=NS(returncode=0)) as run:
            runner.probe('ping','192.168.0.1',8080); self.assertIn('56',run.call_args.args[0]); self.assertIn('dont',run.call_args.args[0])
            runner.probe('ping','192.168.0.1',8080,1364,True); self.assertIn('1364',run.call_args.args[0]); self.assertIn('do',run.call_args.args[0])

    def test_managed_runner_finishes_at_unattended_limit_without_controller_or_api(self):
        with tempfile.TemporaryDirectory() as d:
            root=pathlib.Path(d); f=fixture(root); cfg=f.obj.session()['runners']['ew-app']
            cfg.update(output=str(root/'run'),boot=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),maximum_lifetime=.15,
                       duration=0,drain=.05,max_rate=100,probes=[dict(name='small',type='ping',host='127.0.0.1',port=1,interval=.01)])
            directory=pathlib.Path(cfg['output']); directory.mkdir(); v.save(directory/'state.json',dict(status='START_REQUESTED',config=cfg))
            v.save(directory/'config.json',cfg)
            with patch.object(sys,'argv',['runner.py','--config',str(directory/'config.json')]),patch.object(runner.signal,'signal'),patch.object(runner,'probe',return_value=(True,1,None)):
                start=time.monotonic(); self.assertEqual(runner.managed_main(),0); self.assertLess(time.monotonic()-start,2)
            state=json.loads((directory/'state.json').read_text()); self.assertEqual(state['status'],'COMPLETE')
            self.assertEqual(ew.metrics.assess(directory,'migration')['coverage'],'PASS')

    def test_freshness_uses_same_guest_monotonic_clock_not_controller_utc(self):
        cfg=dict(probes=[dict(name='small',interval=1)])
        status=dict(status='RUNNING',current_mono=10,progress={'small':dict(seq=5,mono=9,successes=5,success_sequences=[1,2,3,4,5])})
        self.assertTrue(ew.stable(status,cfg,0,5)); status['current_mono']=20; self.assertFalse(ew.stable(status,cfg,0,5))


class HookTests(unittest.TestCase):
    def test_ew_hooks_preserve_final_target_gate_and_pair_a_boundary(self):
        plays=yaml.safe_load((ROOT/'playbooks/07-migrate-db.yml').read_text()); tasks=[t for p in plays for t in p['tasks']]
        ew_gate=next(i for i,t in enumerate(tasks) if 'ew_workload.py' in t.get('ansible.builtin.shell',''))
        collect=next(i for i,t in enumerate(tasks) if 'begin-pre-freeze' in t.get('ansible.builtin.command',{}).get('argv',[]))
        freeze=next(i for i,t in enumerate(tasks) if '--freeze-start' in t.get('ansible.builtin.command',{}).get('argv',[]))
        self.assertLess(ew_gate,collect); self.assertLess(collect,freeze)
        tasks=[t for p in yaml.safe_load((ROOT/'playbooks/12-workload-validation.yml').read_text()) for t in p['tasks']]
        recovery=next(i for i,t in enumerate(tasks) if 'ew_workload.py' in t.get('ansible.builtin.shell',''))
        capture=next(i for i,t in enumerate(tasks) if 'capture-finish' in t.get('ansible.builtin.shell',''))
        self.assertLess(recovery,capture)

    def test_baseline_never_imports_cloud_mutating_migration_phases(self):
        imported=[p['import_playbook'] for p in yaml.safe_load((ROOT/'ew-baseline.yml').read_text()) if 'import_playbook' in p]
        self.assertEqual(imported,['playbooks/00-bootstrap.yml','playbooks/02-precheck.yml'])


if __name__=='__main__': unittest.main()
