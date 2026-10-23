"""Exercise the socket ownership and create-only/start boundary without a cloud."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests.support import ROOT
import metadata_handoff as handoff
from ovn_ready import decode, metadata_missing, wait


class Model:
    def __init__(self):
        self.containers = {}
        self.calls = []
        self.enabled = False

    def command(self, argv, check=True):
        self.calls.append(argv)
        out, rc, err = '', 0, ''
        if argv[:3] == ['docker', 'container', 'inspect']:
            value = self.containers.get(argv[3])
            if value is None:
                rc, err = 1, 'Error: No such container: ' + argv[3]
            else:
                out = json.dumps([value])
        elif argv[:2] == ['systemctl', 'is-enabled']:
            out, rc = ('enabled', 0) if self.enabled else ('disabled', 1)
        elif argv[:2] == ['systemctl', 'is-active']:
            out, rc = 'inactive', 3
        elif argv[:2] == ['systemctl', 'show']:
            out = 'loaded' if handoff.OLD_NAME in self.containers else 'not-found'
        elif argv[:2] == ['systemctl', 'disable']:
            if '--now' in argv:
                self.containers[handoff.OLD_NAME]['State']['Running'] = False
            else:
                self.enabled = False
        elif argv[:2] == ['systemctl', 'enable']:
            self.enabled = True
        elif argv[:2] == ['systemctl', 'start']:
            self.containers[handoff.NAME]['State'].update(Status='running', Running=True)
        if check and rc:
            raise subprocess.CalledProcessError(rc, argv, out, err)
        return subprocess.CompletedProcess(argv, rc, out, err)


class Worker:
    def __init__(self, directory, model):
        self.model, self.changed, self.creations = model, False, 0
        self.params = dict(name=handoff.NAME, container_engine='docker', detach=True,
            restart_policy='unless-stopped', labels={handoff.RUN_LABEL: 'run-one'},
            image='mirror/metadata:approved',
            volumes=[str(directory)+':/var/lib/kolla/config_files:ro'])
        self.systemd = SimpleNamespace(enable=self.enable, create_unit_file=self.unit)
        self.dc = SimpleNamespace(inspect_container=lambda name: copy.deepcopy(self.model.containers[name]))

    def enable(self):
        raise AssertionError('Native enable must be suppressed during preparation')

    def unit(self):
        self.systemd.enable()
        return True

    def check_image(self):
        return True

    def check_container(self):
        return self.model.containers.get(handoff.NAME)

    def check_container_differs(self):
        return self.check_container()['Config']['Labels'] != self.params['labels']

    def remove_container(self):
        del self.model.containers[handoff.NAME]

    def create_container(self):
        self.creations += 1
        self.changed = True
        self.model.containers[handoff.NAME] = dict(Id='container-one', Image='sha256:approved',
            State=dict(Status='created', Running=False),
            Config=dict(Labels=copy.deepcopy(self.params['labels'])),
            HostConfig=dict(Binds=self.params['volumes'], RestartPolicy=dict(Name='no')))
        self.systemd.create_unit_file()


class MetadataHandoffTests(unittest.TestCase):
    def test_native_kolla_trailing_slash_and_docker_spelling_resolve_the_same_config(self):
        for bind in ('/etc/kolla/neutron-ovn-metadata-agent/:/var/lib/kolla/config_files/:ro',
                     '/etc/kolla/neutron-ovn-metadata-agent:/var/lib/kolla/config_files:ro',
                     '/etc/kolla/neutron-ovn-metadata-agent/:/var/lib/kolla/config_files/:ro,z'):
            self.assertEqual(handoff.config_directory([bind]),Path('/etc/kolla/neutron-ovn-metadata-agent'))
        for binds in (['/etc/kolla/agent:/var/lib/kolla/config_files/:rw'],
                      ['/etc/kolla/agent:/var/lib/kolla/config_files-else:ro'],
                      ['/etc/kolla/one:/var/lib/kolla/config_files/:ro','/etc/kolla/two:/var/lib/kolla/config_files:ro']):
            with self.assertRaisesRegex(ValueError,'Exactly one read-only'):
                handoff.config_directory(binds)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        (self.directory/'config.json').write_text('{"command":"neutron-ovn-metadata-agent"}')
        (self.directory/'neutron.conf').write_text('secret=do-not-persist-in-evidence\n')
        self.model = Model()
        self.worker = Worker(self.directory, self.model)
        mocked = patch.object(handoff, 'command', side_effect=self.model.command)
        mocked.start()
        self.addCleanup(mocked.stop)

    def prepare(self):
        self.value = handoff.preparation(self.worker)
        return self.value

    def test_preparation_does_not_start_image_or_old_agent_and_disables_unit(self):
        value = self.prepare()
        self.assertEqual(value['status'], 'PREPARED')
        self.assertEqual(self.model.containers[handoff.NAME]['State']['Status'], 'created')
        self.assertFalse(self.model.enabled)
        self.assertNotIn('do-not-persist', json.dumps(value))
        self.assertFalse(any('--now' in args or args[:2] == ['systemctl', 'start'] for args in self.model.calls))
        self.assertEqual((self.directory/handoff.MANIFEST).stat().st_mode & 0o777, 0o600)

    def test_same_run_preparation_retry_reuses_container_and_timestamp(self):
        first = self.prepare()
        self.worker.changed = False
        self.assertEqual(self.prepare(), first)
        self.assertEqual(self.worker.creations, 1)

    def test_running_or_foreign_metadata_is_never_stopped_or_adopted(self):
        self.prepare()
        for change, message in ((lambda d: d['State'].update(Status='running', Running=True), 'already run'),
                (lambda d: d['Config']['Labels'].update({handoff.RUN_LABEL: 'foreign'}), 'not owned')):
            original = copy.deepcopy(self.model.containers[handoff.NAME])
            change(self.model.containers[handoff.NAME])
            self.model.calls.clear()
            with self.assertRaisesRegex(ValueError, message):
                handoff.preparation(self.worker)
            self.assertFalse(self.model.calls)
            self.model.containers[handoff.NAME] = original

    def test_missing_image_fails_without_pull_or_service_changes(self):
        self.worker.check_image = lambda: False
        with self.assertRaisesRegex(ValueError, 'image is not local'):
            self.prepare()
        self.assertFalse(self.model.calls)
        self.assertEqual(self.worker.creations, 0)

    def test_config_change_blocks_handoff_before_stopping_legacy(self):
        value = self.prepare()
        (self.directory/'neutron.conf').write_text('changed')
        self.model.calls.clear()
        with self.assertRaisesRegex(ValueError, 'configuration changed'):
            handoff.start(dict(run='run-one', expected=value))
        self.assertEqual(len(self.model.calls), 1)

    def test_controller_checkpoint_rejects_replaced_container(self):
        value = self.prepare()
        self.model.containers[handoff.NAME]['Id'] = 'container-two'
        # Even a locally updated manifest cannot replace the controller's ID.
        handoff.save_manifest(self.directory, dict(value, container_id='container-two'))
        with self.assertRaisesRegex(ValueError, 'deployment checkpoint'):
            handoff.verify(dict(run='run-one', expected=value))

    def test_docker_restart_policy_change_blocks_handoff(self):
        value = self.prepare()
        self.model.containers[handoff.NAME]['HostConfig']['RestartPolicy']['Name'] = 'always'
        with self.assertRaisesRegex(ValueError,'auto-start was enabled'):
            handoff.start(dict(run='run-one',expected=value))
        self.assertNotIn(['systemctl','start',handoff.UNIT],self.model.calls)

    def test_handoff_stops_old_agent_then_starts_new_without_health_or_chassis_wait(self):
        value = self.prepare()
        self.model.containers[handoff.OLD_NAME] = dict(State=dict(Running=True))
        self.model.calls.clear()
        result = handoff.start(dict(run='run-one', expected=value))
        old_stop = self.model.calls.index(['systemctl', 'disable', '--now', handoff.OLD_UNIT])
        new_start = self.model.calls.index(['systemctl', 'start', handoff.UNIT])
        self.assertLess(old_stop, new_start)
        self.assertEqual(result['status'], 'STARTED')
        self.assertFalse(any('ovn-sbctl' in args for args in self.model.calls))
        self.assertEqual(handoff.ready(dict(run='run-one', expected=value, timeout=1))['status'], 'PASS')

    def test_legacy_docker_still_running_blocks_new_service(self):
        value = self.prepare()
        self.model.containers[handoff.OLD_NAME] = dict(State=dict(Running=True))
        normal = self.model.command
        def stuck(argv, check=True):
            if argv[:3] == ['systemctl', 'disable', '--now']:
                return subprocess.CompletedProcess(argv, 0, '', '')
            return normal(argv, check)
        with patch.object(handoff, 'command', side_effect=stuck):
            with self.assertRaisesRegex(ValueError, 'still running'):
                handoff.start(dict(run='run-one', expected=value))
        self.assertNotIn(['systemctl', 'start', handoff.UNIT], self.model.calls)

    def test_compute_without_old_agent_skips_legacy_service_stop(self):
        self.prepare()
        self.model.calls.clear()
        handoff.start(dict(run='run-one'))
        self.assertFalse(any('--now' in args for args in self.model.calls))

    def test_loaded_old_unit_is_disabled_even_when_its_container_is_absent(self):
        self.prepare()
        normal = self.model.command
        calls = []
        def loaded(argv, check=True):
            calls.append(argv)
            if argv[:2] == ['systemctl', 'show']:
                return subprocess.CompletedProcess(argv, 0, 'loaded', '')
            if argv[:3] == ['systemctl', 'disable', '--now']:
                return subprocess.CompletedProcess(argv, 0, '', '')
            return normal(argv, check)
        with patch.object(handoff, 'command', side_effect=loaded):
            handoff.start(dict(run='run-one'))
        self.assertLess(calls.index(['systemctl', 'disable', '--now', handoff.OLD_UNIT]),
                        calls.index(['systemctl', 'start', handoff.UNIT]))

    def test_health_is_checked_only_after_activation(self):
        self.prepare()
        with self.assertRaisesRegex(ValueError, 'activation checkpoint'):
            handoff.ready(dict(run='run-one', timeout=0.1))
        handoff.start(dict(run='run-one'))
        self.model.containers[handoff.NAME]['State']['Health'] = dict(Status='unhealthy')
        with self.assertRaisesRegex(TimeoutError, 'unhealthy'):
            handoff.ready(dict(run='run-one', timeout=0.01, poll=0.001))


class MetadataReadinessTests(unittest.TestCase):
    def test_registration_requires_correct_host_and_valid_metadata_id(self):
        expected = dict(compute=dict(system_id='compute-system'))
        self.assertTrue(metadata_missing(expected, []))
        self.assertTrue(metadata_missing(expected, [dict(name='compute-system', external_ids={})]))
        row = dict(name='compute-system', external_ids={'neutron:ovn-metadata-id': '59e94c74-a356-5a64-9d9f-9c861700f4d5'})
        self.assertFalse(metadata_missing(expected, [row]))
        self.assertTrue(metadata_missing(expected, [row, row]))

    def test_table_map_decoding_and_selected_placements(self):
        self.assertEqual(decode(['map', [['key', 'value']]]), dict(key='value'))
        cfg = dict(timeout=1, poll=0.1, sb='tcp:db:6642', metadata_hosts=['compute'],
            hosts=dict(compute=dict(system_id='compute-system'), network=dict(system_id='gateway-system')))
        rows = [dict(name='compute-system', external_ids={'neutron:ovn-metadata-id': '59e94c74-a356-5a64-9d9f-9c861700f4d5'})]
        with patch('ovn_ready.query', return_value=rows):
            self.assertEqual(wait(cfg, 'metadata'), dict(status='PASS', checked=1))


if __name__ == '__main__':
    unittest.main()
