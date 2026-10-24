#!/usr/bin/env python3
"""Controller-side rollback planning; no cloud writes and no phase rewrites."""
import sys
from pathlib import Path
if (Path(__file__).resolve().parents[1] / '_paths.py').is_file():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from _paths import configure
    configure()
import argparse
import hashlib
import json
import time
from rollback_common import Stop, journal, no_migration_process, run_id, save, sha
from run_layout import run_path


def runtime(root):
    cfg=json.loads(run_path(root,'runtime.json').read_text())
    if cfg['run_id'] != root.name: raise Stop('Run directory identity differs')
    run_id(root.name)
    if Path(cfg['inventory']).read_bytes() != run_path(root,'inventory.before').read_bytes():
        raise Stop('The full original inventory is required unchanged')
    return cfg


def initialize(root, spec):
    cfg=runtime(root)
    hosts=set(cfg['control_hosts']+cfg['network_hosts']+cfg['compute_hosts']+[spec['db_host']])
    if set(spec['hosts']) != hosts: raise Stop('Rollback source coverage differs from inventory roles')
    for host in hosts:
        if '/' in host or host in ('.','..') or not host: raise Stop('Unsafe inventory hostname')
    value=dict(schema=1,run=root.name,inventory_sha256=sha(cfg['inventory']),**spec)
    path=root/'rollback/spec.json'
    if path.exists() and json.loads(path.read_text()) != value: raise Stop('Rollback specification is immutable')
    save(path,value)
    for name in ('source','frozen','inspection','database','attempts','verified'): (root/'rollback'/name).mkdir(mode=0o700,exist_ok=True)
    return dict(status='PASS',spec=value)


def load(root):
    cfg=runtime(root)
    value=json.loads((root/'rollback/spec.json').read_text())
    if value.get('schema') != 1 or value.get('run') != root.name: raise Stop('No supported rollback source checkpoint for this run')
    if value.get('inventory_sha256') != sha(cfg['inventory']): raise Stop('Original inventory hash differs from the immutable rollback specification')
    return value


def events(root):
    path=root/'rollback/events.jsonl'
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def nova_state(cloud):
    rows=[]
    for server in cloud.compute.servers(all_projects=True,details=True):
        row={key:getattr(server,key,None) for key in ('id','compute_host','status','task_state')}
        if row['task_state'] or row['status'] not in ('ACTIVE','SHUTOFF','PAUSED','SUSPENDED'):
            raise Stop('Nova has an in-flight or unsupported instance state')
        rows.append(row)
    return sorted(rows,key=lambda row:row['id'])


def nova(root,freeze=False):
    import openstack
    load(root)
    cloud=openstack.connect();cloud.compute.default_microversion='2.74'
    rows=nova_state(cloud)
    path=root/'rollback/nova-frozen.json'
    if freeze:
        if path.exists():raise Stop('Never overwrite the frozen Nova inventory')
        domains=set()
        for host,roles in load(root)['hosts'].items():
            if roles.get('compute'):
                domains.update(json.loads((root/'rollback/frozen'/f'{host}.json').read_text()).get('vms',{}))
        if domains!={r['id'] for r in rows}:raise Stop('Nova instances differ from the complete libvirt source inventory')
        save(path,dict(schema=1,run=root.name,servers=rows))
        return dict(status='PASS',servers=len(rows),source_sha256=sha(path))
    original=json.loads(path.read_text())
    value=dict(status='PASS',run=root.name,matches=rows==original['servers'],source_sha256=sha(path),observed_at=time.time())
    save(root/'rollback/nova-inspection.json',value)
    return value


