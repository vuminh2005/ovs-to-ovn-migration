"""Copied with the run-owned observer; no API/network configuration or service changes."""
import base64
import hashlib
import fcntl
import json
import os
import pathlib
import subprocess
import sys
import time


BASE=pathlib.Path('/var/lib/ovn-ns-probes')


def alive(s, base):
    try:
        parts=(pathlib.Path('/proc')/str(s['pid'])/'cmdline').read_bytes().split(b'\0')
        ticks=(pathlib.Path('/proc')/str(s['pid'])/'stat').read_text().rsplit(')',1)[1].split()[19]
        return s['boot']==pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip() and ticks==s['start_ticks'] and str(base).encode() in parts and str(base/'ns_probe.py').encode() in parts
    except (OSError,KeyError): return False


def _operation(payload):
    cfg=payload['config']; base=BASE/cfg['run']/cfg['direction']
    if not all(c.isalnum() or c in '-_' for c in cfg['run']) or cfg['direction'] not in ('egress','ingress'):
        raise RuntimeError('Unsafe run-owned path')
    config=base/'config.json'; intent=base/'start-intent.json'; state=base/'state.json'
    helper=base/'ns_probe.py'
    if state.exists() and (not helper.is_file() or hashlib.sha256(helper.read_bytes()).hexdigest()!=cfg['helper_sha256']):
        raise RuntimeError('Run-owned observer helper changed/missing; evidence preserved')
    def load(p): return json.loads(p.read_text())
    action=payload['action']
    if action=='start':
        if intent.exists():
            if not config.exists() or load(config)!=cfg or not state.exists() or not alive(load(state),base):
                raise RuntimeError('Ambiguous/interrupted N-S start; preserve evidence, no duplicate runner')
            return load(state)
        if base.exists() and any(p.name!='operation.lock' for p in base.iterdir()): raise RuntimeError('Unjournaled N-S files; preserve evidence, no launch')
        if base.is_symlink() or BASE.is_symlink() or (BASE/cfg['run']).is_symlink(): raise RuntimeError('Symlinked observer ownership path refused')
        BASE.mkdir(parents=True,exist_ok=True,mode=0o700)
        (BASE/cfg['run']).mkdir(exist_ok=True,mode=0o700)
        base.mkdir(parents=True,exist_ok=True,mode=0o700); base.chmod(0o700)
        source=base64.b64decode(payload['source'],validate=True)
        if hashlib.sha256(source).hexdigest()!=cfg['helper_sha256']: raise RuntimeError('Helper identity mismatch')
        from ns_probe import save
        save(config,cfg); save(intent,dict(run=cfg['run'],boot=cfg['boot']))  # durable BEFORE launch
        helper=base/'ns_probe.py'; helper.write_bytes(source); helper.chmod(0o600)
        with (base/'runner.log').open('ab') as log:
            subprocess.Popen([sys.executable,str(helper),str(base)],stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
        deadline=time.monotonic()+5
        while (not state.exists() or not (base/'events.jsonl').exists()) and time.monotonic()<deadline: time.sleep(.1)
    if not state.exists() or load(config)!=cfg: raise RuntimeError('Missing/changed run-owned observer')
    s=load(state)
    if s['status']=='RUNNING' and not alive(s,base): raise RuntimeError('Observer died; capture coverage invalid, no restart')
    if action=='stop':
        if s['status']=='RUNNING':
            (base/'stop').touch(mode=0o600)
            deadline=time.monotonic()+2*cfg['timeout']+5
            while alive(s,base) and time.monotonic()<deadline: time.sleep(.1)
            s=load(state)
        if s['status']!='STOPPED': raise RuntimeError('Exact observer did not stop cleanly')
    if action in ('collect','stop'):
        rows=[json.loads(l) for l in (base/'events.jsonl').read_text().splitlines() if l.strip()]
        rows=[r for r in rows if r['seq']<=s['seq']]
        return dict(state=s,rows=rows,current_mono=time.monotonic())
    return s


def operation(payload):
    cfg=payload['config']
    if not cfg.get('run') or not all(c.isalnum() or c in '-_' for c in cfg['run']) or cfg['direction'] not in ('egress','ingress'):
        raise RuntimeError('Unsafe run-owned path')
    base=BASE/cfg['run']/cfg['direction']
    if base.is_symlink() or BASE.is_symlink() or (BASE/cfg['run']).is_symlink():
        raise RuntimeError('Symlinked observer ownership path refused')
    BASE.mkdir(parents=True,exist_ok=True,mode=0o700)
    (BASE/cfg['run']).mkdir(exist_ok=True,mode=0o700)
    base.mkdir(exist_ok=True,mode=0o700)
    with (base/'operation.lock').open('a') as lock:
        os.fchmod(lock.fileno(),0o600)
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return _operation(payload)


if __name__=='__main__': print(json.dumps(operation(json.load(sys.stdin))))
