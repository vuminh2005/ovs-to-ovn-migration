"""Authenticated post-capture export of the original guest ICMP sender log.

The existing SNAT echo server requests a one-off export in an echo response.
The guest opens a separate outbound socket; held sockets and ping never restart.
No remote path is accepted. A fixed-size JSONL prefix is hashed and published
only after identity/sequence checks. The console mirror remains separate.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import stat
import threading
import time
import uuid

MAX_BYTES = 67108864


def send(sock, row):
    sock.sendall((json.dumps(row, sort_keys=True)+'\n').encode())


def line(sock):
    data = bytearray()
    while not data.endswith(b'\n'):
        part = sock.recv(1)
        if not part or len(data) >= 65536:
            raise ValueError('Missing/oversized export acknowledgement')
        data.extend(part)
    return json.loads(data)


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(65536), b''):
            value.update(chunk)
    return value.hexdigest()


def inspect_log(path, expected):
    previous = 0
    last = None
    count = 0
    native = None
    with Path(path).open() as stream:
        for text in stream:
            if not text.endswith('\n'):
                raise ValueError('Incomplete sender log line')
            row = json.loads(text)
            if any(row.get(k) != expected[k] for k in ('run', 'label', 'boot', 'log_id', 'ping_pid')):
                raise ValueError('Exported sender identity changed')
            if type(row.get('event_index')) is not int or row['event_index'] != previous+1:
                raise ValueError('Exported sender event gap/duplicate')
            if any(type(row.get(k)) not in (int, float) or not math.isfinite(row[k])
                   for k in ('observed_ts', 'observed_mono')):
                raise ValueError('Invalid exported sender clocks')
            if last and row['observed_mono'] <= last['observed_mono']:
                raise ValueError('Exported sender monotonic clock changed')
            if count == 0 and row.get('kind') != 'start':
                raise ValueError('Export must start at the original ping start')
            previous, last, count = row['event_index'], row, count+1
            if type(row.get('native_ts')) in (int, float) and math.isfinite(row['native_ts']):
                native = row['native_ts']
    if not last:
        raise ValueError('Empty exported sender log')
    return dict(event_count=count, last_event_index=previous, last_observed_ts=last['observed_ts'], last_native_ts=native)


class Receiver:
    def __init__(self, cfg):
        self.cfg = cfg
        self.path = Path(cfg['icmp_receive_path'])
        self.receipt = self.path.with_suffix('.receipt.json')
        self.pending = None
        self.lock = threading.Lock()

    def request(self, row):
        expected = row['expected']
        if (not isinstance(expected, dict) or expected.get('run') != self.cfg['run'] or expected.get('label') != 'snat' or
            any(not expected.get(k) for k in ('boot', 'log_id', 'ping_pid', 'probe_id')) or
            not isinstance(row.get('nonce'), str) or len(row['nonce']) != 32):
            raise ValueError('Invalid export ownership request')
        with self.lock:
            if self.path.exists() or self.receipt.exists():
                raise ValueError('Sender export already exists; replacement prohibited')
            pending = dict(nonce=row['nonce'], expected=expected,
                           not_before_timestamp=row.get('not_before_timestamp'))
            cutoff = pending['not_before_timestamp']
            if cutoff is not None and (type(cutoff) not in (int, float) or not math.isfinite(cutoff) or cutoff <= 0):
                raise ValueError('Invalid sender export time fence')
            if self.pending and self.pending != pending:
                raise ValueError('Export request changed')
            self.pending = pending
        return dict(status='PASS', run=self.cfg['run'], nonce=row['nonce'])

    def instruction(self):
        pending = self.pending
        return {k:pending[k] for k in ('nonce', 'not_before_timestamp')} if pending else None

    def receive(self, sock, row):
        if not self.lock.acquire(blocking=False):
            raise ValueError('Sender export already in progress')
        temporary = self.path.with_name(self.path.name+'.'+uuid.uuid4().hex+'.partial')
        try:
            request = self.pending
            if (not request or row.get('nonce') != request['nonce'] or
                any(row.get(k) != v for k, v in request['expected'].items()) or
                type(row.get('bytes')) is not int or not 0 < row['bytes'] <= MAX_BYTES or
                not isinstance(row.get('sha256'), str) or len(row['sha256']) != 64):
                raise ValueError('Sender upload identity/size mismatch')
            if self.path.exists() or self.receipt.exists():
                raise ValueError('Sender export already published')
            self.path.parent.mkdir(parents=True, exist_ok=True)
            sock.settimeout(10)
            send(sock, dict(status='READY', nonce=request['nonce']))
            value, remaining = hashlib.sha256(), row['bytes']
            deadline = time.monotonic()+60
            with temporary.open('xb') as stream:
                temporary.chmod(0o600)
                while remaining:
                    sock.settimeout(min(10, max(.001, deadline-time.monotonic())))
                    chunk = sock.recv(min(65536, remaining))
                    if not chunk or time.monotonic() > deadline:
                        raise ValueError('Incomplete/timed-out sender upload')
                    stream.write(chunk)
                    value.update(chunk)
                    remaining -= len(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if value.hexdigest() != row['sha256']:
                raise ValueError('Sender export hash mismatch')
            inspected = inspect_log(temporary, request['expected'])
            if request.get('not_before_timestamp') is not None and (
                inspected['last_native_ts'] is None or inspected['last_native_ts'] < request['not_before_timestamp']):
                raise ValueError('Exported sender log does not cross the capture time fence')
            temporary.replace(self.path)
            receipt = dict(status='PASS', nonce=request['nonce'], expected=request['expected'],
                           bytes=row['bytes'], sha256=row['sha256'], received_at=time.time(), **inspected)
            from dataplane_capture import save
            save(self.receipt, receipt)
            self.pending = None
            send(sock, dict(status='PASS', run=self.cfg['run'], nonce=request['nonce']))
        finally:
            temporary.unlink(missing_ok=True)
            self.lock.release()


def upload(client, nonce, *, not_before=None):
    """Snapshot the append-only file; never stop/restart ping to export it."""
    cfg = client.cfg
    ping = client.ping.snapshot()
    path = Path(cfg['icmp_output'])
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        deadline = time.monotonic()+60
        while True:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_BYTES:
                raise ValueError('Invalid sender export source')
            size = info.st_size
            # Snapshot only complete lines of the original append-only file.
            stream.seek(max(0, size-16384))
            tail = stream.read(size-stream.tell())
            newline = tail.rfind(b'\n')
            if newline < 0:
                raise ValueError('No complete sender log tail')
            size = size-len(tail)+newline+1
            last = json.loads(tail[:newline].rsplit(b'\n', 1)[-1])
            native = last.get('native_ts')
            if not_before is None or (type(native) in (int, float) and math.isfinite(native) and native >= not_before):
                break
            if client.stop.is_set() or time.monotonic() >= deadline:
                raise TimeoutError('Original sender file has not crossed the capture time fence')
            client.stop.wait(.2)
        stream.seek(0)
        value, remaining = hashlib.sha256(), size
        while remaining:
            chunk = stream.read(min(65536, remaining))
            if not chunk:
                raise ValueError('Sender file truncated')
            value.update(chunk)
            remaining -= len(chunk)
        header = dict(run=cfg['run'], label='snat', boot=client.ping.boot,
            log_id=ping['log_id'], ping_pid=ping['pid'], probe_id=client.probe_id,
            control='ICMP_UPLOAD', token=cfg['control_token'], nonce=nonce,
            bytes=size, sha256=value.hexdigest())
        with socket.socket() as sock:
            sock.settimeout(10)
            sock.bind((cfg['source'], 0))
            sock.connect((cfg['target'], cfg['port']))
            send(sock, header)
            ack = line(sock)
            if ack != dict(status='READY', nonce=nonce):
                raise ValueError('Sender upload refused: '+str(ack))
            stream.seek(0)
            remaining = size
            while remaining:
                chunk = stream.read(min(65536, remaining))
                if not chunk:
                    raise ValueError('Sender file changed during upload')
                sock.sendall(chunk)
                remaining -= len(chunk)
            ack = line(sock)
            if ack != dict(status='PASS', run=cfg['run'], nonce=nonce):
                raise ValueError('Sender upload not committed')


def collect(extended, *, timeout=90):
    """Called only after all original N-S captures have stopped and fetched."""
    root, cfg = extended.root, extended.cfg
    state = json.loads((root/'ns-diagnostics.json').read_text())
    if (set(state.get('captures', {})) != {'endpoint', 'guest-snat', 'guest-fip'} or
        any(c.get('remote', {}).get('status') != 'STOPPED' for c in state['captures'].values())):
        raise ValueError('Cannot export sender log before captures stop')
    baseline = json.loads((root/'ns-baseline.json').read_text())['clients']['snat']
    expected = dict(run=cfg['run'], label='snat', boot=baseline['boot'],
        log_id=baseline['icmp']['log_id'], ping_pid=baseline['icmp']['pid'], probe_id=baseline['probe_id'])
    path = root/'ns/snat-icmp-sender.full.jsonl'
    receipt_path = path.with_suffix('.receipt.json')
    if not receipt_path.exists():
        request_path = root/'ns/snat-icmp-export-request.json'
        if request_path.exists():
            request = json.loads(request_path.read_text())
            if request.get('expected') != expected:
                raise ValueError('Sender export checkpoint changed')
        else:
            request = dict(run=cfg['run'], token=extended.state['ns']['control_token'],
                control='ICMP_EXPORT', expected=expected, nonce=uuid.uuid4().hex,
                not_before_timestamp=(state['captures']['guest-snat']['remote']['stopped_at']+2
                                      if state['captures']['guest-snat']['remote'].get('stopped_at') else None))
            from dataplane_capture import save
            save(request_path, request)
        with socket.create_connection((cfg['ns']['endpoint_ip'], cfg['ns']['port']), 10) as sock:
            sock.settimeout(10)
            send(sock, request)
            reply = line(sock)
            if reply != dict(status='PASS', run=cfg['run'], nonce=request['nonce']):
                raise ValueError('Sender export request refused')
        deadline = time.monotonic()+timeout
        while not receipt_path.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError('Original SNAT sender file export did not complete')
            time.sleep(.2)
    receipt = json.loads(receipt_path.read_text())
    if (receipt.get('status') != 'PASS' or receipt.get('expected') != expected or
        receipt.get('bytes') != path.stat().st_size or receipt.get('sha256') != digest(path)):
        raise ValueError('Sender export receipt/identity/hash mismatch')
    return verify_export(root, cfg['run'], baseline)


def verify_export(root, run, baseline):
    root = Path(root)
    path = root/'ns/snat-icmp-sender.full.jsonl'
    receipt = json.loads(path.with_suffix('.receipt.json').read_text())
    request = json.loads((root/'ns/snat-icmp-export-request.json').read_text())
    expected = dict(run=run, label='snat', boot=baseline['boot'], log_id=baseline['icmp']['log_id'],
                    ping_pid=baseline['icmp']['pid'], probe_id=baseline['probe_id'])
    if (receipt.get('status') != 'PASS' or receipt.get('expected') != expected or
        request.get('expected') != expected or receipt.get('nonce') != request.get('nonce') or
        receipt.get('bytes') != path.stat().st_size or receipt.get('sha256') != digest(path)):
        raise ValueError('Original sender export ownership/hash not proven')
    return receipt
