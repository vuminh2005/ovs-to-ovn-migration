"""Nova state must remain available with Neutron HTTP down, without DB writes."""
import copy
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace as NS

import pytest
import yaml

from tests.support import ROOT
import rollback_common as common
import rollback_controller as controller
import rollback_host as host
import rollback_nova_native as native

SERVER = '11111111-1111-4111-8111-111111111111'


@pytest.fixture
def cells(tmp_path, monkeypatch):
    api=tmp_path/'api.sqlite';cell0=tmp_path/'cell0.sqlite';cell1=tmp_path/'cell1.sqlite'
    with sqlite3.connect(api) as db:
        db.executescript('CREATE TABLE cell_mappings(id INTEGER,uuid TEXT,database_connection TEXT);'
                         'CREATE TABLE instance_mappings(instance_uuid TEXT,cell_id INTEGER,queued_for_delete INTEGER);')
        db.executemany('INSERT INTO cell_mappings VALUES(?,?,?)',[(0,'cell0',str(cell0)),(1,'cell1',str(cell1))])
        db.execute('INSERT INTO instance_mappings VALUES(?,?,0)',(SERVER,1))
    for path in (cell0,cell1):
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE instances(uuid TEXT,host TEXT,vm_state TEXT,task_state TEXT,deleted INTEGER)')
    with sqlite3.connect(cell1) as db:db.execute('INSERT INTO instances VALUES(?,?,?,NULL,0)',(SERVER,'compute1','active'))
    calls=[]
    def read_rows(url,statements):
        calls.extend(statements)
        with sqlite3.connect('file:'+str(url)+'?mode=ro',uri=True) as db:
            db.row_factory=sqlite3.Row
            db.execute('BEGIN')
            return [[dict(row) for row in db.execute(sql)] for sql in statements]
    monkeypatch.setattr(native,'read_rows',read_rows)
    return NS(api=api,cell0=cell0,cell1=cell1,calls=calls,read_rows=read_rows)


def test_native_snapshot_reads_all_cells_with_neutron_http_unavailable(cells, monkeypatch):
    import openstack
    monkeypatch.setattr(openstack,'connect',lambda *a,**kw:(_ for _ in ()).throw(AssertionError('Neutron/Nova HTTP must not be used')))
    paths=[cells.api,cells.cell0,cells.cell1];before=[p.read_bytes() for p in paths]
    result=native.observe(str(cells.api))
    assert result['source']=='nova-cell-db' and result['cells']==['cell0','cell1']
    assert result['servers']==[dict(id=SERVER,compute_host='compute1',status='ACTIVE',task_state=None)]
    assert [p.read_bytes() for p in paths]==before
    assert all(sql.startswith('SELECT ') for sql in cells.calls)
    assert str(cells.api) not in json.dumps(result)


@pytest.mark.parametrize('state,status',[('active','ACTIVE'),('stopped','SHUTOFF'),('paused','PAUSED'),('suspended','SUSPENDED')])
def test_native_state_matches_existing_checkpoint_statuses(cells,state,status):
    with sqlite3.connect(cells.cell1) as db:db.execute('UPDATE instances SET vm_state=?',(state,))
    assert native.observe(str(cells.api))['servers'][0]['status']==status


@pytest.mark.parametrize('change',['task','building','missing-host','missing-instance','new-unmapped','unknown-cell','pending-build','duplicate'])
def test_native_rejects_partial_or_inflight_nova_inventory(cells,change):
    with sqlite3.connect(cells.cell1) as db:
        if change=='task':db.execute("UPDATE instances SET task_state='migrating'")
        elif change=='building':db.execute("UPDATE instances SET vm_state='building'")
        elif change=='missing-host':db.execute('UPDATE instances SET host=NULL')
        elif change=='missing-instance':db.execute('DELETE FROM instances')
        elif change=='new-unmapped':db.execute('INSERT INTO instances VALUES(?,?,?,NULL,0)',('22222222-2222-4222-8222-222222222222','compute2','active'))
        elif change=='duplicate':db.execute('INSERT INTO instances SELECT * FROM instances')
    with sqlite3.connect(cells.api) as db:
        if change=='unknown-cell':db.execute('UPDATE instance_mappings SET cell_id=99')
        elif change=='pending-build':db.execute('UPDATE instance_mappings SET cell_id=NULL')
    with pytest.raises(native.ReadStop):native.observe(str(cells.api))


