#!/usr/bin/env python3
"""Effective IPv4 overlay limits, per-network preparation and retry evidence."""
import argparse
import configparser
import datetime
import json
import pathlib
import re
import uuid

from dataplane_capture import save


def config_values(neutron, ml2):
    n = configparser.ConfigParser(interpolation=None)
    m = configparser.ConfigParser(interpolation=None)
    n.read_string(neutron); m.read_string(ml2)
    return dict(global_physnet_mtu=n.getint('DEFAULT', 'global_physnet_mtu', fallback=1500),
                path_mtu=m.getint('ml2', 'path_mtu', fallback=0),
                overlay_ip_version=m.getint('ml2', 'overlay_ip_version', fallback=4),
                geneve_max_header_size=m.getint('ml2_type_geneve', 'max_header_size', fallback=30),
                mechanism_drivers=m.get('ml2', 'mechanism_drivers', fallback=''),
                tenant_network_types=m.get('ml2', 'tenant_network_types', fallback=''))


def template_header(text):
    section = re.search(r'^\[ml2_type_geneve\]\s*\n(.*?)(?=^\[|\Z)', text, re.M | re.S)
    values = re.findall(r'^max_header_size\s*=\s*(\d+)\s*$', section[1], re.M) if section else []
    if len(values) != 1:
        raise RuntimeError('Installed Kolla Geneve template must expose one literal max_header_size; configure the correct template path')
    return int(values[0])


def require_geneve_header(value):
    if type(value) is not int or value < 38:
        raise RuntimeError('OVN Geneve max_header_size must be an integer of at least 38 bytes; refusing to clamp it')


def validate_plan_header(plan):
    require_geneve_header(plan['inputs']['geneve_max_header_size'])
    require_geneve_header(plan['geneve_header_bytes'])
    if plan['inputs']['geneve_max_header_size'] != plan['geneve_header_bytes']:
        raise RuntimeError('Saved Geneve header contradicts the MTU calculation inputs')


def calculate(inputs):
    configs = inputs['source_configs']; underlay = inputs['underlay']
    if not configs or not underlay:
        raise RuntimeError('Missing effective source configuration or chassis tunnel-interface evidence')
    fields = ('global_physnet_mtu', 'path_mtu', 'overlay_ip_version')
    first = next(iter(configs.values()))
    if any(any(c[k] != first[k] for k in fields) for c in configs.values()):
        raise RuntimeError('Neutron controllers disagree on effective MTU configuration')
    if first['overlay_ip_version'] != 4:
        raise RuntimeError('MTU preparation supports the existing IPv4 tunnel scope only')
    if any(type(c['mtu']) is not int or c['mtu'] <= 0 or not c.get('ipv4') for c in underlay.values()):
        raise RuntimeError('Each network/compute tunnel interface needs a positive MTU and IPv4 evidence')
    global_mtu, path_mtu = first['global_physnet_mtu'], first['path_mtu']
    header = inputs['geneve_max_header_size']
    require_geneve_header(header)
    if type(global_mtu) is not int or global_mtu <= 0 or type(path_mtu) is not int or path_mtu < 0:
        raise RuntimeError('Invalid global/path MTU')
    physical = min(c['mtu'] for c in underlay.values())
    configured = min([global_mtu] + ([path_mtu] if path_mtu > 0 else []))
    effective = min(configured, physical)
    source, target = effective-20-30, effective-20-header
    if target < 1280:
        raise RuntimeError('Calculated Geneve MTU is below 1280; unsupported preparation')
    # A cap below Neutron's advertised maximum would allow unsafe future
    # auto-created networks. Require the config to account for the underlay.
    if configured > physical:
        raise RuntimeError('Neutron global/path MTU exceeds the observed tunnel-interface limit; correct configuration before migration')
    return dict(schema_version=1, inputs=inputs, configured_path_limit=configured,
                underlay_limit=physical, effective_path_limit=effective,
                ipv4_header_bytes=20, vxlan_header_bytes=30, geneve_header_bytes=header,
                overhead_delta=header-30, validation_source_mtu=source,
                validation_target_mtu=target, fresh_geneve_mtu=target,
                reasoning='min(global_physnet_mtu, positive path_mtu, every chassis tunnel-interface MTU) minus IPv4 header and type-driver encapsulation; each existing network retains its own original MTU minus the header delta, capped by the Geneve limit')


def journal_rows(path):
    result = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if not line.strip(): continue
            fields = line.split()
            if len(fields) != 3 or fields[0] in result:
                raise RuntimeError('Malformed/duplicate network MTU journal; refusing to guess original MTUs')
            result[fields[0]] = (int(fields[1]), int(fields[2]))
    return result


def prepare_networks(cloud, root):
    plan = json.loads((root/'mtu-calculation.json').read_text())
    validate_plan_header(plan)
    journal = root/'network-mtu-migration.tsv'
    recorded = journal_rows(journal)
    rows = []
    for network in cloud.network.networks():
        if network.provider_network_type != 'vxlan': continue
        original, target = recorded.get(network.id, (int(network.mtu), None))
        expected = min(original-plan['overhead_delta'], plan['fresh_geneve_mtu'])
        if target is None: target = expected
        if original <= 0 or target < 1280 or target != expected or int(network.mtu) not in (original, target):
            raise RuntimeError(f'Network {network.id} MTU/journal contradicts the saved calculation; no repeated reduction allowed')
        rows.append(dict(network=network.id, name=network.name, source_mtu=original,
                         target_mtu=target, observed_mtu=int(network.mtu), network_type='vxlan'))
    # Persist every original/target BEFORE any update: retries never subtract
    # from the already-reduced API value, including a lost API response.
    for row in rows: recorded[row['network']] = (row['source_mtu'], row['target_mtu'])
    tmp = journal.with_suffix('.tmp')
    tmp.write_text(''.join(f'{k}\t{a}\t{b}\n' for k,(a,b) in recorded.items()))
    tmp.chmod(0o600); tmp.replace(journal)
    save(root/'network-mtu-plan.json', dict(schema_version=1, networks=rows))
    for row in rows:
        if row['observed_mtu'] != row['target_mtu']:
            cloud.network.update_network(row['network'], mtu=row['target_mtu'])
        live = cloud.network.get_network(row['network'])
        if live.mtu != row['target_mtu']:
            raise RuntimeError(f'Network {row["network"]} did not retain its journaled target MTU')
    return rows


