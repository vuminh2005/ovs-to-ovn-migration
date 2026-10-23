#!/usr/bin/env python3
"""Read effective host networking without changing interfaces or OVS state."""
import argparse
import configparser
import ipaddress
import json
import socket
import subprocess


def command(*args):
    return subprocess.check_output(args, text=True, timeout=20).strip()


def ovs(*args):
    return command('docker', 'exec', 'openvswitch_vswitchd', 'ovs-vsctl', '--timeout=10', *args)


def external_id(name):
    # `get` prints OVSDB text, whose strings need not be JSON-quoted.  Table
    # formatting options apply to `list`/`find`, so request a JSON table and
    # decode its OVSDB map instead of treating scalar `get` output as JSON.
    raw = ovs('--format=json', '--columns=external_ids',
              'list', 'Open_vSwitch', '.')
    try:
        table = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise RuntimeError('Invalid OVS external_ids JSON table') from exc
    if (not isinstance(table, dict) or table.get('headings') != ['external_ids']
            or not isinstance(table.get('data'), list) or len(table['data']) != 1
            or not isinstance(table['data'][0], list) or len(table['data'][0]) != 1):
        raise RuntimeError('Expected exactly one Open_vSwitch external_ids row')
    encoded = table['data'][0][0]
    if (not isinstance(encoded, list) or len(encoded) != 2
            or encoded[0] != 'map' or not isinstance(encoded[1], list)):
        raise RuntimeError('Invalid OVS external_ids map')
    values = {}
    for pair in encoded[1]:
        if (not isinstance(pair, list) or len(pair) != 2
                or not all(isinstance(value, str) for value in pair)
                or pair[0] in values):
            raise RuntimeError('Invalid or duplicate OVS external_ids entry')
        values[pair[0]] = pair[1]
    return values.get(name, '')


def mappings(text):
    result = {}
    for item in text.split(','):
        if not item.strip():
            continue
        name, bridge = item.strip().split(':', 1)
        if not name or not bridge or name in result:
            raise ValueError('Missing or duplicate physical network mapping')
        result[name] = bridge
    return result


def inspect(cfg, target=False):
    iface = cfg['tunnel_interface']
    rows = json.loads(command('ip', '-j', '-4', 'addr', 'show', 'dev', iface))
    if len(rows) != 1:
        raise RuntimeError('Expected exactly one tunnel interface: '+iface)
    addresses = [a for a in rows[0]['addr_info'] if a.get('family') == 'inet']
    parser = configparser.ConfigParser(interpolation=None)
    if not target:
        if not parser.read(cfg.get('source_config', '/etc/kolla/neutron-openvswitch-agent/openvswitch_agent.ini')):
            raise RuntimeError('Missing effective source OVS agent configuration')
        local_ip = parser.get('ovs', 'local_ip')
    else:
        local_ip = external_id('ovn-encap-ip')
    matches = [a for a in addresses if a['local'] == local_ip]
    if len(matches) != 1:
        raise RuntimeError('Tunnel interface does not own the effective OVS/OVN tunnel IP')
    address = matches[0]
    system_id = external_id('system-id')
    if not system_id:
        raise RuntimeError('Missing persistent OVS system-id')
    result = dict(tunnel_interface=iface, tunnel_ip=local_ip,
        tunnel_address=local_ip+'/'+str(address['prefixlen']), tunnel_mtu=rows[0]['mtu'],
        system_id=system_id, identities=sorted({socket.gethostname(), socket.getfqdn()}))
    expected = cfg.get('expected', {})
    for field in ('system_id', 'tunnel_ip', 'tunnel_interface', 'tunnel_mtu'):
        if field in expected and result[field] != expected[field]:
            raise RuntimeError('Host topology changed: '+field)
    if cfg.get('expected_address') and ipaddress.ip_interface(cfg['expected_address']) != ipaddress.ip_interface(result['tunnel_address']):
        raise RuntimeError('Tunnel address differs from the explicit host override')
    if cfg.get('external'):
        physnet = cfg['physnet']
        configured = mappings(external_id('ovn-bridge-mappings') if target else parser.get('ovs', 'bridge_mappings'))
        bridge = configured.get(physnet)
        if not bridge or (expected.get('external_bridge') and bridge != expected['external_bridge']):
            raise RuntimeError('Missing or changed external physical network mapping: '+physnet)
        interfaces = cfg.get('external_interfaces', [])
        if not interfaces:
            raise RuntimeError('Configure neutron_external_interface for every gateway host')
        attached = [i for i in interfaces if ovs('port-to-br', i) == bridge]
        if not attached:
            raise RuntimeError('Configured external interface is not attached to '+bridge)
        result.update(physnet=physnet, external_bridge=bridge, external_interfaces=attached)
    return dict(status='PASS', **result)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('source', 'target'))
    parser.add_argument('config')
    args = parser.parse_args()
    print(json.dumps(inspect(json.loads(args.config), args.action == 'target')))
