"""Rollback ownership, drift barriers, restore rehearsal and real Ansible guards."""
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
from types import SimpleNamespace as NS
from unittest.mock import patch

import pytest
import yaml

from tests.support import ROOT
import rollback_common as common
import rollback_controller as controller
import rollback_database as database
import rollback_host as host
import rollback_validate as validation


def seed_tables():
    return dict(ports=[dict(id='vm-port',device_owner='compute:nova',network_id='net',mac_address='fa:16:3e:00:00:01',status='ACTIVE',standard_attr_id=1)],
        networks=[dict(id='net',mtu=1392,status='ACTIVE')],ipallocations=[dict(port_id='vm-port',ip_address='192.0.2.10')],
        standardattributes=[dict(id=1,revision_number=1,created_at='then',updated_at='then',description='customer')],
        networksegments=[dict(id='segment',network_type='vxlan',segmentation_id=100)],
        providerresourceassociations=[dict(resource_id='router',provider_name='single_node')],
        ml2_port_bindings=[dict(port_id='vm-port',host='compute1',vnic_type='normal',vif_type='ovs',vif_details='old',status='ACTIVE',profile='{}')],
        securitygrouprules=[dict(id='sg-rule',protocol='tcp',port_range_min=22)],agents=[dict(id='agent',heartbeat_timestamp='then')])


def test_business_projection_accepts_only_expected_migration_and_heartbeat_changes():
    original=seed_tables(); current=copy.deepcopy(original)
    current['networksegments'][0]['network_type']='geneve'
    current['providerresourceassociations'][0]['provider_name']='ovn'
    current['ports'][0]['status']='DOWN'
    current['standardattributes'][0].update(revision_number=22,updated_at='now')
    current['agents'][0]['heartbeat_timestamp']='now'
    current['ml2_port_bindings'][0].update(vif_type='ovs',vif_details='new',status='INACTIVE')
    current['ports'].append(dict(id='metadata',device_owner='network:distributed',standard_attr_id=2))
    current['ipallocations'].append(dict(port_id='metadata',ip_address='192.0.2.11'))
    current['standardattributes'].append(dict(id=2,description='metadata'))
    assert common.business_sha(original)==common.business_sha(current)


@pytest.mark.parametrize('change',['new-port','deleted-port','mac','ip','binding-host','profile','sg-rule','mtu','description','new-table'])
def test_business_changes_are_never_normalized_away(change):
    before=seed_tables();after=copy.deepcopy(before)
    if change=='new-port':after['ports'].append(dict(id='new',device_owner='compute:nova'))
    elif change=='deleted-port':after['ports']=[]
    elif change=='mac':after['ports'][0]['mac_address']='fa:16:3e:11:11:11'
    elif change=='ip':after['ipallocations'][0]['ip_address']='192.0.2.12'
    elif change=='binding-host':after['ml2_port_bindings'][0]['host']='compute2'
    elif change=='profile':after['ml2_port_bindings'][0]['profile']='{"capabilities":["new"]}'
    elif change=='sg-rule':after['securitygrouprules'][0]['port_range_min']=23
    elif change=='mtu':after['networks'][0]['mtu']=1400
    elif change=='description':after['standardattributes'][0]['description']='changed'
    else:after['unknown_extension']=[dict(id='new')]
    assert common.business_sha(before)!=common.business_sha(after)


def test_owned_run_rejects_foreign_host_and_symlink(tmp_path,monkeypatch):
    monkeypatch.setattr(common,'BASE',tmp_path)
    directory=common.owned('run','network1',True)
    assert directory.stat().st_mode & 0o777==0o700
    with pytest.raises(common.Stop):common.owned('run','compute1')
    (tmp_path/'foreign').symlink_to(directory,target_is_directory=True)
    with pytest.raises(common.Stop):common.owned('foreign','network1')
    with pytest.raises(common.Stop):common.owned('../run','network1')


