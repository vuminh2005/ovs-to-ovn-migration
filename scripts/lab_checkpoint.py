#!/usr/bin/env python3
"""Standalone coordinated cold checkpoint; never imported by migration entrypoints."""
import argparse
import base64
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time

from lab_checkpoint_host import Refused, checkpoint_path, digest, save, space, verify_archive
from ew_transport import Transport, verify_profile


NODE=Path(__file__).with_name('lab_checkpoint_host.py')


def checked(argv, timeout=180, **kwargs):
    result=subprocess.run(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=timeout,**kwargs)
    if result.returncode: raise Refused(f'{Path(argv[0]).name} failed (exit {result.returncode}); output suppressed to protect secrets')
    return result.stdout


class Hosts:
    def __init__(self,cfg): self.cfg=cfg
    def module(self,host,module,args,timeout=None):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*',host): raise Refused('Unsafe inventory host')
        with tempfile.TemporaryDirectory(prefix='cold-checkpoint-') as directory:
            process=subprocess.run([str(Path(self.cfg['venv'])/'bin/ansible'),host,'-i',self.cfg['inventory'],'--become','-m',module,'-a',args,'--tree',directory],
                    stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=timeout or self.cfg['archive_timeout']+180)
            path=Path(directory)/host
            if not path.is_file(): raise Refused('No node response from '+host+'; remote operation state may be ambiguous')
            result=json.loads(path.read_text())
            if process.returncode or result.get('failed') or result.get('unreachable') or result.get('rc',0):
                try:
                    safe=json.loads(result.get('stdout',''))
                    reason=safe['reason'] if safe.get('status')=='FAILED' and safe.get('error')=='Refused' else 'Node operation failed; inspect private journal'
                except (ValueError,KeyError,TypeError): reason='Node response/transport failed; inspect private journal'
                raise Refused(host+': '+reason)
            return result
    def call(self,host,action,**payload):
        data=dict(self.cfg,**payload)
        data['role']=self.cfg['roles'][host]
        encoded=base64.b64encode(json.dumps(data).encode()).decode()
        row=self.module(host,'ansible.builtin.script',shlex.join([str(NODE),action,encoded])+' executable=/usr/bin/python3')
        try: return json.loads(row['stdout'].strip())
        except (KeyError,ValueError): raise Refused('Invalid/suppressed node evidence from '+host) from None
    def fetch(self,host,name,destination):
        self.module(host,'ansible.builtin.fetch',json.dumps(dict(src=str(checkpoint_path(self.cfg)/name),dest=str(destination),flat=True)))
        Path(destination).chmod(0o600)


def inventory_roles(cfg):
    inventory=json.loads(checked([str(Path(cfg['venv'])/'bin/ansible-inventory'),'-i',cfg['inventory'],'--list']))
    def members(group):
        row=inventory.get(group,{})
        return set(row.get('hosts',[]))|set().union(*(members(g) for g in row.get('children',[])))
    groups={role:members(group) for role,group in (('control','control'),('network','network'),('compute','compute'))}
    if [len(groups[k]) for k in ('control','network','compute')]!=[1,2,2] or len(set.union(*groups.values()))!=5:
        raise Refused('This standalone tool requires exactly one controller, two network and two compute nodes')
    return {h:role for role,hosts in groups.items() for h in hosts}


def cloud_snapshot(cloud):
    resources={}
    definitions=(('networks',cloud.network.networks,('id','name','mtu','provider_network_type','is_router_external')),
                 ('subnets',cloud.network.subnets,('id','name','network_id','cidr','gateway_ip','is_dhcp_enabled')),
                 ('routers',cloud.network.routers,('id','name','external_gateway_info','is_distributed','is_ha')),
                 ('ports',cloud.network.ports,('id','network_id','device_id','device_owner','fixed_ips','mac_address')))
    for key,method,fields in definitions:
        resources[key]={r.id:{f:(sorted(getattr(r,f),key=lambda x:(x['subnet_id'],x['ip_address'])) if f=='fixed_ips' else getattr(r,f,None)) for f in fields} for r in method()}
    resources['images']={r.id:dict(id=r.id,name=r.name,checksum=r.checksum,size=r.size) for r in cloud.image.images()}
    resources['flavors']={r.id:dict(id=r.id,name=r.name,vcpus=r.vcpus,ram=r.ram,disk=r.disk) for r in cloud.compute.flavors(details=True)}
    resources['security_groups']={r.id:dict(id=r.id,name=r.name) for r in cloud.network.security_groups()}
    resources['security_group_rules']={r.id:dict(id=r.id,security_group_id=r.security_group_id) for r in cloud.network.security_group_rules()}
    for key,method in (('users',cloud.identity.users),('projects',cloud.identity.projects),('roles',cloud.identity.roles),('domains',cloud.identity.domains)):
        resources[key]={r.id:dict(id=r.id,name=r.name) for r in method()}
    resources['servers']={}
    for item in cloud.compute.servers(all_projects=True):
        r=cloud.compute.get_server(item.id)
        resources['servers'][r.id]=dict(id=r.id,name=r.name,status=r.status,host=r.compute_host,
            image=r.image,flavor=r.flavor,metadata=r.metadata)
    if list(cloud.network.ips()): raise Refused('Floating IPs are outside checkpoint scope')
    if any(n['is_router_external'] for n in resources['networks'].values()) or any(r['external_gateway_info'] or r['is_distributed'] or r['is_ha'] for r in resources['routers'].values()):
        raise Refused('Provider/external routing is outside checkpoint scope')
    return resources


