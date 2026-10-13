"""Report integrity, unavailable evidence and honest presentation boundaries."""
import copy
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

from test_validation import ROOT, ready_evidence, v
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

    def test_missing_evidence_never_looks_like_zero_loss_or_zero_duration(self):
        report = self.report()
        text = render_text(report, pathlib.Path('/run'), modern=True)
        self.assertRegex(text, r'Total run duration\s+: UNAVAILABLE')
        self.assertRegex(text, r'Packet loss\s+: UNAVAILABLE')
        self.assertRegex(text, r'Longest packet outage\s+: UNAVAILABLE')
        self.assertRegex(text, r'Resource identity preservation\s+: UNAVAILABLE')
        self.assertIn('Migration completed; validation or cleanup is incomplete.', text)
        self.assertNotIn('Migration and validation passed.', text)

    def test_valid_packet_numbers_survive_independent_workload_failure(self):
        report = self.report(
            dataplane_probe={'status': 'PASS', 'evidence_source': 'compute-tap-pcap',
                             'packet_loss_percent': 0.1835985312117503,
                             'actual_dataplane_outage_seconds': 4.985532999038696,
                             'packets_failed': 24, 'packets_attempted': 13072},
            existing_workload_metadata='FAIL',
        )
        before = copy.deepcopy(report)
        text = render_text(report, pathlib.Path('/run'), modern=True)
        self.assertIn('0.1836% (24 / 13072 probes)', text)
        self.assertIn('4.986 s', text)
        self.assertRegex(text, r'Metadata\s+: FAIL')
        self.assertEqual(report, before)

    def test_disabled_tcp_does_not_emit_pass_or_counter_table(self):
        report = self.report(pair_d_tcp={'status': 'DISABLED', 'streams': {
            'held': {'status': 'PASS', 'longest_recovered_failure_seconds': 0}}})
        text = render_text(report, pathlib.Path('/run'), modern=True)
        tcp = text.split('EW TCP -')[1].split('NORTH-SOUTH -')[0]
        self.assertRegex(tcp, r'Result\s+: DISABLED')
        self.assertNotIn('PASS', tcp)
        self.assertNotIn('Existing TCP session', tcp)
        self.assertIn('Existing ICMP / DHCP workloads', text)
        self.assertNotIn('Existing ICMP / DHCP / TCP workloads', text)

    def test_open_tcp_failure_is_visible_beside_previous_recovered_duration(self):
        report = self.report(pair_d_tcp={'status': 'FAIL', 'streams': {
            'held': {'status': 'FAIL', 'longest_recovered_failure_seconds': 6.666414792,
                     'open_failure_since': 1200},
        }})
        text = render_text(report, pathlib.Path('/run'), modern=True)
        self.assertRegex(text, r'Existing TCP session\s+FAIL\s+6\.666 s')
        self.assertIn('Unrecovered probe failure: Existing TCP session', text)
        self.assertNotIn('The new listener was opened during', text)

    def test_automatic_fail_and_remediation_are_not_hidden_by_success(self):
        report = self.report(
            result='SUCCESS_WITH_REMEDIATION',
            existing_workload_migration={'status': 'PASS AFTER REMEDIATION'},
            automatic_guest_mtu_convergence='FAIL',
            pre_cutover_remediation_action='soft reboot', pre_cutover_mtu_readiness='PASS',
            automatic_post_cutover_dhcp_convergence='FAIL',
            post_cutover_remediation_action='soft reboot', post_cutover_dhcp_ready='PASS',
        )
        text = render_text(report, pathlib.Path('/run'), modern=True)
        self.assertRegex(text, r'Automatic MTU convergence\s+: FAIL')
        self.assertRegex(text, r'Automatic DHCP convergence\s+: FAIL')
        self.assertEqual(text.count('soft reboot'), 2)
        self.assertIn('Boot checks account for the recorded remediation reboots.', text)

    def test_ns_broken_socket_details_are_visible_without_zero_outage_claim(self):
        report = self.report(north_south={'status': 'FAIL', 'directions': {
            'snat': {'held_session_preservation': 'FAIL', 'streams': {
                'held': {'status': 'FAIL', 'attempts': 10, 'failures': 10,
                    'open_failure_since': 120, 'longest_recovered_failure_seconds': None,
                    'terminal_error': '[Errno 104] Connection reset by peer',
                    'terminal_event': {'operation': 'receive', 'errno': 104, 'ts': 1700000000}},
                'new': {'status': 'PASS', 'attempts': 30, 'failures': 2,
                    'longest_recovered_failure_seconds': 1.5},
            }}}})
        text = render_text(report, pathlib.Path('/run'), modern=True)
        ns = text.split('NORTH-SOUTH -')[1].split('4. VALIDATION RESOURCE CLEANUP')[0]
        self.assertRegex(ns, r'held recovered probe failure\s+: UNAVAILABLE')
        self.assertIn('held failure still open', ns)
        self.assertIn('held first terminal error', ns)
        self.assertIn('[Errno 104] Connection reset by peer', ns)
        self.assertRegex(ns, r'held terminal operation\s+: receive')
        self.assertRegex(ns, r'held terminal errno\s+: 104')
        self.assertIn('1700000000', ns)
        self.assertRegex(ns, r'new recovered probe failure\s+: 1\.500 s')

    def test_historical_ns_error_is_not_claimed_to_be_original_error(self):
        report = self.report(north_south={'status': 'FAIL', 'directions': {
            'snat': {'held_session_preservation': 'FAIL', 'streams': {
                'held': {'status': 'FAIL', 'terminal_error':
                         'held socket already closed; reconnect prohibited'},
            }}}})
        text = render_text(report, pathlib.Path('/run'), modern=True)
        self.assertIn('held recorded terminal error', text)
        self.assertNotIn('held first terminal error', text)
        self.assertNotIn('held terminal epoch', text)

    def test_missing_and_zero_times_remain_distinct_and_legacy_scope_is_honest(self):
        for value, expected in [(None, 'UNAVAILABLE'), (0, '0.000 s'),
                                (float('nan'), 'UNAVAILABLE'), (True, 'UNAVAILABLE')]:
            with self.subTest(value=value):
                report = self.report(db_migration_seconds=value, phase_marker_schema_version=1)
                text = render_text(report, pathlib.Path('/run'), modern=False)
                self.assertRegex(text, r'DB migration\s+: ' + expected)
                self.assertIn('excludes report/finalization and capture cleanup.', text)
                self.assertIn('Legacy VM1/VM2 migration', text)
                self.assertNotIn('Total duration includes preparation, validation, cleanup', text)

    def test_terminal_summary_contains_only_result_run_and_paths(self):
        report = self.report()
        self.assertEqual(terminal_summary(report, pathlib.Path('/run')).splitlines(), [
            'RESULT: MIGRATED_VALIDATION_INCOMPLETE', 'Run: test-run',
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
            result = subprocess.run([sys.executable, str(ROOT/'scripts/migration_report.py'),
                                     d, 'test-run', 'inventory'],
                                    check=True, capture_output=True, text=True)
            report = json.loads((root/'migration-report.json').read_text())
            text = (root/'migration-report.txt').read_text()
            self.assertEqual(report['result'], 'SUCCESS')
            self.assertEqual(report['dataplane_probe'], probe)
            self.assertEqual(len(report['phase_timings']), 14)
            self.assertIsNone(report['total_duration_seconds'])
            self.assertEqual(len(result.stdout.splitlines()), 4)
            self.assertNotIn('1791699764', text)
            self.assertNotIn('Individual check fields', text)
            self.assertNotIn('Canonical phase timings', text)
            self.assertNotIn('Tenant dataplane probe:', text)


if __name__ == '__main__':
    unittest.main()