def test_unreachable_cell0_is_not_ignored(cells):
    cells.cell0.unlink()
    with pytest.raises(sqlite3.OperationalError):native.observe(str(cells.api))


def test_mapping_change_between_reads_cannot_commit_a_snapshot(cells,monkeypatch):
    original=native.snapshot;calls=[]
    def snapshot(url):
        rows=original(url);calls.append(url)
        if len(calls)==1:
            with sqlite3.connect(cells.cell1) as db:db.execute("UPDATE instances SET host='compute2'")
        return rows
    monkeypatch.setattr(native,'snapshot',snapshot)
    with pytest.raises(native.ReadStop,match='changed while'):native.observe(str(cells.api))


def test_mysql_connections_are_transaction_read_only_before_select(monkeypatch):
    import sqlalchemy
    calls=[]
    class Connection:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def execute(self,query):
            calls.append(str(query));return NS(mappings=lambda:[dict(value=1)])
        def commit(self):calls.append('COMMIT_SESSION_OPTIONS')
        @contextmanager
        def begin(self):
            calls.append('BEGIN_READ');yield
    monkeypatch.setattr(sqlalchemy,'create_engine',lambda *a,**kw:NS(connect=lambda:Connection(),dispose=lambda:calls.append('DISPOSE')))
    assert native.read_rows('mysql+pymysql://nova:private@db/nova',['SELECT value'])==[[dict(value=1)]]
    assert calls==['SET SESSION TRANSACTION READ ONLY','COMMIT_SESSION_OPTIONS','BEGIN_READ','SELECT value','DISPOSE']


def fake_nova(monkeypatch):
    parse=[]
    monkeypatch.setitem(sys.modules,'nova',NS(version=NS(version_string=lambda:'29.2.1'),
        config=NS(parse_args=lambda *a,**kw:parse.append(kw)),
        conf=NS(CONF=NS(api_database=NS(connection='private-dsn')))))
    return parse


def test_native_bootstrap_disables_rpc_and_database_configuration(monkeypatch,capsys):
    parse=fake_nova(monkeypatch)
    monkeypatch.setattr(native,'observe',lambda url:dict(status='PASS'))
    assert native.main()==0
    assert parse==[dict(configure_db=False,init_rpc=False)]
    assert json.loads(capsys.readouterr().out)['nova_version']=='29.2.1'


def test_native_sql_error_never_prints_credentials(monkeypatch,capsys):
    fake_nova(monkeypatch)
    monkeypatch.setattr(native,'observe',lambda url:(_ for _ in ()).throw(RuntimeError('mysql://nova:secret@db/private?password=secret')))
    assert native.main()==1
    text=capsys.readouterr().out
    assert json.loads(text)['status']=='STOP'
    assert 'secret' not in text and 'mysql://' not in text


def test_host_native_reader_executes_only_original_container(monkeypatch,tmp_path):
    monkeypatch.setattr(host,'container',lambda name:dict(Id='id',Image='image',State=dict(Running=True)))
    calls=[]
    def command(argv,**kwargs):
        calls.append((argv,kwargs));return NS(returncode=0,stdout=json.dumps(dict(status='PASS')),stderr='')
    monkeypatch.setattr(host,'command',command)
    value=host.nova_state(tmp_path,dict(run='run',host='controller',control=True))
    assert value['reader_sha256']==common.sha(ROOT/'scripts/recovery/rollback_nova_native.py')
    assert calls[0][0]==['docker','exec','-i','--user','nova','nova_api','/var/lib/kolla/venv/bin/python','-']
    assert 'SET SESSION TRANSACTION READ ONLY' in calls[0][1]['input']


def seed_receipt_run(tmp_path):
    root=tmp_path/'run';root.mkdir();inventory=tmp_path/'inventory';inventory.write_text('original')
    common.save(root/'runtime.json',dict(run_id='run',inventory=str(inventory),control_hosts=['controller'],network_hosts=[],compute_hosts=['compute1']))
    (root/'inventory.before').write_bytes(inventory.read_bytes())
    controller.initialize(root,dict(db_host='controller',hosts=dict(controller=dict(control=True),compute1=dict(compute=True)),database='neutron'))
    common.save(root/'rollback/frozen/compute1.json',dict(vms={SERVER:{}}))
    value=dict(schema=1,status='PASS',source='nova-cell-db',run='run',host='controller',observed_at=time.time(),
               reader_sha256=common.sha(ROOT/'scripts/recovery/rollback_nova_native.py'),container_id='id',image_id='image',
               cells=['cell0','cell1'],servers=[dict(id=SERVER,compute_host='compute1',status='ACTIVE',task_state=None)])
    common.save(root/'rollback/nova-preflight.json',value)
    common.save(root/'rollback/nova-current.json',value)
    return root,value