def ew_catalog(snapshot, cfg):
    result=dict(router=None,servers={},configuration=cfg['ew_workload_config'])
    for definition in cfg['ew_workload_config']['servers']:
        matches=[s for s in snapshot['servers'].values() if s['name']==definition['name']]
        if len(matches)!=1: raise Refused('Missing/ambiguous EW guest')
        server=matches[0]
        if server['status']!='ACTIVE' or server['host']!=definition['compute_host']: raise Refused('EW guest must be ACTIVE on its expected host')
        nets=[n for n in snapshot['networks'].values() if n['name']==definition['network'] and n['provider_network_type']=='vxlan']
        if len(nets)!=1: raise Refused('Missing/ambiguous EW source VXLAN network')
        ports=[p for p in snapshot['ports'].values() if p['device_id']==server['id'] and p['network_id']==nets[0]['id'] and any(f['ip_address']==definition['ip'] for f in p['fixed_ips'])]
        if len(ports)!=1: raise Refused('EW exact source port/IP is ambiguous')
        if ports[0]['fixed_ips']!=[dict(subnet_id=ports[0]['fixed_ips'][0]['subnet_id'],ip_address=definition['ip'])]: raise Refused('Only one fixed IPv4 address per EW guest supported')
        result['servers'][definition['name']]=dict(server=server['id'],port=ports[0]['id'],network=nets[0]['id'],
            fixed_ips=ports[0]['fixed_ips'],ip=definition['ip'],actual_host=server['host'],mac=ports[0]['mac_address'],mtu=nets[0]['mtu'])
    if len(result['servers'])!=6 or set(snapshot['servers'])!={v['server'] for v in result['servers'].values()}:
        raise Refused('Checkpoint requires exactly the six EW guests; unrelated instances must not be stopped')
    routers=[r for r in snapshot['routers'].values() if r['name']==cfg['ew_workload_config']['router']]
    if len(routers)!=1: raise Refused('EW router is ambiguous')
    result['router']=routers[0]['id']
    return result


def validation_owned(run_dirs, snapshot):
    owned={}
    for directory in run_dirs:
        root=Path(directory); config=json.loads((root/'validation-config.json').read_text())
        state=json.loads((root/'validation-resources.json').read_text())
        if state.get('schema_version')!=2: raise Refused('Historical validation evidence cannot authorize maintenance')
        for topology,roles in (('pre',('measure','existing')),('post',('fresh',))):
            for role in roles:
                for vm in state.get(topology,{}).get(role,{}).values():
                    if vm.get('server') not in snapshot['servers']: continue
                    server=snapshot['servers'][vm['server']]; port=snapshot['ports'].get(vm.get('port'))
                    if (vm.get('owned') is not True or server['metadata'].get('ovn_migration_run')!=config['run'] or
                        server['metadata'].get('ovn_validation_role')!=role or not port or port['device_id']!=vm['server'] or
                        port['fixed_ips']!=vm['fixed_ips']): raise Refused('Validation ownership/port evidence mismatch')
                    owned[vm['server']]=server
    return owned


