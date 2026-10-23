#!/usr/bin/env python3
"""Prepare native Kolla OVN metadata containers without starting them."""
import sys as _sys
from pathlib import Path as _Path
_source = _Path(__file__).resolve().parents[1]
if _source.name == 'scripts' and (_source / '_paths.py').is_file():
    _sys.path.insert(0, str(_source))
    from _paths import configure as _configure
    _configure()
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import uuid

import yaml

from prepare_migration_images import kolla_tree
from run_layout import run_path


def prepare_tree(source, destination):
    """Reuse native config generation, service selection and handler options."""
    source, destination = Path(source), Path(destination)
    handler_path = source/'roles/neutron/handlers/main.yml'
    handlers = yaml.safe_load(handler_path.read_text())
    matches = [h for h in handlers if h.get('name') == 'Restart neutron-ovn-metadata-agent container']
    if len(matches) != 1:
        raise ValueError('Unsupported installed Kolla metadata handler')
    handler = matches[0]
    options = handler.get('kolla_container')
    if (not isinstance(options, dict) or options.get('action') != 'recreate_or_restart_container'
            or handler.get('vars', {}).get('service_name') != 'neutron-ovn-metadata-agent'
            or 'kolla_action != "config"' not in handler.get('when', [])):
        raise ValueError('Unsupported installed Kolla metadata creation options')
    # Fail before any host changes if the native 2024.1 API shape is different.
    for filename, methods in (
            ('library/kolla_container.py', ('generate_module',)),
            ('module_utils/kolla_docker_worker.py', ('create_container', 'build_container_options')),
            ('module_utils/kolla_systemd_worker.py', ('create_unit_file', 'enable'))):
        tree = ast.parse((source/filename).read_text())
        functions = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
        if not set(methods) <= functions:
            raise ValueError('Unsupported installed Kolla API: ' + filename)
    shutil.copytree(source, destination, symlinks=False,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    root = Path(__file__).resolve().parents[2]
    shutil.copyfile(root/'plugins/modules/migration_metadata_container.py',
                    destination/'library/migration_metadata_container.py')
    shutil.copyfile(source/'library/kolla_container.py', destination/'module_utils/migration_kolla_api.py')
    shutil.copyfile(Path(__file__).with_name('metadata_handoff.py'),
                    destination/'module_utils/migration_metadata_common.py')
    original = destination/'roles/neutron/tasks/config.yml'
    original.rename(original.with_name('migration-metadata-config.yml'))
    options = dict(options)
    options.update(action='compare_container', labels="{{ (service.labels | default({})) | combine({'ovn-migration.metadata.run': migration_metadata_run}) }}")
    tasks = [
        {'import_tasks': 'migration-metadata-config.yml'},
        {'name': 'Require metadata-only preparation with native config-only handlers',
         'ansible.builtin.assert': {'that': ["kolla_action == 'config'", "kolla_container_engine == 'docker'",
             "inventory_hostname in groups['neutron-ovn-metadata-agent']",
             "neutron_services['neutron-ovn-metadata-agent'].enabled | bool"]}},
        {'name': 'Prepare OVN metadata container without starting or enabling it',
         'become': True, 'vars': handler['vars'],
         'migration_metadata_container': options,
         'register': 'migration_metadata_preparation'},
        {'name': 'Save per-host metadata preparation identity on deployment host',
         'delegate_to': 'localhost', 'become': True,
         'ansible.builtin.copy': {'dest': '{{ migration_metadata_evidence }}/{{ inventory_hostname }}.json',
             'mode': '0600', 'content': '{{ migration_metadata_preparation.preparation | to_nice_json }}\n'}},
    ]
    original.write_text(yaml.safe_dump(tasks, sort_keys=False))
    return destination/'site.yml'


def run(root, binary, inventory, globals_path):
    root = Path(root).resolve()
    if (run_path(root, 'metrics/control_plane_downtime.start')).exists():
        raise ValueError('Metadata preparation is prohibited after Neutron freeze')
    runtime = json.loads(run_path(root, 'runtime.json').read_text())
    if runtime['run_id'] != root.name or str(Path(runtime['inventory']).resolve()) != str(Path(inventory).resolve()):
        raise ValueError('Metadata preparation run/inventory differs')
    if Path(inventory).read_bytes() != run_path(root, 'inventory.before').read_bytes():
        raise ValueError('Inventory changed since migration backup')
    source = kolla_tree(binary)
    evidence = run_path(root, 'metadata-preparation.json').parent/'metadata-preparation'/str(uuid.uuid4())
    evidence.mkdir(parents=True, mode=0o700, exist_ok=True)
    value = dict(schema=1, status='STARTED', run=runtime['run_id'], started_at=time.time(),
                 attempt=str(evidence),
                 inventory_sha256=hashlib.sha256(Path(inventory).read_bytes()).hexdigest(),
                 source_handler_sha256=hashlib.sha256((source/'roles/neutron/handlers/main.yml').read_bytes()).hexdigest())
    log = evidence/'kolla-prepare.log'
    try:
        with tempfile.TemporaryDirectory(prefix='ovn-metadata-prepare-') as temporary:
            entry = prepare_tree(source, Path(temporary)/'ansible')
            extra = Path(temporary)/'metadata-options.json'
            extra.write_text(json.dumps(dict(migration_metadata_run=runtime['run_id'],
                                             migration_metadata_evidence=str(evidence))))
            argv = [str(binary), '-i', str(inventory), '--configdir', str(Path(globals_path).parent),
                    '--playbook', str(entry), 'genconfig', '--tags', 'neutron',
                    '--limit', 'neutron-ovn-metadata-agent']
            if Path(globals_path).name != 'globals.yml':
                argv.extend(['-e', '@'+str(globals_path)])
            argv.extend(['-e', '@'+str(extra)])
            with open(log, 'w', opener=lambda p, f: os.open(p, f, 0o600)) as output:
                result = subprocess.run(argv, stdout=output, stderr=subprocess.STDOUT)
            if result.returncode:
                raise RuntimeError('Kolla metadata preparation failed; inspect ' + str(log))
        hosts = {p.stem: json.loads(p.read_text()) for p in evidence.glob('*.json')}
        if not hosts or any(v.get('run') != runtime['run_id'] or v.get('status') != 'PREPARED' for v in hosts.values()):
            raise ValueError('No complete per-host metadata preparation evidence')
        value.update(status='PASS', hosts=hosts)
        return value
    except Exception as exc:
        value.update(status='FAIL', error=str(exc))
        raise
    finally:
        value['ended_at'] = time.time()
        path = run_path(root, 'metadata-preparation.json')
        path.write_text(json.dumps(value, indent=2, sort_keys=True)+'\n')
        path.chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('--kolla-ansible', required=True, type=Path)
    parser.add_argument('--inventory', required=True, type=Path)
    parser.add_argument('--globals', required=True, dest='globals_path', type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.root, args.kolla_ansible, args.inventory, args.globals_path), sort_keys=True))


if __name__ == '__main__':
    main()
