"""A metadata-only failure must not replay MTU changes or replace measurements."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

import yaml

from tests.support import ROOT
import tests.recovery.test_pre_stage_recovery as stage_fixture
sys.path.insert(0, str(ROOT/'scripts'))
from metadata_recovery import inspect, record


class MetadataRecoveryTests(unittest.TestCase):
    setUp = stage_fixture.PreStageRecoveryTests.setUp
    put = stage_fixture.PreStageRecoveryTests.put
    workloads = stage_fixture.PreStageRecoveryTests.workloads

    def make(self, root):
        stage_fixture.PreStageRecoveryTests.make(self, root)
        self.globals.write_text('neutron_plugin_agent: ovn\nopenstack_release: "2024.1"\n')
        (root/'metrics/phase05.end').write_text('13')
        (root/'metrics/phase06.start').write_text('14')
        self.put(root, 'metadata-preparation.json', dict(schema=1, run=root.name, status='FAIL'))
        self.put(root, 'diagnostics-config.json', dict(enabled=False))
        (root/'network-mtu-migration.tsv').write_text('aaaabbbb-cccc-dddd-eeee-123456789abc\t1400\t1392\n')

    def ns_captures(self):
        self.workloads()
        cfg = json.loads((self.root/'validation-config.json').read_text())
        cfg['ns']['diagnostics_enabled'] = True
        self.put(self.root, 'validation-config.json', cfg)
        value = dict(schema=1, run=self.root.name, captures={kind:dict(status='RUNNING',
            remote=dict(status='RUNNING', supervisor=21, tcpdump=22, started_at=10, interface='eth1'))
            for kind in ('endpoint', 'guest-snat', 'guest-fip')})
        self.put(self.root, 'ns-diagnostics-checkpoint.json', value)
        return value

    def test_inspect_and_record_preserve_all_existing_measurements_and_mtu(self):
        self.ns_captures()
        before = {p.relative_to(self.root):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertTrue(inspect(self.root, self.inventory)['workloads_enabled'])
        record(self.root)
        self.assertEqual(before, {name:(self.root/name).read_bytes() for name in before})
        self.assertEqual(json.loads((self.root/'orchestration-recovery.json').read_text())['events'][0]['route'],
                         'metadata-preparation')

    def test_freeze_later_phase_and_finalized_phase06_block_resume(self):
        for name in ('metrics/phase06.end', 'metrics/phase07.start', 'metrics/phase08.start',
                     'metrics/control_plane_downtime.start', 'metrics/db_migration.start',
                     'metrics/total.end', 'ns-arm.json', 'pair-d-trigger.json'):
            with self.subTest(name=name):
                path = self.root/name
                path.write_text('15')
                with self.assertRaises(ValueError):
                    inspect(self.root, self.inventory)
                path.unlink()

    def test_wrong_run_inventory_or_incomplete_earlier_phase_block_resume(self):
        self.inventory.write_text('different')
        with self.assertRaisesRegex(ValueError, 'Inventory changed'):
            inspect(self.root, self.inventory)
        self.inventory.write_bytes((self.root/'inventory.before').read_bytes())
        self.put(self.root, 'metadata-preparation.json', dict(schema=1, run='another-run', status='FAIL'))
        with self.assertRaisesRegex(ValueError, 'original completed'):
            inspect(self.root, self.inventory)
        self.put(self.root, 'metadata-preparation.json', dict(schema=1, run=self.root.name, status='FAIL'))
        (self.root/'metrics/phase05.end').unlink()
        with self.assertRaisesRegex(ValueError, 'Completed ordered'):
            inspect(self.root, self.inventory)

    def test_mtu_journal_is_required_and_malformed_or_duplicate_rows_block_resume(self):
        path = self.root/'network-mtu-migration.tsv'
        row = path.read_text()
        for value in ('network\t1400\t1392\n', 'network\t1400\n', row+row,
                      row.replace('1392', '1400'), row.replace('1392', '0')):
            with self.subTest(value=value):
                path.write_text(value)
                with self.assertRaises(ValueError):
                    inspect(self.root, self.inventory)
        path.unlink()
        with self.assertRaises(FileNotFoundError):
            inspect(self.root, self.inventory)

    def test_finalized_or_missing_original_ns_captures_block_resume_without_mutation(self):
        value = self.ns_captures()
        for fault in ('stopped', 'remote-stopped', 'missing', 'wrong-run'):
            changed = json.loads(json.dumps(value))
            if fault == 'stopped':
                changed['captures']['endpoint']['status'] = 'STOPPED'
            elif fault == 'remote-stopped':
                changed['captures']['endpoint']['remote']['status'] = 'STOPPED'
            elif fault == 'missing':
                del changed['captures']['guest-fip']
            else:
                changed['run'] = 'another-run'
            self.put(self.root, 'ns-diagnostics-checkpoint.json', changed)
            before = {p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
            with self.subTest(fault=fault), self.assertRaisesRegex(ValueError, 'diagnostic captures'):
                inspect(self.root, self.inventory)
            self.assertEqual(before, {p:p.read_bytes() for p in before})

    def execute(self, source_ovs=True, controller_running=False):
        self.workloads()
        plays = yaml.safe_load((ROOT/'playbooks/recovery/resume-metadata-preparation.yml').read_text())[:3]
        harness = self.parent/'harness'
        harness.mkdir()
        (harness/'scripts').symlink_to(ROOT/'scripts', target_is_directory=True)
        for play in plays:
            play['become'] = False
            if 'vars_files' in play:
                play['vars_files'] = [str((ROOT/'playbooks/recovery'/file).resolve()) for file in play['vars_files']]
        plays.append(dict(name='Check original settings', hosts='localhost', gather_facts=False, tasks=[
            {'ansible.builtin.assert': {'that': [
                "migration_run_dir == migration_resume_run_dir", "migration_run_id == 'run-one'",
                "validation_workloads_enabled | bool", "not validation_allow_pre_cutover_guest_reboot | bool",
                "not migration_diagnostics_enabled | bool",
                "chassis_count_runtime | int == 3", "target_geneve_mtu | int == 1392"]}}]))
        playbook = harness/'playbooks/recovery/recover.yml'
        playbook.parent.mkdir(parents=True)
        playbook.write_text(yaml.safe_dump(plays, sort_keys=False))
        binaries = harness/'bin'
        binaries.mkdir()
        live_config = harness/'ml2_conf.ini'
        live_config.write_text('[ml2]\nmechanism_drivers='+('openvswitch,l2population' if source_ovs else 'ovn')+
                               '\ntenant_network_types=vxlan\n')
        docker = binaries/'docker'
        docker.write_text('#!/usr/bin/env python3\nimport json,sys\n'
            'if sys.argv[1:4]==["container","inspect","ovn_controller"]:print(json.dumps([{"State":{"Running":'+
            repr(controller_running)+'}}]))\n'
            'elif sys.argv[1]=="inspect":print(json.dumps([{"State":{"Running":True}}]))\n'
            'elif sys.argv[1:3]==["exec","neutron_server"]:exec(sys.argv[5].replace("/etc/neutron/plugins/ml2/ml2_conf.ini",'+
            repr(str(live_config))+'))\n'
            'elif "br-exists" in sys.argv:pass\n'
            'else:raise SystemExit(9)\n')
        docker.chmod(0o700)
        env = dict(os.environ, PATH=str(binaries)+':'+os.environ['PATH'], ANSIBLE_NOCOLOR='1')
        return subprocess.run([sys.executable, '-m', 'ansible.cli.playbook', '-i', str(self.inventory), str(playbook),
            '-e', 'migration_resume_run_dir='+str(self.root)], capture_output=True, text=True, env=env, timeout=60)

    def test_real_ansible_restores_runtime_without_replaying_mtu_or_changing_baselines(self):
        self.workloads()
        before = {p.relative_to(self.root):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result = self.execute()
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertEqual(before, {name:(self.root/name).read_bytes() for name in before})
        self.assertTrue((self.root/'orchestration-recovery.json').exists())

    def test_real_ansible_rejects_live_ovn_before_preparation(self):
        result = self.execute(source_ovs=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root/'orchestration-recovery.json').exists())

    def test_real_ansible_rejects_running_ovn_controller_before_preparation(self):
        result = self.execute(controller_running=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Require OVN controller to remain stopped', result.stdout)
        self.assertFalse((self.root/'orchestration-recovery.json').exists())

    def test_route_runs_only_metadata_suffix_then_existing_freeze_and_cutover(self):
        plays = yaml.safe_load((ROOT/'playbooks/recovery/resume-metadata-preparation.yml').read_text())
        imports = [play['import_playbook'] for play in plays if 'import_playbook' in play]
        self.assertEqual(imports, ['../'+name for name in ('06-prepared-metadata.yml', '07-migrate-db.yml',
            '08-cutover.yml', '09-cleanup.yml', '10-restore-neutron.yml', '11-validate.yml',
            '12-workload-validation.yml', '13-report.yml')])
        suffix = (ROOT/'playbooks/06-prepared-metadata.yml').read_text()
        self.assertNotIn('network set --mtu', suffix)
        self.assertNotIn('prepare-dhcp', suffix)
        self.assertNotIn('genconfig --tags neutron', suffix)
        self.assertNotIn('Start phase 06', suffix)
        self.assertNotIn('anchor-start', suffix)


if __name__ == '__main__':
    unittest.main()
