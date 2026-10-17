"""Correlate EW/NS event clocks without relabeling binding time as an outage."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from test_validation import ROOT
from report_format import render_text
from traffic_timeline import build, tcp_clock_events


class TrafficTimelineTests(unittest.TestCase):
    def test_all_fifteen_flows_keep_clocks_in_json_and_only_tasks_in_text(self):
        ping = dict(status='PASS', start_anchor={'timestamp':1005}, end_anchor={'timestamp':1200},
            first_failure_timestamp=1090, recovery_timestamp=1090.4, failure_burst_count=1,
            packets_attempted=100, packets_failed=2, packet_loss_percent=2,
            actual_dataplane_outage_seconds=.4)
        keys = ('ew_different_compute_different_subnet', 'ew_different_compute_same_subnet',
                'ew_same_compute_different_subnet', 'ew_same_compute_same_subnet')
        ew = dict(status='PASS', flows={k:ping for k in keys})
        stream = dict(status='PASS', first_failure_mono=190, first_recovery_mono=191,
            failure_burst_count=2, longest_recovered_failure_seconds=8, open_failure_since=None,
            attempts=100, failures=3)
        tcp = dict(status='PASS', snapshot={'0':dict(ts=1200, mono=300, armed_mono=100)},
            streams={k:stream for k in ('held','new_old','new_listener')})
        ns_stream = dict(stream, start_timestamp=1005, end_timestamp=1200,
                         first_failure_timestamp=1090, recovery_timestamp=1091)
        ns = dict(status='PASS', icmp_enabled=True, new_listener_enabled=True,
            directions={d:dict(icmp=ping, streams={k:ns_stream for k in tcp['streams']})
                        for d in ('snat','fip')})
        report = dict(dataplane_probe=ping, ew_connectivity_matrix=ew, pair_d_tcp=tcp,
            north_south=ns, ovn_portbinding_convergence_seconds=987.654321)
        before = copy.deepcopy(report)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'task-timings').mkdir()
            tasks = [dict(execution_id=str(i), started_at=dict(epoch=start), ended_at=dict(epoch=end),
                          play='Phase '+phase, task_name=task)
                     for i,(start,end,phase,task) in enumerate((
                         (999,1010,'07','Arm probes'), (1080,1110,'08','Deploy OVN controller'),
                         (1190,1210,'12','Validate workloads')))]
            (root/'task-timings/main.json').write_text(json.dumps({'tasks':tasks}))
            timeline = build(root, ns, ew=ew, dataplane=ping, tcp=tcp)
            self.assertEqual(len(timeline),15)
            for key in keys:
                self.assertEqual(timeline[key]['start_timestamp']['timestamp'],1005)
                self.assertEqual(timeline[key]['end_timestamp']['task']['phase'],'12')
                self.assertEqual(timeline[key]['first_failure_timestamp']['task']['phase'],'08')
            first = timeline['ew_tcp/held']
            self.assertEqual(first['start_timestamp']['timestamp'],1000)
            self.assertEqual(first['recovery_timestamp']['timestamp'],1091)
            self.assertEqual(first['recovery_timestamp']['task']['phase'],'08')
            self.assertEqual(first['clock_scope'],
                'Client snapshot wall-minus-monotonic mapping; task correlation only')
            self.assertEqual(report,before)
            report['traffic_timeline'] = timeline
            text = render_text(report,root,modern=True)
        for label in ('Start measurement','End measurement','First failure','First recovery'):
            self.assertEqual(text.count(label),15)
        self.assertIn('phase 07 | Arm probes',text)
        self.assertIn('phase 08 | Deploy OVN controller',text)
        self.assertNotIn('1970-',text)
        self.assertNotIn('+07:00',text)
        self.assertNotIn('987.654',text)
        self.assertEqual(report['ovn_portbinding_convergence_seconds'],987.654321)

    def test_legacy_recovery_is_derived_only_for_one_completed_burst(self):
        tcp = dict(snapshot={'0':dict(ts=1200,mono=300,armed_mono=100)})
        probe = dict(failure_burst_count=1,first_failure_mono=190,
                     longest_recovered_failure_seconds=8,open_failure_since=None)
        result = tcp_clock_events(tcp,probe)
        self.assertEqual(result['recovery_timestamp'],1098)
        self.assertEqual(result['recovery_source'],'legacy-single-recovered-burst-duration')
        self.assertIsNone(tcp_clock_events(tcp,dict(probe,failure_burst_count=2))['recovery_timestamp'])
        self.assertIsNone(tcp_clock_events(tcp,dict(probe,open_failure_since=190))['recovery_timestamp'])

    def test_missing_clock_never_becomes_epoch_zero_and_zero_failures_are_explicit(self):
        self.assertIsNone(tcp_clock_events({},dict(first_failure_mono=10))['first_failure_timestamp'])
        tcp = dict(status='PASS',streams={'held':dict(status='PASS',failure_burst_count=0)},
                   snapshot={'0':dict(ts=1200,mono=300,armed_mono=100)})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            timeline = build(root,{},tcp=tcp)
            text = render_text(dict(pair_d_tcp=tcp,traffic_timeline=timeline),root,modern=True)
        held = text.split('Existing TCP session')[1].split('Old listener')[0]
        self.assertEqual(held.count('NONE (no failure)'),2)
        self.assertNotIn('1970-',text)


if __name__ == '__main__':
    unittest.main()
