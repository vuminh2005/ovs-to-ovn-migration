"""Real socket ARM, unchanged held identity, sender PCAP and full N-S text report."""
import copy
import json
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from tests.validation.test_validation import ROOT
from tests.diagnostics.test_dataplane_capture import frame
from ns_probe import Client, Server
from ns_control import request
from ns_icmp_evidence import finalize
from report_format import render_text
from traffic_timeline import build, task_at


def free_ports(count):
    sockets = [socket.socket() for _ in range(count)]
    try:
        for sock in sockets:
            sock.bind(('127.0.0.1', 0))
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def wait_for(check, timeout=3):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(.01)
    raise AssertionError('Timed out waiting for socket evidence')


class ExpandedSocketTests(unittest.TestCase):
    def test_additional_listener_opens_only_on_arm_and_held_socket_is_not_replaced(self):
        old, new, control = free_ports(3)
        cfg = dict(run='new-run', bind='127.0.0.1', source='127.0.0.1', target='127.0.0.1',
            port=old, new_port=new, control_port=control, control_token='private-token',
            expected_peer='127.0.0.1', expected_server='server', server='server',
            interval=.03, timeout=.2, lifetime=10, console=False)
        server = Server(cfg)
        client = Client(cfg)
        workers = [threading.Thread(target=client.stream, args=(name,), daemon=True) for name in client.stats]
        workers.append(threading.Thread(target=client.control_server, daemon=True))
        for worker in workers:
            worker.start()
        try:
            wait_for(lambda: client.stats['held'].snapshot()['successes'] >= 3)
            before = client.stats['held'].snapshot()
            self.assertEqual(client.stats['new_listener'].snapshot()['attempts'], 0)
            with self.assertRaises(OSError):
                socket.create_connection(('127.0.0.1', new), .1)
            payload = dict(run=cfg['run'], token=cfg['control_token'], ip='127.0.0.1', port=control, action='CHECK')
            checked = request(payload)
            self.assertFalse(checked['armed'])
            ack = request(dict(payload, action='ARM'))
            wait_for(lambda: client.stats['new_listener'].snapshot()['successes'] >= 3)
            again = request(dict(payload, action='ARM'))
            self.assertEqual(again['armed_mono'], ack['armed_mono'])
            self.assertEqual(again['listener_ack'], ack['listener_ack'])
            held = client.stats['held'].snapshot()
            self.assertEqual(held['connection_attempts'], 1)
            self.assertEqual(held['first_session'], before['first_session'])
            self.assertEqual(held['last_session'], before['last_session'])
            self.assertFalse(held['held_broken'])
            self.assertNotEqual(client.stats['new_listener'].snapshot()['first_session']['listener'],
                                held['first_session']['listener'])
            self.assertGreater(client.stats['new_old'].snapshot()['connection_attempts'], 2)
        finally:
            client.stop.set()
            for worker in workers:
                worker.join(2)
            for _, listener in server.listeners.values():
                listener.shutdown()
                listener.server_close()

    def test_cannot_arm_without_a_preexisting_held_session(self):
        client = Client(dict(run='run', source='127.0.0.1', target='127.0.0.1', port=1,
            new_port=2, interval=.1, timeout=.1))
        with self.assertRaisesRegex(RuntimeError, 'held session'):
            client.arm()
        self.assertFalse(client.armed)


class ExpandedPcapTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        ns = dict(enabled=True, icmp_enabled=True, outbound_ip='10.233.0.11',
            endpoint_ip='10.1.0.187', fip_ip='10.1.0.5')
        (self.root/'validation-config.json').write_text(json.dumps(dict(ns=ns, interval=.2)))
        (self.root/'ns-baseline.json').write_text(json.dumps(dict(clients={
            d:dict(ts=10.3) for d in ('snat', 'fip')})))
        (self.root/'ns-results.json').write_text(json.dumps(dict(status='PASS', directions={
            d:dict(status='PASS', streams={}) for d in ('snat', 'fip')})))
        self.captures = {}
        for direction, kind, source, peer in (
            ('snat','guest-snat','10.233.0.11','10.1.0.187'),
            ('fip','endpoint','10.1.0.187','10.1.0.5')):
            folder = self.root/'ns-diagnostics'/kind
            folder.mkdir(parents=True)
            data = b'\xd4\xc3\xb2\xa1'+struct.pack('<HHIIII',2,4,0,0,65535,1)
            packets = []
            for i in range(1,16):
                ts = 10+i*.2
                packets.append((ts,frame(source,peer,i)))
                if i not in (3,4,8):
                    packets.append((ts+.01,frame(source,peer,i,True)))
            for ts, packet in sorted(packets):
                sec = int(ts)
                data += struct.pack('<IIII',sec,round((ts-sec)*1e6),len(packet),len(packet))+packet
            (folder/'tcp.pcap').write_bytes(data)
            self.captures[kind] = dict(remote=dict(status='STOPPED', returncode=0, dropped_packets=0,
                started_at=9, stopped_at=15, supervisor_gap=False))
        self.write_state()

    def write_state(self):
        (self.root/'ns-diagnostics.json').write_text(json.dumps(dict(captures=self.captures)))

    def test_both_directions_use_sender_capture_and_sum_all_loss_bursts(self):
        outcomes = finalize(self.root)
        for value in outcomes.values():
            self.assertEqual(value['status'], 'PASS', value)
            self.assertEqual(value['failure_burst_count'], 2)
            self.assertEqual(value['packets_failed'], 3)
            self.assertAlmostEqual(value['actual_dataplane_outage_seconds'], .41)
            self.assertAlmostEqual(value['total_downtime_seconds'], .62)
            self.assertEqual(value['measurement_workload'][:3], 'N-S')

    def test_capture_drops_never_look_like_zero_loss(self):
        self.captures['guest-snat']['remote']['dropped_packets'] = 1
        self.write_state()
        outcomes = finalize(self.root)
        self.assertEqual(outcomes['snat']['status'], 'UNAVAILABLE')
        self.assertNotEqual(json.loads((self.root/'ns-results.json').read_text())['status'], 'PASS')

    def test_sender_log_explains_sequence_gap_without_certifying_missing_packets(self):
        cfg=json.loads((self.root/'validation-config.json').read_text());cfg['run']='run-gap'
        (self.root/'validation-config.json').write_text(json.dumps(cfg))
        baseline=json.loads((self.root/'ns-baseline.json').read_text())
        baseline['clients']['fip'].update(boot='boot',icmp=dict(log_id='log',pid=42))
        (self.root/'ns-baseline.json').write_text(json.dumps(baseline))
        packets=[]
        for i in range(1,16):
            if i==4:continue
            ts=10+i*.2
            packets.append((ts,frame('10.1.0.187','10.1.0.5',i)))
            if i not in (3,8):packets.append((ts+.01,frame('10.1.0.187','10.1.0.5',i,True)))
        data=b'\xd4\xc3\xb2\xa1'+struct.pack('<HHIIII',2,4,0,0,65535,1)
        for ts,packet in sorted(packets):
            sec=int(ts);data+=struct.pack('<IIII',sec,round((ts-sec)*1e6),len(packet),len(packet))+packet
        (self.root/'ns-diagnostics/endpoint/tcp.pcap').write_bytes(data)
        folder=self.root/'ns';folder.mkdir()
        log=[]
        for i in range(1,18):
            ts=10+i*.2-.02
            log.append(dict(run='run-gap',label='fip',boot='boot',log_id='log',ping_pid=42,event_index=i,
                observed_ts=ts,observed_mono=ts,icmp_sequence=i,native_ts=ts,
                kind='error' if i==4 else 'reply',text='send failure' if i==4 else 'reply'))
        (folder/'fip-icmp-sender.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in log))
        value=finalize(self.root)['fip']
        self.assertEqual(value['status'],'UNAVAILABLE')
        self.assertEqual(value['sender_log']['status'],'PASS')
        self.assertEqual(value['request_discontinuities'][0]['missing_request_count'],1)
        self.assertEqual(value['request_discontinuities'][0]['sender_observations'][0]['kind'],'error')
        self.assertFalse(value['observed_window']['certified'])
        self.assertIsNone(value['actual_dataplane_outage_seconds'])


class ExpandedReportTests(unittest.TestCase):
    def test_eight_ns_flows_have_task_events_without_printing_timestamps(self):
        stream = dict(status='PASS', attempts=20, failures=2, failure_burst_count=1,
            longest_recovered_failure_seconds=2.0, total_downtime_seconds=2.0,
            start_timestamp=100, end_timestamp=200, first_failure_timestamp=120, recovery_timestamp=122)
        ping = dict(status='PASS', packets_attempted=20, packets_failed=1, packet_loss_percent=5,
            failure_burst_count=1, actual_dataplane_outage_seconds=.21, total_downtime_seconds=.21,
            start_timestamp=100, end_timestamp=200, first_failure_timestamp=120, recovery_timestamp=120.21)
        ns = dict(status='PASS', icmp_enabled=True, new_listener_enabled=True,
            directions={d:dict(status='PASS', icmp=ping, streams={
                n:stream for n in ('held', 'new_old', 'new_listener')}) for d in ('snat', 'fip')})
        with tempfile.TemporaryDirectory() as directory:
            timeline = build(Path(directory), ns)
            text = render_text(dict(north_south=ns, traffic_timeline=timeline), Path(directory), modern=True)
        ns_text = text.split('2. NORTH - SOUTH')[1]
        self.assertEqual(ns_text.count('Existing TCP session'), 2)
        self.assertEqual(ns_text.count('Old listener / new connection'), 2)
        self.assertEqual(ns_text.count('New listener / new connection'), 2)
        self.assertEqual(ns_text.count('Total packet downtime'), 2)
        self.assertEqual(ns_text.count('Start measurement'), 8)
        self.assertEqual(ns_text.count('First recovery'), 8)
        self.assertNotIn('+07:00', ns_text)
        self.assertNotIn('1970-', ns_text)
        self.assertEqual(timeline['snat/held']['start_timestamp']['timestamp'],100)

    def test_migration_report_script_writes_expanded_ns_to_txt_and_keeps_json(self):
        from tests.validation.test_validation import ready_evidence
        stream = dict(status='PASS', attempts=10, failures=0, failure_burst_count=0,
            longest_recovered_failure_seconds=0, total_downtime_seconds=0,
            start_timestamp=100, end_timestamp=200)
        ping = dict(status='PASS', packets_attempted=10, packets_failed=0, packet_loss_percent=0,
            failure_burst_count=0, actual_dataplane_outage_seconds=0, total_downtime_seconds=0,
            start_timestamp=100, end_timestamp=200)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'metrics').mkdir()
            ready_evidence(root)
            (root/'validation-config.json').write_text(json.dumps(dict(ns=dict(enabled=True,
                icmp_enabled=True, new_port=18181))))
            ns = dict(status='PASS', directions={d:dict(status='PASS', icmp=ping,
                streams={n:stream for n in ('held','new_old','new_listener')}) for d in ('snat','fip')})
            (root/'ns-results.json').write_text(json.dumps(ns))
            subprocess.run([sys.executable,str(ROOT/'scripts/reporting/migration_report.py'),str(root),'new-run','inventory'],
                check=True,capture_output=True,text=True)
            text = (root/'migration-report.txt').read_text().split('2. NORTH - SOUTH')[1]
            report = json.loads((root/'migration-report.json').read_text())
            self.assertEqual(text.count('Start measurement'),8)
            self.assertIn('Inbound (FIP, ICMP)',text)
            self.assertEqual(report['north_south']['directions']['snat']['streams']['new_listener'],stream)
            self.assertEqual(len(report['traffic_timeline']),15)

    def test_nested_kolla_task_inherits_enclosing_phase(self):
        rows = [dict(started_at=dict(epoch=100), ended_at=dict(epoch=200), play='Phase 08 - Deploy',
                     task_name='Deploy OVN controller', source='/repo/playbooks/08-cutover.yml:1'),
                dict(started_at=dict(epoch=120), ended_at=dict(epoch=130), play='Apply role ovn-controller',
                     task_name='Restart ovn-controller container', host='network1')]
        result = task_at(125, rows)
        self.assertEqual(result['phase'], '08')
        self.assertEqual(result['task'], 'Restart ovn-controller container')
        self.assertEqual(result['status'], 'OBSERVED_OVERLAP')


if __name__ == '__main__':
    unittest.main()
