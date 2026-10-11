#!/usr/bin/env python3
"""Versioned phase instrumentation and checkpoint meanings; no cloud access."""
import argparse
import json
import math
import pathlib
import time


LEGACY_SCHEMA = 1
CANONICAL_SCHEMA = 2
PHASES = (
    ('bootstrap', '00-bootstrap.yml'),
    ('backup', '01-backup.yml'),
    ('precheck', '02-precheck.yml'),
    ('validation prerequisites', '03-validation-prerequisites.yml'),
    ('validation workloads', '04-validation-workloads.yml'),
    ('stage OVN DB', '05-stage-ovn-db.yml'),
    ('target configuration/MTU preparation', '06-target-config.yml'),
    ('DB migration', '07-migrate-db.yml'),
    ('cutover', '08-cutover.yml'),
    ('legacy cleanup', '09-cleanup.yml'),
    ('restore Neutron', '10-restore-neutron.yml'),
    ('infrastructure validation', '11-validate.yml'),
    ('workload validation', '12-workload-validation.yml'),
    ('report/finalization', '13-report.yml'),
)
# Indexed by canonical phase. Missing entries were never instrumented in v1.
LEGACY_MARKERS = {0: 0, 1: 1, 2: 2, 5: 3, 6: 4, 7: 5, 8: 6, 9: 7,
                  10: 8, 11: 9, 13: 10}
TOTAL_SCOPES = {
    LEGACY_SCHEMA: 'Run-directory timer initialization to report-phase entry; excludes report/finalization and capture cleanup; includes resume waits.',
    CANONICAL_SCHEMA: 'First bootstrap task timestamp to completion of report/finalization and capture cleanup; excludes final timing-only report publication, terminal output and exit gate; includes resume waits.',
}
PHASE_SCOPE = 'Per-file execution, across all plays; latest execution on resume. Phase 13 ends after finalization/capture cleanup, before final timing-only publication and terminal output/exit gate. Legacy scope follows historical marker boundaries.'


def schema_version(root):
    path = pathlib.Path(root)/'runtime.json'
    runtime = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(runtime, dict):
        raise ValueError('Malformed runtime checkpoint')
    version = runtime.get('phase_marker_schema_version', LEGACY_SCHEMA)
    if type(version) is not int or version not in (LEGACY_SCHEMA, CANONICAL_SCHEMA):
        raise ValueError('Unsupported phase-marker schema; checkpoint interpretation prohibited')
    return version


def marker_name(version, phase, edge):
    if phase not in range(len(PHASES)) or edge not in ('start', 'end'):
        raise ValueError('Invalid canonical phase or marker edge')
    if version == CANONICAL_SCHEMA:
        number = phase
    elif version == LEGACY_SCHEMA:
        number = LEGACY_MARKERS.get(phase)
        if number is None or (phase == 13 and edge == 'end'):
            return None  # legacy report phase was start-only
    else:
        raise ValueError('Unsupported phase-marker schema')
    return f'phase{number:02d}.{edge}'


def timestamp(path):
    try:
        value = float(path.read_text().strip())
        return value if math.isfinite(value) else None
    except (OSError, ValueError):
        return None


def phase_measurements(root):
    root = pathlib.Path(root)
    version = schema_version(root)
    result = []
    for phase, (name, filename) in enumerate(PHASES):
        start_name = marker_name(version, phase, 'start')
        end_name = marker_name(version, phase, 'end')
        start_path = root/'metrics'/start_name if start_name else None
        end_path = root/'metrics'/end_name if end_name else None
        start = timestamp(start_path) if start_path else None
        end = timestamp(end_path) if end_path else None
        duration = None
        if start_path is None or not (start_path.exists() or (end_path and end_path.exists())):
            availability = 'NOT MEASURED'
        elif start_path.exists() and start is not None and (end_path is None or not end_path.exists()):
            availability = 'INCOMPLETE'
        elif start is None or end is None or end < start:
            availability = 'UNAVAILABLE'
        else:
            availability = 'MEASURED'
            duration = round(end-start, 3)
        result.append(dict(number=f'{phase:02d}', name=name, filename=filename,
                           start_marker=start_name, end_marker=end_name,
                           duration_seconds=duration, availability=availability))
    return result


def resume_eligible(root):
    root = pathlib.Path(root)
    version = schema_version(root)
    return any((root/'metrics'/marker_name(version, phase, edge)).exists()
               for phase, edge in ((8, 'end'), (9, 'start')))


def pre_cutover_reboot_prohibited(root):
    root = pathlib.Path(root)
    version = schema_version(root)
    # v2 phase 07 begins with a readiness check BEFORE freeze. Its file-entry
    # timer alone cannot change the existing pre-freeze remediation eligibility.
    names = ([marker_name(version, phase, 'start') for phase in (7, 8)]
             if version == LEGACY_SCHEMA else ['phase08.start', 'db_migration.start'])
    names.append('control_plane_downtime.start')
    return any((root/'metrics'/name).exists() for name in names)


