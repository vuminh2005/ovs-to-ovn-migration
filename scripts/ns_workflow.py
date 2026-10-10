#!/usr/bin/env python3
"""Opt-in N-S orchestration: exact existing resources, independent owned observers."""
import argparse
import base64
import copy
import fcntl
import hashlib
import json
import pathlib
import shlex
import subprocess
import sys
import time
import yaml

from dataplane_capture import save
from ew_transport import Transport, verify_profile, ssh_options
from ew_tcp_experiment import restoration_markers
from ns_scope import require, validate_config, snapshot, preserved
from ns_measurement import read, report
from ns_ovn import verify, ConvergencePending

SCRIPTS=pathlib.Path(__file__).resolve().parent


def remote_code(modules, expression):
    code='import base64,json,sys,types\n'
    for name in modules:
        source=base64.b64encode((SCRIPTS/(name+'.py')).read_bytes()).decode()
        code+=f"m=types.ModuleType({name!r}); sys.modules[{name!r}]=m; exec(base64.b64decode({source!r}),m.__dict__)\n"
    return code+expression


def check_kolla(cfg, values):
    # Kolla 18.8 setup-ovs generates physnet<N> from ordered bridge names.
    # Initial bounded topology uses one explicit external bridge on network hosts.
    g=next(iter(cfg['gateways'].values()))
    require(all(x['bridge']==g['bridge'] and x['uplink']==g['uplink'] for x in cfg['gateways'].values()), 'initial scope needs consistent network-host Kolla bridge/uplink settings')
    require(values.get('neutron_bridge_name')==g['bridge'] and values.get('neutron_external_interface')==g['uplink'], 'explicit source Kolla bridge/uplink settings required; resolve/review defaults before migration')
    require(cfg['physnet']=='physnet1', 'installed Kolla single-bridge setup generates physnet1; custom mappings need separate review')
    require(not values.get('enable_neutron_provider_networks',False) in (True,'yes','true'), 'provider networking on computes remains unsupported')


