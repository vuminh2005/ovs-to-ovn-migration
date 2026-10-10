#!/usr/bin/env python3
"""Read-only Work Item 2 host/storage evidence. No API or service operations."""
import base64
import json
import os
import pathlib
import platform
import re
import shutil
import socket
import subprocess
import sys
import uuid


def require(ok, reason):
    if not ok:
        raise RuntimeError(reason)


def command(argv):
    return subprocess.check_output(argv, text=True, stderr=subprocess.PIPE, timeout=60)


def identity():
    try:
        raw = pathlib.Path('/sys/class/dmi/id/product_uuid').read_text().strip()
        value = uuid.UUID(raw)
    except (OSError, ValueError):
        raise RuntimeError('Missing/malformed DMI product UUID; host scope cannot be verified') from None
    require(value.int not in (0, (1 << 128)-1), 'Invalid host product UUID')
    return dict(product_uuid=str(value), machine_id=pathlib.Path('/etc/machine-id').read_text().strip(), hostname=socket.gethostname())


def mount_table(text):
    def unescape(value):
        return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), value)
    return [(fields[2], unescape(fields[3]), unescape(fields[4]))
            for line in text.splitlines() if (fields := line.split())]


def path_evidence(value, mounts, required=True):
    path = pathlib.Path(value)
    require(path.is_absolute(), 'Retained/deleted paths must be absolute: '+str(path))
    resolved = path.resolve()
    require(not required or resolved.exists(), 'Missing retained artifact: '+str(path))
    matches = [m for m in mounts if resolved.is_relative_to(m[2])]
    require(bool(matches), 'Cannot resolve filesystem identity: '+str(path))
    device, root, point = max(matches, key=lambda m: len(pathlib.Path(m[2]).parts))
    physical = str(pathlib.PurePosixPath(root)/resolved.relative_to(point))
    result = dict(path=str(path), lexical=os.path.normpath(path), resolved=str(resolved), device=device, physical=physical,
                  exists=resolved.exists())
    if resolved.exists():
        stat = resolved.stat()
        result.update(uid=stat.st_uid, gid=stat.st_gid, mode=stat.st_mode & 0o7777)
    return result


def overlaps(left, right):
    def related(a, b):
        a, b = pathlib.PurePosixPath(a), pathlib.PurePosixPath(b)
        return a.is_relative_to(b) or b.is_relative_to(a)
    return (related(left.get('lexical', left['path']), right.get('lexical', right['path'])) or
            related(left['resolved'], right['resolved']) or
            left['device'] == right['device'] and related(left['physical'], right['physical']))


def check_retention(protected, deletions):
    for item in protected:
        for removed in deletions:
            require(not overlaps(item, removed), 'Retained path overlaps reset scope: '+item['path']+' / '+removed['path'])


