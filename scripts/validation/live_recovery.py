#!/usr/bin/env python3
"""Observe existing continuity probes before AFTER snapshots or new resources.

This barrier never sets traffic measurement anchors, calculates downtime, starts
probes, reconnects held sockets, or performs resource creation/remediation.
"""

# Source helpers are grouped; remote/guest copies keep flat sibling imports.
import sys as _sys
from pathlib import Path as _Path
_source = _Path(__file__).resolve().parents[1]
if _source.name == 'scripts' and (_source / '_paths.py').is_file():
    _sys.path.insert(0, str(_source))
    from _paths import configure as _configure
    _configure()
from run_layout import run_path
import argparse
import json
import math
from pathlib import Path
import signal
import time
import uuid

from workload_validation import Validation, read_evidence, save, valid_packet
from tcp_validation import PairD


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def stream_ready(stats):
    return (stats.get('consecutive_successes', 0) >= 5 and stats.get('open_failure_since') is None
            and not stats.get('held_broken') and not stats.get('terminal_error'))


def evaluate(points, fences):
    result = {}
    for name, point in points.items():
        cursor = point.get('cursor')
        if name not in fences and finite(cursor):
            fences[name] = cursor
        result[name] = dict(ready=bool(point.get('ready') and finite(cursor) and
                                      name in fences and cursor > fences[name]),
                            cursor=cursor, entry_fence=fences.get(name))
    return result


class Reader:
    def __init__(self, validation):
        self.v = validation
        self.root, self.cfg = validation.root, validation.cfg
        self.d = PairD(validation)
        self.ext = None
        if self.cfg.get('ew_matrix_enabled') or self.cfg.get('ns', {}).get('enabled'):
            from extended_validation import Extended
            self.ext = Extended(validation)
        self.measure = read_evidence(self.root, 'measure-baseline.json')
        self.tcp = read_evidence(self.root, 'pair-d-baseline.json')
        self.ns = read_evidence(self.root, 'ns-baseline.json').get('clients', {})

    def fast_points(self, deadline):
        result = {}
        rows = self.v.collect('measure', deadline=deadline)
        for key in ('0', '1'):
            packets = [r for r in rows[key] if valid_packet(r)]
            if any(row['boot'] != self.measure[key]['boot'] for row in packets):
                raise ValueError('Original EW ICMP guest boot changed')
            tail = sorted(packets, key=lambda r:r['seq'])[-5:]
            latest = tail[-1] if tail else {}
            result['ew-icmp/'+key] = dict(cursor=latest.get('mono'), ready=len(tail) == 5 and
                all(r['success'] for r in tail) and all(b['seq'] == a['seq']+1 for a,b in zip(tail,tail[1:])))
        if self.d.enabled:
            current = self.d.latest(self.v.collect('tcp', deadline=deadline))
            for key,row in current.items():
                if row['boot'] != self.tcp['boots'][key]:
                    raise ValueError('Original EW TCP guest boot changed')
            client = current.get('0', {})
            if client and (client.get('session_id') != self.tcp['session_id'] or client.get('held_broken')):
                raise ValueError('Original EW TCP held session changed')
            for name in ('held','new_old','new_listener'):
                stats = client.get('streams', {}).get(name, {})
                result['ew-tcp/'+name] = dict(cursor=stats.get('last_success_mono'),
                    ready=client.get('armed') is True and client.get('held_established') is True and stream_ready(stats))
        if self.ext and self.ext.ns:
            # One Nova console read and the existing controller JSONL; no Neutron inventory queries.
            for direction,row in self.ext.ns_rows().items():
                original = self.ns[direction]
                if row and (row.get('run') != self.cfg['run'] or any(
                        row.get(k) != original.get(k) for k in ('boot','probe_id'))):
                    raise ValueError('Original N-S client identity changed: '+direction)
                names=(('held','new_old','new_listener') if self.cfg['ns'].get('new_port') else ('held','new')) if self.cfg['ns'].get('tcp_enabled',True) else ()
                for name in names:
                    stats = row.get('streams', {}).get(name, {})
                    if name == 'held' and stats and (stats.get('held_broken') or
                            stats.get('connection_attempts') != 1 or any(
                            stats.get(k) != original['streams']['held']['first_session']
                            for k in ('first_session','last_session'))):
                        raise ValueError('Original N-S held session changed: '+direction)
                    result[direction+'/'+name] = dict(cursor=stats.get('last_success_mono'),
                        ready=(not self.cfg['ns'].get('new_port') or row.get('armed') is True) and stream_ready(stats))
                if self.cfg['ns'].get('icmp_enabled'):
                    ping, old = row.get('icmp') or {}, original['icmp']
                    if ping and any(ping.get(k) != old.get(k) for k in ('pid','log_id')):
                        raise ValueError('Original N-S ping identity changed: '+direction)
                    last, mono = ping.get('last_reply_mono'), row.get('mono')
                    result[direction+'/icmp'] = dict(cursor=last, ready=ping.get('alive') is True and
                        finite(last) and finite(mono) and 0 <= mono-last <= 3)
        return result

    def matrix_points(self):
        result = {}
        if self.ext and self.ext.matrix:
            for name, pair in self.ext.flows().items():
                capture = self.ext.capture(name, pair)
                if not capture.checkpoint():
                    raise ValueError('Original EW matrix capture missing; replacement prohibited')
                state = capture.snapshot()
                endpoint = state.get('observed_endpoint') or {}
                result[name] = dict(cursor=endpoint.get('index'), ready=state.get('status') == 'RUNNING'
                                    and not state.get('parse_problems') and bool(endpoint))
        return result


