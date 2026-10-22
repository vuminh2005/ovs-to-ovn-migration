#!/usr/bin/env python3
"""Guard recovery of a failed phase 05 before target configuration or freeze."""

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
import os
from pathlib import Path
import time

from phase_schema import timestamp


def read(root, name):
    value = json.loads((run_path(root, name)).read_text())
    if not isinstance(value, dict):
        raise ValueError('Malformed checkpoint: '+name)
    return value


def inspect(root, inventory):
    root = Path(root).resolve()
    runtime = read(root, 'runtime.json')
    if (runtime.get('phase_marker_schema_version') != 2 or runtime.get('run_id') != root.name or
            Path(runtime['inventory']).resolve() != Path(inventory).resolve()):
        raise ValueError('Recovery requires the original schema-2 run and inventory')
    if (run_path(root, 'inventory.before')).read_bytes() != Path(inventory).read_bytes():
        raise ValueError('Inventory changed after the source snapshot')
    metrics = run_path(root, 'metrics')
    previous = timestamp(metrics/'total.start')
    if previous is None:
        raise ValueError('Original total.start is absent')
    for phase in range(5):
        start, end = [timestamp(metrics/f'phase{phase:02d}.{edge}') for edge in ('start', 'end')]
        if start is None or end is None or not previous <= start <= end:
            raise ValueError('Completed ordered phases 00 through 04 are required')
        previous = end
    start = timestamp(metrics/'phase05.start')
    if start is None or start < previous or (metrics/'phase05.end').exists():
        raise ValueError('Recovery requires an incomplete phase 05')
    forbidden = [f'phase{phase:02d}.{edge}' for phase in range(6, 14) for edge in ('start', 'end')]
    forbidden += ['control_plane_downtime.start', 'control_plane_downtime.end',
                  'db_migration.start', 'db_migration.end', 'dataplane_convergence.start',
                  'dataplane_convergence.end', 'total.end']
    if any((metrics/name).exists() for name in forbidden):
        raise ValueError('Target configuration, freeze or later phases already began')
    for name in ('network-mtu-migration.tsv', 'ml2_conf.ini.target', 'neutron.conf.target',
                 'db-sync-migrate.log', 'pair-d-trigger.json', 'migration-report.json'):
        if (run_path(root, name)).exists():
            raise ValueError('Later-phase artifact prohibits this recovery: '+name)
    cfg = read(root, 'validation-config.json') if (run_path(root, 'validation-resources.json')).exists() else {}
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
        if cfg.get('ns', {}).get('enabled') and read(root, 'ns-baseline.json').get('status') != 'PASS':
            raise ValueError('Original N-S baseline is not PASS')
    counts = read(root, 'resource-counts.before.json')
    return dict(status='PRE_TARGET_STAGE_CHECKPOINT', root=str(root), runtime=runtime,
                workload_config=cfg, workloads_enabled=bool(cfg), source_network_count=counts['networks'])


def select(parent, inventory):
    matches = []
    for root in sorted(Path(parent).iterdir()):
        if not root.is_dir():
            continue
        try:
            inspect(root, inventory)
            matches.append(root.resolve())
        except (OSError, ValueError, KeyError, TypeError):
            continue
    if len(matches) != 1:
        raise ValueError('Expected exactly one eligible phase-05 checkpoint; found '+
                         str(len(matches))+': '+', '.join(map(str, matches)))
    return matches[0]


def record(root):
    path = run_path(Path(root), 'orchestration-recovery.json')
    value = read(Path(root), path.name) if path.exists() else {'events': []}
    value['events'].append(dict(route='pre-target-stage', resumed_at=time.time(),
        reason='Resume interrupted phase 05 using original workloads and image cache',
        timing='Original run start and phase 00-04/capture/session baselines retained; total duration includes recovery pause'))
    with open(path, 'w', opener=lambda p, f: os.open(p, f, 0o600)) as output:
        json.dump(value, output, indent=2)
        output.write('\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('inspect', 'select', 'record'))
    parser.add_argument('path', type=Path)
    parser.add_argument('--inventory', required=True)
    args = parser.parse_args()
    if args.action == 'select':
        print(select(args.path, args.inventory))
    else:
        value = inspect(args.path, args.inventory)
        if args.action == 'record':
            record(args.path)
        print(json.dumps(value))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SystemExit('STOP: '+str(exc))
