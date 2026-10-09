"""Three-pair boundaries and at-most-once, validation-owned reboot safety."""
import copy
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from test_validation import v, packet, anchor, ready_evidence, ROOT


class Scenario:
    def __init__(self, root, stale=True, enabled=False):
        self.obj=v.Validation.__new__(v.Validation)
        self.obj.root=root; self.obj.path=root/'validation-resources.json'
        self.obj.cfg=dict(run='run',prefix='validation',image='image',flavor='flavor',
            interval=.2,timeout=1,dhcp_timeout=1,dhcp_t1=30,dhcp_t2=60,dhcp_renewal_tolerance=5,
            target_mtu=1442,source_mtu=1450,lifetime=1000,allow_pre_cutover_guest_reboot=enabled,
            cidrs={'pre':['10.0.0.0/24','10.0.1.0/24'],'post':['10.1.0.0/24','10.1.1.0/24']})
        self.obj.state={'schema_version':2}
        self.servers={}; self.ports={}; self.networks={}; self.boots={}; self.seq={}; self.logs={}
        self.health={}; self.failures=set(); self.reboots=[]; self.clock=0
        for stage,roles in [('pre',('measure','existing')),('post',('fresh',))]:
            topology={'router':stage+'-router','security_group':stage+'-sg','networks':{}}
            self.obj.state[stage]=topology
            for i in ('0','1'):
                net=stage+'-net'+i; sub=stage+'-sub'+i
                topology['networks'][i]=dict(network=net,subnet=sub,interface=True)
                self.networks[net]=NS(id=net,mtu=1442,provider_network_type='vxlan' if stage=='pre' else 'geneve')
            for role in roles:
                topology[role]={}; self.logs[role]={}; self.seq[role]={}; self.boots[role]={}; self.health[role]={}
                for i in ('0','1'):
                    server=role+i; port=role+'-port'+i
                    fixed=[{'subnet_id':stage+'-sub'+i,'ip_address':'10.'+('0' if stage=='pre' else '1')+'.'+i+'.'+('2' if role=='measure' else '4')}]
                    topology[role][i]=dict(server=server,port=port,network=stage+'-net'+i,subnet=stage+'-sub'+i,
                        fixed_ips=fixed,ip=fixed[0]['ip_address'],record_vm=role+i,name='validation-run-'+stage+'-'+role+i,owned=True)
                    self.servers[server]=NS(id=server,status='ACTIVE',metadata={'ovn_migration_run':'run','ovn_validation_role':role})
                    self.ports[port]=NS(id=port,device_id=server,fixed_ips=fixed,status='ACTIVE',binding_host_id='compute',binding_vif_type='ovs')
                    self.boots[role][i]=role+'-boot'+i; self.seq[role][i]=0; self.logs[role][i]=[]
                    self.health[role][i]=dict(dhcp=True,metadata=True,mtu=1450 if role=='measure' or (role=='existing' and stale) else 1442,
                        metadata_gateway='10.0.0.9',dhcp_t1_seconds=27,dhcp_t2_seconds=57,dhcp_ack_count=3,
                        dhcp_last_renewal_interval_seconds=28,configured_static_mtu=None,dhcp_use_mtu=True,mtu_configuration='dhcp_mtu_enabled')
        compute=Mock(); network=Mock(); image=Mock()
        compute.get_server.side_effect=lambda uuid:self.servers[uuid]
        compute.wait_for_server.side_effect=lambda server,**kw:server
        compute.reboot_server.side_effect=self.reboot
        compute.find_server.return_value=None
        network.get_port.side_effect=lambda uuid:self.ports[uuid]
        network.get_network.side_effect=lambda uuid:self.networks[uuid]
        network.security_group_rules.return_value=[NS(direction='ingress',protocol='icmp')]
        network.ports.side_effect=self.port_list
        image.find_image.return_value=NS(id='image'); compute.find_flavor.return_value=NS(id='flavor',disk=8,ram=1024)
        self.obj.cloud=NS(compute=compute,network=network,image=image)
        self.obj.collect=self.collect
        self.obj.commit()
        initial={i:dict(server='existing'+i,port='existing-port'+i,fixed_ips=self.ports['existing-port'+i].fixed_ips,
                        boot='existing-boot'+i) for i in ('0','1')}
        v.save(root/'existing-initial-baseline.json',initial)

    def port_list(self, **kw):
        if kw.get('device_owner')=='network:distributed':
            net=kw['network_id']; stage='pre' if net.startswith('pre') else 'post'; i=net[-1]
            return [NS(device_owner='network:distributed',fixed_ips=[{'subnet_id':stage+'-sub'+i,'ip_address':'10.0.0.9'}])]
        return []

    def monotonic(self):
        self.clock+=.4
        return self.clock

    def collect(self, stage, deadline=None):
        role=v.ROLES[stage][1]
        for i in ('0','1'):
            start=self.seq[role][i]
            for seq in range(start+1,start+11):
                row=packet(seq, not (role=='measure' and i=='0' and seq in self.failures),boot=self.boots[role][i])
                row['vm']=role+i; self.logs[role][i].append(row)
            self.seq[role][i]+=10
            seq=self.seq[role][i]
            health=dict(kind='health',seq=seq,boot=self.boots[role][i],vm=role+i,mono=seq*.2,**self.health[role][i])
            health.setdefault('dhcp_last_ack_monotonic',seq*.2-.1)
            self.logs[role][i].append(health)
            v.save(self.obj.root/(role+i+'-console-records.json'),self.logs[role][i])
        return copy.deepcopy(self.logs[role])

    def reboot(self, uuid, reboot_type):
        assert uuid.startswith('existing') and reboot_type=='SOFT'
        key=uuid[-1]
        persisted=v.read_evidence(self.obj.root,'existing-mtu-remediation.json')
        assert persisted['guests'][key]['reboot_requested'] is True
        self.reboots.append(uuid)
        self.boots['existing'][key]='existing-reboot'+key
        self.seq['existing'][key]=0
        self.health['existing'][key]['mtu']=1442


class ThreePairsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=pathlib.Path(self.temp.name)

    def scenario(self, **kw):
        scenario=Scenario(self.root,**kw)
        self.addCleanup(patch.stopall)
        patch.object(v.time,'sleep').start()
        patch.object(v.time,'monotonic',side_effect=scenario.monotonic).start()
        return scenario

    def test_pair_a_stale_mtu_and_failed_metadata_do_not_gate_measurement(self):
        s=self.scenario()
        for health in s.health['measure'].values(): health.update(metadata=False,dhcp=False)
        s.obj.wait_measure(initial=True)
        self.assertEqual(v.read_evidence(self.root,'measure-readiness.json')['status'],'PASS')
        s.obj.cloud.compute.reboot_server.assert_not_called()

    def test_pair_a_loss_counts_and_pair_b_reboot_packets_are_excluded(self):
        s=self.scenario(enabled=True)
        s.obj.wait_measure(initial=True); start=s.obj.checkpoint_start()
        s.failures={start['seq']+5,start['seq']+6,start['seq']+7}
        s.obj.prepare_dhcp(target=True)
        s.obj.wait_measure()
        result=v.read_evidence(self.root,'tenant-dataplane-probe.json')
        self.assertEqual(result['status'],'PASS'); self.assertEqual(result['packets_failed'],3)
        self.assertAlmostEqual(result['actual_dataplane_outage_seconds'],.6)
        self.assertEqual(result['measurement_workload'],'Pair A')
        self.assertEqual(result['pair_a_boot_continuity'],'PASS')
        self.assertEqual(s.reboots,['existing0','existing1'])
        self.assertTrue(all(r['vm']=='measure0' for r in s.logs['measure']['0']))
        self.assertEqual(v.read_evidence(self.root,'validation-window.json')['start_anchor'],start)

    def test_pair_a_start_anchor_survives_resume_without_collection(self):
        s=self.scenario(); s.obj.wait_measure(initial=True); start=s.obj.checkpoint_start()
        s.obj.collect=Mock(side_effect=AssertionError('must not reset anchor'))
        self.assertEqual(s.obj.checkpoint_start(),start)

    def test_console_collection_filters_pair_b_packets_out_of_pair_a(self):
        s=self.scenario()
        def output(server,**kw):
            rows=[]
            for role in ('measure','existing'):
                for seq in range(1,12):
                    row=packet(seq,success=role=='measure',boot=role+'-boot'+server[-1])
                    row.update(run='run',vm=role+server[-1]); rows.append(row)
            return {'output':'\n'.join(v.PREFIX+json.dumps(row) for row in rows)}
        s.obj.cloud.compute.get_server_console_output.side_effect=output
        rows=v.Validation.collect(s.obj,'measure')
        self.assertTrue(all(r['vm']=='measure0' for r in rows['0']))
        result=v.probe_metrics(rows['0'],anchor(1,'measure-boot0'),anchor(11,'measure-boot0'),.2)
        self.assertEqual(result['status'],'PASS'); self.assertEqual(result['packets_failed'],0)
        self.assertFalse((self.root/'existing0-console-records.json').exists())

    def test_pair_b_initial_source_mtu_remains_required(self):
        s=self.scenario(); s.obj.cfg['initial']=True
        for net in ('pre-net0','pre-net1'): s.networks[net].mtu=1450
        s.obj.wait('pre')
        s.health['existing']['0']['mtu']=1442
        with self.assertRaises(RuntimeError): s.obj.wait('pre')

    def test_pair_a_reboot_invalidates_metric_even_when_other_guest_recovers(self):
        s=self.scenario(); s.obj.wait_measure(initial=True); s.obj.checkpoint_start()
        s.boots['measure']['1']='unexpected-reboot'; s.seq['measure']['1']=0
        with self.assertRaises(TimeoutError): s.obj.wait_measure()
        self.assertEqual(v.read_evidence(self.root,'tenant-dataplane-probe.json')['status'],'UNAVAILABLE')
        self.assertNotIn('end_anchor',v.read_evidence(self.root,'validation-window.json'))
        s.obj.cloud.compute.reboot_server.assert_not_called()

    def test_automatic_target_mtu_convergence_never_reboots(self):
        s=self.scenario(stale=False,enabled=True)
        s.obj.prepare_dhcp(target=True)
        s.obj.cloud.compute.reboot_server.assert_not_called()
        self.assertEqual(v.read_evidence(self.root,'existing-mtu-automatic.json')['status'],'PASS')
        self.assertFalse(v.read_evidence(self.root,'existing-mtu-remediation.json')['remediation_required'])
        self.assertEqual(set(v.read_evidence(self.root,'existing-migration-baseline.json')),{'0','1'})

    def test_automatic_mtu_success_does_not_fabricate_identity_preservation(self):
        s=self.scenario(stale=False,enabled=True)
        s.ports['existing-port0'].fixed_ips=[]
        with self.assertRaisesRegex(RuntimeError,'resource preservation'): s.obj.prepare_dhcp(target=True)
        self.assertEqual(v.read_evidence(self.root,'existing-mtu-remediation.json')['guests']['0']['fixed_ip_preservation'],'FAIL')
        self.assertFalse((self.root/'existing-migration-baseline.json').exists())
        s.obj.cloud.compute.reboot_server.assert_not_called()

    def test_healthy_dhcp_old_mtu_classifies_reboot_required_but_disabled_fails(self):
        s=self.scenario()
        with self.assertRaisesRegex(RuntimeError,'disabled'): s.obj.prepare_dhcp(target=True)
        journal=v.read_evidence(self.root,'existing-mtu-remediation.json')
        self.assertEqual(journal['automatic_mtu_convergence'],'FAIL')
        self.assertTrue(journal['remediation_required'])
        self.assertEqual(journal['guests']['0']['classification'],'REBOOT_REQUIRED')
        self.assertEqual(v.read_evidence(self.root,'dhcp-precutover-preparation.json')['status'],'FAIL')
        s.obj.cloud.compute.reboot_server.assert_not_called()
        with self.assertRaisesRegex(RuntimeError,'baseline'): s.obj.verify_precutover()

    def test_owned_enabled_reboots_sequentially_and_new_boot_preserves_ids(self):
        s=self.scenario(enabled=True)
        s.obj.prepare_dhcp(target=True)
        self.assertEqual(s.reboots,['existing0','existing1'])
        journal=v.read_evidence(self.root,'existing-mtu-remediation.json')
        for i in ('0','1'):
            row=journal['guests'][i]
            self.assertEqual(row['original_boot'],'existing-boot'+i)
            self.assertEqual(row['post_remediation_boot'],'existing-reboot'+i)
            self.assertEqual((row['guest_mtu_before'],row['guest_mtu_after']),(1450,1442))
            self.assertEqual(row['server_uuid_preservation'],'PASS')
            self.assertEqual(row['port_uuid_preservation'],'PASS')
            self.assertEqual(row['fixed_ip_preservation'],'PASS')
        self.assertEqual(journal['automatic_mtu_convergence'],'FAIL')
        self.assertEqual(v.read_evidence(self.root,'dhcp-precutover-preparation.json')['status'],'PASS')

    def test_reboot_eligibility_requires_every_non_mtu_condition(self):
        cases=[{'dhcp':False},{'metadata':False},{'dhcp_last_ack_monotonic':0},
               {'dhcp_last_renewal_interval_seconds':100},{'dhcp_t1_seconds':31},
               {'configured_static_mtu':1450,'mtu_configuration':'static_mtu'},
               {'dhcp_use_mtu':False,'mtu_configuration':'dhcp_mtu_disabled'},
               {'mtu_configuration':'unknown'}]
        for changes in cases:
            with self.subTest(changes=changes),tempfile.TemporaryDirectory() as d:
                s=Scenario(pathlib.Path(d),enabled=True); s.health['existing']['0'].update(changes)
                with patch.object(v.time,'sleep'),patch.object(v.time,'monotonic',side_effect=s.monotonic):
                    with self.assertRaises((TimeoutError,RuntimeError)): s.obj.prepare_dhcp(target=True)
                s.obj.cloud.compute.reboot_server.assert_not_called()

    def test_wrong_network_mtu_or_packet_failure_cannot_trigger_reboot(self):
        for fault in ('network','connectivity','boot'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as d:
                s=Scenario(pathlib.Path(d),enabled=True)
                if fault=='network': s.networks['pre-net0'].mtu=1450
                if fault=='boot': s.boots['existing']['0']='unexpected'
                collect=s.obj.collect
                def bad_collect(stage,deadline=None):
                    rows=collect(stage,deadline)
                    if fault=='connectivity':
                        for row in rows['0']:
                            if row['kind']=='packet': row['success']=False
                    return rows
                s.obj.collect=bad_collect
                with patch.object(v.time,'sleep'),patch.object(v.time,'monotonic',side_effect=s.monotonic):
                    with self.assertRaises((TimeoutError,RuntimeError)): s.obj.prepare_dhcp(target=True)
                s.obj.cloud.compute.reboot_server.assert_not_called()

    def journal(self,s):
        with self.assertRaises(RuntimeError): s.obj.prepare_dhcp(target=True)
        s.obj.cfg['allow_pre_cutover_guest_reboot']=True
        return v.read_evidence(s.obj.root,'existing-mtu-remediation.json')

    def test_changed_server_port_or_ip_refuses_reboot(self):
        for field in ('server','port','fixed_ips'):
            with self.subTest(field=field),tempfile.TemporaryDirectory() as d:
                s=Scenario(pathlib.Path(d))
                with patch.object(v.time,'sleep'),patch.object(v.time,'monotonic',side_effect=s.monotonic):
                    journal=self.journal(s)
                    if field=='server': s.servers['existing0'].id='other'
                    if field=='port': s.ports['existing-port0'].id='other'
                    if field=='fixed_ips': s.ports['existing-port0'].fixed_ips=[]
                    with self.assertRaisesRegex(RuntimeError,'changed'): s.obj.remediate(journal)
                s.obj.cloud.compute.reboot_server.assert_not_called()

    def test_unowned_pair_b_and_pair_a_alias_refuse_reboot(self):
        for fault in ('unowned','alias'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as d:
                s=Scenario(pathlib.Path(d))
                with patch.object(v.time,'sleep'),patch.object(v.time,'monotonic',side_effect=s.monotonic):
                    journal=self.journal(s)
                    if fault=='unowned': s.obj.state['pre']['existing']['0']['owned']=False
                    else: s.obj.state['pre']['measure']['0']['server']='existing0'
                    with self.assertRaisesRegex(RuntimeError,'owned'): s.obj.remediate(journal)
                s.obj.cloud.compute.reboot_server.assert_not_called()

    def test_db_freeze_checkpoint_prohibits_reboot(self):
        s=self.scenario(); journal=self.journal(s)
        (self.root/'metrics').mkdir(); (self.root/'metrics/phase05.start').write_text('1')
        with self.assertRaisesRegex(RuntimeError,'prohibited'): s.obj.remediate(journal)
        s.obj.cloud.compute.reboot_server.assert_not_called()

    def test_live_ovn_network_prohibits_reboot_even_without_phase_metrics(self):
        s=self.scenario(); journal=self.journal(s)
        s.networks['pre-net0'].provider_network_type='geneve'
        with self.assertRaisesRegex(RuntimeError,'OVN activation'): s.obj.remediate(journal)
        s.obj.cloud.compute.reboot_server.assert_not_called()

    def test_post_reboot_wrong_mtu_fails_without_retrying_reboot(self):
        s=self.scenario(enabled=True)
        def stale_reboot(uuid,reboot_type):
            s.reboot(uuid,reboot_type); s.health['existing'][uuid[-1]]['mtu']=1450
        s.obj.cloud.compute.reboot_server.side_effect=stale_reboot
        with self.assertRaises(TimeoutError): s.obj.prepare_dhcp(target=True)
        s.obj.cloud.compute.reboot_server.assert_called_once()
        failed=v.read_evidence(self.root,'existing-mtu-remediation.json')['guests']['0']
        self.assertEqual(failed['guest_mtu_after'],1450)
        self.assertEqual(failed['post_remediation_boot'],'existing-reboot0')
        with self.assertRaises(TimeoutError): s.obj.prepare_dhcp(target=True)
        s.obj.cloud.compute.reboot_server.assert_called_once()

    def test_crash_after_nova_request_resumes_without_duplicate_reboot(self):
        s=self.scenario(enabled=True)
        def accepted_then_lost(uuid,reboot_type):
            s.reboot(uuid,reboot_type)
            raise OSError('lost Nova reply')
        s.obj.cloud.compute.reboot_server.side_effect=accepted_then_lost
        with self.assertRaises(OSError): s.obj.prepare_dhcp(target=True)
        self.assertTrue(v.read_evidence(self.root,'existing-mtu-remediation.json')['guests']['0']['reboot_requested'])
        s.obj.cloud.compute.reboot_server.side_effect=s.reboot
        s.obj.prepare_dhcp(target=True)
        self.assertEqual(s.reboots,['existing0','existing1'])
        self.assertEqual(s.obj.cloud.compute.reboot_server.call_count,2)

    def test_crash_before_request_delivery_fails_safely_without_resend(self):
        s=self.scenario(enabled=True)
        s.obj.cloud.compute.reboot_server.side_effect=OSError('request uncertain')
        with self.assertRaises(OSError): s.obj.prepare_dhcp(target=True)
        with self.assertRaises(TimeoutError): s.obj.prepare_dhcp(target=True)
        s.obj.cloud.compute.reboot_server.assert_called_once()

    def test_completed_remediation_resume_never_reboots_again(self):
        s=self.scenario(enabled=True); s.obj.prepare_dhcp(target=True)
        baseline=v.read_evidence(self.root,'existing-migration-baseline.json')
        s.obj.prepare_dhcp(target=True)
        self.assertEqual(s.reboots,['existing0','existing1'])
        self.assertEqual(v.read_evidence(self.root,'existing-migration-baseline.json'),baseline)
        self.assertEqual(v.read_evidence(self.root,'existing-mtu-automatic.json')['status'],'FAIL')

    def test_changed_completed_boot_cannot_rebase_or_authorize_next_reboot(self):
        s=self.scenario(); journal=self.journal(s)
        entry=journal['guests']['0']; entry['reboot_requested']=True
        v.save(self.root/'existing-mtu-remediation.json',journal)
        s.reboot('existing0','SOFT')
        s.obj.wait_remediated('0',entry)
        expected=entry['post_remediation_boot']
        s.boots['existing']['0']='unexpected-third-boot'; s.seq['existing']['0']=0
        with self.assertRaisesRegex(RuntimeError,'Completed.*boot changed'): s.obj.remediate(journal)
        self.assertEqual(entry['post_remediation_boot'],expected)
        s.obj.cloud.compute.reboot_server.assert_not_called()

    def test_post_migration_boot_compares_to_post_remediation_baseline(self):
        s=self.scenario(enabled=True); s.obj.prepare_dhcp(target=True)
        for net in ('pre-net0','pre-net1'): s.networks[net].provider_network_type='geneve'
        s.obj.wait('pre')
        self.assertEqual(v.read_evidence(self.root,'pre-workload-checks.json')['0']['boot_continuity'],'PASS')
        s.boots['existing']['0']='third-boot'; s.seq['existing']['0']=0
        with self.assertRaises(RuntimeError): s.obj.wait('pre')
        self.assertEqual(v.read_evidence(self.root,'pre-workload-checks.json')['0']['boot_continuity'],'FAIL')

    def test_pair_c_geneve_dhcp_mtu_metadata_connectivity_validate(self):
        s=self.scenario(); s.obj.wait('post')
        checks=v.read_evidence(self.root,'post-workload-checks.json')
        self.assertTrue(v.workload_pass(checks,'geneve'))
        self.assertEqual(checks['0']['mtu'],'PASS')

    def test_pair_c_failures_propagate(self):
        for failure in ('dhcp','metadata','mtu','bound','connectivity','geneve'):
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as d:
                s=Scenario(pathlib.Path(d))
                if failure in ('dhcp','metadata'): s.health['fresh']['0'][failure]=False
                if failure=='mtu': s.health['fresh']['0']['mtu']=1450
                if failure=='bound': s.ports['fresh-port0'].binding_vif_type='unbound'
                if failure=='geneve': s.networks['post-net0'].provider_network_type='vxlan'
                collect=s.obj.collect
                def bad_collect(stage,deadline=None):
                    rows=collect(stage,deadline)
                    if failure=='connectivity':
                        for row in rows['0']:
                            if row['kind']=='packet': row['success']=False
                    return rows
                s.obj.collect=bad_collect
                with patch.object(v.time,'sleep'),patch.object(v.time,'monotonic',side_effect=s.monotonic):
                    with self.assertRaises(RuntimeError): s.obj.wait('post')

    def test_creation_retry_reuses_all_six_owned_servers_and_ports(self):
        s=self.scenario()
        for net in ('pre-net0','pre-net1'): s.networks[net].provider_network_type='geneve'
        for role in ('measure','pre','post'): s.obj.create(role)
        s.obj.cloud.compute.create_server.assert_not_called()
        s.obj.cloud.network.create_port.assert_not_called()
        s.obj.cloud.compute.reboot_server.assert_not_called()

    def test_new_explicit_ports_and_servers_checkpoint_before_boot_wait(self):
        s=self.scenario()
        for vm in s.obj.state['pre']['existing'].values():
            vm.pop('port'); vm.pop('server')
        count=0
        def create_port(**kw):
            nonlocal count
            key=str(count); count+=1
            fixed=[{'subnet_id':'pre-sub'+key,'ip_address':'10.0.'+key+'.20'}]
            port=NS(id='new-port'+key,name=kw['name'],fixed_ips=fixed,security_group_ids=['pre-sg'],device_id='')
            s.ports[port.id]=port
            return port
        def create_server(**kw):
            key=kw['name'][-1]; port=kw['networks'][0]['port']
            checkpoint=v.read_evidence(self.root,'validation-resources.json')
            self.assertEqual(checkpoint['pre']['existing'][key]['port'],port)
            server=NS(id='new-server'+key,status='ACTIVE',metadata=kw['metadata'])
            s.servers[server.id]=server; s.ports[port].device_id=server.id
            return server
        def wait(server,**kw):
            key=server.id[-1]
            self.assertEqual(v.read_evidence(self.root,'validation-resources.json')['pre']['existing'][key]['server'],server.id)
            return server
        s.obj.cloud.network.create_port.side_effect=create_port
        s.obj.cloud.compute.create_server.side_effect=create_server
        s.obj.cloud.compute.servers.return_value=[]
        s.obj.cloud.compute.wait_for_server.side_effect=wait
        s.obj.create('pre')
        self.assertEqual(s.obj.cloud.network.create_port.call_count,2)
        self.assertEqual(s.obj.cloud.compute.create_server.call_count,2)

    def test_recover_port_after_lost_creation_reply_without_duplicate(self):
        s=self.scenario()
        vm=s.obj.state['pre']['existing']['0']; vm.pop('port'); vm.pop('server')
        existing=NS(id='recovered-port',name=vm['name'],fixed_ips=vm['fixed_ips'],security_group_ids=['pre-sg'],device_id='')
        s.ports[existing.id]=existing
        def ports(**kw):
            return [existing] if kw.get('name')==existing.name else []
        s.obj.cloud.network.ports.side_effect=ports
        s.obj.cloud.compute.servers.return_value=[]
        s.obj.cloud.compute.create_server.return_value=NS(id='existing0')
        s.obj.create('pre')
        self.assertEqual(vm['port'],'recovered-port')
        s.obj.cloud.network.create_port.assert_not_called()

    def test_pair_c_cannot_be_created_on_ovs_before_migration(self):
        s=self.scenario()
        with self.assertRaisesRegex(RuntimeError,'OVN/Geneve'): s.obj.create('post')
        s.obj.cloud.compute.create_server.assert_not_called(); s.obj.cloud.network.create_port.assert_not_called()

    def test_resume_requested_reboot_waits_through_nova_transition(self):
        s=self.scenario(); journal=self.journal(s)
        entry=journal['guests']['0']; entry['reboot_requested']=True
        v.save(self.root/'existing-mtu-remediation.json',journal)
        s.reboot('existing0','SOFT')
        s.servers['existing0'].status='REBOOT'
        def wait(server,**kw):
            server.status='ACTIVE'; return server
        s.obj.cloud.compute.wait_for_server.side_effect=wait
        s.obj.remediate(journal)
        self.assertEqual(s.obj.cloud.compute.reboot_server.call_count,1)  # only existing1

    def test_historical_schema_keeps_uuid_and_console_label_without_pair_a_alias(self):
        cfg={'run':'old'}; v.save(self.root/'validation-config.json',cfg)
        legacy={'pre':{'router':'old-router','security_group':'old-sg',
                      '0':{'server':'old0','port':'oldp0','network':'oldnet0','subnet':'oldsub0','interface':True},
                      '1':{'server':'old1','port':'oldp1','network':'oldnet1','subnet':'oldsub1','interface':True}}}
        v.save(self.root/'validation-resources.json',legacy)
        with patch.dict(sys.modules,{'openstack':NS(connect=lambda:Mock())}):
            obj=v.Validation(self.root)
            again=v.Validation(self.root)
        self.assertEqual(obj.pair('pre')['0']['server'],'old0')
        self.assertEqual(obj.pair('pre')['0']['record_vm'],'pre0')
        self.assertFalse(obj.pair('pre')['0']['owned'])
        self.assertEqual(obj.state,again.state)
        with self.assertRaisesRegex(RuntimeError,'Missing measure'): obj.pair('measure')

    def test_preservation_excludes_only_owned_nested_pair_c_additions(self):
        import yaml
        s=self.scenario()
        tasks=yaml.safe_load((ROOT/'playbooks/11-validate.yml').read_text())[0]['tasks']
        shell=next(t['ansible.builtin.shell'] for t in tasks if t['name']=='Compare critical resource identity sets before and after migration')
        code=shell.split("python3 - <<'PY'\n",1)[1].rsplit('\nPY',1)[0].replace('{{ migration_run_dir }}',str(self.root))
        ids={'networks':['post-net0','post-net1'],'subnets':['post-sub0','post-sub1'],
             'routers':['post-router'],'servers':['fresh0','fresh1'],'compute-ports':['fresh-port0','fresh-port1']}
        for kind,added in ids.items():
            v.save(self.root/(kind+'.before.json'),[{'ID':'original-'+kind}])
            v.save(self.root/(kind+'.after.json'),[{'ID':'original-'+kind}]+[{'ID':i} for i in added])
        subprocess.run([sys.executable,'-c',code],check=True,stdout=subprocess.DEVNULL)
        self.assertTrue(all(r['unchanged'] for r in v.read_evidence(self.root,'resource-consistency.json').values()))
        rows=v.read_evidence(self.root,'networks.after.json'); rows.append({'ID':'unrelated'})
        v.save(self.root/'networks.after.json',rows)
        result=subprocess.run([sys.executable,'-c',code],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.assertEqual(result.returncode,1)
        self.assertEqual(v.read_evidence(self.root,'resource-consistency.json')['networks']['added'],['unrelated'])

    def test_cleanup_handles_six_servers_ports_and_four_shared_networks(self):
        s=self.scenario(); s.obj.cleanup('post'); s.obj.cleanup('pre')
        deleted=[c.args[0] for c in s.obj.cloud.compute.delete_server.call_args_list]
        self.assertEqual(set(deleted),set(s.servers)); self.assertEqual(len(deleted),6)
        self.assertEqual(s.obj.cloud.network.delete_port.call_count,6)
        self.assertEqual(s.obj.cloud.network.delete_network.call_count,4)
        self.assertEqual(s.obj.cloud.network.delete_subnet.call_count,4)
        s.obj.cloud.image.delete_image.assert_not_called(); s.obj.cloud.compute.delete_flavor.assert_not_called()
        s.obj.cleanup('pre'); self.assertEqual(s.obj.cloud.compute.delete_server.call_count,6)

    def test_full_success_can_report_failed_automatic_mtu_and_successful_reboot(self):
        s=self.scenario(enabled=True); s.obj.wait_measure(initial=True); s.obj.checkpoint_start()
        s.obj.prepare_dhcp(target=True); s.obj.wait_measure()
        remediation=v.read_evidence(self.root,'existing-mtu-remediation.json')
        metric=v.read_evidence(self.root,'tenant-dataplane-probe.json')
        # Synthetic authoritative metric fixture for the report boundary; legacy
        # console-only measurement is tested separately, never promoted by code.
        metric['evidence_source']='compute-tap-pcap'
        baseline=v.read_evidence(self.root,'existing-migration-baseline.json')
        ready_evidence(self.root)
        v.save(self.root/'existing-mtu-automatic.json',{'status':'FAIL'})
        v.save(self.root/'existing-mtu-remediation.json',remediation)
        v.save(self.root/'tenant-dataplane-probe.json',metric)
        v.save(self.root/'existing-migration-baseline.json',baseline)
        for name in ('pre-cleanup.json','post-cleanup.json'): v.save(self.root/name,{'status':'PASS'})
        (self.root/'metrics').mkdir()
        subprocess.run([sys.executable,str(ROOT/'scripts/migration_report.py'),str(self.root),'run','inventory'],check=True,stdout=subprocess.DEVNULL)
        report=v.read_evidence(self.root,'migration-report.json'); text=(self.root/'migration-report.txt').read_text()
        self.assertEqual(report['result'],'SUCCESS')
        self.assertEqual(report['automatic_mtu_convergence'],'FAIL')
        self.assertTrue(report['remediation_required']); self.assertEqual(report['remediation_action'],'soft reboot')
        self.assertEqual(report['dataplane_continuity']['measurement_workload'],'Pair A')
        self.assertIn('Automatic guest MTU convergence: FAIL',text)
        self.assertIn('Pre-cutover MTU readiness: PASS',text)

    def test_failure_preserves_all_owned_resources(self):
        s=self.scenario(); self.assertFalse(s.obj.finalize())
        s.obj.cloud.compute.delete_server.assert_not_called(); s.obj.cloud.network.delete_port.assert_not_called()

    def test_freeze_guard_precedes_downtime_and_neutron_stop(self):
        import yaml
        plays=yaml.safe_load((ROOT/'playbooks/07-migrate-db.yml').read_text())
        first=plays[0]['tasks'][1]
        self.assertIn('precutover-ready',first['ansible.builtin.shell'])
        self.assertNotIn('failed_when',first)
        self.assertTrue(plays[0]['any_errors_fatal'])
        tasks=[t for play in plays for t in play['tasks']]
        freeze=next(i for i,t in enumerate(tasks) if '--freeze-start' in t.get('ansible.builtin.command',{}).get('argv',[]))
        self.assertGreater(freeze, tasks.index(first))
        self.assertEqual(tasks[freeze-1]['ansible.builtin.command']['argv'][2],'ready')
        self.assertEqual(plays[1]['hosts'],'control')


if __name__=='__main__': unittest.main()
