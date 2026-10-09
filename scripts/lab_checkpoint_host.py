#!/usr/bin/env python3
"""Private, bounded node operations for the standalone same-host cold checkpoint."""
import argparse
import base64
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import stat
import subprocess
import tarfile
import time


class Refused(RuntimeError): pass


def command(argv, timeout=120):
    result=subprocess.run(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=timeout)
    if result.returncode:
        # Docker/virsh/systemd stderr can contain configuration or credentials.
        raise Refused(f'{Path(argv[0]).name} failed (exit {result.returncode}); inspect private node journal')
    return result.stdout


def save(path, value):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temp=path.with_suffix('.tmp'); temp.write_text(json.dumps(value,indent=2)); temp.chmod(0o600); temp.replace(path)


def identity():
    import platform
    os_info=platform.freedesktop_os_release()
    if os_info.get('ID')!='ubuntu' or os_info.get('VERSION_ID')!='24.04': raise Refused('Only intact Ubuntu 24.04 hosts supported')
    return dict(machine_id=Path('/etc/machine-id').read_text().strip(),hostname=socket.gethostname())


def checkpoint_path(cfg):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{5,63}',cfg['id']): raise Refused('Unsafe checkpoint ID')
    base=Path(cfg['root'])
    if not base.is_absolute() or base==Path('/') or any(p.is_symlink() for p in (base,*base.parents)): raise Refused('Unsafe checkpoint root')
    if base.exists() and base.stat().st_mode&0o077: raise Refused('Checkpoint root must have private 0700 permissions')
    path=base/cfg['id']
    if path.is_symlink(): raise Refused('Symlink checkpoint directory')
    return path


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''): h.update(block)
    return h.hexdigest()


VOLUMES={'glance','mariadb','rabbitmq','keystone_fernet_tokens','keystone_credential_tokens',
         'nova_compute','libvirtd','nova_libvirt_qemu','openvswitch_db','fluentd_data','kolla_logs'}
DURABLE_BINDS=('/etc/kolla','/var/log/kolla','/var/lib/nova','/var/lib/libvirt','/etc/libvirt',
               '/var/lib/mysql','/var/lib/rabbitmq','/var/lib/glance','/var/lib/neutron','/etc/openvswitch')
EPHEMERAL=('/run','/var/run','/dev','/proc','/sys')
HOST_INPUTS=('/etc/localtime','/etc/timezone','/etc/hosts','/etc/hostname','/etc/resolv.conf',
             '/etc/machine-id','/lib/modules','/usr/src')


def beneath(path, root): return path==root or root in path.parents


def classify(mount):
    source=mount.get('Source',''); destination=mount.get('Destination','')
    if mount['Type']=='tmpfs': return 'ephemeral'
    if mount['Type']=='volume':
        if mount.get('Name')=='neutron_metadata_socket': return 'ephemeral'
        if mount.get('Name') in VOLUMES|{'ovn_nb_db','ovn_sb_db'}: return 'durable'
        raise Refused('Unclassified Docker volume: '+mount.get('Name','unknown'))
    if mount['Type']!='bind': raise Refused('Unsupported Docker storage driver/type')
    if source==destination=='/var/log/journal' and mount.get('RW') is False:
        return 'host-input'
    p=Path(source)
    if any(beneath(p,Path(v)) for v in EPHEMERAL): return 'ephemeral'
    if any(beneath(p,Path(v)) for v in DURABLE_BINDS): return 'durable'
    if any(beneath(p,Path(v)) for v in HOST_INPUTS) and not mount.get('RW'): return 'host-input'
    raise Refused('Unclassified bind mount: '+source+' -> '+destination)


def containers():
    ids=command(['docker','ps','-aq']).split()
    rows=json.loads(command(['docker','inspect',*ids])) if ids else []
    for row in rows:
        if not row['Config'].get('Labels',{}).get('kolla_version'):
            raise Refused('Unmanaged Docker container present; inventory cannot scope all writers')
        if row['HostConfig']['NetworkMode']!='host' or row['HostConfig'].get('AutoRemove'):
            raise Refused('Only persistent Kolla host-network containers are supported')
    return rows


