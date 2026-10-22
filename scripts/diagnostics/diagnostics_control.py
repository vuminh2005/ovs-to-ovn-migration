#!/usr/bin/env python3
"""Coordinate run-owned host observers and archive even failed collection attempts."""

# Source helpers are grouped; remote/guest copies keep flat sibling imports.
import sys as _sys
from pathlib import Path as _Path
_source = _Path(__file__).resolve().parents[1]
if _source.name == 'scripts' and (_source / '_paths.py').is_file():
    _sys.path.insert(0, str(_source))
    from _paths import configure as _configure
    _configure()

from _paths import playbook_path
from run_layout import run_path
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
import time
import uuid

from migration_diagnostics import save


LEGACY_TUNNEL_INTERFACE = "{{ 'br-underlay' if inventory_hostname in groups['network'] else 'eth1' }}"


def effective_config(config):
    """Materialize the known source-lab formula without evaluating arbitrary Jinja."""
    value = dict(config)
    if value.get('tunnel_interface') == LEGACY_TUNNEL_INTERFACE:
        value['tunnel_interface'] = 'br-underlay' if value.get('network') is True else 'eth1'
    return value


def operate(root, action, role=None, label='checkpoint'):
    root = Path(root).resolve()
    cfg = json.loads((run_path(root, 'diagnostics-config.json')).read_text())
    if action == 'abort':
        errors = []
        try:
            operate(root, 'stop')
        except Exception as exc:
            errors.append(str(exc))
        if (run_path(root, 'ns-diagnostics-checkpoint.json')).exists():
            try:
                from ns_diagnostics import Diagnostics
                from types import SimpleNamespace
                Diagnostics(SimpleNamespace(root=root, cfg=json.loads((run_path(root, 'validation-config.json')).read_text()))).finish()
            except Exception as exc:
                errors.append(str(exc))
        save(run_path(root, 'diagnostics-abort.json'), dict(errors=errors, collected_at=time.time()))
        return dict(errors=errors)
    if not cfg.get('enabled'):
        save(run_path(root, 'migration-diagnostics.json'), dict(status='DISABLED'))
        return {}
    runtime = json.loads((run_path(root, 'runtime.json')).read_text())
    directory = run_path(root, 'diagnostics')
    checkpoints = directory/'checkpoints'
    checkpoints.mkdir(parents=True, exist_ok=True, mode=0o700)
    entries = {}
    if action == 'ensure':
        if (run_path(root, 'metrics/phase08.start')).exists():
            return operate(root, 'check')
        operate(root, 'start', role='core')
        return operate(root, 'start', role='central')
    if action == 'start':
        hosts = ([runtime['ovn_cli_host']] if role == 'central' else list(dict.fromkeys(
            runtime.get('control_hosts', [])+runtime.get('network_hosts', [])+
            runtime.get('compute_hosts', [])+[runtime['ovn_cli_host']])))
        central = json.loads((run_path(root, 'diagnostics-central-config.json')).read_text()) if role == 'central' else {}
        for host in hosts:
            config = dict(cfg, **central, run=runtime['run_id'], host=host, role=role,
                control=host in runtime.get('control_hosts', []), network=host in runtime.get('network_hosts', []),
                chassis=host in runtime.get('compute_hosts', [])+runtime.get('network_hosts', []))
            config = effective_config(config)
            remote = str(Path(cfg['directory'])/runtime['run_id']/(host+'-'+role))
            entry = dict(host=host, role=role, config=config, remote_directory=remote,
                archive_name=host+'-'+role+'.tar.gz', archive_directory='diagnostics/'+host+'/'+role, segments=[])
            path = checkpoints/(host+'-'+role+'.json')
            if path.exists():
                old = json.loads(path.read_text())
                if effective_config(old['config']) != config:
                    raise RuntimeError('Diagnostics checkpoint configuration changed')
                entry = old
                if archived_stopped(root, old):
                    prune_archive(root, old)
                    if (run_path(root, 'metrics/phase08.start')).exists():
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
                for row in rows.values():
                    if archived_stopped(root, row):
                        prune_archive(root, row)
                rows = {host: row for host, row in rows.items() if not archived_stopped(root, row)}
            if rows:
                results.update(transport(root, action, rows, label))
        if action == 'stop':
            return summarize(root)
        return results
    return transport(root, action, entries, label)


def archived_stopped(root, entry):
    path = run_path(root, entry.get('archive_directory', 'diagnostics/'+entry['host']+'/'+entry['role']))/'state.json'
    if not path.exists():
        return False
    value = json.loads(path.read_text())
    return (value.get('stopped') is True and value.get('cleanup_complete', True) is True
            and effective_config(value.get('config', {})) == effective_config(entry['config']))


