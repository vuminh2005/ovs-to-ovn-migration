#!/usr/bin/env python3
"""Small TCP application probes; held sockets never reconnect after establishment."""
import argparse
import json
import os
from pathlib import Path
import socket
import threading
import time
import uuid

PREFIX = 'OVN_NS_JSON '


def boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def emit(cfg, value):
    row = dict(value, run=cfg['run'], boot=boot_id(), mono=time.monotonic(), ts=time.time())
    line = PREFIX + json.dumps(row, sort_keys=True)
    if cfg.get('output'):
        path = Path(cfg['output'])
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a') as f:
            f.write(line + '\n')
            f.flush()
        path.chmod(0o600)
    if cfg.get('console', True):
        with open('/dev/ttyS0', 'w') as f:
            f.write(line + '\n')


class Wire:
    def __init__(self, sock):
        self.sock = sock
        self.buffer = b''

    def line(self, deadline):
        while b'\n' not in self.buffer:
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise TimeoutError('echo timeout')
            self.sock.settimeout(remaining)
            chunk = self.sock.recv(8192)
            if not chunk:
                raise EOFError('socket closed')
            self.buffer += chunk
            if len(self.buffer) > 65536:
                raise ValueError('oversized echo')
        row, self.buffer = self.buffer.split(b'\n', 1)
        return json.loads(row)


class Stats:
    def __init__(self):
        self.value = dict(attempts=0, successes=0, failures=0, consecutive_successes=0,
                          connection_attempts=0, first_session=None, last_session=None,
                          open_failure_since=None, longest_recovered_failure_seconds=0.0,
                          observed_peer_ips=[], held_broken=False, terminal_error=None,
                          recovered_failure_bursts=[])
        self.lock = threading.Lock()

    def record(self, start, response=None, error=None):
        with self.lock:
            v = self.value
            v['attempts'] += 1
            if error:
                v['failures'] += 1
                v['consecutive_successes'] = 0
                if v['open_failure_since'] is None:
                    v['open_failure_since'] = start
            else:
                v['successes'] += 1
                v['consecutive_successes'] += 1
                if v['open_failure_since'] is not None:
                    v['recovered_failure_bursts'].append(dict(start_mono=v['open_failure_since'],
                        recovery_mono=time.monotonic(), duration_seconds=time.monotonic()-v['open_failure_since']))
                    v['longest_recovered_failure_seconds'] = max(
                        v['longest_recovered_failure_seconds'], time.monotonic()-v['open_failure_since'])
                    v['open_failure_since'] = None
                session = {k: response[k] for k in ('server', 'server_boot', 'listener', 'connection')}
                if v['first_session'] is None:
                    v['first_session'] = session
                v['last_session'] = session
                if response['peer'] not in v['observed_peer_ips']:
                    v['observed_peer_ips'].append(response['peer'])

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.value))


class Client:
    def __init__(self, cfg):
        self.cfg = cfg
        self.stats = {name: Stats() for name in ('held', 'new')}
        self.stop = threading.Event()
        self.probe_id = str(uuid.uuid4())

    def connect(self, stats):
        with stats.lock:
            stats.value['connection_attempts'] += 1
        sock = socket.socket()
        try:
            sock.settimeout(self.cfg['timeout'])
            sock.bind((self.cfg['source'], 0))
            sock.connect((self.cfg['target'], self.cfg['port']))
            return Wire(sock)
        except BaseException:
            sock.close()
            raise

    def stream(self, name):
        stats = self.stats[name]
        wire = None
        established = False
        sequence = 0
        try:
            while not self.stop.is_set():
                sequence += 1
                started = time.monotonic()
                response = None
                error = None
                try:
                    if name == 'held' and established and wire is None:
                        raise EOFError('held socket already closed; reconnect prohibited')
                    if wire is None:
                        wire = self.connect(stats)
                        if name == 'held':
                            established = True
                    request = dict(run=self.cfg['run'], probe=self.probe_id, seq=sequence)
                    wire.sock.settimeout(self.cfg['timeout'])
                    wire.sock.sendall((json.dumps(request)+'\n').encode())
                    deadline = started+self.cfg['timeout']
                    while True:
                        response = wire.line(deadline)
                        if response.get('seq', 0) < sequence:
                            continue  # Drain late echoes, preserving the same held socket.
                        if any(response.get(k) != val for k, val in request.items()):
                            raise ValueError('echo identity mismatch')
                        if response.get('peer') != self.cfg['expected_peer']:
                            raise ValueError('unexpected translated/source address')
                        if response.get('server') != self.cfg['expected_server']:
                            raise ValueError('unexpected listener server')
                        session = {k: response[k] for k in ('server', 'server_boot', 'listener', 'connection')}
                        first = stats.snapshot()['first_session']
                        if name == 'held' and first and session != first:
                            raise ValueError('held session identity changed')
                        break
                except (TimeoutError, socket.timeout) as exc:
                    error = str(exc)
                except (OSError, EOFError, ValueError, KeyError, TypeError) as exc:
                    error = str(exc)
                    if name == 'held' and established:
                        with stats.lock:
                            stats.value.update(held_broken=True, terminal_error=error)
                    if wire:
                        wire.sock.close()
                        wire = None
                stats.record(started, response, error)
                if name == 'new' and wire:
                    wire.sock.close()
                    wire = None
                self.stop.wait(max(0, self.cfg['interval']-(time.monotonic()-started)))
        finally:
            if wire:
                wire.sock.close()

    def run(self):
        threads = [threading.Thread(target=self.stream, args=(name,), daemon=True) for name in self.stats]
        for thread in threads:
            thread.start()
        started = time.monotonic()
        try:
            while time.monotonic()-started < self.cfg['lifetime']:
                emit(self.cfg, dict(kind='client', label=self.cfg['label'], probe_id=self.probe_id,
                                   started_mono=started, streams={k: s.snapshot() for k, s in self.stats.items()}))
                self.stop.wait(1)
        finally:
            self.stop.set()
            for thread in threads:
                thread.join(self.cfg['timeout']+1)


def serve(cfg):
    listener_id = str(uuid.uuid4())
    def handle(sock, peer):
        with sock:
            wire = Wire(sock)
            connection = str(uuid.uuid4())
            while True:
                try:
                    row = wire.line(time.monotonic()+cfg['lifetime'])
                    if row.get('run') != cfg['run']:
                        return
                    row.update(server=cfg['server'], server_boot=boot_id(), listener=listener_id,
                               connection=connection, peer=peer[0])
                    sock.sendall((json.dumps(row)+'\n').encode())
                except (OSError, EOFError, ValueError):
                    return
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((cfg['bind'], cfg['port']))
        listener.listen(64)
        listener.settimeout(1)
        started = time.monotonic()
        while time.monotonic()-started < cfg['lifetime']:
            emit(cfg, dict(kind='server', listener=listener_id, server=cfg['server']))
            try:
                sock, peer = listener.accept()
                threading.Thread(target=handle, args=(sock, peer), daemon=True).start()
            except socket.timeout:
                pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('client', 'serve'))
    parser.add_argument('config', type=Path)
    args = parser.parse_args()
    cfg = json.loads(args.config.read_text())
    if cfg.get('server') == 'instance-id':
        cfg['server'] = Path('/var/lib/cloud/data/instance-id').read_text().strip()
    serve(cfg) if args.action == 'serve' else Client(cfg).run()


if __name__ == '__main__':
    main()