def test_atomic_private_journal_records_intent_without_claiming_completion(tmp_path):
    path=tmp_path/'events.jsonl'
    common.journal(path,'db-migration','intent')
    assert path.stat().st_mode & 0o777==0o600
    rows=[json.loads(line) for line in path.read_text().splitlines()]
    assert rows[0]['edge']=='intent' and len(rows)==1


@pytest.fixture
def frozen_db(tmp_path,monkeypatch):
    sql=tmp_path/'neutron.sql';sql.write_text('CREATE DATABASE neutron;\nUSE neutron;\nCREATE TABLE ports(id text);\n')
    tables=seed_tables()
    checkpoint=dict(schema=1,status='PASS',run='run',database='neutron',dump_sha256=common.sha(sql),business_sha256=common.business_sha(tables),image_id='image',roundtrip_sha256=database.canonical(sql))
    common.save(tmp_path/'database-checkpoint.json',checkpoint)
    common.save(tmp_path/'database-inspection.json',dict(status='PASS',run='run',business_matches=True,current_business_sha256=common.business_sha(tables)))
    monkeypatch.setattr(database,'command',lambda *a,**kw:NS(stdout='image'))
    monkeypatch.setattr(database,'dump',lambda container,options,schema,path:Path(path).write_bytes(sql.read_bytes()))
    return NS(directory=tmp_path,tables=tables,checkpoint=checkpoint,cfg=dict(run='run',execute=True,maintenance=True,all_writers_stopped=True))


@contextmanager
def auth():yield ['--private-config']


def test_neutron_restore_requires_all_guards_and_never_touches_other_databases(frozen_db,monkeypatch):
    s=frozen_db;queries=[];imports=[]
    monkeypatch.setattr(database,'client_config',auth)
    monkeypatch.setattr(database,'tables',lambda *args:copy.deepcopy(s.tables))
    monkeypatch.setattr(database,'query',lambda *args:queries.append(args[-1]) or [])
    monkeypatch.setattr(database,'import_dump',lambda *args:imports.append(args))
    result=database.restore(s.directory,s.cfg)
    assert result['status']=='PASS'
    assert queries==['DROP DATABASE IF EXISTS `neutron`;']
    assert imports[0][2]==s.directory/'neutron.sql'
    assert 'nova' not in ''.join(queries) and 'keystone' not in ''.join(queries)
    assert json.loads((s.directory/'database-restore.json').read_text())['status']=='PASS'


@pytest.mark.parametrize('guard',['execute','maintenance','all_writers_stopped'])
def test_missing_restore_authorization_cannot_issue_sql(frozen_db,monkeypatch,guard):
    s=frozen_db;s.cfg[guard]=False
    with patch.object(database,'client_config') as connect,pytest.raises(common.Stop):database.restore(s.directory,s.cfg)
    connect.assert_not_called()


def test_changed_dump_and_new_business_delta_block_before_drop(frozen_db,monkeypatch):
    s=frozen_db
    monkeypatch.setattr(database,'client_config',auth)
    changed=copy.deepcopy(s.tables);changed['ports'][0]['mac_address']='different'
    monkeypatch.setattr(database,'tables',lambda *args:changed)
    with patch.object(database,'query') as query,pytest.raises(common.Stop,match='changed after'):
        database.restore(s.directory,s.cfg)
    query.assert_not_called()
    (s.directory/'neutron.sql').write_text('tampered')
    with pytest.raises(common.Stop,match='hash'):database.restore(s.directory,s.cfg)


def test_restore_does_not_accept_geneve_or_allocation_drift_as_exact_source(frozen_db,monkeypatch):
    s=frozen_db;monkeypatch.setattr(database,'client_config',auth)
    changed=copy.deepcopy(s.tables);changed['networksegments'][0]['network_type']='geneve'
    monkeypatch.setattr(database,'tables',lambda *a:changed)
    monkeypatch.setattr(database,'query',lambda *a:[])
    monkeypatch.setattr(database,'import_dump',lambda *a:None)
    monkeypatch.setattr(database,'dump',lambda container,options,schema,path:Path(path).write_text('wrong full source data'))
    with pytest.raises(common.Stop,match='full schema/data'):database.restore(s.directory,s.cfg)
    assert json.loads((s.directory/'database-restore.json').read_text())['status']=='RESTORING'


