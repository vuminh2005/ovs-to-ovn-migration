#!/usr/bin/env python3
"""Read-only API/service/source-MTU gate before new-generation EW provisioning."""
import json
import pathlib
import sys

from reset_host_preflight import require
from reset_workflow import save


def check(cloud, spec, hosts, expected, observed, state):
    for key in ('validation_source_mtu', 'validation_target_mtu', 'geneve_header_bytes'):
        require(observed[key] == expected[key], 'Rebuilt effective MTU differs from reviewed source plan: '+key)
    for key in ('global_physnet_mtu', 'path_mtu', 'overlay_ip_version'):
        require(all(c[key] == next(iter(expected['inputs']['source_configs'].values()))[key]
                    for c in observed['inputs']['source_configs'].values()), 'Rebuilt source setting changed: '+key)
    require(set(observed['inputs']['source_configs']) == set(spec['control']) and
            set(observed['inputs']['underlay']) == set(spec['network']+spec['compute']), 'Incomplete source host MTU collection')
    for values in observed['inputs']['source_configs'].values():
        require('openvswitch' in {v.strip() for v in values['mechanism_drivers'].split(',')} and
                'ovn' not in values['mechanism_drivers'] and values['tenant_network_types'].strip() == 'vxlan',
                'Effective source ML2 mechanism/type differs from OVS/VXLAN')
    for host in spec['compute']:
        services = [s for s in cloud.compute.services(binary='nova-compute') if s.host == host]
        require(len(services) == 1 and services[0].state == 'up' and services[0].status == 'enabled', 'Required compute placement unavailable: '+host)
    agents = list(cloud.network.agents())
    for host in spec['network']+spec['compute']:
        hostname = hosts[host]['hostname']
        required = ['Open vSwitch agent']+(['L3 agent', 'DHCP agent', 'Metadata agent'] if host in spec['network'] else [])
        for kind in required:
            matches = [a for a in agents if a.host == hostname and a.agent_type == kind]
            require(len(matches) == 1 and matches[0].is_alive and matches[0].is_admin_state_up,
                    'Required OVS source agent unavailable: '+host+' / '+kind)
    owned = set(state.get('resources', {}).values())
    for server in cloud.compute.servers(all_projects=True):
        require(server.id in owned, 'Rebuilt source has an uncheckpointed server; scope ambiguous')
    for network in cloud.network.networks():
        require(network.id in owned and network.provider_network_type == 'vxlan' and not network.is_router_external and
                network.mtu == expected['validation_source_mtu'], 'Rebuilt source network is unowned/incompatible; reduced MTU cannot become source')
    for router in cloud.network.routers():
        require(router.id in owned and not router.external_gateway_info and not router.is_distributed and not router.is_ha,
                'Unowned or unsupported router after reset')
    require(not list(cloud.network.ips()), 'Floating IPs remain outside reset/EW scope')
    return dict(status='PASS', source_mtu=observed['validation_source_mtu'], target_mtu=observed['validation_target_mtu'],
                api_services_and_placement='PASS', scope='empty cloud or exact generation-checkpointed partial provisioning')


def main():
    generation, run = map(pathlib.Path, sys.argv[1:3])
    spec = json.loads((generation/'retained-inputs.json').read_text())['spec']
    hosts = {h:json.loads((generation/'hosts'/h).read_text()) for h in spec['hosts']}
    state_path = generation/'provisioning/resources.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    import openstack
    result = check(openstack.connect(api_timeout=15), spec, hosts,
                   json.loads((generation/'source-mtu-plan.json').read_text()),
                   json.loads((run/'mtu-calculation.json').read_text()), state)
    result.update(evidence=str(run), generation=spec['generation'])
    save(generation/'source-readiness.json', result)


if __name__ == '__main__': main()