def sizes(paths):
    allocated=apparent=0
    for root in paths:
        for p in entries(root)[0]:
            st=p.lstat()
            if stat.S_ISREG(st.st_mode): allocated+=st.st_blocks*512; apparent+=st.st_size
    return dict(allocated_bytes=allocated,apparent_bytes=apparent)


def entries(root):
    """Explicit no-recursion tar list; sockets and PID files are never durable."""
    root=Path(root); files=[]; ephemeral=[]
    candidates=[root]
    if root.is_dir() and not root.is_symlink():
        candidates.extend(p for directory,dirs,names in os.walk(root,followlinks=False)
                          for p in [*(Path(directory)/d for d in dirs),*(Path(directory)/n for n in names)])
    for p in candidates:
        if p.lstat().st_dev!=root.lstat().st_dev: raise Refused('Nested durable mount requires a separate storage plan')
        mode=p.lstat().st_mode
        if stat.S_ISSOCK(mode) or p.name.endswith(('.pid','.sock')): ephemeral.append(str(p)); continue
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode)):
            raise Refused('Unclassified special file in durable storage: '+str(p))
        files.append(p)
    return files,ephemeral


def compact(rows):
    return [dict(name=r['Name'].lstrip('/'),id=r['Id'],image=r['Image'],running=r['State']['Running'],
                 restart=r['HostConfig']['RestartPolicy'],mounts=r['Mounts']) for r in rows]


def backing_files(chain, mounts, roots, domain):
    result=[]; previous=None
    for item in chain:
        f=Path(item['filename'])
        if not f.is_absolute():
            if previous is None: raise Refused('Ambiguous relative disk source')
            f=Path(os.path.normpath(str(previous.parent/f)))
        previous=f
        matches=[m for m in mounts if m['classification']=='durable' and beneath(f,Path(m['Destination']))]
        if not matches: raise Refused('Backing chain outside archived storage')
        m=max(matches,key=lambda m:len(m['Destination']))
        hostpath=Path(m['Source'])/f.relative_to(m['Destination'])
        if not hostpath.is_file() or not any(beneath(hostpath.resolve(),Path(p)) for p in roots): raise Refused('Missing/outside backing file')
        result.append(dict(domain=domain,guest_path=str(f),host_path=str(hostpath.resolve()),format=item['format']))
    if not result: raise Refused('Empty backing chain')
    return result


def primary_storage(rows, role, recovery=False):
    if role!='control': return
    required={'mariadb':('/var/lib/mysql','mariadb'),
              'rabbitmq':('/var/lib/rabbitmq','rabbitmq'),
              'glance_api':('/var/lib/glance','glance'),
              'keystone':('/etc/keystone/fernet-keys','keystone_fernet_tokens')}
    for name,(destination,volume) in required.items():
        matches=[r for r in rows if r['Name'].lstrip('/')==name]
        if len(matches)!=1 or (not recovery and not matches[0]['State']['Running']): raise Refused('Required primary Kolla container not running: '+name)
        if not any(m['Type']=='volume' and m.get('Name')==volume and
                   beneath(Path(destination),Path(m['Destination'])) for m in matches[0]['Mounts']):
            raise Refused('Primary service storage is not in its classified local volume: '+name)


def domain_ports(xml, domain):
    if xml.findtext('uuid')!=domain: raise Refused('Libvirt XML UUID mismatch')
    result=[]
    for interface in xml.findall('./devices/interface'):
        mac=interface.find('mac'); parameters=interface.find('./virtualport/parameters')
        if mac is None or parameters is None or not parameters.get('interfaceid'):
            raise Refused('Libvirt interface lacks exact Neutron port identity')
        result.append(dict(port=parameters.get('interfaceid'),mac=mac.get('address','').lower()))
    if not result or len({v['port'] for v in result})!=len(result): raise Refused('Missing/ambiguous libvirt interfaces')
    return result