def test_partial_import_is_marked_and_can_retry_only_the_same_checkpoint(frozen_db,monkeypatch):
    s=frozen_db;monkeypatch.setattr(database,'client_config',auth)
    monkeypatch.setattr(database,'tables',lambda *a:copy.deepcopy(s.tables))
    monkeypatch.setattr(database,'query',lambda *a:[])
    monkeypatch.setattr(database,'import_dump',lambda *a:(_ for _ in ()).throw(common.Stop('injected partial import')))
    with pytest.raises(common.Stop):database.restore(s.directory,s.cfg)
    assert json.loads((s.directory/'database-restore.json').read_text())['status']=='RESTORING'
    proof=database.inspect(s.directory,s.cfg)
    assert proof['resume_partial_import'] is True
    monkeypatch.setattr(database,'import_dump',lambda *a:None)
    assert database.restore(s.directory,s.cfg)['status']=='PASS'


def test_checkpoint_must_pass_both_restore_roundtrip_and_business_equivalence(tmp_path,monkeypatch):
    common.save(tmp_path/'database-prepared.json',dict(database='neutron',shadow='isolated',image_id='image'))
    monkeypatch.setattr(database,'command',lambda *a,**kw:NS(stdout='image'))
    monkeypatch.setattr(database,'client_config',auth)
    monkeypatch.setattr(database,'tables',lambda *a:seed_tables())
    monkeypatch.setattr(database,'import_dump',lambda *a:None)
    def dump(container,options,schema,path):Path(path).write_text('CREATE DATABASE neutron;\n' if container=='mariadb' else 'DIFFERENT;\n')
    monkeypatch.setattr(database,'dump',dump)
    with pytest.raises(common.Stop,match='differs'):database.checkpoint(tmp_path,dict(run='run'))
    assert not (tmp_path/'database-checkpoint.json').exists()
    assert not any('complete'==json.loads(line)['edge'] for line in (tmp_path/'events.jsonl').read_text().splitlines())


def test_shadow_database_has_no_network_or_production_volume(tmp_path,monkeypatch):
    calls=[]
    def cmd(argv,**kw):
        calls.append(argv)
        if argv[:4]==['docker','inspect','mariadb','--format']:return NS(stdout='sha256:'+'a'*64)
        if argv[:2]==['docker','inspect']:return NS(returncode=1,stdout='')
        return NS(stdout='')
    def query(container,options,sql):
        if 'information_schema.TABLES' in sql:return [dict(name='ports',engine='InnoDB')]
        if 'TRIGGERS' in sql:return [dict(count=0)]*3
        return [dict(ready=1)]
    monkeypatch.setattr(database,'command',cmd);monkeypatch.setattr(database,'query',query);monkeypatch.setattr(database,'client_config',auth)
    database.prepare(tmp_path,dict(run='run'))
    argv=next(a for a in calls if a[:2]==['docker','run'])
    assert '--network=none' in argv and '--tmpfs' in argv and '--pull=never' in argv
    assert '--volume' not in argv and '-v' not in argv and '--volumes-from' not in argv and '--publish' not in argv


@pytest.mark.parametrize('name',['../../etc/passwd','/etc/passwd','etc/kolla/../passwd'])
def test_archive_path_escape_is_rejected(name):
    with pytest.raises(common.Stop):host.selected_member(name)