def plan(root):
    spec=load(root); no_migration_process()
    if (root/'rollback/complete.json').exists(): raise Stop('This run has already been rolled back; use verify mode')
    observations={}; source_hashes={}
    for host in spec['hosts']:
        source=json.loads((root/'rollback/source'/f'{host}.json').read_text())
        archive=root/'rollback/source'/f'{host}.tar'
        if source['run'] != root.name or source['host'] != host or sha(archive) != source['archive_sha256']:
            raise Stop('Source backup identity/hash differs: '+host)
        source_hashes[host]=source['archive_sha256']
        path=root/'rollback/inspection'/f'{host}.json'
        row=json.loads(path.read_text())
        if row.get('status') != 'PASS' or row.get('run') != root.name or row.get('host') != host:
            raise Stop('Host inspection is not valid: '+host)
        if time.time()-path.stat().st_mtime > 600: raise Stop('Host inspection is stale; inspect again')
        observations[host]=row
    intent={row['stage'] for row in events(root) if row['edge']=='intent'}
    controller_seen=any(o['services'].get('ovn_controller') for o in observations.values()) or 'controller-deploy' in intent
    dirty=run_path(root,'metrics/db_migration.start').exists() or 'db-migration' in intent or controller_seen
    dbpath=root/'rollback/database/database-inspection.json'
    db=json.loads(dbpath.read_text()) if dbpath.exists() else {}
    if dirty and (db.get('status') != 'PASS' or db.get('business_matches') is not True):
        raise Stop('No verified checkpoint or unreconciled business changes; database restore is prohibited')
    if dirty:
        checkpoint=db['checkpoint']
        if checkpoint.get('database') != spec['database']: raise Stop('Checkpoint database differs from the original Neutron schema')
        dump=root/'rollback/database/neutron.sql'
        if checkpoint['run'] != root.name or sha(dump) != checkpoint['dump_sha256']:
            raise Stop('Controller copy of the frozen database dump differs')
        for host, roles in spec['hosts'].items():
            if roles.get('chassis') and not (root/'rollback/frozen'/f'{host}.json').is_file():
                raise Stop('Frozen VM/OVS baseline missing: '+host)
        nova_path=root/'rollback/nova-inspection.json'
        nova_check=json.loads(nova_path.read_text())
        if (nova_check.get('matches') is not True or nova_check.get('source_sha256')!=sha(root/'rollback/nova-frozen.json') or
                time.time()-nova_path.stat().st_mtime>600):raise Stop('Nova changed or its inspection is stale; restore is prohibited')
    live={host:{name:row and dict(id=row['id'],image_id=row['image_id'],running=row['running'])
                for name,row in observation['services'].items()} for host,observation in observations.items()}
    api_reopened=run_path(root,'metrics/control_plane_downtime.end').exists() or any(o.get('api_driver')=='ovn' and
        o['services'].get('neutron_server',{}).get('running') for o in observations.values())
    value=dict(schema=1,status='READY',run=root.name,inventory_sha256=spec['inventory_sha256'],
               source_hashes=source_hashes,db_restore=bool(dirty),flow_rebuild=bool(controller_seen),
               api_reopened=bool(api_reopened),live_services=live,
               business_sha256=db.get('current_business_sha256'),dump_sha256=db.get('checkpoint',{}).get('dump_sha256'),
               nova_sha256=sha(root/'rollback/nova-frozen.json') if dirty else None,
               metadata_port_ids=db.get('metadata_port_ids',[]), maintenance_required=True,
               scope='In-place OVS restoration; no VM deletion, global MariaDB restore or original-report rewrite')
    value['plan_sha256']=hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()
    save(root/'rollback/plan.json',value)
    return value


def authorize(root, cfg):
    value=json.loads((root/'rollback/plan.json').read_text())
    no_migration_process()
    if cfg.get('execute') is not True or cfg.get('maintenance') is not True:
        raise Stop('Execute requires explicit authorization and externally enforced maintenance')
    if value.get('status')!='READY' or cfg.get('plan_sha256')!=value.get('plan_sha256'):
        raise Stop('Inspect and approve the current concrete plan hash before executing')
    save(root/'rollback/execution.json',dict(status='AUTHORIZED',run=root.name,plan_sha256=value['plan_sha256'],authorized_at=time.time()))
    return dict(status='PASS',plan=value)


def complete(root, cfg):
    value=json.loads((root/'rollback/functional.json').read_text())
    if value.get('status')!='PASS': raise Stop('Functional rollback checks have not passed; keep maintenance enforced')
    for host in load(root)['hosts']:
        row=json.loads((root/'rollback/verified'/f'{host}.json').read_text())
        if row.get('status')!='PASS': raise Stop('Source host verification has not passed')
    result=dict(status='RESTORED_REQUIRES_ACCEPTANCE',run=root.name,completed_at=time.time(),
                maintenance_released=False,held_tcp_preservation=False,
                note='Infrastructure and existing functional probes passed; operator acceptance is required before removing maintenance')
    save(root/'rollback/complete.json',result)
    return dict(status='PASS',result=result)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('init','load','plan','authorize','complete','event','nova-freeze','nova-inspect'))
    parser.add_argument('root',type=Path); parser.add_argument('config',nargs='?',default='{}')
    args=parser.parse_args(); cfg=json.loads(args.config)
    try:
        if args.action=='event':
            load(args.root); journal(args.root/'rollback/events.jsonl',cfg['stage'],cfg['edge']); result=dict(status='PASS')
        else:
            action={'init':initialize,'load':lambda r,c:dict(status='PASS',spec=load(r),
                    plan=json.loads((r/'rollback/plan.json').read_text()) if (r/'rollback/plan.json').exists() else {}),
                    'nova-freeze':lambda r,c:nova(r,True),'nova-inspect':lambda r,c:nova(r),
                    'plan':lambda r,c:plan(r),'authorize':authorize,'complete':complete}[args.action]
            result=action(args.root,cfg)
    except Exception as exc:
        result=dict(status='STOP',error_type=type(exc).__name__)
        if isinstance(exc,Stop):result['reason']=str(exc)
        if args.root.is_dir() and args.action in ('plan','authorize'):save(args.root/'rollback/blocked.json',result)
    print(json.dumps(result,sort_keys=True))
    return 0 if result['status'] in ('PASS','READY') else 1


if __name__=='__main__':sys.exit(main())
