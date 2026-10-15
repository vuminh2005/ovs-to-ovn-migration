#!/usr/bin/env python3
"""Coordinate run-owned host observers and archive even failed collection attempts."""
import argparse
import json
from pathlib import Path
import subprocess
import tarfile
import time
import uuid

from migration_diagnostics import save


def operate(root, action, role=None, label='checkpoint'):
    root = Path(root).resolve()
    cfg = json.loads((root/'diagnostics-config.json').read_text())
    if action == 'abort':
        errors = []
        try:
            operate(root, 'stop')
        except Exception as exc:
            errors.append(str(exc))
        if (root/'ns-diagnostics-checkpoint.json').exists():
            try:
                from ns_diagnostics import Diagnostics
                from types import SimpleNamespace
                Diagnostics(SimpleNamespace(root=root, cfg=json.loads((root/'validation-config.json').read_text()))).finish()
            except Exception as exc:
                errors.append(str(exc))
        save(root/'diagnostics-abort.json', dict(errors=errors, collected_at=time.time()))
        return dict(errors=errors)
    if not cfg.get('enabled'):
        save(root/'migration-diagnostics.json', dict(status='DISABLED'))
        return {}
    runtime = json.loads((root/'runtime.json').read_text())
    directory = root/'diagnostics'
    checkpoints = directory/'checkpoints'
    checkpoints.mkdir(parents=True, exist_ok=True, mode=0o700)
    entries = {}
    if action == 'ensure':
        if (root/'metrics/phase08.start').exists():
            return operate(root, 'check')
        operate(root, 'start', role='core')
        return operate(root, 'start', role='central')
    if action == 'start':
        hosts = ([runtime['ovn_cli_host']] if role == 'central' else list(dict.fromkeys(
            runtime.get('control_hosts', [])+runtime.get('network_hosts', [])+
            runtime.get('compute_hosts', [])+[runtime['ovn_cli_host']])))
        central = json.loads((root/'diagnostics-central-config.json').read_text()) if role == 'central' else {}
        for host in hosts:
            config = dict(cfg, **central, run=runtime['run_id'], host=host, role=role,
                control=host in runtime.get('control_hosts', []), network=host in runtime.get('network_hosts', []),
                chassis=host in runtime.get('compute_hosts', [])+runtime.get('network_hosts', []))
            remote = str(Path(cfg['directory'])/runtime['run_id']/(host+'-'+role))
            entry = dict(host=host, role=role, config=config, remote_directory=remote,
                archive_name=host+'-'+role+'.tar.gz', archive_directory='diagnostics/'+host+'/'+role, segments=[])
            path = checkpoints/(host+'-'+role+'.json')
            if path.exists():
                old = json.loads(path.read_text())
                if old['config'] != config:
                    raise RuntimeError('Diagnostics checkpoint configuration changed')
                entry = old
                if archived_stopped(root, old):
                    if (root/'metrics/phase08.start').exists():
                        raise RuntimeError('A diagnostics gap during/after cutover cannot be hidden by restarting collectors')
                    segment = uuid.uuid4().hex[:12]
                    entry = dict(old, remote_directory=remote+'-'+segment,
                        archive_name=host+'-'+role+'-'+segment+'.tar.gz',
                        archive_directory='diagnostics/'+host+'/'+role+'-'+segment,
                        segments=old.get('segments', [])+[dict(archive_directory=old.get('archive_directory', 'diagnostics/'+host+'/'+role),
                            remote_directory=old['remote_directory'], gap_before_cutover=True)])
                    save(path, entry)
            else:
                save(path, entry)  # Intent precedes any remote launch.
            entries[host] = entry
    else:
        # Group roles into separate transports when core+central share one host.
        roles = [role] if role else ['core', 'central']
        results = {}
        for selected in roles:
            rows = {p.stem.rsplit('-', 1)[0]: json.loads(p.read_text())
                    for p in checkpoints.glob('*-'+selected+'.json')}
            if action == 'stop':
                rows = {host: row for host, row in rows.items() if not archived_stopped(root, row)}
            if rows:
                results.update(transport(root, action, rows, label))
        if action == 'stop':
            return summarize(root)
        return results
    return transport(root, action, entries, label)