def test_restore_scope_preserves_other_service_config():
    assert host.selected_member('etc/kolla/neutron-server/neutron.conf')
    assert host.selected_member('etc/kolla/globals.yml')
    assert not host.selected_member('etc/kolla/passwords.yml')
    assert not host.selected_member('etc/kolla/nova-compute/nova.conf')
    assert not host.selected_member('var/lib/mysql/ibdata1')


def test_symlink_member_cannot_be_restored(tmp_path,monkeypatch):
    archive=tmp_path/'source.tar'
    with tarfile.open(archive,'w') as bundle:
        row=tarfile.TarInfo('etc/kolla/neutron-server/neutron.conf');row.type=tarfile.SYMTYPE;row.linkname='/etc/shadow';bundle.addfile(row)
    monkeypatch.setattr(host,'validate_source',lambda *a:{})
    with patch.object(host,'command') as command,pytest.raises(common.Stop,match='Non-regular'):
        host.restore_config(tmp_path,dict(run='run'))
    command.assert_not_called()


def test_preparation_rollback_does_not_stop_working_legacy_agents(tmp_path,monkeypatch):
    stopped=[]
    monkeypatch.setattr(host,'validate_source',lambda *a:{})
    monkeypatch.setattr(host,'command',lambda *a,**kw:NS(stdout=''))
    monkeypatch.setattr(host,'stop_service',lambda name,**kw:stopped.append(name))
    monkeypatch.setattr(host,'observe',lambda cfg:{})
    host.quiesce(tmp_path,dict(run='run',host='network1',db_restore=False,flow_rebuild=False))
    assert stopped==['ovn_controller','neutron_ovn_metadata_agent']


def test_removed_or_replaced_source_container_blocks_restoration(tmp_path,monkeypatch):
    (tmp_path/'source.tar').write_bytes(b'original')
    common.save(tmp_path/'source.json',dict(run='run',host='compute1',archive_sha256=common.sha(tmp_path/'source.tar'),
                services={'neutron_openvswitch_agent':dict(id='original',image_id='image',mounts=[])}))
    monkeypatch.setattr(host,'container',lambda name:dict(Id='replacement',Image='image'))
    with pytest.raises(common.Stop,match='removed/replaced'):host.validate_source(tmp_path,dict(run='run',host='compute1'))


@pytest.fixture
def planned_run(tmp_path,monkeypatch):
    root=tmp_path/'run';root.mkdir();inventory=tmp_path/'inventory'
    inventory.write_text('[all:vars]\nansible_connection=local\nansible_python_interpreter='+sys.executable+'\n[control]\ncontroller\n[network]\nnetwork1\n[compute]\n')
    (root/'runtime.json').write_text(json.dumps(dict(run_id='run',inventory=str(inventory),control_hosts=['controller'],network_hosts=['network1'],compute_hosts=[])))
    (root/'inventory.before').write_bytes(inventory.read_bytes())
    roles={'controller':dict(control=True), 'network1':dict(chassis=True)}
    controller.initialize(root,dict(hosts=roles,db_host='controller',openrc='/openrc',database='neutron'))
    for name in roles:
        archive=root/f'rollback/source/{name}.tar';archive.write_bytes(b'config-'+name.encode())
        common.save(root/f'rollback/source/{name}.json',dict(run='run',host=name,archive_sha256=common.sha(archive)))
        common.save(root/f'rollback/inspection/{name}.json',dict(status='PASS',run='run',host=name,services=dict(neutron_server=None,ovn_controller=None)))
    monkeypatch.setattr(controller,'no_migration_process',lambda:None)
    return root


def test_pre_migration_plan_requires_no_database_restore(planned_run):
    value=controller.plan(planned_run)
    assert value['db_restore'] is False and value['flow_rebuild'] is False
    assert value['status']=='READY'
    with pytest.raises(common.Stop):controller.authorize(planned_run,dict(execute=True,maintenance=False,plan_sha256=value['plan_sha256']))
    assert controller.authorize(planned_run,dict(execute=True,maintenance=True,plan_sha256=value['plan_sha256']))['status']=='PASS'