def test_actual_ansible_staging_preserves_reader_bytes_and_freezes_native_receipt(tmp_path,monkeypatch,cells):
    # Execute the real phase-01 task, including its file lookup/copy behavior.
    # In-memory fixtures alone miss Ansible file lookup's default rstrip.
    plays=yaml.safe_load((ROOT/'playbooks/01-backup.yml').read_text())
    task=copy.deepcopy(next(t for p in plays for t in p['tasks'] if t['name']=='[01] [SAVE] Stage standalone rollback helpers'))
    staged=tmp_path/'staged';staged.mkdir()
    task['ansible.builtin.copy']['dest']=str(staged/'{{ item }}')
    playbooks=tmp_path/'playbooks';playbooks.mkdir()
    (tmp_path/'scripts').symlink_to(ROOT/'scripts',target_is_directory=True)
    phase=playbooks/'stage.yml'
    phase.write_text(yaml.safe_dump([dict(name='Real helper staging',hosts='localhost',gather_facts=False,tasks=[task])]))
    entry=tmp_path/'migrate.yml'
    entry.write_text(yaml.safe_dump([{'ansible.builtin.import_playbook':'playbooks/stage.yml'}]))
    result=subprocess.run([sys.executable,'-m','ansible.cli.playbook','-i','localhost,','-c','local',str(entry),
                           '-e','ansible_python_interpreter='+sys.executable],cwd=tmp_path,capture_output=True,text=True,
                          timeout=60,env=dict(os.environ,ANSIBLE_CONFIG=str(ROOT/'ansible.cfg'),ANSIBLE_NOCOLOR='1'))
    assert result.returncode==0,result.stdout+result.stderr
    spec=importlib.util.spec_from_file_location('staged_nova_host',staged/'rollback_host.py')
    staged_host=importlib.util.module_from_spec(spec);spec.loader.exec_module(staged_host)
    monkeypatch.setattr(staged_host,'container',lambda name:dict(Id='id',Image='image',State=dict(Running=True)))
    def command(argv,**kwargs):
        assert kwargs['input']==(staged/'rollback_nova_native.py').read_text()
        return NS(returncode=0,stdout=json.dumps(native.observe(str(cells.api))),stderr='')
    monkeypatch.setattr(staged_host,'command',command)
    root,_=seed_receipt_run(tmp_path)
    receipt=staged_host.nova_state(staged,dict(run='run',host='controller',control=True))
    common.save(root/'rollback/nova-preflight.json',receipt)
    common.save(root/'rollback/nova-current.json',receipt)
    assert controller.nova_preflight(root)['status']=='PASS'
    assert controller.nova(root,True)['status']=='PASS'
    for name in task['loop']:
        assert (staged/name).read_bytes()==(ROOT/'scripts/recovery'/name).read_bytes(),name


def test_frozen_native_inventory_is_immutable_and_needs_no_http(tmp_path,monkeypatch):
    root,value=seed_receipt_run(tmp_path)
    import openstack
    monkeypatch.setattr(openstack,'connect',lambda:(_ for _ in ()).throw(AssertionError('HTTP called')))
    assert controller.nova(root,True)['status']=='PASS'
    assert controller.nova(root)['matches'] is True
    with pytest.raises(common.Stop,match='Never overwrite'):controller.nova(root,True)
    value['servers'][0]['compute_host']='compute2';common.save(root/'rollback/nova-current.json',value)
    assert controller.nova(root)['matches'] is False


@pytest.mark.parametrize('key,value',[('run','foreign'),('host','compute1'),('reader_sha256','changed'),('observed_at',0),('cells',[]),('source','http-cache')])
def test_invalid_native_receipt_cannot_freeze(tmp_path,key,value):
    root,receipt=seed_receipt_run(tmp_path);receipt[key]=value
    common.save(root/'rollback/nova-current.json',receipt)
    common.save(root/'rollback/nova-preflight.json',receipt)
    for validate in (controller.nova_preflight,lambda root:controller.nova(root,True)):
        with pytest.raises(common.Stop,match='identity/source/freshness is invalid: '+key):validate(root)
    assert not (root/'rollback/nova-frozen.json').exists()


