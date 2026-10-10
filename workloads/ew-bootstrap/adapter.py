#!/usr/bin/env python3
"""Reconstructed Work Item 1 adapters, reviewed 2026-10-10; NOT original scripts.

Only the observed ewapp/ewlab configuration is managed. No package installation,
credential replacement, application schema or AMQP topology/data operations.
"""
import argparse
import fcntl
import ipaddress
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile


class Refused(Exception):
    pass


class MissingDependencies(Refused):
    pass


def require(ok, message):
    if not ok:
        raise Refused(message)


def run(argv, data=None):
    """Never forward tool stdout/stderr on failure (may contain secrets)."""
    p = subprocess.run(argv, input=data, text=True, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, timeout=45)
    require(p.returncode == 0, 'Dependency command failed: ' + pathlib.Path(argv[0]).name)
    return p.stdout.strip()


def password(path):
    p = pathlib.Path(path); s = p.lstat()
    require(stat.S_ISREG(s.st_mode) and s.st_uid == 0 and s.st_gid == 0 and
            stat.S_IMODE(s.st_mode) == 0o600 and s.st_nlink == 1,
            'Credential must be a root-owned regular 0600 file: ' + str(p))
    value = p.read_text().strip()
    require(re.fullmatch('[0-9a-f]{48}', value) is not None, 'Invalid credential format: ' + str(p))
    return value


def write(path, text, uid=0, gid=0, mode=0o600):
    p = pathlib.Path(path)
    if p.exists():
        require(not p.is_symlink() and p.is_file(), 'Non-regular managed path: ' + str(p))
        s = p.stat(); uid, gid, mode = s.st_uid, s.st_gid, stat.S_IMODE(s.st_mode)
        if p.read_text() == text:
            return False
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=p.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(text); stream.flush(); os.fsync(stream.fileno())
        os.chown(temp, uid, gid); os.chmod(temp, mode); os.replace(temp, p)
        d = os.open(p.parent, os.O_DIRECTORY)
        try: os.fsync(d)
        finally: os.close(d)
    finally:
        if os.path.exists(temp): os.unlink(temp)
    return True


