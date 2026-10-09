"""Bounded controller-side Neutron read observer, separate from orchestration freeze."""
import argparse
import json
import os
import pathlib
import signal
import subprocess
import sys
import time

from dataplane_capture import save


def process_identity(pid):
    fields=pathlib.Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
    if fields[0]=='Z': raise ProcessLookupError('Observer process exited')
    return fields[19]


def sample():
    try:
        import openstack
        cloud=openstack.connect(api_timeout=3)
        next(iter(cloud.network.networks(limit=1)),None)
        return dict(ok=True,error=None)
    except Exception as exc: return dict(ok=False,error=type(exc).__name__)


def observe(root,cfg):
    root=pathlib.Path(root); stop=False
    def requested(*_args):
        nonlocal stop; stop=True
    signal.signal(signal.SIGTERM,requested); signal.signal(signal.SIGINT,requested)
    state=dict(status='RUNNING',pid=os.getpid(),process_identity=process_identity(os.getpid()),
               boot=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
               started=time.monotonic(),configuration=cfg)
    save(root/'api-state.json',state)
    deadline=time.monotonic()+cfg['maximum_lifetime']+cfg['drain']+60; seq=0
    with (root/'api-events.jsonl').open('x') as stream:
        while not stop and time.monotonic()<deadline:
            start=time.monotonic(); seq+=1
            try:
                result=subprocess.run([sys.executable,__file__,'sample'],stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,text=True,timeout=6)
                value=json.loads(result.stdout) if result.returncode==0 else dict(ok=False,error='SampleProcessFailure',observer_error=True)
                if not isinstance(value,dict) or type(value.get('ok')) is not bool:
                    raise ValueError('Malformed observer sample')
            except subprocess.TimeoutExpired: value=dict(ok=False,error='SampleTimeout')
            except ValueError: value=dict(ok=False,error='MalformedSample',observer_error=True)
            from datetime import datetime,timezone
            stream.write(json.dumps(dict(seq=seq,mono=start,utc=datetime.now(timezone.utc).isoformat(),
                                         duration_seconds=time.monotonic()-start,**value))+'\n'); stream.flush()
            until=time.monotonic()+max(0,cfg['api_interval']-(time.monotonic()-start))
            while not stop and time.monotonic()<until: time.sleep(.1)
    state.update(status='COMPLETE',ended=time.monotonic(),samples=seq); save(root/'api-state.json',state)


def main():
    p=argparse.ArgumentParser(); p.add_argument('action',choices=('sample','observe')); p.add_argument('root',nargs='?')
    args=p.parse_args()
    if args.action=='sample': print(json.dumps(sample()))
    else:
        root=pathlib.Path(args.root); observe(root,json.loads((root/'observer-config.json').read_text()))


if __name__=='__main__': main()
