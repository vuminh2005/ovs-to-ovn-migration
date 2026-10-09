"""Offline fresh-target freeze gates and OVN Geneve minimum regressions."""
import copy
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml
from test_validation import ROOT, v
from test_mtu_and_ew import inputs
import mtu_plan as m


class FreshTargetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.plan = m.calculate(inputs())
        v.save(self.root/'mtu-calculation.json', self.plan)
        v.save(self.root/'runtime.json', {'mtu_plan_schema_version': 1})

    def target(self):
        return {'controller': dict(self.plan['inputs']['source_configs']['controller'],
                    geneve_max_header_size=38, mechanism_drivers='ovn', tenant_network_types='geneve')}

    def fresh(self):
        intent = m.begin_pre_freeze_collection(self.root)
        records = {host: dict(controller=host, collection_id=intent['collection_id'],
                    collected_at=intent['requested_at'], settings=settings)
                   for host, settings in self.target().items()}
        collection = dict(collection_id=intent['collection_id'], controllers=records)
        v.save(self.root/'mtu-pre-freeze-target-configs.json', collection)
        return collection

    def test_headers_30_and_37_rejected_in_calculation(self):
        for header in (30, 37):
            with self.subTest(header=header):
                evidence = inputs(); evidence['geneve_max_header_size'] = header
                with self.assertRaisesRegex(RuntimeError, 'at least 38'): m.calculate(evidence)

    def test_headers_38_and_larger_supported_in_calculation_and_verification(self):
        for header, target_mtu in ((38, 1392), (46, 1384)):
            with self.subTest(header=header):
                evidence = inputs(); evidence['geneve_max_header_size'] = header
                plan = m.calculate(evidence)
                self.assertEqual((plan['validation_source_mtu'], plan['validation_target_mtu']), (1400, target_mtu))
                v.save(self.root/'mtu-calculation.json', plan)
                target = self.target(); target['controller']['geneve_max_header_size'] = header
                m.verify_target(self.root, target)

    def test_forged_matching_small_headers_fail_target_verification(self):
        for header in (30, 37):
            with self.subTest(header=header):
                forged = copy.deepcopy(self.plan)
                forged['inputs']['geneve_max_header_size'] = header
                forged['geneve_header_bytes'] = header
                v.save(self.root/'mtu-calculation.json', forged)
                target = self.target(); target['controller']['geneve_max_header_size'] = header
                with self.assertRaisesRegex(RuntimeError, 'at least 38'): m.verify_target(self.root, target)
                self.assertEqual(v.read_evidence(self.root, 'mtu-target-config-verification.json')['status'], 'FAIL')

    def test_forged_inputs_cannot_hide_behind_valid_derived_header(self):
        forged = copy.deepcopy(self.plan); forged['inputs']['geneve_max_header_size'] = 30
        v.save(self.root/'mtu-calculation.json', forged)
        with self.assertRaisesRegex(RuntimeError, 'at least 38'): m.verify_target(self.root, self.target())

    def test_malformed_header_plan_rejected_before_network_api_or_updates(self):
        from unittest.mock import Mock
        for header in (30, 37):
            with self.subTest(header=header):
                forged = copy.deepcopy(self.plan); forged['geneve_header_bytes'] = header
                v.save(self.root/'mtu-calculation.json', forged)
                cloud = Mock()
                with self.assertRaisesRegex(RuntimeError, 'at least 38'): m.prepare_networks(cloud, self.root)
                self.assertEqual(cloud.mock_calls, [])

    def test_matching_forged_small_header_cannot_pass_ready(self):
        for header in (30, 37):
            with self.subTest(header=header):
                v.save(self.root/'mtu-calculation.json', self.plan)
                collection = self.fresh()
                forged = copy.deepcopy(self.plan)
                forged['inputs']['geneve_max_header_size'] = header
                forged['geneve_header_bytes'] = header
                v.save(self.root/'mtu-calculation.json', forged)
                collection['controllers']['controller']['settings']['geneve_max_header_size'] = header
                v.save(self.root/'mtu-pre-freeze-target-configs.json', collection)
                with self.assertRaisesRegex(RuntimeError, 'at least 38'): m.require_pre_freeze(self.root)
                self.assertEqual(v.read_evidence(self.root, 'mtu-pre-freeze-target-config-verification.json')['status'], 'FAIL')

    def test_fresh_changed_target_fails_without_overwriting_phase06_proof(self):
        m.verify_target(self.root, self.target())
        phase06 = (self.root/'mtu-target-config-verification.json').read_bytes()
        collection = self.fresh(); collection['controllers']['controller']['settings']['path_mtu'] = 1500
        v.save(self.root/'mtu-pre-freeze-target-configs.json', collection)
        with self.assertRaisesRegex(RuntimeError, 'contradicts'): m.require_pre_freeze(self.root)
        self.assertEqual((self.root/'mtu-target-config-verification.json').read_bytes(), phase06)
        self.assertEqual(v.read_evidence(self.root, 'mtu-pre-freeze-target-config-verification.json')['status'], 'FAIL')

    def test_fresh_proof_records_controller_timestamp_and_attempt(self):
        collection = self.fresh(); m.require_pre_freeze(self.root)
        proof = v.read_evidence(self.root, 'mtu-pre-freeze-target-config-verification.json')
        self.assertEqual(proof['status'], 'PASS'); self.assertEqual(proof['collection'], collection)
        self.assertEqual(proof['configurations'], self.target())

    def test_missing_controller_identity_token_or_timestamp_fails_safely(self):
        for fault in ('missing_controller', 'wrong_identity', 'old_token', 'no_timestamp', 'invalid_timestamp'):
            with self.subTest(fault=fault):
                collection = self.fresh(); record = collection['controllers']['controller']
                if fault == 'missing_controller': collection['controllers'] = {}
                elif fault == 'wrong_identity': record['controller'] = 'another-controller'
                elif fault == 'old_token': record['collection_id'] = 'phase06-token'
                elif fault == 'no_timestamp': record.pop('collected_at')
                else: record['collected_at'] = 'invalid'
                v.save(self.root/'mtu-pre-freeze-target-configs.json', collection)
                with self.assertRaisesRegex(RuntimeError, 'freeze prohibited'): m.require_pre_freeze(self.root)

    def test_retry_invalidates_previous_complete_collection(self):
        self.fresh(); m.require_pre_freeze(self.root)
        prior = v.read_evidence(self.root, 'mtu-pre-freeze-collection.json')['collection_id']
        next_intent = m.begin_pre_freeze_collection(self.root)
        self.assertNotEqual(prior, next_intent['collection_id'])
        with self.assertRaisesRegex(RuntimeError, 'another pre-freeze attempt'): m.require_pre_freeze(self.root)

    def test_historical_read_does_not_require_or_create_new_artifacts(self):
        v.save(self.root/'runtime.json', {'phase_marker_schema_version': 1})
        # Historical configuration can retain a 30-byte default when merely read.
        self.assertEqual(m.config_values('[DEFAULT]\n', '[ml2_type_geneve]\nmax_header_size=30\n')['geneve_max_header_size'], 30)
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        self.assertEqual(m.begin_pre_freeze_collection(self.root), {'required': False})
        m.require_pre_freeze(self.root)
        self.assertEqual({p.name: p.read_bytes() for p in self.root.iterdir()}, before)