# Runs as OS postgres via stdin, not a command line carrying a password. Private
# rolpassword is tested for its SCRAM format in SQL; no verifier is returned.
PG_CODE = r'''
import base64,hashlib,hmac,json,os,subprocess,sys
p=json.load(sys.stdin)
def query(text,db='postgres',auth=False):
 env=dict(os.environ,PGHOST='127.0.0.1' if auth else '/var/run/postgresql',PGPORT='5432',
          PGUSER='ewapp' if auth else 'postgres',PGDATABASE=db,PGCONNECT_TIMEOUT='5',
          PGOPTIONS='-c statement_timeout=10000 -c lock_timeout=5000',LC_ALL='C.UTF-8')
 env.pop('PGSERVICE',None); env.pop('PGSERVICEFILE',None); env.pop('PGPASSWORD',None)
 if auth: env['PGPASSWORD']=p['password']
 r=subprocess.run(['psql','-XqAt','-w','-v','ON_ERROR_STOP=1'],input=text+';',env=env,
                  text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=20)
 if r.returncode: raise RuntimeError('PostgreSQL operation failed (details withheld)')
 return r.stdout.strip()
def rows(text,db='postgres'):
 return json.loads(query("SELECT coalesce(json_agg(row_to_json(t)),'[]'::json) FROM ("+text+") t",db))
role=rows("SELECT rolcanlogin,rolinherit,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls,rolconnlimit,rolvaliduntil::text,rolconfig FROM pg_roles WHERE rolname='ewapp'")
db=rows("SELECT pg_get_userbyid(datdba) AS owner,pg_encoding_to_char(encoding) AS encoding,datcollate,datctype,datallowconn,datconnlimit,datacl::text FROM pg_database WHERE datname='ewlab'")
if p['op']=='create_role':
 if role: raise RuntimeError('Role appeared concurrently')
 salt=os.urandom(16); salted=hashlib.pbkdf2_hmac('sha256',p['password'].encode(),salt,4096)
 stored=hashlib.sha256(hmac.new(salted,b'Client Key','sha256').digest()).digest()
 server=hmac.new(salted,b'Server Key','sha256').digest()
 b64=lambda b:base64.b64encode(b).decode()
 verifier='SCRAM-SHA-256$4096:'+b64(salt)+'$'+b64(stored)+':'+b64(server)
 query("CREATE ROLE ewapp LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD '"+verifier+"'")
elif p['op']=='create_database':
 if db: raise RuntimeError('Database appeared concurrently')
 query("CREATE DATABASE ewlab OWNER ewapp TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C.UTF-8' LC_CTYPE 'C.UTF-8'")
elif p['op']=='inspect':
 result=dict(role=role,database=db,memberships=rows("SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.roleid JOIN pg_roles u ON u.oid=m.member WHERE r.rolname='ewapp' OR u.rolname='ewapp'"))
 result['version']=int(query('SHOW server_version_num'))
 result['settings']={r['name']:r['setting'] for r in rows("SELECT name,setting FROM pg_settings WHERE name IN ('port','password_encryption','ssl','ssl_cert_file','ssl_key_file','data_directory','hba_file','ident_file','unix_socket_directories')")}
 result['credential_valid']=True
 if role:
  scram=rows("SELECT rolpassword LIKE 'SCRAM-SHA-256$%' AS valid FROM pg_authid WHERE rolname='ewapp'")
  result['credential_valid']=len(scram)==1 and scram[0]['valid'] is True
  try:
   result['credential_valid'] &= query('BEGIN READ ONLY; SELECT 1; COMMIT', 'ewlab' if db else 'postgres',True)=='1'
  except Exception: result['credential_valid']=False
 if db:
  schema=rows("SELECT pg_get_userbyid(nspowner) AS owner,nspacl::text AS acl FROM pg_namespace WHERE nspname='public'",'ewlab')
  result['schema']=[schema[0]['owner'],schema[0]['acl']] if len(schema)==1 else None
  objects=rows("SELECT c.relname,pg_get_userbyid(c.relowner) AS owner,c.relacl::text AS acl FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('r','p','i','I','S','v','m','f')",'ewlab')
  result['objects']=[[r['relname'],r['owner'],r['acl']] for r in objects]
  result['default_privileges']=rows('SELECT 1 FROM pg_default_acl','ewlab')
 print(json.dumps(result))
'''


ROLE = dict(rolcanlogin=True, rolinherit=True, rolsuper=False, rolcreatedb=False,
            rolcreaterole=False, rolreplication=False, rolbypassrls=False,
            rolconnlimit=-1, rolvaliduntil=None, rolconfig=None)
DATABASE = dict(owner='ewapp', encoding='UTF8', datcollate='C.UTF-8', datctype='C.UTF-8',
                datallowconn=True, datconnlimit=-1, datacl=None)
PG_SETTINGS = dict(port='5432', password_encryption='scram-sha-256', ssl='on',
    ssl_cert_file='/etc/ssl/certs/ssl-cert-snakeoil.pem', ssl_key_file='/etc/ssl/private/ssl-cert-snakeoil.key',
    data_directory='/var/lib/postgresql/16/main', hba_file='/etc/postgresql/16/main/pg_hba.conf',
    ident_file='/etc/postgresql/16/main/pg_ident.conf', unix_socket_directories='/var/run/postgresql')
BASE_HBA = [('local','all','postgres','peer'), ('local','all','all','peer'),
    ('host','all','all','127.0.0.1/32','scram-sha-256'), ('host','all','all','::1/128','scram-sha-256'),
    ('local','replication','all','peer'), ('host','replication','all','127.0.0.1/32','scram-sha-256'),
    ('host','replication','all','::1/128','scram-sha-256')]
WORKLOAD_HBA = ('host','ewlab','ewapp','192.168.101.11/32','scram-sha-256')


def hba_rows(text):
    result=[]
    for line in text.splitlines():
        row=line.split('#',1)[0].split()
        if not row: continue
        require(row[0] in ('local','host') and len(row)==(4 if row[0]=='local' else 5),
                'Conflicting/unsupported PostgreSQL authentication rule')
        if row[0]=='host':
            try: row[3]=str(ipaddress.ip_network(row[3], strict=False))
            except ValueError: raise Refused('Invalid PostgreSQL HBA address') from None
        result.append(tuple(row))
    require(result in (BASE_HBA, BASE_HBA+[WORKLOAD_HBA]),
            'PostgreSQL authentication rules differ from reviewed local/loopback/workload policy')
    return result


