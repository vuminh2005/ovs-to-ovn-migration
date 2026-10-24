"""Rollback preparation is opt-in; core cutover executes with no rollback files."""
import copy
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import jinja2
import pytest
import yaml

from tests.support import ROOT, expanded_tasks
import rollback_common as common
import rollback_controller as controller
from run_layout import initialize, run_path


def templates():
    env=jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters.update(bool=bool,to_json=json.dumps,quote=shlex.quote)
    return env


def test_minimal_defaults_retain_six_ping_vms_without_optional_work():
    cfg=yaml.safe_load((ROOT/'group_vars/all.yml').read_text())
    ns=yaml.safe_load((ROOT/'group_vars/ns-lab.yml').read_text())
    assert cfg['migration_rollback_enabled'] is False
    assert cfg['migration_diagnostics_enabled'] is False
    assert cfg['validation_ephemeral_network'] is False
    assert cfg['validation_ping_only'] is True and cfg['validation_pair_d_enabled'] is False
    assert cfg['validation_workloads_enabled'] is True and ns['validation_ew_matrix_enabled'] is True
    assert ns['ns_enabled'] is True and ns['ns_icmp_enabled'] is True and ns['ns_diagnostics_enabled'] is True
    assert ns['ns_tcp_enabled'] is False


@pytest.mark.parametrize('enabled',[False,True])
@pytest.mark.parametrize('override',[None,False,True])
def test_actual_bootstrap_saves_profile_and_independent_backup_override(tmp_path,enabled,override):
    run=tmp_path/'run';initialize(run)
    inventory=tmp_path/'inventory';inventory.write_text('[control]\ncontroller\n[network]\nnetwork1\n[compute]\ncompute1\ncompute2\n')
    plays=yaml.safe_load((ROOT/'playbooks/00-bootstrap.yml').read_text())
    resolve=copy.deepcopy(next(t for p in plays for t in p['tasks'] if t['name']=='[00] Resolve inventory and migration hosts'))
    save=copy.deepcopy(next(t for p in plays for t in p['tasks'] if t['name']=='[00] [SAVE] Save runtime settings'))
    vars=dict(migration_run_dir=str(run),kolla_ansible_bin_cmd=dict(stdout='/usr/bin/kolla-ansible'))
    entry=tmp_path/'bootstrap.yml'
    entry.write_text(yaml.safe_dump([dict(hosts='localhost',gather_facts=False,vars_files=[str(ROOT/'group_vars/all.yml')],
        vars=vars,tasks=[resolve,save])],sort_keys=False))
    extra=dict(migration_rollback_enabled=enabled)
    if override is not None:extra['run_mariadb_backup']=override
    result=subprocess.run([sys.executable,'-m','ansible.cli.playbook','-i',str(inventory),str(entry),'-e',json.dumps(extra)],
        capture_output=True,text=True,timeout=30,env=dict(os.environ,ANSIBLE_FILTER_PLUGINS=str(ROOT/'plugins/filter')))
    assert result.returncode==0,result.stdout+result.stderr
    cfg=json.loads(run_path(run,'runtime.json').read_text())
    assert cfg['migration_rollback_enabled'] is enabled
    assert cfg['run_mariadb_backup'] is (enabled if override is None else override)
    assert not cfg['migration_diagnostics_enabled'] and not cfg['validation_ephemeral_network']


