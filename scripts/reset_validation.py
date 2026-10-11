#!/usr/bin/env python3
"""Scope and completion checks for repeatable four-node Kolla lab resets."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from datetime import datetime, timezone

from workload_validation import save
import validation_prerequisites as prerequisites

EXPECTED = {'control': {'controller'}, 'network': {'network1'},
            'compute': {'compute1', 'compute2'}}


def run(args):
    # Let normal CLI diagnostics reach the operator; never enable debug logging.
    result = subprocess.run(args, stdout=subprocess.PIPE, text=True)
    if result.returncode:
        raise RuntimeError(f'{" ".join(args[:3])} failed (rc={result.returncode})')
    return result.stdout


def members(data, group, seen=None):
    seen = set() if seen is None else seen
    if group in seen:
        return set()
    seen.add(group)
    value = data.get(group, {})
    hosts = set(value.get('hosts', []))
    for child in value.get('children', []):
        hosts.update(members(data, child, seen))
    return hosts


def validate_inventory(data):
    for group, expected in EXPECTED.items():
        if members(data, group) != expected:
            raise RuntimeError(f'Reset requires {group}={sorted(expected)}')
    allowed = set().union(*EXPECTED.values()) | {'localhost', '127.0.0.1'}
    extra = members(data, 'all') - allowed
    if extra:
        raise RuntimeError(f'Unexpected reset inventory hosts: {sorted(extra)}')
    if not {'ovn-controller', 'ovn-database', 'neutron-ovn-metadata-agent'} <= data.keys():
        raise RuntimeError('Use the full Kolla inventory, including OVN groups')


def scope(inventory):
    data = json.loads(run(['ansible-inventory', '-i', str(inventory), '--list']))
    validate_inventory(data)
    print('RESET_4NODE_SCOPE_VERIFIED')


def cloud_json(*args):
    return json.loads(run(['openstack', *args, '-f', 'json']))


def validate_cloud():
    for noun, extra in (('server', ['--all-projects']), ('network', []), ('subnet', []), ('router', [])):
        if cloud_json(noun, 'list', *extra):
            raise RuntimeError(f'Cloud must have no workload {noun} resources after reset')
    services = [r for r in cloud_json('compute', 'service', 'list') if r.get('Binary') == 'nova-compute']
    if len(services) != 2 or {r.get('Host') for r in services} != EXPECTED['compute']:
        raise RuntimeError('Expected exactly compute1 and compute2 Nova services')
    if any(r.get('Status') != 'enabled' or r.get('State') != 'up' for r in services):
        raise RuntimeError('Both Nova computes must be enabled/up')
    agents = cloud_json('network', 'agent', 'list')
    expected = {('Open vSwitch agent', host) for host in ('network1', 'compute1', 'compute2')}
    expected |= {(kind, 'network1') for kind in ('L3 agent', 'DHCP agent', 'Metadata agent')}
    if len(agents) != 6 or {(r.get('Agent Type'), r.get('Host')) for r in agents} != expected:
        raise RuntimeError('Expected three OVS agents and DHCP/L3/metadata only on network1')
    if any(r.get('Alive') not in (True, ':-)', 'True') or r.get('State') not in ('UP', True, 'enabled') for r in agents):
        raise RuntimeError('All six source Neutron agents must be alive/enabled')


def verify_image(image, provenance):
    if image.get('status') != 'active' or image.get('disk_format') != 'qcow2' or image.get('container_format') != 'bare':
        raise RuntimeError(f'Validation image must be ACTIVE qcow2/bare: status={image.get("status")}, format={image.get("disk_format")}')
    if int(image.get('size') or 0) <= 0:
        raise RuntimeError('Validation image has no content')
    if provenance.get('id') == image['id'] and provenance.get('cache_path'):
        path = Path(provenance['cache_path'])
        if prerequisites.digest(path) != provenance['sha256']:
            raise RuntimeError('Verified Ubuntu cache checksum changed')
        algorithm = image.get('os_hash_algo') if image.get('os_hash_value') else 'md5'
        expected = image.get('os_hash_value') or image.get('checksum')
        if not algorithm or not expected:
            raise RuntimeError('Glance image has no content checksum')
        actual = hashlib.new(algorithm)
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                actual.update(block)
        if path.stat().st_size != int(image['size']) or actual.hexdigest().lower() != str(expected).lower():
            raise RuntimeError('Glance content differs from verified Ubuntu download')


def finish(root, cloud):
    marker = root / 'reset-complete.json'
    if marker.exists():
        raise RuntimeError('This reset snapshot already completed; start a new reset for a new cycle')
    validate_cloud()
    cfg = json.loads((root / 'validation-prerequisites-config.json').read_text())
    prior_path = root / 'validation-prerequisites.json'
    prior = json.loads(prior_path.read_text()) if prior_path.exists() else {}
    result = prerequisites.prepare(cloud, cfg, root)
    image = cloud_json('image', 'show', result['image']['id'])
    provenance = result['image'] if result['image'].get('cache_path') else prior.get('image', {})
    verify_image(image, provenance)
    # Preserve provenance during a retry that reuses the partially prepared image.
    if provenance.get('id') == image['id']:
        result['image'].update({k: v for k, v in provenance.items() if k not in ('status', 'created')})
    save(prior_path, result)
    validate_cloud()
    vars_path = root / 'migration-lab.yml'
    save(vars_path, {'validation_image': image['id'],
                    'validation_flavor': result['flavor']['id'],
                    'target_geneve_mtu': cfg['target_geneve_mtu']})
    save(marker, {'status': 'OVS_4NODE_BASELINE_READY',
                 'completed_at': datetime.now(timezone.utc).isoformat(),
                 'source_mtu': cfg['source_mtu'], 'target_mtu': cfg['target_geneve_mtu'],
                 'image': result['image'], 'flavor': result['flavor'],
                 'variables': str(vars_path)})
    print(json.dumps({'status': 'OVS_4NODE_BASELINE_READY', 'variables': str(vars_path),
                      'image': result['image']['name'], 'flavor': result['flavor']['name']}))


def main():
    p = argparse.ArgumentParser()
    p.add_argument('action', choices=('scope', 'finish'))
    p.add_argument('path', type=Path)
    args = p.parse_args()
    if args.action == 'scope':
        scope(args.path)
    else:
        import openstack
        finish(args.path, openstack.connect())


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        raise SystemExit(f'STOP: {error}')
