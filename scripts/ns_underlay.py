#!/usr/bin/env python3
"""Persistent shared eth1 Linux bridge/veth, retaining the underlay IP and MAC."""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import time
import yaml


def run(*args):
    return subprocess.check_output(args, text=True)


def inspect(cfg, deployed=False):
    links = {l['ifname']: l for l in json.loads(run('ip', '-j', '-d', 'link', 'show'))}
    addresses = json.loads(run('ip', '-j', '-4', 'addr', 'show'))
    owners = [a['ifname'] for a in addresses for ip in a['addr_info']
              if ip.get('family') == 'inet' and ip['local']+'/'+str(ip['prefixlen']) == cfg['address']]
    if len(owners) != 1 or owners[0] not in ('eth1', 'br-underlay'):
        raise RuntimeError('Expected underlay address must exist once on eth1 or br-underlay')
    if links['eth1']['mtu'] != cfg['mtu'] or links[owners[0]]['mtu'] != cfg['mtu']:
        raise RuntimeError('Unexpected underlay MTU')
    mac = links['eth1']['address']
    if owners[0] == 'br-underlay' and (links['br-underlay']['address'] != mac or
            links['eth1'].get('master') != 'br-underlay'):
        raise RuntimeError('Bridge must retain eth1 MAC and membership')
    if deployed:
        if owners != ['br-underlay']:
            raise RuntimeError('Kolla must use br-underlay, not IP on eth1')
        for name in ('ns-uplink', 'ns-ex'):
            if name not in links or links[name]['mtu'] != cfg['mtu'] or links[name].get('linkinfo', {}).get('info_kind') != 'veth':
                raise RuntimeError('Missing/invalid N-S veth pair')
        if links['ns-uplink'].get('master') != 'br-underlay':
            raise RuntimeError('ns-uplink is not attached to br-underlay')
        # Confirm the actual pair, never substitute similarly named interfaces.
        left = int(Path('/sys/class/net/ns-uplink/iflink').read_text())
        right = int(Path('/sys/class/net/ns-ex/ifindex').read_text())
        if left != right:
            raise RuntimeError('N-S veth peer identity changed')
        if run('ovs-vsctl', '--timeout=10', 'port-to-br', 'ns-ex').strip() != 'br-ex':
            raise RuntimeError('ns-ex must be a port of br-ex')
        ovs = run('ovs-vsctl', 'get', 'Open_vSwitch', '.', 'external_ids:ovn-bridge-mappings') if cfg.get('ovn') else ''
        if cfg.get('ovn') and cfg['physnet']+':br-ex' not in ovs:
            raise RuntimeError('OVN external physnet mapping missing')
        if not cfg.get('ovn'):
            parser = __import__('configparser').ConfigParser(interpolation=None)
            parser.read('/etc/kolla/neutron-openvswitch-agent/openvswitch_agent.ini')
            if cfg['physnet']+':br-ex' not in parser.get('ovs', 'bridge_mappings') or parser.get('ovs', 'local_ip') != cfg['address'].split('/')[0]:
                raise RuntimeError('Rendered OVS bridge mapping/tunnel IP mismatch')
    return dict(status='PASS', address=cfg['address'], mtu=cfg['mtu'], mac=mac, owner=owners[0])