def qemu_domains():
    """Enumerate all host QEMU writers, including ones outside running libvirt."""
    result=set()
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit(): continue
        try: argv=(proc/'cmdline').read_bytes().decode().split('\0')
        except FileNotFoundError: continue  # Process exited while enumerating.
        if not argv or not Path(argv[0]).name.startswith(('qemu-system','qemu-kvm')): continue
        if '-uuid' not in argv: raise Refused('Unidentified QEMU process; offline scope ambiguous')
        value=argv[argv.index('-uuid')+1]
        if not re.fullmatch(r'[0-9a-fA-F-]{36}',value) or value in result: raise Refused('Ambiguous QEMU UUID')
        result.add(value)
    return result


def offline_domains(mounts):
    """Read stopped libvirt definitions without starting services or containers."""
    import xml.etree.ElementTree as ET
    target=Path('/etc/libvirt/qemu')
    matches=[m for m in mounts if m['classification']=='durable' and beneath(target,Path(m['Destination']))]
    if not matches: raise Refused('Stopped libvirt persistent XML path cannot be resolved')
    mount=max(matches,key=lambda m:len(m['Destination']))
    root=Path(mount['Source'])/target.relative_to(mount['Destination'])
    if not root.is_dir() or root.resolve()!=root: raise Refused('Missing/unsafe persistent libvirt XML directory')
    if (root/'autostart').exists() and list((root/'autostart').iterdir()): raise Refused('Libvirt autostart must be disabled')
    result={}
    for path in root.glob('*.xml'):
        if path.is_symlink() or not path.is_file(): raise Refused('Unsafe libvirt domain XML')
        xml=ET.fromstring(path.read_text()); domain=xml.findtext('uuid')
        if not domain or domain in result: raise Refused('Missing/duplicate persistent libvirt UUID')
        result[domain]=xml
    return result


