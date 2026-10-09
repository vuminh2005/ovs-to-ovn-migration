"""Bounded guest measurement lifecycle. No networking or application remediation."""
import argparse
import base64
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import pwd
import re
import subprocess
import time

import runner

ROOT = Path('/var/lib/ew-load')


def command(argv, timeout=15):
    result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,text=True,timeout=timeout)
    if result.returncode: raise RuntimeError(f'{argv[0]} failed with code {result.returncode}')
    return result.stdout


def profile():
    addresses = json.loads(command(['ip','-j','address']))
    instance = Path('/sys/class/dmi/id/product_uuid').read_text().strip().lower()
    return dict(server=instance,boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                hostname=command(['hostname']).strip(), interfaces=addresses,
                routes=json.loads(command(['ip','-j','route'])), utc=runner.utc(), mono=time.monotonic())


def unit(cfg):
    for value in (cfg['run_id'], cfg['client_id']):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',value): raise RuntimeError('Unsafe run/client ID')
    return 'ew-load-'+cfg['run_id']+'-'+cfg['client_id']


def validate_config(cfg):
    unit(cfg)
    if cfg['mode'] not in ('baseline','migration') or len(cfg['database_run_id'])>64:
        raise RuntimeError('Invalid measurement mode/database identity')
    for key in ('maximum_lifetime','drain','max_rate','slo','interval'):
        value=cfg[key]
        if type(value) not in (int,float) or not math.isfinite(value) or not 0 < value <= 86400: raise RuntimeError('Invalid bounded '+key)
    if not 0 <= cfg['duration'] <= cfg['maximum_lifetime'] or not cfg['probes']:
        raise RuntimeError('Invalid finite duration/probes')
    if len({p['name'] for p in cfg['probes']})!=len(cfg['probes']): raise RuntimeError('Duplicate probes')
    if cfg['output'] != str(ROOT/cfg['run_id']): raise RuntimeError('Invalid owned measurement path')


def status(cfg):
    directory=ROOT/cfg['run_id']; path=directory/'state.json'
    if not path.exists(): return dict(status='ABSENT')
    state=json.loads(path.read_text())
    if state['config'] != cfg or profile()['boot'] != cfg['boot']: raise RuntimeError('Run identity/configuration/boot changed')
    if state['status']!='COMPLETE':
        info=command(['systemctl','show',unit(cfg),'-p','ActiveState','-p','SubState','-p','ExecMainStatus','-p','MainPID'])
        state['unit_state']=dict(line.split('=',1) for line in info.splitlines() if '=' in line)
        if (state['unit_state'].get('ActiveState') not in ('active','activating','deactivating') or
            state['unit_state'].get('SubState')=='exited' or
            (state.get('pid') and str(state['pid'])!=state['unit_state'].get('MainPID'))):
            state['status']='CRASHED_OR_AMBIGUOUS'
        if state.get('process_identity') and state['status']!='CRASHED_OR_AMBIGUOUS':
            try:
                if runner.process_identity(int(state['unit_state']['MainPID']))!=state['process_identity']:
                    state['status']='CRASHED_OR_AMBIGUOUS'
            except (OSError,ValueError,KeyError): state['status']='CRASHED_OR_AMBIGUOUS'
    events=directory/'events.jsonl'; progress={}; tcp_progress={}; last_seq=0
    if events.exists():
        with events.open('rb') as stream:
            stream.seek(max(0,events.stat().st_size-131072))
            for line in stream:
                try: row=json.loads(line)
                except ValueError: continue
                last_seq=max(last_seq,row.get('seq',0))
                if row.get('kind')=='tcp_echo':
                    key=str(row['port']); entry=tcp_progress.setdefault(key,dict(successes=0))
                    entry['success_sequences']=(entry.get('success_sequences',[])+[row['seq']])[-10:] if row.get('ok') else []
                    entry.update(seq=row['seq'],mono=row['mono'],successes=entry['successes']+1 if row.get('ok') else 0,
                                 run_id=row.get('run_id'),boot=row.get('boot'))
                if row.get('kind') in {p['name'] for p in cfg['probes']}:
                    probe=progress.setdefault(row['kind'],dict(successes=0,success_sequences=[]))
                    probe['success_sequences']=(probe['success_sequences']+[row['seq']])[-10:] if row.get('ok') else []
                    probe.update(successes=probe['successes']+1 if row.get('ok') else 0,
                                 seq=row['seq'],mono=row['mono'])
    state.update(progress=progress,last_event_seq=last_seq,current_mono=time.monotonic())
    state['tcp_progress']=tcp_progress
    state['tcp_listeners']=json.loads((directory/'tcp-listeners.json').read_text()) if (directory/'tcp-listeners.json').exists() else {}
    return state


