"""Evidence survives failed cleanup; successful archival retains one PCAP/bundle."""
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests.validation.test_validation import ROOT
from diagnostics_control import prune_archive, transport
from ns_diagnostics import Diagnostics, save
from report_format import terminal_summary


class EvidenceStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'run-one'
        self.root.mkdir()

    def bundle(self, payload=b'original events'):
        entry = dict(host='compute1', role='core')
        archive = self.root/'diagnostics/compute1-core.tar.gz'
        archive.parent.mkdir(exist_ok=True)
        with tarfile.open(archive, 'w:gz') as bundle:
            member = tarfile.TarInfo('./events.jsonl')
            member.size = len(payload)
            bundle.addfile(member, io.BytesIO(payload))
        dest = self.root/'diagnostics/compute1/core'
        dest.mkdir(parents=True)
        return entry, archive, dest

    def test_tar_removed_only_after_every_file_matches(self):
        entry, archive, dest = self.bundle()
        (dest/'events.jsonl').write_bytes(b'original events')
        prune_archive(self.root, entry)
        self.assertFalse(archive.exists())
        self.assertEqual((dest/'events.jsonl').read_bytes(), b'original events')
        prune_archive(self.root, entry)  # Idempotent stop/rebuild.

    def test_mismatched_or_missing_extraction_keeps_original_tar(self):
        entry, archive, dest = self.bundle()
        original = archive.read_bytes()
        for payload in (None, b'damaged extraction'):
            with self.subTest(payload=payload):
                if payload is not None:
                    (dest/'events.jsonl').write_bytes(payload)
                with self.assertRaises(RuntimeError):
                    prune_archive(self.root, entry)
                self.assertEqual(archive.read_bytes(), original)

    def test_corrupt_transport_tar_is_retained(self):
        entry, archive, _ = self.bundle()
        archive.write_bytes(b'incomplete download')
        save(self.root/'runtime.json', dict(inventory='/inventory'))
        with patch('diagnostics_control.subprocess.run', return_value=SimpleNamespace(
                returncode=0, stdout='', stderr='')):
            with self.assertRaises(tarfile.ReadError):
                transport(self.root, 'stop', {'compute1': entry}, 'test')
        self.assertEqual(archive.read_bytes(), b'incomplete download')

    def endpoint(self, legacy=True):
        d = Diagnostics(SimpleNamespace(root=self.root, cfg=dict(run='run-one')))
        source = self.root/'ns-diagnostics'/('endpoint-agent' if legacy else 'endpoint')
        source.mkdir(parents=True)
        cp = dict(kind='endpoint', host='localhost', config=dict(directory=str(source)))
        state = dict(config=cp['config'], status='STOPPED')
        for name in ('tcp.pcap', 'tcpdump.log', 'supervisor.log'):
            (source/name).write_bytes(b'original '+name.encode())
        save(source/'tcp-capture-state.json', state)
        return d, cp, state, source

    def test_legacy_endpoint_moves_once_and_repeat_stop_works(self):
        d, cp, state, source = self.endpoint()
        for _ in range(2):
            d.archive_endpoint(cp, state)
            self.assertFalse((source/'tcp.pcap').exists())
            self.assertEqual((self.root/cp['pcap_path']).read_bytes(), b'original tcp.pcap')
            self.assertEqual(json.loads((source/'tcp-capture-state.json').read_text()), state)
        self.assertEqual(len(list(self.root.rglob('tcp.pcap'))), 1)

    def test_identical_existing_endpoint_copy_is_removed_by_hash(self):
        d, cp, state, source = self.endpoint()
        dest = self.root/'ns-diagnostics/endpoint'
        dest.mkdir()
        for old in source.iterdir():
            (dest/old.name).write_bytes(old.read_bytes())
        d.archive_endpoint(cp, state)
        self.assertFalse((source/'tcp.pcap').exists())
        self.assertEqual((dest/'tcp.pcap').read_bytes(), b'original tcp.pcap')

    def test_conflict_keeps_both_files_and_does_not_move_any_evidence(self):
        d, cp, state, source = self.endpoint()
        dest = self.root/'ns-diagnostics/endpoint'
        dest.mkdir()
        (dest/'supervisor.log').write_bytes(b'different log')
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with self.assertRaisesRegex(RuntimeError, 'content mismatch'):
            d.archive_endpoint(cp, state)
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_live_or_changed_endpoint_cannot_be_archived(self):
        d, cp, state, source = self.endpoint()
        with self.assertRaisesRegex(RuntimeError, 'must be stopped'):
            d.archive_endpoint(cp, dict(state, status='RUNNING'))
        cp['remote'] = dict(supervisor={'pid': 123})
        with self.assertRaisesRegex(RuntimeError, 'identity changed'):
            d.archive_endpoint(cp, state)
        self.assertTrue((source/'tcp.pcap').exists())

    def test_canonical_endpoint_needs_no_copy(self):
        d, cp, state, source = self.endpoint(legacy=False)
        before = (source/'tcp.pcap').stat().st_ino
        d.archive_endpoint(cp, state)
        self.assertEqual((self.root/cp['pcap_path']).stat().st_ino, before)
        self.assertEqual(len(list(self.root.rglob('tcp.pcap'))), 1)

    def test_terminal_does_not_publish_result_but_preserves_report_data(self):
        for result in ('SUCCESS', 'SUCCESS_WITH_REMEDIATION', 'MIGRATED_VALIDATION_INCOMPLETE'):
            with self.subTest(result=result):
                report = dict(result=result, run_id='run-one')
                summary = terminal_summary(report, self.root)
                self.assertNotIn(result, summary)
                self.assertEqual(report['result'], result)
                self.assertEqual(len(summary.splitlines()), 3)