def restore_scope(current, original, run_dirs):
    if any(r.get('status') not in ('ACTIVE','SHUTOFF') for r in current['servers'].values()): raise Refused('Transitional/paused/error workloads must be resolved before maintenance')
    extras=validation_owned(run_dirs,current)
    if set(current['servers'])-set(original['servers']) != set(extras): raise Refused('Unrelated current workloads; restore prohibited')
    for sid,old in original['servers'].items():
        if sid not in current['servers'] or current['servers'][sid]['host']!=old['host']: raise Refused('Original EW UUID/placement changed')
        for field in ('image','flavor'):
            if field in old and current['servers'][sid].get(field)!=old[field]: raise Refused('Original EW image/flavor changed')
    for pid,old in original.get('ports',{}).items():
        if old.get('device_id') in original['servers']:
            now=current.get('ports',{}).get(pid,{})
            if any(now.get(k)!=old.get(k) for k in ('device_id','network_id','fixed_ips','mac_address')): raise Refused('Original EW port/IP/MAC changed')
    allowed={key:set() for key in ('networks','subnets','routers','ports','security_groups','images','flavors')}
    for directory in run_dirs:
        root=Path(directory); state=json.loads((root/'validation-resources.json').read_text())
        validation_cfg=json.loads((root/'validation-config.json').read_text())
        for key in ('pre','post'):
            topology=state.get(key,{})
            prefix=validation_cfg['prefix']+'-'+validation_cfg['run']+'-'+key
            for field,target in (('router','routers'),('security_group','security_groups')):
                if topology.get(field):
                    observed=current.get(target,{}).get(topology[field])
                    if observed and observed.get('name')!=prefix: raise Refused('Validation resource name/UUID ownership mismatch')
                    allowed[target].add(topology[field])
            for net in topology.get('networks',{}).values():
                if net.get('network'):
                    observed=current.get('networks',{}).get(net['network'])
                    if observed and not observed.get('name','').startswith(prefix+'-network-'): raise Refused('Validation network name/UUID ownership mismatch')
                    allowed['networks'].add(net['network'])
                if net.get('subnet'):
                    observed=current.get('subnets',{}).get(net['subnet'])
                    if observed and (observed.get('network_id')!=net['network'] or not observed.get('name','').startswith(prefix+'-network-')): raise Refused('Validation subnet ownership mismatch')
                    allowed['subnets'].add(net['subnet'])
            for role in ('measure','existing','fresh'):
                for vm in topology.get(role,{}).values():
                    if vm.get('port'): allowed['ports'].add(vm['port'])
        prerequisites=root/'validation-prerequisites.json'
        if prerequisites.exists():
            data=json.loads(prerequisites.read_text())
            for key,target in (('image','images'),('flavor','flavors')):
                if data.get(key,{}).get('created') is True: allowed[target].add(data[key]['id'])
    for key,values in current.items():
        if key=='servers': continue
        if key=='ports':
            for pid,p in values.items():
                if pid in original.get(key,{}): continue
                if p['device_owner']=='network:distributed' and p['network_id'] in original.get('networks',{}): allowed['ports'].add(pid)
                if p['device_owner'].startswith('network:') and p['network_id'] in allowed['networks']: allowed['ports'].add(pid)
        if key=='security_group_rules':
            allowed[key]={rid for rid,r in values.items() if r['security_group_id'] in allowed['security_groups']}
        if set(values)-set(original.get(key,{}))-allowed.get(key,set()): raise Refused('Unrelated current '+key+'; restore prohibited')
        if key in ('images','flavors','users','projects','roles','domains'):
            if any(rid not in values or values[rid]!=old for rid,old in original.get(key,{}).items()): raise Refused('Existing unrelated '+key+' changed')
    return extras


