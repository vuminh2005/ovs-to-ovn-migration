#!/usr/bin/env python3
"""Small TCP application probes; held sockets never reconnect after establishment."""
import argparse
import json
import os
from pathlib import Path
import socket
import socketserver
import signal
import threading
import time
import uuid

PREFIX = 'OVN_NS_JSON '
CONSOLE_LOCK = threading.Lock()


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
        with CONSOLE_LOCK:
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
                          terminal_event=None,
                          first_local_endpoint=None, last_local_endpoint=None,
                          first_peer_endpoint=None, last_peer_endpoint=None,
                          recovered_failure_bursts=[], first_failure_ts=None, first_failure_mono=None,
                          last_success_ts=None, last_success_mono=None, open_failure_ts=None)
        self.lock = threading.Lock()

    def break_held(self, exc, operation, sequence, wire):
        """Retain the first socket/identity failure, before closing the socket."""
        with self.lock:
            if self.value['held_broken']:
                return
            endpoints = {}
            if wire:
                for key, method in (('socket_local', 'getsockname'), ('socket_peer', 'getpeername')):
                    try:
                        endpoints[key] = list(getattr(wire.sock, method)())
                    except OSError:
                        endpoints[key] = None
            self.value.update(held_broken=True, terminal_error=str(exc),
                terminal_event=dict(error=str(exc), exception=type(exc).__name__,
                    errno=getattr(exc, 'errno', None), operation=operation, sequence=sequence,
                    mono=time.monotonic(), ts=time.time(), **endpoints))

    def record(self, start, response=None, error=None, local_endpoint=None):
        with self.lock:
            v = self.value
            v['attempts'] += 1
            if error:
                v['failures'] += 1
                v['consecutive_successes'] = 0
                if v['open_failure_since'] is None:
                    v['open_failure_since'] = start
                    v['open_failure_ts'] = time.time()-(time.monotonic()-start)
                    if v['first_failure_ts'] is None:
                        v['first_failure_ts'], v['first_failure_mono'] = v['open_failure_ts'], start
            else:
                v['successes'] += 1
                completed, completed_ts = time.monotonic(), time.time()
                v['last_success_mono'], v['last_success_ts'] = completed, completed_ts
                v['consecutive_successes'] += 1
                if v['open_failure_since'] is not None:
                    v['recovered_failure_bursts'].append(dict(start_mono=v['open_failure_since'],
                        recovery_mono=completed, duration_seconds=completed-v['open_failure_since'],
                        start_timestamp=v['open_failure_ts'], recovery_timestamp=completed_ts))
                    v['longest_recovered_failure_seconds'] = max(
                        v['longest_recovered_failure_seconds'], time.monotonic()-v['open_failure_since'])
                    v['open_failure_since'] = None
                v['open_failure_ts'] = None
                session = {k: response[k] for k in ('server', 'server_boot', 'listener', 'connection')}
                if v['first_session'] is None:
                    v['first_session'] = session
                v['last_session'] = session
                if local_endpoint:
                    if v['first_local_endpoint'] is None:
                        v['first_local_endpoint'] = local_endpoint
                    v['last_local_endpoint'] = local_endpoint
                if type(response.get('peer_port')) is int and 0 < response['peer_port'] <= 65535:
                    peer_endpoint = [response['peer'], response['peer_port']]
                    if v['first_peer_endpoint'] is None:
                        v['first_peer_endpoint'] = peer_endpoint
                    v['last_peer_endpoint'] = peer_endpoint
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
        self.arm_lock = threading.Lock()
        self.armed = False
        self.armed_mono = None
        self.armed_ts = None
        self.listener_ack = None
        if cfg.get('new_port'):
            self.stats['new_old'] = self.stats.pop('new')
            self.stats['new_listener'] = Stats()
        self.ping = None
        if cfg.get('icmp_enabled'):
            from ns_icmp import Ping, PREFIX as ICMP_PREFIX
            def publish(value):
                if cfg.get('console', True):
                    with CONSOLE_LOCK:
                        with open('/dev/ttyS0', 'w') as console:
                            console.write(ICMP_PREFIX+json.dumps(value, sort_keys=True)+'\n')
            self.ping = Ping(cfg, publish=publish)
        self.probe_id = str(uuid.uuid4())
        self.export_lock = threading.Lock()
        self.export_nonce = None
        self.export_error = None

    def request_export(self, instruction):
        if not self.cfg.get('icmp_export_enabled') or not instruction or not self.ping:
            return
        nonce = instruction.get('nonce')
        if not isinstance(nonce, str) or len(nonce) != 32:
            return
        with self.export_lock:
            if self.export_nonce == nonce:
                return
            self.export_nonce = nonce
        def transfer():
            from ns_log_transfer import upload
            for attempt in range(3):
                try:
                    upload(self, nonce, not_before=instruction.get('not_before_timestamp'))
                    self.export_error = None
                    return
                except Exception as exc:
                    self.export_error = str(exc)
                    if attempt < 2:
                        self.stop.wait(1)
                if self.stop.is_set():
                    return
        threading.Thread(target=transfer, daemon=True).start()

    def connect(self, stats, port=None):
        with stats.lock:
            stats.value['connection_attempts'] += 1
        sock = socket.socket()
        try:
            sock.settimeout(self.cfg['timeout'])
            sock.bind((self.cfg['source'], 0))
            sock.connect((self.cfg['target'], port or self.cfg['port']))
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
                if name == 'new_listener' and not self.armed:
                    self.stop.wait(.05)
                    continue
                sequence += 1
                started = time.monotonic()
                response = None
                error = None
                local_endpoint = None
                operation = 'held_socket_guard'
                try:
                    if name == 'held' and established and wire is None:
                        raise EOFError('held socket already closed; reconnect prohibited')
                    if wire is None:
                        operation = 'connect'
                        wire = self.connect(stats, self.cfg['new_port'] if name == 'new_listener' else None)
                        if name == 'held':
                            established = True
                    request = dict(run=self.cfg['run'], probe=self.probe_id, seq=sequence)
                    local_endpoint = list(wire.sock.getsockname())
                    operation = 'send'
                    wire.sock.settimeout(self.cfg['timeout'])
                    wire.sock.sendall((json.dumps(request)+'\n').encode())
                    deadline = started+self.cfg['timeout']
                    while True:
                        operation = 'receive'
                        response = wire.line(deadline)
                        if response.get('seq', 0) < sequence:
                            continue  # Drain late echoes, preserving the same held socket.
                        operation = 'validate_echo'
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
                        self.request_export(response.get('icmp_export'))
                        break
                except (TimeoutError, socket.timeout) as exc:
                    error = str(exc)
                except (OSError, EOFError, ValueError, KeyError, TypeError) as exc:
                    error = str(exc)
                    if name == 'held' and established:
                        stats.break_held(exc, operation, sequence, wire)
                    if wire:
                        wire.sock.close()
                        wire = None
                stats.record(started, response, error, local_endpoint)
                if name != 'held' and wire:
                    wire.sock.close()
                    wire = None
                self.stop.wait(max(0, self.cfg['interval']-(time.monotonic()-started)))
        finally:
            if wire:
                wire.sock.close()

    def arm(self):
        with self.arm_lock:
            if not self.armed:
                held = self.stats['held'].snapshot()
                if not held['first_session'] or held['held_broken']:
                    raise RuntimeError('Original held session is not ready')
                wire = self.connect(Stats())  # Dedicated control socket, never the held stream.
                try:
                    wire.sock.sendall((json.dumps(dict(run=self.cfg['run'],
                        control='OPEN', token=self.cfg['control_token']))+'\n').encode())
                    reply = wire.line(time.monotonic()+self.cfg['timeout'])
                finally:
                    wire.sock.close()
                if (reply.get('status') != 'PASS' or reply.get('run') != self.cfg['run'] or
                        reply.get('new_port') != self.cfg['new_port'] or not reply.get('listener') or
                        reply.get('server_boot') != held['first_session']['server_boot'] or
                        reply.get('server') != held['first_session']['server']):
                    raise RuntimeError('New listener acknowledgement/identity mismatch')
                self.listener_ack = reply
                self.armed_mono, self.armed_ts = time.monotonic(), time.time()
                self.armed = True
            return self.control_status()

    def control_status(self):
        held = self.stats['held'].snapshot()
        return dict(status='PASS', run=self.cfg['run'], armed=self.armed,
            armed_mono=self.armed_mono, armed_ts=self.armed_ts,
            ready=bool(held['first_session']) and not held['held_broken'],
            probe_id=self.probe_id, held_session=held['first_session'],
            connection_attempts=held['connection_attempts'], listener_ack=self.listener_ack,
            boot=boot_id(), ts=time.time(), mono=time.monotonic())

    def control_server(self):
        owner = self
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.settimeout(5)
                try:
                    row = Wire(self.request).line(time.monotonic()+5)
                    if row.get('run') != owner.cfg['run'] or row.get('token') != owner.cfg['control_token']:
                        raise ValueError('Control ownership mismatch')
                    if row.get('action') == 'ARM':
                        reply = owner.arm()
                    elif row.get('action') == 'CHECK':
                        reply = owner.control_status()
                    else:
                        raise ValueError('Unknown control action')
                except Exception as exc:
                    reply = dict(status='FAIL', run=owner.cfg['run'], error=str(exc))
                self.request.sendall((json.dumps(reply)+'\n').encode())
        server = ThreadServer((self.cfg.get('control_bind', self.cfg['source']), self.cfg['control_port']), Handler)
        server.timeout = .2
        try:
            while not self.stop.is_set():
                server.handle_request()
        finally:
            server.server_close()

    def run(self):
        threads = [threading.Thread(target=self.stream, args=(name,), daemon=True) for name in self.stats]
        if self.cfg.get('control_port'):
            threads.append(threading.Thread(target=self.control_server, daemon=True))
        if self.ping:
            threads.append(threading.Thread(target=self.ping.run, daemon=True))
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, lambda *_: self.stop.set())
            signal.signal(signal.SIGINT, lambda *_: self.stop.set())
        for thread in threads:
            thread.start()
        started = time.monotonic()
        try:
            while not self.stop.is_set() and time.monotonic()-started < self.cfg['lifetime']:
                emit(self.cfg, dict(kind='client', label=self.cfg['label'], probe_id=self.probe_id,
                                   started_mono=started, armed=self.armed, armed_mono=self.armed_mono,
                                   armed_ts=self.armed_ts, listener_ack=self.listener_ack,
                                   icmp=self.ping.snapshot() if self.ping else None,
                                   icmp_export_error=self.export_error,
                                   streams={k: s.snapshot() for k, s in self.stats.items()}))
                self.stop.wait(1)
        finally:
            self.stop.set()
            if self.ping:
                self.ping.close()
            for thread in threads:
                thread.join(self.cfg['timeout']+1)


class ThreadServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class Server:
    def __init__(self, cfg):
        self.cfg = cfg
        self.listeners = {}
        self.lock = threading.Lock()
        self.opened = None
        self.stop = threading.Event()
        self.log_receiver = None
        if cfg.get('icmp_receive_path'):
            from ns_log_transfer import Receiver
            self.log_receiver = Receiver(cfg)
        self.add_listener(cfg['port'])

    def add_listener(self, port):
        owner = self
        listener_id = str(uuid.uuid4())
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                sock, peer = self.request, self.client_address
                wire, connection, last_request = Wire(sock), str(uuid.uuid4()), None
                if owner.cfg.get('output'):
                    emit(owner.cfg, dict(kind='connection-open', connection=connection,
                        listener=listener_id, server=owner.cfg['server'], peer=list(peer), local=list(sock.getsockname())))
                while True:
                    try:
                        row = wire.line(time.monotonic()+owner.cfg['lifetime'])
                        if row.get('run') != owner.cfg['run']:
                            return
                        if row.get('control'):
                            if port != owner.cfg['port'] or row.get('token') != owner.cfg.get('control_token'):
                                raise ValueError('Listener control ownership mismatch')
                            if row['control'] == 'ICMP_UPLOAD' and owner.log_receiver:
                                owner.log_receiver.receive(sock, row)
                                return
                            elif row['control'] == 'ICMP_EXPORT' and owner.log_receiver:
                                reply = owner.log_receiver.request(row)
                            elif row['control'] == 'OPEN':
                                reply = owner.open_new()
                            else:
                                raise ValueError('Unknown listener control action')
                            sock.sendall((json.dumps(reply)+'\n').encode())
                            return
                        last_request = {k: row.get(k) for k in ('run', 'probe', 'seq')}
                        row.update(server=owner.cfg['server'], server_boot=boot_id(), listener=listener_id,
                            connection=connection, peer=peer[0], peer_port=peer[1])
                        if owner.log_receiver:
                            row['icmp_export'] = owner.log_receiver.instruction()
                        sock.sendall((json.dumps(row)+'\n').encode())
                    except (OSError, EOFError, ValueError) as exc:
                        if owner.cfg.get('output'):
                            emit(owner.cfg, dict(kind='connection-close', connection=connection,
                                listener=listener_id, peer=list(peer), last_request=last_request,
                                error=str(exc), errno=getattr(exc, 'errno', None)))
                        return
        server = ThreadServer((self.cfg['bind'], port), Handler)
        self.listeners[port] = (listener_id, server)
        threading.Thread(target=server.serve_forever, kwargs={'poll_interval':.1}, daemon=True).start()
        return listener_id

    def open_new(self):
        with self.lock:
            if not self.opened:
                port = self.cfg['new_port']
                listener = self.add_listener(port)
                self.opened = dict(status='PASS', run=self.cfg['run'], new_port=port,
                    listener=listener, server=self.cfg['server'], server_boot=boot_id(),
                    opened_ts=time.time(), opened_mono=time.monotonic())
                emit(self.cfg, dict(kind='listener-open', **self.opened))
            return dict(self.opened)

    def run(self):
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, lambda *_: self.stop.set())
        started = time.monotonic()
        try:
            while not self.stop.is_set() and time.monotonic()-started < self.cfg['lifetime']:
                emit(self.cfg, dict(kind='server', listener=self.listeners[self.cfg['port']][0],
                    server=self.cfg['server'], new_listener_opened=bool(self.opened), listener_ack=self.opened))
                self.stop.wait(.5)
        finally:
            for _, server in list(self.listeners.values()):
                server.shutdown()
                server.server_close()


def serve(cfg):
    Server(cfg).run()


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
