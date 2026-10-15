#!/usr/bin/env python3
"""Authenticated run-owned N-S control, independent of the Neutron API."""
import ipaddress
import json
import socket
import subprocess
import sys
import uuid


def request(cfg):
    ipaddress.IPv4Address(cfg['ip'])
    if cfg['action'] not in ('CHECK', 'ARM'):
        raise ValueError('Unknown N-S control action')
    with socket.create_connection((cfg['ip'], int(cfg['port'])), 10) as sock:
        sock.settimeout(15)
        sock.sendall((json.dumps({k: cfg[k] for k in ('action', 'run', 'token')})+'\n').encode())
        data = b''
        while b'\n' not in data:
            block = sock.recv(8192)
            if not block or len(data)+len(block) > 16384:
                raise RuntimeError('Missing/oversized N-S control acknowledgement')
            data += block
        reply = json.loads(data.split(b'\n', 1)[0])
    if reply.get('run') != cfg['run'] or reply.get('status') != 'PASS' or not reply.get('ready'):
        raise RuntimeError('Invalid N-S control acknowledgement: '+str(reply))
    if reply.get('armed') != (cfg['action'] == 'ARM'):
        raise RuntimeError('Unexpected N-S ARM state')
    return reply


def main():
    cfg = json.loads(sys.argv[1])
    if cfg.get('router'):
        namespace = 'qrouter-'+str(uuid.UUID(cfg.pop('router')))
        subprocess.run(['ip', 'netns', 'exec', namespace, sys.executable,
                        __file__, json.dumps(cfg)], check=True)
    else:
        print(json.dumps(request(cfg)))


if __name__ == '__main__':
    main()