def verify_target(root, configurations, evidence_filename='mtu-target-config-verification.json', collection=None):
    plan = json.loads((root/'mtu-calculation.json').read_text())
    evidence = dict(configurations=configurations)
    if collection is not None:
        evidence['collection'] = collection
    try:
        validate_plan_header(plan)
        expected = next(iter(plan['inputs']['source_configs'].values()))
        if set(configurations) != set(plan['inputs']['source_configs']):
            raise RuntimeError('Generated target configuration missing a source controller')
        for host, values in configurations.items():
            require_geneve_header(values['geneve_max_header_size'])
            if (any(values[k] != expected[k] for k in ('global_physnet_mtu','path_mtu','overlay_ip_version')) or
                values['geneve_max_header_size'] != plan['geneve_header_bytes'] or
                values['mechanism_drivers'].strip() != 'ovn' or values['tenant_network_types'].strip() != 'geneve'):
                raise RuntimeError(f'Generated target config on {host} contradicts the MTU calculation or ML2/OVN scope')
        save(root/evidence_filename, dict(evidence, status='PASS'))
    except Exception as exc:
        save(root/evidence_filename, dict(evidence, status='FAIL', reason=str(exc)))
        raise


def needs_pre_freeze_collection(root):
    runtime_path=root/'runtime.json'
    runtime=json.loads(runtime_path.read_text()) if runtime_path.exists() else {}
    if 'mtu_plan_schema_version' not in runtime:
        return False  # historical runs retain their old preparation/evidence contract
    if type(runtime['mtu_plan_schema_version']) is not int or runtime['mtu_plan_schema_version']!=1:
        raise RuntimeError('Unsupported MTU plan schema; Neutron freeze prohibited')
    return True


def begin_pre_freeze_collection(root):
    if not needs_pre_freeze_collection(root):
        return dict(required=False)
    plan = json.loads((root/'mtu-calculation.json').read_text())
    validate_plan_header(plan)
    controllers = sorted(plan['inputs']['source_configs'])
    if not controllers:
        raise RuntimeError('Missing controller identities; Neutron freeze prohibited')
    intent = dict(required=True, schema_version=1, collection_id=str(uuid.uuid4()),
                  expected_controllers=controllers,
                  requested_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
    # A new token fences out Phase 06 facts and previous failed/retried collections.
    save(root/'mtu-pre-freeze-collection.json', intent)
    return intent


def require_pre_freeze(root):
    if not needs_pre_freeze_collection(root):
        return
    evidence_name = 'mtu-pre-freeze-target-config-verification.json'
    collection = None
    configurations = {}
    try:
        intent = json.loads((root/'mtu-pre-freeze-collection.json').read_text())
        collection = json.loads((root/'mtu-pre-freeze-target-configs.json').read_text())
        plan = json.loads((root/'mtu-calculation.json').read_text())
        expected = sorted(plan['inputs']['source_configs'])
        if (not intent.get('collection_id') or collection['collection_id'] != intent['collection_id'] or
            intent['expected_controllers'] != expected or not expected or
            sorted(collection['controllers']) != expected):
            raise RuntimeError('Fresh target collection is incomplete or belongs to another pre-freeze attempt')
        for host, record in collection['controllers'].items():
            if record['controller'] != host or record['collection_id'] != intent['collection_id']:
                raise RuntimeError('Controller identity/token mismatch in fresh target collection')
            timestamp = datetime.datetime.fromisoformat(record['collected_at'])
            if timestamp.tzinfo is None:
                raise RuntimeError('Fresh controller collection timestamp requires a timezone')
            configurations[host] = record['settings']
    except Exception as exc:
        reason = f'Fresh pre-freeze target configuration unavailable: {exc}; Neutron freeze prohibited'
        save(root/evidence_name, dict(status='FAIL', reason=reason, collection=collection))
        raise RuntimeError(reason) from exc
    verify_target(root, configurations, evidence_name, collection)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('calculate','prepare','verify','begin-pre-freeze','ready'))
    p.add_argument('root', type=pathlib.Path)
    args = p.parse_args()
    if args.action == 'calculate':
        inputs = json.loads((args.root/'mtu-inputs.json').read_text())
        save(args.root/'mtu-calculation.json', calculate(inputs))
    elif args.action == 'prepare':
        import openstack
        prepare_networks(openstack.connect(), args.root)
    elif args.action=='verify':
        configurations = json.loads((args.root/'mtu-target-configs.json').read_text())
        verify_target(args.root, configurations)
    elif args.action=='begin-pre-freeze':
        print(json.dumps(begin_pre_freeze_collection(args.root)))
    else:
        require_pre_freeze(args.root)


if __name__ == '__main__': main()
