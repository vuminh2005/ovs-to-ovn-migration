#!/usr/bin/env python3
"""Explicit private controller recovery; no guest writes or credential generation."""
import argparse
import hmac
import json
import os
import pathlib
import re
import shlex
import stat
import sys
import tempfile

from ew_bootstrap_access import sources,recheck

READ_CODE = r'''
import json,os,pathlib,re,stat,sys
result={}
for kind in json.loads(sys.argv[1]):
 p=pathlib.Path('/etc/ew-lab/'+kind+'-password'); s=p.lstat()
 if not stat.S_ISREG(s.st_mode) or s.st_uid!=0 or s.st_gid!=0 or stat.S_IMODE(s.st_mode)!=0o600 or s.st_nlink!=1:
  raise RuntimeError('Credential is not a private root-owned regular file')
 value=p.read_text().strip()
 if not re.fullmatch('[0-9a-f]{48}',value): raise RuntimeError('Invalid private credential format')
 result[kind]=value
print(json.dumps(result))
'''


def private_tree(root):
    root=pathlib.Path(root)
    if not root.is_absolute() or '..' in root.parts or not root.is_relative_to('/root') or root==pathlib.Path('/root'):
        raise RuntimeError('Choose a dedicated absolute private directory below /root')
    forbidden=('/root/ovs-to-ovn-backup','/root/kolla-reset-snapshots','/root/ovs-to-ovn-migration','/root/.ssh')
    if any(root.is_relative_to(p) for p in forbidden):
        raise RuntimeError('Private recovery directory overlaps backup, source or SSH inputs')
    for path in (root,*root.parents):
        if path.exists() or path.is_symlink():
            s=path.lstat()
            if not stat.S_ISDIR(s.st_mode) or s.st_uid!=0 or s.st_mode & 0o022:
                raise RuntimeError('Unsafe private recovery directory ancestry')
    if root.exists() and stat.S_IMODE(root.stat().st_mode)!=0o700:
        raise RuntimeError('Private recovery directory must have mode 0700')


def existing(path, value, secret=False):
    if not path.exists() and not path.is_symlink(): return False
    s=path.lstat()
    if not stat.S_ISREG(s.st_mode) or s.st_uid!=0 or s.st_gid!=0 or stat.S_IMODE(s.st_mode)!=0o600:
        raise RuntimeError('Unsafe existing private output: '+path.name)
    old=path.read_text()
    if secret:
        old=old.strip()
        if not re.fullmatch('[0-9a-f]{48}',old): raise RuntimeError('Invalid existing private credential')
    if not hmac.compare_digest(old,value): raise RuntimeError('Differing existing private output; never overwritten: '+path.name)
    return True


def atomic_private(path, value, secret=False):
    if existing(path,value,secret): return False
    fd,temp=tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd,'w') as stream:
            stream.write(value); stream.flush(); os.fsync(stream.fileno())
        os.chmod(temp,0o600)
        # Atomic no-replace publication; a race never overwrites an existing file.
        try: os.link(temp,path)
        except FileExistsError:
            existing(path,value,secret)
            return False
        directory=os.open(path.parent,os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        os.unlink(temp)
    return True


def recover(root, reference, private_root, cloud, transport=None):
    if os.geteuid()!=0: raise RuntimeError('Credential recovery must run as controller root')
    private_root=pathlib.Path(private_root); private_tree(private_root)
    transport,observed=sources(root,reference,cloud,transport)
    values={}
    for name,kinds in (('ew-app',['db','mq']),('ew-db',['db']),('ew-queue',['mq'])):
        s=observed[name]
        raw=transport.run(transport.guest_argv(s['vm'],s['access'])+
            [shlex.join(['sudo','-n','python3','-c',READ_CODE,json.dumps(kinds)])])
        result=json.loads(raw)
        if set(result)!=set(kinds) or any(not isinstance(v,str) or not re.fullmatch('[0-9a-f]{48}',v) for v in result.values()):
            raise RuntimeError('Invalid remote credential response')
        values[name]=result
        recheck(transport,s)
    for kind,name in (('db','ew-db'),('mq','ew-queue')):
        if not hmac.compare_digest(values['ew-app'][kind],values[name][kind]):
            raise RuntimeError('Corresponding guest credentials differ: '+kind)
    mapping='ew_provision_secret_files:\n'+''.join('  '+kind+': '+json.dumps(str(private_root/(kind+'-password')))+'\n' for kind in ('db','mq'))
    identity=json.dumps({name:{k:s[k] for k in ('boot','mac')} | {k:s['vm'][k] for k in ('server','port','ip')}
                         for name,s in observed.items()},sort_keys=True,indent=2)+'\n'
    outputs=[(private_root/(kind+'-password'),values['ew-app'][kind],True) for kind in ('db','mq')]
    outputs += [(private_root/'ew-provision-inputs.yml',mapping,False),(private_root/'recovery-evidence.json',identity,False)]
    # Refuse every known conflict before creating any local file.
    for path,value,secret in outputs: existing(path,value,secret)
    private_root.mkdir(mode=0o700,parents=True,exist_ok=True); private_tree(private_root)
    changed=False
    for path,value,secret in outputs: changed |= atomic_private(path,value,secret)
    return dict(status='PASS',changed=changed,private_directory=str(private_root),
                inputs=str(private_root/'ew-provision-inputs.yml'),credentials='privately-compared-not-disclosed')


def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('root',type=pathlib.Path)
    p.add_argument('--reference',type=pathlib.Path,required=True); p.add_argument('--private-dir',type=pathlib.Path,default='/root/ew-private')
    args=p.parse_args()
    import openstack
    print(json.dumps(recover(args.root,args.reference,args.private_dir,openstack.connect(compute_api_version='2.74',api_timeout=10))))


if __name__=='__main__':
    try: main()
    except Exception as exc:
        # Never forward SDK/SSH/JSON bodies; these may contain the in-memory secret.
        print('Recovery refused: '+(str(exc) if type(exc) is RuntimeError else type(exc).__name__),file=sys.stderr)
        sys.exit(1)
