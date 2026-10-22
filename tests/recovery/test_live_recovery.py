"""Recovery coordination consumes fresh original evidence and never rebases traffic."""
import json
from pathlib import Path
import signal
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests.validation.test_validation import ROOT, packet
import live_recovery as r
import workload_validation as w


class RecoveryTests(unittest.TestCase):
    def test_static_success_is_not_fresh_recovery_and_failure_must_close(self):
        fence={}
        self.assertFalse(r.evaluate({'flow':dict(cursor=5,ready=True)},fence)['flow']['ready'])
        self.assertFalse(r.evaluate({'flow':dict(cursor=5,ready=True)},fence)['flow']['ready'])
        self.assertFalse(r.evaluate({'flow':dict(cursor=6,ready=False)},fence)['flow']['ready'])
        self.assertTrue(r.evaluate({'flow':dict(cursor=7,ready=True)},fence)['flow']['ready'])
        self.assertEqual(fence,{'flow':5})
        self.assertFalse(r.stream_ready(dict(consecutive_successes=20,open_failure_since=1)))
        self.assertFalse(r.stream_ready(dict(consecutive_successes=20,held_broken=True)))

    def test_wait_preserves_original_anchor_and_requires_matrix_advance(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); anchor=root/'validation-window.json'; anchor.write_text('{"pcap_start":{"index":123}}')
            class Reader:
                tick=0
                matrix_tick=0
                def fast_points(self,deadline):
                    self.tick+=1
                    return {'held':dict(cursor=self.tick,ready=self.tick>=3)}
                def matrix_points(self):
                    self.matrix_tick+=1
                    return {'matrix':dict(cursor=self.matrix_tick,ready=True)}
            reader=Reader(); result=r.wait(root,reader,timeout=1,poll=.001)
            self.assertEqual(result['status'],'PASS'); self.assertGreaterEqual(reader.matrix_tick,2)
            self.assertEqual(anchor.read_text(),'{"pcap_start":{"index":123}}')
            self.assertFalse(result['probe_restart']); self.assertFalse(result['resource_creation'])
            self.assertEqual(len(list((root/'post-restore-observations').glob('*.json'))),1)

    def test_timeout_and_reader_initialization_error_leave_truthful_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            reader=SimpleNamespace(fast_points=lambda _: {'fip':dict(cursor=1,ready=True)})
            with self.assertRaises(TimeoutError): r.wait(root,reader,timeout=.01,poll=.001)
            self.assertEqual(json.loads((root/'post-restore-traffic.json').read_text())['status'],'UNAVAILABLE')
            def unavailable(): raise RuntimeError('SDK initialization unavailable')
            with self.assertRaises(RuntimeError): r.wait(root,unavailable,timeout=1)
            self.assertIn('SDK initialization',json.loads((root/'post-restore-traffic.json').read_text())['reason'])

    def test_global_deadline_interrupts_blocked_reader_and_persists_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            def blocked(_): time.sleep(10)
            def alarm(*_): raise r.RecoveryDeadline('bounded RPC observation')
            old=signal.signal(signal.SIGALRM,alarm)
            start=time.monotonic()
            try:
                signal.setitimer(signal.ITIMER_REAL,.05)
                with self.assertRaises(r.RecoveryDeadline):
                    r.wait(root,SimpleNamespace(fast_points=blocked),timeout=1)
            finally:
                signal.setitimer(signal.ITIMER_REAL,0); signal.signal(signal.SIGALRM,old)
            self.assertLess(time.monotonic()-start,1)
            self.assertEqual(json.loads((root/'post-restore-traffic.json').read_text())['status'],'UNAVAILABLE')

    def reader(self):
        obj=r.Reader.__new__(r.Reader)
        obj.cfg={'run':'original','ns':{'enabled':True,'new_port':18181,'icmp_enabled':True}}
        obj.measure={'0':{'boot':'one'},'1':{'boot':'one'}}
        obj.v=SimpleNamespace(collect=lambda *a,**k: {key:[packet(i) for i in [10,12,13,14,11]] for key in ('0','1')})
        obj.d=SimpleNamespace(enabled=False)
        held=dict(consecutive_successes=10,open_failure_since=None,held_broken=False,
            first_session='original-socket',last_session='original-socket',connection_attempts=1,last_success_mono=20)
        obj.ns={name:dict(boot='old-boot',probe_id='old-probe',streams={'held':held},icmp={'pid':123,'log_id':'ping-log'})
            for name in ('snat','fip')}
        obj.rows={name:dict(run='original',boot='old-boot',probe_id='old-probe',armed=True,mono=21,
            streams={s:dict(held) for s in ('held','new_old','new_listener')},
            icmp=dict(pid=123,log_id='ping-log',alive=True,last_reply_mono=20.5)) for name in ('snat','fip')}
        obj.ext=SimpleNamespace(ns=True,ns_rows=lambda:obj.rows)
        return obj

    def test_uses_highest_completed_sequence_and_original_ns_sessions(self):
        obj=self.reader(); points=obj.fast_points(time.monotonic()+1)
        self.assertAlmostEqual(points['ew-icmp/0']['cursor'],2.8)
        self.assertTrue(all(x['ready'] for x in points.values()))
        obj.rows['fip']['streams']['held']['last_session']='replacement'
        with self.assertRaisesRegex(ValueError,'held session changed'): obj.fast_points(time.monotonic()+1)

    def test_ping_restart_and_guest_reboot_are_rejected(self):
        obj=self.reader(); obj.rows['snat']['icmp']['pid']=456
        with self.assertRaisesRegex(ValueError,'ping identity changed'): obj.fast_points(time.monotonic()+1)
        obj=self.reader(); obj.rows['fip']['boot']='new-boot'
        with self.assertRaisesRegex(ValueError,'client identity changed'): obj.fast_points(time.monotonic()+1)

    def test_original_ew_tcp_session_is_required_for_all_three_streams(self):
        obj=self.reader(); obj.ext=None
        obj.tcp=dict(boots={'0':'original-client','1':'original-server'},session_id='same-socket')
        current={'0':dict(boot='original-client',session_id='same-socket',held_established=True,armed=True,
            streams={s:dict(consecutive_successes=5,last_success_mono=42,open_failure_since=None)
                     for s in ('held','new_old','new_listener')}),'1':dict(boot='original-server')}
        obj.d=SimpleNamespace(enabled=True,latest=lambda _:current)
        points=obj.fast_points(time.monotonic()+1)
        self.assertTrue(all(points['ew-tcp/'+s]['ready'] for s in ('held','new_old','new_listener')))
        current['0']['session_id']='replacement'
        with self.assertRaisesRegex(ValueError,'EW TCP held session changed'): obj.fast_points(time.monotonic()+1)

    def test_legacy_ns_stream_names_are_read_without_creating_a_new_listener(self):
        obj=self.reader(); obj.cfg['ns'].pop('new_port')
        for row in obj.rows.values():
            row['armed']=False
            row['streams']['new']=row['streams'].pop('new_old'); row['streams'].pop('new_listener')
        points=obj.fast_points(time.monotonic()+1)
        self.assertTrue(points['fip/new']['ready'])
        self.assertNotIn('fip/new_listener',points)

    def test_pair_a_check_is_read_only_and_refuses_absent_original_capture(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); path=root/'validation-window.json'; path.write_text('{"pcap_start":{"index":123}}')
            v=w.Validation.__new__(w.Validation); v.root=root; v.cfg={'capture_enabled':True}
            capture=Mock(); capture.checkpoint.return_value={'status':'RUNNING'}
            capture.snapshot.return_value={'status':'RUNNING','observed_endpoint':{'index':130}}
            with patch.object(w,'Capture',return_value=capture):
                v.checkpoint_check()
                capture.start.assert_not_called(); capture.anchor.assert_not_called()
                self.assertEqual(path.read_text(),'{"pcap_start":{"index":123}}')
                capture.checkpoint.return_value={}
                with self.assertRaisesRegex(RuntimeError,'rebase prohibited'): v.checkpoint_check()


if __name__=='__main__': unittest.main()