def discover(cfg, recovery=False):
    if not shutil.which('lsof'): raise Refused('Install lsof before cold checkpoint planning')
    rows=containers(); volumes=[]
    primary_storage(rows,cfg['role'],recovery=recovery)
    for image in sorted({r['Image'] for r in rows}): command(['docker','image','inspect',image])
    names=command(['docker','volume','ls','-q']).split()
    if names: volumes=json.loads(command(['docker','volume','inspect',*names]))
    for v in volumes:
        if v['Driver']!='local' or v.get('Options'): raise Refused('Only plain local Docker volumes supported')
        if v['Name'] not in VOLUMES|{'neutron_metadata_socket','ovn_nb_db','ovn_sb_db'}: raise Refused('Unclassified/unmounted Docker volume: '+v['Name'])
    paths={Path('/etc/kolla')}; services={}; mounts=[]; host_inputs=[]
    if cfg['role']=='control':
        paths.add(Path(cfg['inventory']).resolve(strict=True))
        if not Path('/etc/kolla/passwords.yml').is_file(): raise Refused('Missing Kolla passwords restore input')
    for r in rows:
        name=r['Name'].lstrip('/'); unit='kolla-'+name+'-container.service'
        props=dict(line.split('=',1) for line in command(['systemctl','show',unit,'-p','LoadState','-p','FragmentPath','-p','DropInPaths','-p','ActiveState','-p','UnitFileState']).splitlines() if '=' in line)
        if props['LoadState']!='loaded' or not props['FragmentPath'].startswith('/etc/systemd/system/'):
            raise Refused('Missing supported Kolla systemd unit: '+unit)
        services[unit]=props; paths.add(Path(props['FragmentPath']))
        for p in props.get('DropInPaths','').split(): paths.add(Path(p))
        for m in r['Mounts']:
            kind=classify(m); mounts.append(dict(m,classification=kind))
            if kind=='durable': paths.add(Path(m['Source']).resolve(strict=True))
            if kind=='host-input': host_inputs.append(dict(path=m['Source'],realpath=str(Path(m['Source']).resolve())))
    for v in volumes:
        if v['Name']!='neutron_metadata_socket': paths.add(Path(v['Mountpoint']).resolve(strict=True))
    paths=sorted(p for p in paths if not any(q!=p and beneath(p,q) for q in paths))
    for p in paths:
        if beneath(checkpoint_path(cfg),p) or beneath(p,checkpoint_path(cfg)): raise Refused('Checkpoint storage overlaps durable source')
        if p.is_symlink() or not p.exists(): raise Refused('Missing/symlink durable root')
        if str(p)!=str(p.resolve()): raise Refused('Symlink ancestor in durable root')
    if cfg['role']=='control' and not {'glance','mariadb','rabbitmq','keystone_fernet_tokens'}<=set(names): raise Refused('Missing controller durable volumes/Keystone keys')
    if cfg['role']=='compute' and not {'nova_compute','libvirtd','nova_libvirt_qemu'}<=set(names): raise Refused('Missing complete Nova/libvirt volumes')
    if cfg['role'] in ('compute','network') and 'openvswitch_db' not in names: raise Refused('Missing OVSDB volume')
    nearest=checkpoint_path(cfg).parent
    while not nearest.exists(): nearest=nearest.parent
    if any(p.stat().st_dev!=nearest.stat().st_dev for p in paths): raise Refused('Separate storage filesystems need a dedicated space plan; unsupported')
    domains=[]; backing=[]; domain_states={}; domain_interfaces={}
    libvirt=next((r for r in rows if r['Name']=='/nova_libvirt'),None)
    if libvirt:
        import xml.etree.ElementTree as ET
        if libvirt['State']['Running']:
            domains=command(['docker','exec','nova_libvirt','virsh','list','--all','--uuid']).split()
            xmls={d:ET.fromstring(command(['docker','exec','nova_libvirt','virsh','dumpxml',d])) for d in domains}
            for domain in domains:
                domain_states[domain]=command(['docker','exec','nova_libvirt','virsh','domstate',domain]).strip()
                info=command(['docker','exec','nova_libvirt','virsh','dominfo',domain])
                if re.search(r'Autostart:\s+enable',info): raise Refused('Libvirt autostart must be disabled before maintenance planning')
        elif recovery:
            xmls=offline_domains(mounts)
            domains=list(xmls); domain_states={d:'shut off' for d in domains}
        else: raise Refused('Source libvirt must be running during checkpoint creation')
        for domain,xml in xmls.items():
            domain_interfaces[domain]=domain_ports(xml,domain)
            for disk in xml.findall('./devices/disk/source'):
                filename=disk.get('file')
                if not filename: raise Refused('Non-file libvirt storage is unsupported')
                if libvirt['State']['Running']:
                    chain=json.loads(command(['docker','exec','nova_libvirt','qemu-img','info','--force-share','--backing-chain','--output=json',filename]))
                else:
                    # Current data is quarantined, not archived. The SEALED backing
                    # chain is verified separately; never start a helper/daemon here.
                    chain=[dict(filename=filename,format='CURRENT_NOT_ARCHIVED')]
                backing.extend(backing_files(chain,mounts,paths,domain))
    if recovery:
        running={d for d,state in domain_states.items() if state=='running'}
        if qemu_domains()!=running: raise Refused('QEMU process/libvirt scope ambiguous; no service or data mutation permitted')

    return dict(identity=identity(),boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),role=cfg['role'],containers=compact(rows),volumes=volumes,services=services,
                roots=[str(p) for p in paths],mounts=mounts,host_inputs=host_inputs,domains=domains,domain_states=domain_states,domain_interfaces=domain_interfaces,backing=backing,
                sizes=sizes(paths),free_bytes=shutil.disk_usage(nearest).free)


def space(plan, free, reserve, central=0):
    # Uncompressed sparse archive + full apparent verification/staging + central copy.
    required=2*plan['sizes']['apparent_bytes']+central+reserve
    if free<required: raise Refused(f'Insufficient space: require {required} available bytes, have {free}')
    return required


