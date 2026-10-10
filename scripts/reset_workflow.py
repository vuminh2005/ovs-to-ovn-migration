#!/usr/bin/env python3
"""Work Item 2 controller journal. Explicit destroy once; retry only later stages."""
import argparse
import copy
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import time

import yaml

from ew_provision import validated_spec
from mtu_plan import calculate, config_values, template_header
from reset_host_preflight import identity, require

REPO = pathlib.Path(__file__).resolve().parents[1]
# Reviewed upstream 18.8.0 destroy behavior. Different installed scripts require
# source review, not guessing which paths they remove. No network fetch at runtime.
DESTROY_PINS = {
    'ansible/destroy.yml': '25179ceb9a029fdc4508a7a45807668590fe0d5ccfb0ab708031b690a53cee87',
    'ansible/roles/destroy/tasks/validate_docker_execute.yml': 'ea9deceb2566b2ace7ace928740c177c7c8c0f7912588e897e070edf3c4d3cec',
    'tools/cleanup-host': '1430a2d789834521e0274d919159aa339e8f6387b358ace5ba8d5b2f3c964341',
    'tools/cleanup-containers': '99b625cd43de9c21ceed178ed04f0dc1267208765151b54c95c72dcead75f891',
    'tools/cleanup-images': '018f2882fb55b1bb16a344d00fb32178c139520ce74aa5831722d4f34c04e190',
    'ansible/roles/destroy/tasks/main.yml': '8470d5dc26ff8ac64ed937415941561f59c17f6cad7e8196ca4eea5bc81e9856',
    'ansible/roles/destroy/tasks/cleanup_host.yml': 'a8e50aba84130bb023a50f39b6e568fda28f5645631cb431fe629cc6985f051a',
    'ansible/roles/destroy/tasks/cleanup_containers.yml': 'a8832b3c4e5bb08d1f3be4bda5d3d78ac474def147a743c0254363af91b2eade',
    'ansible/roles/destroy/tasks/cleanup_images.yml': '4c251aa5c14392856447ff407c7400955a8a2687672afe756ec27ee75fc12c5e',
}
DATA_VOLUMES = dict(glance_file_datadir_volume='glance', nova_instance_datadir_volume='nova_compute',
                    gnocchi_metric_datadir_volume='gnocchi', influxdb_datadir_volume='influxdb',
                    opensearch_datadir_volume='opensearch')


def save(path, value):
    """Root-private atomic publication with both file and directory fsync."""
    path = pathlib.Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name+'.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(value, stream, indent=2, sort_keys=True); stream.write('\n')
        stream.flush(); os.fsync(stream.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


def digest(path):
    value = hashlib.sha256()
    with pathlib.Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''): value.update(chunk)
    return value.hexdigest()


def truth(value):
    return str(value).lower() in ('true', 'yes', '1')


def management_trust(spec, inventory):
    """Keep connection options, but make the first SSH trust options authoritative."""
    from ansible import constants as C
    from ansible.parsing.dataloader import DataLoader
    from ansible.plugins.loader import connection_loader
    from ansible.template import Templar
    connection_loader.get('ssh', class_only=True)  # register installed plugin options
    known = spec['host_known_hosts']
    require(pathlib.Path(known).is_absolute() and not any(c in known for c in ('%', '\r', '\n', '\0')),
            'Management known-hosts path must be absolute and literal (no SSH tokens/newlines)')
    # The inner quotes belong to SSH's config parser; the outer quoting belongs
    # to Ansible's argument splitter. This also supports paths containing spaces.
    quoted = '"'+known.replace('\\', '\\\\').replace('"', '\\"')+'"'
    strict = shlex.join(['-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile='+quoted,
                         '-o', 'UpdateHostKeys=no', '-o', 'ControlMaster=no', '-o', 'ControlPath=none'])
    def options(variables):
        template = Templar(loader=DataLoader(), variables=variables)
        effective = C.config.get_plugin_options('connection', 'ssh', variables=variables)
        return {key:strict+' '+template.template(effective[key] or '', fail_on_undefined=True)
                for key in ('ssh_args', 'ssh_common_args')}
    defaults = options({})
    hosts = {}
    for host in spec['hosts']:
        variables = dict(inventory['_meta']['hostvars'][host], inventory_hostname=host)
        require(variables.get('ansible_connection', C.DEFAULT_TRANSPORT) in ('ssh', 'local', 'ansible.builtin.ssh', 'ansible.builtin.local'),
                'Reset management trust requires the SSH or local connection plugin: '+host)
        value = options(variables)
        # ansible_host resolves to the delegated target for delegated connections.
        address = Templar(loader=DataLoader(), variables=variables).template(variables.get('ansible_host', host))
        for name in (host, address):
            require(name not in hosts or hosts[name] == value, 'Ambiguous management connection options: '+name)
            hosts[name] = value
    lookup = 'reset_management_ssh.get(ansible_host | default(inventory_hostname), reset_management_ssh.get(inventory_hostname, reset_management_ssh_defaults))'
    return dict(reset_management_ssh=hosts, reset_management_ssh_defaults=defaults,
                ansible_ssh_args='{{ '+lookup+"['ssh_args'] }}",
                ansible_ssh_common_args='{{ '+lookup+"['ssh_common_args'] }}",
                ansible_host_key_checking=True, ansible_ssh_host_key_checking=True)


