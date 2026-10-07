import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
spec = importlib.util.spec_from_file_location('validation', ROOT/'scripts/workload_validation.py')
v = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v)


def anchor(seq, boot='one'):
    return {'boot': boot, 'seq': seq}


def packet(seq, success=True, boot='one', epoch=100):
    return dict(kind='packet', seq=seq, ts=epoch+seq*.2, mono=seq*.2,
                success=success, boot=boot)


def ready_evidence(root):
    for name, nettype in [('initial-workload-checks.json','vxlan'),
                          ('pre-workload-checks.json','geneve'),
                          ('post-workload-checks.json','geneve')]:
        row={k:'PASS' for k in ('identity','active','bound','dhcp','metadata','connectivity')}
        row.update(mtu='PASS',boot_continuity='PASS')
        row['network_type']=nettype
        if nettype=='geneve': row.update(dhcp_availability='PASS',dhcp_convergence='PASS')
        v.save(root/name, {'0':row,'1':row})
    for name in ('existing-network-semantics.json','post-ovn-bindings.json','tenant-dataplane-probe.json'):
        v.save(root/name, {'status':'PASS'})
    v.save(root/'tenant-dataplane-probe.json',{'status':'PASS','measurement_workload':'Pair A','evidence_source':'compute-tap-pcap',
           'pair_a_boot_continuity':'PASS','packet_loss_percent':0,'actual_dataplane_outage_seconds':0})
    v.save(root/'resource-consistency.json', {'networks':{'unchanged':True}})
    v.save(root/'validation-orchestration.json', {'semantics_rc':0, 'workload_rc':0})
    v.save(root/'workload-errors.json', [])
    for name in ('measure-readiness.json','measure-post-checks.json','dhcp-initial-preparation.json','dhcp-precutover-preparation.json'):
        v.save(root/name,{'status':'PASS'})
    v.save(root/'existing-mtu-automatic.json',{'status':'PASS'})
    v.save(root/'existing-mtu-remediation.json',{'status':'PASS','automatic_mtu_convergence':'PASS','remediation_required':False,'remediation_action':'none'})
    v.save(root/'existing-migration-baseline.json',{'0':{'boot':'one'},'1':{'boot':'one'}})