def verify_archive(path, roots):
    path=Path(path)
    if path.is_symlink() or path.stat().st_size<1024 or path.stat().st_size%512: raise Refused('Truncated/unsafe tar archive')
    with path.open('rb') as stream:
        stream.seek(-1024,2)
        if stream.read()!=b'\0'*1024: raise Refused('Missing tar end-of-archive blocks')
    allowed=[PurePosixPath(p.lstrip('/')) for p in roots]
    seen=set(); members=[]
    with tarfile.open(path,'r:') as archive:
        for m in archive:
            p=PurePosixPath(m.name)
            if p.is_absolute() or '..' in p.parts or not any(p==r or r in p.parents for r in allowed): raise Refused('Unsafe archive member path')
            if m.name in seen or not (m.isfile() or m.isdir() or m.issym() or m.islnk()): raise Refused('Duplicate/unsupported archive member')
            seen.add(m.name); members.append(m)
        symlinks={PurePosixPath(m.name) for m in members if m.issym()}
        for m in members:
            p=PurePosixPath(m.name)
            if any(parent in symlinks for parent in p.parents): raise Refused('Archive traverses a symlink')
            if m.issym() or m.islnk():
                if PurePosixPath(m.linkname).is_absolute(): raise Refused('Absolute archive links unsupported')
                target=os.path.normpath(str((p.parent if m.issym() else PurePosixPath())/m.linkname))
                t=PurePosixPath(target)
                if '..' in t.parts or not any(t==r or r in t.parents for r in allowed): raise Refused('Archive link escapes durable roots')
                if m.islnk() and target not in seen: raise Refused('Archive hardlink target absent')
            if m.isfile():
                stream=archive.extractfile(m)
                for _ in iter(lambda:stream.read(1024*1024),b''): pass
    if not members or any(str(r) not in seen for r in allowed): raise Refused('Archive missing required roots')
    return len(members)


def journal(root, operation, callback):
    path=root/'operations.json'; state=json.loads(path.read_text()) if path.exists() else {}
    if operation in state: raise Refused('Operation already entered; inspect journal, no automatic retry: '+operation)
    state[operation]=dict(status='INTENT',time=time.time(),pid=os.getpid(),boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()); save(path,state)
    try:
        result=callback(); state[operation].update(status='COMPLETE',ended=time.time()); save(path,state); return result
    except Exception as exc:
        state[operation].update(status='FAILED',error=type(exc).__name__); save(path,state); raise


def stopped(plan):
    live=containers()
    if any(r['State']['Running'] for r in live): raise Refused('Live container writer remains')
    for original in plan['roots']:
        argv=['lsof','-t',*(['+D',original] if Path(original).is_dir() else ['--',original])]
        result=subprocess.run(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=120)
        if result.returncode not in (0,1) or result.stdout.strip() or result.stderr.strip():
            raise Refused('Open files or unavailable writer check in durable storage: '+original)
    for unit in plan['services']:
        if command(['systemctl','show',unit,'-p','ActiveState','--value']).strip() not in ('inactive','failed'): raise Refused('Live systemd writer remains')


def shutdown_guests(ids, timeout):
    for server in ids:
        command(['docker','exec','nova_libvirt','virsh','shutdown',server,'--mode','acpi'])
    deadline=time.monotonic()+timeout
    for server in ids:
        while command(['docker','exec','nova_libvirt','virsh','domstate',server]).strip()!='shut off':
            if time.monotonic()>=deadline: raise Refused('Graceful guest shutdown timeout; no destroy fallback')
            time.sleep(2)


def quiesce(plan):
    for unit in plan['services']: command(['systemctl','disable','--now',unit])
    for c in plan['containers']:
        command(['docker','update','--restart=no',c['id']])
        command(['docker','stop','--time','60',c['id']],timeout=90)
        state=json.loads(command(['docker','inspect',c['id']]))[0]['State']
        if c['running'] and (state.get('OOMKilled') or state.get('ExitCode') in (137,-9) or
                             (c['name'] in ('mariadb','rabbitmq') and state.get('ExitCode')!=0)):
            raise Refused('Writer did not exit cleanly; crash-consistent checkpoint prohibited')
    stopped(plan)


