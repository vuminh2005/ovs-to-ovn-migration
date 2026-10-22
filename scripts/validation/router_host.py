"""Resolve each source router independently before freeze; ARM uses its checkpoint."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import uuid

from _paths import playbook_path
from run_layout import run_path


def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2)+'\n')
    temporary.chmod(0o600)
    temporary.replace(path)


def select(router, hosts, observations):
    namespace = 'qrouter-'+str(uuid.UUID(router))
    if not hosts or set(observations) != set(hosts):
        raise RuntimeError('Router discovery did not inspect every network host')
    matches = [h for h in hosts if namespace in observations[h]]
    if len(matches) != 1:
        raise RuntimeError(f'Expected one source namespace for router {router}; found {matches}')
    return matches[0]


def resolve(validation, router, action):
    cfg, root = validation.cfg, Path(validation.root)
    # Historical single-network checkpoints keep their original transport host.
    if not cfg.get('topology_discovery'):
        return cfg['pair_d_network_host']
    if action not in ('CHECK', 'ARM'):
        raise ValueError('Unsupported router control action')
    hosts = cfg['network_hosts']
    inventory = Path(cfg['inventory'])
    fingerprint = hashlib.sha256(inventory.read_bytes()).hexdigest()
    if inventory.read_bytes() != run_path(root, 'inventory.before').read_bytes():
        raise RuntimeError('Inventory changed since migration backup')
    path = run_path(root, 'router-host-checkpoint.json')
    state = json.loads(path.read_text()) if path.exists() else dict(run=cfg['run'], inventory_sha256=fingerprint, routers={})
    if state.get('run') != cfg['run'] or state.get('inventory_sha256') != fingerprint:
        raise RuntimeError('Router host checkpoint identity changed')
    previous = state['routers'].get(router)
    frozen = run_path(root, 'metrics/control_plane_downtime.start').exists()
    if action == 'ARM':
        if not frozen or not previous or previous not in hosts:
            raise RuntimeError('Router host discovery must finish before freeze')
        return previous
    if frozen:
        raise RuntimeError('Do not rediscover or rebase router hosts after freeze')
    with tempfile.TemporaryDirectory(prefix='router-host-', dir=root) as directory:
        extra = Path(directory)/'request.json'
        save(extra, dict(router_host_observations=directory))
        completed = subprocess.run(['ansible-playbook', '-i', str(inventory),
            str(playbook_path('router-host-tasks.yml')), '-e', '@'+str(extra)],
            capture_output=True, text=True, timeout=cfg['timeout'])
        log = run_path(root, 'router-host-'+str(uuid.UUID(router))+'.log')
        log.write_text(completed.stdout+'\n'+completed.stderr)
        log.chmod(0o600)
        if completed.returncode:
            raise RuntimeError('Router namespace discovery failed; inspect '+str(log))
        observations = {}
        for item in Path(directory).glob('host-*.json'):
            row = json.loads(item.read_text())
            if row['host'] in observations:
                raise RuntimeError('Duplicate router host observation')
            observations[row['host']] = [line.split()[0] for line in row['namespaces'] if line.strip()]
        host = select(router, hosts, observations)
    if previous and previous != host:
        raise RuntimeError('Source router moved since its checkpoint; refusing to retarget an existing run')
    state['routers'][router] = host
    save(path, state)
    return host