class EvidenceTests(unittest.TestCase):
    def rows(self):
        return [packet(i, i not in (4,5,6)) for i in range(1,12)]

    def test_one_loss_burst(self):
        m=v.probe_metrics(self.rows(),anchor(0),anchor(11),.2)
        self.assertEqual(m['status'],'PASS')
        self.assertEqual(m['packets_attempted'],11)
        self.assertEqual(m['packets_failed'],3)
        self.assertEqual(m['failure_burst_count'],1)
        self.assertEqual(m['maximum_consecutive_failed_probes'],3)
        self.assertAlmostEqual(m['actual_dataplane_outage_seconds'],.6)
        self.assertEqual(m['actual_dataplane_outage_seconds'],
                         m['longest_outage_recovery_timestamp']-m['longest_outage_start_timestamp'])

    def test_middle_burst_is_longest(self):
        rows=[packet(i, i not in (2,7,8,9,10,15,16)) for i in range(1,19)]
        m=v.probe_metrics(rows,anchor(0),anchor(18),.2)
        self.assertEqual(m['failure_burst_count'],3)
        self.assertEqual(m['first_failure_timestamp'],100.4)
        self.assertEqual(m['recovery_timestamp'],100.6)  # recovery for FIRST burst
        self.assertEqual(m['longest_outage_start_timestamp'],101.4)
        self.assertEqual(m['longest_outage_recovery_timestamp'],102.2)
        self.assertEqual(m['maximum_consecutive_failed_probes'],4)
        self.assertAlmostEqual(m['actual_dataplane_outage_seconds'],.8)
        self.assertEqual(m['actual_dataplane_outage_seconds'],
                         m['longest_outage_recovery_timestamp']-m['longest_outage_start_timestamp'])

    def test_missing_sequence_is_unavailable(self):
        rows=self.rows(); del rows[4]
        m=v.probe_metrics(rows,anchor(0),anchor(11),.2)
        self.assertEqual(m['status'],'UNAVAILABLE')
        self.assertIsNone(m['actual_dataplane_outage_seconds'])
        self.assertFalse(m['coverage_complete'])

    def test_missing_end_sequence_is_unavailable(self):
        self.assertEqual(v.probe_metrics(self.rows(),anchor(0),anchor(12),.2)['status'],'UNAVAILABLE')

    def test_unrecovered_tail_is_unavailable(self):
        rows=self.rows(); rows[-1]['success']=False
        m=v.probe_metrics(rows,anchor(0),anchor(11),.2)
        self.assertEqual(m['status'],'UNAVAILABLE')
        self.assertIsNone(m['actual_dataplane_outage_seconds'])
        self.assertIsNone(m['longest_outage_recovery_timestamp'])

    def test_empty_is_unavailable(self):
        self.assertEqual(v.probe_metrics([],anchor(0),anchor(11),.2)['status'],'UNAVAILABLE')

    def test_guest_reboot(self):
        rows=self.rows(); rows[-1]['boot']='two'
        self.assertEqual(v.probe_metrics(rows,anchor(0),anchor(11),.2)['status'],'UNAVAILABLE')
        self.assertEqual(v.probe_metrics(rows,anchor(0),anchor(1,'two'),.2)['status'],'UNAVAILABLE')

    def test_no_loss(self):
        m=v.probe_metrics([packet(i) for i in range(1,12)],anchor(0),anchor(11),.2)
        self.assertEqual(m['status'],'PASS')
        self.assertEqual(m['failure_burst_count'],0)
        self.assertEqual(m['actual_dataplane_outage_seconds'],0)
        self.assertIsNone(m['first_failure_timestamp'])
        self.assertIsNone(m['longest_outage_start_timestamp'])

    def test_sequence_window_excludes_staging_and_later_traffic(self):
        rows=[packet(i, 5 <= i <= 15, epoch=987654321) for i in range(1,21)]
        m=v.probe_metrics(rows,anchor(5),anchor(15),.2)
        self.assertEqual(m['status'],'PASS')
        self.assertEqual(m['packets_attempted'],10)
        self.assertEqual(m['packets_failed'],0)

    def test_console_dedup_and_conflict(self):
        rows=self.rows()
        self.assertEqual(v.probe_metrics(rows+rows,anchor(0),anchor(11),.2)['status'],'PASS')
        self.assertEqual(v.probe_metrics(rows+[packet(2,False)],anchor(0),anchor(11),.2)['status'],'UNAVAILABLE')

    def test_guest_timestamp_jump_is_unavailable(self):
        rows=[packet(i) for i in range(1,12)]
        rows[6]['ts'] += 1000
        self.assertEqual(v.probe_metrics(rows,anchor(0),anchor(11),.2)['status'],'UNAVAILABLE')

    def test_missing_anchor_is_unavailable(self):
        self.assertEqual(v.probe_metrics(self.rows(),None,anchor(11),.2)['status'],'UNAVAILABLE')

    def test_console_filter_and_malformed(self):
        row=dict(run='run',vm='pre0',kind='packet',seq=1,ts=100,success=True)
        log='boot message\nOVN_MIGRATION_JSON broken\nOVN_MIGRATION_JSON '+json.dumps(row)+'\n'
        self.assertEqual(v.records(log,'run','pre0'),[row])
        self.assertEqual(v.records(log,'other','pre0'),[])