@contextlib.contextmanager
def locked(path):
    with open(path, 'a', opener=lambda p,f:os.open(p,f,0o600)) as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another reset/provisioning operation holds the state lock; no reset stage may begin') from None
        yield


def source_configuration(spec):
    source = pathlib.Path(spec['source_globals'])
    values = yaml.safe_load(source.read_text())
    require(isinstance(values, dict), 'Authoritative source globals must be a mapping')
    expected = dict(openstack_release='2024.1', kolla_base_distro='ubuntu', kolla_base_distro_version='noble',
                    neutron_plugin_agent='openvswitch', neutron_tenant_network_types='vxlan',
                    enable_openvswitch='yes', enable_ovn='no', enable_neutron_dvr='no',
                    enable_neutron_agent_ha='no', enable_neutron_provider_networks='no')
    for key, value in expected.items():
        require(key in values and (truth(values[key]) == truth(value) if key.startswith('enable_') else str(values[key]).lower() == value),
                'Authoritative OVS source globals mismatch: '+key)
    for key in ('network_interface', 'api_interface', 'tunnel_interface', 'neutron_external_interface',
                'kolla_internal_vip_address', 'kolla_external_vip_address', 'nova_compute_virt_type'):
        require(isinstance(values.get(key), str) and values[key] and '{{' not in values[key], 'Explicit resolved source setting required: '+key)
    require(not truth(values.get('enable_swift', 'no')) and not truth(values.get('enable_octavia', 'no')),
            'Swift/Octavia reset is outside this lab workflow')
    require(pathlib.Path(values.get('node_custom_config', spec['config_path'])).resolve() == pathlib.Path(spec['config_path']).resolve(),
            'Source node_custom_config differs from reset_config_path')
    require(values.get('node_config', '/etc/kolla') == '/etc/kolla', 'Source node_config must remain /etc/kolla for existing migration gates')
    config = pathlib.Path(spec['source_config'])
    for name in ('neutron.conf', 'neutron/ml2_conf.ini', 'neutron/openvswitch_agent.ini'):
        require((config/name).is_file(), 'Missing retained source override: '+str(config/name))
    # These are Kolla's existing supported Neutron override locations.
    neutron = (config/'neutron.conf').read_text()
    ml2 = (config/'neutron/ml2_conf.ini').read_text()
    firewall = (config/'neutron/openvswitch_agent.ini').read_text()
    import configparser
    parser = configparser.ConfigParser(); parser.read_string(firewall)
    require(parser.get('securitygroup', 'firewall_driver', fallback='') == 'openvswitch', 'Source native OVS firewall override required')
    n = configparser.ConfigParser(); n.read_string(neutron)
    m = configparser.ConfigParser(); m.read_string(ml2)
    require(n.has_option('DEFAULT', 'global_physnet_mtu') and m.has_option('ml2', 'path_mtu') and
            m.has_option('ml2', 'overlay_ip_version'), 'Explicit source global/path MTU and IPv4 overrides required; no reduced tenant baseline')
    settings = config_values(neutron, ml2)
    for key in ('mechanism_drivers', 'tenant_network_types'):
        require(not settings[key] or settings[key] == {'mechanism_drivers':'openvswitch', 'tenant_network_types':'vxlan'}[key],
                'Source ML2 override conflicts with OVS/VXLAN: '+key)
    settings.update(mechanism_drivers='openvswitch', tenant_network_types='vxlan')
    return values, settings


def retained_files(spec):
    require(spec['provision_spec']['image'].get('sha256_file'), 'Reset requires the retained QCOW2 SHA256 sidecar')
    required = [spec[k] for k in ('source_globals', 'source_config', 'inventory', 'passwords', 'kolla_venv',
                                  'access_file', 'credentials_file', 'guest_key', 'host_known_hosts')]
    required += [str(REPO), spec['provision_spec']['image']['path'], spec['provision_spec']['image']['sha256_file'],
                 spec['provision_spec']['keypair_public_key'], *spec['provision_spec']['secrets'].values()]
    required += [b['path'] for b in spec['provision_spec']['bootstrap'].values()]
    required += spec['required_paths']
    if spec['openrc'] != '/etc/kolla/admin-openrc.sh': required.append(spec['openrc'])
    for key in spec['host_keys']:
        if key: required.append(key)
    if spec['forward'].get('mtu_kolla_ml2_template_path'):
        required.append(spec['forward']['mtu_kolla_ml2_template_path'])
    # A retained directory can contain symlinks into otherwise deleted storage.
    # Resolve those aliases too; a top-level lexical prefix check is insufficient.
    for path in list(required):
        if pathlib.Path(path).is_dir():
            required.extend(str(p) for p in pathlib.Path(path).rglob('*') if p.is_symlink())
    optional = [spec['old_state'], spec['old_guest_known_hosts'], spec['backup_root'], spec['snapshot_root'],
                spec['capture_root'], spec['evidence_root'], spec['generation_root'], spec.get('private_dir', ''),
                spec.get('bootstrap_reference', ''), str(pathlib.Path(spec['provision_spec']['image']['path']).parent),
                '/root/ovs-to-ovn-backup', '/root/kolla-reset-snapshots', '/var/lib/ovn-migration-validation',
                '/var/lib/ovs-to-ovn-ew-provisioning', '/var/lib/ovs-to-ovn-reset', '/etc/kolla/certificates']
    old = pathlib.Path(spec['old_state'])/'resources.json'
    if old.exists() and json.loads(old.read_text()).get('guests'):
        require(spec['old_guest_known_hosts'] and pathlib.Path(spec['old_guest_known_hosts']).is_file(),
                'Prior guest identities require retained guest trust history before reset')
    return sorted(set(required)), sorted(set(p for p in optional if p))