class Workflow:
    def __init__(self, root, cloud=None):
        self.root=pathlib.Path(root); self.cfg=read(root,'ns-config.json'); self.cloud=cloud
        require(self.cfg, 'enabled run is missing immutable N-S configuration')
        self.before=read(root,'ns-before.json')
        self.transport=Transport(self.cfg['transport'],dict(router=self.cfg['router']),cloud=cloud)
        signature=hashlib.sha256(json.dumps(self.cfg,sort_keys=True).encode()).hexdigest()
        self.life=read(root,'ns-lifecycle.json',dict(schema_version=1,config_sha256=signature,observers={}))
        require(self.life.get('config_sha256')==signature, 'N-S configuration changed across resume')

    def commit(self): save(self.root/'ns-lifecycle.json',self.life)

    def hosts(self, target=False, evidence_path=None):
        hosts={}
        for host in self.cfg['hosts']:
            code=remote_code(['ovn_workload_evidence','ns_scope','ns_host'], 'print(json.dumps(sys.modules["ns_host"].read()))')
            evidence=json.loads(self.transport.host(host,['python3','-c',code]))
            if evidence_path:
                hosts[host]=evidence; save(evidence_path,hosts)  # retain rejected/partial raw host reads too
            # Source mapping file read happens on the host, never inferred from globals.
            if not target and host in self.cfg['gateways']:
                g=self.cfg['gateways'][host]
                check='import configparser; p=configparser.ConfigParser(); p.read("/etc/kolla/neutron-openvswitch-agent/openvswitch_agent.ini"); print(p.get("ovs","bridge_mappings",fallback=""))'
                mapping=self.transport.host(host,['python3','-c',check]).strip().replace(' ','')
                require(mapping==self.cfg['physnet']+':'+g['bridge'], 'source OVS bridge mapping differs on '+host)
            from ns_host import verify_path, cleanup_plan
            # Local verification must not read controller OVS-agent files.
            if target or host not in self.cfg['gateways']:
                verify_path(self.cfg,host,evidence,target)
            else:
                synthetic=copy.deepcopy(evidence); synthetic['external_ids'].update({'ovn-bridge-mappings':self.cfg['physnet']+':'+self.cfg['gateways'][host]['bridge'],'ovn-cms-options':'enable-chassis-as-gw'})
                verify_path(self.cfg,host,synthetic,True)
            if target:
                source=read(self.root,'ns-host-before.json',{}).get(host)
                require(source and source['external_ids'].get('system-id')==evidence['external_ids'].get('system-id'), 'host/chassis system-id changed during migration')
                if host in self.cfg['gateways']:
                    uplink=self.cfg['gateways'][host]['uplink']
                    require([p for p in source['ports'] if p['name']==uplink]==[p for p in evidence['ports'] if p['name']==uplink], 'physical uplink OVS identity changed during takeover')
            if not target: evidence['cleanup']=cleanup_plan(self.cfg,self.before,evidence)
            hosts[host]=evidence
        return hosts

    def api_snapshot(self, target=False):
        actual=snapshot(self.cloud,self.cfg,target)
        preserved(self.before,actual)
        return actual

    def guest(self, direction):
        vm=copy.deepcopy(self.cfg[direction]['guest']); vm['fixed_ips']=[dict(subnet_id=vm['subnet'],ip_address=vm['ip'])]
        return vm

    def profile(self, direction, phase):
        if direction=='egress':
            vm=self.guest(direction); access=self.transport.access(vm,'source' if phase=='prepared' else phase)
            profile=self.transport.profile(vm,access)
            entry=self.life['observers'].get(direction)
            mtu=self.cloud.network.get_network(vm['network']).mtu if phase=='source' and self.cloud is not None else None
            if phase=='ovn' or phase=='prepared':
                journal=read(self.root,'network-mtu-plan.json',{}).get('networks',[])
                row=[r for r in journal if r['network']==vm['network']]
                require(len(row)==1, 'NS guest network missing original/target MTU journal')
                mtu=row[0]['target_mtu']
            verify_profile(profile,vm,entry['config']['boot'] if entry else None,mtu,vm['mac'])
            return profile,access
        observer=self.cfg['ingress']['observer']
        command='import json,pathlib,subprocess; print(json.dumps(dict(product_uuid=pathlib.Path("/sys/class/dmi/id/product_uuid").read_text().strip().lower(),boot=pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip(),links=json.loads(subprocess.check_output(["ip","-j","address"],text=True)))))'
        result=json.loads(self.external(['python3','-c',command]))
        require(result['product_uuid']==observer['product_uuid'] and result['boot']==observer['boot'], 'external observer identity/boot changed')
        require(any(any(a.get('local')==observer['ip'] for a in l.get('addr_info',[])) for l in result['links']), 'external observer source IP missing')
        return result,None

    def external(self, argv, data=None):
        observer=self.cfg['ingress']['observer']
        require(all(c.isalnum() or c in '._:-' for c in observer['address']) and all(c.isalnum() or c in '_-' for c in observer['user']), 'unsafe external SSH endpoint')
        command=['ssh',*ssh_options(observer['key'],observer['known_hosts'],self.cfg['transport']['transport_timeout']),observer['user']+'@'+observer['address']]
        return self.transport.run(command+[shlex.join(([] if observer['user']=='root' else ['sudo','-n'])+argv)],data)

    def call(self, direction, action, phase='source'):
        entry=self.life['observers'][direction]
        source=(SCRIPTS/'ns_probe.py').read_bytes()
        require(hashlib.sha256(source).hexdigest()==entry['config']['helper_sha256'], 'observer helper changed across resume')
        code=remote_code(['ns_probe','ns_probe_agent'],'print(json.dumps(sys.modules["ns_probe_agent"].operation(json.load(sys.stdin))))')
        payload=json.dumps(dict(action=action,config=entry['config'],source=base64.b64encode(source).decode()))
        if direction=='egress':
            vm=self.guest(direction); access=entry['access'] if phase=='source' else self.transport.access(vm,'ovn')
            return json.loads(self.transport.run(self.transport.guest_argv(vm,access)+[shlex.join(['sudo','-n','python3','-c',code])],payload))
        return json.loads(self.external(['python3','-c',code],payload))

    def stable(self, raw, fence=0):
        rows=raw['rows'][-self.cfg['stable_samples']:]
        return (raw['state']['status']=='RUNNING' and len(rows)==self.cfg['stable_samples'] and
                all(r['seq']>fence and r['http_success'] and (not raw['state']['config']['probe'].get('session_port') or (r.get('session') or {}).get('success') is True) for r in rows) and
                raw['state']['last_mono']-rows[-1]['end_mono']==0 and
                0<=raw['current_mono']-rows[-1]['end_mono']<=self.cfg['interval']+2*self.cfg['timeout']+2)

    def start(self):
        actual=self.api_snapshot()
        # Refresh only legacy implementation artifacts after Pair A/B creation.
        # Original operator network MTUs/identities remain the authoritative baseline.
        self.before.update(legacy_ports=actual['legacy_ports'],legacy_namespaces=actual['legacy_namespaces'])
        save(self.root/'ns-before.json',self.before)
        fresh=self.hosts(); old=read(self.root,'ns-host-before.json')
        if old:
            require({h:v['cleanup'] for h,v in old.items()}=={h:v['cleanup'] for h,v in fresh.items()}, 'legacy source attachments changed on resume')
        else: save(self.root/'ns-host-before.json',fresh)
        require(not (self.root/'metrics/control_plane_downtime.start').exists(), 'cannot establish new NS start after freeze')
        for direction in ('egress','ingress'):
            if not self.cfg.get(direction): continue
            profile,access=self.profile(direction,'source'); entry=self.life['observers'].get(direction)
            if not entry:
                config=dict(run=self.root.name,direction=direction,boot=profile['boot'],source_ip=(self.cfg['egress']['guest']['ip'] if direction=='egress' else self.cfg['ingress']['observer']['ip']),
                    probe=self.cfg[direction]['probe'],interval=self.cfg['interval'],timeout=self.cfg['timeout'],lifetime=self.cfg['lifetime'],stable_samples=self.cfg['stable_samples'],
                    expected_peer=self.before['gateway']['fixed_ips'][0]['ip_address'] if direction=='egress' else self.cfg['ingress']['observer']['ip'],
                    helper_sha256=hashlib.sha256((SCRIPTS/'ns_probe.py').read_bytes()).hexdigest())
                entry=dict(config=config,access=access,status='START_INTENT'); self.life['observers'][direction]=entry; self.commit()
            # Remote intent refuses an ambiguous launch; an existing exact live process is reused.
            self.call(direction,'start')
            deadline=time.monotonic()+self.cfg['readiness_timeout']
            while True:
                raw=self.call(direction,'collect')
                if self.stable(raw):
                    if 'start' not in entry:
                        row=raw['rows'][-1]; entry['start']=dict(seq=row['seq'],mono=row['end_mono'],boot=row['boot']); entry['status']='RUNNING'; self.commit()
                    break
                require(time.monotonic()<deadline, 'controlled N-S endpoint not ready before migration; no baseline anchor')
                time.sleep(self.cfg['interval'])

    def ready(self):
        current_globals=read(self.root,'runtime.json')['globals']
        check_kolla(self.cfg,yaml.safe_load(pathlib.Path(current_globals).read_text()))
        self.api_snapshot(); fresh=self.hosts()
        old=read(self.root,'ns-host-before.json'); require(old and {h:v['cleanup'] for h,v in old.items()}=={h:v['cleanup'] for h,v in fresh.items()}, 'fresh pre-freeze legacy attachment evidence changed')
        save(self.root/'ns-host-prefreeze.json',fresh)
        baseline=read(self.root,'ns-guest-baselines.json')
        for d in ('egress','ingress'):
            if not self.cfg.get(d): continue
            vm=self.guest(d); access=self.transport.access(vm,'source'); profile=self.transport.profile(vm,access)
            row=[r for r in read(self.root,'network-mtu-plan.json')['networks'] if r['network']==vm['network']]
            require(len(row)==1, 'N-S endpoint target MTU journal missing before freeze')
            verify_profile(profile,vm,baseline[d]['boot'],row[0]['target_mtu'],vm['mac'])
        for direction,entry in self.life['observers'].items():
            self.profile(direction,'prepared' if direction=='egress' else 'source')
            require(entry.get('start') and self.stable(self.call(direction,'collect')), 'continuous observer lost pre-freeze readiness')
        require(set(self.life['observers'])==({'egress','ingress'} if self.cfg.get('ingress') else {'egress'}), 'required N-S observer missing')

    def retire(self, namespaces=False):
        require((self.root/'metrics/control_plane_downtime.start').exists(), 'legacy retirement requires freeze')
        if namespaces:
            require(read(self.root,'ns-ovn-takeover.json',{}).get('status')=='PASS', 'N-S takeover required before namespace cleanup')
            self.takeover()  # refresh live gateway/localnet evidence on a late cleanup resume
        plans=read(self.root,'ns-host-prefreeze.json'); require(plans, 'missing exact pre-freeze retirement evidence')
        for host,evidence in plans.items():
            if host not in self.cfg['gateway_hosts']: continue
            intent=self.root/('ns-cleanup-'+host+'.json' if namespaces else 'ns-retire-'+host+'.json')
            save(intent,dict(status='STARTED',plan=evidence['cleanup']))
            code=remote_code(['ovn_workload_evidence','ns_scope','ns_host'], 'p=json.load(sys.stdin); e=sys.modules["ns_host"].read(); sys.modules["ns_host"].retire(p["plan"],e,p["namespaces"]); print(json.dumps(e))')
            output=self.transport.run(self.transport.host_argv(host)+[shlex.join(([] if self.transport.host_argv(host)[-1].startswith('root@') else ['sudo','-n'])+['python3','-c',code])],json.dumps(dict(plan=evidence['cleanup'],namespaces=namespaces)))
            save(intent,dict(status='COMPLETE',plan=evidence['cleanup'],fresh_evidence=json.loads(output)))

    def takeover(self):
        save(self.root/'ns-ovn-takeover.json',dict(status='PENDING',started_at=time.time()))
        runtime=read(self.root,'runtime.json'); import configparser
        config=configparser.ConfigParser(interpolation=None); config.read(self.root/'ml2_conf.ini.target')
        code=remote_code(['ovn_workload_evidence','ns_scope','ns_ovn'], 'p=json.load(sys.stdin); print(json.dumps(sys.modules["ns_ovn"].collect(p["nb"],p["sb"])))')
        deadline=time.monotonic()+self.cfg['readiness_timeout']
        unset=object()
        attempt=max([int(p.stem.rsplit('-',1)[1]) for p in self.root.glob('ns-takeover-attempt-*.json')] or [0])
        while True:
            if time.monotonic()>=deadline:
                failure=read(self.root,'ns-ovn-takeover.json',{})
                failure.update(status='FAIL',timed_out=True)
                save(self.root/'ns-ovn-takeover.json',failure)
                raise RuntimeError('N-S: takeover convergence timed out; fresh attempt evidence retained')
            attempt+=1; evidence=dict(attempt=attempt,status='COLLECTING',started_at=time.time())
            path=self.root/f'ns-takeover-attempt-{attempt:04d}.json'
            host_path=self.root/f'ns-host-attempt-{attempt:04d}.json'
            evidence['host_evidence_file']=host_path.name
            save(self.root/'ns-ovn-takeover.json',dict(status='PENDING',attempt=attempt))
            previous_deadline=getattr(self.transport,'deadline',unset)
            self.transport.deadline=min(deadline,previous_deadline) if isinstance(previous_deadline,(int,float)) else deadline
            try:
                # Both observations belong to this attempt. No API calls while frozen.
                evidence['stage']='hosts'; hosts=self.hosts(True,evidence_path=host_path); evidence['hosts']=hosts
                save(self.root/'ns-host-target.json',hosts); save(path,evidence)
                evidence['stage']='ovn'
                argv=self.transport.host_argv(runtime['ovn_cli_host'])
                prefix=[] if argv[-1].startswith('root@') else ['sudo','-n']
                db=json.loads(self.transport.run(argv+[shlex.join(prefix+['python3','-c',code])],json.dumps(dict(nb=config['ovn']['ovn_nb_connection'],sb=config['ovn']['ovn_sb_connection']))))
                evidence['ovn']=db; save(self.root/'ns-ovn-raw.json',db); save(path,evidence)
                evidence['stage']='verify'; result=verify(self.cfg,self.before,db,hosts)
                require(time.monotonic()<=deadline, 'takeover convergence deadline expired')
                evidence.update(status='PASS',result=result); save(path,evidence)
                result['evidence_file']=path.name; save(self.root/'ns-ovn-takeover.json',result); return
            except Exception as exc:
                # Only absent target state or bounded collection timeouts are transient.
                # SSH trust/authentication, malformed data, identity/config violations fail closed.
                retryable=isinstance(exc,(ConvergencePending,TimeoutError,subprocess.TimeoutExpired))
                reason=str(exc) if isinstance(exc,RuntimeError) else type(exc).__name__
                evidence.update(status='FAIL',reason=reason,retryable=retryable); save(path,evidence)
                save(self.root/'ns-ovn-takeover.json',dict(status='FAIL',reason=reason,evidence_file=path.name,retryable=retryable))
                remaining=deadline-time.monotonic()
                if not retryable: raise
                if remaining<=0:
                    failure=read(self.root,'ns-ovn-takeover.json')
                    failure['timed_out']=True; save(self.root/'ns-ovn-takeover.json',failure)
                    raise RuntimeError('N-S: takeover convergence timed out; fresh attempt evidence retained') from exc
            finally:
                if previous_deadline is unset: del self.transport.deadline
                else: self.transport.deadline=previous_deadline
            time.sleep(min(self.cfg['interval'],remaining))

    def recovery(self):
        markers=restoration_markers(self.root)
        actual=self.api_snapshot(True); save(self.root/'ns-after.json',actual)
        self.takeover()  # fresh gateway/NAT/physical evidence after API restoration
        profiles={direction:self.profile(direction,'ovn')[0] for direction in self.life['observers']}
        # Check BOTH endpoint guest identities, including an ingress guest not used as observer.
        if self.cfg.get('ingress'):
            vm=self.guest('ingress'); access=self.transport.access(vm,'ovn')
            profile=self.transport.profile(vm,access)
            baseline=read(self.root,'ns-guest-baselines.json')['ingress']
            journal=read(self.root,'network-mtu-plan.json')['networks']
            mtu=[r['target_mtu'] for r in journal if r['network']==vm['network']]
            require(len(mtu)==1,'ingress target MTU journal missing')
            verify_profile(profile,vm,baseline['boot'],mtu[0],vm['mac']); profiles['ingress_guest']=profile
        save(self.root/'ns-post-identities.json',dict(status='PASS',controller_markers=markers,profiles=profiles,established_at=time.time()))
        recovery=read(self.root,'ns-recovery.json')
        if not recovery:
            fences={d:self.call(d,'collect','ovn')['state']['seq'] for d in self.life['observers']}
            recovery=dict(status='PENDING',fences=fences,controller_markers=markers,established_at=time.time())
            save(self.root/'ns-recovery.json',recovery)  # fences survive interrupted recovery
        require(recovery['controller_markers']==markers,'restoration markers changed across resume')
        deadline=time.monotonic()+self.cfg['readiness_timeout']
        while True:
            complete=True
            for d,entry in self.life['observers'].items():
                raw=self.call(d,'collect','ovn'); save(self.root/('ns-'+d+'-raw.json'),raw)
                if not entry.get('end') and self.stable(raw,recovery['fences'][d]):
                    row=raw['rows'][-1]; entry['end']=dict(seq=row['seq'],mono=row['end_mono'],boot=row['boot']); self.commit()
                complete=complete and bool(entry.get('end'))
            if complete:
                recovery['status']='PASS'; save(self.root/'ns-recovery.json',recovery); return
            require(time.monotonic()<deadline, 'no stable fresh post-OVN N-S recovery; open outages preserved')
            time.sleep(self.cfg['interval'])

    def collect(self):
        for direction in self.life['observers']:
            raw=self.call(direction,'collect','ovn'); save(self.root/('ns-'+direction+'-raw.json'),raw)

    def finalize(self):
        result=read(self.root,'migration-report.json',{})
        require(result.get('result') in ('SUCCESS','SUCCESS_WITH_REMEDIATION') and report(self.root)['status']=='PASS', 'preserve N-S observers/evidence on incomplete acceptance; finite lifetime remains')
        for direction in self.life['observers']:
            raw=self.call(direction,'stop','ovn'); save(self.root/('ns-'+direction+'-raw.json'),raw)
        save(self.root/'ns-finalization.json',dict(status='PASS',raw_evidence_retained=True,operator_resources_deleted=False))