@pytest.mark.parametrize('enabled',[False,True])
def test_real_ansible_option_skips_all_preparation_includes_and_pins_the_choice(tmp_path,enabled):
    log=tmp_path/'calls';harness=tmp_path/'profile.yml'
    inventory=tmp_path/'inventory'
    inventory.write_text('[all:vars]\nansible_connection=local\nansible_python_interpreter='+sys.executable+
                         '\n[control]\ncontroller\n[network]\nnetwork1\n[compute]\ncompute1\ncompute2\n[mariadb]\ncontroller\n')
    groups=[]
    for file in ('01-backup.yml','07-migrate-db.yml'):
        for play in yaml.safe_load((ROOT/'playbooks'/file).read_text()):
            if not any(t.get('ansible.builtin.include_tasks','').startswith('tasks/rollback-') for t in play['tasks']):continue
            clone=copy.deepcopy(play);clone['become']=False;clone.pop('vars_files',None)
            for task in clone['tasks']:
                target=ROOT/'playbooks'/task['ansible.builtin.include_tasks']
                if enabled:
                    # Only transport contents are fixtures; execute the real
                    # include conditions, host roles and stored runtime fact.
                    fixture=tmp_path/target.name
                    fixture.write_text(yaml.safe_dump([dict(name='Observe optional preparation',**{'ansible.builtin.shell':
                        'echo '+shlex.quote(target.name)+'-{{ inventory_hostname }} >> '+shlex.quote(str(log))})]))
                    task['ansible.builtin.include_tasks']=str(fixture)
                else:
                    # Real contents reference spec/host files which are absent.
                    # A disabled dynamic include must never evaluate them.
                    task['ansible.builtin.include_tasks']=str(target)
            groups.append(clone)
    assert len(groups)==5
    facts=dict(hosts='localhost',gather_facts=False,tasks=[
        {'ansible.builtin.set_fact':dict(migration_rollback_enabled_runtime=enabled,migration_run_id='run',migration_run_dir=str(tmp_path/'run'))},
        {'ansible.builtin.set_fact':dict(migration_rollback_enabled=not enabled)}])
    harness.write_text(yaml.safe_dump([facts]+groups,sort_keys=False))
    result=subprocess.run([sys.executable,'-m','ansible.cli.playbook','-i',str(inventory),str(harness)],capture_output=True,text=True,timeout=35)
    assert result.returncode==0,result.stdout+result.stderr
    if enabled:
        rows=log.read_text().splitlines()
        assert len(rows)==11
        assert rows[0].startswith('rollback-source-init') and rows[-1].startswith('rollback-checkpoint')
    else:
        assert not log.exists() and not (tmp_path/'run/rollback').exists()


@pytest.mark.parametrize('enabled',[False,True])
def test_core_bash_cutover_runs_without_rollback_helpers_when_disabled(tmp_path,enabled):
    binary=tmp_path/'bin';binary.mkdir();log=tmp_path/'commands.jsonl'
    code=('#!'+sys.executable+'\nimport json,sys\nfrom pathlib import Path\n'
          'with Path('+repr(str(log))+').open("a") as stream:stream.write(json.dumps([Path(sys.argv[0]).name]+sys.argv[1:])+"\\n")\n'
          'if Path(sys.argv[0]).name=="systemctl" and sys.argv[1]=="is-active":print("inactive")\n')
    for name in ('python3','docker','systemctl','ip','kolla-ansible'):
        path=binary/name;path.write_text(code);path.chmod(0o700)
    values=dict(hostvars={'localhost':dict(migration_rollback_enabled_runtime=enabled,migration_run_id='run',
                migration_run_dir=str(tmp_path/'run'),cutover_preparation_runtime=dict(neutron_image='image'))},
        inventory_hostname='network1',migration_run_dir=str(tmp_path/'run'),playbook_dir=str(ROOT/'playbooks'),
        kolla_ansible_bin_runtime='kolla-ansible',kolla_inventory_runtime=str(tmp_path/'inventory'),item='legacy-agent')
    count=0
    for file in ('07-migrate-db.yml','08-cutover.yml','09-cleanup.yml'):
        for task in expanded_tasks(ROOT/'playbooks'/file):
            shell=task.get('ansible.builtin.shell','')
            if 'rollback_host.py event' not in shell and 'rollback_controller.py' not in shell:continue
            rendered=templates().from_string(shell).render(**values)
            result=subprocess.run(['/bin/bash','-euo','pipefail','-c',rendered],capture_output=True,text=True,timeout=10,
                env=dict(os.environ,PATH=str(binary)+os.pathsep+os.environ['PATH']))
            assert result.returncode==0,task['name']+'\n'+result.stdout+result.stderr
            if not enabled:assert 'rollback_host.py' not in rendered and 'rollback_controller.py' not in rendered
            count+=1
    assert count==7
    rows=[json.loads(line) for line in log.read_text().splitlines()]
    events=[row for row in rows if row[0]=='python3']
    assert bool(events) is enabled
    assert any(row[:2]==['docker','run'] for row in rows)
    assert any(row[0]=='kolla-ansible' and row[-1]=='ovn-controller' for row in rows)
    assert any('br-tun' in row for row in rows)


