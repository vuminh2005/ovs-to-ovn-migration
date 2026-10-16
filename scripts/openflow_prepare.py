#!/usr/bin/env python3
"""Add supported OpenFlow protocols without replacing a bridge or its flows."""
import argparse
import json
import re
import subprocess


def run(args):
    return subprocess.check_output(['docker', 'exec', 'openvswitch_vswitchd']+args,
                                   text=True, timeout=20)


def bridge():
    value = json.loads(run(['ovs-vsctl', '--format=json', '--columns=_uuid,name,protocols',
                           'list', 'Bridge', 'br-int']))
    if len(value['data']) != 1:
        raise ValueError('Expected the existing br-int bridge')
    row = dict(zip(value['headings'], value['data'][0]))
    protocols = row['protocols']
    protocols = protocols[1] if isinstance(protocols, list) and protocols[0] == 'set' else [protocols]
    return dict(uuid=row['_uuid'], name=row['name'], protocols=protocols)


def prepare(protocols, check=False):
    requested = set(protocols.split(','))
    if 'OpenFlow15' not in requested or any(not re.fullmatch(r'OpenFlow1[0-5]', p) for p in requested):
        raise ValueError('Explicit OpenFlow10..15 list including OpenFlow15 required')
    before = bridge()
    if before['name'] != 'br-int':
        raise ValueError('Unexpected bridge identity')
    # An empty OVS protocol set means the implementation defaults to OpenFlow10.
    desired = set(before['protocols'] or ['OpenFlow10']) | requested
    changed = not desired <= set(before['protocols'])
    if changed and check:
        raise ValueError('Prepared OpenFlow protocols are missing; prepare before Neutron freeze')
    if changed:
        run(['ovs-vsctl', 'set', 'Bridge', 'br-int', 'protocols='+','.join(sorted(desired))])
    after = bridge()
    if after['uuid'] != before['uuid'] or not desired <= set(after['protocols']):
        raise ValueError('Bridge identity or protocol verification failed')
    run(['ovs-ofctl', '-O', 'OpenFlow15', 'show', 'br-int'])
    return dict(status='PASS', changed=changed, before=before, after=after,
                scope='Protocol preparation only; no flow/controller/port deletion')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('protocols')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    print(json.dumps(prepare(args.protocols, args.check), sort_keys=True))