def activate_tcp(cfg):
    current=status(cfg)
    if cfg['client_id']!='ew-app' or not cfg.get('tcp_experiment') or current['status']!='RUNNING':
        raise RuntimeError('Only the exact running EW server measurement may activate a listener')
    directory=ROOT/cfg['run_id']; path=directory/'tcp-second.request.json'
    intent=dict(run_id=cfg['run_id'],boot=cfg['boot'],port=cfg['tcp_experiment']['ports'][1])
    if path.exists() and json.loads(path.read_text())!=intent: raise RuntimeError('TCP activation intent changed')
    # Public run/boot/port intent must be readable by the Ubuntu runner. Set
    # mode BEFORE atomic publication: a root-only intermediate file can crash it.
    runner.atomic_json(path,intent,mode=0o644)
    return dict(status='REQUESTED')  # never claim bind success from this request


def start(cfg):
    validate_config(cfg); ROOT.mkdir(parents=True,exist_ok=True,mode=0o755); ROOT.chmod(0o755)
    with (ROOT/'launch.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        existing=status(cfg)
        if existing['status']!='ABSENT':
            if existing['status'] in ('RUNNING','DRAINING','COMPLETE') or (
                existing['status']=='START_REQUESTED' and int(existing.get('unit_state',{}).get('MainPID','0'))>0): return existing
            raise RuntimeError('Start was requested already; ambiguous/crashed runner will not be relaunched')
        active=ROOT/'active.json'
        if active.exists():
            prior=json.loads(active.read_text())
            if status(prior)['status']!='COMPLETE': raise RuntimeError('An incomplete measurement already owns this guest')
        if profile()['boot']!=cfg['boot']: raise RuntimeError('Guest boot changed before start')
        directory=ROOT/cfg['run_id']; directory.mkdir(mode=0o700)
        state=dict(status='START_REQUESTED',config=cfg,requested_utc=runner.utc(),boot=cfg['boot'])
        runner.atomic_json(directory/'config.json',cfg); runner.atomic_json(directory/'state.json',state)
        runner.atomic_json(active,cfg)  # durable intent BEFORE systemd-run; no ambiguous retry
        user=pwd.getpwnam('ubuntu')
        for path in (directory,directory/'config.json',directory/'state.json'):
            os.chown(path,user.pw_uid,user.pw_gid)
        command(['systemd-run','--unit='+unit(cfg),'--property=Type=exec','--property=User=ubuntu',
                 '--property=Group=ubuntu','--property=RemainAfterExit=yes',
                 '--property=TimeoutStopSec='+str(cfg['drain']+30),
                 '--property=RuntimeMaxSec='+str(cfg['maximum_lifetime']+cfg['drain']+30),
                 '/usr/bin/python3','/opt/ew-load/runner.py','--config',str(directory/'config.json')])
        return state


def stop(cfg):
    state=status(cfg)
    if state['status']=='COMPLETE': return state
    if state['status'] not in ('START_REQUESTED','RUNNING','DRAINING'): raise RuntimeError('Cannot safely stop ambiguous measurement')
    (ROOT/cfg['run_id']/'stop.request').touch(mode=0o644)
    # Runner watches the durable request as well as SIGTERM. Do not use a stop
    # that kills a service before its in-flight task has a chance to drain.
    command(['systemctl','kill','--kill-who=main','--signal=SIGTERM',unit(cfg)])
    return dict(status='STOP_REQUESTED')


def drain(cfg, timeout):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        state=status(cfg)
        if state['status']=='COMPLETE': return state
        if state['status']=='CRASHED_OR_AMBIGUOUS': raise RuntimeError('Runner crashed; raw evidence retained')
        time.sleep(.5)
    raise TimeoutError('Bounded guest drain expired; evidence retained')


def collect(cfg, maximum_bytes):
    directory=ROOT/cfg['run_id']; output={}; remaining=maximum_bytes
    for name in ('config.json','state.json','events.jsonl','summary.json'):
        path=directory/name
        if path.is_symlink(): raise RuntimeError('Collection refuses symlink evidence files')
        if not path.exists(): continue
        # Bound the actual read too: an incomplete runner may still append.
        import stat
        with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW),'rb') as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode): raise RuntimeError('Collection requires regular evidence files')
            if info.st_size>remaining: raise RuntimeError('Collection exceeds byte bound; raw guest evidence retained')
            data=stream.read(remaining+1)
        if len(data)>remaining: raise RuntimeError('Collection exceeds byte bound; raw guest evidence retained')
        remaining-=len(data)
        output[name]=dict(base64=base64.b64encode(data).decode(),sha256=hashlib.sha256(data).hexdigest())
    return dict(files=output,status='COMPLETE' if 'summary.json' in output else 'INCOMPLETE')


