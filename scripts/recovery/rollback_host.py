#!/usr/bin/env python3
"""Source-config snapshots and guarded in-place ML2/OVS restoration per host."""
import sys
from pathlib import Path
if (Path(__file__).resolve().parents[1] / '_paths.py').is_file():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from _paths import configure
    configure()
import argparse
import configparser
import json
import os
import re
import tarfile
import time
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET
from rollback_common import LEGACY, UNITS, Stop, command, journal, owned, save, sha

CONFIG_ROOT = Path('/etc/kolla')


def container(name):
    result = command(['docker', 'inspect', name], check=False)
    if result.returncode:
        if 'No such' in result.stderr: return None
        raise Stop('Cannot inspect container ' + name)
    return json.loads(result.stdout)[0]


def service_manifest(name):
    row = container(name)
    if row is None: return None
    return dict(id=row['Id'], image_id=row['Image'], running=row['State']['Running'],
                restart=row['HostConfig']['RestartPolicy'],
                unit_enabled=command(['systemctl', 'is-enabled', UNITS[name]], check=False).stdout.strip(),
                mounts=[{k: m.get(k) for k in ('Type','Name','Source','Destination','RW')} for m in row.get('Mounts', [])])


def ovs(*args):
    return command(['docker', 'exec', 'openvswitch_vswitchd', 'ovs-vsctl', '--timeout=10'] + list(args)).stdout


def decode(value):
    if isinstance(value, list) and len(value) == 2:
        if value[0] == 'set': return [decode(v) for v in value[1]]
        if value[0] == 'map': return dict(value[1])
        if value[0] in ('uuid', 'named-uuid'): return value[1]
    return value


def ovs_table(table):
    raw = json.loads(ovs('--format=json', 'list', table))
    return [{k: decode(v) for k, v in zip(raw['headings'], row)} for row in raw['data']]


def vm_state():
    raw = command(['docker', 'exec', 'nova_libvirt', 'virsh', 'list', '--all', '--uuid']).stdout
    result = {}
    for identifier in sorted(raw.split()):
        if not re.fullmatch(r'[0-9a-f-]{36}', identifier): raise Stop('Invalid libvirt domain UUID')
        root = ET.fromstring(command(['docker', 'exec', 'nova_libvirt', 'virsh', 'dumpxml', identifier]).stdout)
        state = command(['docker', 'exec', 'nova_libvirt', 'virsh', 'domstate', identifier]).stdout.strip()
        interfaces = []
        for node in root.findall('./devices/interface'):
            interfaces.append({tag: dict(child.attrib) for tag in ('mac', 'target', 'source')
                               if (child := node.find(tag)) is not None})
        result[identifier] = dict(state=state, interfaces=sorted(interfaces, key=lambda v: json.dumps(v, sort_keys=True)))
    return result


def observe(cfg):
    result = dict(schema=1, run=cfg['run'], host=cfg['host'], observed_at=time.time(), services={})
    for name in UNITS:
        row = container(name)
        result['services'][name] = None if row is None else dict(id=row['Id'], image_id=row['Image'], running=row['State']['Running'])
    if cfg.get('chassis'):
        result['ovs'] = {table: ovs_table(table) for table in ('Open_vSwitch','Bridge','Port','Interface','Controller')}
        result['namespaces'] = command(['ip', 'netns', 'list']).stdout.splitlines()
        result['addresses'] = json.loads(command(['ip', '-j', '-4', 'addr', 'show']).stdout)
    if cfg.get('compute'): result['vms'] = vm_state()
    api = result['services']['neutron_server']
    if api and api['running']:
        raw = command(['docker', 'exec', 'neutron_server', 'cat', '/etc/neutron/plugins/ml2/ml2_conf.ini']).stdout
        parser = configparser.ConfigParser(interpolation=None); parser.read_string(raw)
        result['api_driver'] = parser.get('ml2', 'mechanism_drivers')
    return result


