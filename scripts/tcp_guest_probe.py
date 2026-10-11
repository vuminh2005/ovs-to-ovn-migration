#!/usr/bin/env python3
"""Pair D: one held socket (never reconnect), fresh old/new-port connections.

Only a run-scoped ARM command opens the new listener, after Neutron freeze.
All timing is on the client monotonic clock; summaries survive console truncation.
"""
import copy
import json
import pathlib
import socket
import socketserver
import threading
import time
import uuid


def receive(stream, buffer=b''):
    while b'\n' not in buffer:
        chunk = stream.recv(4096)
        if not chunk:
            raise EOFError('Peer closed the held TCP session')
        buffer += chunk
        if len(buffer) > 8192:
            raise ValueError('Oversized TCP response')
    line, buffer = buffer.split(b'\n', 1)
    return line.decode(), buffer


class LineReader:
    """Preserve a partially received echo across socket timeouts."""
    def __init__(self, stream):
        self.stream, self.buffer = stream, b''

    def read(self):
        while b'\n' not in self.buffer:
            chunk = self.stream.recv(4096)
            if not chunk:
                raise EOFError('Peer closed the held TCP session')
            self.buffer += chunk
            if len(self.buffer) > 8192:
                raise ValueError('Oversized TCP response')
        line, self.buffer = self.buffer.split(b'\n', 1)
        return line.decode()


class Stats:
    def __init__(self):
        self.value = dict(attempts=0, successes=0, failures=0, consecutive_successes=0,
                          first_failure_mono=None, last_success_mono=None,
                          longest_recovered_failure_seconds=0.0, open_failure_since=None)

    def record(self, ok, started, completed):
        v = self.value
        v['attempts'] += 1
        v['successes' if ok else 'failures'] += 1
        v['consecutive_successes'] = v['consecutive_successes'] + 1 if ok else 0
        if ok:
            if v['open_failure_since'] is not None:
                v['longest_recovered_failure_seconds'] = max(
                    v['longest_recovered_failure_seconds'], completed-v['open_failure_since'])
                v['open_failure_since'] = None
            v['last_success_mono'] = completed
        else:
            if v['first_failure_mono'] is None:
                v['first_failure_mono'] = started
            if v['open_failure_since'] is None:
                v['open_failure_since'] = started


class TCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class Probe:
    def __init__(self, cfg, emit, bind='0.0.0.0'):
        self.cfg, self.emit, self.bind = cfg, emit, bind
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.armed = False
        self.listener_opened = False
        self.armed_mono = None
        self.session_id = None
        self.held_established = False
        self.held_broken = False
        self.arm_attempts = 0
        self.total = {k: Stats() for k in ('held', 'new_old', 'new_listener')}
        self.phase = {k: Stats() for k in self.total}
        self.servers = []

    def listener(self, port, control=False):
        probe = self
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                reader = LineReader(self.request)
                self.request.settimeout(1)
                while not probe.stop.is_set():
                    try:
                        line = reader.read()
                        parts = line.split()
                        if len(parts) < 2 or parts[1] != probe.cfg['run']:
                            raise ValueError('Wrong validation run')
                        if parts[0] == 'ARM' and control:
                            response = probe.arm()
                        elif parts[0] == 'CHECK' and control:
                            with probe.lock:
                                response = {'status':'PASS', 'run':probe.cfg['run'],
                                            'ready':probe.held_established and not probe.held_broken,
                                            'armed':probe.armed, 'session_id':probe.session_id}
                        elif parts[0] == 'OPEN' and not control:
                            with probe.lock:
                                if not probe.listener_opened:
                                    probe.listener(probe.cfg['tcp_new_port'])
                                    probe.listener_opened = True
                            response = {'status': 'PASS', 'run': probe.cfg['run'], 'opened': True}
                        elif parts[0] == 'PING' and not control:
                            response = {'echo': line, 'run': probe.cfg['run']}
                        else:
                            raise ValueError('Unexpected command')
                        self.request.sendall(json.dumps(response).encode()+b'\n')
                    except socket.timeout:
                        continue
                    except (OSError, EOFError, ValueError):
                        return
        server = TCPServer((self.bind, port), Handler)
        self.servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()

    def command(self, port, text):
        with socket.create_connection((self.cfg['peer'], port), self.cfg['tcp_timeout']) as stream:
            stream.settimeout(self.cfg['tcp_timeout'])
            stream.sendall(text.encode()+b'\n')
            line, _ = receive(stream)
            return json.loads(line)

    def arm(self):
        with self.lock:
            if not self.armed:
                self.arm_attempts += 1
                reply = self.command(self.cfg['tcp_old_port'], 'OPEN '+self.cfg['run'])
                if reply.get('run') != self.cfg['run'] or reply.get('opened') is not True:
                    raise ValueError('New listener acknowledgement missing')
                self.phase = {k: Stats() for k in self.total}
                self.armed_mono = time.monotonic()
                self.armed = True
            return {'status': 'PASS', 'run': self.cfg['run'], 'armed': True,
                    'armed_mono': self.armed_mono, 'session_id': self.session_id}

    def held(self):
        stream = None
        pending = None
        reader = None
        seq = 0
        while not self.stop.is_set():
            started = time.monotonic()
            ok = False
            try:
                if self.held_broken:
                    raise EOFError('Held session already broken; reconnect prohibited')
                if stream is None:
                    stream = socket.create_connection((self.cfg['peer'], self.cfg['tcp_old_port']), self.cfg['tcp_timeout'])
                    stream.settimeout(self.cfg['tcp_timeout'])
                    self.session_id = str(uuid.uuid4())
                    reader = LineReader(stream)
                if pending is None:
                    seq += 1
                    pending = f"PING {self.cfg['run']} {self.session_id} {seq}"
                    stream.sendall(pending.encode()+b'\n')
                line = reader.read()
                reply = json.loads(line)
                ok = reply.get('echo') == pending and reply.get('run') == self.cfg['run']
                if not ok:
                    raise ValueError('Held echo identity mismatch')
                pending = None
                self.held_established = True
            except socket.timeout:
                # Retain the exact socket and pending echo; no reconnect on a stall.
                pass
            except (OSError, EOFError, ValueError):
                if self.held_established:
                    self.held_broken = True
                else:
                    if stream is not None:
                        stream.close()
                    stream, pending, reader = None, None, None
            self.record('held', ok, started)
            self.stop.wait(max(0, self.cfg['tcp_interval']-(time.monotonic()-started)))
        if stream is not None:
            stream.close()

    def fresh(self, key, port):
        seq = 0
        while not self.stop.is_set():
            if key == 'new_listener' and not self.armed:
                self.stop.wait(.1)
                continue
            started = time.monotonic()
            seq += 1
            text = f"PING {self.cfg['run']} {key} {seq}"
            try:
                reply = self.command(port, text)
                ok = reply.get('echo') == text and reply.get('run') == self.cfg['run']
            except (OSError, EOFError, ValueError):
                ok = False
            self.record(key, ok, started)
            self.stop.wait(max(0, self.cfg['tcp_interval']-(time.monotonic()-started)))

    def record(self, key, ok, started):
        with self.lock:
            completed = time.monotonic()
            self.total[key].record(ok, started, completed)
            if self.armed:
                self.phase[key].record(ok, max(started, self.armed_mono), completed)

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(dict(kind='tcp', mono=time.monotonic(), ts=time.time(),
                server_id=self.cfg['server_id'], armed=self.armed, armed_mono=self.armed_mono,
                listener_opened=self.listener_opened,
                arm_attempts=self.arm_attempts, held_established=self.held_established,
                held_broken=self.held_broken, session_id=self.session_id,
                total={k:s.value for k,s in self.total.items()},
                streams={k:s.value for k,s in self.phase.items()}))

    def start(self):
        if self.cfg['tcp_role'] == 'server':
            self.listener(self.cfg['tcp_old_port'])
            return
        self.listener(self.cfg['tcp_control_port'], control=True)
        for function, args in ((self.held, ()), (self.fresh, ('new_old', self.cfg['tcp_old_port'])),
                               (self.fresh, ('new_listener', self.cfg['tcp_new_port']))):
            threading.Thread(target=function, args=args, daemon=True).start()

    def close(self):
        self.stop.set()
        for server in self.servers:
            server.shutdown()
            server.server_close()


def main():
    cfg = json.loads(pathlib.Path('/etc/migration-probe.json').read_text())
    boot = pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    def emit(value):
        value.update(run=cfg['run'], vm=cfg['vm'], boot=boot)
        with open('/dev/ttyS0', 'w') as stream:
            stream.write('OVN_MIGRATION_JSON '+json.dumps(value, separators=(',', ':'))+'\n')
    probe = Probe(cfg, emit)
    probe.start()
    deadline = time.monotonic()+cfg['lifetime']
    try:
        while time.monotonic() < deadline:
            emit(probe.snapshot())
            probe.stop.wait(2)
    finally:
        probe.close()


if __name__ == '__main__':
    main()
