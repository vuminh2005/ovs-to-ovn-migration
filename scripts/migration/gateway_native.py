#!/usr/bin/env python3
"""Isolated Neutron 24.x gateway scheduler; never run the DB synchronizer.

Imports stay inside bootstrap: this file is also inspected on the controller,
which need not have Neutron installed. The preparation action creates no IDL,
plugin manager, API listener or RPC worker.
"""
import argparse
import importlib.metadata
import json
from pathlib import Path
import signal
import sys
import time


RESULT_PREFIX = 'GATEWAY_RESULT='


class GatewayCheck(RuntimeError):
    """A diagnostic constructed here, safe to include in evidence."""


def configuration():
    from neutron.cmd.ovn import neutron_ovn_db_sync_util as sync
    from neutron.services.ovn_l3 import plugin
    from neutron.plugins.ml2.drivers.ovn.mech_driver.ovsdb import impl_idl_ovn, ovn_client

    version = importlib.metadata.version('neutron')
    if version.split('.')[0] != '24':
        raise GatewayCheck('Only the Neutron 24.x scheduler bootstrap is supported')
    for cls, names in (
        (plugin.OVNL3RouterPlugin, ('schedule_unhosted_gateways', '_get_gateway_port_physnet_mapping')),
        (impl_idl_ovn.OvsdbNbOvnIdl, ('get_gateway_chassis_binding', 'get_gateway_chassis_az_hints',
                                   'get_unhosted_gateways', 'schedule_unhosted_gateways')),
        (impl_idl_ovn.OvsdbSbOvnIdl, ('get_chassis_and_physnets', 'get_gateway_chassis_from_cms_options',
                                   'get_chassis_and_azs')),
        (ovn_client.OVNClient, ('get_candidates_for_scheduling',)),
    ):
        if any(not callable(getattr(cls, name, None)) for name in names):
            raise GatewayCheck('Native gateway scheduling API is incompatible')
    conf = sync.setup_conf()
    conf(args=['--config-file', '/etc/neutron/neutron.conf', '--config-file',
               '/etc/neutron/plugins/ml2/ml2_conf.ini'], project='neutron')
    if not (conf.core_plugin == 'ml2' or conf.core_plugin.endswith('.Ml2Plugin')):
        raise GatewayCheck('Gateway helper requires ML2')
    if 'ovn' not in conf.ml2.mechanism_drivers:
        raise GatewayCheck('Target configuration does not enable OVN')
    if not any('ovn' in item.lower() for item in conf.service_plugins):
        raise GatewayCheck('Target configuration does not enable OVN L3')
    return conf, version


def bootstrap(conf):
    # Same non-server ML2 driver and MaintenanceWorker IDLs as the native
    # db-sync utility. Deliberately do not instantiate either synchronizer.
    from neutron import manager
    from neutron_lib.plugins import constants, directory
    from neutron.plugins.ml2.drivers.ovn.mech_driver.ovsdb import impl_idl_ovn, worker

    conf.core_plugin = 'neutron.cmd.ovn.neutron_ovn_db_sync_util.Ml2Plugin'
    conf.set_override('mechanism_drivers', ['ovn-sync'], 'ml2')
    conf.service_plugins = ['neutron.services.ovn_l3.plugin.OVNL3RouterPlugin',
                            'neutron.services.segments.plugin.Plugin', 'port_forwarding', 'qos']
    conf.set_override('extension_drivers', list(set(conf.ml2.extension_drivers + ['qos'])), 'ml2')
    conf.set_override('ovsdb_connection_timeout', 10, 'ovn')
    nb = impl_idl_ovn.OvsdbNbOvnIdl.from_worker(worker.MaintenanceWorker)
    sb = impl_idl_ovn.OvsdbSbOvnIdl.from_worker(worker.MaintenanceWorker)
    manager.init()
    core = directory.get_plugin()
    driver = core.mechanism_manager.mech_drivers['ovn-sync']
    core.mechanism_manager.mech_drivers['ovn'] = driver
    driver.obj.nb_ovn, driver.obj.sb_ovn = nb, sb
    driver.obj._post_fork_event.set()
    return directory.get_plugin(constants.L3), core, nb, sb


def identity_snapshot(core, l3, context):
    """Frozen gateway identity, including external gateway info, never secrets."""
    fields = ('id', 'network_id', 'device_id', 'device_owner', 'mac_address', 'fixed_ips')
    ports = core.get_ports(context, filters={'device_owner': ['network:router_gateway']})
    result = []
    for port in sorted(ports, key=lambda row: row['id']):
        row = {key: port[key] for key in fields}
        row['fixed_ips'] = sorted(row['fixed_ips'], key=lambda item: (item['subnet_id'], item['ip_address']))
        row['external_gateway_info'] = l3.get_router(context, port['device_id']).get('external_gateway_info')
        result.append(row)
    return result