ANSIBLE_SIBLING = pathlib.Path(sys.executable).with_name('ansible-playbook')
ANSIBLE = str(ANSIBLE_SIBLING) if ANSIBLE_SIBLING.is_file() else (shutil.which('ansible-playbook') or '')


@unittest.skipUnless(pathlib.Path(ANSIBLE).is_file(), 'Ansible unavailable for local-only orchestration tests')
class PreFreezeOrchestrationTests(unittest.TestCase):
    """Run real gate plays against temporary INI files; replace cloud/service tasks.

    No SSH, Docker, Neutron, Nova, genconfig or systemd command is executed.
    The only appended stop action writes a sentinel in the temporary directory.
    """
    def exercise(self, fault, guests):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d); (root/'metrics').mkdir()
            plan_inputs = inputs()
            plan_inputs['source_configs'] = {host: copy.deepcopy(plan_inputs['source_configs']['controller'])
                                            for host in ('controller1', 'controller2')}
            plan = m.calculate(plan_inputs)
            v.save(root/'mtu-calculation.json', plan)
            v.save(root/'runtime.json', {'mtu_plan_schema_version': 1, 'phase_marker_schema_version': 2})
            phase06 = {h: dict(c, mechanism_drivers='ovn', tenant_network_types='geneve', geneve_max_header_size=38)
                       for h, c in plan['inputs']['source_configs'].items()}
            v.save(root/'mtu-target-configs.json', phase06); m.verify_target(root, phase06)
            old_snapshot = (root/'mtu-target-config-verification.json').read_bytes()
            for host in ('controller1', 'controller2'):
                target = root/host; target.mkdir()
                (target/'neutron.conf').write_text('[DEFAULT]\nglobal_physnet_mtu=1450\n')
                (target/'ml2_conf.ini').write_text('[ml2]\npath_mtu=1450\noverlay_ip_version=4\n'
                    'mechanism_drivers=ovn\ntenant_network_types=geneve\n[ml2_type_geneve]\nmax_header_size=38\n')
            target = root/'controller2'/'ml2_conf.ini'
            if fault == 'changed': target.write_text(target.read_text().replace('path_mtu=1450', 'path_mtu=1500'))
            elif fault == 'missing_file': target.unlink()
            elif fault == 'unreadable': target.unlink(); target.mkdir()  # fails open() even when tests run as root
            helper = (ROOT/'playbooks/mtu-config-read-tasks.yml').read_text()
            for filename in ('neutron.conf', 'ml2_conf.ini'):
                helper = helper.replace('/etc/kolla/neutron-server/'+filename, str(root/'{{ inventory_hostname }}'/filename))
            (root/'mtu-config-read-tasks.yml').write_text(helper)
            plays = yaml.safe_load((ROOT/'playbooks/07-migrate-db.yml').read_text())[:3]
            for play in plays:
                play['become'] = False; play.pop('vars_files')
                play['vars'] = dict(migration_run_dir=str(root), validation_workloads_enabled=guests, ew_workloads_enabled=fault=='ew_readiness')
                for task in play['tasks']:
                    if 'ew_workload.py' in task.get('ansible.builtin.shell', ''):
                        task.pop('ansible.builtin.shell'); task.pop('args')
                        task['ansible.builtin.command'] = dict(argv=[sys.executable, '-c',
                            "raise SystemExit('EW readiness failed: guest MTU is not target')"])
                    if 'precutover-ready' in task.get('ansible.builtin.shell', ''):
                        task.pop('ansible.builtin.shell'); task.pop('args')
                        task['ansible.builtin.command'] = dict(argv=[sys.executable, '-c', 'pass'])
                    for i, arg in enumerate(task.get('ansible.builtin.command', {}).get('argv', [])):
                        if isinstance(arg, str) and arg.startswith('{{ playbook_dir }}/../scripts/'):
                            task['ansible.builtin.command']['argv'][i] = str(ROOT/'scripts'/arg.split('/')[-1])
            # Bootstrap normally publishes this host fact before Phase 07.
            plays[0]['tasks'].insert(0, {'name':'Publish temporary run directory bootstrap fact',
                'ansible.builtin.set_fact':dict(migration_run_dir=str(root))})
            # Same controller target as the real stop play; safely records whether later tasks execute.
            plays.append(dict(name='Mock Neutron stop sentinel', hosts='control', gather_facts=False,
                tasks=[{'name':'Record mock worker stop', 'ansible.builtin.command':dict(argv=[sys.executable, '-c',
                    f"from pathlib import Path; Path({str(root/'neutron-stop')!r}).touch()"])}]))
            (root/'gate.yml').write_text(yaml.safe_dump(plays, sort_keys=False))
            hosts = ['controller1'] if fault == 'missing_controller' else ['controller1', 'controller2']
            (root/'inventory.ini').write_text('[control]\n'+'\n'.join(hosts)+'\n[all:vars]\n'
                f'ansible_connection=local\nansible_python_interpreter={sys.executable}\n')
            result = subprocess.run([ANSIBLE, '-i', str(root/'inventory.ini'), str(root/'gate.yml')],
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
            if fault == 'healthy':
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertTrue((root/'metrics/control_plane_downtime.start').exists())
                self.assertTrue((root/'neutron-stop').exists())
                fresh = v.read_evidence(root, 'mtu-pre-freeze-target-config-verification.json')
                self.assertEqual(fresh['status'], 'PASS')
                self.assertEqual(set(fresh['collection']['controllers']), {'controller1', 'controller2'})
            else:
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual((root/'mtu-pre-freeze-collection.json').exists(),fault!='ew_readiness', result.stdout)
                reason = {'changed':'contradicts', 'missing_file':'No such file or directory',
                          'unreadable':'Is a directory', 'missing_controller':'collection is incomplete',
                          'ew_readiness':'EW readiness failed: guest MTU is not target'}[fault]
                self.assertIn(reason, result.stdout)
                self.assertFalse((root/'metrics/control_plane_downtime.start').exists(), result.stdout)
                self.assertFalse((root/'neutron-stop').exists(), result.stdout)
            self.assertEqual((root/'mtu-target-config-verification.json').read_bytes(), old_snapshot)

    def test_files_changed_after_phase06_block_freeze_with_and_without_guests(self):
        for guests in (True, False):
            with self.subTest(guests=guests): self.exercise('changed', guests)

    def test_missing_or_unreadable_controller_files_block_freeze_with_and_without_guests(self):
        for fault in ('missing_file', 'unreadable', 'missing_controller'):
            for guests in (True, False):
                with self.subTest(fault=fault, guests=guests): self.exercise(fault, guests)

    def test_healthy_fresh_settings_allow_marker_with_and_without_guests(self):
        for guests in (True, False):
            with self.subTest(guests=guests): self.exercise('healthy', guests)

    def test_ew_readiness_failure_blocks_freeze_with_and_without_validation_guests(self):
        for guests in (True, False):
            with self.subTest(guests=guests): self.exercise('ew_readiness', guests)


if __name__ == '__main__': unittest.main()
