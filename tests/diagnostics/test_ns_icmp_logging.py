"""Raw sender output, persistent ping identity and loss-aware console mirroring."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from tests.validation.test_validation import ROOT
from ns_icmp import Ping,PREFIX
from ns_icmp_logging import mirror_console,sender_evidence


class SenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.cfg=dict(run='r',label='fip',source='10.1.0.187',target='10.1.0.5',icmp_interval=.2,
            icmp_output=str(self.root/'sender.jsonl'))

    def execute(self,lines,**changes):
        cfg=dict(self.cfg,**changes)
        process=Mock(pid=1234,stdout=iter(lines));process.poll.return_value=0
        published=[];ping=Ping(cfg,publish=published.append)
        with patch('ns_icmp.subprocess.Popen',return_value=process) as launch:
            ping.run()
        self.assertEqual(launch.call_count,1)
        self.assertEqual(launch.call_args.kwargs['env']['LC_ALL'],'C')
        return ping,published

    def test_original_ping_records_native_clocks_sequences_errors_and_end(self):
        ping,events=self.execute(['PING 10.1.0.5\n',
            '[100.100001] 64 bytes from 10.1.0.5: icmp_seq=1 ttl=63 time=1 ms\n',
            '[100.300001] no answer yet for icmp_seq=2\n',
            '[100.500001] From 10.1.0.187 icmp_seq=3 Destination Host Unreachable\n',
            'ping: sendmsg: No route to host\n'])
        rows=[json.loads(x) for x in Path(self.cfg['icmp_output']).read_text().splitlines()]
        self.assertEqual(rows,events)
        self.assertEqual([r['event_index'] for r in rows],list(range(1,len(rows)+1)))
        self.assertEqual([r['kind'] for r in rows],['start','output','reply','outstanding','error','error','end'])
        self.assertEqual(rows[2]['native_ts'],100.100001)
        self.assertEqual(rows[4]['icmp_sequence'],3)
        self.assertEqual(ping.snapshot()['replies'],1)
        self.assertEqual(ping.snapshot()['error_events'],2)
        self.assertEqual({r['ping_pid'] for r in rows},{1234})

    def test_log_failure_does_not_restart_or_stop_traffic_and_never_overwrites(self):
        path=Path(self.cfg['icmp_output']);path.write_bytes(b'original evidence')
        ping,_=self.execute(['[100.1] 64 bytes from 10.1.0.5: icmp_seq=1 ttl=63 time=1 ms\n'])
        self.assertEqual(path.read_bytes(),b'original evidence')
        self.assertTrue(ping.snapshot()['log_error'])
        self.assertEqual(ping.snapshot()['replies'],1)

    def test_byte_limit_keeps_early_evidence_and_ping_continues(self):
        ping,_=self.execute(['[100.1] 64 bytes from 10.1.0.5: icmp_seq=1 ttl=63 time=1 ms\n']*8,
            icmp_log_max_bytes=700)
        self.assertLessEqual(Path(self.cfg['icmp_output']).stat().st_size,700)
        self.assertIn('budget exhausted',ping.snapshot()['log_error'])
        self.assertEqual(ping.snapshot()['replies'],8)


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.rows=[dict(schema_version=1,run='r',label='snat',boot='boot',log_id='log',ping_pid=42,
            event_index=i,observed_ts=float(i),observed_mono=float(i),native_ts=float(i),
            kind='outstanding' if i in (3,4) else 'reply',icmp_sequence=i,text='native output') for i in range(1,8)]
        self.base=dict(boot='boot',icmp=dict(log_id='log',pid=42))

    def mirror(self,rows):
        mirror_console(self.root,'\n'.join(PREFIX+json.dumps(r) for r in rows),'r')

    def summary(self):
        return sender_evidence(self.root/'ns/snat-icmp-sender.jsonl','r','snat',self.base,2.5,6.5)

    def test_overlap_is_deduplicated_and_all_in_window_events_remain(self):
        self.mirror(self.rows[:5]);self.mirror(self.rows[2:]);self.mirror(self.rows)
        self.assertEqual(len((self.root/'ns/snat-icmp-sender.jsonl').read_text().splitlines()),7)
        self.assertEqual(self.summary()['status'],'PASS')
        self.assertEqual(self.summary()['outstanding_events'],2)

    def test_truncated_console_or_conflicting_event_never_claims_complete(self):
        self.mirror([r for r in self.rows if r['event_index']!=4])
        self.assertEqual(self.summary()['missing_event_count'],1)
        self.assertEqual(self.summary()['status'],'UNAVAILABLE')
        self.mirror([dict(self.rows[2],text='conflict')])
        self.assertIn('Conflicting',self.summary()['reason'])

    def test_wrong_pid_clock_or_window_is_unavailable(self):
        self.mirror([dict(r,ping_pid=43) for r in self.rows])
        self.assertIn('PID',self.summary()['reason'])
        (self.root/'ns/snat-icmp-sender.jsonl').unlink()
        self.mirror(self.rows[:4])
        self.assertIn('window',self.summary()['reason'])


if __name__=='__main__':unittest.main()
