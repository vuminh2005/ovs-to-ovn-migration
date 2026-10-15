#!/usr/bin/env python3
"""One persistent, source-bound ping; raw PCAP remains authoritative for loss."""
import re
import subprocess
import threading
import time


class Ping:
    def __init__(self, cfg):
        self.cfg = cfg
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.process = None
        self.value = dict(alive=False, replies=0, last_reply_mono=None,
                          started_ts=None, started_mono=None, error=None)

    def run(self):
        try:
            # -D is source wall time; payload/cadence match the EW packet test.
            self.process = subprocess.Popen(
                ['ping', '-n', '-D', '-O', '-I', self.cfg['source'], '-s', '56',
                 '-i', str(self.cfg.get('icmp_interval', .2)), self.cfg['target']],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            with self.lock:
                self.value.update(alive=True, pid=self.process.pid,
                                  started_ts=time.time(), started_mono=time.monotonic())
            for line in self.process.stdout:
                if self.stop.is_set():
                    break
                if re.search(r'bytes from .*icmp_seq=\d+', line):
                    with self.lock:
                        self.value['replies'] += 1
                        self.value['last_reply_mono'] = time.monotonic()
                elif 'Operation not permitted' in line or 'Network is unreachable' in line:
                    with self.lock:
                        self.value['error'] = line.strip()
            if not self.stop.is_set():
                with self.lock:
                    self.value['error'] = 'Original ping ended; restart prohibited'
        except (OSError, ValueError) as exc:
            with self.lock:
                self.value['error'] = str(exc)
        finally:
            self.close()

    def close(self):
        self.stop.set()
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        with self.lock:
            self.value['alive'] = False

    def snapshot(self):
        with self.lock:
            return dict(self.value)