def archived_stopped(root, entry):
    path = root/entry.get('archive_directory', 'diagnostics/'+entry['host']+'/'+entry['role'])/'state.json'
    if not path.exists():
        return False
    value = json.loads(path.read_text())
    return value.get('stopped') is True and value.get('config') == entry['config']


def transport(root, action, entries, label):
    directory = root/'diagnostics'
    selected = next(iter(entries.values()))['role']
    extra = directory/('transport-'+selected+'.json')
    save(extra, dict(diagnostics_targets=list(entries), diagnostics_entries=entries,
        diagnostics_action=action, diagnostics_label=label, diagnostics_root=str(directory)))
    inventory = json.loads((root/'runtime.json').read_text())['inventory']
    script = Path(__file__).parent.parent/'playbooks/diagnostics-control.yml'
    result = subprocess.run(['ansible-playbook', '-i', inventory, str(script), '--limit', ':'.join(entries), '-e', '@'+str(extra)],
        capture_output=True, text=True, timeout=180)
    (directory/('transport-'+selected+'-'+action+'.log')).write_text(result.stdout+'\n'+result.stderr)
    if result.returncode:
        if action in ('start', 'check'):
            raise RuntimeError('Diagnostics '+action+' failed; inspect diagnostics transport log')
    if action == 'stop':
        for host, entry in entries.items():
            archive = directory/entry.get('archive_name', host+'-'+entry['role']+'.tar.gz')
            if archive.exists():
                dest = root/entry.get('archive_directory', 'diagnostics/'+host+'/'+entry['role'])
                dest.mkdir(parents=True, exist_ok=True, mode=0o700)
                with tarfile.open(archive) as bundle:
                    bundle.extractall(dest, filter='data')
    return {host+'-'+entry['role']: entry for host, entry in entries.items()}


def summarize(root):
    cfg = json.loads((root/'diagnostics-config.json').read_text())
    rows = []
    for path in sorted((root/'diagnostics/checkpoints').glob('*.json')):
        entry = json.loads(path.read_text())
        state_path = root/entry.get('archive_directory', 'diagnostics/'+entry['host']+'/'+entry['role'])/'state.json'
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        rows.append(dict(host=entry['host'], role=entry['role'], archive=str(state_path.parent.relative_to(root)),
            status='PASS' if state.get('status') == 'STOPPED' and not state.get('errors') else 'UNAVAILABLE',
            errors=state.get('errors', ['No archived final state']), jobs=state.get('jobs', {}),
            preparation_segments=entry.get('segments', [])))
    roles = {row['role'] for row in rows}
    result = dict(status='PASS' if rows and roles == {'core', 'central'} and all(
        r['status'] == 'PASS' for r in rows) else 'UNAVAILABLE', collectors=rows,
        collected_at=time.time(), read_only_network_state=True,
        limits=dict(max_bytes_per_collector=cfg['max_bytes'], lifetime_seconds=cfg['lifetime']),
        timestamp_scope='Observer receive time plus native source log/PCAP timestamps; inspect clock-offset snapshots',
        coverage_scope='Current observation segment begins before cutover; preparation gaps and earlier segments are retained explicitly',
        interpretation='Temporal correlation is evidence, not automatic proof of causality')
    save(root/'migration-diagnostics.json', result)
    return result


def reset_stop(backup_root):
    errors = []
    stopped = []
    for path in sorted(Path(backup_root).glob('*/diagnostics-config.json')):
        root = path.parent
        if not (root/'diagnostics/checkpoints').exists():
            continue
        try:
            operate(root, 'stop')
            stopped.append(root.name)
        except Exception as exc:
            errors.append(dict(run=root.name, error=str(exc)))
    result = dict(status='FAIL' if errors else 'PASS', runs=stopped, errors=errors)
    save(Path(backup_root)/'diagnostics-reset-stop.json', result)
    if errors:
        raise RuntimeError('Host observers could not be stopped/archived; reset blocked')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['start', 'check', 'snapshot', 'stop', 'abort', 'reset-stop', 'ensure'])
    parser.add_argument('root', type=Path)
    parser.add_argument('--role', choices=['core', 'central'])
    parser.add_argument('--label', default='checkpoint')
    args = parser.parse_args()
    print(json.dumps(reset_stop(args.root) if args.action == 'reset-stop' else
                     operate(args.root, args.action, args.role, args.label)))


if __name__ == '__main__':
    main()
