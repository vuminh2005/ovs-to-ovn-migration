"""Offline stateful adapter regressions; every service/DB/broker call is mocked."""
import contextlib
import copy
import importlib.util
import io
import json
import os
import pathlib
import re
import stat
import sys
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock,patch

ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
spec=importlib.util.spec_from_file_location('reconstructed_adapter',ROOT/'workloads/ew-bootstrap/adapter.py')
a=importlib.util.module_from_spec(spec); spec.loader.exec_module(a)
import ew_provision as provision


class Model:
    def __init__(self, directory, kind, healthy=False):
        self.root=pathlib.Path(directory); self.kind=kind; self.commands=[]; self.actions=[]
        self.active=True; self.enabled=True; self.fail=None; self.credential=True
        self.rows=[{'task_id':'old','result':'preserve'}]; self.messages=['existing-message']
        self.role=[copy.deepcopy(a.ROLE)] if healthy else []
        self.database=[copy.deepcopy(a.DATABASE)] if healthy else []
        self.schema=['pg_database_owner','{pg_database_owner=UC/pg_database_owner,=U/pg_database_owner}']
        self.objects=[['ew_jobs','ewapp',None],['ew_jobs_run_idx','ewapp',None]]
        self.memberships=[]; self.defaults=[]
        self.users=[dict(user='guest',tags=['administrator'])]+([dict(user='ewapp',tags=[])] if healthy else [])
        self.vhosts=[dict(name='/',tracing=False)]+([dict(name='ewlab',tracing=False)] if healthy else [])
        self.perms=[dict(vhost='ewlab',configure='.*',write='.*',read='.*')] if healthy else []
        self.setting_overrides={}; self.pg_settings=dict(a.PG_SETTINGS)
        self.running=a.PostgreSQL.target if healthy and kind=='pg' else a.RabbitMQ.target if healthy else 'localhost' if kind=='pg' else a.RabbitMQ.baseline
        self.config=self.root/'config'; self.hba=self.root/'hba'
        self.config.write_text("listen_addresses = '"+ (a.PostgreSQL.target if healthy else 'localhost')+"'\n" if kind=='pg' else 'listeners.tcp.1 = 192.168.102.11:5672\n' if healthy else '# stock defaults\n')
        self.hba.write_text('\n'.join(' '.join(r) for r in a.BASE_HBA+([a.WORKLOAD_HBA] if healthy else []))+'\n')

    def command(self,argv,**kw):
        self.commands.append((list(argv),kw.get('input')))
        if argv[0]=='systemctl':
            verb=argv[1]
            if verb=='is-active': return NS(returncode=0 if self.active else 3,stdout='',stderr='')
            if verb=='is-enabled': return NS(returncode=0 if self.enabled else 1,stdout='',stderr='')
            self.actions.append(verb)
            if self.fail==verb:
                self.fail=None; return NS(returncode=1,stdout='PRIVATE FAILURE',stderr='PRIVATE FAILURE')
            if verb=='enable': self.enabled=True
            if verb in ('start','restart'):
                self.active=True; self.running=self.configured()
            return NS(returncode=0,stdout='',stderr='')
        if argv[0]=='dpkg-query': return NS(returncode=0,stdout='16.15-0ubuntu0.24.04.1' if self.kind=='pg' else '3.12.1-1ubuntu1.6',stderr='')
        if argv[0]=='pg_lsclusters': return NS(returncode=0,stdout='16 main 5432 online postgres /var/lib/postgresql/16/main /var/log/postgresql/postgresql-16-main.log',stderr='')
        if '-C' in argv:
            key=argv[-1]; value=self.configured() if key=='listen_addresses' else self.pg_settings[key]
            return NS(returncode=0,stdout=value,stderr='')
        if 'psql' in argv: return NS(returncode=0,stdout=self.running,stderr='')
        if 'rabbitmq-diagnostics' in argv: return NS(returncode=0,stdout='3.12.1',stderr='')
        if 'rabbitmq-plugins' in argv: return NS(returncode=0,stdout='',stderr='')
        if 'rabbitmqctl' in argv:
            args=argv[argv.index('-q')+1:]
            if args[0]=='--formatter=json': args=args[1:]
            op=args[0]; value=None
            if op=='eval':
                key=args[1].split(',')[1].split(')')[0]
                value=self.setting_overrides.get(key,dict(tcp_listeners=self.running,ssl_listeners='{ok,[]}',
                    auth_mechanisms="{ok,['PLAIN','AMQPLAIN']}",auth_backends='{ok,[rabbit_auth_backend_internal]}',loopback_users='{ok,[<<"guest">>]}')[key])
            elif op=='list_users': value=self.users
            elif op=='list_vhosts': value=self.vhosts
            elif op=='list_user_permissions': value=self.perms
            elif op=='list_permissions': value=[dict(user='ewapp',**{k:p[k] for k in ('configure','write','read')}) for p in self.perms]
            elif op=='list_topic_permissions': value=[]
            elif op=='authenticate_user':
                return NS(returncode=0 if self.credential else 65,stdout='Success' if self.credential else 'PRIVATE SECRET',stderr='PRIVATE SECRET')
            elif op=='add_user': self.users.append(dict(user='ewapp',tags=[])); self.actions.append('create_user')
            elif op=='add_vhost': self.vhosts.append(dict(name='ewlab',tracing=False)); self.actions.append('create_vhost')
            elif op=='set_permissions': self.perms=[dict(vhost='ewlab',configure='.*',write='.*',read='.*')]; self.actions.append('grant')
            else: raise AssertionError(argv)
            if self.fail==op:
                self.fail=None; return NS(returncode=1,stdout='PRIVATE FAILURE',stderr='PRIVATE FAILURE')
            return NS(returncode=0,stdout=value if isinstance(value,str) else json.dumps(value) if value is not None else '',stderr='')
        raise AssertionError(argv)

    def configured(self):
        if self.kind=='mq': return a.RabbitMQ.target if 'listeners.tcp.1' in self.config.read_text() else a.RabbitMQ.baseline
        return re.search("listen_addresses = '([^']+)'",self.config.read_text())[1]

    def rpc(self, op):
        if op=='inspect': return dict(role=copy.deepcopy(self.role),database=copy.deepcopy(self.database),memberships=self.memberships,
            credential_valid=self.credential,schema=self.schema,objects=self.objects,default_privileges=self.defaults,version=160015,settings=dict(a.PG_SETTINGS))
        if op=='create_role':
            assert not self.role; self.role=[copy.deepcopy(a.ROLE)]; self.actions.append(op)
        elif op=='create_database':
            assert not self.database; self.database=[copy.deepcopy(a.DATABASE)]; self.actions.append(op)
        else: raise AssertionError(op)
        if self.fail==op:
            self.fail=None; raise a.Refused('Injected interruption after durable create')
        return {}

    def adapter(self):
        obj=a.PostgreSQL() if self.kind=='pg' else a.RabbitMQ()
        obj.config=self.config; obj.journal=self.root/'journal.json'
        if self.kind=='pg': obj.hba=self.hba; obj.rpc=self.rpc
        # Real JSON persistence is exercised without requiring test runner root.
        def pending():
            if not obj.journal.exists(): return False
            assert json.loads(obj.journal.read_text())==dict(schema_version=1,kind=obj.kind,pending_restart=True)
            return True
        obj.pending=pending
        return obj


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.directory=pathlib.Path(self.temp.name)
        self.stack=contextlib.ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(a.shutil,'which',side_effect=lambda p:p))
        self.stack.enter_context(patch.dict(sys.modules,{'psycopg2':Mock(),'pika':Mock()}))
        self.stack.enter_context(patch.object(a,'password',return_value='a'*48))
        self.stack.enter_context(patch.object(a.os,'chown'))
        self.stack.enter_context(patch('grp.getgrnam',return_value=NS(gr_gid=4242)))

    def model(self,kind,healthy=False):
        m=Model(self.directory,kind,healthy)
        self.stack.enter_context(patch.object(a.subprocess,'run',side_effect=m.command))
        return m

    def test_fresh_postgresql_and_unchanged_rerun_preserve_data(self):
        m=self.model('pg'); obj=m.adapter()
        check=obj.execute('check'); self.assertEqual(check['status'],'CHANGE_REQUIRED')
        self.assertFalse(m.actions); self.assertFalse(obj.journal.exists())
        first=obj.execute('apply'); self.assertTrue(first['changed'])
        self.assertEqual(m.actions,['restart','create_role','create_database'])
        before={p:p.read_bytes() for p in (m.config,m.hba)}; mtimes={p:p.stat().st_mtime_ns for p in before}
        m.actions.clear(); self.assertFalse(m.adapter().execute('apply')['changed']); self.assertFalse(m.actions)
        self.assertEqual({p:p.read_bytes() for p in before},before)
        self.assertEqual({p:p.stat().st_mtime_ns for p in before},mtimes)
        self.assertEqual(m.rows,[{'task_id':'old','result':'preserve'}])
        self.assertEqual(a.hba_rows(m.hba.read_text()),a.BASE_HBA+[a.WORKLOAD_HBA])

    def test_fresh_rabbitmq_and_unchanged_rerun_preserve_guest_and_messages(self):
        m=self.model('mq'); self.assertEqual(m.adapter().execute('check')['status'],'CHANGE_REQUIRED')
        self.assertFalse(m.actions)
        self.assertTrue(m.adapter().execute('apply')['changed'])
        self.assertEqual(m.actions,['restart','create_user','create_vhost','grant'])
        before=m.config.read_bytes(); stamp=m.config.stat().st_mtime_ns; m.actions.clear()
        self.assertFalse(m.adapter().execute('apply')['changed']); self.assertFalse(m.actions)
        self.assertEqual(m.config.read_bytes(),before); self.assertEqual(m.config.stat().st_mtime_ns,stamp)
        self.assertEqual(m.messages,['existing-message']); self.assertEqual(m.users[0],dict(user='guest',tags=['administrator']))
        for argv,data in m.commands:
            self.assertNotIn('a'*48,repr(argv))
            if 'add_user' in argv or 'authenticate_user' in argv: self.assertEqual(data,'a'*48+'\n')

    def test_postgresql_conflicts_block_all_mutation(self):
        m=self.model('pg',True)
        cases=[('role',dict(a.ROLE,rolsuper=True)),('role',dict(a.ROLE,rolinherit=False)),
               ('database',dict(a.DATABASE,owner='postgres')),('database',dict(a.DATABASE,datacl='{extra=CTc/ewapp}'))]
        for key,value in cases:
            old=getattr(m,key); setattr(m,key,[value])
            with self.subTest(key=key,value=value),self.assertRaises(a.Refused): m.adapter().execute('apply')
            setattr(m,key,old)
        for key,value in [('credential',False),('schema',['ewapp',None]),('objects',[['ew_jobs','postgres',None]]),('memberships',[{'role':'other'}])]:
            old=getattr(m,key); setattr(m,key,value)
            with self.subTest(key=key),self.assertRaises(a.Refused): m.adapter().execute('apply')
            setattr(m,key,old)
        self.assertFalse(m.actions)

    def test_rabbitmq_conflicts_block_all_mutation(self):
        m=self.model('mq',True)
        cases=[('users',[dict(user='guest',tags=['administrator']),dict(user='ewapp',tags=['administrator'])]),
            ('vhosts',[dict(name='ewlab',tracing=True)]),('perms',[dict(vhost='ewlab',configure='.*',write='limited',read='.*')]),
            ('perms',[dict(vhost='other',configure='.*',write='.*',read='.*')]),('credential',False)]
        for key,value in cases:
            old=getattr(m,key); setattr(m,key,value)
            with self.subTest(key=key),self.assertRaises(a.Refused): m.adapter().execute('apply')
            setattr(m,key,old)
        for name,value in [('ssl_listeners','{ok,[5671]}'),('loopback_users','{ok,[]}'),('auth_backends','{ok,[ldap]}')]:
            m.setting_overrides={name:value}
            with self.subTest(setting=name),self.assertRaises(a.Refused): m.adapter().execute('apply')
        self.assertFalse(m.actions)

    def test_postgresql_interrupted_role_create_does_not_recreate_or_reset(self):
        m=self.model('pg'); m.fail='create_role'
        with self.assertRaises(a.Refused): m.adapter().execute('apply')
        self.assertTrue(m.role); self.assertFalse(m.database)
        m.adapter().execute('apply')
        self.assertEqual(m.actions.count('create_role'),1); self.assertEqual(m.actions.count('create_database'),1)
        self.assertEqual(m.rows,[{'task_id':'old','result':'preserve'}])

    def test_rabbitmq_interrupted_user_create_does_not_replace_password(self):
        m=self.model('mq'); m.fail='add_user'
        with self.assertRaises(a.Refused): m.adapter().execute('apply')
        m.adapter().execute('apply')
        self.assertEqual(m.actions.count('create_user'),1); self.assertEqual(m.actions.count('grant'),1)
        self.assertFalse(any('change_password' in argv for argv,_ in m.commands))

    def test_pending_restart_survives_failure_after_config_write_both_adapters(self):
        for kind in ('pg','mq'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as d:
                m=Model(d,kind); m.fail='restart'
                with patch.object(a.subprocess,'run',side_effect=m.command):
                    obj=m.adapter()
                    with self.assertRaises(a.Refused): obj.execute('apply')
                    self.assertTrue(obj.journal.exists()); stamp=m.config.stat().st_mtime_ns
                    self.assertEqual(m.adapter().execute('check')['status'],'CHANGE_REQUIRED')
                    m.adapter().execute('apply'); self.assertFalse(obj.journal.exists())
                    self.assertEqual(m.config.stat().st_mtime_ns,stamp)
                    count=m.actions.count('restart'); m.adapter().execute('apply')
                    self.assertEqual(m.actions.count('restart'),count)

    def test_stopped_baked_service_is_explicitly_pending_inspection(self):
        m=self.model('pg',True); m.active=False; m.enabled=False
        check=m.adapter().execute('check'); self.assertIn('database_inspection_pending',check['missing'])
        self.assertFalse(m.actions)
        m.adapter().execute('apply'); self.assertEqual(m.actions,['enable','start'])

    def test_missing_dependency_and_wrong_package_fail_without_mutation(self):
        m=self.model('pg',True)
        with patch.object(a.shutil,'which',return_value=None),self.assertRaisesRegex(a.Refused,'Missing baked dependencies'): m.adapter().execute('check')
        with patch.object(a,'run',return_value='16.14-wrong'),self.assertRaisesRegex(a.Refused,'Incompatible baked package'): m.adapter().execute('apply')
        self.assertFalse(m.actions)

    def test_hba_unrelated_rules_and_duplicate_workload_are_conflicts(self):
        baseline='\n'.join(' '.join(r) for r in a.BASE_HBA+[a.WORKLOAD_HBA])
        for extra in ('host all all 0.0.0.0/0 trust', ' '.join(a.WORKLOAD_HBA), 'include bad.conf'):
            with self.subTest(extra=extra),self.assertRaises(a.Refused): a.hba_rows(baseline+'\n'+extra)

    def test_read_only_check_no_journal_config_or_service_mutations(self):
        for kind in ('pg','mq'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as d:
                m=Model(d,kind,True)
                with patch.object(a.subprocess,'run',side_effect=m.command),patch.object(a,'write') as write:
                    self.assertEqual(m.adapter().execute('check')['status'],'PASS')
                    write.assert_not_called(); self.assertFalse(m.actions)

    def test_pg_private_rpc_has_no_secret_in_argv_or_result(self):
        obj=a.PostgreSQL(); obj.secret='a'*48
        with patch.object(a,'run',return_value='{}') as runner:
            obj.rpc('create_role')
        argv,payload=runner.call_args.args
        self.assertNotIn(obj.secret,repr(argv)); self.assertEqual(json.loads(payload)['password'],obj.secret)
        for forbidden in ('ALTER ROLE','DROP ','TRUNCATE','DELETE FROM','INSERT INTO','UPDATE '): self.assertNotIn(forbidden,a.PG_CODE)
        self.assertNotIn('print(verifier)',a.PG_CODE)

    def test_json_failure_never_contains_subprocess_output_or_secrets(self):
        with patch.object(sys,'argv',['adapter.py','check','ew-db']),patch.object(a.os,'geteuid',return_value=0), \
             patch.object(a.PostgreSQL,'inspect',side_effect=ValueError('a'*48)),contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(a.main(),0)
        self.assertEqual(json.loads(output.getvalue())['status'],'UNAVAILABLE'); self.assertNotIn('a'*48,output.getvalue())

    def test_missing_cluster_only_created_when_data_and_configuration_are_absent(self):
        obj=a.PostgreSQL(); obj.config=self.directory/'absent'/'postgresql.conf'
        obj.hba=obj.config.parent/'pg_hba.conf'; obj.dependencies=Mock(); obj.journal=self.directory/'journal.json'
        original=pathlib.Path.exists
        def exists(path):
            if str(path)=='/var/lib/postgresql/16/main': return False
            return original(path)
        def command(argv):
            if argv[0]=='pg_createcluster':
                self.assertTrue(obj.journal.with_name('postgresql16-cluster.json').exists())
                obj.hba.parent.mkdir(); obj.hba.write_text('\n'.join(' '.join(r) for r in a.BASE_HBA[1:])+'\n')
            return ''
        with patch.object(a,'run',side_effect=command) as runner,patch.object(pathlib.Path,'exists',exists):
            self.assertEqual(obj.execute('check')['missing'],['cluster_16_main'])
            self.assertEqual(runner.call_count,1)
            obj.prepare()
            self.assertEqual(runner.call_args.args[0],['pg_createcluster','16','main','--start-conf=auto','--locale=C.UTF-8','--encoding=UTF8','--','--auth-local=peer','--auth-host=scram-sha-256'])
            self.assertEqual(a.hba_rows(obj.hba.read_text()),a.BASE_HBA)
            self.assertFalse(obj.journal.with_name('postgresql16-cluster.json').exists())
        obj.cluster_intent=Mock(return_value=False)
        with patch.object(a,'run',return_value=''),patch.object(pathlib.Path,'exists',return_value=True),self.assertRaisesRegex(a.Refused,'no initdb'):
            obj.inspect()

    def test_interrupted_new_cluster_finalization_never_repeats_initdb(self):
        m=self.model('pg'); obj=m.adapter()
        m.hba.write_text('\n'.join(' '.join(r) for r in a.BASE_HBA[1:])+'\n')
        intent=obj.journal.with_name('postgresql16-cluster.json')
        intent.write_text(json.dumps(dict(schema_version=1,pending_cluster_creation=True))); intent.chmod(0o600)
        original=pathlib.Path.stat
        def stat_as_root(p,*args,**kw):
            s=original(p,*args,**kw)
            if p==intent: return NS(st_mode=s.st_mode,st_uid=0)
            return s
        with patch.object(pathlib.Path,'stat',stat_as_root):
            self.assertEqual(obj.execute('check')['missing'],['cluster_finalize'])
            obj.execute('apply')
        self.assertFalse(intent.exists()); self.assertEqual(a.hba_rows(m.hba.read_text()),a.BASE_HBA+[a.WORKLOAD_HBA])
        self.assertFalse(any(argv[0]=='pg_createcluster' for argv,_ in m.commands))

    def test_unexpected_cluster_and_configuration_conflicts_block_mutation(self):
        m=self.model('pg',True)
        obj=m.adapter(); obj.dependencies=Mock()
        with patch.object(a,'run',return_value='15 other 5433 online'),self.assertRaisesRegex(a.Refused,'ambiguous PostgreSQL clusters'):
            obj.execute('apply')
        m.pg_settings['ssl']='off'
        with self.assertRaisesRegex(a.Refused,'setting: ssl'): m.adapter().execute('apply')
        self.assertFalse(m.actions)

    def test_private_psql_helper_generates_scram_without_password_in_sql_or_argv(self):
        import base64,hashlib,hmac
        calls=[]
        def run(argv,**kw):
            calls.append((argv,kw))
            text=kw['input']
            return NS(returncode=0,stdout='[]' if text.startswith('SELECT') else '',stderr='')
        with patch('subprocess.run',side_effect=run),patch.object(sys,'stdin',io.StringIO(json.dumps(dict(op='create_role',password='a'*48)))), \
             patch('os.urandom',return_value=b'fixed-salt-16byt!'),contextlib.redirect_stdout(io.StringIO()) as out:
            exec(compile(a.PG_CODE,'<pg-helper>','exec'),{})
        self.assertEqual(out.getvalue(),'')
        statements=[kw['input'] for argv,kw in calls]
        create=next(q for q in statements if q.startswith('CREATE ROLE'))
        verifier=create.split("PASSWORD '",1)[1].split("'",1)[0]
        algorithm,params,keys=verifier.split('$'); iterations,salt=params.split(':'); stored,server=keys.split(':')
        self.assertEqual(algorithm,'SCRAM-SHA-256')
        salted=hashlib.pbkdf2_hmac('sha256',b'a'*48,base64.b64decode(salt),int(iterations))
        self.assertEqual(base64.b64decode(stored),hashlib.sha256(hmac.new(salted,b'Client Key','sha256').digest()).digest())
        self.assertEqual(base64.b64decode(server),hmac.new(salted,b'Server Key','sha256').digest())
        self.assertNotIn('a'*48,repr(statements)); self.assertTrue(all('a'*48 not in repr(argv) for argv,_ in calls))
        self.assertTrue(all('PGPASSWORD' not in kw['env'] for _,kw in calls))

    def test_private_psql_helper_readonly_auth_failure_never_returns_secret_or_hash(self):
        calls=[]
        def command(argv,**kw):
            text=kw['input']; calls.append((argv,kw))
            if kw['env']['PGUSER']=='ewapp': return NS(returncode=2,stdout='a'*48,stderr='secret verifier')
            if 'FROM pg_roles WHERE' in text: value=[copy.deepcopy(a.ROLE)]
            elif 'FROM pg_database WHERE' in text: value=[]
            elif 'SHOW server_version_num' in text: return NS(returncode=0,stdout='160015',stderr='')
            elif 'FROM pg_settings' in text: value=[dict(name=k,setting=v) for k,v in a.PG_SETTINGS.items()]
            elif 'rolpassword LIKE' in text: value=[dict(valid=True)]
            else: value=[]
            return NS(returncode=0,stdout=json.dumps(value),stderr='')
        with patch('subprocess.run',side_effect=command),patch.object(sys,'stdin',io.StringIO(json.dumps(dict(op='inspect',password='a'*48)))),contextlib.redirect_stdout(io.StringIO()) as out:
            exec(compile(a.PG_CODE,'<pg-helper>','exec'),{})
        result=json.loads(out.getvalue()); self.assertFalse(result['credential_valid'])
        self.assertNotIn('a'*48,out.getvalue()); self.assertNotIn('secret verifier',out.getvalue())
        auth=[kw for _,kw in calls if kw['env']['PGUSER']=='ewapp']; self.assertEqual(len(auth),1)
        self.assertTrue(auth[0]['input'].startswith('BEGIN READ ONLY; SELECT 1; COMMIT'))
        self.assertEqual(auth[0]['env']['PGPASSWORD'],'a'*48)
        self.assertTrue(all('a'*48 not in repr(argv) for argv,_ in calls))

    def test_missing_dependencies_emit_distinct_json_status(self):
        with patch.object(sys,'argv',['adapter.py','check','ew-db']),patch.object(a.os,'geteuid',return_value=0), \
             patch.object(a.PostgreSQL,'inspect',side_effect=a.MissingDependencies('Missing baked dependencies: psql')),contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(a.main(),0)
        self.assertEqual(json.loads(output.getvalue())['status'],'MISSING_DEPENDENCIES')

    def test_default_sources_pin_actual_file_and_accept_reconstructed_adapter(self):
        import hashlib,yaml
        cfg=yaml.safe_load((ROOT/'group_vars/all.yml').read_text())
        for name in ('ew-db','ew-queue'):
            d=cfg['ew_provision_bootstrap_sources'][name]
            path=pathlib.Path(d['path'].replace('{{ playbook_dir }}',str(ROOT)))
            self.assertEqual(d['sha256'],hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(d['interpreter'],'python3')


if __name__=='__main__': unittest.main()
