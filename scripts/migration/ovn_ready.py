#!/usr/bin/env python3
"""Bounded identity-based chassis and batched original-port readiness checks."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import subprocess
import time


def decode(value):
    if isinstance(value, list) and len(value) == 2:
        if value[0] == 'set':
            return [decode(v) for v in value[1]]
        if value[0] in ('uuid', 'named-uuid'):
            return value[1]
    return value


def query(db, table, columns, timeout):
    raw = json.loads(subprocess.check_output(['docker', 'exec', 'ovn_northd', 'ovn-sbctl',
        '--timeout='+str(max(1, math.ceil(timeout))), '--db='+db, '--format=json',
        '--columns='+','.join(columns), 'list', table], text=True, timeout=timeout))
    if raw['headings'] != columns or any(len(row) != len(columns) for row in raw['data']):
        raise RuntimeError('Malformed OVN readiness response: '+table)
    return [dict(zip(columns, map(decode, row))) for row in raw['data']]


def chassis_missing(expected, chassis, encaps):
    encap = {r['_uuid']: r for r in encaps}
    missing = []
    for host, wanted in expected.items():
        matches = [r for r in chassis if r['name'] == wanted['system_id']]
        if len(matches) != 1:
            missing.append(host+': missing/duplicate chassis identity')
            continue
        refs = matches[0]['encaps']
        refs = refs if isinstance(refs, list) else [refs]
        if not any(encap.get(ref, {}).get('type') == 'geneve' and
                   encap[ref].get('ip') == wanted['tunnel_ip'] for ref in refs):
            missing.append(host+': Geneve tunnel IP differs')
    return missing


def ports_missing(ports, rows):
    by_port = {}
    for row in rows:
        by_port.setdefault(row['logical_port'], []).append(row)
    missing = []
    for port in ports:
        matches = by_port.get(port, [])
        flag = matches[0]['up'] if len(matches) == 1 else None
        up = flag is True or (isinstance(flag, list) and len(flag) == 1 and flag[0] is True)
        if len(matches) != 1 or not matches[0]['chassis'] or not up:
            missing.append(port)
    return missing


def wait(cfg, mode):
    deadline = time.monotonic()+cfg['timeout']
    ports = []
    if mode == 'bindings':
        raw = Path(cfg['ports_path']).read_bytes()
        if hashlib.sha256(raw).hexdigest() != cfg['ports_sha256']:
            raise RuntimeError('Original VM port checkpoint hash changed')
        ports = [p for p in raw.decode().splitlines() if p]
    elif len({v['system_id'] for v in cfg['hosts'].values()}) != len(cfg['hosts']):
        raise RuntimeError('Duplicate OVS system-id across inventory hosts')
    missing = []
    while time.monotonic() < deadline:
        def read(table, columns):
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Readiness deadline reached')
            return query(cfg['sb'], table, columns, min(10, remaining))
        try:
            if mode == 'chassis':
                missing = chassis_missing(cfg['hosts'], read('Chassis', ['name', 'hostname', 'encaps']),
                    read('Encap', ['_uuid', 'type', 'ip']))
            else:
                missing = ports_missing(ports, read('Port_Binding', ['logical_port', 'chassis', 'up']))
            if not missing:
                return dict(status='PASS', checked=len(ports) if mode == 'bindings' else len(cfg['hosts']))
        except (subprocess.SubprocessError, TimeoutError) as exc:
            missing = [str(exc)]
        remaining = deadline-time.monotonic()
        if remaining > 0:
            time.sleep(min(cfg['poll'], remaining))
    raise TimeoutError(mode+' did not converge: '+json.dumps(missing))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('chassis', 'bindings'))
    parser.add_argument('config')
    args = parser.parse_args()
    config = json.loads(args.config)
    if config['timeout'] <= 0 or config['poll'] <= 0:
        parser.error('Positive readiness timeout and polling interval required')
    print(json.dumps(wait(config, args.mode)))
