import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from test_validation import v, packet, anchor


class DhcpTests(unittest.TestCase):
    def test_metadata_failure_does_not_block_packet_end(self):
        with tempfile.TemporaryDirectory() as d:
            obj=v.Validation.__new__(v.Validation); obj.root=pathlib.Path(d); obj.cfg={'interval':.2}
            v.save(obj.root/'validation-window.json',{'start_anchor':anchor(1)})
            rows={key:[packet(i) for i in range(2,12)] + [dict(kind='health',seq=11,boot='one',metadata=False,dhcp=True)] for key in ('0','1')}
            self.assertTrue(obj.checkpoint_recovery(rows,{'0':anchor(1),'1':anchor(1)}))
            window=v.read_evidence(obj.root,'validation-window.json')
            self.assertEqual(window['end_anchor'],anchor(11))
            self.assertEqual(v.probe_metrics(rows['0'],window['start_anchor'],window['end_anchor'],.2)['status'],'PASS')

    def test_reboot_after_packet_window_does_not_invalidate_it(self):
        rows=[packet(i) for i in range(1,12)]+[packet(1,boot='later')]
        self.assertEqual(v.probe_metrics(rows,anchor(1),anchor(11),.2)['status'],'PASS')

    def test_no_recovery_has_no_end(self):
        with tempfile.TemporaryDirectory() as d:
            obj=v.Validation.__new__(v.Validation); obj.root=pathlib.Path(d); obj.cfg={'interval':.2}
            v.save(obj.root/'validation-window.json',{'start_anchor':anchor(1)})
            rows={key:[packet(i,False) for i in range(2,12)] for key in ('0','1')}
            self.assertFalse(obj.checkpoint_recovery(rows,{'0':anchor(1),'1':anchor(1)}))
            self.assertNotIn('end_anchor',v.read_evidence(obj.root,'validation-window.json'))
            self.assertIsNone(v.probe_metrics(rows['0'],anchor(1),None,.2)['actual_dataplane_outage_seconds'])

    def test_availability_independent_of_wrong_route_and_mtu(self):
        for mtu,gateway in ((1450,'10.231.0.3'),(1442,'10.231.0.2')):
            health=dict(kind='health',seq=11,boot='one',dhcp=True,metadata=False,mtu=mtu,metadata_gateway=gateway)
            rows=[packet(i) for i in range(2,12)]+[health]
            self.assertEqual(v.guest_checks(rows,anchor(1),.2)['dhcp'],'PASS')
            self.assertEqual(v.dhcp_convergence(health,1442,'10.231.0.3'),'FAIL')

    def test_derive_actual_metadata_port_for_both_allocations(self):
        for ip in ('10.231.0.2','10.231.0.3'):
            ports=[SimpleNamespace(device_owner='network:distributed',fixed_ips=[{'subnet_id':'subnet','ip_address':ip}, {'subnet_id':'other','ip_address':'10.3.0.2'}]),
                   SimpleNamespace(device_owner='network:dhcp',fixed_ips=[{'subnet_id':'subnet','ip_address':'10.231.0.9'}])]
            expected=v.metadata_port_ip(ports,'subnet')
            self.assertEqual(expected,ip)
            self.assertEqual(v.dhcp_convergence({'dhcp':True,'mtu':1442,'metadata_gateway':ip},1442,expected),'PASS')
            self.assertIsNone(v.metadata_port_ip(ports,'missing'))

    def test_preparation_timeout_persists_failure_before_db_freeze(self):
        with tempfile.TemporaryDirectory() as d:
            obj=v.Validation.__new__(v.Validation); obj.root=pathlib.Path(d)
            obj.cfg=dict(interval=.2,dhcp_timeout=1)
            obj.collect=Mock(side_effect=[{key:[packet(1)] for key in ('0','1')},
                                         {key:[packet(i) for i in range(2,12)] for key in ('0','1')}])
            with patch.object(v.time,'monotonic',side_effect=[0,2]),patch.object(v.time,'sleep'):
                with self.assertRaisesRegex(TimeoutError,'Short-T1 renewal preparation'): obj.prepare_dhcp()
            self.assertEqual(v.read_evidence(obj.root,'dhcp-initial-preparation.json')['status'],'FAIL')
            import yaml
            repo=pathlib.Path(__file__).parents[1]
            imports=[p['import_playbook'] for p in yaml.safe_load((repo/'migrate-to-ovn.yml').read_text())]
            self.assertLess(imports.index('playbooks/06-target-config.yml'),imports.index('playbooks/07-migrate-db.yml'))
            stage=(repo/'playbooks/06-target-config.yml').read_text()
            self.assertIn('prepare-dhcp',stage)
            self.assertNotIn('failed_when: false',stage)

    def preparation(self, root, health, target=False, baseline_ack=0):
        obj=v.Validation.__new__(v.Validation); obj.root=root
        obj.cfg=dict(interval=.2,dhcp_timeout=1,dhcp_t1=30,dhcp_t2=60,target_mtu=1442)
        obj.state={'pre':{key:{'network':key} for key in ('0','1')}}
        obj.cloud=SimpleNamespace(network=Mock())
        obj.cloud.network.get_network.return_value=SimpleNamespace(mtu=1442 if target else 1450)
        baseline={key:[packet(1),dict(kind='health',seq=1,boot='one',mono=.2,
                                    dhcp_ack_count=baseline_ack)] for key in ('0','1')}
        current={key:baseline[key]+[packet(i) for i in range(2,12)]+[dict(
            kind='health',seq=11,boot='one',mono=2.2,**health)] for key in ('0','1')}
        obj.collect=Mock(side_effect=[baseline,current])
        v.save(root/'initial-freshness-anchors.json',{'0':anchor(1),'1':anchor(1)})
        return obj

    def healthy(self, **changes):
        health=dict(dhcp=True,metadata=True,metadata_gateway='10.231.0.2',mtu=1450,
                    dhcp_t1_seconds=30,dhcp_t2_seconds=60,dhcp_ack_count=2,
                    dhcp_last_ack_monotonic=1.8)
        health.update(changes)
        return health

    def test_initial_ovs_mtu_and_metadata_pass_without_target_mtu(self):
        with tempfile.TemporaryDirectory() as d:
            obj=self.preparation(pathlib.Path(d),self.healthy())
            obj.prepare_dhcp()
            evidence=v.read_evidence(obj.root,'dhcp-initial-preparation.json')
            self.assertEqual(evidence['status'],'PASS')
            self.assertEqual(evidence['guests']['0']['health']['mtu'],1450)
            # Initial gate must not query OVN ports or target network MTUs.
            obj.cloud.network.get_network.assert_not_called()
            obj.cloud.network.ports.assert_not_called()

    def test_initial_missing_matching_renewals_fails(self):
        with tempfile.TemporaryDirectory() as d:
            obj=self.preparation(pathlib.Path(d),self.healthy(dhcp_ack_count=0,dhcp_last_ack_monotonic=None))
            with patch.object(v.time,'monotonic',side_effect=[0,2]):
                with self.assertRaisesRegex(TimeoutError,'Short-T1 renewal preparation'):
                    obj.prepare_dhcp()

    def test_initial_renewal_must_be_newer_than_preparation_anchor(self):
        with tempfile.TemporaryDirectory() as d:
            obj=self.preparation(pathlib.Path(d),self.healthy(dhcp_last_ack_monotonic=.1),baseline_ack=2)
            with patch.object(v.time,'monotonic',side_effect=[0,2]):
                with self.assertRaises(TimeoutError): obj.prepare_dhcp()
            self.assertFalse(v.read_evidence(obj.root,'dhcp-initial-preparation.json')['guests']['0']['fresh_renewal'])

    def test_initial_requires_working_ovs_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            obj=self.preparation(pathlib.Path(d),self.healthy(metadata=False))
            with patch.object(v.time,'monotonic',side_effect=[0,2]):
                with self.assertRaises(TimeoutError): obj.prepare_dhcp()

    def test_renewal_and_target_mtu_preparation_pass(self):
        with tempfile.TemporaryDirectory() as d:
            obj=self.preparation(pathlib.Path(d),self.healthy(mtu=1442),target=True)
            obj.prepare_dhcp(target=True)
            self.assertEqual(v.read_evidence(obj.root,'dhcp-precutover-preparation.json')['status'],'PASS')
            obj.cloud.network.ports.assert_not_called()

    def test_precutover_network_mtu_changed_but_guest_mtu_stale_fails(self):
        with tempfile.TemporaryDirectory() as d:
            obj=self.preparation(pathlib.Path(d),self.healthy(),target=True)
            with patch.object(v.time,'monotonic',side_effect=[0,2]):
                with self.assertRaisesRegex(TimeoutError,'Guest MTU convergence before cutover'):
                    obj.prepare_dhcp(target=True)
            self.assertEqual(v.read_evidence(obj.root,'dhcp-precutover-preparation.json')['status'],'FAIL')

    def test_precutover_old_renewals_cannot_pass_with_new_health_and_mtu(self):
        with tempfile.TemporaryDirectory() as d:
            obj=self.preparation(pathlib.Path(d),self.healthy(mtu=1442,dhcp_last_ack_monotonic=.1),target=True,baseline_ack=2)
            with patch.object(v.time,'monotonic',side_effect=[0,2]):
                with self.assertRaises(TimeoutError): obj.prepare_dhcp(target=True)

    def test_precutover_old_health_record_cannot_pass(self):
        with tempfile.TemporaryDirectory() as d:
            obj=self.preparation(pathlib.Path(d),self.healthy(mtu=1442),target=True)
            baseline={key:[packet(1),dict(kind='health',seq=1,boot='one',mono=.2)] for key in ('0','1')}
            for rows in baseline.values():
                rows[-1].update(self.healthy(mtu=1442),dhcp_last_ack_monotonic=.1)
            obj.collect=Mock(side_effect=[baseline,{key:rows+[packet(i) for i in range(2,12)] for key,rows in baseline.items()}])
            with patch.object(v.time,'monotonic',side_effect=[0,2]):
                with self.assertRaises(TimeoutError): obj.prepare_dhcp(target=True)

    def test_precutover_boot_change_fails(self):
        with tempfile.TemporaryDirectory() as d:
            obj=self.preparation(pathlib.Path(d),self.healthy(mtu=1442),target=True)
            v.save(obj.root/'initial-freshness-anchors.json',{'0':anchor(1,'previous'),'1':anchor(1,'previous')})
            with patch.object(v.time,'monotonic',side_effect=[0,2]):
                with self.assertRaises(TimeoutError): obj.prepare_dhcp(target=True)

    def test_post_migration_checks_real_metadata_port_and_target_mtu(self):
        for actual_ip in ('10.231.0.2','10.231.0.3'):
            with tempfile.TemporaryDirectory() as d:
                obj=self.preparation(pathlib.Path(d),self.healthy(),target=True)
                for key in ('0','1'):
                    obj.state['pre'][key].update(server='vm'+key,port='p'+key,subnet='s'+key,fixed_ips=[{'subnet_id':'s'+key,'ip_address':'ip'+key}])
                obj.cloud.compute=Mock(); obj.cloud.compute.get_server.return_value=SimpleNamespace(status='ACTIVE')
                obj.cloud.network.get_port.side_effect=lambda port: SimpleNamespace(device_id='vm'+port[1],fixed_ips=obj.state['pre'][port[1]]['fixed_ips'],status='ACTIVE',binding_host_id='host',binding_vif_type='ovs')
                obj.cloud.network.get_network.return_value=SimpleNamespace(mtu=1442,provider_network_type='geneve')
                obj.cloud.network.ports.side_effect=lambda **kw:[SimpleNamespace(device_owner='network:distributed',fixed_ips=[{'subnet_id':'s'+kw['network_id'],'ip_address':actual_ip}])]
                rows={key:[packet(i) for i in range(2,12)]+[dict(kind='health',seq=11,boot='one',**self.healthy(mtu=1442,metadata_gateway=actual_ip))] for key in ('0','1')}
                checks=obj.check('pre',{'0':anchor(1),'1':anchor(1)},rows)
                self.assertEqual(checks['0']['dhcp_convergence'],'PASS')
                self.assertEqual(checks['0']['dhcp_expected']['metadata_ip'],actual_ip)
                obj.cloud.network.ports.assert_any_call(network_id='0',device_owner='network:distributed')
                stale=obj.check('pre',{'0':anchor(11),'1':anchor(11)},rows)
                self.assertEqual(stale['0']['dhcp_convergence'],'UNAVAILABLE')
                obj.cloud.network.get_network.return_value=SimpleNamespace(mtu=1450,provider_network_type='geneve')
                wrong_network=obj.check('pre',{'0':anchor(1),'1':anchor(1)},rows)
                self.assertEqual(wrong_network['0']['dhcp_convergence'],'FAIL')