class Adapter:
    def __init__(self, kind):
        self.kind=kind; self.journal=pathlib.Path('/var/lib/ew-provision/'+kind+'.json')
        self.actions=[]

    def pending(self):
        if not self.journal.exists(): return False
        require(not self.journal.is_symlink() and self.journal.stat().st_uid==0 and
                stat.S_IMODE(self.journal.stat().st_mode)==0o600, 'Unsafe bootstrap journal')
        state=json.loads(self.journal.read_text())
        require(state==dict(schema_version=1,kind=self.kind,pending_restart=True), 'Ambiguous bootstrap journal')
        return True

    def mark(self):
        write(self.journal,json.dumps(dict(schema_version=1,kind=self.kind,pending_restart=True))+'\n')

    def clear(self):
        if self.journal.exists(): self.journal.unlink()

    def dependencies(self, tools, modules, package, version):
        missing=[t for t in tools if not shutil.which(t)]
        for module in modules:
            try: __import__(module)
            except Exception: missing.append(module)
        if missing:
            raise MissingDependencies('Missing baked dependencies (no installation attempted): '+', '.join(missing))
        installed=run(['dpkg-query','-W','-f=${Version}',package])
        require(installed==version, 'Incompatible baked package '+package+'; expected '+version)

    def active(self):
        p=subprocess.run(['systemctl','is-active','--quiet',self.unit],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=10)
        require(p.returncode in (0,3), 'Unknown dependency service state: '+self.unit)
        return p.returncode==0

    def enabled(self):
        p=subprocess.run(['systemctl','is-enabled','--quiet',self.unit],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=10)
        require(p.returncode in (0,1), 'Unknown dependency enablement: '+self.unit)
        return p.returncode==0

    def service(self, verb):
        run(['systemctl',verb,self.unit]); self.actions.append(verb+':'+self.unit)

    def execute(self, action):
        missing=self.inspect()
        if action=='check':
            return dict(status='CHANGE_REQUIRED' if missing else 'PASS',missing=missing,
                        implementation='reconstructed-reviewed',kind=self.kind)
        if not missing:
            self.clear()
            return dict(status='PASS',changed=False,actions=[],implementation='reconstructed-reviewed')
        self.prepare()
        if not self.enabled(): self.service('enable')
        if not self.active(): self.service('start')
        # Revalidate roles/credentials after starting a baked stopped service and
        # before modifying any existing configuration or owned logical resources.
        self.inspect()
        self.configure()
        self.inspect()
        self.create_missing()
        require(not self.inspect(), 'Bootstrap readiness still incomplete')
        self.clear()
        return dict(status='PASS',changed=bool(self.actions),actions=self.actions,
                    implementation='reconstructed-reviewed')

    def prepare(self):
        pass