def mark(root, phase, edge, when=None, also=None):
    root = pathlib.Path(root)
    version = schema_version(root)
    name = marker_name(version, phase, edge)
    when = time.time() if when is None else when
    if type(when) not in (int, float) or not math.isfinite(when):
        raise ValueError('Invalid phase timestamp')
    if name:
        metrics = root/'metrics'
        if edge == 'start' and version == CANONICAL_SCHEMA:
            (metrics/marker_name(version, phase, 'end')).unlink(missing_ok=True)
        (metrics/name).write_text(f'{when:.9f}\n')
    if also:
        (root/'metrics'/also).write_text(f'{when:.9f}\n')


def report_start(root):
    now = time.time()
    mark(root, 13, 'start', now)
    if schema_version(root) == LEGACY_SCHEMA:
        (pathlib.Path(root)/'metrics/total.end').write_text(f'{now:.9f}\n')
    else:
        (pathlib.Path(root)/'metrics/total.end').unlink(missing_ok=True)


def finish(root):
    if schema_version(root) == CANONICAL_SCHEMA:
        mark(root, 13, 'end', also='total.end')


def preparation_retry_check(root, inventory):
    """Permit only an interrupted phase 04 before any recorded VM or later phase."""
    root = pathlib.Path(root)
    if not root.is_absolute() or schema_version(root) != CANONICAL_SCHEMA:
        raise ValueError('Preparation retry requires an absolute schema-2 run directory')
    runtime = json.loads((root/'runtime.json').read_text())
    cfg = json.loads((root/'validation-config.json').read_text())
    state = json.loads((root/'validation-resources.json').read_text())
    if (runtime.get('run_id') != root.name or cfg.get('run') != root.name or
            runtime.get('inventory') != inventory or cfg.get('inventory') != inventory):
        raise ValueError('Preparation retry run/inventory identity changed')
    for phase in range(4):
        if timestamp(root/'metrics'/f'phase{phase:02d}.end') is None:
            raise ValueError('Preparation retry requires completed phases 00 through 03')
    if timestamp(root/'metrics/phase04.start') is None or (root/'metrics/phase04.end').exists():
        raise ValueError('Preparation retry requires an incomplete phase 04')
    forbidden = ['total.end', 'control_plane_downtime.start', 'control_plane_downtime.end',
                 'db_migration.start', 'db_migration.end',
                 'dataplane_convergence.start', 'dataplane_convergence.end']
    forbidden += [f'phase{phase:02d}.{edge}' for phase in range(5,14) for edge in ('start','end')]
    if any((root/'metrics'/name).exists() for name in forbidden):
        raise ValueError('Preparation retry is forbidden after OVN staging, target changes or freeze')
    pre = state.get('pre', {})
    pair = pre.get('measure', {})
    if (state.get('schema_version') != 2 or state.get('historical_dual_pair') or
            state.get('post') or pre.get('cleanup_started') or pre.get('cleaned') or
            pre.get('existing') or pre.get('tcp') or set(pair) != {'0','1'} or
            any(vm.get('server') or vm.get('owned') is not True or not vm.get('port')
                for vm in pair.values())):
        raise ValueError('Preparation retry supports first VM-create failure only; preserve later workloads')
    if cfg.get('placement_enabled') is not True or set(cfg.get('placement', {})) != {'0','1'}:
        raise ValueError('Preparation retry requires the original explicit placement checkpoint')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('mark', 'phase-entry', 'report-start', 'finish', 'resume-check', 'preparation-retry-check'))
    parser.add_argument('root', type=pathlib.Path)
    parser.add_argument('phase', type=int, nargs='?')
    parser.add_argument('edge', choices=('start', 'end'), nargs='?')
    parser.add_argument('--timestamp', type=float)
    parser.add_argument('--timestamp-file', type=pathlib.Path)
    parser.add_argument('--freeze-start', action='store_true')
    parser.add_argument('--also', choices=('total.start', 'dataplane_convergence.start'))
    parser.add_argument('--inventory')
    args = parser.parse_args()
    if args.action == 'preparation-retry-check':
        if not args.inventory:
            parser.error('preparation-retry-check requires --inventory')
        preparation_retry_check(args.root, args.inventory)
    elif args.action == 'phase-entry':
        if args.phase is None:
            parser.error('phase-entry requires a canonical phase')
        if schema_version(args.root) == CANONICAL_SCHEMA:
            mark(args.root, args.phase, 'start')
    elif args.action == 'mark':
        if args.phase is None or args.edge is None:
            parser.error('mark requires a canonical phase and edge')
        now = time.time()
        when = args.timestamp
        if args.timestamp_file:
            when = timestamp(args.timestamp_file)
            if when is None:
                parser.error('Timestamp source missing or invalid')
        if args.freeze_start:
            if (args.phase, args.edge) != (7, 'start'):
                parser.error('freeze-start is only valid at the phase 07 freeze boundary')
            if schema_version(args.root) == LEGACY_SCHEMA:
                mark(args.root, args.phase, args.edge, now)
            elif not (args.root/'metrics/phase07.start').exists():
                parser.error('Canonical phase 07 entry marker missing')
            (args.root/'metrics/control_plane_downtime.start').write_text(f'{now:.9f}\n')
        else:
            mark(args.root, args.phase, args.edge, when, args.also)
    elif args.action == 'report-start':
        report_start(args.root)
    elif args.action == 'finish':
        finish(args.root)
    elif not resume_eligible(args.root):
        parser.error('No late-phase takeover checkpoint for this run schema')


if __name__ == '__main__':
    main()
