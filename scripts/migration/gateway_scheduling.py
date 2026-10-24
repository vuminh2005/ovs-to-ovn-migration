#!/usr/bin/env python3
"""Run-owned, bounded Docker supervisor for the optional early L3 scheduler."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid


CONFIG = Path('/etc/kolla/neutron-server')
PREFIX = 'GATEWAY_RESULT='


class PreparationCheck(RuntimeError):
    """A local diagnostic that contains no configuration values."""


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')
    temporary.chmod(0o600)
    temporary.replace(path)


def command(args, timeout=5):
    return subprocess.run(['docker'] + args, capture_output=True, text=True,
                          check=True, timeout=timeout).stdout.strip()


def config_digest():
    files = {str(path.relative_to(CONFIG)): digest(path)
             for path in sorted(CONFIG.rglob('*')) if path.is_file()}
    if 'config.json' not in files or not files:
        raise PreparationCheck('Kolla neutron-server configuration is missing')
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def validated(directory, expected):
    owner = json.loads((directory / 'owner.json').read_text())
    if (not re.fullmatch(r'[A-Za-z0-9_-]+', expected['run']) or
            directory != Path('/tmp/ovs-to-ovn-preparation-' + expected['run']) or
            directory.is_symlink() or owner != expected):
        raise PreparationCheck('Gateway helper ownership does not match this run')
    topology = directory / 'gateway-topology.json'
    if digest(topology) != expected['topology_sha256']:
        raise PreparationCheck('Gateway topology checkpoint changed')
    value = json.loads(topology.read_text())
    if not value or any(not row.get('system_id') or not row.get('tunnel_ip') for row in value.values()):
        raise PreparationCheck('Gateway topology lacks chassis identities')
    image_id = command(['image', 'inspect', expected['neutron_image'], '--format', '{{.Id}}'])
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', image_id):
        raise PreparationCheck('Cannot pin the local Neutron image')
    return dict(schema=1, owner=expected, image_id=image_id, config_sha256=config_digest(),
                helper_sha256=digest(directory / 'gateway_native.py'),
                supervisor_sha256=digest(Path(__file__)))


def container(directory, manifest, action, budget):
    """Own by returned immutable container ID; terminate on every exit path.

    The in-container alarm expires five seconds before the attach deadline.
    The force removal has its own five-second bound. No pull, API or RPC server.
    """
    cid = None
    output = ''
    attach_failed = False
    try:
        cid = command(['create', '--pull=never', '--network', 'host', '--user', 'root',
                       '--name', 'ovn-gateway-' + uuid.uuid4().hex,
                       '--label', 'ovs-to-ovn.gateway-run=' + manifest['owner']['run'],
                       '-e', 'KOLLA_CONFIG_STRATEGY=COPY_ALWAYS',
                       '-e', 'KOLLA_SERVICE_NAME=neutron-server',
                       '-v', str(CONFIG) + ':/var/lib/kolla/config_files:ro',
                       '-v', 'kolla_logs:/var/log/kolla',
                       '-v', str(directory / 'gateway_native.py') + ':/opt/gateway_native.py:ro',
                       '-v', str(directory / 'gateway-topology.json') + ':/opt/gateway-topology.json:ro',
                       '--entrypoint', '/bin/bash', manifest['image_id'], '-lc',
                       'set -euo pipefail; kolla_set_configs; '
                       'exec /var/lib/kolla/venv/bin/python /opt/gateway_native.py "$@"',
                       'gateway-helper', action, '--topology', '/opt/gateway-topology.json',
                       '--timeout', str(budget - 5)])
        if not re.fullmatch(r'[0-9a-f]{64}', cid):
            cid = None
            raise PreparationCheck('Docker did not return an immutable container ID')
        try:
            output = command(['start', '--attach', cid], timeout=budget)
        except subprocess.CalledProcessError as exc:
            output = exc.stdout or ''
            attach_failed = True
        finally:
            log = directory / ('gateway-' + action + '.log')
            log.write_text(output)
            log.chmod(0o600)
        results = [json.loads(line[len(PREFIX):]) for line in output.splitlines() if line.startswith(PREFIX)]
        if len(results) != 1 or results[0].get('status') not in ('PASS', 'FALLBACK'):
            raise PreparationCheck('Native gateway helper did not return a valid result')
        if attach_failed and results[0]['status'] == 'PASS':
            raise PreparationCheck('Container exited unsuccessfully despite a PASS result')
        return results[0]
    finally:
        if cid:
            command(['rm', '--force', cid], timeout=5)


def run(action, directory, expected, cleanup_verified=False, journal=None):
    started = time.monotonic()
    result = dict(status='FALLBACK', action=action, run=expected['run'])
    def record(edge):
        if journal and action == 'schedule':
            path = Path(journal)
            if str(path) != '/var/lib/ovs-to-ovn-rollback/' + expected['run'] + '/events.jsonl' or path.is_symlink():
                raise PreparationCheck('Gateway journal ownership differs')
            with open(path, 'a', opener=lambda p, f: os.open(p, f, 0o600)) as output:
                output.write(json.dumps(dict(stage='gateway-scheduling', edge=edge, time=time.time())) + '\n')
                output.flush(); os.fsync(output.fileno())
    try:
        manifest = validated(directory, expected)
        checkpoint = directory / 'gateway-preparation.json'
        if action == 'prepare':
            if checkpoint.exists() and json.loads(checkpoint.read_text()) != manifest:
                raise PreparationCheck('Prepared gateway inputs changed; refusing to rebase')
            native = container(directory, manifest, 'preflight', 60)
            if native['status'] == 'PASS':
                save(checkpoint, manifest)
        else:
            if not cleanup_verified:
                raise PreparationCheck('Legacy cleanup was not verified on every network host')
            if json.loads(checkpoint.read_text()) != manifest:
                raise PreparationCheck('Gateway helper/image/config changed since preparation')
            record('intent')
            native = container(directory, manifest, 'schedule', 30)
            record('complete' if native['status'] == 'PASS' else 'fallback')
        result.update(status=native['status'], native=native)
    except Exception as exc:
        # No subprocess command/configuration/connection strings in stdout.
        result['error_type'] = type(exc).__name__
        if isinstance(exc, PreparationCheck):
            result['reason'] = str(exc)
    result['elapsed_seconds'] = round(time.monotonic() - started, 6)
    save(directory / ('gateway-' + action + '-result.json'), result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'schedule'))
    parser.add_argument('directory', type=Path)
    parser.add_argument('owner', help='Exact cutover preparation JSON')
    parser.add_argument('--cleanup-verified', action='store_true')
    parser.add_argument('--journal')
    args = parser.parse_args(argv)
    result = run(args.action, args.directory, json.loads(args.owner), args.cleanup_verified, args.journal)
    print(json.dumps(result, sort_keys=True))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    sys.exit(main())