def archive_node(cfg, plan):
    root=checkpoint_path(cfg); stopped(plan)
    if identity()!=plan['identity']: raise Refused('Wrong host')
    libvirt=next((c for c in plan['containers'] if c['name']=='nova_libvirt'),None)
    if libvirt:
        prefix=['docker','run','--rm','--network','none','--read-only','--user','0','--volumes-from','nova_libvirt:ro','--entrypoint','qemu-img',libvirt['image']]
        for entry in plan['backing']:
            chain=json.loads(command(prefix+['info','--backing-chain','--output=json',entry['guest_path']]))
            current=backing_files(chain,plan['mounts'],plan['roots'],entry['domain'])
            if any(value not in plan['backing'] for value in current): raise Refused('Backing chain changed after planning')
            if entry['format']=='qcow2': command(prefix+['check',entry['guest_path']],timeout=cfg['archive_timeout'])
        stopped(plan)
    paths=[Path(p) for p in plan['roots']]; files=[]; ephemeral=[]
    for p in paths:
        f,e=entries(p); files.extend(f); ephemeral.extend(e)
    listing=root/'files.list'; listing.write_bytes(b''.join(str(p).lstrip('/').encode()+b'\0' for p in files)); listing.chmod(0o600)
    archive=root/'data.tar'
    command(['tar','--create','--file',str(archive),'--directory','/','--format=pax','--sparse','--numeric-owner',
             '--acls','--xattrs','--xattrs-include=*','--selinux','--no-recursion','--null','--files-from',str(listing)],timeout=cfg['archive_timeout'])
    archive.chmod(0o600); verify_archive(archive,plan['roots'])
    command(['tar','--compare','--file',str(archive),'--directory','/','--numeric-owner','--acls','--xattrs','--xattrs-include=*'],timeout=cfg['archive_timeout'])
    save(root/'ephemeral-excluded.json',ephemeral)
    return dict(sha256=digest(archive),bytes=archive.stat().st_size,members=verify_archive(archive,plan['roots']))


class UnixHTTP(http.client.HTTPConnection):
    def connect(self):
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); self.sock.settimeout(120); self.sock.connect('/var/run/docker.sock')


def recreate(private):
    for row in private:
        name=row['Name'].lstrip('/')
        config=dict(row['Config']); config['Image']=row['Image']
        config['HostConfig']=dict(row['HostConfig'],RestartPolicy=dict(Name='no',MaximumRetryCount=0))
        conn=UnixHTTP('localhost'); conn.request('POST','/containers/create?name='+name,body=json.dumps(config),headers={'Content-Type':'application/json'})
        response=conn.getresponse(); response.read(); conn.close()
        if response.status!=201: raise Refused('Docker recreation failed for '+name+'; private config retained')


def restore_data(cfg, plan):
    root=checkpoint_path(cfg); stopped(plan)
    stage=root/'restore-stage'; quarantine=root/'quarantine'
    if stage.exists() or quarantine.exists(): raise Refused('Partial restore present; inspect journal, no overwrite')
    stage.mkdir(mode=0o700); quarantine.mkdir(mode=0o700)
    verify_archive(root/'data.tar',plan['roots'])
    command(['tar','--extract','--file',str(root/'data.tar'),'--directory',str(stage),'--numeric-owner','--same-owner',
             '--same-permissions','--acls','--xattrs','--xattrs-include=*','--selinux','--sparse'],timeout=cfg['archive_timeout'])
    for original in plan['roots']:
        p=Path(original); replacement=stage/original.lstrip('/')
        if not replacement.exists() or p.is_symlink(): raise Refused('Missing staged root or changed destination')
        old=quarantine/original.lstrip('/'); old.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        save(root/'replacement-intent.json',dict(path=original,quarantine=str(old),stage=str(replacement)))
        if p.exists(): p.rename(old)
        replacement.rename(p)
    current=containers()
    for row in current: command(['docker','rm',row['Id']])  # never -v; volumes/checkpoints retained
    recreate(json.loads((root/'containers.private.json').read_text()))
    for volume in plan['volumes']:
        if volume['Name']=='neutron_metadata_socket':
            for p in Path(volume['Mountpoint']).rglob('*'):
                if p.is_socket() or p.name.endswith(('.pid','.sock')): p.unlink()
                elif p.is_file(): raise Refused('Unexpected durable data in metadata socket volume')
    command(['systemctl','daemon-reload'])
    for unit in plan['services']: command(['systemctl','disable',unit])
    return dict(status='DATA_RESTORED_REBOOT_REQUIRED',quarantine=str(quarantine))


