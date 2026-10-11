#!/usr/bin/env python3
"""Resolve exact inventory -> Nova -> Neutron identities before workload writes."""
import json
import pathlib
import sys


def resolve(cloud, request):
    aliases = request['pair_hosts']
    if len(aliases) != 2 or len(set(aliases)) != 2 or any(a not in request['compute_hosts'] for a in aliases):
        raise RuntimeError('Pair placement requires two distinct inventory compute hosts')
    services = list(cloud.compute.services(binary='nova-compute'))
    hypervisors = list(cloud.compute.hypervisors(details=True))
    agents = list(cloud.network.agents(agent_type='Open vSwitch agent'))
    result = {}
    for index, alias in enumerate(aliases):
        names = {alias, *request['identities'][alias]}
        matches = [s for s in services if s.host in names]
        ovs = [a for a in agents if a.host in names]
        if len(matches) != 1 or len(ovs) != 1:
            raise RuntimeError(f'Ambiguous/missing Nova or OVS identity for {alias}')
        service, agent = matches[0], ovs[0]
        nodes = [h for h in hypervisors if h.service_details and h.service_details.get('host') == service.host]
        if (service.status != 'enabled' or service.state != 'up' or
                not agent.is_alive or not agent.is_admin_state_up or len(nodes) != 1 or
                nodes[0].state != 'up' or nodes[0].status != 'enabled'):
            raise RuntimeError(f'Compute/OVS readiness failed for {alias}')
        zone = service.availability_zone
        node = nodes[0].name
        if not node:
            raise RuntimeError('Invalid Nova availability zone/host/hypervisor identity')
        result[str(index)] = dict(inventory_host=alias, nova_host=service.host,
                                 neutron_host=agent.host, hypervisor=node,
                                 availability_zone=zone)
    if len({r['nova_host'] for r in result.values()}) != 2 or len({r['neutron_host'] for r in result.values()}) != 2:
        raise RuntimeError('Placement resolves both pair members to the same compute')
    return result


def check(server, port, target):
    return dict(status='PASS' if (server.compute_host == target['nova_host'] and
                                 server.hypervisor_hostname == target['hypervisor'] and
                                 port.binding_host_id == target['neutron_host']) else 'FAIL',
                expected=target, nova_host=server.compute_host,
                hypervisor=server.hypervisor_hostname, neutron_host=port.binding_host_id)


def main():
    import openstack
    root = pathlib.Path(sys.argv[1])
    cloud = openstack.connect()
    cloud.compute.default_microversion = '2.74'
    result = resolve(cloud, json.loads(sys.argv[2]))
    from workload_validation import save
    save(root/'validation-placement.json', result)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