def main():
    p=argparse.ArgumentParser(); p.add_argument('action',choices=['inspect','start','ready','retire','cleanup','takeover','recovery','collect','finalize']); p.add_argument('root',type=pathlib.Path); a=p.parse_args()
    runtime=read(a.root,'runtime.json',{})
    if not runtime.get('ns_enabled',False): return
    lock=(a.root/'ns.lock').open('a'); fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    try:
        cloud=None
        if a.action in ('inspect','start','ready','recovery','collect','finalize'):
            import openstack
            cloud=openstack.connect(api_timeout=10)
        if a.action=='inspect':
            cfg=read(a.root,'ns-config.json'); validate_config(cfg,runtime['network_hosts'],runtime['compute_hosts']); check_kolla(cfg,read(a.root,'source-globals-ns.json'))
            before=snapshot(cloud,cfg); save(a.root/'ns-before.json',before)
            w=Workflow(a.root,cloud); save(a.root/'ns-host-inspection.json',w.hosts())
            return
        w=Workflow(a.root,cloud)
        if a.action=='start':
            baseline=read(a.root,'ns-guest-baselines.json',{})
            for d in ('egress','ingress'):
                if not w.cfg.get(d): continue
                vm=w.guest(d); profile=w.transport.profile(vm,w.transport.access(vm,'source')); verify_profile(profile,vm,baseline.get(d,{}).get('boot'),mtu=w.cloud.network.get_network(vm['network']).mtu,mac=vm['mac']); baseline[d]=dict(boot=profile['boot'],server=vm['server'],port=vm['port'],ip=vm['ip'])
            save(a.root/'ns-guest-baselines.json',baseline)
        if a.action in ('retire','cleanup'): w.retire(a.action=='cleanup')
        else: getattr(w,a.action)()
    except Exception as exc:
        save(a.root/('ns-'+a.action+'-failure.json'),dict(status='FAIL',error_type=type(exc).__name__,reason=str(exc) if type(exc) in (RuntimeError,ValueError) else 'Operation failed; inspect private local diagnostics; no restart or rollback'))
        raise


if __name__=='__main__': main()