def test_partial_migration_without_frozen_database_never_gets_executable_plan(planned_run):
    (planned_run/'metrics').mkdir();(planned_run/'metrics/db_migration.start').write_text('100')
    with pytest.raises(common.Stop,match='No verified checkpoint'):controller.plan(planned_run)


def test_full_original_inventory_is_required(planned_run):
    original=Path(json.loads((planned_run/'runtime.json').read_text())['inventory']);original.write_text('limited inventory')
    with pytest.raises(common.Stop,match='full original'):controller.plan(planned_run)


def test_frozen_vm_change_prevents_even_quiescence(tmp_path,monkeypatch):
    (tmp_path/'source.tar').write_text('source')
    common.save(tmp_path/'source.json',dict(run='run',host='compute1',archive_sha256=common.sha(tmp_path/'source.tar'),services={}))
    common.save(tmp_path/'freeze.json',dict(vms={'original':dict(state='running')}))
    monkeypatch.setattr(host,'vm_state',lambda:{'different':dict(state='running')})
    with patch.object(host,'stop_service') as stop,pytest.raises(common.Stop,match='placement changed'):
        host.quiesce(tmp_path,dict(run='run',host='compute1',compute=True,db_restore=True))
    stop.assert_not_called()


def test_migration_checkpoint_is_before_database_metric_and_takeover():
    phases=yaml.safe_load((ROOT/'playbooks/07-migrate-db.yml').read_text())
    tasks=[t for p in phases for t in p['tasks']];names=[t['name'] for t in tasks]
    assert names.index('[07] Stop and disable neutron-server')<names.index('[07] Dump Neutron and prove isolated round-trip restore')<names.index('[07] [TIMER] Start database migration measurement')
    freeze=next(p for p in phases if p['name']=='Phase 07 - Stop Neutron API workers')
    assert freeze['any_errors_fatal'] is True
    assert len(list((ROOT/'playbooks').glob('[0-9][0-9]-*.yml')))==14


def test_real_ansible_inspect_default_has_no_mutation_calls(tmp_path,planned_run):
    # The production entry runs its real Jinja conditions and plan construction.
    # Replace only remote transport commands with safe receipt-emitting fixtures.
    plays=yaml.safe_load((ROOT/'rollback-to-ovs.yml').read_text())
    events=tmp_path/'mutations'
    for p in plays:
        p['become']=False
        for task in p['tasks']:
            cmd=task.get('ansible.builtin.command',{})
            args=cmd.get('argv',[])
            if args and 'rollback_controller.py' in str(args[1]):args[1]=str(ROOT/'scripts/recovery/rollback_controller.py')
            if task['name']=='Inspect source identity and live host state':
                task['ansible.builtin.command']['argv']=[sys.executable,'-c',
                    'import json;print(json.dumps(dict(status="PASS",run="run",host="{{ inventory_hostname }}",services=dict(neutron_server=None,ovn_controller=None))))']
            if task['name']=='Locate frozen database checkpoint':
                task['ansible.builtin.stat']['path']=str(tmp_path/'absent')
            if task['name'].startswith(('Stop relevant','Restore and verify','Restore captured','Remove proven','Start original','Start only')):
                task['ansible.builtin.command']['argv']=['touch',str(events)]
    inventory=Path(json.loads((planned_run/'runtime.json').read_text())['inventory'])
    inventory.write_text('[all:vars]\nansible_connection=local\nansible_python_interpreter='+sys.executable+'\n[control]\ncontroller\n[network]\nnetwork1\n[compute]\n')
    (planned_run/'inventory.before').write_bytes(inventory.read_bytes())
    entry=tmp_path/'inspect.yml';entry.write_text(yaml.safe_dump(plays,sort_keys=False))
    result=subprocess.run([sys.executable,'-m','ansible.cli.playbook','-i',str(inventory),str(entry),'-e','rollback_run_dir='+str(planned_run)],
                          cwd=tmp_path,capture_output=True,text=True,timeout=45)
    assert result.returncode==0,result.stdout+result.stderr
    assert not events.exists()
    assert json.loads((planned_run/'rollback/plan.json').read_text())['db_restore'] is False


