"""Real observer subprocess reconnects; never restart traffic or hide coverage loss."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from tests.validation.test_validation import ROOT
import migration_diagnostics as d


class ReconnectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.obj = d.Observer(self.root,dict(run='r',host='node',role='core',lifetime=60,
            max_bytes=1048576,reconnect_attempts=2,reconnect_window_seconds=30))
        self.obj.snapshot = lambda label:self.obj.record('test-snapshot',label=label)
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.obj.stop.set()
        for name,row in self.obj.children.items():
            self.obj.stop_job(name,row)
        self.pool.shutdown(wait=True)

    def tick_until(self, check):
        deadline = time.monotonic()+5
        while time.monotonic() < deadline:
            row = self.obj.children.get('openflow-watch',{})
            row['retry_at'] = 0
            self.obj.reconnect_jobs(self.pool)
            if check():return
            time.sleep(.02)
        self.fail('observer did not reach expected reconnect state')

    def test_eof_reconnect_retains_both_streams_initial_snapshot_and_gap(self):
        marker = self.root/'attempt'
        program = ('from pathlib import Path;import sys,time\n'
            'p=Path(sys.argv[1]);retry=p.exists();p.touch()\n'
            'print("NXST_FLOW_MONITOR reply (xid=0x6):",flush=True)\n'
            'print("event=ADDED segment="+str(int(retry)),flush=True)\n'
            'if not retry:print("ovs-ofctl: vconn_recv (End of file)",flush=True);sys.exit(1)\n'
            'while True:time.sleep(.05)\n')
        self.obj.start_job('openflow-watch',dict(reconnect=True,
            args=[sys.executable,'-u','-c',program,str(marker),'watch:!initial']))
        self.tick_until(lambda:len(self.obj.segments['openflow-watch'])==2 and
            self.obj.gaps['openflow-watch'][0]['end_timestamp'] is not None)
        self.obj.reconnect_jobs(self.pool)
        state = self.obj.status('UNAVAILABLE')
        self.assertEqual(state['coverage'],'GAPPED')
        self.assertEqual(state['errors'],['openflow-watch'])
        self.assertEqual(state['jobs']['openflow-watch']['reconnect_attempts'],1)
        self.assertEqual(state['jobs']['openflow-watch']['segments'][0]['rc'],1)
        data = (self.root/'openflow-watch.jsonl').read_text()
        self.assertIn('segment=0',data)
        self.assertIn('segment=1',data)
        events = [json.loads(x) for x in self.obj.events.read_text().splitlines()]
        self.assertEqual([x for x in events if x['event']=='observer-start'][-1]['command'][-1],'watch:')
        self.assertTrue(any(x['event']=='observer-reconnected' for x in events))

    def test_errors_do_not_acknowledge_reconnect_and_attempts_are_bounded(self):
        self.obj.start_job('openflow-watch',dict(reconnect=True,args=[sys.executable,'-c',
            'import sys;print("Error response from daemon: not running");sys.exit(1)','watch:!initial']))
        self.tick_until(lambda:self.obj.children['openflow-watch'].get('reconnect_exhausted'))
        self.assertEqual(len(self.obj.segments['openflow-watch']),3)
        self.assertEqual(self.obj.reconnect_counts['openflow-watch'],2)
        self.assertIsNone(self.obj.gaps['openflow-watch'][0]['end_timestamp'])
        self.assertEqual(len(self.obj.gaps['openflow-watch']),1)

    def test_capture_processes_are_never_reconnect_candidates(self):
        jobs=d.core_jobs(dict(chassis=True,network=True,ns_enabled=True,tunnel_interface='eth1',
            external_interface='ns-ex',endpoint_ip='10.1.0.187',old_port=18180,new_port=18181),self.root)
        self.assertTrue(jobs['openflow-watch']['reconnect'])
        self.assertTrue(jobs['ovsdb-local']['reconnect'])
        for name in ('tunnel-pcap','gateway-pcap','docker-events'):
            self.assertFalse(jobs[name].get('reconnect',False))

    def test_live_but_unacknowledged_reconnect_is_not_recovery(self):
        gap=dict(detected_at=time.time(),detected_mono=time.monotonic(),end_timestamp=None)
        self.obj.gaps['openflow-watch']=[gap]
        self.obj.start_job('openflow-watch',dict(reconnect=True,args=[sys.executable,'-c',
            'import time;time.sleep(30)','watch:!initial']),gap=gap)
        row=self.obj.children['openflow-watch']
        row['started_mono']=time.monotonic()-11
        self.obj.reconnect_jobs(self.pool)
        self.assertIsNotNone(row['process'].poll())
        self.assertIsNone(gap['end_timestamp'])
        self.assertIn('observer-reconnect-ack-timeout',self.obj.events.read_text())

    def test_retire_orphan_uses_unique_token_even_without_pid_ack(self):
        token=uuid.uuid4().hex
        owned=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],
            env=dict(os.environ,MIGRATION_DIAG_TOKEN=token))
        other=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
        wrapper=subprocess.Popen([sys.executable,'-c','pass']);wrapper.wait()
        reader=threading.Thread(target=lambda:None);reader.start();reader.join()
        row=dict(process=wrapper,spec={'container':'test-only'},token=token,inner_pid=None,thread=reader)
        def run(args,**kw):
            # This executor mounts host /proc inside a child PID namespace. Docker
            # normally provides a matching mount; adapt only the fixture's signals.
            shim='''kill() {
 sig="$1"; shift
 for proc in "$@"; do
  mapped=$(awk '/^NSpid:/ {print $NF}' "/proc/$proc/status")
  command kill "$sig" "${mapped:-$proc}"
 done
}
'''
            shell=args[3:]
            shell[2]=shim+shell[2]
            result=subprocess.run(shell,capture_output=True,text=True,timeout=5)
            return dict(rc=result.returncode,stdout=result.stdout,stderr=result.stderr)
        try:
            with patch.object(d,'command',side_effect=run):
                self.assertTrue(self.obj.stop_job('test-only',row,verify=True))
            owned.wait(timeout=3)
            self.assertIsNone(other.poll())
        finally:
            for proc in (owned,other):
                if proc.poll() is None:proc.terminate()
                proc.wait(timeout=3)


if __name__=='__main__':unittest.main()
