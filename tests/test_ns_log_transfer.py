"""Real echo sockets export a durable file without touching held/ping identity."""
import copy
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from test_validation import ROOT
from test_ns_expanded_probes import free_ports, wait_for
from ns_probe import Client, Server
from ns_log_transfer import collect, digest, line, send, upload, verify_export


class SenderTransferTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root/'ns').mkdir()
        self.source = self.root/'guest.jsonl'
        rows = [dict(run='run', label='snat', boot='boot', log_id='log', ping_pid=42,
            event_index=i, observed_ts=100+i*.2, observed_mono=200+i*.2,
            kind='start' if i == 1 else 'reply', text='original source record '*8)
            for i in range(1, 4001)]
        self.original = ''.join(json.dumps(r)+'\n' for r in rows).encode()
        self.source.write_bytes(self.original)
        old, new, control = free_ports(3)
        self.cfg = dict(run='run', bind='127.0.0.1', source='127.0.0.1', target='127.0.0.1',
            port=old, new_port=new, control_port=control, control_token='private-token',
            server='endpoint', expected_server='endpoint', expected_peer='127.0.0.1',
            console=False, interval=.02, timeout=.3, lifetime=30,
            icmp_export_enabled=True, icmp_output=str(self.source),
            icmp_receive_path=str(self.root/'ns/snat-icmp-sender.full.jsonl'))
        self.server = Server(self.cfg)
        def close_server():
            for _, listener in list(self.server.listeners.values()):
                listener.shutdown()
                listener.server_close()
        self.addCleanup(close_server)
        self.client = Client(self.cfg)
        self.client.ping = SimpleNamespace(boot='boot', snapshot=lambda:dict(log_id='log', pid=42))
        self.expected = dict(run='run', label='snat', boot='boot', log_id='log', ping_pid=42,
                             probe_id=self.client.probe_id)
        self.baseline = dict(boot='boot', probe_id=self.client.probe_id, icmp=dict(log_id='log', pid=42))
        (self.root/'ns-baseline.json').write_text(json.dumps(dict(clients=dict(snat=self.baseline))))
        self.states = {k:dict(remote=dict(status='STOPPED')) for k in ('endpoint','guest-snat','guest-fip')}
        (self.root/'ns-diagnostics.json').write_text(json.dumps(dict(captures=self.states)))
        self.extended = SimpleNamespace(root=self.root, cfg=dict(run='run', ns=dict(endpoint_ip='127.0.0.1', port=old)),
            state=dict(ns=dict(control_token='private-token')))
        self.workers = [threading.Thread(target=self.client.stream, args=(name,), daemon=True)
                        for name in self.client.stats]
        for worker in self.workers:
            worker.start()
        def close_client():
            self.client.stop.set()
            for worker in self.workers:
                worker.join(2)
        self.addCleanup(close_client)
        wait_for(lambda:self.client.stats['held'].snapshot()['successes'] >= 3)
        self.client.arm()
        wait_for(lambda:self.client.stats['new_listener'].snapshot()['successes'] >= 3)

    def test_full_export_hash_identity_ports_and_held_connection_are_preserved(self):
        mirror = self.root/'ns/snat-icmp-sender.jsonl'
        mirror.write_bytes(b'incomplete console mirror\n')
        held = self.client.stats['held'].snapshot()
        receipt = collect(self.extended, timeout=5)
        full = Path(self.cfg['icmp_receive_path'])
        self.assertEqual(full.read_bytes(), self.original)
        self.assertEqual(receipt['sha256'], digest(full))
        self.assertEqual(receipt['event_count'], 4000)
        self.assertEqual(full.stat().st_mode & 0o777, 0o600)
        self.assertEqual(mirror.read_bytes(), b'incomplete console mirror\n')
        self.assertEqual(self.source.read_bytes(), self.original)
        after = self.client.stats['held'].snapshot()
        self.assertEqual(after['connection_attempts'], 1)
        self.assertEqual(after['last_session'], held['first_session'])
        self.assertFalse(after['held_broken'])
        before = full.read_bytes()
        self.assertEqual(collect(self.extended, timeout=1), receipt)
        self.assertEqual(full.read_bytes(), before)

    def test_running_capture_blocks_export_before_any_request(self):
        self.states['endpoint']['remote']['status'] = 'RUNNING'
        (self.root/'ns-diagnostics.json').write_text(json.dumps(dict(captures=self.states)))
        with self.assertRaisesRegex(ValueError, 'before captures stop'):
            collect(self.extended, timeout=.1)
        self.assertIsNone(self.server.log_receiver.pending)

    def test_export_waits_for_native_sender_log_to_cross_capture_end(self):
        self.states['guest-snat']['remote']['stopped_at'] = 1000
        (self.root/'ns-diagnostics.json').write_text(json.dumps(dict(captures=self.states)))
        def finish_original_log():
            wait_for(lambda:self.server.log_receiver.pending is not None)
            time.sleep(.25)
            last=json.loads(self.original.splitlines()[-1])
            last.update(event_index=4001,observed_mono=1000.2,observed_ts=1002.2,native_ts=1002.2)
            with self.source.open('ab') as stream:
                stream.write((json.dumps(last)+'\n').encode())
        writer=threading.Thread(target=finish_original_log)
        writer.start()
        try:
            receipt=collect(self.extended,timeout=5)
        finally:
            writer.join(4)
        self.assertEqual(receipt['event_count'],4001)
        self.assertGreaterEqual(receipt['last_native_ts'],1002)
        self.assertEqual(Path(self.cfg['icmp_receive_path']).read_bytes(),self.source.read_bytes())

    def test_wrong_token_cannot_trigger_export(self):
        with socket.create_connection(('127.0.0.1', self.cfg['port']), 1) as sock:
            sock.settimeout(1)
            send(sock, dict(run='run', control='ICMP_EXPORT', token='foreign', expected=self.expected, nonce='a'*32))
            with self.assertRaises(ValueError):
                line(sock)
        self.assertIsNone(self.server.log_receiver.pending)
        self.assertFalse(Path(self.cfg['icmp_receive_path']).exists())

    def test_wrong_ping_identity_cannot_publish_a_log(self):
        nonce = 'a'*32
        self.server.log_receiver.request(dict(expected=self.expected, nonce=nonce))
        self.client.ping.snapshot = lambda:dict(log_id='log', pid=99)
        with self.assertRaises(ValueError):
            upload(self.client, nonce)
        self.assertFalse(Path(self.cfg['icmp_receive_path']).exists())

    def test_digest_mismatch_never_publishes_partial_file(self):
        nonce = 'a'*32
        self.server.log_receiver.request(dict(expected=self.expected, nonce=nonce))
        with socket.create_connection(('127.0.0.1', self.cfg['port']), 1) as sock:
            sock.settimeout(1)
            send(sock, dict(self.expected, run='run', control='ICMP_UPLOAD', token='private-token',
                           nonce=nonce, bytes=3, sha256='0'*64))
            self.assertEqual(line(sock)['status'], 'READY')
            sock.sendall(b'bad')
            with self.assertRaises(ValueError):
                line(sock)
        self.assertFalse(Path(self.cfg['icmp_receive_path']).exists())
        self.assertEqual(list((self.root/'ns').glob('*.partial')), [])

    def test_trailing_live_write_is_excluded_without_stopping_source(self):
        self.source.write_bytes(self.original+b'{"event_index":')
        collect(self.extended, timeout=5)
        self.assertEqual(Path(self.cfg['icmp_receive_path']).read_bytes(), self.original)
        self.assertTrue(self.source.read_bytes().endswith(b'{"event_index":'))

    def test_tampered_export_or_request_is_rejected_without_replacement(self):
        collect(self.extended, timeout=5)
        full = Path(self.cfg['icmp_receive_path'])
        full.write_bytes(full.read_bytes()+b'corruption')
        with self.assertRaisesRegex(ValueError, 'ownership/hash'):
            verify_export(self.root, 'run', self.baseline)
        with self.assertRaises(ValueError):
            collect(self.extended, timeout=1)
        self.assertTrue(full.read_bytes().endswith(b'corruption'))

    def test_production_guest_and_endpoint_configs_wire_the_export_module(self):
        from extended_validation import Extended
        owner=Extended.__new__(Extended)
        owner.root=self.root
        owner.cfg=dict(run='run',tcp_interval=.2,tcp_timeout=1,lifetime=30,ns=dict(
            port=self.cfg['port'],new_port=self.cfg['new_port'],control_port=self.cfg['control_port'],
            icmp_enabled=True,icmp_sender_logging_enabled=True,icmp_sender_export_enabled=True))
        owner.state=dict(ns=dict(control_token='private-token'))
        server=owner.ns_config(bind='127.0.0.1',server='external-endpoint',console=False)
        self.assertEqual(server['icmp_receive_path'],str(self.root/'ns/snat-icmp-sender.full.jsonl'))
        guest=owner.ns_config(source='127.0.0.1',target='127.0.0.1',label='snat')
        self.assertTrue(guest['icmp_export_enabled'])
        owner.server=Mock()
        owner.add_ns_guest({},'client',guest)
        paths={f['path'] for f in owner.server.call_args.args[1]['write_files']}
        self.assertIn('/usr/local/bin/ns_log_transfer.py',paths)
        self.assertIn('/usr/local/bin/ns_icmp.py',paths)

    def test_original_file_event_gap_cannot_be_published(self):
        lines=self.original.splitlines(keepends=True)
        self.source.write_bytes(b''.join(lines[:10]+lines[11:]))
        nonce='a'*32
        self.server.log_receiver.request(dict(expected=self.expected,nonce=nonce))
        with self.assertRaises(ValueError):
            upload(self.client,nonce)
        self.assertFalse(Path(self.cfg['icmp_receive_path']).exists())


if __name__ == '__main__':
    unittest.main()