class AnchorTests(unittest.TestCase):
    def test_stale_packets_and_health_cannot_pass(self):
        rows=[packet(i) for i in range(1,11)]
        rows.append(dict(kind='health',seq=10,boot='one',dhcp=True,metadata=True))
        a=v.freshness_anchor(rows)
        self.assertEqual(a,anchor(10))
        self.assertEqual(v.guest_checks(rows,a,.2)['connectivity'],'UNAVAILABLE')
        self.assertEqual(v.guest_checks(rows,a,.2)['metadata'],'UNAVAILABLE')
        newer=rows+[packet(i) for i in range(11,16)]
        self.assertEqual(v.guest_checks(newer,a,.2)['connectivity'],'PASS')
        self.assertEqual(v.guest_checks(newer,a,.2)['metadata'],'UNAVAILABLE')
        newer.append(dict(kind='health',seq=15,boot='one',dhcp=True,metadata=True))
        self.assertTrue(all(x=='PASS' for x in v.guest_checks(newer,a,.2).values()))

    def test_health_inflight_marker_is_fenced(self):
        rows=[packet(10),dict(kind='health',seq=12,boot='one',dhcp=True,metadata=True)]
        self.assertEqual(v.freshness_anchor(rows),anchor(12))

    def test_new_evidence_ignores_controller_clock_offset(self):
        rows=[packet(i,epoch=-123456789) for i in range(11,17)]
        rows.append(dict(kind='health',seq=16,boot='one',ts=-99999,dhcp=True,metadata=True))
        self.assertTrue(all(x=='PASS' for x in v.guest_checks(rows,anchor(10),.2).values()))

    def test_new_boot_does_not_pass_readiness(self):
        rows=[packet(i,boot='two') for i in range(11,17)]
        self.assertEqual(v.guest_checks(rows,anchor(10),.2)['connectivity'],'UNAVAILABLE')

    def test_start_checkpoint_is_immutable(self):
        with tempfile.TemporaryDirectory() as d:
            obj=v.Validation.__new__(v.Validation); obj.root=pathlib.Path(d)
            obj.collect=Mock(return_value={'0':[packet(20)]})
            self.assertEqual(obj.checkpoint_start(),anchor(20))
            obj.collect.return_value={'0':[packet(50)]}
            self.assertEqual(obj.checkpoint_start(),anchor(20))
            self.assertEqual(obj.collect.call_count,1)

    def test_bounded_console_and_dedup(self):
        with tempfile.TemporaryDirectory() as d:
            obj=v.Validation.__new__(v.Validation); obj.root=pathlib.Path(d)
            obj.cfg={'run':'run','console_tail_lines':123}
            obj.state={'pre':{'0':{'server':'vm1'},'1':{'server':'vm2'}}}
            compute=Mock()
            row=packet(1); row.update(run='run',vm='pre0')
            compute.get_server_console_output.return_value={'output':'OVN_MIGRATION_JSON '+json.dumps(row)}
            obj.cloud=SimpleNamespace(compute=compute)
            obj.collect('pre'); rows=obj.collect('pre')
            self.assertEqual(len(rows['0']),1)
            self.assertTrue(all(call.kwargs['length']==123 for call in compute.get_server_console_output.call_args_list))


class CleanupTests(unittest.TestCase):
    def test_gate_preserves_all_resources_on_validation_failure(self):
        with tempfile.TemporaryDirectory() as d:
            root=pathlib.Path(d); ready_evidence(root)
            obj=v.Validation.__new__(v.Validation); obj.root=root; obj.cleanup=Mock()
            v.save(root/'tenant-dataplane-probe.json',{'status':'UNAVAILABLE'})
            self.assertFalse(obj.finalize()); obj.cleanup.assert_not_called()
            v.save(root/'tenant-dataplane-probe.json',{'status':'PASS'})
            v.save(root/'existing-network-semantics.json',{'status':'FAIL'})
            self.assertFalse(obj.finalize()); obj.cleanup.assert_not_called()

    def test_success_cleanup_order(self):
        with tempfile.TemporaryDirectory() as d:
            root=pathlib.Path(d); ready_evidence(root)
            obj=v.Validation.__new__(v.Validation); obj.root=root; obj.cleanup=Mock()
            self.assertTrue(obj.finalize())
            self.assertEqual([c.args[0] for c in obj.cleanup.call_args_list],['post','pre'])

    def test_scoped_cleanup_retries(self):
        with tempfile.TemporaryDirectory() as d:
            obj=v.Validation.__new__(v.Validation); obj.root=pathlib.Path(d)
            obj.cfg={'timeout':1}; obj.path=obj.root/'validation-resources.json'
            pair={'router':'owned-router','security_group':'owned-sg'}
            for i in range(2):
                pair[str(i)]={k:'owned-'+k+str(i) for k in ('server','port','subnet','network')}
                pair[str(i)]['interface']=True
            obj.state={'pre':pair}
            compute=Mock(); compute.find_server.return_value=None
            network=Mock(); network.ports.return_value=[]
            network.delete_network.side_effect=[RuntimeError('temporary failure'),None,None]
            obj.cloud=SimpleNamespace(compute=compute,network=network)
            with self.assertRaisesRegex(RuntimeError,'temporary failure'): obj.cleanup('pre')
            self.assertEqual(v.read_evidence(obj.root,'pre-cleanup.json')['status'],'FAIL')
            obj.cleanup('pre'); obj.cleanup('pre')
            self.assertEqual(compute.delete_server.call_count,2)
            self.assertTrue(obj.state['pre']['cleaned'])
            evidence=v.read_evidence(obj.root,'pre-cleanup.json')
            self.assertEqual(evidence['status'],'PASS')
            self.assertEqual(len(evidence['deleted']),10)
            for call in compute.delete_server.call_args_list:
                self.assertIn(call.args[0],('owned-server0','owned-server1'))
            for method in ('delete_port','delete_subnet','delete_network','delete_router','delete_security_group'):
                for call in getattr(network,method).call_args_list:
                    self.assertTrue(call.args[0].startswith('owned-'))