class LocalEndpointTransportTests(unittest.TestCase):
    def test_real_ansible_stopped_capture_uses_one_canonical_pcap_and_separate_transport(self):
        self.capture(legacy=False)

    def test_real_ansible_legacy_checkpoint_archives_and_stops_again(self):
        self.capture(legacy=True)

    def test_new_capture_selects_canonical_endpoint_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cfg = self.config(root, '/inventory')
            d = Diagnostics(SimpleNamespace(root=root, cfg=cfg))
            with patch.object(d, 'transport', return_value=dict(status='RUNNING')):
                d.start('endpoint')
            cp = d.state['captures']['endpoint']
            self.assertEqual(cp['config']['directory'], str(root/'ns-diagnostics/endpoint'))
            self.assertEqual(cp['pcap_path'], 'ns-diagnostics/endpoint/tcp.pcap')

    @staticmethod
    def config(root, inventory):
        return dict(run='run-one', inventory=str(inventory), lifetime=60, timeout=60,
            ns=dict(enabled=True, diagnostics_enabled=True, capture_interface='eth1',
                endpoint_ip='10.1.0.187', snat_ip='10.1.0.4', fip_ip='10.1.0.5',
                outbound_ip='10.233.0.11', port=18180))

    def capture(self, *, legacy):
        # Exercise the production Ansible stage/inspect/stop/archive transport.
        # Packet creation/lifecycle is unchanged and is covered separately by
        # the supervisor tests; this fixture seeds its completed raw evidence.
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)/'run-one'
            root.mkdir()
            inventory = root/'inventory.ini'
            inventory.write_text('localhost ansible_connection=local ansible_python_interpreter='+sys.executable+'\n')
            cfg = self.config(root, inventory)
            ns = cfg['ns']
            source = root/'ns-diagnostics'/('endpoint-agent' if legacy else 'endpoint')
            config = dict(run=cfg['run'], kind='endpoint', endpoint_ip=ns['endpoint_ip'],
                snat_ip=ns['snat_ip'], fip_ip=ns['fip_ip'], guest_ip=ns['outbound_ip'],
                tcp_port=ns['port'], lifetime=cfg['lifetime'], allow_create=True,
                interface=ns['capture_interface'], directory=str(source))
            cp = dict(kind='endpoint', host='localhost', status='START_INTENT', config=config)
            d = Diagnostics(SimpleNamespace(root=root, cfg=cfg))
            # Control files must not contaminate agent_inspect/agent_start's
            # evidence directory, even though source and archive now coincide.
            inspected = d.transport('inspect', cp)
            self.assertEqual(inspected['status'], 'NOT_STARTED')
            state = dict(config=config, status='STOPPED', path=str(source/'tcp.pcap'),
                interface='eth1', started_at=100, dropped_packets=0, captured_packets=5)
            for name in ('tcp.pcap', 'tcpdump.log', 'supervisor.log'):
                (source/name).write_bytes(b'original '+name.encode())
            save(source/'tcp-capture-state.json', state)
            cp.update(status='STOPPED', remote=state)
            for _ in range(2):
                stopped = d.transport('stop', cp)
                self.assertEqual(stopped, state)
            self.assertEqual(cp['pcap_path'], 'ns-diagnostics/endpoint/tcp.pcap')
            self.assertEqual(len(list(root.rglob('tcp.pcap'))), 1)
            self.assertEqual((root/cp['pcap_path']).read_bytes(), b'original tcp.pcap')
            if legacy:
                self.assertTrue((source/'tcp-capture-state.json').exists())
                self.assertFalse((source/'tcp.pcap').exists())
            else:
                self.assertFalse((root/'ns-diagnostics/endpoint-agent').exists())
            self.assertTrue((root/'ns-diagnostics/transport/endpoint/operation.json').exists())


if __name__ == '__main__':
    unittest.main()