def source(directory, cfg):
    path = directory / 'source.json'
    if path.exists(): raise Stop('Source snapshot already exists; never rebase a run')
    if container('ovn_controller') or container('neutron_ovn_metadata_agent'):
        raise Stop('Source rollback snapshot requires a clean ML2/OVS baseline without OVN host containers')
    services = {name: service_manifest(name) for name in LEGACY}
    if cfg.get('control'):
        if not services['neutron_server'] or not services['neutron_server']['running']: raise Stop('Source Neutron API is not running')
        conf = configparser.ConfigParser(interpolation=None); conf.read(CONFIG_ROOT/'neutron-server/neutron.conf')
        database = urlsplit(conf.get('database','connection')).path.strip('/')
        if database != cfg.get('database', 'neutron'): raise Stop('Configured Neutron database differs from rollback schema')
        ml2 = configparser.ConfigParser(interpolation=None); ml2.read(CONFIG_ROOT/'neutron-server/ml2_conf.ini')
        if 'openvswitch' not in ml2.get('ml2','mechanism_drivers'): raise Stop('Source is not ML2/OVS')
    archive = directory / 'source.tar'
    with tarfile.open(archive, 'w', dereference=True) as bundle:
        if CONFIG_ROOT.is_dir(): bundle.add(CONFIG_ROOT, arcname='etc/kolla', recursive=True)
        for name in LEGACY:
            unit = Path('/etc/systemd/system') / UNITS[name]
            for target in (unit, unit.with_name(unit.name + '.d')):
                if target.exists(): bundle.add(target, arcname=str(target).lstrip('/'), recursive=True)
    archive.chmod(0o600)
    value = dict(schema=1, status='PASS', run=cfg['run'], host=cfg['host'], roles=cfg,
                 archive_sha256=sha(archive), services=services, created_at=time.time())
    save(path, value)
    return dict(status='PASS', host=cfg['host'], archive_sha256=value['archive_sha256'])


def freeze(directory, cfg):
    path = directory / 'freeze.json'
    if path.exists(): raise Stop('Frozen host topology must never be overwritten')
    state = observe(cfg)
    if cfg.get('chassis'):
        ext = state['ovs']['Open_vSwitch'][0]['external_ids']
        if not ext.get('system-id'): raise Stop('Missing persistent source system-id')
        controllers={r['_uuid']:r['target'] for r in state['ovs']['Controller']}
        for bridge in state['ovs']['Bridge']:
            if bridge.get('datapath_type') not in ('','system'):raise Stop('Rollback does not support DPDK/netdev datapaths')
            refs=bridge.get('controller',[]);refs=refs if isinstance(refs,list) else [refs]
            if any(not controllers[r].startswith(('tcp:127.0.0.1:','tcp:[::1]:','unix:','punix:')) for r in refs):
                raise Stop('External OpenFlow controllers require a separate rollback design')
    save(path, state)
    return dict(status='PASS', host=cfg['host'], freeze_sha256=sha(path))


def validate_source(directory, cfg):
    source = json.loads((directory/'source.json').read_text())
    if source['run'] != cfg['run'] or source['host'] != cfg['host'] or sha(directory/'source.tar') != source['archive_sha256']:
        raise Stop('Source configuration ownership/hash differs')
    for name, original in source['services'].items():
        if not original: continue
        current = container(name)
        if not current or current['Id'] != original['id'] or current['Image'] != original['image_id']:
            raise Stop('Original container was removed/replaced: ' + name)
        mounts = [{k: m.get(k) for k in ('Type','Name','Source','Destination','RW')} for m in current.get('Mounts', [])]
        if mounts != original['mounts']: raise Stop('Source container mounts changed: ' + name)
    if cfg.get('compute') and (directory/'freeze.json').exists():
        frozen=json.loads((directory/'freeze.json').read_text())
        if vm_state() != frozen.get('vms'):
            raise Stop('VM/domain/interface placement changed since the frozen checkpoint')
        if 'ovs' in frozen:
            macs={interface.get('mac',{}).get('address','').lower() for vm in frozen['vms'].values() for interface in vm['interfaces']}
            current={row['name']:row for row in ovs_table('Interface')}
            for row in frozen['ovs']['Interface']:
                ext=row.get('external_ids',{})
                if ext.get('attached-mac','').lower() not in macs or not ext.get('iface-id'):continue
                actual=current.get(row['name'],{}).get('external_ids',{})
                if actual.get('iface-id')!=ext['iface-id'] or actual.get('attached-mac','').lower()!=ext.get('attached-mac','').lower():
                    raise Stop('Original VM VIF identity changed; refusing destructive restoration')
    return source


