#!/usr/bin/env python3
"""Stream reconstructed adapters in check mode; no guest installation or E2E writes."""
import argparse
import hashlib
import json
import pathlib
import shlex
import sys

from dataplane_capture import save
from ew_bootstrap_access import sources,recheck

ADAPTER=pathlib.Path(__file__).resolve().parents[1]/'workloads/ew-bootstrap/adapter.py'
APP_CHECK="""import importlib,json
missing=[]
for name in ('gunicorn','psycopg2','pika'):
 try: importlib.import_module(name)
 except Exception: missing.append(name)
print(json.dumps({'status':'MISSING_DEPENDENCIES' if missing else 'PASS','missing':missing}))
"""


def verify(root, reference, cloud, transport=None):
    root=pathlib.Path(root); transport,observed=sources(root,reference,cloud,transport)
    result=dict(status='PASS',implementation='reconstructed-reviewed',guests={})
    for name,s in observed.items():
        source=APP_CHECK if name=='ew-app' else ADAPTER.read_text()
        argv=['sudo','-n','python3','-']+([] if name=='ew-app' else ['check',name])
        outcome=json.loads(transport.run(transport.guest_argv(s['vm'],s['access'],450)+[shlex.join(argv)],source,timeout=450))
        recheck(transport,s)
        result['guests'][name]=outcome
        if outcome.get('status')!='PASS': result['status']='NOT_READY'
        save(root/'ew-bootstrap-check.json',result)
    result['adapter_sha256']=hashlib.sha256(ADAPTER.read_bytes()).hexdigest()
    save(root/'ew-bootstrap-check.json',result)
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('root',type=pathlib.Path)
    p.add_argument('--reference',type=pathlib.Path,required=True); args=p.parse_args()
    import openstack
    result=verify(args.root,args.reference,openstack.connect(compute_api_version='2.74',api_timeout=10))
    print(json.dumps(result)); return 0 if result['status']=='PASS' else 1


if __name__=='__main__':
    try: sys.exit(main())
    except Exception as exc:
        print('Bootstrap check refused: '+(str(exc) if type(exc) is RuntimeError else type(exc).__name__),file=sys.stderr)
        sys.exit(1)
