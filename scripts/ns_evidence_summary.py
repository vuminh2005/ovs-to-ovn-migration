#!/usr/bin/env python3
"""Read one completed run's SNAT tuple, server connection log, and TCP resets."""
import argparse
import ipaddress
import json
from pathlib import Path
import shutil
import subprocess


def read(path):
    return json.loads(path.read_text()) if path.exists() else {}


def summarize(root):
    result = read(root/'ns-results.json')
    held = result.get('directions', {}).get('snat', {}).get('streams', {}).get('held', {})
    print('=== SNAT HELD ===')
    keys = ('status', 'connection_attempts', 'first_session', 'last_session', 'first_local_endpoint',
            'first_peer_endpoint', 'terminal_error', 'terminal_event')
    print(json.dumps({k:held.get(k) for k in keys}, indent=2))
    print('=== MATCHING SERVER CONNECTION EVENTS ===')
    connection = (held.get('first_session') or {}).get('connection')
    path = root/'ns/snat-server.jsonl'
    if path.exists() and connection:
        for line in path.open():
            try:
                row = json.loads(line.split('OVN_NS_JSON ', 1)[1])
            except (ValueError, IndexError):
                continue
            if row.get('connection') == connection:
                print(json.dumps(row, sort_keys=True))
    print('=== CAPTURE EVIDENCE ===')
    evidence = read(root/'ns-diagnostics.json')
    print('Status:', evidence.get('status', 'UNAVAILABLE'), 'Errors:', evidence.get('errors', []))
    if not shutil.which('tcpdump'):
        print('tcpdump unavailable; raw captures remain on disk')
        return
    ns = read(root/'validation-config.json').get('ns', {})
    if not ns:
        return
    guest, translated = held.get('first_local_endpoint'), held.get('first_peer_endpoint')
    ports = sorted({p[1] for p in (guest, translated) if isinstance(p, list) and len(p) == 2 and type(p[1]) is int and 0 < p[1] < 65536})
    if not ports:
        print('No archived held port identity; no speculative port filter applied')
        return
    endpoint, snat, source = (str(ipaddress.IPv4Address(ns[k])) for k in ('endpoint_ip','snat_ip','outbound_ip'))
    for kind, peer in [('endpoint',snat), ('guest-snat',source)]:
        path = root/'ns-diagnostics'/kind/'tcp.pcap'
        print('=== HELD-RELATED TCP RST:', kind, '===')
        if not path.exists():
            print('MISSING:', path)
            continue
        bpf = ('tcp and host '+endpoint+' and host '+peer+' and ('+
               ' or '.join('port '+str(port) for port in ports)+') and tcp[tcpflags] & tcp-rst != 0')
        proc = subprocess.run(['tcpdump','-tt','-nn','-r',str(path),'-c','12',bpf], capture_output=True, text=True, timeout=30)
        print(proc.stdout.strip() or 'No matching RST in this capture')
        if proc.returncode:
            print('Read error:',proc.stderr.strip())
    print('Packet source IP alone does not establish the physical device that originated a RST.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    summarize(parser.parse_args().root)
