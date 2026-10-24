#!/usr/bin/env python3
"""Read-only functional validation of existing guests after OVS restoration.

Does not create resources, reconnect held sockets, reboot guests or modify any
migration measurement/checkpoint/report. Missing probes fail, never imply PASS.
"""
import sys
from pathlib import Path
if (Path(__file__).resolve().parents[1] / '_paths.py').is_file():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from _paths import configure
    configure()
import argparse
import json
import signal
import time
from rollback_common import Stop, save
from run_layout import run_path


def wait(root, timeout=180):
    from workload_validation import Validation, records, sequence_anchor, guest_checks, latest_health
    config=json.loads(run_path(root,'validation-config.json').read_text())
    state=json.loads(run_path(root,'validation-resources.json').read_text())
    if state.get('schema_version')!=2: raise Stop('Unsupported guest checkpoint; do not rewrite historical validation state')
    validation=Validation(root)
    extended=None
    if config.get('ns',{}).get('enabled'):
        extended=json.loads(run_path(root,'extended-resources.json').read_text())
        if extended.get('run')!=config['run']:raise Stop('Extended probe run identity differs')
    deadline=time.monotonic()+timeout; fences={}; ns_fences={}; checks={}
    while time.monotonic()<deadline:
        checks={}
        for role in (('measure',) if config.get('ping_only') else ('measure','pre')):
            collected={}
            for key,vm in validation.pair(role).items():
                label=vm.get('record_vm',role+key)
                text=validation.console_output(vm['server'],deadline)
                collected[key]=records(text,config['run'],label)
            for key,rows in collected.items():
                name=role+'/'+key
                if name not in fences:
                    anchor=sequence_anchor(rows)
                    if anchor:fences[name]=anchor
                health=guest_checks(rows,fences.get(name),config.get('probe_interval',0.2))
                if config.get('ping_only'):
                    checks[name]=dict(connectivity=health['connectivity'])
                    continue
                vm=validation.pair(role)[key]
                expected_mtu=validation.cloud.network.get_network(vm['network']).mtu
                latest=latest_health(rows,fences.get(name),config.get('probe_interval',0.2))
                checks[name]=dict(**health,mtu='PASS' if latest and latest.get('mtu')==expected_mtu else 'UNAVAILABLE')
        if extended:
            # The ordinary Extended reader writes console mirrors. Parse these
            # two existing streams directly so migration evidence stays intact.
            prefix='OVN_NS_JSON '
            text=validation.console_output(extended['ns']['vms']['0']['server'],deadline)
            local=run_path(root,'ns/fip-client.jsonl')
            rows={}
            for name,raw in (('snat',text),('fip',local.read_text() if local.exists() else '')):
                values=[]
                for line in raw.splitlines():
                    if prefix in line:
                        try:
                            value=json.JSONDecoder().raw_decode(line.split(prefix,1)[1])[0]
                            if value.get('run')==config['run']:values.append(value)
                        except (ValueError,TypeError):pass
                rows[name]=values[-1] if values else {}
            for name,row in rows.items():
                use_icmp=config['ns'].get('icmp_enabled',False)
                probe=row.get('icmp') or {} if use_icmp else row.get('streams',{}).get('new_old',row.get('streams',{}).get('new',{}))
                stamp=probe.get('last_reply_mono') if use_icmp else probe.get('last_success_mono')
                if name not in ns_fences and isinstance(stamp,(int,float)):ns_fences[name]=stamp
                mono=(row or {}).get('mono')
                valid=((probe.get('alive') is True if use_icmp else probe.get('consecutive_successes',0)>=3 and probe.get('open_failure_since') is None)
                       and isinstance(stamp,(int,float)) and isinstance(mono,(int,float))
                       and name in ns_fences and stamp>ns_fences[name] and 0<=mono-stamp<=3)
                checks['ns/'+name]={('icmp' if use_icmp else 'new_tcp'):'PASS' if valid else 'UNAVAILABLE'}
        result=dict(status='PASS' if checks and all(v=='PASS' for row in checks.values() for v in row.values()) else 'WAITING',
                    checks=checks,observed_at=time.time(),new_resources=False,probe_restart=False,
                    held_tcp_preservation=False,security_group_packet_deny_test='NOT_RUN',
                    scope='Existing EW ICMP, guest DHCP/metadata/MTU and configured SNAT/FIP ICMP; policy state is verified separately against the Neutron checkpoint')
        save(root/'rollback/functional.json',result)
        if result['status']=='PASS':return result
        time.sleep(min(2,max(0,deadline-time.monotonic())))
    raise Stop('Rollback functional probes did not converge; keep maintenance enforced')


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('root',type=Path)
    parser.add_argument('--timeout',type=int,default=180);args=parser.parse_args()
    if not 1<=args.timeout<=1800:parser.error('Timeout must be between 1 and 1800 seconds')
    def expire(signum,frame):raise TimeoutError('Functional verification deadline')
    signal.signal(signal.SIGALRM,expire);signal.alarm(args.timeout)
    try: result=wait(args.root,args.timeout)
    except Exception as exc:
        result=dict(status='STOP',error_type=type(exc).__name__)
        if isinstance(exc,Stop):result['reason']=str(exc)
        save(args.root/'rollback/functional.json',result)
    print(json.dumps(result,sort_keys=True));return 0 if result['status']=='PASS' else 1


if __name__=='__main__':sys.exit(main())
