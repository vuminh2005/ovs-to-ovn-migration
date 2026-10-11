#!/usr/bin/env python3
"""Check/arm TCP through the exact owned OVS router namespace."""
import ipaddress
import json
import subprocess
import sys
import uuid


def main():
    cfg = json.loads(sys.argv[1])
    namespace = 'qrouter-'+str(uuid.UUID(cfg['router']))
    ipaddress.IPv4Address(cfg['ip'])
    if cfg.get('action', 'ARM') not in ('ARM', 'CHECK'):
        raise ValueError('Unsupported TCP control action')
    code = '''import json,socket,sys
c=json.loads(sys.argv[1])
with socket.create_connection((c['ip'],c['port']),10) as s:
 s.settimeout(15); s.sendall((c.get('action','ARM')+' '+c['run']+'\\n').encode())
 data=b''
 while b'\\n' not in data:
  block=s.recv(4096)
  if not block: raise RuntimeError('Missing ARM acknowledgement')
  data+=block
  if len(data)>8192: raise RuntimeError('Oversized ARM reply')
 r=json.loads(data.split(b'\\n')[0])
 if r.get('run')!=c['run'] or r.get('status')!='PASS':
  raise RuntimeError('Invalid control acknowledgement')
 if c.get('action','ARM')=='ARM' and r.get('armed') is not True:
  raise RuntimeError('Missing ARM acknowledgement')
 if c.get('action')=='CHECK' and (r.get('ready') is not True or r.get('armed') is not False):
  raise RuntimeError('D control is not ready or was armed before freeze')
 print(json.dumps(r))
'''
    subprocess.run(['ip','netns','exec',namespace,'python3','-c',code,json.dumps(cfg)], check=True)


if __name__ == '__main__':
    main()
