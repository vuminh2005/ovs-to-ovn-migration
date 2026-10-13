#!/usr/bin/env python3
"""Read-only post-DB/pre-cutover checkpoint selection and guarded recovery notes."""
import argparse
import configparser
import json
from pathlib import Path
import re
import sys
import time

from phase_schema import timestamp
from provider_associations import verification
from workload_validation import save


def read(root, name):
    value=json.loads((root/name).read_text())
    if not isinstance(value,dict):
        raise ValueError('Malformed checkpoint: '+name)
    return value


def inspect(root, inventory):
    root=Path(root).resolve()
    runtime=read(root,'runtime.json')
    if runtime.get('phase_marker_schema_version') != 2:
        raise ValueError('Recovery requires marker schema v2')
    if Path(runtime['inventory']).resolve() != Path(inventory).resolve():
        raise ValueError('Recovery inventory differs from the original run')
    if Path(runtime['run_id']).name != root.name:
        raise ValueError('Runtime run ID differs from checkpoint directory')
    if (root/'inventory.before').read_bytes() != Path(inventory).read_bytes():
        raise ValueError('Kolla inventory changed after source snapshot')
    metrics=root/'metrics'
    times={name:timestamp(metrics/name) for name in ('phase06.end','phase07.start',
        'control_plane_downtime.start','db_migration.start','db_migration.end')}
    if any(value is None for value in times.values()):
        raise ValueError('Missing preparation/freeze/completed DB migration markers')
    if not (times['phase06.end'] <= times['phase07.start'] <= times['control_plane_downtime.start'] <= times['db_migration.start'] <= times['db_migration.end']):
        raise ValueError('Inconsistent migration marker order')
    for name in ('phase08.start','phase08.end','phase09.start','phase10.start','control_plane_downtime.end','total.end'):
        if (metrics/name).exists():
            raise ValueError('Cutover/cleanup/API restoration already began: '+name)
    # DB migration is never repeated by this recovery route.
    log=(root/'db-sync-migrate.log').read_text()
    if not re.match(r'\Areturncode:\s*0\s*\n',log):
        raise ValueError('Authoritative DB migration did not report rc=0')
    if read(root,'provider-associations-verification.json').get('status') != 'PASS':
        raise ValueError('L3 provider association verification is not PASS')
    if verification(json.loads((root/'provider-associations.after.json').read_text()))['status'] != 'PASS':
        raise ValueError('Provider snapshot still contains legacy associations')
    if not (root/'compute-port-ids.txt').is_file():
        raise ValueError('Pre-existing compute port ID checkpoint is absent')
    counts=read(root,'resource-counts.before.json')
    config=configparser.ConfigParser(interpolation=None)
    if not config.read(root/'ml2_conf.ini.target'):
        raise ValueError('Target ML2 configuration checkpoint is absent')
    nb=config.get('ovn','ovn_nb_connection')
    sb=config.get('ovn','ovn_sb_connection')
    if not nb or not sb:
        raise ValueError('Checkpointed OVN endpoints are empty')
    cfg=read(root,'validation-config.json') if (root/'validation-resources.json').exists() else {}
    if cfg:
        if cfg.get('run') != runtime['run_id']:
            raise ValueError('Workload run identity differs')
        state=read(root,'validation-resources.json')
        if any(state.get(stage,{}).get('cleanup_started') for stage in ('pre','post')):
            raise ValueError('Owned workload cleanup already began')
        if cfg.get('capture_enabled') and not read(root,'validation-window.json').get('pcap_start'):
            raise ValueError('Original ICMP capture anchor is absent')
        if cfg.get('pair_d_enabled'):
            trigger=read(root,'pair-d-trigger.json')
            baseline=read(root,'pair-d-baseline.json')
            if (trigger.get('status') != 'PASS' or trigger.get('ack',{}).get('session_id') != baseline.get('session_id') or
                not times['control_plane_downtime.start'] <= trigger.get('acknowledged_at',0) <= times['db_migration.end']):
                raise ValueError('Original TCP ARM/session/freeze evidence is absent')
        if cfg.get('ns',{}).get('enabled'):
            if read(root,'ns-baseline.json').get('status') != 'PASS' or read(root,'ns-precutover.json').get('status') != 'PASS':
                raise ValueError('Original NS baseline/precutover evidence is absent')
    return dict(status='POST_DB_PRE_CUTOVER_CHECKPOINT', root=str(root), runtime=runtime,
                nb=nb,sb=sb,source_network_count=counts['networks'],workload_config=cfg,
                workloads_enabled=bool(cfg),original_timestamps=times)


def select(parent, inventory):
    matches=[]
    for root in sorted(Path(parent).iterdir()):
        if not root.is_dir():continue
        try:
            inspect(root,inventory)
            matches.append(root.resolve())
        except (OSError,ValueError,KeyError,TypeError,configparser.Error):
            continue
    if len(matches) != 1:
        raise ValueError('Expected exactly one eligible post-DB checkpoint; found '+str(len(matches))+': '+', '.join(map(str,matches)))
    return matches[0]


def record(root):
    path=Path(root)/'orchestration-recovery.json'
    prior=read(Path(root),path.name) if path.exists() else {'events':[]}
    if not any(event.get('route')=='post-db-pre-cutover' for event in prior['events']):
        prior['events'].append(dict(route='post-db-pre-cutover',resumed_at=time.time(),
            reason='Topology verification interrupted after successful DB migration',
            timing='Original freeze/DB/capture start markers retained; control-plane downtime includes recovery pause'))
        save(path,prior)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=('inspect','select','record'))
    parser.add_argument('path',type=Path)
    parser.add_argument('--inventory',required=True)
    args=parser.parse_args()
    if args.action=='select':
        print(select(args.path,args.inventory))
    else:
        result=inspect(args.path,args.inventory)
        if args.action=='record':record(args.path)
        print(json.dumps(result))


if __name__=='__main__':
    try:main()
    except (OSError,ValueError,KeyError,TypeError,configparser.Error) as exc:
        raise SystemExit('STOP: '+str(exc))
