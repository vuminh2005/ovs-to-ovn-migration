#!/usr/bin/env python3
"""Finite independent N-S HTTP observer and optional established echo session."""
import hashlib
import http.client
import json
import os
import pathlib
import socket
import signal
import time
import urllib.parse
import uuid


def save(path, value):
    path=pathlib.Path(path); tmp=path.with_suffix('.tmp')
    with tmp.open('w') as f:
        json.dump(value,f); f.flush(); os.fsync(f.fileno())
    tmp.chmod(0o600); tmp.replace(path)
    fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


def http_attempt(cfg):
    nonce=str(uuid.uuid4()); p=cfg['probe']
    connection=http.client.HTTPConnection(p['address'],p['port'],timeout=cfg['timeout'],source_address=(cfg['source_ip'],0))
    try:
        path=p['path']+('&' if '?' in p['path'] else '?')+urllib.parse.urlencode({'nonce':nonce})
        connection.request('GET',path,headers={'Connection':'close'})
        reply=connection.getresponse(); body=json.loads(reply.read(16385))
        return (reply.status==200 and isinstance(body,dict) and body.get('endpoint_id')==p['endpoint_id'] and body.get('nonce')==nonce and
                (not cfg.get('expected_peer') or body.get('peer')==cfg['expected_peer']))
    finally: connection.close()


def run(base):
    cfg=json.loads((base/'config.json').read_text()); boot=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if boot!=cfg['boot']: raise RuntimeError('Observer boot changed before startup')
    def timed_out(signum,frame): raise TimeoutError('Bounded N-S observation expired')
    signal.signal(signal.SIGALRM,timed_out)
    state=dict(status='RUNNING',config=cfg,boot=boot,pid=os.getpid(),seq=0)
    state['start_ticks']=pathlib.Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19]
    save(base/'state.json',state); deadline=time.monotonic()+cfg['lifetime']; conn=None; connections=0
    with (base/'events.jsonl').open('x',buffering=1) as log:
        while time.monotonic()<deadline and not (base/'stop').exists():
            begin=time.monotonic(); ok=False; error=None
            signal.setitimer(signal.ITIMER_REAL,cfg['timeout'])
            try: ok=http_attempt(cfg)
            except (OSError,ValueError,http.client.HTTPException) as exc: error=type(exc).__name__
            finally: signal.setitimer(signal.ITIMER_REAL,0)
            row=dict(seq=state['seq']+1,run=cfg['run'],direction=cfg['direction'],boot=boot,mono=begin,
                     http_end=time.monotonic(),http_success=ok,error=error,session=None)
            if cfg['probe'].get('session_port'):
                opened=False; reset=False; success=False
                signal.setitimer(signal.ITIMER_REAL,cfg['timeout'])
                try:
                    if conn is None:
                        conn=socket.create_connection((cfg['probe']['address'],cfg['probe']['session_port']),cfg['timeout'],source_address=(cfg['source_ip'],0))
                        connections+=1; opened=True
                    message=(cfg['run']+':'+str(row['seq'])+'\n').encode(); conn.sendall(message)
                    reply=b''
                    while len(reply)<len(message):
                        part=conn.recv(len(message)-len(reply))
                        if not part: raise ConnectionError('EOF')
                        reply+=part
                    success=reply==message
                    if not success: raise ConnectionError('Invalid echo')
                except OSError:
                    reset=conn is not None
                    if conn: conn.close()
                    conn=None
                finally: signal.setitimer(signal.ITIMER_REAL,0)
                row['session']=dict(success=success,opened=opened,reset=reset,connection=connections)
            row['end_mono']=time.monotonic(); row['ts']=time.time()
            log.write(json.dumps(row)+'\n'); log.flush(); os.fsync(log.fileno())
            state['seq']=row['seq']; state['last_mono']=row['end_mono']; save(base/'state.json',state)
            time.sleep(max(0,cfg['interval']-(time.monotonic()-begin)))
    if conn: conn.close()
    state['status']='STOPPED' if (base/'stop').exists() else 'EXPIRED'; save(base/'state.json',state)


if __name__=='__main__':
    import sys
    run(pathlib.Path(sys.argv[1]))
