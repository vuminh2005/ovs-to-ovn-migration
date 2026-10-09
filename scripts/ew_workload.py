#!/usr/bin/env python3
"""Catalog-owned EW measurements, never cloud workload remediation."""
import argparse
import base64
import copy
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import pathlib
import signal
import subprocess
import sys
import time
import uuid

from dataplane_capture import save
from ew_transport import Transport, verify_profile
from ew_api_observer import process_identity
from ew_tcp_experiment import restoration_markers, target_ready

METRICS=pathlib.Path(__file__).parents[1]/'workloads/ew-workload-metrics'
spec=importlib.util.spec_from_file_location('ew_metrics_report',METRICS/'report.py')
metrics=importlib.util.module_from_spec(spec); spec.loader.exec_module(metrics)
ACTORS=('ew-client-a1','ew-client-a2','ew-client-b','ew-app')


def read(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def safe_error(exc):
    # SDK/auth exception bodies are never archived; our own messages contain
    # resource identities and prerequisite descriptions, no credential values.
    return str(exc) if type(exc).__module__ in ('builtins','json.decoder','subprocess') else type(exc).__name__


def stable(status, cfg, fence=0, samples=3):
    if status.get('status')!='RUNNING': return False
    for probe in cfg['probes']:
        if probe.get('source_boundary'): continue
        row=status.get('progress',{}).get(probe['name'],{})
        if (row.get('successes',0)<samples or row.get('seq',0)<=fence or
            len(row.get('success_sequences',[]))<samples or
            any(seq<=fence for seq in row['success_sequences'][-samples:]) or
            type(row.get('mono')) not in (int,float) or not math.isfinite(row['mono']) or
            not 0<=status.get('current_mono',-1)-row['mono']<=probe['interval']+5): return False
    return True


def observer_report(root, cfg, run_id=None, mode=None):
    try:
        state=read(root/'api-state.json'); rows=[json.loads(l) for l in (root/'api-events.jsonl').read_text().splitlines()]
        if (not state or state['status']!='COMPLETE' or len(rows)<3 or
            [r['seq'] for r in rows]!=list(range(1,state['samples']+1))): raise ValueError('Incomplete API observer')
        if run_id is not None and state.get('configuration')!=dict(cfg,run_id=run_id,mode=mode):
            raise ValueError('API observer run/configuration identity mismatch')
        limit=cfg['api_interval']+7
        monos=[r['mono'] for r in rows]
        if any(type(x) not in (int,float) or not math.isfinite(x) for x in [*monos,state['started'],state['ended']]): raise ValueError('Invalid API observer clock')
        if any(type(r.get('ok')) is not bool or r.get('observer_error') for r in rows): raise ValueError('API observer instrumentation failed')
        if (not 0<=rows[0]['mono']-state['started']<=limit or not 0<=state['ended']-rows[-1]['mono']<=limit or
            any(not 0<=b-a<=limit for a,b in zip(monos,monos[1:]))): raise ValueError('API observer coverage gap')
        windows=[]; start=None
        for row in rows:
            if not row['ok'] and start is None: start=row
            elif row['ok'] and start:
                windows.append(dict(start_utc=start['utc'],recovery_utc=row['utc'],
                                    observed_seconds=row['mono']-start['mono'],recovered=True)); start=None
        if start: windows.append(dict(start_utc=start['utc'],recovered=False,observed_seconds=None))
        return dict(status='PASS',coverage='PASS',recovery='PASS' if rows[-1]['ok'] else 'FAIL',
                    samples=len(rows),failed_samples=sum(not r['ok'] for r in rows),windows=windows,
                    timing_scope='bounded sampled Neutron read availability from one controller process; separate from orchestration freeze')
    except (OSError,TypeError,ValueError,KeyError) as exc: return dict(status='UNAVAILABLE',reason=str(exc))


def readiness_ready(session, catalog, phase):
    try:
        row=session['readiness'][phase]
        if (row['status']!='PASS' or set(row['guests'])!=set(catalog['servers']) or
            set(row['tasks'])!=set(ACTORS[:3]) or
            (phase=='recovery' and row.get('observation_status')!='PASS')): return False
        for name,vm in catalog['servers'].items():
            guest=row['guests'][name]; baseline=session['guests'][name]
            mtu=baseline['source_mtu'] if phase=='initial' else baseline['target_mtu']
            if (guest['server']!=vm['server'] or guest['port']!=vm['port'] or guest['fixed_ip']!=vm['ip'] or
                guest['actual_host']!=vm['actual_host'] or guest['network_mtu']!=mtu or
                guest['network_type']!=('geneve' if phase=='recovery' else 'vxlan')): return False
            verify_profile(guest['guest'],vm,baseline['boot'],mtu,baseline['mac'])
        return all(r['status']=='PASS' and r['result']['e2e'] is True and r['result']['dependencies'] is True
                   for r in row['tasks'].values())
    except (KeyError,TypeError,ValueError,RuntimeError,AttributeError): return False


def coverage_anchor_ready(session, key):
    value=session.get(key)
    return (isinstance(value,dict) and set(value)==set(ACTORS) and
            all(type(seq) is int and seq>0 for seq in value.values()))


def report_evidence(root):
    root=pathlib.Path(root)
    try:
        runtime=read(root/'runtime.json',{})
        if not (root/'ew-measurement-config.json').exists():
            if runtime.get('ew_measurement_schema_version')==1 and runtime.get('ew_workloads_enabled') is True:
                raise ValueError('Enabled EW measurement configuration is missing')
            return dict(status='NOT TESTED',enabled=False)
        cfg=read(root/'ew-measurement-config.json',{})
        if not isinstance(cfg,dict): raise ValueError('Malformed EW measurement configuration')
        if cfg.get('enabled') is False: return dict(status='NOT TESTED',enabled=False)
        if cfg.get('enabled') is not True: raise ValueError('Missing explicit EW enabled state')
        state=read(root/'ew-lifecycle.json',{}); catalog=read(root/'ew-resources.json',{})
        if not isinstance(catalog,dict) or not isinstance(state,dict): raise ValueError('Malformed EW catalog/lifecycle')
        sessions=state.get('sessions',{})
        if not isinstance(sessions,dict) or any(mode not in ('baseline','migration') or not isinstance(s,dict) for mode,s in sessions.items()):
            raise ValueError('Malformed EW lifecycle sessions')
    except (OSError,ValueError,AttributeError,TypeError) as exc:
        output=dict(status='UNAVAILABLE',enabled=True,reason=str(exc),sessions={})
        save(root/'ew-report.json',output); return output
    if not cfg.get('enabled'): return dict(status='NOT TESTED',enabled=False)
    try: action_errors=read(root/'ew-action-errors.json',[])
    except (OSError,ValueError): action_errors=[dict(reason='Unreadable controller action diagnostics')]
    output=dict(enabled=True,sessions={},action_errors=action_errors)
    for mode, session in sessions.items():
        base=root/'ew'/mode
        actors={name:metrics.assess(base/name,mode) for name in ACTORS}
        for name in ACTORS:
            try: matches=read(base/name/'config.json')==session.get('runners',{}).get(name)
            except (OSError,ValueError,TypeError,AttributeError): matches=False
            if not matches:
                actors[name]=dict(status='UNAVAILABLE',coverage='UNAVAILABLE',reason='Collected runner configuration differs from checkpoint')
        api=observer_report(base,cfg,session.get('run_id'),mode)
        readiness=session.get('readiness',{})
        if not isinstance(readiness,dict): readiness={}
        required=('initial',) if mode=='baseline' else ('initial','pre-freeze','recovery')
        ready=all(readiness_ready(session,catalog,k) for k in required)
        anchors=('initial_coverage',) if mode=='baseline' else ('initial_coverage','pre_freeze_coverage')
        ready=ready and all(coverage_anchor_ready(session,k) for k in anchors)
        api_samples=session.get('initial_api_samples',0)
        ready=ready and type(api_samples) is int and 3<=api_samples<=api.get('samples',0)
        for name in ACTORS:
            try:
                last=read(base/name/'summary.json')['last_event_seq']
                initial=session['initial_coverage'][name]
                ready=ready and 0<initial<=last
                if mode=='migration': ready=ready and initial<session['pre_freeze_coverage'][name]<=last
            except (OSError,ValueError,KeyError,TypeError): ready=False
        collection=session.get('collection',{})
        if not isinstance(collection,dict): collection={}
        complete=(session.get('status')=='COMPLETE' and api['status']=='PASS' and ready and
                  all(isinstance(collection.get(n),dict) and collection[n].get('status')=='COMPLETE' for n in ACTORS))
        api_ready=api.get('recovery')=='PASS' and (mode=='migration' or api.get('failed_samples')==0)
        statuses=[a['status'] for a in actors.values()]
        status='PASS' if complete and api_ready and all(s=='PASS' for s in statuses) else 'FAIL' if ('FAIL' in statuses or (complete and not api_ready)) else 'UNAVAILABLE'
        if any(isinstance(row,dict) and row.get('status')=='FAIL' for row in readiness.values()): status='FAIL'
        output['sessions'][mode]=dict(status=status,actors=actors,neutron_api_observer=api,readiness=readiness,
            lifecycle=session.get('status','UNAVAILABLE'),collection=collection,
            acceptance_scope=('zero required probe/HTTP/SLO failures and all tasks reconcile' if mode=='baseline' else
                'complete evidence, no unresolved/corrupt/reprocessed tasks, reconciliation and stable recovery; transient failures and SLO misses reported'),
            route_scope='same-compute routed OVS traffic can traverse centralized router on a network node')
    output['status']=output['sessions'].get('migration',{}).get('status','UNAVAILABLE')
    save(root/'ew-report.json',output)
    return output


class Workload:
    def __init__(self, root, cloud, transport=None, mode='migration'):
        self.root=pathlib.Path(root); self.cfg=read(self.root/'ew-measurement-config.json')
        if not self.cfg or self.cfg.get('enabled') is not True: raise RuntimeError('EW measurement is not configured/enabled')
        for key in ('transport_timeout','readiness_timeout','collection_timeout','maximum_lifetime','drain','interval','api_interval','recovery_seconds'):
            if type(self.cfg.get(key)) not in (int,float) or not math.isfinite(self.cfg[key]) or self.cfg[key]<=0:
                raise RuntimeError('Missing/invalid bounded EW setting: '+key)
        self.catalog=read(self.root/'ew-resources.json'); self.cloud=cloud; self.mode=mode
        if not self.catalog or self.catalog.get('ownership')!='external-existing-never-validation-owned':
            raise RuntimeError('Missing protected EW catalog')
        self.path=self.root/'ew-lifecycle.json'; self.state=read(self.path,dict(schema_version=1,sessions={}))
        if self.state.get('configuration',self.cfg)!=self.cfg: raise RuntimeError('EW measurement configuration changed; no silent rebase')
        self.state['configuration']=self.cfg
        inventory=self.state['sessions'].get(mode,{}).get('source_inventory') if cloud is None else None
        if cloud is None and transport is None and not inventory: raise RuntimeError('Missing checkpointed source SSH host identities; no frozen-API discovery')
        self.transport=transport or Transport(self.cfg,self.catalog,cloud=cloud,inventory=inventory)
        self.base=self.root/'ew'/mode; self.base.mkdir(parents=True,exist_ok=True,mode=0o700)

    def commit(self): save(self.path,self.state)

    def session(self): return self.state['sessions'][self.mode]

    def budget(self, seconds):
        self.deadline=time.monotonic()+seconds if seconds is not None else None
        self.transport.deadline=self.deadline

    def check_budget(self):
        if getattr(self,'deadline',None) is not None and time.monotonic()>=self.deadline:
            raise TimeoutError('Total EW readiness/collection deadline expired')

    def cloud_check(self, phase, names=None):
        journal=read(self.root/'network-mtu-plan.json',{}).get('networks',[])
        plan={r['network']:r for r in journal}
        calculation=read(self.root/'mtu-calculation.json'); result={}
        for name, vm in self.catalog['servers'].items():
            if names is not None and name not in names: continue
            self.check_budget()
            server=self.cloud.compute.get_server(vm['server']); port=self.cloud.network.get_port(vm['port'])
            network=self.cloud.network.get_network(vm['network'])
            fixed=lambda rows: sorted((f['subnet_id'],f['ip_address']) for f in rows)
            if (not server or not port or not network or server.id!=vm['server'] or port.id!=vm['port'] or
                server.status!='ACTIVE' or server.compute_host!=vm['actual_host'] or port.device_id!=vm['server'] or
                port.network_id!=vm['network'] or fixed(port.fixed_ips)!=fixed(vm['fixed_ips']) or port.status!='ACTIVE'):
                raise RuntimeError('EW cloud identity/placement/port state changed: '+name)
            if phase=='initial':
                if network.provider_network_type!='vxlan': raise RuntimeError('Initial EW network is not VXLAN')
                source=int(network.mtu); target=min(source-calculation['overhead_delta'],calculation['fresh_geneve_mtu'])
            else:
                if vm['network'] not in plan: raise RuntimeError('Missing per-network EW MTU journal')
                source=plan[vm['network']]['source_mtu']; target=plan[vm['network']]['target_mtu']
                if network.mtu!=target: raise RuntimeError('EW target network MTU is not ready')
                if network.provider_network_type!=('geneve' if phase=='recovery' else 'vxlan'):
                    raise RuntimeError('EW network type contradicts migration phase')
            baseline=self.state['sessions'].get(self.mode,{}).get('guests',{}).get(name)
            if baseline and (baseline['mac']!=port.mac_address or baseline['source_mtu']!=source or baseline['target_mtu']!=target):
                raise RuntimeError('EW port/MTU baseline changed; no silent rebase: '+name)
            result[name]=dict(server=vm['server'],port=vm['port'],fixed_ip=vm['ip'],actual_host=server.compute_host,
                              source_mtu=source,target_mtu=target,mac=port.mac_address,network=vm['network'],status='PASS',
                              network_type=network.provider_network_type,network_mtu=network.mtu)
        if len(result)!=(len(names) if names is not None else 6): raise RuntimeError('All requested EW guests are required')
        return result

    def guest_call(self,name,action,phase='source',**extra):
        self.check_budget()
        session=self.session(); vm=self.catalog['servers'][name]
        if getattr(self,'frozen_source',False):
            access=session['readiness']['pre-freeze']['guests'][name]['access']
            if ('direct' not in access and (access.get('namespace')!='qrouter-'+self.catalog['router'] or
                access.get('host') not in self.cfg['namespace_hosts'])): raise RuntimeError('Invalid checkpointed source namespace access')
        else: access=self.transport.access(vm,phase)
        profile=self.transport.profile(vm,access)
        verify_profile(profile,vm,session['guests'][name]['boot'],mac=session['guests'][name]['mac'])
        session.setdefault('transport',{})[name]=dict(access=access,last_collected_utc=profile['utc']); self.commit()
        return self.transport.operation(vm,access,dict(action=action,config=session['runners'].get(name,{}),**extra),
                                        extra.get('operation_timeout',self.cfg['transport_timeout']))

    def runner_config(self,name,session):
        guests=session['guests']; endpoints=self.catalog['configuration']['endpoints']
        source=self.catalog['servers'][name]; targets=['ew-queue','ew-db'] if name=='ew-app' else ['ew-app']; probes=[]
        for dest in targets:
            peer=self.catalog['servers'][dest]
            endpoint=endpoints['rabbitmq' if dest=='ew-queue' else 'postgresql' if dest=='ew-db' else 'api']
            if endpoint['host']!=peer['ip']: raise RuntimeError('EW endpoint contradicts catalog IP')
            target=min(guests[name]['target_mtu'],guests[dest]['target_mtu'])
            original=min(guests[name]['source_mtu'],guests[dest]['source_mtu'])
            placement=dict(source_host=source['actual_host'],destination_host=peer['actual_host'],
                           same_subnet=source['network']==peer['network'],same_compute=source['actual_host']==peer['actual_host'])
            for kind,suffix,payload,boundary in (('ping','small',56,False),('df','target-df',target-28,False),
                                               ('df','source-boundary-df',original-28,True),('tcp','tcp',56,False)):
                probes.append(dict(name=name+'->'+dest+'.'+suffix,type=kind,host=peer['ip'],port=endpoint['port'],
                    source_ip=source['ip'],source_port=source['port'],destination_port=peer['port'],payload=payload,
                    source_boundary=boundary,interval=self.cfg['interval'],placement=placement))
        api=endpoints['api']; url=f"http://{api['host']}:{api['port']}"
        for kind in ('live','dependency'):
            probes.append(dict(name=name+'.'+kind,type=kind,url=url,host=api['host'],port=api['port'],interval=self.cfg['interval']))
        tcp={}
        if self.mode=='migration' and self.cfg.get('tcp_experiment_enabled') and name in ('ew-app','ew-client-b'):
            ports=self.cfg['tcp_ports']
            if len(ports)!=2 or len(set(ports))!=2 or any(type(p) is not int or not 1024<p<=65535 or p in {e['port'] for e in endpoints.values()} for p in ports):
                raise RuntimeError('Dedicated TCP ports must be distinct non-application ports')
            tcp=dict(ports=ports,server_ip=self.catalog['servers']['ew-app']['ip'],server_boot=guests['ew-app']['boot'])
        return dict(run_id=session['run_id'],database_run_id=session['run_id']+'-'+name,client_id=name,mode=self.mode,
            output='/var/lib/ew-load/'+session['run_id'],boot=guests[name]['boot'],server=source['server'],port=source['port'],ip=source['ip'],
            placement={n:self.catalog['servers'][n]['actual_host'] for n in (name,*targets)},
            duration=self.cfg['baseline_seconds'] if self.mode=='baseline' else 0,
            maximum_lifetime=self.cfg['maximum_lifetime'],drain=self.cfg['drain'],max_rate=self.cfg['max_rate'],slo=self.cfg['slo'],
            interval=self.cfg['interval'],stable_samples=self.cfg['stable_samples'],tasks=name!='ew-app',url=url,probes=probes,
            runner_sha256=hashlib.sha256((METRICS/'runner.py').read_bytes()).hexdigest(),tcp_experiment=tcp)

    def tcp_ready(self,statuses,second=False):
        if not self.session()['runners']['ew-app'].get('tcp_experiment'): return True
        port=self.cfg['tcp_ports'][int(second)]; server=statuses['ew-app']; client=statuses['ew-client-b']
        bound=server.get('tcp_listeners',{}).get(str(port),{})
        row=client.get('tcp_progress',{}).get(str(port),{})
        return (server.get('status')=='RUNNING' and client.get('status')=='RUNNING' and
            bound.get('run_id')==self.session()['run_id'] and bound.get('boot')==self.session()['guests']['ew-app']['boot'] and
            row.get('run_id')==self.session()['run_id'] and row.get('boot')==self.session()['guests']['ew-client-b']['boot'] and
            row.get('successes',0)>=self.cfg['stable_samples'] and
            type(row.get('mono')) in (int,float) and math.isfinite(row['mono']) and
            0<=client.get('current_mono',-1)-row['mono']<=self.cfg['interval']+5)

    def activate_tcp(self):
        """Frozen API: only checkpointed source SSH access; no SDK requests."""
        if not self.cfg.get('tcp_experiment_enabled'): return dict(status='NOT TESTED')
        if (self.mode!='migration' or not (self.root/'metrics/control_plane_downtime.start').exists() or
            any((self.root/'metrics'/p).exists() for p in ('db_migration.start','control_plane_downtime.end'))):
            raise RuntimeError('TCP second listener must activate after freeze and before DB migration')
        session=self.session()
        if not readiness_ready(session,self.catalog,'pre-freeze'):
            raise RuntimeError('Missing checkpointed pre-freeze EW readiness')
        self.frozen_source=True
        self.budget(self.cfg['transport_timeout']+self.cfg['stable_samples']*(self.cfg['interval']+2))
        if 'tcp_activation' not in session:
            client=self.guest_call('ew-client-b','status','source')
            server=self.guest_call('ew-app','status','source')
            session['tcp_activation']=dict(status='REQUESTED',client_fence=client['last_event_seq'],
                server_fence=server['last_event_seq'],
                server=self.catalog['servers']['ew-app']['server'],port=self.catalog['servers']['ew-app']['port'])
            self.commit()
        evidence=session['tcp_activation']
        try:
            self.guest_call('ew-app','tcp-activate','source')
            while time.monotonic()<self.deadline:
                statuses={n:self.guest_call(n,'status','source') for n in ('ew-app','ew-client-b')}
                port=self.cfg['tcp_ports'][1]
                row=statuses['ew-client-b'].get('tcp_progress',{}).get(str(port),{})
                fresh=row.get('success_sequences',[])[-self.cfg['stable_samples']:]
                bound=statuses['ew-app'].get('tcp_listeners',{}).get(str(port),{})
                if (self.tcp_ready(statuses,True) and len(fresh)==self.cfg['stable_samples'] and
                    all(seq>evidence['client_fence'] for seq in fresh) and bound.get('seq',0)>evidence['server_fence']):
                    evidence.update(status='PASS',binding=statuses['ew-app']['tcp_listeners'][str(port)],client=row,verified_at_epoch=time.time())
                    self.commit(); return evidence
                time.sleep(.2)
            raise TimeoutError('TCP listener bind/validated new-port echo was not observed after freeze')
        except Exception as exc:
            evidence.update(status='FAIL',reason=safe_error(exc)); self.commit(); raise

    def readiness(self,phase):
        session=self.session(); key=phase
        evidence=dict(status='CHECKING',attempt=str(uuid.uuid4()),guests={},tasks={})
        if key in session.get('readiness',{}):
            session.setdefault('readiness_attempts',[]).append(copy.deepcopy(session['readiness'][key]))
        session.setdefault('readiness',{})[key]=evidence; self.commit()
        try:
            cloud=self.cloud_check(phase)
            for name, vm in self.catalog['servers'].items():
                access=self.transport.access(vm,'ovn' if phase=='recovery' else 'source')
                actual=self.transport.profile(vm,access)
                expected=cloud[name]['source_mtu'] if phase=='initial' else cloud[name]['target_mtu']
                verify_profile(actual,vm,session['guests'][name]['boot'],expected,cloud[name]['mac'])
                evidence['guests'][name]=dict(cloud[name],boot=actual['boot'],guest=actual,access=access)
                self.commit()
            for name in ACTORS[:3]:
                task=dict(task_id=str(uuid.uuid4()),run_id='ready-'+evidence['attempt'][:12]+'-'+name,
                          client_id=name,payload='EW readiness payload')
                evidence['tasks'][name]=dict(task=task,status='INTENT'); self.commit()
                result=self.guest_call(name,'check','ovn' if phase=='recovery' else 'source',task=task,
                    url=session['runners'][name]['url'],timeout=min(30,self.cfg['readiness_timeout']),
                    operation_timeout=self.cfg['transport_timeout']+35)
                evidence['tasks'][name].update(result=result,status='PASS' if result.get('e2e') and result.get('dependencies') else 'FAIL')
                self.commit()
                if evidence['tasks'][name]['status']!='PASS': raise RuntimeError('Real EW end-to-end readiness failed: '+name)
            evidence['status']='PASS'
            if phase!='recovery' and isinstance(self.transport,Transport): session['source_inventory']=self.transport.checkpoint_inventory()
            self.commit(); return evidence
        except Exception as exc:
            evidence.update(status='FAIL',reason=safe_error(exc)); self.commit(); raise

    def observer_start(self):
        path=self.base/'api-state.json'; intent=self.base/'api-start-intent.json'
        previous=read(path)
        if previous:
            try:
                if (previous['status']=='RUNNING' and previous.get('configuration')==self.observer_config()
                    and previous['boot']==pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
                    and process_identity(previous['pid'])==previous['process_identity']): return
            except (OSError,KeyError): pass
            raise RuntimeError('API observer crashed/completed; do not replace measurement evidence')
        if intent.exists(): raise RuntimeError('API observer start is ambiguous; no duplicate observer')
        save(intent,dict(status='START_REQUESTED',run_id=self.session()['run_id'],mode=self.mode))
        save(self.base/'observer-config.json',self.observer_config())
        with (self.base/'observer.log').open('a') as log:
            subprocess.Popen([sys.executable,str(pathlib.Path(__file__).with_name('ew_api_observer.py')),'observe',str(self.base)],
                             stdout=log,stderr=log,stdin=subprocess.DEVNULL,start_new_session=True)

    def observer_config(self): return dict(self.cfg,run_id=self.session()['run_id'],mode=self.mode)

    def start(self):
        self.budget(self.cfg['readiness_timeout'])
        if self.mode not in self.state['sessions']:
            cloud=self.cloud_check('initial'); guests={}
            for name,vm in self.catalog['servers'].items():
                access=self.transport.access(vm,'source'); actual=self.transport.profile(vm,access)
                verify_profile(actual,vm,mtu=cloud[name]['source_mtu'],mac=cloud[name]['mac'])
                guests[name]=dict(cloud[name],boot=actual['boot'])
            session=dict(run_id='ew-'+uuid.uuid4().hex[:16],status='STARTING',guests=guests,runners={},launch={},readiness={})
            self.state['sessions'][self.mode]=session
            session['runners']={n:self.runner_config(n,session) for n in ACTORS}; self.commit()
        session=self.session()
        if session['status']=='COMPLETE': self.budget(None); return session
        # Resolve every existing launch intent before creating any new runner.
        # An ambiguous earlier actor must not change load on healthy siblings.
        for name in self.catalog['servers']:
            vm=self.catalog['servers'][name]; access=self.transport.access(vm,'source'); actual=self.transport.profile(vm,access)
            verify_profile(actual,vm,session['guests'][name]['boot'],mac=session['guests'][name]['mac'])
            if name in session['launch']:
                status=self.guest_call(name,'status')
                if status.get('status') not in ('RUNNING','START_REQUESTED'):
                    raise RuntimeError('Partial startup is ambiguous/crashed; guest is not automatically relaunched: '+name)
        for name in self.catalog['servers']:
            vm=self.catalog['servers'][name]; access=self.transport.access(vm,'source')
            if name in session['launch']:
                continue
            self.transport.install(vm,access)
            if name in ACTORS:
                session['launch'][name]=dict(status='START_REQUESTED'); self.commit()
                result=self.guest_call(name,'start')
                session['launch'][name]['status']=result['status']; self.commit()
        self.readiness('initial'); self.observer_start()
        statuses=self.coverage('source',tcp_initial=True)
        session.setdefault('initial_coverage',{n:s['last_event_seq'] for n,s in statuses.items()})
        session.setdefault('initial_api_samples',len((self.base/'api-events.jsonl').read_text().splitlines()))
        session['status']='RUNNING'; self.commit(); self.budget(None); return session

    def coverage(self,phase,fences=None,minimum_seconds=0,tcp_initial=False):
        deadline=min(time.monotonic()+self.cfg['readiness_timeout'],getattr(self,'deadline',None) or math.inf); started=time.monotonic()
        while time.monotonic()<deadline:
            statuses={n:self.guest_call(n,'status',phase) for n in ACTORS}
            if any(s.get('status') in ('CRASHED_OR_AMBIGUOUS','COMPLETE','ABSENT') for s in statuses.values()):
                raise RuntimeError('EW runner lost continuity; do not restart or infer successful samples')
            api=read(self.base/'api-state.json',{})
            api_rows=(self.base/'api-events.jsonl').read_text().splitlines() if (self.base/'api-events.jsonl').exists() else []
            api_alive=False
            try: api_alive=(api.get('status')=='RUNNING' and api.get('configuration')==self.observer_config()
                            and api.get('boot')==pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
                            and process_identity(api['pid'])==api['process_identity'])
            except (OSError,KeyError): pass
            api_fresh=False
            if len(api_rows)>=3:
                try:
                    latest=[json.loads(line) for line in api_rows[-3:]]
                    api_fresh=(all(r.get('ok') is True and not r.get('observer_error') for r in latest)
                        and type(latest[-1]['mono']) in (int,float) and math.isfinite(latest[-1]['mono'])
                        and 0<=time.monotonic()-latest[-1]['mono']<=self.cfg['api_interval']+7)
                except (ValueError,KeyError,TypeError): pass
            if api_alive and api_fresh and (not tcp_initial or self.tcp_ready(statuses)) and all(stable(statuses[n],self.session()['runners'][n],(fences or {}).get(n,0),self.cfg['stable_samples']) for n in ACTORS):
                if time.monotonic()-started>=minimum_seconds: return statuses
            time.sleep(min(1,max(0,deadline-time.monotonic())))
        raise TimeoutError('Bounded EW coverage/recovery gate failed')

    def ready(self):
        self.budget(self.cfg['readiness_timeout'])
        if self.session()['status']!='RUNNING': raise RuntimeError('Pre-freeze EW measurements are not running')
        self.readiness('pre-freeze'); statuses=self.coverage('source',tcp_initial=True)
        self.session()['pre_freeze_coverage']={n:s['last_event_seq'] for n,s in statuses.items()}; self.commit()

    def recover(self):
        self.budget(self.cfg['readiness_timeout'])
        session=self.session()
        # Independent TCP evidence must survive application readiness failure.
        self.tcp_recovery_evidence()
        if 'recovery_fences' not in session:
            session['recovery_fences']={n:self.guest_call(n,'status','ovn')['last_event_seq'] for n in ACTORS}; self.commit()
        try:
            self.readiness('recovery')
            self.coverage('ovn',session['recovery_fences'],self.cfg['recovery_seconds'])
            session['readiness']['recovery']['observation_status']='PASS'; self.commit()
        except Exception as exc:
            session.setdefault('readiness',{}).setdefault('recovery',{}).update(status='FAIL',reason=safe_error(exc)); self.commit(); raise

    def tcp_recovery_evidence(self):
        if self.mode!='migration' or not self.cfg.get('tcp_experiment_enabled'): return
        session=self.session()
        if session.get('tcp_recovery',{}).get('status')=='PASS':
            return target_ready(self.root,session,self.catalog['servers'])
        evidence=dict(status='CHECKING',run_id=session['run_id'],guests={})
        session['tcp_recovery']=evidence; self.commit()
        try:
            evidence['controller_markers']=restoration_markers(self.root)
            cloud=self.cloud_check('recovery',names=('ew-app','ew-client-b'))
            for name in ('ew-app','ew-client-b'):
                vm=self.catalog['servers'][name]; baseline=session['guests'][name]
                access=self.transport.access(vm,'ovn'); actual=self.transport.profile(vm,access)
                verify_profile(actual,vm,baseline['boot'],cloud[name]['target_mtu'],cloud[name]['mac'])
                evidence['guests'][name]=dict(cloud[name],boot=actual['boot'],guest=actual,access=access)
                self.commit()
            status=self.guest_call('ew-client-b','status','ovn')
            if status.get('status')!='RUNNING': raise RuntimeError('TCP client runner is not continuous after OVN restoration')
            evidence.update(client_fence=status['last_event_seq'],established_at_epoch=time.time(),status='PASS')
            target_ready(self.root,session,self.catalog['servers']); self.commit()
            return evidence
        except Exception as exc:
            evidence.update(status='FAIL',reason=safe_error(exc)); self.commit(); raise

    def observer_stop(self):
        state=read(self.base/'api-state.json')
        if not state: raise RuntimeError('No API observer state')
        if state['status']=='COMPLETE': return
        if (state['boot']!=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip() or
            process_identity(state['pid'])!=state['process_identity']): raise RuntimeError('API observer process identity changed')
        os.kill(state['pid'],signal.SIGTERM)
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            if read(self.base/'api-state.json',{}).get('status')=='COMPLETE': return
            time.sleep(.2)
        raise TimeoutError('API observer stop incomplete')

    def finish(self,phase='ovn'):
        if self.session()['status']=='COMPLETE':
            report_evidence(self.root); return self.session()  # immutable complete evidence; no duplicate stop/collection
        self.budget(self.cfg['collection_timeout'])
        session=self.session(); session['status']='STOPPING'; self.commit()
        deadline=time.monotonic()+self.cfg['collection_timeout']; errors=[]
        remaining=lambda:max(1,min(self.cfg['transport_timeout'],deadline-time.monotonic()))
        for name in ACTORS:
            try: self.guest_call(name,'stop',phase,operation_timeout=remaining())
            except Exception as exc: errors.append(dict(actor=name,operation='stop',reason=safe_error(exc)))
        session.setdefault('collection',{})
        for name in ACTORS:
            actor=self.base/name; actor.mkdir(exist_ok=True,mode=0o700)
            try:
                if time.monotonic()>=deadline: raise TimeoutError('Total collection bound exceeded')
                self.guest_call(name,'drain',phase,timeout=min(self.cfg['drain']+15,deadline-time.monotonic()),
                                operation_timeout=min(self.cfg['drain']+20,deadline-time.monotonic()))
            except Exception as exc: errors.append(dict(actor=name,operation='drain',reason=safe_error(exc)))
            try:
                result=self.guest_call(name,'collect',phase,maximum_bytes=self.cfg['maximum_collection_bytes'],operation_timeout=remaining())
                for filename,value in result['files'].items():
                    if filename not in ('events.jsonl','summary.json','state.json','config.json'): raise RuntimeError('Unsafe collection filename')
                    raw=base64.b64decode(value['base64'],validate=True)
                    if hashlib.sha256(raw).hexdigest()!=value['sha256']: raise RuntimeError('Collection hash mismatch')
                    path=actor/filename
                    if path.exists() and filename=='events.jsonl' and not raw.startswith(path.read_bytes()):
                        raise RuntimeError('Raw event history was replaced/truncated')
                    temporary=path.with_suffix(path.suffix+'.tmp')
                    temporary.write_bytes(raw); temporary.chmod(0o600); temporary.replace(path)
                diagnostics=self.guest_call(name,'diagnostics',phase,timeout=min(10,remaining()),
                    maximum_bytes=self.cfg['maximum_collection_bytes'],operation_timeout=remaining())
                save(actor/'diagnostics.json',diagnostics)
                session['collection'][name]=dict(status=result['status'] if diagnostics['status']=='COMPLETE' else 'INCOMPLETE')
                if session['collection'][name]['status']!='COMPLETE':
                    errors.append(dict(actor=name,operation='collect',reason='Incomplete guest summary or run-scoped diagnostics'))
            except Exception as exc:
                session['collection'][name]=dict(status='INCOMPLETE',reason=safe_error(exc)); errors.append(dict(actor=name,operation='collect',reason=safe_error(exc)))
            self.commit()
        try: self.observer_stop()
        except Exception as exc: errors.append(dict(actor='controller',operation='observer-stop',reason=safe_error(exc)))
        session.update(status='INCOMPLETE' if errors else 'COMPLETE',collection_errors=errors); self.commit()
        report_evidence(self.root)
        return session


def run_command():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('start','ready','recovery','status','stop','drain','collect','report','baseline','tcp-activate'))
    p.add_argument('root',type=pathlib.Path); p.add_argument('--mode',choices=('baseline','migration'),default='migration')
    p.add_argument('--transport-phase',choices=('source','ovn'))
    args=p.parse_args(); args.root.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (args.root/'ew-controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if args.action=='report': print(json.dumps(report_evidence(args.root))); return
        if args.action=='tcp-activate':
            print(json.dumps(Workload(args.root,None).activate_tcp())); return
        import openstack
        obj=Workload(args.root,openstack.connect(api_timeout=3),mode='baseline' if args.action=='baseline' else args.mode)
        phase=args.transport_phase or ('source' if obj.mode=='baseline' or not (args.root/'metrics/control_plane_downtime.end').exists() else 'ovn')
        if args.action=='start': result=obj.start()
        elif args.action=='ready': obj.ready(); result=obj.session()
        elif args.action=='recovery': obj.recover(); result=obj.session()
        elif args.action=='status': result=dict(actors={n:obj.guest_call(n,'status',phase) for n in ACTORS},
                                               neutron_observer=read(obj.base/'api-state.json'))
        elif args.action=='stop': result={n:obj.guest_call(n,'stop',phase) for n in ACTORS}
        elif args.action=='drain': result={n:obj.guest_call(n,'drain',phase,timeout=obj.cfg['drain']+15,operation_timeout=obj.cfg['drain']+20) for n in ACTORS}
        elif args.action=='collect': result=obj.finish(phase)
        else:
            obj.start(); deadline=time.monotonic()+obj.cfg['baseline_seconds']+obj.cfg['drain']+30
            while time.monotonic()<deadline:
                if all(obj.guest_call(n,'status')['status']=='COMPLETE' for n in ACTORS): break
                time.sleep(1)
            result=obj.finish('source')
        print(json.dumps(result))
        if args.action in ('collect','baseline'):
            status=report_evidence(args.root)['sessions'].get(obj.mode,{}).get('status','UNAVAILABLE')
            if status!='PASS': raise SystemExit(1)


def main():
    try: run_command()
    except Exception as exc:
        if len(sys.argv)>=3 and pathlib.Path(sys.argv[2]).is_dir():
            from datetime import datetime,timezone
            root=pathlib.Path(sys.argv[2]); path=root/'ew-action-errors.json'
            rows=read(path,[]); rows.append(dict(action=sys.argv[1],utc=datetime.now(timezone.utc).isoformat(),
                error_type=type(exc).__name__,reason=safe_error(exc)))
            save(path,rows)
        raise


if __name__=='__main__': main()