class PostgreSQL(Adapter):
    unit='postgresql@16-main.service'
    config=pathlib.Path('/etc/postgresql/16/main/postgresql.conf')
    hba=pathlib.Path('/etc/postgresql/16/main/pg_hba.conf')
    binary='/usr/lib/postgresql/16/bin/postgres'
    target='127.0.0.1,192.168.102.12'

    def __init__(self): super().__init__('postgresql16')

    def cluster_intent(self):
        path=self.journal.with_name('postgresql16-cluster.json')
        if path.exists():
            require(not path.is_symlink() and path.stat().st_uid==0 and stat.S_IMODE(path.stat().st_mode)==0o600 and
                    json.loads(path.read_text())==dict(schema_version=1,pending_cluster_creation=True),
                    'Ambiguous PostgreSQL cluster creation intent')
            return True
        return False

    def rpc(self, op):
        return json.loads(run(['sudo','-n','-u','postgres','python3','-c',PG_CODE],
            json.dumps(dict(op=op,password=self.secret))) or '{}')

    def inspect(self):
        self.dependencies(['sudo','systemctl','dpkg-query','psql','pg_lsclusters','pg_createcluster',self.binary],[],
                          'postgresql-16','16.15-0ubuntu0.24.04.1')
        self.secret=password('/etc/ew-lab/db-password')
        self.cluster_intent()
        clusters=[line.split()[:2] for line in run(['pg_lsclusters','--no-header']).splitlines() if line.strip()]
        require(clusters in ([],[['16','main']]), 'Unexpected/ambiguous PostgreSQL clusters; no replacement permitted')
        self.cluster_missing=not clusters
        if self.cluster_missing:
            require(not self.config.parent.exists() and not pathlib.Path('/var/lib/postgresql/16/main').exists(),
                    'PostgreSQL cluster data/config exists without a catalog entry; no initdb permitted')
            return ['cluster_16_main']
        require(self.config.is_file() and not self.config.is_symlink() and self.hba.is_file() and
                not self.hba.is_symlink(), 'PostgreSQL 16/main cluster/config incomplete; no replacement permitted')
        if self.cluster_intent(): return ['cluster_finalize']
        self.pending()
        self.settings={name:run(['sudo','-n','-u','postgres',self.binary,'-D','/var/lib/postgresql/16/main',
            '-c','config_file='+str(self.config),'-C',name]) for name in ['listen_addresses',*PG_SETTINGS]}
        for name,value in PG_SETTINGS.items():
            require(self.settings[name]==value,'Conflicting PostgreSQL setting: '+name)
        require(self.settings['listen_addresses'] in (self.target,'localhost','127.0.0.1'),
                'Conflicting PostgreSQL listen_addresses')
        self.hba_text=self.hba.read_text(); rules=hba_rows(self.hba_text)
        missing=[]
        if self.settings['listen_addresses']!=self.target: missing.append('listen_addresses')
        if WORKLOAD_HBA not in rules: missing.append('workload_hba')
        if not self.enabled(): missing.append('service_enabled')
        if not self.active(): return missing+['service_active','database_inspection_pending']
        self.db=self.rpc('inspect')
        require(self.db['version']==160015, 'Incompatible PostgreSQL runtime version')
        require(self.db['settings']==PG_SETTINGS, 'Conflicting effective PostgreSQL SSL/auth/cluster configuration')
        require(len(self.db['role'])<=1 and len(self.db['database'])<=1, 'Ambiguous PostgreSQL catalog')
        if self.db['role']:
            require(self.db['role'][0]==ROLE and not self.db['memberships'], 'Conflicting ewapp PostgreSQL role/memberships')
            require(self.db['credential_valid'], 'Existing ewapp PostgreSQL credentials conflict or authentication unavailable')
        else: missing.append('role')
        if self.db['database']:
            require(self.db['role'] and self.db['database'][0]==DATABASE, 'Conflicting ewlab PostgreSQL database/ownership/permissions')
            schema=self.db.get('schema')
            require(schema and schema[0]=='pg_database_owner' and isinstance(schema[1],str) and
                    set(schema[1].strip('{}').split(','))=={'pg_database_owner=UC/pg_database_owner','=U/pg_database_owner'},
                    'Conflicting PostgreSQL 16 public-schema permissions/ownership')
            require(all(owner=='ewapp' and acl is None for _,owner,acl in self.db['objects']) and
                    not self.db['default_privileges'], 'Conflicting application object ownership/permissions')
        else: missing.append('database')
        effective=run(['sudo','-n','-u','postgres','psql','-XqAt','-h','/var/run/postgresql','-p','5432','-d','postgres','-c','SHOW listen_addresses'])
        require(effective in (self.target,'localhost','127.0.0.1'), 'Conflicting effective PostgreSQL listener')
        if effective!=self.target: missing.append('listener_restart')
        # HBA changes need a reload/restart even if its on-disk text now matches.
        if self.pending(): missing.append('pending_restart')
        return missing

    def prepare(self):
        intent=self.journal.with_name('postgresql16-cluster.json')
        if self.cluster_missing:
            # No pre-existing data/config/other clusters: the baked Ubuntu tool
            # initializes only this missing cluster; never runs apt or downloads.
            write(intent,json.dumps(dict(schema_version=1,pending_cluster_creation=True))+'\n')
            run(['pg_createcluster','16','main','--start-conf=auto','--locale=C.UTF-8','--encoding=UTF8',
                 '--','--auth-local=peer','--auth-host=scram-sha-256'])
            self.actions.append('create:cluster_16_main')
        if self.cluster_missing or self.cluster_intent():
            # Explicit initdb authentication options retain initdb's six rules.
            # Add the reviewed Ubuntu admin-peer rule only to this exact newly
            # created/journaled cluster, before its first service start.
            text=self.hba.read_text()
            try: hba_rows(text)
            except Refused:
                proposed='local all postgres peer\n'+text
                hba_rows(proposed)  # refuses any other authentication change
                write(self.hba,proposed)
                self.actions.append('configure:cluster_authentication')
            intent.unlink()

    def configure(self):
        if self.settings['listen_addresses']!=self.target:
            text=self.config.read_text()
            matches=list(re.finditer(r'(?m)^\s*listen_addresses\s*=.*$',text))
            require(len(matches)<=1, 'Ambiguous listen_addresses assignments')
            line="listen_addresses = '"+self.target+"'"
            text=re.sub(r'(?m)^\s*listen_addresses\s*=.*$',line,text) if matches else text.rstrip()+'\n'+line+'\n'
            self.mark(); write(self.config,text); self.actions.append('configure:listen_addresses')
        if WORKLOAD_HBA not in hba_rows(self.hba_text):
            self.mark(); write(self.hba,self.hba_text.rstrip()+'\n'+' '.join(WORKLOAD_HBA)+'\n')
            self.actions.append('configure:workload_hba')
        if self.pending(): self.service('restart'); self.clear()

    def create_missing(self):
        if not self.db['role']: self.rpc('create_role'); self.actions.append('create:ewapp')
        # Reinspection makes interruption after CREATE ROLE safely resumable.
        self.inspect()
        if not self.db['database']: self.rpc('create_database'); self.actions.append('create:ewlab')