def prune_archive(root, entry):
    """Discard only a transport bundle whose files are all retained verbatim."""
    archive = run_path(root, 'diagnostics')/entry.get('archive_name', entry['host']+'-'+entry['role']+'.tar.gz')
    if not archive.exists():
        return
    dest = run_path(root, entry.get('archive_directory', 'diagnostics/'+entry['host']+'/'+entry['role']))
    with tarfile.open(archive) as bundle:
        for member in bundle:
            target = dest/member.name
            if not target.resolve().is_relative_to(dest.resolve()):
                raise RuntimeError('Diagnostic archive path escapes evidence directory')
            if member.isdir():
                if not target.is_dir():
                    raise RuntimeError('Diagnostic archive directory missing: '+member.name)
            elif member.isfile() and target.is_file() and not target.is_symlink():
                with bundle.extractfile(member) as original, target.open('rb') as retained:
                    if hashlib.file_digest(original, 'sha256').digest() != hashlib.file_digest(retained, 'sha256').digest():
                        raise RuntimeError('Diagnostic archive content mismatch: '+member.name)
            else:
                raise RuntimeError('Diagnostic archive entry not retained: '+member.name)
    archive.unlink()


def transport(root, action, entries, label):
    directory = run_path(root, 'diagnostics')
    selected = next(iter(entries.values()))['role']
    extra = directory/('transport-'+selected+'.json')
    save(extra, dict(diagnostics_targets=list(entries), diagnostics_entries=entries,
        diagnostics_action=action, diagnostics_label=label, diagnostics_root=str(directory)))
    inventory = json.loads((run_path(root, 'runtime.json')).read_text())['inventory']
    script = playbook_path('diagnostics-control.yml')
    result = subprocess.run(['ansible-playbook', '-i', inventory, str(script), '--limit', ':'.join(entries), '-e', '@'+str(extra)],
        capture_output=True, text=True, timeout=180)
    (directory/('transport-'+selected+'-'+action+'.log')).write_text(result.stdout+'\n'+result.stderr)
    if result.returncode:
        if action in ('start', 'check', 'snapshot-wait'):
            raise RuntimeError('Diagnostics '+action+' failed; inspect diagnostics transport log')
    if action == 'stop':
        for host, entry in entries.items():
            archive = directory/entry.get('archive_name', host+'-'+entry['role']+'.tar.gz')
            if archive.exists():
                dest = run_path(root, entry.get('archive_directory', 'diagnostics/'+host+'/'+entry['role']))
                dest.mkdir(parents=True, exist_ok=True, mode=0o700)
                with tarfile.open(archive) as bundle:
                    bundle.extractall(dest, filter='data')
                prune_archive(root, entry)
    return {host+'-'+entry['role']: entry for host, entry in entries.items()}


def summarize(root):
    cfg = json.loads((run_path(root, 'diagnostics-config.json')).read_text())
    rows = []
    for path in sorted((run_path(root, 'diagnostics/checkpoints')).glob('*.json')):
        entry = json.loads(path.read_text())
        state_path = run_path(root, entry.get('archive_directory', 'diagnostics/'+entry['host']+'/'+entry['role']))/'state.json'
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        rows.append(dict(host=entry['host'], role=entry['role'], archive=str(state_path.parent.relative_to(root)),
            status='PASS' if state.get('status') == 'STOPPED' and not state.get('errors') else 'UNAVAILABLE',
            errors=state.get('errors', ['No archived final state']), jobs=state.get('jobs', {}),
            cleanup_complete=state.get('cleanup_complete', state.get('status') == 'STOPPED' and state.get('stopped') is True),
            coverage=state.get('coverage', 'UNAVAILABLE'),
            preparation_segments=entry.get('segments', [])))
    roles = {row['role'] for row in rows}
    result = dict(status='PASS' if rows and roles == {'core', 'central'} and all(
        r['status'] == 'PASS' for r in rows) else 'UNAVAILABLE', collectors=rows,
        collection_status='STOPPED' if rows and roles == {'core', 'central'} and all(
            r['cleanup_complete'] for r in rows) else 'UNAVAILABLE',
        collected_at=time.time(), read_only_network_state=True,
        limits=dict(max_bytes_per_collector=cfg['max_bytes'], lifetime_seconds=cfg['lifetime'],
            reconnect_attempts=cfg.get('reconnect_attempts', 8), reconnect_window_seconds=cfg.get('reconnect_window_seconds', 60)),
        timestamp_scope='Observer receive time plus native source log/PCAP timestamps; inspect clock-offset snapshots',
        coverage_scope='Current observation segment begins before cutover; preparation gaps and earlier segments are retained explicitly',
        interpretation='Temporal correlation is evidence, not automatic proof of causality')
    save(run_path(root, 'migration-diagnostics.json'), result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['start', 'check', 'snapshot', 'snapshot-wait', 'stop', 'abort', 'ensure'])
    parser.add_argument('root', type=Path)
    parser.add_argument('--role', choices=['core', 'central'])
    parser.add_argument('--label', default='checkpoint')
    args = parser.parse_args()
    print(json.dumps(operate(args.root, args.action, args.role, args.label)))


if __name__ == '__main__':
    main()