def check(payload):
    evidence=profile()
    if payload.get('task'):
        class Events:
            def __init__(self): self.rows=[]
            def event(self,kind,**fields): self.rows.append(dict(kind=kind,**fields))
        events=Events(); client=runner.Client(payload['url'],events)
        code,health,error=client.request('/health',log=False)
        evidence['dependencies']=bool(not error and code==200 and health.get('dependencies')=={'postgresql':'ok','rabbitmq':'ok'})
        deadline=time.monotonic()+payload['timeout']
        evidence['e2e']=runner.run_task(client,payload['task'],events,lambda:deadline,payload['timeout'])
        evidence['events']=events.rows
    return evidence


def diagnostics(cfg, timeout, maximum_bytes):
    state=json.loads((ROOT/cfg['run_id']/'state.json').read_text())
    until=state.get('finished_utc',runner.utc()); since=state.get('started_utc',state['requested_utc'])
    from datetime import datetime
    journal_time=lambda value:datetime.fromisoformat(value).strftime('%Y-%m-%d %H:%M:%S UTC')
    argv=['journalctl','--no-pager','-o','json','--since='+journal_time(since),'--until='+journal_time(until),
          '-u',unit(cfg)]
    if cfg['client_id']=='ew-app': argv+=['-u','ew-api.service','-u','ew-worker.service']
    # Bound output while it is produced, not only after loading a journal tail.
    with subprocess.Popen(argv,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL) as proc:
        import selectors
        selector=selectors.DefaultSelector(); selector.register(proc.stdout,selectors.EVENT_READ)
        raw=bytearray(); deadline=time.monotonic()+timeout
        while time.monotonic()<deadline and len(raw)<=maximum_bytes:
            if selector.select(.1):
                chunk=os.read(proc.stdout.fileno(),min(65536,maximum_bytes+1-len(raw)))
                if not chunk: break
                raw.extend(chunk)
        try: proc.wait(timeout=.5)
        except subprocess.TimeoutExpired: pass
        truncated=proc.poll() is None or len(raw)>maximum_bytes
        if proc.poll() is None: proc.kill()
        proc.wait(timeout=3); selector.close()
    rows=[]; skipped=0
    for line in bytes(raw[:maximum_bytes]).splitlines():
        try:
            row=json.loads(line)
            try: message=json.loads(row.get('MESSAGE',''))
            except ValueError:
                match=re.search(r'request_failed type=([A-Za-z][A-Za-z0-9_]*)',row.get('MESSAGE',''))
                message=dict(event='request_failed',type=match[1]) if match else {}
            safe={k:message[k] for k in ('event','task_id','duplicate','redelivered','type','error_type') if k in message}
            if safe: rows.append(dict(utc_microseconds=row.get('__REALTIME_TIMESTAMP'),message=safe))
            else: skipped+=1
        except (ValueError,TypeError): skipped+=1
    return dict(since=since,until=until,status='TRUNCATED_OR_TIMED_OUT' if truncated else 'UNAVAILABLE' if proc.returncode else 'COMPLETE',
                returncode=proc.returncode,events=rows,
                skipped_entries=skipped,scope='run time window; allowlisted structured events only; no environment or secret files')


def main():
    os.umask(0o077)
    payload=json.load(os.sys.stdin); action=payload['action']; cfg=payload.get('config',{})
    if action=='profile': result=profile()
    elif action=='check': result=check(payload)
    elif action=='start': result=start(cfg)
    elif action=='status': result=status(cfg)
    elif action=='tcp-activate': result=activate_tcp(cfg)
    elif action=='stop': result=stop(cfg)
    elif action=='drain': result=drain(cfg,payload['timeout'])
    elif action=='collect': result=collect(cfg,payload['maximum_bytes'])
    elif action=='diagnostics': result=diagnostics(cfg,payload['timeout'],payload['maximum_bytes'])
    else: raise RuntimeError('Unknown guest lifecycle operation')
    print(json.dumps(result))


if __name__=='__main__': main()