def gateway_plan(l3, nb, sb, topology, before):
    mapping = l3._get_gateway_port_physnet_mapping()
    if set(mapping) != {row['id'] for row in before}:
        raise GatewayCheck('Not every frozen router gateway belongs to the OVN provider')
    if 'Gateway_Chassis' not in nb.idl.tables:
        raise GatewayCheck('Gateway_Chassis schema is required')
    known = {row['system_id']: row for row in topology.values()}
    if len(known) != len(topology) or not known:
        raise GatewayCheck('Inventory chassis identities are missing or duplicated')
    physnets = sb.get_chassis_and_physnets()
    cms = sb.get_gateway_chassis_from_cms_options()
    plan = {}
    for port, physnet in sorted(mapping.items()):
        name = 'lrp-' + port
        nb.lookup('Logical_Router_Port', name)  # Missing LRPs must not be created here.
        hints = nb.get_gateway_chassis_az_hints(name)
        candidates = set(l3._ovn_client.get_candidates_for_scheduling(
            physnet, cms=cms, chassis_physnets=physnets, availability_zone_hints=hints))
        if not candidates or not candidates <= set(known):
            raise GatewayCheck('No safe inventory candidates for gateway ' + port)
        for chassis in candidates:
            row = sb.lookup('Chassis', chassis)
            if not any(encap.type == 'geneve' and encap.ip == known[chassis]['tunnel_ip']
                       for encap in row.encaps):
                raise GatewayCheck('Candidate Geneve identity differs from the source checkpoint')
        existing = nb.get_gateway_chassis_binding(name)
        plan[name] = dict(port_id=port, physnet=physnet, az_hints=hints,
                          candidates=sorted(candidates), before=list(existing))
    return plan


def schedule(l3, core, nb, sb, topology, context):
    before = identity_snapshot(core, l3, context)
    plan = gateway_plan(l3, nb, sb, topology, before)
    # Use the native candidate selection, AZ filtering, HA priorities and
    # primary-preservation transaction. No hand-written Gateway_Chassis rows.
    l3.schedule_unhosted_gateways()
    for name, row in plan.items():
        assigned = list(nb.get_gateway_chassis_binding(name))
        if not assigned or not set(assigned) <= set(row['candidates']):
            raise GatewayCheck('Gateway scheduling verification failed for ' + name)
        if row['before'] and row['before'][0] in row['candidates'] and assigned[0] != row['before'][0]:
            raise GatewayCheck('A valid gateway primary was moved')
        row['after'] = assigned
        # SB convergence is evidence only: do not wait for it with the API down.
        binding = sb.lookup('Port_Binding', 'cr-' + name, default=None)
        row['redirect_chassis'] = [ch.name for ch in binding.chassis] if binding else []
        if binding and 'up' in sb.idl.tables['Port_Binding'].columns:
            row['redirect_up'] = binding.up
    if identity_snapshot(core, l3, context) != before:
        raise GatewayCheck('Frozen Neutron gateway identities changed during scheduling')
    return dict(status='PASS', gateway_count=len(plan), changed_count=sum(
        row['before'] != row['after'] for row in plan.values()), gateways=plan)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('preflight', 'schedule'))
    parser.add_argument('--topology', type=Path, required=True)
    parser.add_argument('--timeout', type=int, default=25)
    args = parser.parse_args(argv)
    if not 1 <= args.timeout <= 55:
        parser.error('Timeout must be between 1 and 55 seconds')
    def expired(signum, frame):
        raise TimeoutError('Gateway helper deadline')
    signal.signal(signal.SIGALRM, expired)
    signal.alarm(args.timeout)
    started = time.monotonic()
    stage = 'configuration'
    try:
        topology = json.loads(args.topology.read_text())
        conf, version = configuration()
        if args.action == 'preflight':
            result = dict(status='PASS', neutron_version=version, bootstrap='native-ovn-sync')
        else:
            from neutron_lib import context
            stage = 'bootstrap'
            plugins = bootstrap(conf)
            stage = 'schedule-and-verify'
            result = schedule(*plugins, topology, context.get_admin_context())
            result['neutron_version'] = version
    except Exception as exc:
        # Database/config exceptions can contain passwords. Never print repr/str.
        result = dict(status='FALLBACK', stage=stage, error_type=type(exc).__name__)
        if isinstance(exc, GatewayCheck):
            result['reason'] = str(exc)
    result['elapsed_seconds'] = round(time.monotonic() - started, 6)
    print(RESULT_PREFIX + json.dumps(result, sort_keys=True), flush=True)
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    sys.exit(main())
