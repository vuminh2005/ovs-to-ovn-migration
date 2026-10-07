"""Exact OVN state/owned UUID post-cutover reboot safety and crash recovery."""
import copy
import pathlib
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch
from test_validation import v, ready_evidence, ROOT
from test_three_pairs import Scenario


class PostScenario(Scenario):
    def __init__(self, root, stale=False, enabled=False):
        super().__init__(root,stale=False)
        self.obj.cfg.update(post_cutover_timeout=1,post_cutover_dhcp_enabled=True,
                            allow_post_cutover_guest_reboot=enabled)
        for net in self.networks.values(): net.provider_network_type='geneve'
        for role in self.health:
            for key,health in self.health[role].items():
                health.update(mtu=1442,dhcp_observer_running=True,metadata_gateway='10.0.'+key+'.3')
        self.obj.cloud.network.ports.side_effect=lambda **kw:[NS(device_owner='network:distributed',
            fixed_ips=[{'subnet_id':'pre-sub'+kw['network_id'][-1],'ip_address':'10.0.'+kw['network_id'][-1]+'.3'}])]
        self.obj.cloud.network.get_subnet.side_effect=lambda uuid:NS(cidr='10.0.'+uuid[-1]+'.0/24',
            gateway_ip='10.0.'+uuid[-1]+'.1',is_dhcp_enabled=True)
        self.raw={key:self.raw_evidence(key) for key in ('0','1')}
        self.obj.ovn_evidence=lambda key:copy.deepcopy(self.raw[key])
        self.obj.cloud.compute.reboot_server.side_effect=self.post_reboot
        v.save(root/'existing-migration-baseline.json',v.read_evidence(root,'existing-initial-baseline.json'))
        v.save(root/'validation-config.json',self.obj.cfg)
        if stale:
            self.health['existing']['0'].update(metadata_gateway='10.0.0.2',metadata=False,dhcp_last_ack_monotonic=1)

    def raw_evidence(self, key):
        port='existing-port'+key; cidr='10.0.'+key+'.0/24'; metadata='10.0.'+key+'.3'
        return dict(port=port,bindings=[dict(logical_port=port,chassis='chassis',up=True)],
            lsps=[dict(name=port,dhcpv4_options=['dhcp'+key])],
            dhcp_options=[dict(_uuid='dhcp'+key,cidr=cidr,external_ids={'subnet_id':'pre-sub'+key,'port_id':port},
                options={'T1':'30','T2':'60','mtu':'1442','lease_time':'43200','router':'10.0.'+key+'.1',
                         'classless_static_route':'{169.254.169.254/32,'+metadata+',0.0.0.0/0,10.0.'+key+'.1}'})])

    def post_reboot(self, uuid, reboot_type):
        self.obj.post_owner(uuid[-1],v.read_evidence(self.obj.root,'existing-post-cutover-remediation.json')['guests'][uuid[-1]])
        assert uuid.startswith('existing') and reboot_type=='SOFT'
        key=uuid[-1]
        assert v.read_evidence(self.obj.root,'existing-post-cutover-remediation.json')['guests'][key]['reboot_requested'] is True
        self.reboots.append(uuid); self.boots['existing'][key]='post-reboot'+key; self.seq['existing'][key]=0
        self.health['existing'][key].update(metadata=True,metadata_gateway='10.0.'+key+'.3')
        self.health['existing'][key].pop('dhcp_last_ack_monotonic',None)


class PostCutoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root=pathlib.Path(self.tmp.name)

    def scenario(self, **kwargs):
        s=PostScenario(self.root,**kwargs); self.addCleanup(patch.stopall)
        patch.object(v.time,'sleep').start(); patch.object(v.time,'monotonic',side_effect=s.monotonic).start()
        return s

    def test_automatic_fresh_ovn_renewal_needs_no_reboot(self):
        s=self.scenario(); s.obj.post_cutover_dhcp()
        self.assertEqual(v.read_evidence(self.root,'existing-post-cutover-automatic.json')['status'],'PASS')
        self.assertEqual(v.read_evidence(self.root,'existing-post-cutover-readiness.json')['status'],'PASS')
        s.obj.cloud.compute.reboot_server.assert_not_called()

    def test_stale_route_no_renewal_and_healthy_ovn_classified_reboot_required(self):
        s=self.scenario(stale=True)
        with self.assertRaisesRegex(RuntimeError,'disabled'): s.obj.post_cutover_dhcp()
        journal=v.read_evidence(self.root,'existing-post-cutover-remediation.json')
        self.assertEqual(journal['guests']['0']['classification'],'POST_CUTOVER_REBOOT_REQUIRED')
        self.assertEqual(journal['guests']['1']['classification'],'PASS')
        s.obj.cloud.compute.reboot_server.assert_not_called()

    def test_explicit_opt_in_owned_uuid_one_soft_reboot_then_full_readiness(self):
        s=self.scenario(stale=True,enabled=True); s.obj.post_cutover_dhcp()
        self.assertEqual(s.reboots,['existing0']); s.obj.cloud.compute.reboot_server.assert_called_once_with('existing0',reboot_type='SOFT')
        self.assertEqual(v.read_evidence(self.root,'existing-post-cutover-automatic.json')['status'],'FAIL')
        journal=v.read_evidence(self.root,'existing-post-cutover-remediation.json')
        self.assertEqual(journal['status'],'PASS'); self.assertTrue(journal['guests']['0']['reboot_completed'])
        self.assertEqual(journal['guests']['0']['post_remediation_boot'],'post-reboot0')
        self.assertEqual(v.read_evidence(self.root,'existing-migration-baseline.json')['0']['boot'],'existing-boot0')
        self.assertEqual(v.read_evidence(self.root,'existing-post-cutover-baseline.json')['0']['boot'],'post-reboot0')
        self.assertEqual(v.read_evidence(self.root,'existing-post-cutover-readiness.json')['metadata_ready'],'PASS')

    def test_all_ovn_defects_fail_safely_without_reboot(self):
        mutations=[lambda r:r['bindings'][0].update(up=False),lambda r:r['bindings'][0].update(chassis=[]),
                   lambda r:r['lsps'][0].update(dhcpv4_options=[]),lambda r:r['dhcp_options'].append(copy.deepcopy(r['dhcp_options'][0])),
                   lambda r:r['dhcp_options'][0]['options'].update(mtu='1450'),
                   lambda r:r['dhcp_options'][0]['options'].update(T1='31'),
                   lambda r:r['dhcp_options'][0]['options'].update(classless_static_route='{169.254.169.254/32,10.0.0.2}'),
                   lambda r:r['dhcp_options'][0]['external_ids'].update(subnet_id='other')]
        for mutate in mutations:
            with self.subTest(mutate=mutate),tempfile.TemporaryDirectory() as d:
                s=PostScenario(pathlib.Path(d),stale=True,enabled=True); mutate(s.raw['0'])
                with patch.object(v.time,'sleep'),patch.object(v.time,'monotonic',side_effect=s.monotonic):
                    with self.assertRaises(TimeoutError): s.obj.post_cutover_dhcp()
                s.obj.cloud.compute.reboot_server.assert_not_called()
                self.assertEqual(v.read_evidence(s.obj.root,'existing-post-cutover-remediation.json')['guests']['0']['classification'],'FAIL')

    def test_missing_duplicate_or_wrong_subnet_metadata_port_never_reboot(self):
        for fault in ('missing','duplicate','subnet'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as d:
                s=PostScenario(pathlib.Path(d),stale=True,enabled=True)
                def ports(**kw):
                    ip=NS(device_owner='network:distributed',fixed_ips=[{'subnet_id':'other' if fault=='subnet' else 'pre-sub'+kw['network_id'][-1],
                        'ip_address':'10.0.'+kw['network_id'][-1]+'.3'}])
                    return [] if fault=='missing' else [ip,ip] if fault=='duplicate' else [ip]
                s.obj.cloud.network.ports.side_effect=ports
                with patch.object(v.time,'sleep'),patch.object(v.time,'monotonic',side_effect=s.monotonic):
                    with self.assertRaises(TimeoutError): s.obj.post_cutover_dhcp()
                s.obj.cloud.compute.reboot_server.assert_not_called()

    def test_server_port_fixed_ip_or_boot_change_never_reboot(self):
        for fault in ('server','port','fixed_ips','boot'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as d:
                s=PostScenario(pathlib.Path(d),stale=True,enabled=True)
                if fault=='server': s.servers['existing0'].id='changed'
                if fault=='port': s.ports['existing-port0'].id='changed'
                if fault=='fixed_ips': s.ports['existing-port0'].fixed_ips=[]
                if fault=='boot': s.boots['existing']['0']='changed'
                with patch.object(v.time,'sleep'),patch.object(v.time,'monotonic',side_effect=s.monotonic):
                    with self.assertRaises((RuntimeError,TimeoutError)): s.obj.post_cutover_dhcp()
                s.obj.cloud.compute.reboot_server.assert_not_called()

    def test_unhealthy_guest_or_fresh_renewal_with_stale_route_never_reboot(self):
        for change in ({'mtu':1450},{'dhcp':False},{'dhcp_observer_running':False},{'dhcp_ack_count':0},
                       {'dhcp_last_ack_monotonic':999}):
            with self.subTest(change=change),tempfile.TemporaryDirectory() as d:
                s=PostScenario(pathlib.Path(d),stale=True,enabled=True); s.health['existing']['0'].update(change)
                with patch.object(v.time,'sleep'),patch.object(v.time,'monotonic',side_effect=s.monotonic):
                    with self.assertRaises(TimeoutError): s.obj.post_cutover_dhcp()
                s.obj.cloud.compute.reboot_server.assert_not_called()

    def test_metadata_still_fails_after_reboot_preserves_request_and_resources(self):
        s=self.scenario(stale=True,enabled=True)
        def bad_reboot(uuid,reboot_type):
            s.post_reboot(uuid,reboot_type); s.health['existing'][uuid[-1]]['metadata']=False
        s.obj.cloud.compute.reboot_server.side_effect=bad_reboot
        with self.assertRaises(TimeoutError): s.obj.post_cutover_dhcp()
        self.assertTrue(v.read_evidence(self.root,'existing-post-cutover-remediation.json')['guests']['0']['reboot_requested'])
        with self.assertRaises(TimeoutError): s.obj.post_cutover_dhcp()
        s.obj.cloud.compute.reboot_server.assert_called_once(); s.obj.cloud.compute.delete_server.assert_not_called()

    def test_lost_nova_reply_resume_proves_completion_without_duplicate(self):
        s=self.scenario(stale=True,enabled=True)
        def lost_reply(uuid,reboot_type): s.post_reboot(uuid,reboot_type); raise OSError('lost reply')
        s.obj.cloud.compute.reboot_server.side_effect=lost_reply
        with self.assertRaises(OSError): s.obj.post_cutover_dhcp()
        s.obj.cloud.compute.reboot_server.side_effect=s.post_reboot
        s.obj.post_cutover_dhcp(); self.assertEqual(s.reboots,['existing0'])
        s.obj.post_cutover_dhcp(); self.assertEqual(s.reboots,['existing0'])

    def test_ambiguous_intent_without_new_boot_never_resends(self):
        s=self.scenario(stale=True,enabled=True); s.obj.cloud.compute.reboot_server.side_effect=OSError('not delivered?')
        with self.assertRaises(OSError): s.obj.post_cutover_dhcp()
        with self.assertRaises(TimeoutError): s.obj.post_cutover_dhcp()
        s.obj.cloud.compute.reboot_server.assert_called_once()

    def test_completed_post_reboot_unexpected_third_boot_fails_without_rebase(self):
        s=self.scenario(stale=True,enabled=True); s.obj.post_cutover_dhcp()
        before=v.read_evidence(self.root,'existing-post-cutover-baseline.json')
        s.boots['existing']['0']='unexpected-third'; s.seq['existing']['0']=0
        with self.assertRaisesRegex(RuntimeError,'boot changed'): s.obj.post_cutover_dhcp()
        self.assertEqual(v.read_evidence(self.root,'existing-post-cutover-baseline.json'),before)
        self.assertEqual(s.reboots,['existing0'])

    def test_pair_a_pair_c_alias_and_unowned_uuid_are_protected(self):
        for fault in ('measure','fresh','unowned'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as d:
                s=PostScenario(pathlib.Path(d),stale=True,enabled=True)
                if fault=='unowned': s.obj.state['pre']['existing']['0']['owned']=False
                elif fault=='measure': s.obj.state['pre']['measure']['0']['server']='existing0'
                else: s.obj.state['post']['fresh']['0']['port']='existing-port0'
                with patch.object(v.time,'sleep'),patch.object(v.time,'monotonic',side_effect=s.monotonic):
                    with self.assertRaisesRegex(RuntimeError,'Pair-B'): s.obj.post_cutover_dhcp()
                s.obj.cloud.compute.reboot_server.assert_not_called()

    def test_correct_metadata_ip_can_end_in_two_or_three(self):
        s=self.scenario()
        vm=s.obj.pair('pre')['0']; subnet=s.obj.cloud.network.get_subnet(vm['subnet'])
        for ip in ('10.0.0.2','10.0.0.3'):
            raw=s.raw_evidence('0'); raw['dhcp_options'][0]['options']['classless_static_route']='{169.254.169.254/32,'+ip+'}'
            self.assertEqual(v.ovn_dhcp_health(raw,vm,subnet,ip,s.obj.cfg)['status'],'PASS')

    def test_post_freshness_anchor_is_immutable_across_retry(self):
        s=self.scenario(stale=True)
        with self.assertRaises(RuntimeError): s.obj.post_cutover_dhcp()
        fence=v.read_evidence(self.root,'existing-post-cutover-anchor.json')
        with self.assertRaises(RuntimeError): s.obj.post_cutover_dhcp()
        self.assertEqual(v.read_evidence(self.root,'existing-post-cutover-anchor.json'),fence)

    def test_pair_a_metric_is_unchanged_by_post_remediation(self):
        s=self.scenario(stale=True,enabled=True); metric=dict(status='PASS',evidence_source='compute-tap-pcap',packet_loss_percent=.1)
        v.save(self.root/'tenant-dataplane-probe.json',metric); s.obj.post_cutover_dhcp()
        self.assertEqual(v.read_evidence(self.root,'tenant-dataplane-probe.json'),metric)

    def test_report_seamless_vs_post_reboot_and_automatic_failure_preserved(self):
        for reboot in (False,True):
            with self.subTest(reboot=reboot),tempfile.TemporaryDirectory() as d:
                root=pathlib.Path(d); s=PostScenario(root,stale=reboot,enabled=reboot)
                with patch.object(v.time,'sleep'),patch.object(v.time,'monotonic',side_effect=s.monotonic): s.obj.post_cutover_dhcp()
                automatic=v.read_evidence(root,'existing-post-cutover-automatic.json'); remediation=v.read_evidence(root,'existing-post-cutover-remediation.json')
                readiness=v.read_evidence(root,'existing-post-cutover-readiness.json')
                ready_evidence(root)
                for name,value in [('existing-post-cutover-automatic.json',automatic),('existing-post-cutover-remediation.json',remediation),('existing-post-cutover-readiness.json',readiness),('pre-cleanup.json',{'status':'PASS'}),('post-cleanup.json',{'status':'PASS'})]: v.save(root/name,value)
                (root/'metrics').mkdir()
                subprocess.run([sys.executable,str(ROOT/'scripts/migration_report.py'),str(root),'run','inventory'],check=True,stdout=subprocess.DEVNULL)
                report=v.read_evidence(root,'migration-report.json')
                self.assertEqual(report['result'],'SUCCESS_WITH_REMEDIATION' if reboot else 'SUCCESS')
                self.assertEqual(report['automatic_post_cutover_dhcp_convergence'],'FAIL' if reboot else 'PASS')
                self.assertEqual(report['existing_workload_migration']['status'],'PASS AFTER REMEDIATION' if reboot else 'PASS')

    def test_correct_metadata_route_with_generic_metadata_failure_cannot_authorize_reboot(self):
        s=self.scenario(stale=True,enabled=True)
        s.health['existing']['0']['metadata_gateway']='10.0.0.3'
        with self.assertRaises(TimeoutError): s.obj.post_cutover_dhcp()
        s.obj.cloud.compute.reboot_server.assert_not_called()

    def test_pre_cutover_opt_in_does_not_authorize_post_cutover_reboot(self):
        s=self.scenario(stale=True)
        s.obj.cfg['allow_pre_cutover_guest_reboot']=True
        with self.assertRaisesRegex(RuntimeError,'disabled'): s.obj.post_cutover_dhcp()
        s.obj.cloud.compute.reboot_server.assert_not_called()

    def test_reboot_without_genuine_ovn_renewal_cannot_pass(self):
        s=self.scenario(stale=True,enabled=True)
        def no_renewal(uuid,reboot_type):
            s.post_reboot(uuid,reboot_type)
            s.health['existing'][uuid[-1]].update(dhcp_ack_count=0,dhcp_last_ack_monotonic=None)
        s.obj.cloud.compute.reboot_server.side_effect=no_renewal
        with self.assertRaises(TimeoutError): s.obj.post_cutover_dhcp()
        self.assertEqual(s.reboots,['existing0'])
        self.assertEqual(v.read_evidence(self.root,'existing-post-cutover-readiness.json')['status'],'FAIL')

    def test_success_report_cannot_promote_guest_console_packet_metrics(self):
        ready_evidence(self.root); (self.root/'metrics').mkdir()
        v.save(self.root/'tenant-dataplane-probe.json',dict(status='PASS',measurement_workload='Pair A',
            evidence_source='guest-console-secondary',packet_loss_percent=1,actual_dataplane_outage_seconds=2))
        subprocess.run([sys.executable,str(ROOT/'scripts/migration_report.py'),str(self.root),'run','inventory'],
                       check=True,stdout=subprocess.DEVNULL)
        report=v.read_evidence(self.root,'migration-report.json')
        self.assertEqual(report['dataplane_probe']['status'],'NOT TESTED')
        self.assertEqual(report['result'],'MIGRATED_VALIDATION_INCOMPLETE')
        text=(self.root/'migration-report.txt').read_text()
        self.assertIn('Packet loss: UNAVAILABLE',text)
        self.assertIn('Actual dataplane outage: UNAVAILABLE',text)


if __name__=='__main__': unittest.main()
