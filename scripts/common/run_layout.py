#!/usr/bin/env python3
"""Resolve run files without changing the layout of historical checkpoints.

Only phase 00 creates the layout marker. A directory without the marker keeps
the original flat layout, including all writes made during a historical resume.
Subdirectories (flow captures, host observers and guest files) remain ordinary
paths and do not inherit the controller run's layout.
"""
import argparse
import json
from pathlib import Path


MARKER = 'run-layout.json'
VERSION = 1
DIRECTORIES = ('config', 'backups', 'resources', 'checkpoints', 'evidence',
               'captures', 'logs', 'metrics', 'task-timings', 'diagnostics',
               'flows', 'ns', 'ns-diagnostics', 'image-preflight', 'post-restore-observations')
REPORTS = {'migration-report.json', 'migration-report.txt', MARKER}


def version(root):
    marker = Path(root)/MARKER
    if marker.is_symlink():
        raise ValueError('Run layout marker must be a regular file')
    try:
        value = json.loads(marker.read_text())
    except FileNotFoundError:
        return 0
    if value.get('schema_version') != VERSION:
        raise ValueError('Unsupported run layout; refusing checkpoint relocation')
    return VERSION


def category(name):
    if name in REPORTS or name in DIRECTORIES or '/' in name:
        return None
    if name in ('globals.yml.before', 'inventory.before', 'mariadb-backup.json'):
        return 'backups'
    if name in ('runtime.json', 'ml2_conf.ini.target', 'neutron.conf.target') or '-config.' in name:
        return 'config'
    if ('.before.' in name or '.after.' in name or name == 'compute-port-ids.txt'):
        return 'resources'
    if (name.endswith(('.pcap', '.pcapng')) or name in ('tcpdump.log', 'supervisor.log')
            or name.startswith(('capture-', 'validation-window'))):
        return 'captures'
    if (name.endswith(('.pid', '.lock')) or any(token in name for token in
            ('-resources.', '-checkpoint.', '-baseline.', '-anchors.', '-transport.', 'pair-a-capture.',
             '-trigger.', '-trigger-ack.', 'cutover-preparation.'))):
        return 'checkpoints'
    if name.endswith('.log'):
        return 'logs'
    return 'evidence'


def run_path(root, name):
    root, relative = Path(root), Path(name)
    group = category(str(relative)) if version(root) else None
    return root/group/relative if group else root/relative


def run_glob(root, pattern):
    root = Path(root)
    if not version(root) or '/' in pattern:
        return root.glob(pattern)
    paths = list(root.glob(pattern))
    for group in DIRECTORIES:
        paths.extend((root/group).glob(pattern))
    return iter(sorted(set(paths)))


def initialize(root):
    root = Path(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not (root/MARKER).exists() and any(p.is_file() for p in root.iterdir()):
        raise ValueError('Cannot convert an existing flat run; resume it in place')
    if (root/MARKER).exists():
        version(root)
    for name in DIRECTORIES:
        target = root/name
        if target.is_symlink():
            raise ValueError('Run data directory must not be a symlink')
        target.mkdir(mode=0o700, exist_ok=True)
    data = dict(schema_version=VERSION, directories=list(DIRECTORIES),
                reports=sorted(REPORTS-{MARKER}), legacy_resume='Keep flat layout when marker is absent')
    temporary = root/(MARKER+'.tmp')
    temporary.write_text(json.dumps(data, indent=2)+'\n')
    temporary.chmod(0o600)
    temporary.replace(root/MARKER)
    return data


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('init', 'path'))
    parser.add_argument('root', type=Path)
    parser.add_argument('name', nargs='?')
    args = parser.parse_args()
    if args.action == 'path' and args.name is None:
        parser.error('path requires a filename')
    print(json.dumps(initialize(args.root)) if args.action == 'init' else run_path(args.root, args.name))