def discover(cfg):
    require(os.geteuid() == 0, 'Host preflight requires read-only root inspection')
    release = platform.freedesktop_os_release()
    require(release.get('ID') == 'ubuntu' and release.get('VERSION_ID') == '24.04', 'Reset supports Ubuntu 24.04 hosts only')
    for tool in ('docker', 'ip', 'python3', 'systemctl', 'tar')+ (('nc',) if cfg['chassis'] else ()):
        require(shutil.which(tool), 'Missing reset prerequisite tool: '+tool)
    require(not pathlib.Path('/etc/kolla/ovsdpdk-db/ovs-dpdkctl.sh').exists(), 'OVS-DPDK cleanup is outside this lab scope')
    namespaces = command(['ip', 'netns', 'list']).splitlines()
    require(all(re.fullmatch(r'(qrouter|qdhcp|ovnmeta)-[0-9a-f-]{36}', n.split()[0]) for n in namespaces),
            'Unrelated/unknown host namespace would be deleted by Kolla cleanup-host')
    mounts = mount_table(pathlib.Path('/proc/self/mountinfo').read_text())
    ids = command(['docker', 'ps', '-aq']).split()
    rows = json.loads(command(['docker', 'inspect', *ids])) if ids else []
    require(all('kolla_version' in (r.get('Config', {}).get('Labels') or {}) for r in rows),
            'Non-Kolla container present; lab-wide reset scope ambiguous')
    names = command(['docker', 'volume', 'ls', '-q']).split()
    volumes = json.loads(command(['docker', 'volume', 'inspect', *names])) if names else []
    require(all(v['Driver'] == 'local' and not v.get('Options') for v in volumes),
            'Non-plain-local Docker volume: review its backing storage before reset')
    mounted = {m['Name'] for r in rows for m in r['Mounts'] if m['Type'] == 'volume'}
    require(cfg.get('allow_orphan_volumes', False) or set(names) <= mounted,
            'Unmounted Docker volumes would survive Kolla destroy; review before destructive reset')
    domains = []
    if cfg.get('compute'):
        libvirt = [r for r in rows if r['Name'] == '/nova_libvirt' and r.get('State', {}).get('Running')]
        qemu = qemu_processes()
        require(not qemu or len(libvirt) == 1, 'QEMU is running without an inspectable Nova libvirt; guest-stop boundary unsafe')
        if libvirt:
            domains = command(['docker', 'exec', 'nova_libvirt', 'virsh', 'list', '--all', '--uuid']).split()
            require(len(domains) == len(set(domains)) and all(str(uuid.UUID(d)) == d.lower() for d in domains), 'Ambiguous libvirt domain identities')
    deletions = list(cfg['delete_paths']) + [v['Mountpoint'] for v in volumes]
    # Kolla cleanup-host removes every /etc/kolla child except this exact list.
    keep = {'passwords.yml', 'globals.yml', 'globals.d', 'kolla-build.conf', 'config', 'certificates'}
    for child in pathlib.Path('/etc/kolla').glob('*'):
        if child.name not in keep and str(child) != cfg['inventory']:
            deletions.append(str(child))
    for key, default in (('glance_file_datadir_volume', 'glance'), ('nova_instance_datadir_volume', 'nova_compute'),
                         ('gnocchi_metric_datadir_volume', 'gnocchi'), ('influxdb_datadir_volume', 'influxdb'),
                         ('opensearch_datadir_volume', 'opensearch')):
        value = cfg['cleanup'][key]
        if value != default:
            require(pathlib.Path(value).is_absolute(), 'Ambiguous custom Kolla storage: '+key)
            deletions.append(value)
    deleted = [path_evidence(p, mounts, False) for p in sorted(set(deletions))]
    protected = [path_evidence(p, mounts) for p in cfg['required']]
    protected += [path_evidence(p, mounts) for p in cfg['optional'] if pathlib.Path(p).exists()]
    for item in list(protected):
        path = pathlib.Path(item['path'])
        if path.is_dir():
            protected.extend(path_evidence(p, mounts) for p in path.rglob('*') if p.is_symlink())
    check_retention(protected, deleted)
    links = json.loads(command(['ip', '-j', 'address']))
    require(not set(cfg.get('remove_vips', [])) & {a['local'] for i in links for a in i.get('addr_info', [])},
            'Kolla VIP cleanup would remove an address on an intended management host; review source access/scope')
    by_name = {i['ifname']: i for i in links}
    for name in cfg['interfaces']:
        require(name in by_name, 'Missing intended source interface: '+name)
    underlay = None
    if cfg['chassis']:
        link = by_name[cfg['tunnel_interface']]
        ips = [a['local'] for a in link.get('addr_info', []) if a['family'] == 'inet']
        require(ips and link['mtu'] == cfg['underlay_mtu'], 'Tunnel IPv4/MTU contradicts reviewed source inputs')
        underlay = dict(interface=link['ifname'], mtu=link['mtu'], ipv4=ips)
    return dict(status='PASS', identity=identity(), hostname=socket.gethostname(), boot=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(), protected=protected, deletions=deleted,
                underlay=underlay, cleanup=cfg['cleanup'],
                containers=[dict(name=r['Name'], mounts=r['Mounts']) for r in rows], volumes=volumes, domains=domains)


def qemu_processes():
    return [p.name for p in pathlib.Path('/proc').glob('[0-9]*') if (p/'comm').exists() and
            (name := (p/'comm').read_text().strip()).startswith('qemu') and name not in ('qemu-ga', 'qemu-img')]


def boundary(cfg):
    containers = command(['docker', 'ps', '-aq', '--filter', 'label=kolla_version']).split()
    active = command(['docker', 'ps', '-q', '--filter', 'name=^/nova_libvirt$']).strip()
    guests = command(['docker', 'exec', 'nova_libvirt', 'virsh', 'list', '--name']).split() if active else []
    qemu = qemu_processes()
    keep = {'passwords.yml', 'globals.yml', 'globals.d', 'kolla-build.conf', 'config', 'certificates'}
    return dict(identity=identity(), boot=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                links=[r['ifname'] for r in json.loads(command(['ip','-j','address']))],
                containers=containers, volumes=command(['docker','volume','ls','-q']).split(), running_guests=guests, qemu=qemu,
                namespaces=command(['ip', 'netns', 'list']).splitlines(),
                generated_config=[str(p) for p in pathlib.Path('/etc/kolla').glob('*') if p.name not in keep and str(p) != cfg['inventory']])


if __name__ == '__main__':
    try:
        cfg = json.loads(base64.b64decode(sys.argv[1], validate=True))
        result = (dict(identity=identity(), boot=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip())
                  if cfg.get('action') == 'identity' else boundary(cfg) if cfg.get('action') == 'boundary' else discover(cfg))
        print(json.dumps(result))
    except Exception as exc:
        print('Reset host preflight refused: '+(str(exc) if type(exc) is RuntimeError else type(exc).__name__), file=sys.stderr)
        sys.exit(1)
