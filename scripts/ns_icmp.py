#!/usr/bin/env python3
"""One persistent, source-bound ping; raw PCAP remains authoritative for loss."""
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import uuid

PREFIX = 'OVN_NS_ICMP_JSON '


class Ping:
    def __init__(self, cfg, publish=None):
        self.cfg = cfg
        self.publish = publish
        self.writer = None
        self.boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        self.log_id = str(uuid.uuid4())
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.process = None
        self.value = dict(alive=False, replies=0, last_reply_mono=None,
                          started_ts=None, started_mono=None, error=None, log_id=self.log_id,
                          last_event_index=0, log_error=None, log_bytes=0,
                          outstanding_events=0, error_events=0)

    def event(self, kind, **fields):
        with self.lock:
            self.value['last_event_index'] += 1
            row = dict(schema_version=1, run=self.cfg['run'], label=self.cfg['label'], boot=self.boot,
                log_id=self.log_id, ping_pid=self.value.get('pid'), event_index=self.value['last_event_index'],
                observed_ts=time.time(), observed_mono=time.monotonic(), kind=kind, **fields)
        data = json.dumps(row, sort_keys=True)+'\n'
        try:
            if self.writer:
                if self.value['log_bytes']+len(data.encode()) > self.cfg.get('icmp_log_max_bytes', 67108864):
                    raise ValueError('ICMP sender log byte budget exhausted; original ping continues')
                self.writer.write(data)
                self.writer.flush()
                self.value['log_bytes'] += len(data.encode())
        except (OSError, ValueError) as exc:
            self.value['log_error'] = str(exc)
        if self.publish:
            try:
                self.publish(row)
            except Exception as exc:
                self.value['log_error'] = 'ICMP console mirror: '+str(exc)

    def output(self, line):
        native = re.match(r'^\[(\d+\.\d+)\]\s*', line)
        seq = re.search(r'icmp_seq[= ](\d+)', line)
        kind = ('reply' if re.search(r'bytes from .*icmp_seq=\d+', line) else
                'outstanding' if 'no answer yet for icmp_seq=' in line else
                'error' if re.search(r'unreachable|sendmsg|not permitted|ping:', line, re.I) else 'output')
        with self.lock:
            if kind == 'reply':
                self.value['replies'] += 1
                self.value['last_reply_mono'] = time.monotonic()
            elif kind == 'outstanding':
                self.value['outstanding_events'] += 1
            elif kind == 'error':
                self.value['error_events'] += 1
                self.value['last_error'] = line[:8192]
        self.event(kind, native_ts=float(native[1]) if native else None,
            icmp_sequence=int(seq[1]) if seq else None, text=line[:8192].rstrip())

    def run(self):
        try:
            try:
                path = Path(self.cfg['icmp_output'])
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                self.writer = path.open('x')
                path.chmod(0o600)
                self.value['log_path'] = str(path)
            except (OSError, KeyError) as exc:
                self.value['log_error'] = str(exc)
            # -D is source wall time; payload/cadence match the EW packet test.
            self.process = subprocess.Popen(
                ['ping', '-n', '-D', '-O', '-I', self.cfg['source'], '-s', '56',
                 '-i', str(self.cfg.get('icmp_interval', .2)), self.cfg['target']],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                env=dict(os.environ, LC_ALL='C'))
            with self.lock:
                self.value.update(alive=True, pid=self.process.pid,
                                  started_ts=time.time(), started_mono=time.monotonic())
            self.event('start', source=self.cfg['source'], target=self.cfg['target'],
                       interval_seconds=self.cfg.get('icmp_interval', .2))
            for line in self.process.stdout:
                if self.stop.is_set():
                    break
                self.output(line)
                if 'Operation not permitted' in line or 'Network is unreachable' in line:
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
            self.event('end', returncode=self.process.poll() if self.process else None)
            if self.writer:
                self.writer.close()

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