def inspect(directory, cfg):
    validate_source(directory, cfg)
    state = observe(cfg)
    captures=[]
    for name, schema, socket in ((('ovn_nb_db','OVN_Northbound','ovnnb_db.sock'),('ovn_sb_db','OVN_Southbound','ovnsb_db.sock')) if cfg.get('capture_failure',True) else ()):
        row=container(name)
        if row and row['State']['Running']:
            path=directory/(name+'-before-rollback.backup')
            if not path.exists():
                raw=command(['docker','exec',name,'ovsdb-client','backup','unix:/var/run/ovn/'+socket,schema],timeout=60).stdout
                with open(path,'w',opener=lambda p,f:os.open(p,f,0o600)) as output:output.write(raw)
            captures.append(dict(path=str(path),sha256=sha(path)))
    state['ovn_backups']=captures
    save(directory/'inspection.json', state)
    return dict(status='PASS', **state)


def stop_service(name, graceful=False):
    row = container(name)
    if row:
        command(['docker','update','--restart=no',name])
        if graceful and row['State']['Running']:
            command(['docker','exec',name,'ovn-appctl','-t','ovn-controller','exit'], check=False, timeout=15)
    unit = command(['systemctl','show',UNITS[name],'--property=LoadState','--value'], check=False).stdout.strip()
    if unit in ('loaded','masked'): command(['systemctl','disable','--now',UNITS[name]])
    row = container(name)
    if row and row['State']['Running']: command(['docker','stop','--time=10',name], timeout=20)
    row = container(name)
    if row and row['State']['Running']: raise Stop('Container remains running: ' + name)


def quiesce(directory, cfg):
    validate_source(directory, cfg)
    ids=command(['docker','ps','--quiet']).stdout.split()
    for identifier in ids:
        row=container(identifier)
        commandline=json.dumps(row['Config'].get('Cmd', []))
        if any(token in commandline for token in ('neutron-ovn-db-sync-util','/opt/provider_associations.py','/opt/gateway_native.py')):
            labels=row['Config'].get('Labels') or {}
            if cfg['run'] not in (labels.get('ovs-to-ovn.migration-run'),labels.get('ovs-to-ovn.gateway-run')):
                raise Stop('An unowned migration helper is still running')
            command(['docker','update','--restart=no',identifier])
            command(['docker','stop','--time=10',identifier],timeout=20)
            remaining=container(identifier)
            if remaining and remaining['State']['Running']:raise Stop('Migration helper remains alive')
    # No API, L2, L3 or metadata writers may race the database/flow restoration.
    names=('neutron_server','ovn_controller','neutron_ovn_metadata_agent') + LEGACY[1:]
    if not cfg.get('db_restore') and not cfg.get('flow_rebuild'):
        names=('ovn_controller','neutron_ovn_metadata_agent')
    for name in names:
        stop_service(name, graceful=name == 'ovn_controller')
    state = observe(cfg); save(directory/'quiesced.json', state)
    return dict(status='PASS', host=cfg['host'], all_writers_stopped=bool(cfg.get('db_restore')))


def selected_member(name):
    path = Path(name)
    if path.is_absolute() or '..' in path.parts: raise Stop('Unsafe archive member')
    if path.parts[:2] == ('etc','kolla'):
        return len(path.parts) > 2 and (path.parts[2].startswith('neutron-') or path.parts[2] == 'globals.yml')
    return path.parts[:3] == ('etc','systemd','system') and len(path.parts) > 3 and any(path.parts[3].startswith(UNITS[n]) for n in LEGACY)


def restore_config(directory, cfg):
    validate_source(directory, cfg)
    with tarfile.open(directory/'source.tar') as archive:
        members = [m for m in archive.getmembers() if selected_member(m.name)]
        # Validate every destination before writing any file; no links/devices.
        for member in members:
            if not (member.isfile() or member.isdir()): raise Stop('Non-regular source config member')
            dest = Path('/')/member.name
            if any(p.is_symlink() for p in [dest]+list(dest.parents)): raise Stop('Source restore target is a symlink')
        for member in members:
            dest = Path('/')/member.name
            if member.isdir(): dest.mkdir(parents=True, exist_ok=True, mode=member.mode); continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as stream:
                data = stream.read()
            temporary = dest.with_name(dest.name+'.rollback-tmp')
            with temporary.open('wb') as output: output.write(data)
            temporary.chmod(member.mode); os.chown(temporary, member.uid, member.gid); temporary.replace(dest)
    command(['systemctl','daemon-reload'])
    return dict(status='PASS', host=cfg['host'], restored_files=len(members))


