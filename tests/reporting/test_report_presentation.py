"""Report integrity, unavailable evidence and honest presentation boundaries."""
import copy
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

from tests.validation.test_validation import ROOT, ready_evidence, v
from report_format import ordered_report, render_text, terminal_summary


class PresentationTests(unittest.TestCase):
    def report(self, **changes):
        report = {
            'result': 'MIGRATED_VALIDATION_INCOMPLETE', 'run_id': 'test-run',
            'phase_marker_schema_version': 2,
            'dataplane_probe': {'status': 'UNAVAILABLE', 'packet_loss_percent': 0,
                               'actual_dataplane_outage_seconds': 0},
            'pair_d_tcp': {'status': 'DISABLED'},
        }
        report.update(changes)
        return report

    def test_ordering_preserves_every_value_and_unknown_future_fields(self):
        report = self.report(
            extra_metric={'unrounded': 0.1835985312117503, 'unknown': None},
            phase_timings=[{'duration_seconds': None, 'availability': 'INCOMPLETE'}],
            automatic_guest_mtu_convergence='FAIL',
            existing_workload_migration={'status': 'PASS AFTER REMEDIATION'},
        )
        snapshot = copy.deepcopy(report)
        ordered = ordered_report(report)
        self.assertEqual(ordered, snapshot)
        self.assertEqual(report, snapshot)
        self.assertEqual(json.loads(json.dumps(ordered)), snapshot)
        keys = list(ordered)
        self.assertLess(keys.index('phase_timings'), keys.index('dataplane_probe'))
        self.assertLess(keys.index('dataplane_probe'), keys.index('pair_d_tcp'))
        self.assertIn('extra_metric', keys)

    def text(self, **changes):
        return render_text(self.report(**changes), pathlib.Path('/run'), modern=True)

    def test_missing_evidence_never_looks_like_zero_loss_or_zero_duration(self):
        text = self.text()
        for label in ('Total run duration', 'Packet loss', 'Longest packet outage',
                      'Failure burst count', 'Probe failure rate'):
            self.assertRegex(text, label + r'\s+: UNAVAILABLE')
        self.assertNotIn('0.000 s', text)

    def test_valid_packet_numbers_survive_independent_workload_failure(self):
        report = self.report(
            dataplane_probe={'status': 'PASS', 'evidence_source': 'compute-tap-pcap',
                             'packet_loss_percent': 0.1835985312117503,
                             'actual_dataplane_outage_seconds': 4.985532999038696,
                             'packets_failed': 24, 'packets_attempted': 13072,
                             'failure_burst_count': 3},
            existing_workload_metadata='FAIL')
        before = copy.deepcopy(report)
        text = render_text(report, pathlib.Path('/run'), modern=True)
        self.assertIn('0.1836% (24 / 13072 probes)', text)
        self.assertIn('4.986 s', text)
        self.assertRegex(text, r'Failure burst count\s+: 3')
        self.assertNotIn('Metadata', text)
        self.assertEqual(report, before)

    def test_fixed_flow_and_direction_order_with_identical_fields_for_fip(self):
        stream = dict(status='PASS', attempts=100, failures=2, failure_burst_count=1,
                      longest_recovered_failure_seconds=1.23456789)
        text = self.text(pair_d_tcp={'status':'PASS', 'streams':{
            k: stream for k in ('held','new_old','new_listener')}}, north_south={
            'status':'PASS', 'directions':{k:{'streams':{'held':stream,'new':stream}}
                                         for k in ('fip','snat')}})
        labels = ['Different compute / different subnet (ICMP)',
                  'Different compute / same subnet (ICMP)',
                  'Same compute / different subnet (ICMP)',
                  'Same compute / same subnet (ICMP)',
                  'Different compute / different subnet (TCP)',
                  'Outbound (SNAT, TCP)', 'Inbound (FIP, TCP)']
        self.assertEqual([text.index(label) for label in labels],
                         sorted(text.index(label) for label in labels))
        self.assertEqual(text.count('2.0000% (2 / 100 probes)'), 7)
        self.assertEqual(text.count('1.235 s'), 7)
        self.assertEqual(text.count('Probe failure rate'), 7)
        self.assertEqual(text.count('Longest probe failure'), 7)
        self.assertEqual(text.count('Packet loss'), 4)

    def test_disabled_tcp_does_not_use_stale_pass_counters(self):
        text = self.text(pair_d_tcp={'status':'DISABLED', 'streams':{
            'held':{'status':'PASS','longest_recovered_failure_seconds':0}}})
        self.assertNotIn('Different compute / different subnet (TCP)',text)
        self.assertNotIn('Existing TCP session',text.split('2. NORTH')[0])

    def test_open_tcp_failure_takes_precedence_over_earlier_recovery(self):
        text = self.text(pair_d_tcp={'status':'FAIL','streams':{'held':{
            'status':'FAIL','longest_recovered_failure_seconds':6.666414792,
            'open_failure_since':1200,'failure_burst_count':2,
            'attempts':10,'failures':4}}})
        held = text.split('Existing TCP session')[1].split('Old listener')[0]
        self.assertRegex(held, r'Result\s+: FAIL')
        self.assertRegex(held, r'Longest probe failure\s+: UNRECOVERED')
        self.assertRegex(held, r'Failure burst count\s+: 2')
        self.assertIn('40.0000% (4 / 10 probes)', held)
        self.assertNotIn('6.666 s', text)

    def test_only_traffic_summary_in_text_and_all_other_values_preserved(self):
        report = self.report(result='SUCCESS_WITH_REMEDIATION',
            existing_workload_migration={'status':'PASS AFTER REMEDIATION'},
            automatic_guest_mtu_convergence='FAIL',
            pre_cutover_remediation_action='soft reboot',
            new_ovn_workload_provisioning='PASS', existing_workload_cleanup='PASS',
            phase_timings=[{'duration_seconds':1.23456789}],
            north_south_diagnostics={'status':'PASS'})
        before = copy.deepcopy(report)
        text = render_text(report, pathlib.Path('/run'), modern=True)
        for hidden in ('DHCP','MTU','Metadata','metadata','provisioning','Provisioning',
                       'cleanup','CLEANUP','remediation','soft reboot','diagnostic',
                       'REPORT FILES','DB migration','Boot continuity'):
            self.assertNotIn(hidden, text)
        self.assertEqual(report, before)
        self.assertEqual(ordered_report(report), before)

    def test_ns_broken_socket_is_unrecovered_but_details_stay_in_json(self):
        text = self.text(north_south={'status':'FAIL','directions':{
            'snat':{'streams':{'held':{'status':'FAIL','attempts':10,'failures':10,
                'failure_burst_count':1, 'open_failure_since':120,
                'terminal_error':'[Errno 104] Connection reset by peer',
                'terminal_event':{'operation':'receive','ts':1700000000}}}}}})
        ns = text.split('Outbound (SNAT, TCP)')[1].split('Inbound')[0]
        self.assertRegex(ns, r'Longest probe failure\s+: UNRECOVERED')
        self.assertIn('100.0000% (10 / 10 probes)', ns)
        self.assertNotIn('Connection reset', text)
        self.assertNotIn('1700000000', text)

    def test_historical_tcp_missing_burst_counter_is_unavailable(self):
        text = self.text(pair_d_tcp={'status':'PASS','streams':{
            'held':{'status':'PASS','attempts':30,'failures':2,
                    'longest_recovered_failure_seconds':1.5}}})
        held = text.split('Existing TCP session')[1].split('Old listener')[0]
        self.assertRegex(held, r'Failure burst count\s+: UNAVAILABLE')
        self.assertRegex(held, r'Longest probe failure\s+: 1.500 s')

    def test_dataplane_is_a_heading_and_never_prints_port_binding_timer(self):
        for value in (None, 0, float('nan'), True, 114.17412345):
            with self.subTest(value=value):
                text = self.text(ovn_portbinding_convergence_seconds=value)
                self.assertIn('\nDataplane downtime:\n', text)
                self.assertNotRegex(text, r'Dataplane downtime[ \t]*:[ \t]*\S')
                self.assertNotIn('114.174', text)
        text = self.text(dataplane_convergence_seconds=2.5)
        self.assertIn('\nDataplane downtime:\n', text)
        self.assertNotIn('2.500 s', text)

    def test_terminal_summary_contains_only_run_and_paths(self):
        report = self.report()
        self.assertEqual(terminal_summary(report, pathlib.Path('/run')).splitlines(), [
            'Run: test-run',
            'Text report: /run/migration-report.txt', 'JSON report: /run/migration-report.json'])

    def test_end_to_end_json_keeps_precise_evidence_hidden_from_text(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            (root/'metrics').mkdir()
            (root/'runtime.json').write_text(json.dumps({'phase_marker_schema_version': 2}))
            ready_evidence(root)
            for name in ('pre-cleanup.json', 'post-cleanup.json'):
                v.save(root/name, {'status': 'PASS'})
            probe = v.read_evidence(root, 'tenant-dataplane-probe.json')
            probe.update(packet_loss_percent=0.1835985312117503,
                         actual_dataplane_outage_seconds=4.985532999038696,
                         packets_failed=24, packets_attempted=13072,
                         start_anchor={'timestamp': 1791699764.692184})
            v.save(root/'tenant-dataplane-probe.json', probe)
            result = subprocess.run([sys.executable, str(ROOT/'scripts/reporting/migration_report.py'),
                                     d, 'test-run', 'inventory'],
                                    check=True, capture_output=True, text=True)
            report = json.loads((root/'migration-report.json').read_text())
            text = (root/'migration-report.txt').read_text()
            self.assertEqual(report['result'], 'SUCCESS')
            self.assertEqual(report['dataplane_probe'], probe)
            self.assertEqual(len(report['phase_timings']), 14)
            self.assertIsNone(report['total_duration_seconds'])
            self.assertEqual(len(result.stdout.splitlines()), 3)
            self.assertNotIn('1791699764', text)
            self.assertNotIn('Individual check fields', text)
            self.assertNotIn('Canonical phase timings', text)
            self.assertNotIn('Tenant dataplane probe:', text)


if __name__ == '__main__':
    unittest.main()