def validate_local(spec):
    require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', spec['generation']) is not None, 'Explicit safe reset_generation required')
    require(len(spec['hosts']) == 5 and len(set(spec['hosts'])) == 5 and len(spec['control']) == 1 and
            len(spec['network']) == len(spec['compute']) == 2 and set(spec['control']+spec['network']+spec['compute']) == set(spec['hosts']),
            'Work Item 2 requires exactly one controller, two network and two compute hosts')
    require(all(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', h) for h in spec['hosts']), 'Unsafe reset inventory host identity')
    require({v['compute_host'] for v in spec['topology']['servers']} <= set(spec['compute']), 'EW placement missing from reset scope')
    require(not spec['delete_backups'], 'Migration evidence is protected; reset_delete_migration_backups=true is refused')
    for key in ('inventory','kolla_venv','globals_path','passwords','generation_root','config_path','old_state',
                'backup_root','evidence_root','snapshot_root','capture_root','openrc'):
        require(pathlib.Path(spec[key]).is_absolute(), 'Reset requires an absolute resolved path: '+key)
    require(pathlib.Path(spec['inventory']).resolve() == pathlib.Path(spec['invocation_inventory']).resolve(), 'Reset and provisioning inventory must be identical')
    for key in ('inventory', 'kolla_venv', 'globals_path', 'passwords', 'generation_root', 'config_path'):
        require(re.fullmatch(r'[A-Za-z0-9_./-]+', spec[key]), 'Installed Kolla shell CLI requires a path without whitespace/metacharacters: '+key)
    require(pathlib.Path(spec['globals_path']).name == 'globals.yml' and pathlib.Path(spec['globals_path']).parent == pathlib.Path(spec['passwords']).parent,
            'Kolla config directory must contain globals.yml and the retained passwords.yml')
    require(not list((pathlib.Path(spec['globals_path']).parent/'globals.d').glob('*.yml')) and
            not list((pathlib.Path(spec['globals_path']).parent/'globals.d').glob('*.yaml')),
            'Consolidate/review globals.d overlays before reset; do not leave target OVN overrides active')
    require(not any(os.environ.get(k) for k in ('EXTRA_OPTS', 'ANSIBLE_SERIAL')), 'Unset Kolla EXTRA_OPTS/ANSIBLE_SERIAL; reset inputs must be explicit')
    required, optional = retained_files(spec)
    root = pathlib.Path(spec['generation_root']).resolve()
    require(not root.is_relative_to(REPO) and not root.is_relative_to(pathlib.Path(spec['backup_root']).resolve()),
            'Reset journal must be outside repository and migration run output')
    require(not pathlib.Path(spec['old_state']).resolve().is_relative_to(root/spec['generation']),
            'Old provisioning state cannot be this generation output; preserve original reset inputs on continuation')
    for path in required:
        require(pathlib.Path(path).is_absolute() and pathlib.Path(path).exists(), 'Missing retained artifact: '+str(path))
    source, settings = source_configuration(spec)
    for path in required+optional:
        resolved = pathlib.Path(path).resolve()
        require(not resolved.is_relative_to(pathlib.Path(spec['config_path']).resolve()) and
                not pathlib.Path(spec['config_path']).resolve().is_relative_to(resolved), 'Retained path overlaps custom-config deletion: '+path)
    require(not pathlib.Path(spec['source_config']).resolve().is_relative_to(pathlib.Path(spec['config_path']).resolve()), 'Retained source config cannot be the live custom-config tree')
    for path in (spec['source_globals'], spec['source_config'], spec['inventory'], spec['passwords'], spec['kolla_venv']):
        require(not pathlib.Path(path).resolve().is_relative_to(root), 'Required input must be outside generation output: '+path)
    for path in [spec['access_file'], spec['credentials_file'], *spec['provision_spec']['secrets'].values(), spec['guest_key'], *filter(None, spec['host_keys'])]:
        require(pathlib.Path(path).stat().st_mode & 0o077 == 0 and pathlib.Path(path).stat().st_uid == os.geteuid(),
                'Private reset input/key must have controller-only ownership/permissions: '+path)
    validated_spec(copy.deepcopy(spec['provision_spec']), spec['topology'], spec['backup_root'])
    require(importlib.util.find_spec('openstack') is not None, 'OpenStackSDK must be installed in the existing Kolla environment before reset')
    for tool in ('ssh', 'ssh-keygen', 'openstack', 'ansible-playbook', 'ansible-inventory'):
        require(shutil.which(tool, path=str(pathlib.Path(spec['kolla_venv'])/'bin')+os.pathsep+os.environ.get('PATH','')),
                'Missing controller prerequisite before reset: '+tool)
    public = subprocess.check_output(['ssh-keygen', '-y', '-f', spec['guest_key']], text=True, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    require(public.split()[:2] == pathlib.Path(spec['provision_spec']['keypair_public_key']).read_text().split()[:2],
            'Retained guest private/public keypair mismatch before reset')
    kolla = pathlib.Path(spec['kolla_venv'])/'share/kolla-ansible'
    version = subprocess.check_output([str(pathlib.Path(spec['kolla_venv'])/'bin/kolla-ansible'), '--version'], text=True, stderr=subprocess.PIPE)
    require(re.search(r'18\.8(?:\.|\s|$)', version), 'Reset supports the reviewed Kolla-Ansible 18.8.x only')
    for name, expected in DESTROY_PINS.items():
        require((kolla/name).is_file() and digest(kolla/name) == expected, 'Unreviewed installed Kolla destroy source: '+name)
    header = template_header(pathlib.Path(spec['forward'].get('mtu_kolla_ml2_template_path') or
                             kolla/'ansible/roles/neutron/templates/ml2_conf.ini.j2').read_text())
    return source, settings, header, required, optional


def acceptance(before, after, ready, result):
    for key in ('resources', 'guests', 'preserved_tasks'):
        require(before.get(key) == after.get(key) and bool(after.get(key)), 'New-generation '+key+' changed across apply/verify/rerun')
    require(len([k for k in after['resources'] if k.startswith('server:')]) == 6 and len(after['guests']) == 6 and
            len(after['preserved_tasks']) == 3 and not after.get('application_deployment_pending'), 'Incomplete six-VM identity/task acceptance')
    require(result.get('status') == 'PASS' and result.get('action') == 'apply' and result.get('changed') is False, 'Second successful apply must report changed=false')
    app = ready.get('application_deployment', {})
    require(ready.get('status') == 'PASS' and len(ready.get('tasks', {})) == 3 and ready.get('preserved_tasks', {}).get('status') == 'PASS' and
            app.get('changed_files') == [] and app.get('service_actions') == [] and app.get('environment_changed') is False,
            'Second apply readiness/configuration/service no-op evidence incomplete')
    return dict(status='PASS', six_guests=True, application_tasks=True, identities_and_receipts_preserved=True, second_apply_changed=False)


class Workflow:
    def __init__(self, spec):
        self.spec = spec
        self.root = pathlib.Path(spec['generation_root'])/spec['generation']
        self.path = self.root/'journal.json'
        self.journal = json.loads(self.path.read_text()) if self.path.exists() else None
        self.ansible = str(pathlib.Path(spec['kolla_venv'])/'bin/ansible-playbook')

    def run(self, argv, log):
        with open(self.root/(log+'.log'), 'a', opener=lambda p,f: os.open(p, f, 0o600)) as stream:
            env = dict(os.environ, PATH=str(pathlib.Path(self.spec['kolla_venv'])/'bin')+os.pathsep+os.environ.get('PATH', ''))
            result = subprocess.run(argv, cwd=REPO, stdout=stream, stderr=subprocess.STDOUT, env=env)
        require(result.returncode == 0, log+' failed; inspect private generation log; no destructive replay is authorized')

    def play(self, playbook, stage, variables):
        trust = self.root/'management-trust-vars.json'
        require(trust.is_file(), 'Missing reviewed management trust; child Ansible invocation refused')
        path = self.root/(stage+'-vars.json'); save(path, variables)
        # Last extra-vars wins, including over play/inventory/ambient settings.
        # This applies to imported readiness plays and all three EW invocations.
        self.run([self.ansible, '-i', self.spec['inventory'], str(REPO/playbook),
                  '-e', '@'+str(path), '-e', '@'+str(trust)], stage)

    def preflight(self):
        source, settings, header, required, optional = validate_local(self.spec)
        # Bind effective inputs (including private input contents) privately.
        # Do not log these digests or the inventory's private variable values.
        files = set(p for p in required if pathlib.Path(p).is_file())
        files.update(str(p) for base in (pathlib.Path(self.spec['source_config']), REPO/'scripts', REPO/'playbooks', REPO/'workloads')
                     for p in base.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc')
        files.update(str(p) for p in REPO.glob('*.yml'))
        files.add(str(REPO/'group_vars/all.yml'))
        installed = pathlib.Path(self.spec['kolla_venv'])/'share/kolla-ansible'
        files.add(str(installed/'ansible/group_vars/all.yml'))
        files.update(str(installed/name) for name in DESTROY_PINS)
        files.add(str(installed/'ansible/roles/neutron/templates/ml2_conf.ini.j2'))
        binding = dict(spec=self.spec, files={p:digest(p) for p in sorted(files)},
            locations={p:dict(resolved=str(pathlib.Path(p).resolve()), uid=pathlib.Path(p).stat().st_uid,
                gid=pathlib.Path(p).stat().st_gid, mode=pathlib.Path(p).stat().st_mode & 0o7777) for p in required})
        signature = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
        if self.journal:
            require(self.journal['schema_version'] == 1 and self.journal['input_signature'] == signature,
                    'Generation/input conflict; never rebind an existing reset generation')
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        require(not self.root.is_symlink() and self.root.stat().st_uid == os.geteuid() and self.root.stat().st_mode & 0o077 == 0,
                'Generation directory must be private and controller-owned')
        intent = self.root/'intent.json'
        if intent.exists():
            require(json.loads(intent.read_text())['input_signature'] == signature, 'Generation/input conflict in preflight intent')
        else:
            require(not any(self.root.iterdir()), 'Unrecognized existing generation directory; preserve it and select another ID')
            save(intent, dict(schema_version=1, generation=self.spec['generation'], input_signature=signature))
        # Inventory values must be resolved using the installed Kolla defaults
        # and the SAME globals/password inputs passed by its CLI.
        from ansible.parsing.dataloader import DataLoader
        from ansible.template import Templar
        argv = [str(pathlib.Path(self.ansible).with_name('ansible-inventory')), '-i', self.spec['inventory'],
                '--playbook-dir', str(pathlib.Path(self.spec['kolla_venv'])/'share/kolla-ansible/ansible'), '--list',
                '-e', '@'+self.spec['globals_path'], '-e', '@'+self.spec['passwords']]
        try:
            inventory = json.loads(subprocess.check_output(argv, text=True, stderr=subprocess.PIPE))
        except subprocess.CalledProcessError as exc:
            save(self.root/'inventory-resolution-error.json', dict(returncode=exc.returncode, stderr=exc.stderr))
            raise RuntimeError('Kolla inventory resolution failed; inspect private inventory-resolution-error.json') from None
        configs = {}
        for host in self.spec['hosts']:
            variables = inventory['_meta']['hostvars'][host]
            template = Templar(loader=DataLoader(), variables=variables)
            require(template.template(variables.get('node_config', '/etc/kolla')) == '/etc/kolla', 'Existing migration/source inspection requires node_config=/etc/kolla')
            for name in ('ansible_ssh_private_key_file', 'ansible_private_key_file'):
                if variables.get(name):
                    key = str(pathlib.Path(template.template(variables[name], fail_on_undefined=True)).expanduser().resolve())
                    require(pathlib.Path(key).is_file(), 'Missing management inventory key: '+key)
                    configs.setdefault('_keys', []).append(key)
            require(template.template(variables.get('kolla_container_engine', 'docker')) == 'docker', 'Only Docker reset is reviewed')
            cleanup = {key:template.template(variables[key], fail_on_undefined=True) for key in DATA_VOLUMES}
            for key in ('enable_swift', 'enable_octavia', 'destroy_include_dev', 'destroy_include_images'):
                require(not truth(template.template(variables.get(key, 'no'))), 'Unsupported destructive Kolla option: '+key)
            require(all(isinstance(v, str) and re.fullmatch(r'[A-Za-z0-9_./-]+', v) for v in cleanup.values()),
                    'Unresolved/unsafe Kolla shell deletion path; whitespace/metacharacters are forbidden')
            configs[host] = dict(cleanup=cleanup, inventory=self.spec['inventory'],
                compute=host in self.spec['compute'], allow_orphan_volumes=bool(self.journal and self.journal['stages'].get('destroy', {}).get('status') == 'COMPLETE'),
                required=required if host in self.spec['control'] else [],
                optional=optional+[str(pathlib.Path.home()/'.ssh')]+self.spec['host_protected'].get(host, []),
                delete_paths=[self.spec['config_path'], self.spec['globals_path'], '/var/log/kolla', '/etc/fstab_backup'],
                remove_vips=[template.template(variables[k]) for k in ('kolla_internal_vip_address', 'kolla_external_vip_address')]
                    if truth(template.template(variables.get('enable_haproxy', 'no'))) else [],
                chassis=host in self.spec['network']+self.spec['compute'], tunnel_interface=source['tunnel_interface'],
                interfaces=list(set([source['network_interface'], source['api_interface'], source['tunnel_interface']]+(
                    [source['neutron_external_interface']] if host in self.spec['network'] else []))), underlay_mtu=self.spec['underlay_mtu'])
            # Explicit additional host retention entries are required, not best effort.
            configs[host]['required'] += self.spec['host_protected'].get(host, [])
        inventory_keys = configs.pop('_keys', [])
        configs[self.spec['control'][0]]['required'] += inventory_keys
        key_bindings = {}
        for key in inventory_keys:
            stat = pathlib.Path(key).stat()
            require(stat.st_uid == os.geteuid() and stat.st_mode & 0o077 == 0, 'Inventory management key must be controller-private: '+key)
            key_bindings[key] = dict(sha256=digest(key), uid=stat.st_uid, mode=stat.st_mode & 0o777)
        require(not self.root.is_symlink(), 'Reset generation directory must not be a symlink')
        management = management_trust(self.spec, inventory)
        trust_path = self.root/'management-trust-vars.json'
        if trust_path.exists():
            require(json.loads(trust_path.read_text()) == management, 'Reviewed management connection options changed within the reset generation')
        save(trust_path, management)
        (self.root/'hosts').mkdir(mode=0o700, exist_ok=True)
        self.play('playbooks/reset-hosts.yml', 'preflight', dict(reset_probe=configs, reset_output=str(self.root/'hosts'),
                  reset_probe_identity_only=False, reset_management_known_hosts=self.spec['host_known_hosts']))
        hosts = {host:json.loads((self.root/'hosts'/host).read_text()) for host in self.spec['hosts']}
        require(all(h['status'] == 'PASS' for h in hosts.values()), 'Incomplete all-host preflight')
        require(len({h['identity']['product_uuid'] for h in hosts.values()}) == 5, 'Five distinct physical VM identities required')
        require(hosts[self.spec['control'][0]]['identity'] == identity(), 'Run reset on its intended deployment controller')
        calculation = calculate(dict(source_configs={h:settings for h in self.spec['control']},
            underlay={h:hosts[h]['underlay'] for h in self.spec['network']+self.spec['compute']}, geneve_max_header_size=header))
        require(calculation['effective_path_limit'] == self.spec['underlay_mtu'],
                'Retained source MTU must resolve to the reviewed underlay limit; refusing a reduced tenant-MTU source baseline')
        if self.journal:
            require(self.journal['host_identities'] == {h:r['identity'] for h,r in hosts.items()}, 'Reset host identity changed')
            require(self.journal['management_keys'] == key_bindings, 'Inventory management key binding changed')
            if self.journal['stages'].get('destroy', {}).get('status') != 'COMPLETE':
                require(self.journal['destroy_globals_digest'] == digest(self.spec['globals_path']) and
                        self.journal['cleanup'] == {h:c['cleanup'] for h,c in configs.items()}, 'Effective destroy configuration changed since generation preflight')
        else:
            self.journal = dict(schema_version=1, generation=self.spec['generation'], input_signature=signature,
                host_identities={h:r['identity'] for h,r in hosts.items()}, host_boots={h:r['boot'] for h,r in hosts.items()},
                management_keys=key_bindings, cleanup={h:c['cleanup'] for h,c in configs.items()},
                destroy_globals_digest=digest(self.spec['globals_path']), stages={}, created_at=time.time())
            save(self.path, self.journal)
        save(self.root/'destroy-vars.json', dict(**management, destroy_include_images=False, destroy_include_dev=False, kolla_container_engine='docker',
             reset_bound_cleanup=self.journal['cleanup'], **{k:"{{ reset_bound_cleanup[inventory_hostname]['"+k+"'] }}" for k in DATA_VOLUMES}))
        save(self.root/'retained-inputs.json', binding)
        save(self.root/'source-mtu-plan.json', calculation)
        self.hosts = hosts
        forward = copy.deepcopy(self.spec['forward'])
        forward.update(ew_provision_state_dir=str(self.root/'provisioning'), ew_guest_known_hosts=str(self.root/'guest-known-hosts'),
                       kolla_globals_path=self.spec['globals_path'], openrc_path=self.spec['openrc'],
                       migration_backup_root=self.spec['backup_root'], ew_provision_spec=self.spec['provision_spec'],
                       ew_provision_spec_file='')
        save(self.root/'generation-vars.yml', forward)
        return dict(status='PASS', generation=self.spec['generation'], path=str(self.root), source_mtu=calculation['validation_source_mtu'], target_mtu=calculation['validation_target_mtu'])

    def stage(self, name, action, replayable=True, refresh=False):
        prior = self.journal['stages'].get(name, {})
        if prior.get('status') == 'COMPLETE' and not refresh: return
        require(not prior or replayable, 'Ambiguous destructive boundary '+name+'; inspect logs/current state; automatic replay refused')
        self.journal['stages'][name] = dict(status='STARTED', attempt=prior.get('attempt', 0)+1, started_at=time.time())
        save(self.path, self.journal)
        try: action()
        except Exception:
            self.journal['stages'][name]['status'] = 'INTERRUPTED'; save(self.path, self.journal)
            raise
        self.journal['stages'][name].update(status='COMPLETE', completed_at=time.time())
        save(self.path, self.journal)

    def archive(self):
        archive = self.root/'historical'; archive.mkdir(mode=0o700, exist_ok=True)
        for name, value in (('provisioning', self.spec['old_state']), ('guest-known-hosts', self.spec['old_guest_known_hosts'])):
            path = pathlib.Path(value) if value else None
            if path and path.exists():
                # tar keeps original UUID/receipt/pending/trust bytes, modes and ownership.
                target = archive/(name+'.tar')
                if target.exists(): continue
                tmp = target.with_suffix('.tmp')
                subprocess.run(['tar', '--acls', '--xattrs', '-cpf', str(tmp), '-C', str(path.parent), path.name], check=True)
                tmp.chmod(0o600)
                with tmp.open('rb') as stream: os.fsync(stream.fileno())
                os.replace(tmp, target)
        for name, path in (('globals.before', self.spec['globals_path']), ('config', self.spec['config_path']), ('openrc.before', self.spec['openrc'])):
            target = archive/(name+'.tar')
            if not target.exists() and pathlib.Path(path).exists():
                tmp = target.with_suffix('.tmp')
                subprocess.run(['tar', '--acls', '--xattrs', '-cpf', str(tmp), '-C', str(pathlib.Path(path).parent), pathlib.Path(path).name], check=True)
                tmp.chmod(0o600)
                with tmp.open('rb') as stream: os.fsync(stream.fileno())
                os.replace(tmp, target)
        fd = os.open(archive, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)

    def operation(self, stage):
        self.play('playbooks/reset-ovs-stages.yml', stage, dict(reset_stage=stage, reset_bound_spec=self.spec,
                  reset_generation_dir=str(self.root), reset_bound_identities=self.journal['host_identities'],
                  reset_bound_boots=self.journal['host_boots'], reset_management_known_hosts=self.spec['host_known_hosts'],
                  kolla_globals_path=self.spec['globals_path'], openrc_path=self.spec['openrc'],
                  migration_backup_root=self.spec['backup_root'], ew_workloads_enabled=False,
                  mtu_kolla_ml2_template_path=self.spec['forward'].get('mtu_kolla_ml2_template_path', '')))

    def reconcile(self):
        """Read-only completion proof; never retries destroy/guest stop/reboot."""
        self.preflight()
        pending = [n for n,r in self.journal['stages'].items() if r['status'] != 'COMPLETE' and
                   (n in ('stop-guests', 'destroy') or n.startswith('reboot-'))]
        require(len(pending) == 1, 'Exactly one interrupted destructive boundary is required for reconciliation')
        name = pending[0]
        output = self.root/'boundary'; output.mkdir(mode=0o700, exist_ok=True)
        self.play('playbooks/reset-hosts.yml', 'boundary', dict(reset_probe={h:{'action':'boundary','inventory':self.spec['inventory']} for h in self.spec['hosts']},
                  reset_output=str(output), reset_probe_identity_only=False, reset_management_known_hosts=self.spec['host_known_hosts']))
        rows = {h:json.loads((output/h).read_text()) for h in self.spec['hosts']}
        require(all(rows[h]['identity'] == self.journal['host_identities'][h] for h in rows), 'Host identity changed at interrupted boundary')
        if name == 'stop-guests':
            require(all(not r['running_guests'] and not r['qemu'] for r in rows.values()), 'Guest-stop boundary is ambiguous; no replay authorized')
        elif name == 'destroy':
            require(all(not any(r[k] for k in ('containers', 'volumes', 'namespaces', 'qemu', 'generated_config')) for r in rows.values()),
                    'Destroy was partial/ambiguous; no replay authorized; separate operator investigation required')
        else:
            host = name.removeprefix('reboot-')
            require(rows[host]['boot'] != self.journal['host_boots'][host], 'Reboot completion is ambiguous; no replay authorized')
            source, _ = source_configuration(self.spec)
            required = {source['network_interface'], source['tunnel_interface']}
            if host in self.spec['network']: required.add(source['neutron_external_interface'])
            links = set(rows[host]['links'])
            require(required <= links and not links & {'br-int','br-ex','br-tun','genev_sys_6081'} and
                    not any(rows[host][k] for k in ('containers','volumes','namespaces','qemu','generated_config')),
                    'Post-reboot clean-network/service checks incomplete; no boundary completion authorized')
        self.journal['stages'][name].update(status='COMPLETE', completed_at=time.time(), reconciled_read_only=True)
        save(self.path, self.journal)
        return dict(status='PASS', reconciled=name, next_action='continue')

    def provision(self, action, label):
        variables = json.loads((self.root/'generation-vars.yml').read_text())
        variables.update(ew_provision_action=action, ew_workloads_enabled=False, ew_provision_result_file=str(self.root/(label+'-result.json')))
        self.play('ew-provision.yml', label, variables)
        state = json.loads((self.root/'provisioning/resources.json').read_text())
        ready = json.loads((self.root/'provisioning/readiness.json').read_text())
        require(ready.get('status') == 'PASS' and len(ready.get('tasks', {})) == 3, label+': real application readiness required')
        if label == 'apply-first':
            old = pathlib.Path(self.spec['old_state'])/'resources.json'
            if old.exists():
                previous = json.loads(old.read_text()).get('resources', {})
                # Nova keypair IDs are names, not UUIDs; ew-key intentionally
                # retains that name while every UUID-backed resource is new.
                previous_ids = {v for k,v in previous.items() if not k.startswith('keypair:')}
                current_ids = {v for k,v in state['resources'].items() if not k.startswith('keypair:')}
                require(not current_ids & previous_ids, 'Rebuilt cloud unexpectedly reused old resource UUIDs')
            save(self.root/'first-state.json', state)
        else:
            first = json.loads((self.root/'first-state.json').read_text())
            for key in ('resources', 'guests', 'preserved_tasks'):
                require(first.get(key) == state.get(key), label+': new-generation identity/receipt changed')
            if label == 'apply-second':
                acceptance(first, state, ready, json.loads((self.root/'apply-second-result.json').read_text()))

    def accept(self):
        require(all(self.journal['stages'].get(s, {}).get('status') == 'COMPLETE' for s in
                    ('archive', 'stop-guests', 'destroy', 'residue', 'source-config', 'bootstrap-servers', 'prechecks',
                     'deploy', 'post-deploy', 'source-ready', 'apply-first', 'verify', 'apply-second',
                     *('reboot-'+h for h in self.spec['network']+self.spec['compute']))), 'Reset acceptance stages incomplete')
        self.journal['stages']['acceptance'] = dict(status='STARTED', started_at=time.time()); save(self.path, self.journal)
        try:
            result = acceptance(json.loads((self.root/'first-state.json').read_text()),
                json.loads((self.root/'provisioning/resources.json').read_text()),
                json.loads((self.root/'provisioning/readiness.json').read_text()),
                json.loads((self.root/'apply-second-result.json').read_text()))
        except Exception:
            self.journal['stages']['acceptance']['status']='INTERRUPTED'; save(self.path, self.journal)
            raise
        save(self.root/'acceptance.json', result)
        self.journal['stages']['acceptance'].update(status='COMPLETE', completed_at=time.time()); save(self.path, self.journal)
        return result

    def execute(self, action, confirmed):
        self.preflight()  # all five hosts and retained inputs BEFORE any stage
        stages = self.journal['stages']
        require(action != 'apply' or not stages, 'Existing generation: use continue; never begin another destructive reset')
        require(action != 'continue' or bool(stages), 'No entered generation to continue; use explicit apply/confirmation')
        require(bool(stages) or confirmed, 'New destructive reset requires reset_lab_confirm=true')
        if not stages:
            self.journal['destructive_confirmation'] = dict(input_signature=self.journal['input_signature'], confirmed_at=time.time())
            save(self.path, self.journal)
        require(self.journal.get('destructive_confirmation', {}).get('input_signature') == self.journal['input_signature'],
                'Missing generation-bound destructive confirmation; continuation refused')
        save(pathlib.Path(self.spec['generation_root'])/'active.json', dict(generation=self.spec['generation']))
        self.stage('archive', self.archive)
        self.stage('stop-guests', lambda:self.operation('stop-guests'), replayable=False)
        self.stage('destroy', lambda:self.operation('destroy'), replayable=False)
        self.stage('residue', lambda:self.operation('residue'))
        for host in self.spec['network']+self.spec['compute']:
            self.stage('reboot-'+host, lambda host=host:self.operation('reboot-'+host), replayable=False)
        self.stage('source-config', lambda:self.operation('source-config'))
        for stage in ('bootstrap-servers', 'prechecks', 'deploy', 'post-deploy'):
            self.stage(stage, lambda stage=stage:self.operation(stage))
        # Readiness is intentionally refreshed on every incomplete continuation,
        # including when a previous provisioning attempt left BUILD/pending state.
        self.stage('source-ready', lambda:self.operation('source-ready'), refresh=True)
        self.stage('apply-first', lambda:self.provision('apply', 'apply-first'))
        self.stage('verify', lambda:self.provision('verify', 'verify'))
        self.stage('apply-second', lambda:self.provision('apply', 'apply-second'))
        return self.accept()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('preflight', 'apply', 'continue', 'reconcile', 'accept'))
    parser.add_argument('spec', type=pathlib.Path)
    parser.add_argument('--confirm', action='store_true')
    args = parser.parse_args()
    spec = json.loads(args.spec.read_text())
    # Validate local inputs before creating a generation directory/lock.
    validate_local(spec)
    base = pathlib.Path(spec['generation_root']); base.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(not base.is_symlink() and base.stat().st_uid == os.geteuid() and base.stat().st_mode & 0o077 == 0,
            'Reset generation root must be owned by the controller user and private')
    with locked(base/'.lock'):
        active_path = base/'active.json'
        active = json.loads(active_path.read_text()) if active_path.exists() else None
        if active and active['generation'] != spec['generation']:
            previous = base/active['generation']/'acceptance.json'
            previous_journal = base/active['generation']/'journal.json'
            require(previous.exists() and previous_journal.exists() and json.loads(previous.read_text()).get('status') == 'PASS' and
                    all(r['status']=='COMPLETE' for r in json.loads(previous_journal.read_text())['stages'].values()),
                    'Another reset generation is incomplete; continue it instead')
        workflow = Workflow(spec)
        state = workflow.root/'provisioning' if args.action == 'accept' else pathlib.Path(spec['old_state'])
        with locked(state/'.lock') if state.is_dir() else contextlib.nullcontext():
            if args.action == 'accept':
                require(workflow.journal is not None, 'No generation journal to accept')
                result = workflow.accept()
            elif args.action == 'preflight': result = workflow.preflight()
            elif args.action == 'reconcile': result = workflow.reconcile()
            else:
                result = workflow.execute(args.action, args.confirm)
        print(json.dumps(dict(result, generation=spec['generation'], evidence=str(workflow.root), variables=str(workflow.root/'generation-vars.yml'))))


if __name__ == '__main__':
    try: main()
    except Exception as exc:
        # Subprocess bodies and inventory can contain secrets. Private logs only.
        print('Reset workflow refused: '+(str(exc) if type(exc) is RuntimeError else type(exc).__name__), file=sys.stderr)
        sys.exit(1)