def recover_ovs(directory, cfg):
    if not cfg.get('chassis') or not cfg.get('db_restore'): return dict(status='PASS', host=cfg['host'], skipped=True)
    baseline = json.loads((directory/'freeze.json').read_text())
    current = observe(cfg)
    for name in ('ovn_controller','neutron_ovn_metadata_agent','neutron_openvswitch_agent','neutron_l3_agent','neutron_dhcp_agent'):
        if current['services'][name] and current['services'][name]['running']: raise Stop('Dataplane writer is still running: '+name)
    before = baseline['ovs']; after = current['ovs']
    if before['Open_vSwitch'][0]['external_ids']['system-id'] != after['Open_vSwitch'][0]['external_ids'].get('system-id'):
        raise Stop('Persistent OVS system-id changed')
    source_bridges = {r['name']:r for r in before['Bridge']}
    actual_bridges = {r['name']:r for r in after['Bridge']}
    for name in ('br-int','br-ex'):
        if name in source_bridges and (name not in actual_bridges or source_bridges[name]['_uuid'] != actual_bridges[name]['_uuid']):
            raise Stop('Protected bridge identity changed: '+name)
    source_ports = {r['name'] for r in before['Port']}
    interfaces = {r['name']:r for r in after['Interface']}
    metadata = set(cfg.get('metadata_port_ids', [])); removed=[]
    for port in after['Port']:
        if port['name'] in source_ports: continue
        iface = interfaces.get(port['name'], {})
        ext = {**port.get('external_ids', {}), **iface.get('external_ids', {})}
        is_tunnel = port['name'].startswith('ovn-') and iface.get('type') == 'geneve'
        is_patch = bool(ext.get('ovn-localnet-port') or ext.get('ovn-l2gateway-port') or ext.get('ovn-l3gateway-port'))
        is_metadata = ext.get('iface-id') in metadata
        if is_tunnel or is_patch or is_metadata:
            ovs('--if-exists','del-port',port['name']); removed.append(port['name'])
    for row in current['namespaces']:
        namespace = row.split()[0]
        if namespace.startswith('ovnmeta-'):
            if command(['ip','netns','pids',namespace]).stdout.strip(): raise Stop('OVN metadata namespace still has processes')
            command(['ip','netns','delete',namespace])
    old_ext = before['Open_vSwitch'][0]['external_ids']; now_ext = after['Open_vSwitch'][0]['external_ids']
    for key in sorted(set(old_ext) | set(now_ext)):
        if key.startswith('ovn-'):
            if key in old_ext: ovs('set','Open_vSwitch','.', 'external_ids:'+key+'='+old_ext[key])
            elif key in now_ext: ovs('remove','Open_vSwitch','.', 'external_ids',key)
    controllers = {r['_uuid']:r['target'] for r in before['Controller']}
    for bridge in before['Bridge']:
        if bridge['name'] not in ('br-int','br-ex'): continue
        refs = bridge.get('controller', []); refs = refs if isinstance(refs,list) else [refs]
        ovs('del-controller',bridge['name'])
        targets = [controllers[r] for r in refs]
        if targets: ovs('set-controller',bridge['name'],*targets)
        mode = bridge.get('fail_mode')
        if isinstance(mode,list): mode = mode[0] if mode else None
        if mode: ovs('set-fail-mode',bridge['name'],mode)
        else: ovs('del-fail-mode',bridge['name'])
    # Rebuild the integration pipeline only, preserving every VM/uplink port.
    # OVS agent will regenerate local VLANs, br-tun and physical bridge patches.
    if cfg.get('flow_rebuild'):
        for operation in ('del-flows','del-groups'):
            command(['docker','exec','openvswitch_vswitchd','ovs-ofctl','-O','OpenFlow13,OpenFlow15',operation,'br-int'])
    for bridge in before['Bridge']:
        if bridge['name'] in ('br-int','br-ex'):
            protocols=bridge.get('protocols',[]);protocols=protocols if isinstance(protocols,list) else [protocols]
            if protocols:ovs('set','Bridge',bridge['name'],'protocols='+','.join(protocols))
            else:ovs('clear','Bridge',bridge['name'],'protocols')
    return dict(status='PASS', host=cfg['host'], removed_ovn_ports=removed)


