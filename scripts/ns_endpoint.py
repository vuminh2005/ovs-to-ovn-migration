#!/usr/bin/env python3
"""Check that the controller can bind its N-S listener before creating workloads."""
import argparse
import ipaddress
import json
import socket


def check(address, port):
    address = str(ipaddress.IPv4Address(address))
    if not 1 <= port <= 65535:
        raise ValueError('N-S listener port must be between 1 and 65535')
    result = dict(address=address, port=port)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((address, port))
        except OSError as exc:
            return dict(result, status='FAIL', errno=exc.errno, reason=str(exc))
    return dict(result, status='PASS')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('address')
    parser.add_argument('port', type=int)
    args = parser.parse_args()
    result = check(args.address, args.port)
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if result['status'] == 'PASS' else 1)
