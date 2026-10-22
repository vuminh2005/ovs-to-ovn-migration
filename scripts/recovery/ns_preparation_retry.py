#!/usr/bin/env python3
"""Permit only phase-04 failure before the first endpoint capture launch."""

# Source helpers are grouped; remote/guest copies keep flat sibling imports.
import sys as _sys
from pathlib import Path as _Path
_source = _Path(__file__).resolve().parents[1]
if _source.name == 'scripts' and (_source / '_paths.py').is_file():
    _sys.path.insert(0, str(_source))
    from _paths import configure as _configure
    _configure()
from run_layout import run_path
import argparse
import json
from pathlib import Path

from phase_schema import CANONICAL_SCHEMA, schema_version, timestamp


def retry_check(root, inventory):
    root = Path(root)
    if not root.is_absolute() or schema_version(root) != CANONICAL_SCHEMA:
        raise ValueError('N-S preparation retry requires an absolute schema-2 checkpoint')
    runtime = json.loads((run_path(root, 'runtime.json')).read_text())
    cfg = json.loads((run_path(root, 'validation-config.json')).read_text())
    state = json.loads((run_path(root, 'validation-resources.json')).read_text())
    extended = json.loads((run_path(root, 'extended-resources.json')).read_text())
    diagnostic = json.loads((run_path(root, 'ns-diagnostics-checkpoint.json')).read_text())
    if (runtime.get('run_id') != root.name or cfg.get('run') != root.name or
        runtime.get('inventory') != inventory or cfg.get('inventory') != inventory):
        raise ValueError('N-S retry run/inventory mismatch')
    if any(timestamp(run_path(root, 'metrics')/f'phase{phase:02d}.end') is None for phase in range(4)):
        raise ValueError('N-S retry requires completed phases 00–03')
    if timestamp(run_path(root, 'metrics/phase04.start')) is None or (run_path(root, 'metrics/phase04.end')).exists():
        raise ValueError('N-S retry requires incomplete phase 04')
    forbidden = ['total.end', 'control_plane_downtime.start', 'control_plane_downtime.end',
                 'db_migration.start', 'db_migration.end', 'dataplane_convergence.start', 'dataplane_convergence.end']
    forbidden += [f'phase{phase:02d}.{edge}' for phase in range(5,14) for edge in ('start','end')]
    if any((run_path(root, 'metrics')/name).exists() for name in forbidden):
        raise ValueError('N-S retry is forbidden after staging, target configuration or freeze')
    pre = state.get('pre', {})
    pair = pre.get('measure', {})
    if (state.get('schema_version') != 2 or state.get('historical_dual_pair') or state.get('post') or
        pre.get('cleanup_started') or pre.get('cleaned') or pre.get('existing') or pre.get('tcp') or
        set(pair) != {'0','1'} or any(vm.get('owned') is not True or not vm.get('port') or not vm.get('server') for vm in pair.values())):
        raise ValueError('N-S retry requires the original running Pair A, before later pairs')
    ns = cfg.get('ns', {})
    if (not ns.get('enabled') or not ns.get('diagnostics_enabled') or
        cfg.get('placement_enabled') is not True or set(cfg.get('placement', {})) != {'0','1'}):
        raise ValueError('N-S retry requires the original diagnostic and placement configuration')
    nstate = extended.get('ns', {})
    if (extended.get('schema') != 1 or extended.get('run') != root.name or extended.get('processes') or
        extended.get('cleaned') or not nstate.get('router') or set(nstate.get('vms', {})) != {'0','1'} or
        any(vm.get('server') or vm.get('owned') is not True or not vm.get('port') for vm in nstate['vms'].values())):
        raise ValueError('N-S retry is only supported before any N-S guest or local probe launch')
    if any((run_path(root, name)).exists() for name in ('ns-baseline.json','ns-precutover.json','ns-results.json')):
        raise ValueError('N-S session evidence already exists; preserve it instead of retrying')
    if diagnostic.get('schema') != 1 or diagnostic.get('run') != root.name or set(diagnostic.get('captures', {})) != {'endpoint'}:
        raise ValueError('N-S retry requires exactly one endpoint launch intent')
    cp = diagnostic['captures']['endpoint']
    if cp.get('status') != 'START_INTENT' or cp.get('remote') or cp.get('host') != 'localhost':
        raise ValueError('N-S retry cannot replace a previously launched capture')
    directory = cp.get('config', {}).get('directory')
    if directory not in [str(run_path(root, 'ns-diagnostics')/name) for name in ('endpoint', 'endpoint-agent')]:
        raise ValueError('N-S endpoint directory identity changed')
    expected = dict(run=root.name, kind='endpoint', endpoint_ip=ns['endpoint_ip'], snat_ip=ns['snat_ip'],
                    fip_ip=ns['fip_ip'], guest_ip=ns['outbound_ip'], tcp_port=ns['port'],
                    lifetime=cfg['lifetime'], allow_create=True, interface=ns['capture_interface'],
                    directory=directory)
    if ns.get('new_port'):
        expected.update(new_tcp_port=ns['new_port'], icmp_enabled=ns.get('icmp_enabled', False))
    if cp.get('config') != expected:
        raise ValueError('N-S endpoint intent differs from original configuration')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('--inventory', required=True)
    args = parser.parse_args()
    retry_check(args.root, args.inventory)
    print('N-S phase-04 endpoint pre-launch checkpoint is eligible for guarded retry')
