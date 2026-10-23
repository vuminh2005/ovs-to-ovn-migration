"""Early gateway guards, bounded container lifecycle and real Ansible fallback."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch
from contextlib import redirect_stdout

import pytest
import yaml

from tests.support import ROOT
import gateway_native as native
import gateway_scheduling as host


@pytest.fixture
def scheduler():
    topology = {'network1': dict(system_id='gw-a', tunnel_ip='192.0.2.10'),
                'network2': dict(system_id='gw-b', tunnel_ip='192.0.2.11')}
    port = dict(id='port-a', network_id='external', device_id='router-a',
                device_owner='network:router_gateway', mac_address='fa:16:3e:00:00:01',
                fixed_ips=[dict(subnet_id='subnet-a', ip_address='198.51.100.9')])
    core = MagicMock()
    core.get_ports.side_effect = lambda *a, **kw: copy.deepcopy([port])
    l3, nb, sb = MagicMock(), MagicMock(), MagicMock()
    l3.get_router.return_value = dict(external_gateway_info=dict(network_id='external'))
    l3._get_gateway_port_physnet_mapping.return_value = {'port-a': 'physnet1'}
    l3._ovn_client.get_candidates_for_scheduling.return_value = ['gw-a', 'gw-b']
    nb.idl.tables = {'Gateway_Chassis': NS()}
    nb.get_gateway_chassis_az_hints.return_value = ['az1']
    assignments = []
    nb.get_gateway_chassis_binding.side_effect = lambda name: list(assignments)
    l3.schedule_unhosted_gateways.side_effect = lambda: assignments.extend(['gw-a', 'gw-b'])
    sb.lookup.side_effect = lambda table, name, **kw: (
        NS(encaps=[NS(type='geneve', ip=topology['network1' if name == 'gw-a' else 'network2']['tunnel_ip'])])
        if table == 'Chassis' else NS(chassis=[NS(name='gw-a')], up=[True]))
    sb.idl.tables = {'Port_Binding': NS(columns={'chassis': NS(), 'up': NS()})}
    return NS(topology=topology, port=port, core=core, l3=l3, nb=nb, sb=sb, assignments=assignments)


def invoke(s):
    return native.schedule(s.l3, s.core, s.nb, s.sb, s.topology, object())


def test_native_schedule_verifies_ha_and_frozen_identity_without_api(scheduler):
    s = scheduler
    result = invoke(s)
    assert result['status'] == 'PASS' and result['changed_count'] == 1
    assert result['gateways']['lrp-port-a']['after'] == ['gw-a', 'gw-b']
    assert result['gateways']['lrp-port-a']['redirect_chassis'] == ['gw-a']
    s.l3.schedule_unhosted_gateways.assert_called_once_with()
    s.l3._ovn_client.get_candidates_for_scheduling.assert_called_once_with(
        'physnet1', cms=s.sb.get_gateway_chassis_from_cms_options.return_value,
        chassis_physnets=s.sb.get_chassis_and_physnets.return_value, availability_zone_hints=['az1'])
    s.core.create_port.assert_not_called()
    s.core.update_port.assert_not_called()
    s.l3.update_router.assert_not_called()


@pytest.mark.parametrize('case', ['no-candidates', 'unknown-chassis', 'geneve-mismatch', 'non-ovn',
                                 'missing-lrp', 'missing-schema', 'duplicate-system-id'])
def test_all_gateway_guards_run_before_any_native_write(scheduler, case):
    s = scheduler
    if case == 'no-candidates':
        s.l3._ovn_client.get_candidates_for_scheduling.return_value = []
    elif case == 'unknown-chassis':
        s.l3._ovn_client.get_candidates_for_scheduling.return_value = ['stale-outside-inventory']
    elif case == 'geneve-mismatch':
        s.sb.lookup.return_value = NS(encaps=[NS(type='geneve', ip='203.0.113.100')])
        s.sb.lookup.side_effect = None
    elif case == 'non-ovn':
        s.l3._get_gateway_port_physnet_mapping.return_value = {}
    elif case == 'missing-lrp':
        s.nb.lookup.side_effect = KeyError('missing')
    elif case == 'missing-schema':
        s.nb.idl.tables = {}
    else:
        s.topology['network2']['system_id'] = 'gw-a'
    with pytest.raises((native.GatewayCheck, KeyError)):
        invoke(s)
    s.l3.schedule_unhosted_gateways.assert_not_called()


def test_retry_keeps_valid_primary_and_is_idempotent(scheduler):
    s = scheduler
    invoke(s)
    s.l3.schedule_unhosted_gateways.side_effect = None
    result = invoke(s)
    assert result['changed_count'] == 0
    assert result['gateways']['lrp-port-a']['after'][0] == 'gw-a'


def test_moving_valid_primary_is_reported_as_failure(scheduler):
    s = scheduler
    s.assignments[:] = ['gw-a', 'gw-b']
    s.l3.schedule_unhosted_gateways.side_effect = lambda: s.assignments.reverse()
    with pytest.raises(native.GatewayCheck, match='primary'):
        invoke(s)


@pytest.mark.parametrize('assigned', [[], ['gw-a', 'unknown-chassis']])
def test_incomplete_or_invalid_native_assignment_is_not_pass(scheduler, assigned):
    s = scheduler
    s.l3.schedule_unhosted_gateways.side_effect = lambda: s.assignments.extend(assigned)
    with pytest.raises(native.GatewayCheck, match='verification'):
        invoke(s)


def test_gateway_identity_change_is_detected(scheduler):
    s = scheduler
    def change():
        s.assignments.extend(['gw-a'])
        s.port['mac_address'] = 'fa:16:3e:aa:aa:aa'
    s.l3.schedule_unhosted_gateways.side_effect = change
    with pytest.raises(native.GatewayCheck, match='identities changed'):
        invoke(s)


@pytest.mark.parametrize('binding', [None, NS(chassis=[])])
def test_sb_redirect_convergence_does_not_hold_api_restoration(scheduler, binding):
    s = scheduler
    original = s.sb.lookup.side_effect
    s.sb.lookup.side_effect = lambda table, name, **kw: (
        original(table, name, **kw) if table == 'Chassis' else binding)
    s.sb.idl.tables['Port_Binding'].columns = {'chassis': NS()}
    result = invoke(s)
    assert result['status'] == 'PASS'
    assert result['gateways']['lrp-port-a']['redirect_chassis'] == []
    assert 'redirect_up' not in result['gateways']['lrp-port-a']


def test_preflight_does_not_connect_idls_or_initialize_plugin_manager(tmp_path):
    topo = tmp_path / 'topology.json'
    topo.write_text('{}')
    with patch.object(native, 'configuration', return_value=(object(), '24.2.1')), \
            patch.object(native, 'bootstrap') as bootstrap, patch.object(native.signal, 'alarm'), \
            redirect_stdout(io.StringIO()) as output:
        assert native.main(['preflight', '--topology', str(topo)]) == 0
    bootstrap.assert_not_called()
    assert json.loads(output.getvalue().split(native.RESULT_PREFIX)[1])['status'] == 'PASS'


def test_native_errors_never_expose_database_credentials(tmp_path):
    topo = tmp_path / 'topology.json'; topo.write_text('{}')
    with patch.object(native, 'configuration', side_effect=RuntimeError('mysql://user:SECRET@db/neutron')), \
            patch.object(native.signal, 'alarm'), redirect_stdout(io.StringIO()) as output:
        assert native.main(['preflight', '--topology', str(topo)]) == 1
    assert 'SECRET' not in output.getvalue()
    assert json.loads(output.getvalue().split(native.RESULT_PREFIX)[1])['stage'] == 'configuration'


@pytest.fixture
def prepared(tmp_path):
    directory = Path(tempfile.mkdtemp(prefix='ovs-to-ovn-preparation-', dir='/tmp'))
    try:
        config = tmp_path / 'config'; config.mkdir()
        (config / 'config.json').write_text('{"password":"PRIVATE"}')
        topology = directory / 'gateway-topology.json'
        topology.write_text('{"network1":{"system_id":"gw-a","tunnel_ip":"192.0.2.10"}}')
        (directory / 'gateway_native.py').write_bytes((ROOT / 'scripts/migration/gateway_native.py').read_bytes())
        owner = dict(run=directory.name.removeprefix('ovs-to-ovn-preparation-'), directory=str(directory),
                     neutron_image='neutron:24.2.1', topology_sha256=hashlib.sha256(topology.read_bytes()).hexdigest())
        (directory / 'owner.json').write_text(json.dumps(owner))
        with patch.object(host, 'CONFIG', config), patch.object(host, 'command', return_value='sha256:' + 'a' * 64):
            yield NS(directory=directory, config=config, owner=owner)
    finally:
        shutil.rmtree(directory)


def prepare(s):
    with patch.object(host, 'container', return_value=dict(status='PASS')):
        assert host.run('prepare', s.directory, s.owner)['status'] == 'PASS'


@pytest.mark.parametrize('change', ['helper', 'config', 'image', 'owner', 'topology', 'checkpoint'])
def test_changed_prepared_inputs_cannot_schedule(prepared, change):
    s = prepared; prepare(s)
    path = {'helper': s.directory / 'gateway_native.py', 'config': s.config / 'config.json',
            'owner': s.directory / 'owner.json', 'topology': s.directory / 'gateway-topology.json',
            'checkpoint': s.directory / 'gateway-preparation.json'}.get(change)
    if path:
        path.write_text('{}')
    with patch.object(host, 'container') as run_container:
        if change == 'image':
            with patch.object(host, 'command', return_value='sha256:' + 'b' * 64):
                result = host.run('schedule', s.directory, s.owner, True)
        else:
            result = host.run('schedule', s.directory, s.owner, True)
    assert result['status'] == 'FALLBACK'
    run_container.assert_not_called()
    assert 'PRIVATE' not in json.dumps(result)


def test_cleanup_guard_and_preparation_failure_cannot_launch_scheduler(prepared):
    s = prepared; prepare(s)
    with patch.object(host, 'container') as run_container:
        assert host.run('schedule', s.directory, s.owner, False)['status'] == 'FALLBACK'
    run_container.assert_not_called()
    (s.directory / 'gateway-preparation.json').unlink()
    with patch.object(host, 'container', return_value=dict(status='FALLBACK', error_type='ImportError')):
        result = host.run('prepare', s.directory, s.owner)
    assert result['status'] == 'FALLBACK'
    assert not (s.directory / 'gateway-preparation.json').exists()


def test_successful_preparation_is_immutable_and_schedule_evidence_is_private(prepared):
    s = prepared; prepare(s)
    checkpoint = (s.directory / 'gateway-preparation.json').read_bytes()
    prepare(s)
    assert (s.directory / 'gateway-preparation.json').read_bytes() == checkpoint
    with patch.object(host, 'container', return_value=dict(status='PASS', changed_count=1)):
        result = host.run('schedule', s.directory, s.owner, True)
    path = s.directory / 'gateway-schedule-result.json'
    assert json.loads(path.read_text()) == result
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('failure', ['timeout', 'native', 'malformed', 'success'])
def test_docker_container_is_force_removed_on_every_attach_exit(tmp_path, failure):
    calls = []
    cid = 'c' * 64
    def docker(args, timeout=5):
        calls.append((args, timeout))
        if args[0] == 'create': return cid
        if args[0] == 'rm': return cid
        assert args == ['start', '--attach', cid] and timeout == 30
        if failure == 'timeout': raise subprocess.TimeoutExpired('docker', timeout)
        if failure == 'native':
            raise subprocess.CalledProcessError(1, 'docker', output='GATEWAY_RESULT={"status":"FALLBACK"}')
        if failure == 'malformed': return 'No result'
        return 'Kolla setup log\nGATEWAY_RESULT={"status":"PASS"}'
    with patch.object(host, 'command', side_effect=docker):
        if failure in ('timeout', 'malformed'):
            with pytest.raises((subprocess.TimeoutExpired, host.PreparationCheck)):
                host.container(tmp_path, dict(image_id='sha256:' + 'a' * 64, owner=dict(run='run')), 'schedule', 30)
        else:
            result = host.container(tmp_path, dict(image_id='sha256:' + 'a' * 64, owner=dict(run='run')), 'schedule', 30)
            assert result['status'] == ('PASS' if failure == 'success' else 'FALLBACK')
    assert calls[-1] == (['rm', '--force', cid], 5)
    create = calls[0][0]
    assert '--pull=never' in create and '--timeout' in create and create[-1] == '25'
    assert 'neutron-server' not in create[create.index('--entrypoint') + 1:]


@pytest.mark.parametrize('case', ['clean', 'namespace', 'ovs-port', 'docker-error', 'l3-running'])
def test_real_cleanup_shell_does_not_acknowledge_query_errors_or_leftovers(tmp_path, case):
    plays = yaml.safe_load((ROOT / 'playbooks/09-cleanup.yml').read_text())
    task = next(t for p in plays for t in p['tasks'] if t['name'].endswith('attachments and L3 agent are withdrawn'))
    shell = task['ansible.builtin.shell'].replace('{% raw %}', '').replace('{% endraw %}', '')
    for name, body in {
        'ip': 'echo qrouter-old' if case == 'namespace' else 'echo ovnmeta-keep',
        'docker': ('exit 1' if case == 'docker-error' else
                   'if [ "$1" = ps ]; then echo neutron_l3_agent; elif [ "$1" = inspect ]; then echo ' + ('true' if case == 'l3-running' else 'false') +
                   '; else echo ' + ('qg-old' if case == 'ovs-port' else 'ovn-new-patch') + '; fi'),
    }.items():
        path = tmp_path / name; path.write_text('#!/bin/bash\n' + body + '\n'); path.chmod(0o700)
    result = subprocess.run(['bash', '-c', shell], env=dict(os.environ, PATH=str(tmp_path) + ':' + os.environ['PATH']),
                            capture_output=True, text=True, timeout=5)
    assert (result.returncode == 0) == (case == 'clean'), result.stderr


@pytest.mark.parametrize('case', ['pass', 'helper-failure', 'cleanup-missing', 'api-running'])
def test_real_ansible_always_reaches_api_start_after_optional_attempt(tmp_path, case):
    run = tmp_path / 'run'; run.mkdir()
    helpers = tmp_path / 'helpers'; helpers.mkdir()
    events = tmp_path / 'events'
    payload = helpers / 'gateway_scheduling.py'
    payload.write_text('import json,sys\nfrom pathlib import Path\n'
        f'with Path({str(events)!r}).open("a") as f: f.write("schedule\\n")\n'
        f'print(json.dumps(dict(status={"PASS" if case == "pass" else "FALLBACK"!r},native=dict(changed_count=1))))\n'
        f'sys.exit({0 if case == "pass" else 1})\n')
    binary = tmp_path / 'bin'; binary.mkdir()
    docker = binary / 'docker'; docker.write_text('#!/bin/sh\necho ' + ('true' if case == 'api-running' else 'false') + '\n'); docker.chmod(0o700)
    inventory = tmp_path / 'inventory'
    inventory.write_text('[all:vars]\nansible_connection=local\nansible_python_interpreter=' + sys.executable +
        '\n[control]\ncontroller\ncontroller2\n[network]\nnetwork1\nnetwork2\n')
    facts = [dict(hosts='localhost', gather_facts=False, tasks=[{'ansible.builtin.set_fact': dict(
        migration_run_dir=str(run), cutover_preparation_runtime=dict(directory=str(helpers)))}])]
    if case != 'cleanup-missing':
        facts.append(dict(hosts='network', gather_facts=False, tasks=[
            {'ansible.builtin.set_fact': dict(gateway_cleanup_verified=True)}]))
    phase = yaml.safe_load((ROOT / 'playbooks/10-restore-neutron.yml').read_text())
    attempt = next(p for p in phase if 'Attempt early gateway scheduling' in p['name'])
    start = next(p for p in phase if 'Start Neutron API workers' in p['name'])
    attempt.pop('vars_files'); attempt['become'] = False
    # Execute the production ordering/rescue; replace only the service action
    # to observe the start safely without a systemd daemon or OpenStack lab.
    start.pop('vars_files'); start['become'] = False
    start['tasks'] = [dict(name='Observe API start', **{'ansible.builtin.shell':
        'echo api-{{ inventory_hostname }} >> ' + str(events)})]
    entry = tmp_path / 'check.yml'; entry.write_text(yaml.safe_dump(facts + [attempt, start], sort_keys=False))
    env = dict(os.environ, PATH=str(binary) + ':' + os.environ['PATH'],
               ANSIBLE_FILTER_PLUGINS=str(ROOT / 'plugins/filter'), ANSIBLE_NOCOLOR='1')
    result = subprocess.run([sys.executable, '-m', 'ansible.cli.playbook', '-i', str(inventory), str(entry)],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    rows = events.read_text().splitlines()
    assert set(rows[-2:]) == {'api-controller', 'api-controller2'}
    if case in ('pass', 'helper-failure'):
        assert rows[0] == 'schedule'
    else:
        assert 'schedule' not in rows
    evidence = json.loads((run / 'gateway-scheduling.json').read_text())
    assert evidence['status'] == ('PASS' if case == 'pass' else 'FALLBACK')


def test_preparation_is_before_freeze_and_phase_numbering_is_unchanged():
    phase = yaml.safe_load((ROOT / 'playbooks/07-migrate-db.yml').read_text())
    names = [t['name'] for p in phase for t in p['tasks']]
    assert names.index('[07] Preflight native gateway helper without starting API or scheduling') < names.index('[07] [TIMER] Start Neutron API downtime')
    assert len(list((ROOT / 'playbooks').glob('[0-9][0-9]-*.yml'))) == 14


@pytest.mark.parametrize('leftover', [False, True])
def test_real_ansible_cleanup_failure_blocks_api_on_other_hosts(tmp_path, leftover):
    binary = tmp_path / 'bin'; binary.mkdir()
    for name, body in {
        'ip': 'if [ "$SIM_HOST" = network1 ] && [ "$LEFTOVER" = true ]; then echo qrouter-old; fi',
        'docker': 'if [ "$1" = ps ]; then echo neutron_l3_agent; elif [ "$1" = inspect ]; then echo false; fi',
    }.items():
        file = binary / name; file.write_text('#!/bin/bash\n' + body + '\n'); file.chmod(0o700)
    inventory = tmp_path / 'inventory'
    inventory.write_text('[all:vars]\nansible_connection=local\nansible_python_interpreter=' + sys.executable +
        '\n[network]\nnetwork1\nnetwork2\n[control]\ncontroller\n')
    phase = yaml.safe_load((ROOT / 'playbooks/09-cleanup.yml').read_text())
    cleanup = next(p for p in phase if 'Remove legacy namespaces' in p['name'])
    cleanup.pop('vars_files'); cleanup['become'] = False
    cleanup['tasks'] = [t for t in cleanup['tasks'] if t['name'] in (
        '[09] Verify legacy gateway attachments and L3 agent are withdrawn',
        '[09] Publish per-host legacy gateway withdrawal result')]
    cleanup['environment'] = dict(PATH=str(binary) + ':' + os.environ['PATH'],
                                  SIM_HOST='{{ inventory_hostname }}', LEFTOVER=str(leftover).lower())
    marker = tmp_path / 'api-started'
    start = dict(hosts='control', gather_facts=False, tasks=[
        {'ansible.builtin.command': dict(argv=['touch', str(marker)])}])
    entry = tmp_path / 'check.yml'; entry.write_text(yaml.safe_dump([cleanup, start], sort_keys=False))
    result = subprocess.run([sys.executable, '-m', 'ansible.cli.playbook', '-i', str(inventory), str(entry)],
                            cwd=tmp_path, capture_output=True, text=True, timeout=40)
    assert (result.returncode == 0) == (not leftover), result.stdout + result.stderr
    assert marker.exists() == (not leftover)