def offline_scope(cfg, root, manifest, current):
    """Explicit trust boundary: physical scope is verified, API-only scope is attested.

    No automatic fallback from failed APIs. An operator must assert exclusive
    lab ownership and absence of unjournaled logical resources in a private,
    seal/journal-bound declaration. Unknown host domains/interfaces still refuse.
    """
    path=Path(cfg.get('offline_scope_file',''))
    if (not path.is_absolute() or path.resolve()!=path or not path.is_file() or
        path.stat().st_uid!=os.geteuid() or path.stat().st_mode&0o077):
        raise Refused('Offline recovery requires a trusted private scope declaration')
    declaration=json.loads(path.read_text()); journals={}; expected=copy.deepcopy(manifest['resources']['servers'])
    ports={sid:[] for sid in expected}
    for pid,port in manifest['resources']['ports'].items():
        if port.get('device_id') in expected:
            ports[port['device_id']].append(dict(port=pid,mac=port['mac_address'].lower()))
    extra=set()
    for directory in cfg['validation_runs']:
        run=Path(directory)
        if not run.is_absolute() or run.resolve()!=run: raise Refused('Unsafe validation ownership directory')
        for name in ('validation-config.json','validation-resources.json','validation-prerequisites.json','pre-cleanup.json','post-cleanup.json'):
            f=run/name
            if not f.exists() and name not in ('validation-config.json','validation-resources.json'): continue
            if f.is_symlink() or not f.is_file(): raise Refused('Missing/unsafe offline ownership journal')
            journals[str(f)]=digest(f)
        config=json.loads((run/'validation-config.json').read_text()); state=json.loads((run/'validation-resources.json').read_text())
        if state.get('schema_version')!=2 or not config.get('run'): raise Refused('Unsupported offline validation ownership schema')
        for topology,roles in (('pre',('measure','existing')),('post',('fresh',))):
            receipt=run/(topology+'-cleanup.json')
            cleanup=json.loads(receipt.read_text()) if receipt.exists() else {}
            for role in roles:
                for vm in state.get(topology,{}).get(role,{}).values():
                    if not vm.get('server'):
                        if vm.get('port'): raise Refused('Incomplete offline port/server ownership; scope ambiguous')
                        continue
                    sid=vm['server']
                    if vm.get('owned') is not True or not vm.get('port') or not vm.get('fixed_ips') or sid in expected:
                        raise Refused('Ambiguous/duplicate offline validation ownership')
                    deleted=cleanup.get('deleted',[])
                    if state.get(topology,{}).get('cleaned') and (cleanup.get('status')!='PASS' or
                        any(dict(kind=k,id=vm[field]) not in deleted for k,field in (('server','server'),('port','port')))):
                        raise Refused('Missing/incomplete offline cleanup ownership receipt')
                    if dict(kind='server',id=sid) in deleted: continue
                    # Port UUID is proved from libvirt; fixed IP allocation is only
                    # journal evidence, never represented as a live API observation.
                    expected[sid]={}; ports[sid]=[dict(port=vm['port'])]; extra.add(sid)
                    if vm.get('mac') or vm.get('mac_address'): ports[sid][0]['mac']=(vm.get('mac') or vm['mac_address']).lower()
    if (declaration.get('schema_version')!=1 or declaration.get('checkpoint_manifest_sha256')!=digest(root/'manifest.json') or
        declaration.get('ownership_journals')!=journals or not declaration.get('operator') or
        any(declaration.get(k) is not True for k in ('exclusive_lab_scope','no_unjournaled_resources','no_concurrent_writers'))):
        raise Refused('Offline scope not explicitly authorized for this seal and exact ownership journals')
    observed={}
    for host,node in current.items():
        for sid in node['domains']:
            if sid not in expected or sid in observed: raise Refused('Unrelated/duplicate offline libvirt domain; scope ambiguous')
            if sid not in extra and expected[sid]['host'] not in (host,node['identity']['hostname']): raise Refused('Original EW placement changed')
            interfaces=node.get('domain_interfaces',{}).get(sid,[])
            if {p['port'] for p in interfaces}!={p['port'] for p in ports[sid]}: raise Refused('Offline Neutron port/libvirt identity differs')
            for p in ports[sid]:
                if 'mac' in p and not any(i==p for i in interfaces): raise Refused('Offline checkpointed MAC differs')
            power=node['domain_states'][sid]
            if power not in ('running','shut off'): raise Refused('Transitional/paused offline libvirt domain; scope ambiguous')
            observed[sid]=dict(id=sid,host=host,status='ACTIVE' if power=='running' else 'SHUTOFF')
    if set(observed)!=set(expected): raise Refused('Missing offline workload definitions; scope ambiguous')
    return dict(servers=observed), sorted(extra)


def sealed(root):
    for p in (root/'manifest.json',root/'seal.json'):
        if p.is_symlink() or not p.is_file(): raise Refused('Unsafe/missing checkpoint seal inputs')
    manifest=json.loads((root/'manifest.json').read_text())
    if manifest.get('schema_version')!=1 or manifest.get('state')!='SEALED' or set(manifest['artifacts'])!=set(manifest['nodes']) or len(manifest['nodes'])!=5:
        raise Refused('Incomplete/unsealed checkpoint is never eligible for restore')
    seal=json.loads((root/'seal.json').read_text())
    if seal.get('manifest_sha256')!=digest(root/'manifest.json'): raise Refused('Manifest seal mismatch')
    return manifest


