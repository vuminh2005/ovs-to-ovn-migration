#!/usr/bin/env python3
"""Shared, flat-staged rollback ownership, atomic records and business projection."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

BASE = Path('/var/lib/ovs-to-ovn-rollback')
UNITS = {
    name: 'kolla-' + name + '-container.service' for name in (
        'neutron_server', 'neutron_openvswitch_agent', 'neutron_l3_agent',
        'neutron_dhcp_agent', 'neutron_metadata_agent', 'ovn_controller',
        'neutron_ovn_metadata_agent')}
LEGACY = tuple(n for n in UNITS if n.startswith('neutron_') and n != 'neutron_ovn_metadata_agent')


class Stop(RuntimeError):
    """A safe diagnostic authored by the rollback code, never a DB exception."""


def run_id(value):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', value):
        raise Stop('Invalid run identity')
    return value


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''): digest.update(chunk)
    return digest.hexdigest()


def save(path, value):
    path = Path(path)
    if path.is_symlink() or path.parent.is_symlink(): raise Stop('Unsafe evidence path')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.tmp-' + str(os.getpid()))
    with open(temporary, 'w', opener=lambda p, f: os.open(p, f, 0o600)) as output:
        json.dump(value, output, indent=2, sort_keys=True)
        output.write('\n'); output.flush(); os.fsync(output.fileno())
    temporary.replace(path)


def journal(path, stage, edge, **extra):
    path = Path(path)
    if path.is_symlink(): raise Stop('Unsafe journal path')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path, 'a', opener=lambda p, f: os.open(p, f, 0o600)) as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.write(json.dumps(dict(stage=stage, edge=edge, time=time.time(), **extra), sort_keys=True) + '\n')
        stream.flush(); os.fsync(stream.fileno())


def command(argv, timeout=30, input=None, check=True):
    result = subprocess.run(argv, input=input, text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        # SQL errors and command arguments can contain credentials.
        raise Stop('Command failed: ' + Path(argv[0]).name + ' (rc=' + str(result.returncode) + ')')
    return result


def owned(run, host=None, create=False):
    directory = BASE / run_id(run)
    if directory.is_symlink(): raise Stop('Rollback directory is a symlink')
    owner = directory / 'owner.json'
    if create:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        if not owner.exists(): save(owner, dict(schema=1, run=run, host=host))
    value = json.loads(owner.read_text())
    if value.get('schema') != 1 or value.get('run') != run or (host and value.get('host') != host):
        raise Stop('Rollback ownership differs')
    return directory


def no_migration_process():
    for path in Path('/proc').glob('[0-9]*/cmdline'):
        try: args = path.read_bytes().split(b'\0')
        except (FileNotFoundError, PermissionError, ProcessLookupError): continue
        if any(Path(a.decode(errors='replace')).name == 'migrate-to-ovn.yml' for a in args):
            raise Stop('A migration invocation is still alive; stop it and its helpers before rollback')
        if any(Path(a.decode(errors='replace')).name == 'kolla-ansible' for a in args):
            raise Stop('A Kolla invocation is still alive; stop deployment writers before rollback')


# Only implementation state changed by the migration or ordinary agent activity
# is omitted. Every unknown table/column is retained (fail closed on new drift).
IMPLEMENTATION_TABLES = {
    'agents', 'ovn_revision_numbers', 'ovn_hash_ring', 'quotausages',
    'reservations', 'resourcedeltas', 'ml2_vxlan_allocations', 'ml2_geneve_allocations',
    'routerl3agentbindings', 'networkdhcpagentbindings', 'ha_router_agent_port_bindings',
    'ha_router_networks', 'ha_router_vr_id_allocations', 'ml2_distributed_port_bindings',
    'ipamallocations', 'ipamallocationpools', 'ipamavailabilityranges'}
SERVICE_OWNERS = {'network:dhcp', 'network:distributed', 'network:router_centralized_snat', 'network:ha_router_replicated_interface'}


def business_projection(tables):
    service = {r['id'] for r in tables.get('ports', []) if r.get('device_owner') in SERVICE_OWNERS}
    attrs = {r.get('standard_attr_id') for r in tables.get('ports', []) if r['id'] in service}
    subports = {r.get('port_id') for r in tables.get('subports', [])}
    result = {}
    for table, rows in sorted(tables.items()):
        if table in IMPLEMENTATION_TABLES: continue
        projected = []
        for original in rows:
            row = dict(original)
            if ((table == 'ports' and row['id'] in service) or row.get('port_id') in service or
                    (table == 'standardattributes' and row.get('id') in attrs)):
                continue
            if table in ('ports', 'networks', 'routers'): row.pop('status', None)
            if table == 'standardattributes':
                for field in ('revision_number', 'created_at', 'updated_at'): row.pop(field, None)
            if table == 'networksegments' and row.get('network_type') in ('vxlan', 'geneve'):
                row['network_type'] = 'overlay-vxlan-geneve'
            if table == 'providerresourceassociations' and row.get('provider_name') in ('single_node', 'ha', 'dvr', 'dvrha', 'ovn'):
                row['provider_name'] = 'migrated-l3-provider'
            if table == 'ml2_port_bindings':
                for field in ('vif_type', 'vif_details', 'status'): row.pop(field, None)
                if row.get('port_id') in subports and row.get('profile'):
                    profile = json.loads(row['profile']) if isinstance(row['profile'], str) else dict(row['profile'])
                    for key in ('parent_name', 'tag'): profile.pop(key, None)
                    row['profile'] = profile
            if table == 'ml2_port_binding_levels' and row.get('driver') in ('openvswitch', 'ovn'):
                row['driver'] = 'migrated-ml2-driver'
            projected.append(row)
        result[table] = sorted(projected, key=lambda r: json.dumps(r, sort_keys=True))
    return result


def business_sha(tables):
    return hashlib.sha256(json.dumps(business_projection(tables), sort_keys=True, separators=(',', ':')).encode()).hexdigest()