def start_sources(directory, cfg, api_only=False):
    source = validate_source(directory, cfg)
    for name, original in source['services'].items():
        if (name == 'neutron_server') != api_only or not original: continue
        policy = original['restart']['Name'] or 'no'
        if policy == 'on-failure' and original['restart'].get('MaximumRetryCount'):
            policy += ':' + str(original['restart']['MaximumRetryCount'])
        command(['docker','update','--restart='+policy,name])
        if original['unit_enabled'] == 'enabled': command(['systemctl','enable',UNITS[name]])
        if original['running']:
            if original['unit_enabled'] == 'not-found': command(['docker','start',name])
            else: command(['systemctl','start',UNITS[name]])
    return dict(status='PASS', host=cfg['host'])


def verify(directory, cfg):
    source = validate_source(directory, cfg)
    state = observe(cfg)
    for name, old in source['services'].items():
        if old and old['running']:
            row = container(name)
            if not row or not row['State']['Running'] or row['State'].get('Health',{}).get('Status','healthy') != 'healthy':
                raise Stop('Source service has not converged: '+name)
    for name in ('ovn_controller','neutron_ovn_metadata_agent'):
        if state['services'][name] and state['services'][name]['running']: raise Stop('OVN host service restarted')
    if cfg.get('control') and 'openvswitch' not in state.get('api_driver',''): raise Stop('Neutron API is not using ML2/OVS')
    if cfg.get('chassis'):
        frozen=json.loads((directory/'freeze.json').read_text()) if (directory/'freeze.json').exists() else None
        bridges={r['name'] for r in state['ovs']['Bridge']}
        if 'br-tun' not in bridges: raise Stop('OVS tunnel bridge has not been recreated')
        ports={r['name'] for r in state['ovs']['Port']}
        # All original non-tunnel bridge/VM/service ports must come back.
        expected={r['name'] for r in frozen['ovs']['Port'] if not r['name'].startswith(('vxlan-','gre-'))} if frozen else set()
        if not expected <= ports: raise Stop('Original source ports are still missing')
        def ips(rows): return {r['ifname']:sorted((a['local'],a['prefixlen']) for a in r.get('addr_info',[]) if a.get('family')=='inet') for r in rows if r.get('addr_info')}
        if frozen and ips(frozen['addresses']) != ips(state['addresses']): raise Stop('Root-namespace IPv4/underlay addresses changed')
    save(directory/'verified.json', state)
    return dict(status='PASS', host=cfg['host'], infrastructure_only=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('source','freeze','inspect','quiesce','restore-config','recover-ovs','start-api','start-agents','verify','event'))
    parser.add_argument('config'); args=parser.parse_args(); cfg=json.loads(args.config)
    result={}
    try:
        directory=owned(cfg['run'],cfg['host'],create=args.action=='source')
        if args.action=='event':
            journal(directory/'events.jsonl',cfg['stage'],cfg['edge']); result=dict(status='PASS')
        else:
            mutation=args.action in ('quiesce','restore-config','recover-ovs','start-api','start-agents')
            if mutation and (cfg.get('execute') is not True or cfg.get('maintenance') is not True): raise Stop('Explicit execution and maintenance are required')
            if mutation: journal(directory/'events.jsonl',args.action,'intent')
            action={'restore-config':restore_config,'recover-ovs':recover_ovs,
                    'start-api':lambda d,c:start_sources(d,c,True), 'start-agents':start_sources}.get(args.action)
            result=(action or globals()[args.action])(directory,cfg)
            if mutation: journal(directory/'events.jsonl',args.action,'complete')
    except Exception as exc:
        result=dict(status='STOP',error_type=type(exc).__name__)
        if isinstance(exc,Stop):result['reason']=str(exc)
    print(json.dumps(result,sort_keys=True))
    return 0 if result['status']=='PASS' else 1


if __name__=='__main__':sys.exit(main())
