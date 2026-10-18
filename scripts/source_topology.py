#!/usr/bin/env python3
"""Admit supported pre-existing topology and verify its preservation, read-only."""
from run_layout import run_path
import argparse
import json
from pathlib import Path

from dataplane_capture import save
from shared_external import select, resolve, require_free_ips


def fixed_ips(rows):
    return sorted([dict(subnet_id=p['subnet_id'], ip_address=p['ip_address']) for p in rows],
                  key=lambda p: (p['subnet_id'], p['ip_address']))


def gateway(router):
    info = router.external_gateway_info or {}
    return (dict(network_id=info.get('network_id'), enable_snat=info.get('enable_snat', True),
                 external_fixed_ips=fixed_ips(info.get('external_fixed_ips', []))) if info else {})


def ip_shape(ip):
    return dict(id=ip.id, floating_network_id=ip.floating_network_id,
                floating_ip_address=ip.floating_ip_address, port_id=ip.port_id,
                fixed_ip_address=ip.fixed_ip_address, router_id=ip.router_id)


def port_shape(port):
    return dict(id=port.id, network_id=port.network_id, device_id=port.device_id,
                fixed_ips=fixed_ips(port.fixed_ips), mac_address=port.mac_address,
                security_group_ids=sorted(port.security_group_ids), binding_host_id=port.binding_host_id)


def before(cloud, root):
    cfg = json.loads((run_path(root, 'source-topology-config.json')).read_text())
    networks = list(cloud.network.networks())
    shared = select(cloud.network, cfg, networks)
    selected = shared['network']['id'] if shared else None
    for net in networks:
        if net.id != selected and (net.is_router_external or net.provider_network_type != 'vxlan'):
            raise RuntimeError('Only VXLAN tenants and the configured flat external are supported')
    routers = list(cloud.network.routers())
    ips = list(cloud.network.ips())
    ports = [p for p in cloud.network.ports() if (p.device_owner or '').startswith('compute:')]
    if any(p.network_id == selected for p in ports):
        raise RuntimeError('Direct provider/external VM ports are outside the supported demo scope')
    if any(getattr(r,'is_ha',False) or getattr(r,'is_distributed',False) for r in routers):
        raise RuntimeError('HA/distributed routers are outside the supported demo scope')
    if any(gateway(r).get('network_id') not in (None, selected) for r in routers):
        raise RuntimeError('Existing router uses an unsupported external gateway')
    if any(ip.floating_network_id != selected for ip in ips):
        raise RuntimeError('Existing FIP uses an unsupported external network')
    if shared and cfg.get('reserve_ns_ips', True):
        require_free_ips(cloud.network, shared)
    data = dict(schema=1, status='PASS', run=root.name, shared_external=shared,
        mtu_delta=cfg['mtu_delta'], mode='shared' if shared else 'owned-per-run',
        networks=[dict(id=n.id, network_type=n.provider_network_type, mtu=n.mtu) for n in networks],
        subnets=[dict(id=s.id, network_id=s.network_id, cidr=s.cidr, gateway_ip=s.gateway_ip)
                 for s in cloud.network.subnets()],
        routers=[dict(id=r.id, gateway=gateway(r)) for r in routers],
        floating_ips=[ip_shape(ip) for ip in ips],
        compute_ports=[port_shape(p) for p in ports],
        servers=[dict(id=s.id, status=s.status, compute_host=s.compute_host)
                 for s in cloud.compute.servers(all_projects=True)],
        ownership='Read-only preservation baseline, never workload cleanup ownership')
    for key in ('networks','subnets','routers','floating_ips','compute_ports','servers'):
        data[key].sort(key=lambda row: row['id'])
    path = run_path(root, 'source-topology-before.json')
    if path.exists() and json.loads(path.read_text()) != data:
        raise RuntimeError('Pre-existing topology checkpoint changed; refusing to replace baseline')
    save(path, data)
    return data


def verify(cloud, root, scope):
    result = dict(status='FAIL', scope=scope, owned=False, errors=[])
    try:
        data = json.loads((run_path(root, 'source-topology-before.json')).read_text())
        if data.get('schema') != 1 or data.get('run') != root.name:
            raise RuntimeError('Pre-existing resource baseline identity changed')
        if data['shared_external']:
            resolve(cloud.network, data['shared_external'])
        for row in data['networks']:
            n = cloud.network.get_network(row['id'])
            expected_type = 'geneve' if row['network_type'] == 'vxlan' else row['network_type']
            expected_mtu = row['mtu']-data['mtu_delta'] if row['network_type'] == 'vxlan' else row['mtu']
            if (n.id, n.provider_network_type, n.mtu) != (row['id'], expected_type, expected_mtu):
                raise RuntimeError('Pre-existing network conversion/MTU differs: '+row['id'])
        for row in data['subnets']:
            sub = cloud.network.get_subnet(row['id'])
            if any(getattr(sub, k) != v for k, v in row.items()):
                raise RuntimeError('Pre-existing subnet changed: '+row['id'])
        for row in data['routers']:
            if gateway(cloud.network.get_router(row['id'])) != row['gateway']:
                raise RuntimeError('Pre-existing router gateway changed: '+row['id'])
        for row in data['floating_ips']:
            if ip_shape(cloud.network.get_ip(row['id'])) != row:
                raise RuntimeError('Pre-existing FIP association changed: '+row['id'])
        for row in data['compute_ports']:
            if port_shape(cloud.network.get_port(row['id'])) != row:
                raise RuntimeError('Pre-existing VM port/IP/placement changed: '+row['id'])
        for row in data['servers']:
            server = cloud.compute.get_server(row['id'])
            if server.id != row['id'] or server.status != row['status'] or server.compute_host != row['compute_host']:
                raise RuntimeError('Pre-existing VM identity/status/compute changed: '+row['id'])
        result.update(status='PASS', retained={k:[r['id'] for r in data[k]] for k in
            ('networks','subnets','routers','floating_ips','compute_ports','servers')},
            shared_external=data['shared_external'])
    except Exception as exc:
        result['errors'].append(str(exc))
    save(run_path(root, 'preexisting-resource-preservation.json'), result)
    if result['status'] != 'PASS':
        raise RuntimeError('; '.join(result['errors']))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['before', 'verify'])
    parser.add_argument('root', type=Path)
    parser.add_argument('--scope', default='after-migration')
    args = parser.parse_args()
    import openstack
    cloud = openstack.connect()
    cloud.compute.default_microversion = '2.74'
    print(json.dumps(before(cloud, args.root) if args.action == 'before' else verify(cloud, args.root, args.scope)))