class ReportTests(unittest.TestCase):
    def run_report(self, root):
        subprocess.run([sys.executable,str(ROOT/'scripts/migration_report.py'),str(root),
                        'test-run','test-inventory'],check=True,stdout=subprocess.DEVNULL)
        return json.loads((root/'migration-report.json').read_text())

    def test_optional_evidence_absent(self):
        with tempfile.TemporaryDirectory() as d:
            root=pathlib.Path(d); (root/'metrics').mkdir()
            report=self.run_report(root)
            self.assertEqual(report['dataplane_probe']['status'],'NOT TESTED')
            self.assertEqual(report['initial_ovs_workload_validation'],'NOT TESTED')
            self.assertNotIn('deployment_host_probe',report)
            text=(root/'migration-report.txt').read_text()
            self.assertNotIn('deployment-host',text)
            self.assertNotIn('estimated_outage_seconds',json.dumps(report))
            self.assertIn('Packet loss: UNAVAILABLE',text)
            self.assertIn('Actual dataplane outage: UNAVAILABLE',text)
            self.assertEqual(report['existing_workload_metadata'],'NOT TESTED')
            self.assertEqual(report['result'],'MIGRATED_VALIDATION_INCOMPLETE')

    def test_initial_and_post_results_are_independent(self):
        with tempfile.TemporaryDirectory() as d:
            root=pathlib.Path(d); (root/'metrics').mkdir(); ready_evidence(root)
            rows=v.read_evidence(root,'pre-workload-checks.json')
            rows['0']['metadata']='FAIL'
            v.save(root/'pre-workload-checks.json',rows)
            report=self.run_report(root)
            self.assertEqual(report['initial_ovs_workload_validation'],'PASS')
            self.assertEqual(report['existing_workload_post_migration_validation'],'FAIL')
            self.assertEqual(report['result'],'MIGRATED_VALIDATION_INCOMPLETE')

    def test_complete_evidence_requires_both_cleanups(self):
        with tempfile.TemporaryDirectory() as d:
            root=pathlib.Path(d); (root/'metrics').mkdir(); ready_evidence(root)
            self.assertEqual(self.run_report(root)['result'],'MIGRATED_VALIDATION_INCOMPLETE')
            for f in ('post-cleanup.json','pre-cleanup.json'): v.save(root/f,{'status':'PASS'})
            report=self.run_report(root)
            self.assertEqual(report['result'],'SUCCESS')
            self.assertNotIn('existing_ovs_workload_validation',report)
            self.assertEqual(report['existing_workload_post_migration_validation'],'PASS')
            self.assertEqual(report['initial_ovs_workload_validation'],'PASS')
            self.assertIn('Initial VM1/VM2 validation under ML2/OVS: PASS', (root/'migration-report.txt').read_text())


if __name__=='__main__':
    unittest.main()