@pytest.mark.parametrize('case',['success','wrong-plan','db-failure','host-failure'])
def test_real_ansible_execute_keeps_global_restore_barriers(tmp_path,planned_run,case):
    root=planned_run;inventory=Path(json.loads((root/'runtime.json').read_text())['inventory'])
    (root/'metrics').mkdir();(root/'metrics/db_migration.start').write_text('100')
    sql=root/'rollback/database/neutron.sql';sql.write_text('frozen source SQL')
    checkpoint=dict(run='run',database='neutron',dump_sha256=common.sha(sql),business_sha256='business')
    proof=dict(status='PASS',run='run',business_matches=True,current_business_sha256='business',checkpoint=checkpoint,metadata_port_ids=[])
    common.save(root/'rollback/database/database-inspection.json',proof)
    common.save(root/'rollback/frozen/network1.json',dict(vms={}))
    common.save(root/'rollback/nova-frozen.json',dict(schema=1,run='run',servers=[]))
    common.save(root/'rollback/nova-inspection.json',dict(status='PASS',matches=True,source_sha256=common.sha(root/'rollback/nova-frozen.json')))
    plan=controller.plan(root)
    plays=yaml.safe_load((ROOT/'rollback-to-ovs.yml').read_text());events=tmp_path/'events'
    marker=tmp_path/'checkpoint';marker.write_text('exists')
    action_names={
        'Stop relevant writers before any restore':'quiesce',
        'Restore and verify Neutron without touching other service databases':'db',
        'Restore captured effective source config and units':'config',
        'Remove proven OVN artifacts and return integration bridge ownership':'ovs',
        'Start original API container with restored source config':'api',
        'Start only agents that were running in the source snapshot':'agents'}
    def emit(value,stage=None,fail=False):
        code='import json,sys\nfrom pathlib import Path\n'
        if stage:code+=f'with Path({str(events)!r}).open("a") as f:f.write({stage!r}+"\\n")\n'
        code+='print('+repr(json.dumps(value))+')\n'
        code+='sys.exit('+('1' if fail else '0')+')\n'
        return [sys.executable,'-c',code]
    for p in plays:
        p['become']=False
        for task in p['tasks']:
            name=task['name'];args=task.get('ansible.builtin.command',{}).get('argv',[])
            if args and 'rollback_controller.py' in str(args[1]):args[1]=str(ROOT/'scripts/recovery/rollback_controller.py')
            if name=='Inspect source identity and live host state':
                task['ansible.builtin.command']['argv']=emit(dict(status='PASS',run='run',host='{{ inventory_hostname }}',services=dict(neutron_server=None,ovn_controller=None)))
            elif name=='Locate frozen database checkpoint':task['ansible.builtin.stat']['path']=str(marker)
            elif name in ('Inspect current database without modifying it','Verify restored Neutron business state'):
                task['ansible.builtin.command']['argv']=emit(proof)
            elif name in action_names:
                stage=action_names[name]
                fail=(case=='db-failure' and stage=='db') or (case=='host-failure' and stage=='quiesce')
                task['ansible.builtin.command']['argv']=emit(dict(status='STOP' if fail else 'PASS'),stage,fail)
            elif name=='Wait for Neutron through the operator maintenance path':task['ansible.builtin.shell']='true'
            elif name=='Check current Nova against frozen instance inventory':task['ansible.builtin.shell']='true'
            elif name=='Wait for source services ports and underlay to recover':task['ansible.builtin.command']['argv']=emit(dict(status='PASS'))
            elif name=='Validate existing guests without changing migration probes or reports':
                task.pop('ansible.builtin.shell');task.pop('args')
                task['ansible.builtin.command']=dict(argv=[sys.executable,'-c',
                    f'from pathlib import Path;Path({str(root/"rollback/functional.json")!r}).write_text(\'{{"status":"PASS"}}\')'])
    entry=tmp_path/'execute.yml';entry.write_text(yaml.safe_dump(plays,sort_keys=False))
    extra=dict(rollback_run_dir=str(root),rollback_mode='execute',rollback_execute=True,rollback_maintenance_confirm=True,
               rollback_plan_sha256='wrong' if case=='wrong-plan' else plan['plan_sha256'])
    result=subprocess.run([sys.executable,'-m','ansible.cli.playbook','-i',str(inventory),str(entry),'-e',json.dumps(extra)],
                          cwd=tmp_path,capture_output=True,text=True,timeout=60)
    assert (result.returncode==0)==(case=='success'),result.stdout+result.stderr
    stages=events.read_text().splitlines() if events.exists() else []
    if case=='wrong-plan':assert stages==[]
    elif case=='host-failure':assert stages and set(stages)=={'quiesce'}
    elif case=='db-failure':assert stages[-1]=='db' and 'api' not in stages and 'config' not in stages
    else:
        assert max(i for i,s in enumerate(stages) if s=='quiesce')<stages.index('db')<stages.index('config')<stages.index('ovs')<stages.index('api')<stages.index('agents')
        completed=json.loads((root/'rollback/complete.json').read_text())
        assert completed['maintenance_released'] is False
        assert completed['status']=='RESTORED_REQUIRES_ACCEPTANCE'


