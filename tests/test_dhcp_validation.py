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
            obj.anchors=Mock(return_value={'0':anchor(1),'1':anchor(1)})
            obj.collect=Mock(return_value={key:[packet(i) for i in range(2,12)] for key in ('0','1')})
            with patch.object(v.time,'monotonic',side_effect=[0,2]),patch.object(v.time,'sleep'):
                with self.assertRaisesRegex(TimeoutError,'DHCP preparation'): obj.prepare_dhcp()
            self.assertEqual(v.read_evidence(obj.root,'dhcp-initial-preparation.json')['status'],'FAIL')
            import yaml
            repo=pathlib.Path(__file__).parents[1]
            imports=[p['import_playbook'] for p in yaml.safe_load((repo/'migrate-to-ovn.yml').read_text())]
            self.assertLess(imports.index('playbooks/06-target-config.yml'),imports.index('playbooks/07-migrate-db.yml'))
            stage=(repo/'playbooks/06-target-config.yml').read_text()
            self.assertIn('prepare-dhcp',stage)
            self.assertNotIn('failed_when: false',stage)

    def test_renewal_and_target_mtu_preparation_pass(self):
        with tempfile.TemporaryDirectory() as d:
            obj=v.Validation.__new__(v.Validation); obj.root=pathlib.Path(d)
            obj.cfg=dict(interval=.2,dhcp_timeout=1,dhcp_t1=30,dhcp_t2=60,target_mtu=1442)
            obj.state={'pre':{key:{'network':key} for key in ('0','1')}}
            obj.cloud=SimpleNamespace(network=Mock()); obj.cloud.network.get_network.return_value=SimpleNamespace(mtu=1442)
            obj.anchors=Mock(return_value={'0':anchor(1),'1':anchor(1)})
            obj.collect=Mock(return_value={key:[packet(i) for i in range(2,12)]+[dict(kind='health',seq=11,boot='one',dhcp=True,mtu=1442,dhcp_t1_seconds=30,dhcp_t2_seconds=60,dhcp_ack_count=2)] for key in ('0','1')})
            obj.prepare_dhcp(target=True)
            self.assertEqual(v.read_evidence(obj.root,'dhcp-precutover-preparation.json')['status'],'PASS')