def start(plan):
    # Infrastructure before APIs/agents; Nova guests are started separately.
    rank=lambda c: (0 if c['name'] in ('mariadb','rabbitmq','memcached','openvswitch_db','openvswitch_vswitchd') else 1,c['name'])
    for c in sorted(plan['containers'],key=rank):
        policy=c['restart']; value=policy['Name']
        if value=='on-failure' and policy.get('MaximumRetryCount'): value+=':'+str(policy['MaximumRetryCount'])
        command(['docker','update','--restart='+value,c['name']])
        unit='kolla-'+c['name']+'-container.service'
        if plan['services'][unit]['UnitFileState']=='enabled': command(['systemctl','enable',unit])
        if c['running']: command(['systemctl','start',unit],timeout=180)
    return dict(status='STARTED')


def start_state(plan):
    rows=containers(); expected={c['name']:c for c in plan['containers']}
    if {r['Name'].lstrip('/') for r in rows}!=set(expected): raise Refused('Unexpected/missing source containers; start reconciliation prohibited')
    ready=True
    for row in rows:
        c=expected[row['Name'].lstrip('/')]
        if row['Image']!=c['image'] or row['Mounts']!=c['mounts']: raise Refused('Source container image/mount identity changed')
        unit='kolla-'+c['name']+'-container.service'; old=plan['services'][unit]
        props=dict(line.split('=',1) for line in command(['systemctl','show',unit,'-p','LoadState','-p','FragmentPath','-p','DropInPaths','-p','ActiveState','-p','UnitFileState']).splitlines() if '=' in line)
        if props.get('LoadState')!='loaded' or any(props.get(k,'')!=old.get(k,'') for k in ('FragmentPath','DropInPaths')):
            raise Refused('Source unit identity changed')
        if props.get('ActiveState') not in ('active','inactive','failed'): raise Refused('In-flight source unit state; no retry')
        if props.get('UnitFileState') not in ('enabled','disabled'): raise Refused('Unknown/masked source unit state; no retry')
        ready &= (row['State']['Running']==c['running'] and row['HostConfig']['RestartPolicy']==c['restart'] and
                  props['UnitFileState']==old['UnitFileState'] and props['ActiveState']==('active' if c['running'] else 'inactive'))
    return dict(status='READY' if ready else 'NOT_READY')


def resume_start(root, plan):
    """Only reconciled source starts may continue; INTENT is never replayed."""
    state=json.loads((root/'operations.json').read_text()) if (root/'operations.json').exists() else {}
    if any(r.get('status') not in ('COMPLETE','FAILED') for r in state.values()): raise Refused('Unknown/in-flight node operation; start prohibited')
    observed=start_state(plan)
    if observed['status']=='READY': return observed
    attempt=sum(k.startswith('restore-complete-start') for k in state)
    result=journal(root,'restore-complete-start-attempt-'+str(attempt+1),lambda:start(plan))
    if start_state(plan)['status']!='READY': raise Refused('Source start did not reconcile; health not attempted')
    return result


def node_verify(cfg, plan, artifact):
    root=checkpoint_path(cfg)
    if identity()!=plan['identity']: raise Refused('Wrong host identity')
    for p in plan['roots']:
        if str(Path(p).resolve())!=p or not Path(p).exists(): raise Refused('Required restore destination absent/changed')
    for value in plan['host_inputs']:
        if str(Path(value['path']).resolve())!=value['realpath']: raise Refused('Host OS input path changed')
    for c in plan['containers']: command(['docker','image','inspect',c['image']])
    for v in plan['volumes']:
        now=json.loads(command(['docker','volume','inspect',v['Name']]))[0]
        if now['Driver']!='local' or now['Mountpoint']!=v['Mountpoint']: raise Refused('Volume identity/path changed')
    if any((root/name).is_symlink() for name in ('data.tar','containers.private.json')): raise Refused('Symlink artifact path')
    if digest(root/'data.tar')!=artifact['sha256']: raise Refused('Corrupt checkpoint archive')
    if digest(root/'containers.private.json')!=artifact['private_sha256']: raise Refused('Corrupt private container restore inputs')
    private=json.loads((root/'containers.private.json').read_text())
    if compact(private)!=plan['containers']: raise Refused('Private Docker inputs disagree with manifest')
    verify_archive(root/'data.tar',plan['roots'])
    for entry in plan['backing']:
        relative=entry['host_path'].lstrip('/')
        with tarfile.open(root/'data.tar') as archive:
            try: archive.getmember(relative)
            except KeyError: raise Refused('Backing file absent from archive') from None
    return dict(status='VERIFIED')