def test_functional_verification_never_uses_evidence_writing_collectors(tmp_path,monkeypatch):
    import workload_validation as workload
    cfg=dict(run='run',probe_interval=.2)
    common.save(tmp_path/'validation-config.json',cfg)
    common.save(tmp_path/'validation-resources.json',dict(schema_version=2))
    evidence=tmp_path/'measure0-console-records.json';evidence.write_text('original evidence')
    before={p.name:p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    class Reader:
        def __init__(self,root):self.cloud=NS(network=NS(get_network=lambda network:NS(mtu=1392)));self.calls={}
        def pair(self,role):return {key:dict(server=role+key,network='net') for key in ('0','1')}
        def collect(self,*a,**kw):raise AssertionError('Writing collector was used')
        def console_output(self,server,deadline):
            self.calls[server]=self.calls.get(server,0)+1;last=self.calls[server]*5
            rows=[dict(run='run',vm=server,kind='packet',boot='boot',seq=n,success=True,ts=n*.2,mono=n*.2) for n in range(last-4,last+1)]
            rows.append(dict(run='run',vm=server,kind='health',boot='boot',seq=last,mono=last*.2,dhcp=True,metadata=True,mtu=1392))
            return '\n'.join(workload.PREFIX+json.dumps(row) for row in rows)
    monkeypatch.setattr(workload,'Validation',Reader);monkeypatch.setattr(validation.time,'sleep',lambda *a:None)
    result=validation.wait(tmp_path,timeout=3)
    assert result['status']=='PASS'
    assert result['security_group_packet_deny_test']=='NOT_RUN'
    assert before=={p.name:p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}


def test_nova_task_and_host_changes_are_not_treated_as_unchanged(planned_run,monkeypatch):
    import openstack
    common.save(planned_run/'rollback/nova-frozen.json',dict(schema=1,run='run',servers=[dict(id='server',compute_host='compute1',status='ACTIVE',task_state=None)]))
    server=NS(id='server',compute_host='compute2',status='ACTIVE',task_state=None)
    cloud=NS(compute=NS(servers=lambda **kw:[server]))
    monkeypatch.setattr(openstack,'connect',lambda:cloud)
    assert controller.nova(planned_run)['matches'] is False
    server.task_state='migrating'
    with pytest.raises(common.Stop,match='in-flight'):controller.nova(planned_run)