class Checkpoint:
    def __init__(self,cfg,cloud,hosts=None,transport=None,cloud_factory=None):
        self.cfg=cfg; self.root=checkpoint_path(cfg); self._cloud=cloud; self.cloud_factory=cloud_factory
        self.hosts=hosts or Hosts(cfg); self.transport=transport
    @property
    def cloud(self):
        if self._cloud is None and self.cloud_factory is not None: self._cloud=self.cloud_factory()
        return self._cloud
    def continuation(self,operation,callback):
        # Retry only reconciled service starts / health; data event() stays strict.
        path=self.root/'controller-operations.json'; state=json.loads(path.read_text()) if path.exists() else {}
        prior=state.get(operation)
        if prior and prior.get('status') not in ('COMPLETE','FAILED'): raise Refused('Unknown/in-flight continuation: '+operation)
        history=(prior.get('attempts',[])+[{k:v for k,v in prior.items() if k!='attempts'}]) if prior else []
        state[operation]=dict(status='INTENT',time=time.time(),attempts=history); save(path,state)
        try:
            result=callback(); state[operation].update(status='COMPLETE',ended=time.time()); save(path,state); return result
        except Exception as exc:
            state[operation].update(status='FAILED',error=type(exc).__name__); save(path,state); raise
    def event(self,operation,callback):
        path=self.root/'controller-operations.json'; state=json.loads(path.read_text()) if path.exists() else {}
        if operation in state: raise Refused('Interrupted/already-entered operation: '+operation+'; inspect private journals, no implicit retry')
        state[operation]=dict(status='INTENT',time=time.time()); save(path,state)
        try:
            value=callback(); state[operation].update(status='COMPLETE',ended=time.time()); save(path,state); return value
        except Exception as exc:
            state[operation].update(status='FAILED',error=type(exc).__name__); save(path,state); raise
    def transport_for(self,catalog):
        cfg=dict(inventory=self.cfg['inventory'],namespace_hosts=[h for h,r in self.cfg['roles'].items() if r in ('network','compute')],
            transport_timeout=self.cfg['transport_timeout'],guest_user=self.cfg['guest_user'],guest_key=self.cfg['guest_key'],
            guest_known_hosts=self.cfg['guest_known_hosts'],host_key=self.cfg['host_key'],host_known_hosts=self.cfg['host_known_hosts'])
        return self.transport or Transport(cfg,catalog,cloud=self.cloud)
    def guest_health(self,manifest,initial=False):
        catalog=manifest['ew']; tr=self.transport_for(catalog); result={}
        probe=Path(__file__).parents[1]/'workloads/ew-workload-app/probe.py'
        for name,vm in catalog['servers'].items():
            access=tr.access(vm,'source'); profile=tr.profile(vm,access)
            verify_profile(profile,vm,mtu=vm['mtu'],mac=vm['mac'])
            # Guest-observed lease, metadata and actual routed ping; no allocation inference.
            peer=next(v['ip'] for v in catalog['servers'].values() if v['network']!=vm['network'])
            code="""import glob,json,pathlib,subprocess,urllib.request
ip=IP
leases=[pathlib.Path(p).read_text() for p in glob.glob('/run/systemd/netif/leases/*')]
assert any(('ADDRESS='+ip+'\\n') in s for s in leases),'Missing guest DHCP lease'
subprocess.run(['ping','-c','3','-W','2',PEER],check=True,stdout=subprocess.DEVNULL,timeout=15)
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open('http://169.254.169.254/openstack/latest/meta_data.json',timeout=5) as r:
 assert r.status==200 and json.load(r)['uuid'].lower()==VM_UUID
""".replace('IP',repr(vm['ip'])).replace('PEER',repr(peer)).replace('VM_UUID',repr(vm['server'].lower()))
            tr.run(tr.guest_argv(vm,access)+[shlex.join(['sudo','-n','python3','-c',code])])
            if name.startswith('ew-client-') and not initial:
                url='http://'+catalog['servers']['ew-app']['ip']+':'+str(catalog['configuration']['endpoints']['api']['port'])
                tr.run(tr.guest_argv(vm,access)+[shlex.join(['python3','-c',probe.read_text(),'--url',url,'--client-id',name])],timeout=60)
            result[name]=dict(status='PASS',boot=profile['boot'],mtu=vm['mtu'],dhcp='PASS',routed='PASS',metadata='PASS',
                              application='NOT TESTED (read-only plan)' if initial else 'PASS' if name.startswith('ew-client-') else 'CHECKED_VIA_CLIENTS')
        return result
    def plan(self):
        snapshot=cloud_snapshot(self.cloud); catalog=ew_catalog(snapshot,self.cfg)
        nodes={h:self.hosts.call(h,'discover') for h in self.cfg['roles']}
        for host,plan in nodes.items():
            expected={v['server'] for v in catalog['servers'].values() if v['actual_host']==plan['identity']['hostname'] or v['actual_host']==host}
            if set(plan['domains'])!=expected: raise Refused('Libvirt UUIDs disagree with scoped EW guests: '+host)
            if any(plan['domain_states'][sid]!='running' for sid in expected): raise Refused('EW Nova/libvirt running state differs')
            for vm in catalog['servers'].values():
                if vm['server'] in expected and plan.get('domain_interfaces',{}).get(vm['server'])!=[dict(port=vm['port'],mac=vm['mac'].lower())]:
                    raise Refused('Current libvirt/OVS port identity conflicts with source API catalog')
            if any(c['name'].startswith('ovn') for c in plan['containers']): raise Refused('Cold checkpoint source must be OVS, not OVN')
        if len({p['identity']['machine_id'] for p in nodes.values()})!=5: raise Refused('Inventory aliases duplicate a host identity')
        total=sum(n['sizes']['apparent_bytes'] for n in nodes.values())
        for host,plan in nodes.items(): space(plan,plan['free_bytes'],self.cfg['headroom_bytes'],total if self.cfg['roles'][host]=='control' else 0)
        manifest=dict(schema_version=1,id=self.cfg['id'],state='PLANNED',nodes=nodes,resources=snapshot,ew=catalog,artifacts={},
            inputs=dict(inventory=self.cfg['inventory'],venv=self.cfg['venv'],config='/etc/kolla',images='Exact recorded Docker image IDs must remain cached on their original hosts'),
            maintenance='Guest boots change. Never reuse migration/capture run directories; a new EW baseline is required.')
        manifest['original_guest_health']=self.guest_health(manifest,initial=True)
        return manifest
    def save_manifest(self,m): save(self.root/'manifest.json',m)
    def collect(self,m):
        for host,plan in m['nodes'].items():
            artifact=self.event('archive-'+host,lambda h=host,p=plan:self.hosts.call(h,'archive',plan=p))
            artifact['private_sha256']=m['artifacts'][host]['private_sha256']; m['artifacts'][host]=artifact; self.save_manifest(m)
            directory=self.root/'nodes'/host; directory.mkdir(parents=True,exist_ok=True,mode=0o700)
            for name in ('data.tar','containers.private.json','plan.json','operations.json','ephemeral-excluded.json'):
                self.hosts.fetch(host,name,directory/name)
            if digest(directory/'data.tar')!=artifact['sha256'] or digest(directory/'containers.private.json')!=artifact['private_sha256']: raise Refused('Central artifact copy corrupt')
            verify_archive(directory/'data.tar',plan['roots'])
    def shutdown(self,plans,resources,prefix):
        for host,plan in plans.items():
            ids=[sid for sid,r in resources.items() if r['host'] in (host,plan['identity']['hostname']) and r['status']=='ACTIVE']
            if ids: self.event(prefix+'-guests-'+host,lambda h=host,guests=ids:self.hosts.call(h,'shutdown',operation=prefix+'-shutdown',guests=guests))
        # Every guest shutdown completes before any writer is stopped.
        for host,plan in plans.items():
            self.event(prefix+'-quiesce-'+host,lambda h=host,p=plan:self.hosts.call(h,'quiesce',plan=p,operation=prefix+'-quiesce'))
    def services(self,m,prefix):
        for role in ('control','network','compute'):
            for host,plan in m['nodes'].items():
                if self.cfg['roles'][host]==role:
                    if prefix=='restore-complete':
                        self.continuation(prefix+'-start-'+host,lambda h=host,p=plan:self.hosts.call(h,'resume-start',plan=p))
                    else:
                        self.event(prefix+'-start-'+host,lambda h=host,p=plan:self.hosts.call(h,'start',plan=p,operation=prefix+'-start'))
    def healthy(self,m,prefix):
        deadline=time.monotonic()+self.cfg['health_timeout']
        while True:
            try:
                for host,plan in m['nodes'].items(): self.hosts.call(host,'health',plan=plan)
                snapshot=cloud_snapshot(self.cloud)
                agents=list(self.cloud.network.agents())
                for host,node in m['nodes'].items():
                    required={'Open vSwitch agent'} if node['role']=='compute' else {'Open vSwitch agent','DHCP agent','L3 agent','Metadata agent'} if node['role']=='network' else set()
                    live={a.agent_type for a in agents if a.host in (host,node['identity']['hostname']) and a.is_alive and a.is_admin_state_up}
                    if not required<=live: raise Refused('Original source Neutron agents are not healthy')
                for sid,original in m['resources']['servers'].items():
                    current=self.cloud.compute.get_server(sid)
                    if current.status=='SHUTOFF' and original['status']=='ACTIVE': self.cloud.compute.start_server(sid)
                    self.cloud.compute.wait_for_server(current,status=original['status'],failures=['ERROR'],interval=3,wait=max(1,int(deadline-time.monotonic())))
                snapshot=cloud_snapshot(self.cloud)
                for key in ('networks','subnets','routers','ports','images','flavors','security_groups','security_group_rules','users','projects','roles','domains'):
                    if snapshot[key]!=m['resources'][key]: raise Refused('Restored '+key+' UUIDs/attributes/MTUs differ')
                if set(snapshot['servers'])!=set(m['resources']['servers']): raise Refused('Restored server UUID set differs')
                for sid,old in m['resources']['servers'].items():
                    if snapshot['servers'][sid]['host']!=old['host']: raise Refused('Guest placement changed')
                result=self.guest_health(m)
                save(self.root/(prefix+'-health.json'),dict(status='PASS',guests=result,
                    changed_boots=[n for n,r in result.items() if r['boot']!=m['original_guest_health'][n]['boot']],fresh_baseline_required=True))
                return result
            except Exception:
                if time.monotonic()>=deadline: raise Refused('Bounded service/OVS/EW health recovery failed; resources and journals retained') from None
                time.sleep(3)
    def create(self):
        m=self.plan(); self.root.mkdir(parents=True,exist_ok=False,mode=0o700)
        save(self.root/'restore-inputs.json',self.cfg); self.save_manifest(m)
        for host,plan in m['nodes'].items():
            m['artifacts'][host]=self.event('init-'+host,lambda h=host,p=plan:self.hosts.call(h,'init',plan=p)); self.save_manifest(m)
        try:
            self.shutdown(m['nodes'],m['resources']['servers'],'backup')
            m['state']='QUIESCED'; self.save_manifest(m)
            self.collect(m)
            for host,plan in m['nodes'].items(): self.hosts.call(host,'verify',plan=plan,artifact=m['artifacts'][host])
        except Exception:
            m['state']='INCOMPLETE'; self.save_manifest(m)
            try:
                for host in m['nodes']:
                    operations=self.hosts.call(host,'operation-state')
                    if any(row.get('status')=='INTENT' for row in operations.values()): raise Refused('Remote operation may still be running; service recovery prohibited')
                self.services(m,'backup-recovery'); self.event('backup-recovery-health',lambda:self.healthy(m,'backup-recovery'))
            except Exception: save(self.root/'recovery-required.json',dict(status='OPERATOR_RECOVERY_REQUIRED',reason='Inspect controller/node journals; do not restore incomplete archives'))
            raise
        try:
            self.services(m,'backup-complete'); self.event('backup-complete-health',lambda:self.healthy(m,'backup-complete'))
        except Exception:
            m['state']='INCOMPLETE'; self.save_manifest(m)
            save(self.root/'recovery-required.json',dict(status='OPERATOR_RECOVERY_REQUIRED',reason='Source service/guest health failed; inspect journals'))
            raise
        m['state']='SEALED'; self.save_manifest(m); save(self.root/'seal.json',dict(manifest_sha256=digest(self.root/'manifest.json')))
        return dict(status='SEALED',checkpoint=str(self.root),fresh_baseline_required=True)
    def verify(self):
        m=sealed(self.root)
        if set(self.cfg['roles'])!=set(m['nodes']): raise Refused('Inventory hosts changed')
        for host,plan in m['nodes'].items():
            directory=self.root/'nodes'/host; artifact=m['artifacts'][host]
            if directory.resolve()!=directory or any((directory/name).is_symlink() for name in ('data.tar','containers.private.json')): raise Refused('Unsafe central archive/restore input path')
            if digest(directory/'data.tar')!=artifact['sha256'] or digest(directory/'containers.private.json')!=artifact['private_sha256']: raise Refused('Central checksum failure')
            verify_archive(directory/'data.tar',plan['roots'])
            self.hosts.call(host,'verify',plan=plan,artifact=artifact)
        return m
    def restore_plan(self):
        m=self.verify()
        current={h:self.hosts.call(h,'discover-recovery') for h in self.cfg['roles']}
        for host,node in current.items():
            if node['identity']!=m['nodes'][host]['identity']: raise Refused('Wrong host')
        offline=self.cfg.get('recovery_mode','api')=='offline'
        if offline:
            snapshot,extras=offline_scope(self.cfg,self.root,m,current)
        else:
            snapshot=cloud_snapshot(self.cloud)
            extras=restore_scope(snapshot,m['resources'],self.cfg['validation_runs'])
        for host,plan in current.items():
            if plan['identity']!=m['nodes'][host]['identity']: raise Refused('Wrong host')
            expected={sid for sid,r in snapshot['servers'].items() if r['host'] in (host,plan['identity']['hostname'])}
            if set(plan['domains'])!=expected: raise Refused('Unrelated/missing libvirt domains')
            for sid in expected:
                if not offline:
                    api_ports=sorted([dict(port=pid,mac=p['mac_address'].lower()) for pid,p in snapshot['ports'].items() if p.get('device_id')==sid],key=lambda p:p['port'])
                    if not api_ports or plan.get('domain_interfaces',{}).get(sid)!=api_ports: raise Refused('Current libvirt/OVS port identity conflicts with API scope')
                desired='running' if snapshot['servers'][sid]['status']=='ACTIVE' else 'shut off'
                if plan['domain_states'][sid]!=desired: raise Refused('Current Nova/libvirt power states differ; no force stop')
            # Current durable volume trees remain in quarantine/retained volumes.
            space(m['nodes'][host],plan['free_bytes'],self.cfg['headroom_bytes'])
        return dict(manifest=m,current=current,resources=snapshot,validation_owned=list(extras),status='RESTORE_PLANNED',reboot_required=True,
                    scope_verification='HOST_LIBVIRT_VERIFIED_API_SCOPE_OPERATOR_ATTESTED' if offline else 'LIVE_API_AND_HOST_VERIFIED')
    def restore_apply(self):
        plan=self.restore_plan()  # All hosts/images/archives/capacity BEFORE any stop.
        if (self.root/'restore-state.json').exists(): raise Refused('Restore already entered; inspect journals or use restore-finish after all data is restored and hosts rebooted')
        save(self.root/'restore-state.json',dict(status='INTENT',pre_restore_boots={h:p['boot'] for h,p in plan['current'].items()},
            recovery_mode=self.cfg.get('recovery_mode','api'),scope_verification=plan.get('scope_verification','LIVE_API_AND_HOST_VERIFIED')))
        save(self.root/'current-before-restore.json',plan['current'])
        self.shutdown(plan['current'],plan['resources']['servers'],'restore')
        for host,node in plan['manifest']['nodes'].items():
            self.event('restore-data-'+host,lambda h=host,p=node:self.hosts.call(h,'restore',plan=p))
        state=json.loads((self.root/'restore-state.json').read_text()); state['status']='DATA_RESTORED_REBOOT_REQUIRED'; save(self.root/'restore-state.json',state)
        return dict(status=state['status'],checkpoint=str(self.root),instruction='Reboot all five nodes under operator control, then run restore-finish. Do not start Kolla or guests before all nodes reboot. No restore SUCCESS yet.')
    def restore_finish(self):
        m=self.verify(); state=json.loads((self.root/'restore-state.json').read_text())
        if state['status']!='DATA_RESTORED_REBOOT_REQUIRED': raise Refused('Partial restore; finish prohibited')
        if set(state['pre_restore_boots'])!=set(m['nodes']) or len(m['nodes'])!=5: raise Refused('Every same host must reboot after coordinated restore; incomplete reboot barrier')
        for host,boot in state['pre_restore_boots'].items():
            observed=self.hosts.call(host,'identity')
            if observed['identity']!=m['nodes'][host]['identity'] or observed['boot']==boot: raise Refused('Every same host must reboot after coordinated restore')
        operations=json.loads((self.root/'controller-operations.json').read_text())
        if any(r.get('status') not in ('COMPLETE','FAILED') for r in operations.values()): raise Refused('Unknown/in-flight controller operation; finish prohibited')
        for host in m['nodes']:
            node_ops=self.hosts.call(host,'operation-state')
            if (operations.get('restore-data-'+host,{}).get('status')!='COMPLETE' or
                node_ops.get('restore',{}).get('status')!='COMPLETE' or
                any(r.get('status') not in ('COMPLETE','FAILED') for r in node_ops.values())):
                raise Refused('Partial/unknown/in-flight node restore; finish prohibited')
        self.services(m,'restore-complete'); self.continuation('restore-complete-health',lambda:self.healthy(m,'restore-complete'))
        state.update(status='RESTORED_HEALTHY',fresh_baseline_required=True); save(self.root/'restore-state.json',state)
        return state


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('plan','create','verify','restore-plan','restore-apply','restore-finish'))
    parser.add_argument('--config',required=True,help='JSON configuration on stdin: use -')
    parser.add_argument('--confirm',default='')
    args=parser.parse_args(); cfg=json.load(sys.stdin) if args.config=='-' else json.loads(Path(args.config).read_text())
    for key in ('headroom_bytes','guest_timeout','health_timeout','archive_timeout','transport_timeout'):
        if type(cfg.get(key)) is not int or cfg[key]<=0: raise Refused('Positive bounded setting required: '+key)
    cfg['roles']=inventory_roles(cfg)
    if args.action in ('create','restore-apply','restore-finish') and args.confirm!=cfg['id']: raise Refused('Explicit confirmation must equal checkpoint ID')
    version=checked([str(Path(cfg['venv'])/'bin/kolla-ansible'),'--version'])
    if not re.search(r'\b18\.8\.1\b',version): raise Refused('This cold checkpoint tool requires Kolla-Ansible 18.8.1')
    mode=cfg.get('recovery_mode','api')
    if mode not in ('api','offline') or (mode=='offline' and args.action in ('plan','create')): raise Refused('Offline mode is for recovery only; live source creation requires APIs')
    def connect():
        import openstack
        return openstack.connect(api_timeout=cfg['transport_timeout'])
    obj=Checkpoint(cfg,None,cloud_factory=connect)
    # Read-only plan/verification never creates checkpoint directories or locks.
    if args.action in ('plan','verify','restore-plan'):
        result=getattr(obj,args.action.replace('-','_'))()
        # No credentials or private container configs are included in this output.
        if args.action=='verify': result=dict(status='VERIFIED',checkpoint=str(obj.root))
        elif args.action=='restore-plan': result={k:result[k] for k in ('status','validation_owned','reboot_required','scope_verification')}
        elif args.action=='plan':
            result=dict(status='PLANNED',checkpoint=str(obj.root),nodes={h:dict(identity=p['identity'],roots=p['roots'],sizes=p['sizes'],free_bytes=p['free_bytes'],domains=p['domains'],images=sorted({c['image'] for c in p['containers']})) for h,p in result['nodes'].items()},
                        ew=result['ew'],guest_health=result['original_guest_health'],maintenance=result['maintenance'])
        print(json.dumps(result,indent=2)); return
    lock=Path(cfg['root']); lock.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (lock/'.lock').open('a') as handle:
        os.chmod(handle.name,0o600); fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        result=getattr(obj,args.action.replace('-','_'))(); print(json.dumps(result,indent=2))


if __name__=='__main__':
    try: main()
    except Exception as exc:
        print(json.dumps(dict(status='FAILED',error=type(exc).__name__,reason=str(exc) if isinstance(exc,Refused) else 'Operation failed; inspect private checkpoint journals. Never infer success.')))
        raise SystemExit(1)
