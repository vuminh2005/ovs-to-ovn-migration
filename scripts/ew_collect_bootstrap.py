#!/usr/bin/env python3
"""Read-only Work Item 1 evidence, not original bootstrap-source recovery."""
import argparse
from datetime import datetime, timezone
import json
import os
import pathlib
import shlex
import sys

from dataplane_capture import save
from ew_transport import Transport, verify_profile
from workload_resources import resolve_ew


# Streamed over already verified SSH to python3 -; never installed in a guest.
# Failure stdout/stderr are intentionally discarded: RabbitMQ errors can include
# Erlang-cookie diagnostics. Never query credentials or export definitions.
REMOTE_CODE = r'''
import json,os,pathlib,pwd,grp,stat,subprocess,sys
role=sys.argv[1]
def metadata(path):
 p=pathlib.Path(path)
 try:
  s=p.stat()
  return dict(path=str(p),mode=oct(stat.S_IMODE(s.st_mode)),uid=s.st_uid,gid=s.st_gid,
              owner=pwd.getpwuid(s.st_uid).pw_name,group=grp.getgrgid(s.st_gid).gr_name,symlink=p.is_symlink())
 except (OSError,KeyError) as e: return dict(path=str(p),status='UNAVAILABLE',reason=type(e).__name__)
def query(argv,structured=False):
 try:
  r=subprocess.run(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=25)
  if r.returncode: return dict(status='UNAVAILABLE',returncode=r.returncode)
  return dict(status='COLLECTED',value=json.loads(r.stdout) if structured else r.stdout.strip())
 except (OSError,ValueError,subprocess.TimeoutExpired) as e: return dict(status='UNAVAILABLE',reason=type(e).__name__)
def sql(body,db='postgres'):
 return query(['sudo','-n','-u','postgres','psql','-X','-qAt','-v','ON_ERROR_STOP=1','-d',db,
               '-c','BEGIN READ ONLY; '+body+'; COMMIT;'],True)
def rows(body): return 'SELECT coalesce(json_agg(row_to_json(t)),\'[]\'::json) FROM ('+body+') t'
result=dict(credentials=[metadata('/etc/ew-lab/'+n) for n in
    ('db-password','mq-password','app.env')],services={})
units=['ew-api.service','ew-worker.service'] if role=='ew-app' else ['postgresql.service'] if role=='ew-db' else ['rabbitmq-server.service']
for unit in units:
 result['services'][unit]=query(['systemctl','show',unit,'-p','ActiveState','-p','UnitFileState',
                               '-p','FragmentPath','-p','DropInPaths','-p','EnvironmentFiles'])
if role=='ew-db':
 result['cluster_inventory']=query(['pg_lsclusters','--no-header'])
 result['packages']=query(['dpkg-query','-W','postgresql*'])
 result['version']=sql('SELECT json_build_object(\'version\',version(),\'server_version_num\',current_setting(\'server_version_num\'))')
 result['settings']=sql(rows("SELECT name,setting,source,sourcefile,sourceline FROM pg_settings WHERE name IN ('listen_addresses','port','ssl','ssl_cert_file','ssl_key_file','ssl_ca_file','ssl_crl_file','password_encryption','unix_socket_directories','data_directory','config_file','hba_file','ident_file') ORDER BY name"))
 # HBA options can contain LDAP credentials. Never select options or raw files.
 result['authentication_rules']=sql(rows('SELECT line_number,type,database,user_name,address,netmask,auth_method,(error IS NOT NULL) AS parse_error FROM pg_hba_file_rules ORDER BY line_number'))
 result['role']=sql(rows("SELECT rolname,rolcanlogin,rolinherit,rolsuper,rolcreaterole,rolcreatedb,rolreplication,rolbypassrls,rolconnlimit,rolvaliduntil FROM pg_roles WHERE rolname='ewapp'"))
 result['memberships']=sql(rows("SELECT r.rolname AS granted_role,u.rolname AS member,m.admin_option FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.roleid JOIN pg_roles u ON u.oid=m.member WHERE u.rolname='ewapp' OR r.rolname='ewapp'"))
 result['database']=sql(rows("SELECT datname,pg_get_userbyid(datdba) AS owner,datallowconn,datconnlimit,pg_encoding_to_char(encoding) AS encoding,datcollate,datctype,datacl::text FROM pg_database WHERE datname='ewlab'"))
 result['database_permissions']=sql("SELECT json_build_object('connect',has_database_privilege('ewapp','ewlab','CONNECT'),'create',has_database_privilege('ewapp','ewlab','CREATE'),'temp',has_database_privilege('ewapp','ewlab','TEMP'))")
 result['schemas']=sql(rows("SELECT nspname,pg_get_userbyid(nspowner) AS owner,nspacl::text,has_schema_privilege('ewapp',oid,'USAGE') AS usage,has_schema_privilege('ewapp',oid,'CREATE') AS create FROM pg_namespace WHERE nspname='public'"),'ewlab')
 result['tables']=sql(rows("SELECT c.relname,pg_get_userbyid(c.relowner) AS owner,c.relacl::text FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('r','S')"),'ewlab')
 result['grants']=sql(rows("SELECT table_schema,table_name,grantee,privilege_type,is_grantable FROM information_schema.role_table_grants WHERE grantee='ewapp'"),'ewlab')
 result['default_privileges']=sql(rows("SELECT pg_get_userbyid(defaclrole) AS owner,defaclnamespace::regnamespace::text AS schema,defaclobjtype,defaclacl::text FROM pg_default_acl"),'ewlab')
 result['listeners']=query(['ss','-lnt'])
elif role=='ew-queue':
 def rabbit(tool,*args): return query(['sudo','-n','-u','rabbitmq',tool,'-q',*args])
 result['version']=rabbit('rabbitmq-diagnostics','server_version')
 result['listeners']=rabbit('rabbitmq-diagnostics','listeners')
 result['users']=rabbit('rabbitmqctl','list_users')
 result['vhosts']=rabbit('rabbitmqctl','list_vhosts','name','tracing')
 result['user_permissions']=rabbit('rabbitmqctl','list_user_permissions','ewapp')
 result['vhost_permissions']=rabbit('rabbitmqctl','list_permissions','-p','ewlab')
 result['topic_permissions']=rabbit('rabbitmqctl','list_topic_permissions','-p','ewlab')
 result['enabled_plugins']=rabbit('rabbitmq-plugins','list','-e','-m')
 result['packages']=query(['dpkg-query','-W','rabbitmq-server'])
 result['effective_settings']={name:rabbit('rabbitmqctl','eval','application:get_env(rabbit,'+name+').')
     for name in ('tcp_listeners','ssl_listeners','auth_mechanisms','auth_backends','loopback_users')}
 root=pathlib.Path('/etc/rabbitmq')
 result['candidate_config_paths']=[metadata(p) for p in sorted(root.rglob('*')) if p.is_file()] if root.is_dir() else []
elif role=='ew-app':
 result['dependency_versions']=query(['python3','-c',"import json,gunicorn,psycopg2,pika; print(json.dumps({'gunicorn':gunicorn.__version__,'psycopg2':psycopg2.__version__,'pika':pika.__version__}))"],True)
print(json.dumps(result))
'''


