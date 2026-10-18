#!/usr/bin/env python3
"""Run-scoped, immutable preparation inputs consumed after Neutron is frozen."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import time


def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True))
    temporary.chmod(0o600)
    temporary.replace(path)


def cleanup(parent):
    candidates = []
    # Validate every candidate before removing any directory; never follow links.
    for path in sorted(parent.glob('ovs-to-ovn-preparation-*')):
        owner_path = path/'owner.json'
        if path.is_symlink() or not path.is_dir() or owner_path.is_symlink():
            raise ValueError('Unsafe preparation directory: '+str(path))
        owner = json.loads(owner_path.read_text())
        if (not re.fullmatch(r'[A-Za-z0-9_-]+', owner.get('run', '')) or
                path.name != 'ovs-to-ovn-preparation-'+owner['run'] or
                owner.get('directory') != str(path) or
                not re.fullmatch(r'[0-9a-f]{64}', owner.get('inventory_sha256', ''))):
            raise ValueError('Preparation ownership not proven: '+str(path))
        candidates.append((path, owner))
    for path, _ in candidates:
        shutil.rmtree(path)
    return dict(status='PASS', removed=[dict(path=str(p), owner=o) for p,o in candidates])


def inputs(root):
    from run_layout import run_path

    runtime = json.loads((run_path(root, 'runtime.json')).read_text())
    run = runtime['run_id']
    if run != root.name or not re.fullmatch(r'[A-Za-z0-9_-]+', run):
        raise ValueError('Preparation run identity differs')
    inventory = Path(runtime['inventory'])
    if inventory.read_bytes() != (run_path(root, 'inventory.before')).read_bytes():
        raise ValueError('Inventory changed since migration backup')
    ports = (run_path(root, 'compute-port-ids.txt')).read_bytes()
    return dict(run=run, inventory=str(inventory),
                inventory_sha256=hashlib.sha256(inventory.read_bytes()).hexdigest(),
                ports_sha256=hashlib.sha256(ports).hexdigest(),
                port_count=sum(bool(line.strip()) for line in ports.splitlines()),
                ovn_cli_host=runtime['ovn_cli_host'], db_sync_host=runtime['db_sync_host'])


def verify(root):
    from run_layout import run_path

    value = json.loads((run_path(root, 'cutover-preparation.json')).read_text())
    if value.get('schema') != 1 or value.get('status') != 'PASS' or any(
            value.get(k) != v for k,v in inputs(root).items()):
        raise ValueError('Cutover preparation does not match this run/inventory/VM ports')
    expected = '/tmp/ovs-to-ovn-preparation-'+value['run']
    if (value.get('directory') != expected or value.get('ports_path') != expected+'/compute-port-ids.txt'
            or type(value.get('binding_timeout')) is not int or value['binding_timeout'] <= 0
            or not isinstance(value.get('neutron_image'), str) or not value['neutron_image']):
        raise ValueError('Invalid cutover preparation paths/timeout/image')
    return value


def prepare(root, image, base, per_port, maximum):
    # Reset cleanup is transferred alone by ansible.builtin.script. Keep this
    # controller-side dependency confined to the migration actions.
    from run_layout import run_path

    if (run_path(root, 'metrics/control_plane_downtime.start')).exists():
        raise ValueError('Preparation must complete before Neutron freeze; do not rebase frozen runs')
    value = inputs(root)
    if min(base, maximum) <= 0 or per_port < 0 or not image.strip():
        raise ValueError('Invalid preparation settings')
    directory = '/tmp/ovs-to-ovn-preparation-'+value['run']
    value.update(schema=1, status='PASS', directory=directory,
                 ports_path=directory+'/compute-port-ids.txt', neutron_image=image.strip(),
                 binding_timeout=min(base+value['port_count']*per_port, maximum))
    path = run_path(root, 'cutover-preparation.json')
    if path.exists():
        old = verify(root)
        if any(old.get(k) != v for k,v in value.items()):
            raise ValueError('Prepared cutover inputs changed; refusing to overwrite checkpoint')
        return old
    save(path, dict(value, prepared_at=time.time()))
    return verify(root)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare','verify','cleanup'])
    parser.add_argument('root', type=Path)
    parser.add_argument('--image')
    parser.add_argument('--base', type=int, default=120)
    parser.add_argument('--per-port', type=int, default=2)
    parser.add_argument('--maximum', type=int, default=900)
    args = parser.parse_args()
    result = (cleanup(args.root) if args.action == 'cleanup' else
              prepare(args.root, args.image or '', args.base, args.per_port, args.maximum)
              if args.action == 'prepare' else verify(args.root))
    print(json.dumps(result, sort_keys=True))