@pytest.mark.parametrize('observed',[None,'now',True,float('nan'),float('inf'),float('-inf'),0])
def test_invalid_freshness_is_an_explicit_stop_before_api_freeze(tmp_path,observed):
    root,receipt=seed_receipt_run(tmp_path);receipt['observed_at']=observed
    common.save(root/'rollback/nova-preflight.json',receipt)
    with pytest.raises(common.Stop,match='invalid: observed_at'):controller.nova_preflight(root)
    assert not (root/'rollback/nova-frozen.json').exists()


@pytest.mark.parametrize('changed_reader',[False,True])
def test_real_ansible_preflight_gate_stops_hash_mismatch_before_freeze(tmp_path,changed_reader):
    root,receipt=seed_receipt_run(tmp_path)
    if changed_reader:
        receipt['reader_sha256']='different-staged-bytes'
        common.save(root/'rollback/nova-preflight.json',receipt)
    plays=yaml.safe_load((ROOT/'playbooks/07-migrate-db.yml').read_text())
    task=copy.deepcopy(next(t for p in plays for t in p['tasks'] if t['name']=='[07] Validate native Nova receipt identity and reader bytes before API freeze'))
    task['ansible.builtin.command']['argv'][0]=sys.executable
    marker=tmp_path/'api-stop-reached'
    playbooks=tmp_path/'playbooks';playbooks.mkdir()
    (tmp_path/'scripts').symlink_to(ROOT/'scripts',target_is_directory=True)
    entry=playbooks/'gate.yml'
    entry.write_text(yaml.safe_dump([dict(name='Real pre-freeze gate',hosts='localhost',gather_facts=False,
        vars=dict(migration_run_dir=str(root)),tasks=[task,dict(name='API stop sentinel',
            **{'ansible.builtin.copy':dict(dest=str(marker),content='Reached only after valid preflight')})])]))
    result=subprocess.run([sys.executable,'-m','ansible.cli.playbook','-i','localhost,','-c','local',str(entry),
                           '-e','ansible_python_interpreter='+sys.executable],cwd=tmp_path,capture_output=True,text=True,
                          timeout=60,env=dict(os.environ,ANSIBLE_CONFIG=str(ROOT/'ansible.cfg'),ANSIBLE_NOCOLOR='1'))
    assert marker.exists() is (not changed_reader),result.stdout+result.stderr
    assert (result.returncode==0) is (not changed_reader),result.stdout+result.stderr
    if changed_reader:assert 'invalid: reader_sha256' in result.stdout
    assert not (root/'rollback/nova-frozen.json').exists()


def test_changed_nova_before_stop_and_changed_container_after_freeze_are_blocked(tmp_path):
    root,receipt=seed_receipt_run(tmp_path);receipt['image_id']='replacement'
    common.save(root/'rollback/nova-current.json',receipt)
    with pytest.raises(common.Stop,match='pre-freeze'):controller.nova(root,True)
    common.save(root/'rollback/nova-current.json',json.loads((root/'rollback/nova-preflight.json').read_text()))
    controller.nova(root,True)
    common.save(root/'rollback/nova-current.json',receipt)
    with pytest.raises(common.Stop,match='reader/container/cell'):controller.nova(root)


def test_native_preflight_and_frozen_read_surround_api_stop():
    tasks=[t for play in yaml.safe_load((ROOT/'playbooks/07-migrate-db.yml').read_text()) for t in play['tasks']]
    names=[t['name'] for t in tasks]
    assert names.index('[07] Preflight native read-only Nova cells before API freeze')<names.index('[07] Stop and disable neutron-server')
    assert (names.index('[07] [SAVE] Pin native Nova preflight before closing APIs')<
            names.index('[07] Validate native Nova receipt identity and reader bytes before API freeze')<
            names.index('[07] [TIMER] Start Neutron API downtime')<names.index('[07] Stop and disable neutron-server'))
    assert names.index('[07] Stop and disable neutron-server')<names.index('[07] Read native Nova cells while Neutron API is stopped')<names.index('[07] Dump Neutron and prove isolated round-trip restore')
    for task in tasks:
        if task['name']=='[07] Freeze idle Nova inventory against source hypervisors':assert 'ansible.builtin.shell' not in task
    source=(ROOT/'scripts/recovery/rollback_nova_native.py').read_text()
    assert 'import openstack' not in source and 'nova.objects' not in source
    assert 'init_rpc=False' in source and 'configure_db=False' in source