class RabbitMQ(Adapter):
    unit='rabbitmq-server.service'
    config=pathlib.Path('/etc/rabbitmq/rabbitmq.conf')
    target='{ok,[{"192.168.102.11",5672}]}'
    baseline='{ok,[5672]}'

    def __init__(self): super().__init__('rabbitmq312')

    def ctl(self,*args,secret=False,structured=False):
        prefix=['sudo','-n','-u','rabbitmq','rabbitmqctl','-q']
        if structured: prefix+=['--formatter=json']
        text=run(prefix+list(args), self.secret+'\n' if secret else None)
        return json.loads(text) if structured else text

    def inspect(self):
        self.dependencies(['sudo','systemctl','dpkg-query','rabbitmqctl','rabbitmq-diagnostics','rabbitmq-plugins'],
                          [],'rabbitmq-server','3.12.1-1ubuntu1.6')
        self.secret=password('/etc/ew-lab/mq-password'); self.pending()
        require(not self.config.is_symlink(), 'Unsafe RabbitMQ config path')
        text=self.config.read_text() if self.config.exists() else ''
        self.tcp=[]
        for line in text.splitlines():
            line=line.split('#',1)[0].strip()
            if line.startswith('listeners.tcp.'):
                k,sep,v=line.partition('='); require(sep, 'Malformed RabbitMQ TCP listener')
                self.tcp.append((k.strip(),v.strip()))
        require(not self.tcp or len(self.tcp)==1 and self.tcp[0][1]=='192.168.102.11:5672',
                'Conflicting RabbitMQ listener configuration')
        missing=[]
        if not self.tcp: missing.append('listener_configuration')
        if not self.enabled(): missing.append('service_enabled')
        if not self.active(): return missing+['service_active','broker_inspection_pending']
        version=run(['sudo','-n','-u','rabbitmq','rabbitmq-diagnostics','-q','server_version'])
        require(version=='3.12.1','Incompatible RabbitMQ runtime version')
        settings={'ssl_listeners':'{ok,[]}', 'auth_mechanisms':"{ok,['PLAIN','AMQPLAIN']}",
                  'auth_backends':'{ok,[rabbit_auth_backend_internal]}','loopback_users':'{ok,[<<"guest">>]}'}
        for key,value in settings.items():
            require(self.ctl('eval','application:get_env(rabbit,'+key+').').replace(' ','')==value.replace(' ',''),
                    'Conflicting RabbitMQ effective setting: '+key)
        require(not run(['sudo','-n','-u','rabbitmq','rabbitmq-plugins','-q','list','-e','-m']),
                'Enabled RabbitMQ plugins differ from reviewed configuration')
        effective=self.ctl('eval','application:get_env(rabbit,tcp_listeners).').replace(' ','')
        require(effective in (self.target,self.baseline,'{ok,[{auto,5672}]}'), 'Conflicting RabbitMQ effective listener')
        if effective!=self.target: missing.append('listener_restart')
        self.users=self.ctl('list_users',structured=True)
        self.vhosts=self.ctl('list_vhosts','name','tracing',structured=True)
        users=[u for u in self.users if u['user']=='ewapp']; guests=[u for u in self.users if u['user']=='guest']
        require(len(guests)==1 and guests[0]['tags']==['administrator'], 'Existing RabbitMQ guest account differs; refusing to change it')
        require(len(users)<=1, 'Ambiguous RabbitMQ ewapp user')
        if users:
            require(users[0]['tags']==[], 'Conflicting RabbitMQ ewapp tags')
            self.ctl('authenticate_user','ewapp',secret=True)
            perms=self.ctl('list_user_permissions','ewapp',structured=True)
            require(all(p['vhost']=='ewlab' and all(p[k]=='.*' for k in ('configure','write','read')) for p in perms)
                    and len(perms)<=1, 'Conflicting RabbitMQ ewapp permissions')
            self.permission=bool(perms)
            require(not self.ctl('list_topic_permissions','-p','ewlab',structured=True) if any(v['name']=='ewlab' for v in self.vhosts) else True,
                    'Conflicting RabbitMQ topic permissions')
        else: missing.append('user'); self.permission=False
        vhosts=[v for v in self.vhosts if v['name']=='ewlab']; require(len(vhosts)<=1, 'Ambiguous RabbitMQ ewlab vhost')
        if vhosts:
            require(vhosts[0]['tracing'] is False, 'RabbitMQ ewlab tracing conflicts')
            perms=self.ctl('list_permissions','-p','ewlab',structured=True)
            require(all(p['user']=='ewapp' and all(p[k]=='.*' for k in ('configure','write','read')) for p in perms)
                    and len(perms)<=1, 'Conflicting RabbitMQ ewlab permissions')
        else: missing.append('vhost')
        if not self.permission: missing.append('permissions')
        if self.pending(): missing.append('pending_restart')
        return missing

    def configure(self):
        if not self.tcp:
            text=self.config.read_text() if self.config.exists() else ''
            import grp
            self.mark(); write(self.config,text.rstrip()+'\nlisteners.tcp.1 = 192.168.102.11:5672\n',
                               gid=grp.getgrnam('rabbitmq').gr_gid,mode=0o640)
            self.actions.append('configure:listener')
        if self.pending(): self.service('restart'); self.clear()

    def create_missing(self):
        if not any(u['user']=='ewapp' for u in self.users):
            self.ctl('add_user','ewapp',secret=True); self.actions.append('create:ewapp')
        if not any(v['name']=='ewlab' for v in self.vhosts):
            self.ctl('add_vhost','ewlab'); self.actions.append('create:ewlab')
        if not self.permission:
            self.ctl('set_permissions','-p','ewlab','ewapp','.*','.*','.*'); self.actions.append('grant:ewlab')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('check','apply')); p.add_argument('role',choices=('ew-db','ew-queue'))
    args=p.parse_args()
    try:
        require(os.geteuid()==0, 'Adapter must run as root through verified transport')
        adapter=PostgreSQL() if args.role=='ew-db' else RabbitMQ()
        if args.action=='apply':
            # check never creates a directory, lock, journal, config or resource.
            adapter.journal.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            with (adapter.journal.parent/(adapter.kind+'.lock')).open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                result=adapter.execute(args.action)
        else: result=adapter.execute(args.action)
        print(json.dumps(result))
    except Exception as exc:
        result=dict(status='MISSING_DEPENDENCIES' if isinstance(exc,MissingDependencies) else
                    'CONFLICT' if isinstance(exc,Refused) else 'UNAVAILABLE',
                    reason=str(exc) if isinstance(exc,Refused) else type(exc).__name__,
                    implementation='reconstructed-reviewed')
        print(json.dumps(result))
        return 0 if args.action=='check' else 1
    return 0


if __name__=='__main__': sys.exit(main())