@pytest.mark.parametrize('enabled',[False,True])
def test_metadata_and_gateway_journals_are_absent_without_rollback(enabled):
    values=dict(hostvars={'localhost':dict(migration_run_id='run',migration_rollback_enabled_runtime=enabled,
        metadata_preparation_runtime=dict(hosts=dict(compute1=dict(container_id='id'))),cutover_preparation_runtime=dict(directory='/tmp/prepare'))},
        inventory_hostname='compute1')
    task=next(t for t in expanded_tasks(ROOT/'playbooks/08-cutover.yml') if t['name']=='[08] Stop legacy metadata and start prepared OVN metadata without waiting for chassis')
    command=templates().from_string(task['ansible.builtin.script']['cmd']).render(**values)
    cfg=json.loads(shlex.split(command)[-1])
    assert cfg['journal']==('/var/lib/ovs-to-ovn-rollback/run/events.jsonl' if enabled else None)
    plays=yaml.safe_load((ROOT/'playbooks/10-restore-neutron.yml').read_text())
    task=next(t for p in plays for parent in p['tasks'] for t in parent.get('block',[]) if t['name']=='[10] Schedule unhosted gateways using the prepared native helper')
    arguments=[templates().from_string(arg).render(**values) for arg in task['ansible.builtin.command']['argv']]
    assert '--cleanup-verified' in arguments
    assert arguments[-1]==('/var/lib/ovs-to-ovn-rollback/run/events.jsonl' if enabled else '')


@pytest.mark.parametrize('source_present',[False,True])
def test_disabled_run_cannot_enable_rollback_retrospectively(tmp_path,source_present):
    root=tmp_path/'run';root.mkdir();inventory=tmp_path/'inventory';inventory.write_text('original')
    common.save(root/'runtime.json',dict(run_id='run',inventory=str(inventory),migration_rollback_enabled=False))
    (root/'inventory.before').write_bytes(inventory.read_bytes())
    if source_present:common.save(root/'rollback/spec.json',dict(schema=1,run='run'))
    with pytest.raises(common.Stop,match='Rollback was disabled'):controller.load(root)
    with pytest.raises(common.Stop,match='disabled for this run'):controller.initialize(root,{})


def test_core_order_is_still_gateway_scheduling_order():
    expected=['[07] Stop and disable neutron-server','[07] Run neutron-ovn-db-sync-util migrate',
        '[08] Stop legacy metadata and start prepared OVN metadata without waiting for chassis',
        '[08] Stop and disable legacy network agents','[08] Stop and disable legacy compute agents',
        '[08] Deploy OVN controller','[08] Wait for all existing VM ports to bind and become up',
        '[09] Delete br-tun and patch-tun','[09] Delete qrouter/qdhcp namespaces and their ports',
        '[09] Remove legacy br-int/br-ex patch ports','[09] Verify legacy gateway attachments and L3 agent are withdrawn',
        '[10] Schedule unhosted gateways using the prepared native helper','[10] Enable and start neutron-server',
        '[10] Wait for a successful Neutron network list']
    def walk(tasks):
        for task in tasks:
            yield task
            yield from walk(task.get('block',[]))
    names=[]
    for file in ('07-migrate-db.yml','08-cutover.yml','09-cleanup.yml','10-restore-neutron.yml'):
        names.extend(t['name'] for t in walk(list(expanded_tasks(ROOT/'playbooks'/file))) if t['name'] in expected)
    assert names==expected