def wait(root, reader, timeout=300, poll=2):
    if not finite(timeout) or timeout <= 0:
        raise ValueError('Positive bounded recovery timeout required')
    journal = run_path(root, 'post-restore-observations')/str(uuid.uuid4())
    journal.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    latest = run_path(root, 'post-restore-traffic.json')
    start, deadline = time.time(), time.monotonic()+timeout
    fences, checks = {}, {}
    result = dict(status='IN_PROGRESS', started_at=start, timeout_seconds=timeout,
                  scope='Workflow coordination only; final downtime remains calculated from original evidence',
                  probe_restart=False, resource_creation=False)
    def persist():
        result.update(observed_at=time.time(), checks=checks)
        save(journal.with_suffix('.json'), result)
        save(latest, result)
    persist()
    try:
        if callable(reader):
            reader = reader()
        while time.monotonic() < deadline:
            checks = evaluate(reader.fast_points(deadline), fences)
            if checks and all(c['ready'] for c in checks.values()):
                matrix = evaluate(reader.matrix_points(), fences)
                checks.update(matrix)
                if all(c['ready'] for c in checks.values()):
                    result.update(status='PASS', recovered_observed_at=time.time())
                    persist()
                    return result
            persist()
            time.sleep(min(poll, max(0, deadline-time.monotonic())))
        raise TimeoutError('Existing continuity traffic did not recover within the observation timeout')
    except BaseException as exc:
        result.update(status='UNAVAILABLE', reason=str(exc) or type(exc).__name__)
        persist()
        raise


class RecoveryDeadline(BaseException):
    pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('--timeout', type=float, default=300)
    args = parser.parse_args()
    if not finite(args.timeout) or args.timeout <= 0:
        parser.error('timeout must be positive and finite')
    # Bound SDK/transport calls as well as the polling loop; never leave a detached waiter.
    signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(RecoveryDeadline('Recovery observation deadline reached')))
    signal.setitimer(signal.ITIMER_REAL, args.timeout)
    try:
        print(json.dumps(wait(args.root, lambda: Reader(Validation(args.root)), args.timeout)))
    except RecoveryDeadline as exc:
        raise SystemExit(str(exc))
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


if __name__ == '__main__':
    main()
