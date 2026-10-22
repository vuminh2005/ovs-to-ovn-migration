#!/usr/bin/env python3
"""Use Kolla's own image resolution with a pull-if-missing policy, before freeze.

Only a temporary copy of the installed Ansible tree is changed. The installed
Kolla files, image tags, containers, volumes and network state are untouched.
"""

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


def kolla_tree(binary):
    binary = Path(binary).resolve()
    candidates = [binary.parent.parent/'share/kolla-ansible/ansible']
    # Source/editable installations keep the entrypoint under tools/.
    if binary.parent.name == 'tools':
        candidates.insert(0, binary.parent.parent/'ansible')
    candidates.extend(link.read_text().splitlines()[0].strip() + '/ansible'
                      for link in binary.parent.parent.glob('lib/python*/*-packages/kolla-ansible.egg-link'))
    for candidate in map(Path, candidates):
        if (candidate/'roles/service-images-pull/tasks/main.yml').is_file():
            return candidate.resolve()
    raise ValueError('Cannot locate installed Kolla Ansible tree for ' + str(binary))


def prepare_tree(source, destination):
    """Keep native role defaults, inventory precedence, filters and credentials."""
    shutil.copytree(source, destination, symlinks=False,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    taskfile = destination/'roles/service-images-pull/tasks/main.yml'
    tasks = yaml.safe_load(taskfile.read_text())
    if (not isinstance(tasks, list) or len(tasks) != 1 or
            tasks[0].get('kolla_container', {}).get('action') != 'pull_image'):
        raise ValueError('Unsupported Kolla image-pull task; installed files were not modified')
    original = tasks[0]
    original['name'] = '{{ kolla_role_name | default(project_name) }} | Ensure local images or pull missing images'
    original['kolla_container']['action'] = 'ensure_image'
    # Reuse the exact native service/host selection. Inspect only non-secret
    # identity fields, rather than persisting Docker's image environment.
    fields = '{"id":{{json .Id}},"repo_tags":{{json .RepoTags}},"repo_digests":{{json .RepoDigests}},"architecture":{{json .Architecture}},"os":{{json .Os}}}'
    tasks.extend([
        dict(name='Record resolved local migration image identities', become=True,
             **{'ansible.builtin.command': dict(argv=['docker', 'image', 'inspect',
                  '--format', '{% raw %}' + fields + '{% endraw %}', '{{ service.image }}']),
                'vars': original['vars'], 'with_dict': original['with_dict'],
                'loop_control': original['loop_control'], 'changed_when': False,
                'register': 'migration_image_identity'}),
        dict(name='Persist per-host migration image evidence on deployment host',
             **{'ansible.builtin.copy': dict(
                    dest="{{ migration_image_evidence_dir }}/{{ inventory_hostname }}.{{ kolla_role_name | default(project_name) }}.json",
                    mode='0600', content='{{ migration_image_identity.results | map(attribute="stdout") | map("from_json") | list | to_nice_json }}\n'),
                'delegate_to': 'localhost', 'become': True}),
    ])
    taskfile.write_text(yaml.safe_dump(tasks, sort_keys=False))
    # A missing group must not turn a skipped native play into a successful
    # preflight. The normal migration precheck already requires these groups.
    preflight = [dict(name='Require all target image placements before preparation',
        hosts='localhost', gather_facts=False, tags=['always'], tasks=[
            {'ansible.builtin.assert': {'that': [
                "groups.get(item, []) | length > 0", "kolla_container_engine == 'docker'"],
                'fail_msg': 'Missing target service placement or unsupported container engine'},
             'loop': ['ovn-northd', 'ovn-nb-db', 'ovn-sb-db', 'ovn-controller',
                      'neutron-server', 'neutron-ovn-metadata-agent']}]),
        {'import_playbook': 'site.yml'}]
    entry = destination/'migration-image-preflight.yml'
    entry.write_text(yaml.safe_dump(preflight, sort_keys=False))
    return entry


def run(root, binary, inventory, globals_path):
    root = Path(root).resolve()
    if not (run_path(root, 'runtime.json')).is_file():
        raise ValueError('An existing migration run is required')
    source = kolla_tree(binary)
    evidence = run_path(root, 'image-preflight')/str(uuid.uuid4())
    evidence.mkdir(parents=True, mode=0o700)
    metadata = dict(status='STARTED', policy='missing', started_at=time.time(),
                    kolla_ansible=str(binary), kolla_ansible_tree=str(source),
                    source_pull_task_sha256=hashlib.sha256(
                        (source/'roles/service-images-pull/tasks/main.yml').read_bytes()).hexdigest(),
                    inventory=str(inventory), globals=str(globals_path),
                    services=['ovn-db', 'ovn-controller', 'neutron'],
                    context='Target OVN image selection only; no target config or container changes')
    result_path = evidence/'result.json'
    result_path.touch(mode=0o600)
    result_path.write_text(json.dumps(metadata, indent=2)+'\n')
    try:
        with tempfile.TemporaryDirectory(prefix='ovn-migration-images-') as temporary:
            entry = prepare_tree(source, Path(temporary)/'ansible')
            extra = Path(temporary)/'image-options.json'
            extra.write_text(json.dumps(dict(migration_image_evidence_dir=str(evidence),
                enable_ovn='yes', neutron_plugin_agent='ovn', neutron_ovn_dhcp_agent='no',
                neutron_enable_ovn_agent='no', neutron_ovn_distributed_fip='no')))
            command = [str(binary), '-i', str(inventory), '--configdir', str(Path(globals_path).parent),
                       '--playbook', str(entry), 'pull', '--tags', 'ovn-db,ovn-controller,neutron']
            # Kolla loads globals.yml, then globals.d/*.yml and passwords itself.
            # Loading globals.yml again would incorrectly override globals.d.
            if Path(globals_path).name != 'globals.yml':
                command.extend(['-e', '@'+str(globals_path)])
            command.extend(['-e', '@'+str(extra)])
            log = evidence/'kolla-images.log'
            with open(log, 'x', opener=lambda p, f: os.open(p, f, 0o600)) as output:
                completed = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT)
            metadata.update(status='PASS' if completed.returncode == 0 else 'FAIL',
                            returncode=completed.returncode)
            return completed.returncode
    except Exception as exc:
        metadata.update(status='FAIL', error=str(exc))
        raise
    finally:
        metadata['ended_at'] = time.time()
        result_path.write_text(json.dumps(metadata, indent=2)+'\n')
        print('Migration image preflight: '+metadata['status']+'; evidence: '+str(evidence), flush=True)
        if metadata['status'] != 'PASS':
            print('Inspect kolla-images.log; no Neutron freeze was performed by this helper.', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('--kolla-ansible', required=True, type=Path)
    parser.add_argument('--inventory', required=True, type=Path)
    parser.add_argument('--globals', required=True, dest='globals_path', type=Path)
    args = parser.parse_args()
    raise SystemExit(run(args.root, args.kolla_ansible, args.inventory, args.globals_path))


if __name__ == '__main__':
    main()
