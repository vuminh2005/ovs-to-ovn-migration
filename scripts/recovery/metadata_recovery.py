#!/usr/bin/env python3
"""Guard phase-06 metadata retry without repeating MTU changes or measurements."""
import sys as _sys
from pathlib import Path as _Path
_source = _Path(__file__).resolve().parents[1]
if _source.name == 'scripts' and (_source / '_paths.py').is_file():
    _sys.path.insert(0, str(_source))
    from _paths import configure as _configure
    _configure()

import argparse
import json
import os
from pathlib import Path
import time
import uuid

from phase_schema import timestamp
from pre_stage_recovery import read
from run_layout import run_path


def inspect(root, inventory):
    root = Path(root).resolve()
    runtime = read(root, 'runtime.json')
    if (runtime.get('phase_marker_schema_version') != 2 or runtime.get('run_id') != root.name
            or Path(runtime['inventory']).resolve() != Path(inventory).resolve()):
        raise ValueError('Recovery requires the original schema-2 run and inventory')
    if run_path(root, 'inventory.before').read_bytes() != Path(inventory).read_bytes():
        raise ValueError('Inventory changed after the source snapshot')
    metrics = run_path(root, 'metrics')
    previous = timestamp(metrics/'total.start')
    if previous is None:
        raise ValueError('Original total.start is absent')
    for phase in range(6):
        start, end = [timestamp(metrics/f'phase{phase:02d}.{edge}') for edge in ('start', 'end')]
        if start is None or end is None or not previous <= start <= end:
            raise ValueError('Completed ordered phases 00 through 05 are required')
        previous = end
    start = timestamp(metrics/'phase06.start')
    if start is None or start < previous or (metrics/'phase06.end').exists():
        raise ValueError('Recovery requires an incomplete phase 06')
    forbidden = [f'phase{phase:02d}.{edge}' for phase in range(7, 14) for edge in ('start', 'end')]
    forbidden += ['control_plane_downtime.start', 'control_plane_downtime.end',
                  'db_migration.start', 'db_migration.end', 'dataplane_convergence.start',
                  'dataplane_convergence.end', 'total.end']
    if any((metrics/name).exists() for name in forbidden):
        raise ValueError('Freeze or later phases already began')
    if any(run_path(root, name).exists() for name in (
            'db-sync-migrate.log', 'pair-d-trigger.json', 'ns-arm.json', 'migration-report.json')):
        raise ValueError('Later-phase evidence prohibits metadata recovery')
    preparation = read(root, 'metadata-preparation.json')
    if (preparation.get('schema') != 1 or preparation.get('run') != runtime['run_id']
            or preparation.get('status') not in ('FAIL', 'PASS')):
        raise ValueError('An original completed metadata preparation attempt is required')
    journal = run_path(root, 'network-mtu-migration.tsv').read_text()
    seen = set()
    for line in journal.splitlines():
        columns = line.split('\t')
        if len(columns) != 3:
            raise ValueError('Invalid original MTU journal')
        network, old, target = columns
        uuid.UUID(network)
        if network in seen or not 0 < int(target) < int(old):
            raise ValueError('Invalid or duplicated original MTU journal entry')
        seen.add(network)
    cfg = read(root, 'validation-config.json') if run_path(root, 'validation-resources.json').exists() else {}
    if cfg:
        state = read(root, 'validation-resources.json')
        if cfg.get('run') != runtime['run_id'] or cfg.get('inventory') != runtime['inventory']:
            raise ValueError('Original workload identity differs')
        if state.get('post') or any(state.get(stage, {}).get('cleanup_started') or
                state.get(stage, {}).get('cleaned') for stage in ('pre', 'post')):
            raise ValueError('Workload finalization or cleanup already began')
        checks = read(root, 'initial-workload-checks.json')
        fields = ('identity', 'active', 'bound', 'dhcp', 'connectivity', 'metadata', 'mtu')
        if set(checks) != {'0', '1'} or any(
                row.get(key) != 'PASS' for row in checks.values() for key in fields):
            raise ValueError('Initial workload evidence is not PASS')
        if cfg.get('capture_enabled') and not read(root, 'validation-window.json').get('pcap_start'):
            raise ValueError('Original ICMP capture anchor is absent')
        if cfg.get('pair_d_enabled') and not read(root, 'pair-d-baseline.json').get('session_id'):
            raise ValueError('Original EW TCP session baseline is absent')
        if cfg.get('ns', {}).get('enabled'):
            if read(root, 'ns-baseline.json').get('status') != 'PASS':
                raise ValueError('Original N-S baseline is not PASS')
            if cfg['ns'].get('diagnostics_enabled'):
                captures = read(root, 'ns-diagnostics-checkpoint.json')
                if (captures.get('schema') != 1 or captures.get('run') != runtime['run_id']
                        or set(captures.get('captures', {})) != {'endpoint', 'guest-snat', 'guest-fip'}
                        or any(cp.get('status') != 'RUNNING' or cp.get('remote', {}).get('status') != 'RUNNING'
                               for cp in captures.get('captures', {}).values())):
                    raise ValueError('Original N-S diagnostic captures are missing or stopped; '
                                     'failure collection may have finalized them. Preserve this run; '
                                     'do not restart captures or erase checkpoints to force resume')
    counts = read(root, 'resource-counts.before.json')
    diagnostics = read(root, 'diagnostics-config.json') if run_path(root, 'diagnostics-config.json').exists() else {}
    return dict(status='PRE_FREEZE_METADATA_CHECKPOINT', root=str(root), runtime=runtime,
                workload_config=cfg, workloads_enabled=bool(cfg), source_network_count=counts['networks'],
                diagnostics_enabled=bool(diagnostics.get('enabled')))


def record(root):
    path = run_path(Path(root), 'orchestration-recovery.json')
    value = read(Path(root), path.name) if path.exists() else {'events': []}
    value['events'].append(dict(route='metadata-preparation', resumed_at=time.time(),
        reason='Retry phase-06 metadata preparation after successful MTU convergence and genconfig',
        timing='Original timestamps, MTU journal, workloads and capture/session anchors retained; total duration includes recovery pause'))
    with open(path, 'w', opener=lambda p, f: os.open(p, f, 0o600)) as output:
        json.dump(value, output, indent=2)
        output.write('\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('inspect', 'record'))
    parser.add_argument('root', type=Path)
    parser.add_argument('--inventory', required=True)
    args = parser.parse_args()
    value = inspect(args.root, args.inventory)
    if args.action == 'record':
        record(args.root)
    print(json.dumps(value))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SystemExit('STOP: '+str(exc))
