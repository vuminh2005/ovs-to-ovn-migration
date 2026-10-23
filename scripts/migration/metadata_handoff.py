#!/usr/bin/env python3
"""Create-only metadata preparation and guarded, host-local cutover activation.

Transferred alone by Ansible. Preparation uses the installed Kolla Docker and
systemd workers through a small Ansible adapter; it never executes the image.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time


RUN_LABEL = 'ovn-migration.metadata.run'
HASH_LABEL = 'ovn-migration.metadata.config-sha256'
MANIFEST = 'migration-preparation.json'
NAME = 'neutron_ovn_metadata_agent'
UNIT = 'kolla-' + NAME + '-container.service'
OLD_NAME = 'neutron_metadata_agent'
OLD_UNIT = 'kolla-' + OLD_NAME + '-container.service'


def command(argv, check=True):
    return subprocess.run(argv, text=True, capture_output=True, check=check, timeout=30)


def config_directory(volumes):
    matches = []
    for volume in volumes:
        parts = volume.split(':')
        if len(parts) == 3 and parts[1] == '/var/lib/kolla/config_files' and parts[2] == 'ro':
            matches.append(Path(parts[0]))
    if len(matches) != 1 or not matches[0].is_absolute():
        raise ValueError('Exactly one read-only Kolla configuration bind is required')
    return matches[0]


def config_hash(directory):
    """Hash generated configuration bytes, never include credentials in evidence."""
    directory = Path(directory)
    if directory.is_symlink() or not (directory/'config.json').is_file():
        raise ValueError('Missing or unsafe generated metadata configuration')
    digest = hashlib.sha256()
    for path in sorted(directory.rglob('*')):
        if path.is_symlink():
            raise ValueError('Metadata configuration must not contain symlinks')
        if path.is_file() and not (path.parent == directory and path.name in (MANIFEST, MANIFEST+'.tmp')):
            relative = path.relative_to(directory).as_posix().encode()
            raw = path.read_bytes()
            digest.update(len(relative).to_bytes(8, 'big') + relative)
            digest.update(len(raw).to_bytes(8, 'big') + raw)
    return digest.hexdigest()


def save_manifest(directory, value):
    path = Path(directory)/MANIFEST
    temporary = path.with_name(MANIFEST+'.tmp')
    with open(temporary, 'w', opener=lambda p, f: os.open(p, f, 0o600)) as output:
        json.dump(value, output, indent=2, sort_keys=True)
        output.write('\n')
    temporary.chmod(0o600)
    temporary.replace(path)


def require_run(run):
    if not isinstance(run, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', run):
        raise ValueError('Invalid metadata preparation run identity')


def container(name):
    result = command(['docker', 'container', 'inspect', name], check=False)
    if result.returncode:
        if 'No such container' in result.stderr or 'No such object' in result.stderr:
            return None
        raise RuntimeError('Cannot inspect metadata container: ' + result.stderr.strip())
    rows = json.loads(result.stdout)
    if len(rows) != 1:
        raise ValueError('Ambiguous metadata container inspection')
    return rows[0]


def preparation(worker):
    """Use native Kolla creation options, suppress only unit auto-enablement."""
    params = worker.params
    run = params.get('labels', {}).get(RUN_LABEL)
    require_run(run)
    if params.get('name') != NAME or params.get('container_engine') != 'docker':
        raise ValueError('Preparation is restricted to the OVN metadata Docker container')
    if not params.get('detach') or params.get('restart_policy') == 'oneshot':
        raise ValueError('Unsupported metadata container lifecycle')
    directory = config_directory(params.get('volumes', []))
    digest = config_hash(directory)
    params['labels'][HASH_LABEL] = digest
    # Native comparison normally expects a running container. Preparation
    # deliberately expects Docker's never-started state instead.
    params['state'] = 'created'
    current = worker.check_container()
    if current:
        details = worker.dc.inspect_container(NAME)
        if details['State'].get('Status') != 'created' or details['State'].get('Running'):
            raise ValueError('Existing OVN metadata container has already run; refusing preparation')
        if (details['Config'].get('Labels') or {}).get(RUN_LABEL) != run:
            raise ValueError('Existing metadata container is not owned by this run')
    if not worker.check_image():
        raise ValueError('Metadata image is not local; complete image preflight before preparation')
    # Disable before creation as well: a stale enabled unit must not start the
    # newly created container if a host reboots between preparation tasks.
    state = command(['systemctl', 'is-active', UNIT], check=False)
    if state.stdout.strip() not in ('inactive', 'failed', 'unknown', ''):
        raise ValueError('OVN metadata systemd unit is active before preparation')
    enabled = command(['systemctl', 'is-enabled', UNIT], check=False).stdout.strip()
    if enabled not in ('disabled', 'not-found', ''):
        command(['systemctl', 'disable', UNIT])
    # A temporary adapter object changes no installed Kolla source. In
    # particular, create_unit_file() must not enable the service before freeze.
    native_enable = worker.systemd.enable
    worker.systemd.enable = lambda: False
    try:
        if current and worker.check_container_differs():
            worker.remove_container()
            current = None
        if not current:
            worker.create_container()
        else:
            worker.changed |= worker.systemd.create_unit_file()
    finally:
        worker.systemd.enable = native_enable
    command(['systemctl', 'disable', UNIT])
    if command(['systemctl', 'is-enabled', UNIT], check=False).stdout.strip() != 'disabled':
        raise ValueError('Prepared metadata unit is not disabled')
    details = worker.dc.inspect_container(NAME)
    if details['State'].get('Status') != 'created' or details['State'].get('Running'):
        raise ValueError('Prepared metadata container unexpectedly started')
    if details.get('HostConfig', {}).get('RestartPolicy', {}).get('Name', '') not in ('', 'no'):
        raise ValueError('Prepared metadata container can auto-start through Docker')
    value = dict(schema=1, status='PREPARED', run=run, container=NAME,
                 container_id=details['Id'], image_id=details['Image'],
                 config_directory=str(directory), config_sha256=digest, unit=UNIT)
    # Keep the original timestamp on an unchanged preparation retry.
    path = directory/MANIFEST
    if path.exists():
        old = json.loads(path.read_text())
        if all(old.get(k) == v for k, v in value.items()):
            return old
    value['prepared_at'] = time.time()
    save_manifest(directory, value)
    return value


def verify(cfg, allow_started=False):
    require_run(cfg['run'])
    details = container(NAME)
    if details is None:
        raise ValueError('Prepared metadata container is missing; do not deploy it after freeze')
    labels = details['Config'].get('Labels') or {}
    if labels.get(RUN_LABEL) != cfg['run']:
        raise ValueError('Metadata container belongs to another run')
    directory = config_directory(details['HostConfig'].get('Binds', []))
    value = json.loads((directory/MANIFEST).read_text())
    digest = config_hash(directory)
    expected = dict(schema=1, run=cfg['run'], container=NAME,
                    container_id=details['Id'], image_id=details['Image'],
                    config_directory=str(directory), config_sha256=digest, unit=UNIT)
    if any(value.get(k) != v for k, v in expected.items()) or labels.get(HASH_LABEL) != digest:
        raise ValueError('Metadata preparation identity or configuration changed')
    checkpoint = cfg.get('expected', {})
    if checkpoint and any(value.get(k) != checkpoint.get(k)
                          for k in ('container_id', 'image_id', 'config_sha256')):
        raise ValueError('Host metadata preparation differs from deployment checkpoint')
    if not allow_started:
        if details['State'].get('Status') != 'created' or details['State'].get('Running'):
            raise ValueError('Metadata must remain unstarted before handoff')
        if details.get('HostConfig', {}).get('RestartPolicy', {}).get('Name', '') not in ('', 'no'):
            raise ValueError('Prepared metadata Docker auto-start was enabled before handoff')
        if command(['systemctl', 'is-enabled', UNIT], check=False).stdout.strip() != 'disabled':
            raise ValueError('Prepared metadata unit was enabled before handoff')
    return value, details


def start(cfg):
    value, _ = verify(cfg)
    # Stop the old agent on this host immediately before using its socket.
    # Computes without an old metadata agent need no legacy stop operation.
    load = command(['systemctl', 'show', OLD_UNIT, '--property=LoadState', '--value'], check=False)
    load_state = load.stdout.strip()
    if load_state not in ('loaded', 'masked', 'not-found'):
        raise ValueError('Cannot determine legacy metadata unit state: ' + load_state)
    # Disable a loaded old unit even if its container is currently absent: it
    # may otherwise restart and claim the shared socket after the new agent.
    if load_state in ('loaded', 'masked'):
        command(['systemctl', 'disable', '--now', OLD_UNIT])
    old = container(OLD_NAME)
    if old is not None and old['State'].get('Running'):
        raise ValueError('Legacy metadata agent is still running')
    command(['systemctl', 'enable', UNIT])
    command(['systemctl', 'start', UNIT])
    value.update(status='STARTED', activated_at=time.time())
    save_manifest(value['config_directory'], value)
    # Do not wait for chassis registration or metadata HTTP readiness here.
    # Controller start is a later task and must not be blocked by this agent.
    return dict(status='STARTED', container_id=value['container_id'],
                activated_at=value['activated_at'])


def ready(cfg):
    deadline = time.monotonic() + cfg.get('timeout', 300)
    last = 'not checked'
    while time.monotonic() < deadline:
        value, details = verify(cfg, allow_started=True)
        state = details['State']
        last = state.get('Health', {}).get('Status', 'no-healthcheck')
        if value.get('status') != 'STARTED':
            raise ValueError('Metadata activation checkpoint is missing')
        if state.get('Running') and last in ('healthy', 'no-healthcheck'):
            return dict(status='PASS', container_id=value['container_id'], health=last)
        time.sleep(min(cfg.get('poll', 2), max(0, deadline-time.monotonic())))
    raise TimeoutError('Metadata container did not become ready: ' + last)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('verify', 'start', 'ready'))
    parser.add_argument('config')
    args = parser.parse_args()
    cfg = json.loads(args.config)
    if cfg.get('timeout', 300) <= 0 or cfg.get('poll', 2) <= 0:
        parser.error('Positive timeout and poll interval required')
    result = (verify(cfg)[0] if args.action == 'verify' else
              start(cfg) if args.action == 'start' else ready(cfg))
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