def main():
    os.umask(0o077)
    p=argparse.ArgumentParser(); p.add_argument('action'); p.add_argument('payload'); args=p.parse_args()
    cfg=json.loads(base64.b64decode(args.payload)); root=checkpoint_path(cfg)
    if args.action=='discover': result=discover(cfg)
    elif args.action=='discover-recovery': result=discover(cfg,recovery=True)
    elif args.action=='identity': result=dict(identity=identity(),boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip())
    elif args.action=='operation-state':
        result=json.loads((root/'operations.json').read_text()) if (root/'operations.json').exists() else {}
    elif args.action=='health':
        rows=containers()
        for c in cfg['plan']['containers']:
            actual=next((r for r in rows if r['Name'].lstrip('/')==c['name']),None)
            if not actual or actual['Image']!=c['image'] or actual['State']['Running']!=c['running'] or actual['State'].get('Health',{}).get('Status')=='unhealthy': raise Refused('Original container health/state/image not restored')
        if cfg['role']=='control':
            import configparser
            ml2=configparser.ConfigParser(interpolation=None)
            with open('/etc/kolla/neutron-server/ml2_conf.ini') as stream: ml2.read_file(stream)
            if ml2.get('ml2','mechanism_drivers').strip()!='openvswitch' or ml2.get('ml2','tenant_network_types').strip()!='vxlan': raise Refused('Restored ML2 source is not OVS/VXLAN')
        if cfg['role'] in ('network','compute'):
            import configparser
            ovs=configparser.ConfigParser(interpolation=None)
            with open('/etc/kolla/neutron-openvswitch-agent/openvswitch_agent.ini') as stream: ovs.read_file(stream)
            if ovs.get('securitygroup','firewall_driver').strip()!='openvswitch': raise Refused('Source OVS native firewall is not restored')
        result=dict(status='PASS')
    elif args.action=='init':
        if root.exists():
            if cfg['role']!='control' or not (root/'manifest.json').is_file() or json.loads((root/'manifest.json').read_text()).get('id')!=cfg['id'] or (root/'containers.private.json').exists():
                raise Refused('Checkpoint node directory already exists; no overwrite')
        else: root.mkdir(parents=True,mode=0o700)
        save(root/'containers.private.json',containers()); save(root/'plan.json',cfg['plan'])
        result=dict(private_sha256=digest(root/'containers.private.json'))
    elif args.action=='shutdown': result=journal(root,cfg['operation'],lambda:shutdown_guests(cfg['guests'],cfg['guest_timeout']))
    elif args.action=='quiesce': result=journal(root,cfg['operation'],lambda:quiesce(cfg['plan']))
    elif args.action=='archive': result=journal(root,'archive',lambda:archive_node(cfg,cfg['plan']))
    elif args.action=='verify': result=node_verify(cfg,cfg['plan'],cfg['artifact'])
    elif args.action=='restore': result=journal(root,'restore',lambda:restore_data(cfg,cfg['plan']))
    elif args.action=='start-state': result=start_state(cfg['plan'])
    elif args.action=='resume-start': result=resume_start(root,cfg['plan'])
    elif args.action=='start': result=journal(root,cfg['operation'],lambda:start(cfg['plan']))
    else: raise Refused('Unknown node action')
    print(json.dumps(result))


if __name__=='__main__':
    try: main()
    except Exception as exc:
        print(json.dumps(dict(status='FAILED',error=type(exc).__name__,reason=str(exc) if isinstance(exc,Refused) else 'Node operation failed; inspect private journal')))
        raise SystemExit(1)