def netplan_update(directory, cfg, mac):
    """Remove only definitions for eth1/br-underlay; preserve all other netplan data."""
    for path in sorted(directory.glob('*.yaml'))+sorted(directory.glob('*.yml')):
        data = yaml.safe_load(path.read_text()) or {}
        network = data.get('network', {})
        if network.get('renderer', 'networkd') != 'networkd':
            raise RuntimeError('This Ubuntu server lab requires networkd')
        changed = False
        for key, definition in list(network.get('ethernets', {}).items()):
            match = definition.get('match', {})
            if key == 'eth1' or definition.get('set-name') == 'eth1' or match.get('macaddress', '').lower() == mac.lower() or match.get('name') == 'eth1':
                del network['ethernets'][key]
                changed = True
            elif match.get('name') and any(c in match['name'] for c in '*?['):
                # A wildcard might still apply to eth1. Do not merge conflicting definitions.
                import fnmatch
                if fnmatch.fnmatch('eth1', match['name']):
                    raise RuntimeError('Ambiguous wildcard netplan definition covers eth1')
        if 'br-underlay' in network.get('bridges', {}):
            del network['bridges']['br-underlay']
            changed = True
        for name, bridge in network.get('bridges', {}).items():
            if 'eth1' in bridge.get('interfaces', []):
                raise RuntimeError('eth1 belongs to another configured bridge: '+name)
        if changed:
            path.write_text(yaml.safe_dump(data, sort_keys=False))
            path.chmod(0o600)
    data = dict(network=dict(version=2, ethernets={'eth1': dict(dhcp4=False, dhcp6=False, mtu=cfg['mtu'], optional=True)},
        bridges={'br-underlay': dict(interfaces=['eth1'], addresses=[cfg['address']], macaddress=mac,
            mtu=cfg['mtu'], dhcp4=False, dhcp6=False, parameters={'stp': False, 'forward-delay': 0})}))
    path = directory/'90-ns-underlay.yaml'
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    path.chmod(0o600)


def apply(cfg):
    evidence = inspect(cfg)
    directory = Path('/etc/netplan')
    backup = Path('/var/lib/ns-underlay')/str(time.time_ns())
    backup.mkdir(parents=True, mode=0o700)
    shutil.copytree(directory, backup/'netplan')
    # Work on a full temporary copy first, so an ambiguity never leaves partial edits.
    staging = backup/'staged'
    shutil.copytree(directory, staging)
    netplan_update(staging, cfg, evidence['mac'])
    for path in staging.iterdir():
        if path.suffix in ('.yaml', '.yml'):
            shutil.copy2(path, directory/path.name)
    run('netplan', 'generate')
    # Stop cloud-init from regenerating the old eth1 definition on the next boot.
    cloud_config = Path('/etc/cloud/cloud.cfg.d/99-ns-underlay-network.cfg')
    cloud_config.parent.mkdir(parents=True, exist_ok=True)
    if cloud_config.exists():
        shutil.copy2(cloud_config, backup/cloud_config.name)
    cloud_config.write_text('network: {config: disabled}\n')
    cloud_config.chmod(0o600)
    mtu = int(cfg['mtu'])
    script = Path('/usr/local/sbin/ns-veth-up')
    script.write_text('#!/bin/bash\nset -euo pipefail\n'
        'if ! ip link show ns-ex >/dev/null 2>&1; then\n'
        '  ! ip link show ns-uplink >/dev/null 2>&1\n'
        '  ip link add ns-uplink type veth peer name ns-ex\nfi\n'
        'test "$(cat /sys/class/net/ns-uplink/iflink)" = "$(cat /sys/class/net/ns-ex/ifindex)"\n'
        f'ip link set ns-uplink mtu {mtu}\nip link set ns-ex mtu {mtu}\n'
        'ip link set ns-uplink master br-underlay\nip link set ns-uplink up\nip link set ns-ex up\n')
    script.chmod(0o700)
    unit = Path('/etc/systemd/system/ns-underlay-veth.service')
    unit.write_text('[Unit]\nWants=network-online.target\nAfter=network-online.target\nBefore=docker.service\n'
        '[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart=/usr/local/sbin/ns-veth-up\n'
        '[Install]\nWantedBy=multi-user.target\n')
    run('netplan', 'apply')
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', 'ns-underlay-veth.service')
    run('systemctl', 'restart', 'ns-underlay-veth.service')
    return dict(evidence, backup=str(backup), status='PERSISTENT_CONFIG_READY')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('inspect', 'apply', 'deployed'))
    parser.add_argument('config')
    args = parser.parse_args()
    cfg = json.loads(args.config)
    print(json.dumps(apply(cfg) if args.action == 'apply' else inspect(cfg, args.action == 'deployed')))


if __name__ == '__main__':
    main()