def source_candidates(roots, limit=3000):
    """Paths/permissions only. Never search shell history or print source text."""
    result = []; examined = 0; unavailable = []
    skip = {'.git', '.ssh', '.cache', '.local', 'venvs', '.venv', 'node_modules', '__pycache__',
            'ovs-to-ovn-backup', 'kolla-reset-snapshots'}
    for root in roots:
        for parent, dirs, files in os.walk(root, onerror=lambda e: unavailable.append(dict(path=e.filename,reason=type(e).__name__))):
            dirs[:] = [d for d in dirs if d not in skip and len(pathlib.Path(parent).relative_to(root).parts) < 4]
            for name in files:
                examined += 1
                if examined > limit: return dict(paths=result, truncated=True, roots=list(map(str, roots)), unavailable=unavailable)
                p = pathlib.Path(parent)/name
                if p.suffix in ('.sh', '.sql') or (p.suffix in ('.py', '.yml', '.yaml', '.conf') and
                                                  any(s in name.lower() for s in ('postgres', 'rabbit', 'bootstrap'))):
                    try:
                        s = p.stat()
                        result.append(dict(path=str(p),mode=oct(s.st_mode & 0o777),uid=s.st_uid,gid=s.st_gid))
                    except OSError as exc:
                        result.append(dict(path=str(p),status='UNAVAILABLE',reason=type(exc).__name__))
    return dict(paths=result, truncated=False, roots=list(map(str, roots)), unavailable=unavailable)


def collect(root, cloud, transport=None):
    root = pathlib.Path(root); output = root/'ew-bootstrap-live-evidence.json'
    if output.exists(): raise RuntimeError('Collection evidence already exists; use a fresh private directory')
    cfg = json.loads((root/'ew-measurement-config.json').read_text())
    catalog = resolve_ew(cloud, json.loads((root/'ew-config.json').read_text()), root)
    transport = transport or Transport(cfg, catalog, cloud=cloud)
    result = dict(kind='recovered-live-configuration-not-original-bootstrap-source',
                  collected_at=datetime.now(timezone.utc).isoformat(), guests={},
                  original_bootstrap_sources='UNRESOLVED', adapters_ready=False)
    for name in ('ew-app', 'ew-db', 'ew-queue'):
        vm = catalog['servers'][name]; network = cloud.network.get_network(vm['network'])
        phase = {'vxlan':'source','geneve':'ovn'}[network.provider_network_type]
        access = transport.access(vm, phase); profile = transport.profile(vm, access)
        verify_profile(profile, vm, mac=cloud.network.get_port(vm['port']).mac_address)
        payload = transport.run(transport.guest_argv(vm, access, 450) +
            [shlex.join(['sudo','-n','python3','-',name])], REMOTE_CODE, timeout=450)
        result['guests'][name] = dict(server=vm['server'],port=vm['port'],ip=vm['ip'],boot=profile['boot'],
            network_type=network.provider_network_type,access=access,evidence=json.loads(payload))
        save(output, result)
    result['controller_source_candidates'] = source_candidates([pathlib.Path('/root')])
    result['controller_private_input_paths'] = []
    for p in (pathlib.Path('/root/ew-access.yml'),):
        try:
            s = p.stat()
            result['controller_private_input_paths'].append(dict(path=str(p),mode=oct(s.st_mode & 0o777),uid=s.st_uid,gid=s.st_gid))
        except OSError as exc:
            result['controller_private_input_paths'].append(dict(path=str(p),status='UNAVAILABLE',reason=type(exc).__name__))
    save(output, result)
    return dict(status='COLLECTED', evidence=str(output), adapters_ready=False,
                note='Unavailable fields remain explicit; live settings are not original bootstrap source')


def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('root',type=pathlib.Path)
    args=p.parse_args()
    import openstack
    print(json.dumps(collect(args.root, openstack.connect(compute_api_version='2.74',api_timeout=10))))


if __name__=='__main__':
    try: main()
    except Exception as exc:
        print('Collection refused: '+(str(exc) if type(exc) is RuntimeError else type(exc).__name__),file=sys.stderr)
        sys.exit(1)
