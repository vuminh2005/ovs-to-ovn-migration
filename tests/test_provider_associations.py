"""Caracal compatibility UPDATE against SQLite, and Phase-07 fail-closed ordering.

Test dependencies: pytest, PyYAML and SQLAlchemy. No Neutron or cloud is needed.
The SQLite model mirrors Neutron's composite PK and unique resource_id.
"""
import contextlib
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import sqlalchemy as sa
from sqlalchemy.orm import declarative_base, sessionmaker
import yaml
from test_validation import ROOT
import provider_associations as p


Base = declarative_base()


class Association(Base):
    __tablename__ = 'providerresourceassociations'
    provider_name = sa.Column(sa.String(255), nullable=False, primary_key=True)
    resource_id = sa.Column(sa.String(36), nullable=False, primary_key=True, unique=True)


class ProviderAssociationTests(unittest.TestCase):
    def setUp(self):
        self.engine = sa.create_engine('sqlite:///:memory:')
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)

    def seed(self, providers):
        with self.sessions.begin() as session:
            session.add_all(Association(resource_id=f'00000000-0000-0000-0000-{i:012d}', provider_name=name)
                            for i, name in enumerate(providers, 1))

    def rows(self):
        with self.sessions() as session:
            return p.snapshot(session, Association)

    def convert(self):
        with self.sessions.begin() as session:
            return p.migrate(session, Association)

    def test_each_legacy_provider_is_converted_in_place(self):
        self.seed(p.LEGACY_PROVIDERS)
        before = self.rows()
        self.assertEqual(self.convert(), 4)
        after = self.rows()
        self.assertEqual([r['resource_id'] for r in before], [r['resource_id'] for r in after])
        for original, converted in zip(before, after):
            with self.subTest(provider=original['provider_name']):
                self.assertEqual(converted['provider_name'], 'ovn')
        self.assertEqual(p.verification(after)['status'], 'PASS')

    def test_existing_ovn_and_unrelated_provider_are_unchanged(self):
        self.seed(['ovn', 'third_party', 'single_node'])
        self.assertEqual(self.convert(), 1)
        self.assertEqual([r['provider_name'] for r in self.rows()], ['ovn', 'third_party', 'ovn'])

    def test_no_legacy_rows_is_idempotent_success(self):
        self.seed(['ovn'])
        before = self.rows()
        self.assertEqual(self.convert(), 0)
        self.assertEqual(self.convert(), 0)
        self.assertEqual(self.rows(), before)
        self.assertEqual(p.verification(self.rows()), dict(status='PASS', legacy_provider_count=0, legacy_providers=[]))

    def test_second_conversion_changes_zero_rows(self):
        self.seed(p.LEGACY_PROVIDERS)
        self.assertEqual(self.convert(), 4)
        first = self.rows()
        self.assertEqual(self.convert(), 0)
        self.assertEqual(self.rows(), first)

    def test_empty_table_is_success_with_zero_changes(self):
        self.assertEqual(self.convert(), 0)
        self.assertEqual(p.verification(self.rows())['status'], 'PASS')

    def test_failed_transaction_rolls_back_in_place_conversion(self):
        self.seed(['single_node'])
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            with self.sessions.begin() as session:
                self.assertEqual(p.migrate(session, Association), 1)
                raise RuntimeError('interrupted')
        self.assertEqual(self.rows()[0]['provider_name'], 'single_node')

    def test_verification_fails_on_any_remaining_legacy_provider(self):
        for provider in p.LEGACY_PROVIDERS:
            row = dict(resource_id='router', provider_name=provider)
            with self.subTest(provider=provider):
                self.assertEqual(p.verification([dict(resource_id='other', provider_name='ovn'), row]),
                    dict(status='FAIL', legacy_provider_count=1, legacy_providers=[row]))

    def test_malformed_snapshot_never_passes(self):
        for rows in (None, {}, [None], [{}], [dict(resource_id='router')],
                     [dict(resource_id='router', provider_name='')]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                p.verification(rows)

    def test_failure_verification_is_persisted_before_cli_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            rows = [dict(resource_id='router', provider_name='single_node')]
            (root/'provider-associations.after.json').write_text(json.dumps(rows))
            result = subprocess.run([sys.executable, str(ROOT/'scripts/provider_associations.py'),
                'verify', '--run-dir', str(root)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            artifact = root/'provider-associations-verification.json'
            self.assertEqual(json.loads(artifact.read_text()), p.verification(rows))
            self.assertEqual(artifact.stat().st_mode & 0o777, 0o600)

    def test_success_verification_cli_and_persisted_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root/'provider-associations.after.json').write_text('[{"resource_id":"router","provider_name":"ovn"}]')
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(p.main(['verify', '--run-dir', str(root)]), 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result, json.loads((root/'provider-associations-verification.json').read_text()))
            self.assertEqual(result, dict(status='PASS', legacy_provider_count=0, legacy_providers=[]))

    def test_missing_or_invalid_snapshot_persists_fail(self):
        for content in (None, '', '{}', 'not json', '[{}]'):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as tmp:
                root = pathlib.Path(tmp)
                if content is not None:
                    (root/'provider-associations.after.json').write_text(content)
                result = p.verify_snapshot(root)
                self.assertEqual(result['status'], 'FAIL')
                self.assertIsNone(result['legacy_provider_count'])
                self.assertEqual(json.loads((root/'provider-associations-verification.json').read_text()), result)

    def test_database_errors_do_not_expose_connection_credentials(self):
        with patch.object(p, 'neutron_operation', side_effect=RuntimeError('mysql://user:secret@db/neutron')), \
                contextlib.redirect_stderr(io.StringIO()) as error, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(p.main(['migrate']), 1)
        self.assertNotIn('secret', error.getvalue())
        self.assertIn('migrate failed (RuntimeError)', error.getvalue())
        self.assertEqual(output.getvalue(), '')


class Phase07Tests(unittest.TestCase):
    def setUp(self):
        self.plays = yaml.safe_load((ROOT/'playbooks/07-migrate-db.yml').read_text())
        self.tasks = [task for play in self.plays for task in play['tasks']]

    def named(self, name):
        return next(task for task in self.tasks if task['name'] == name)

    def test_required_sequence_keeps_compatibility_verification_inside_db_metric(self):
        names = [task['name'] for task in self.tasks]
        ordered = [
            'Mark phase 07 and control-plane downtime start',
            'Stop and disable neutron-server through Kolla systemd',
            'Snapshot ProviderResourceAssociation before authoritative migrate mode',
            'Persist provider associations BEFORE on the deployment host',
            'Mark DB migration start',
            'Run neutron-ovn-db-sync-util migrate',
            'Persist authoritative migrate stdout stderr and return code',
            'Stop Phase 07 immediately after a failed authoritative migration',
            'Apply Caracal L3 provider-association compatibility migration',
            'Snapshot ProviderResourceAssociation after compatibility conversion commits',
            'Persist provider associations AFTER on the deployment host',
            'Persist and enforce fail-closed verification of legacy provider associations',
            'Mark DB migration end',
            'Verify NB logical topology and SB datapaths',
        ]
        positions = [names.index(name) for name in ordered]
        self.assertEqual(positions, sorted(positions))
        start = self.named('Mark DB migration start')['ansible.builtin.shell']
        self.assertIn('/metrics/db_migration.start', start)
        early = self.named(ordered[0])['ansible.builtin.command']['argv']
        self.assertIn('--freeze-start', early)
        self.assertEqual(early[4:6], ['07', 'start'])
        self.assertFalse(any('db_migration.start' in arg for arg in early))
        self.assertIn('/metrics/db_migration.end', self.named('Mark DB migration end')['ansible.builtin.shell'])

    def test_db_start_is_unique_local_and_immediately_after_before_persistence(self):
        names = [task['name'] for task in self.tasks]
        start_index = names.index('Mark DB migration start')
        self.assertEqual(names[start_index-1], 'Persist provider associations BEFORE on the deployment host')
        self.assertEqual(names[start_index+1], 'Run neutron-ovn-db-sync-util migrate')
        writes = [task for task in self.tasks if 'db_migration.start' in task.get('ansible.builtin.shell', '')]
        self.assertEqual(writes, [self.named('Mark DB migration start')])
        start = writes[0]
        end = self.named('Mark DB migration end')
        self.assertEqual(start['ansible.builtin.shell'], end['ansible.builtin.shell'].replace('db_migration.end', 'db_migration.start'))
        self.assertIn('set -euo pipefail', start['ansible.builtin.shell'])
        self.assertIn('date +%s.%N >', start['ansible.builtin.shell'])
        self.assertEqual(start['delegate_to'], 'localhost')
        self.assertTrue(start['run_once'])
        self.assertFalse(start['changed_when'])
        self.assertEqual(start['args']['executable'], '/bin/bash')
        end_index = names.index('Mark DB migration end')
        self.assertEqual(names[end_index-1], 'Persist and enforce fail-closed verification of legacy provider associations')
        self.assertEqual(names[end_index+1], 'Verify NB logical topology and SB datapaths')

    def test_authoritative_migrate_mode_and_fail_closed_guards_are_preserved(self):
        self.assertTrue(self.plays[2]['any_errors_fatal'])
        utility = self.named('Run neutron-ovn-db-sync-util migrate')
        self.assertIn('--ovn-neutron_sync_mode migrate', utility['ansible.builtin.shell'])
        self.assertTrue(utility['run_once'])
        self.assertEqual(utility['register'], 'db_sync_migrate')
        guard = self.named('Stop Phase 07 immediately after a failed authoritative migration')
        self.assertEqual(guard['ansible.builtin.assert']['that'], 'db_sync_migrate.rc == 0')
        verify = self.named('Persist and enforce fail-closed verification of legacy provider associations')
        self.assertIn('verify', verify['ansible.builtin.command']['argv'])
        self.assertNotIn('failed_when', verify)
        self.assertNotIn('ignore_errors', verify)

    def test_artifacts_remain_local_private_and_include_both_sync_streams(self):
        artifacts = {}
        for task in self.tasks:
            copy = task.get('ansible.builtin.copy', {})
            if task.get('delegate_to') == 'localhost' and copy:
                artifacts[copy['dest'].split('/')[-1]] = copy
                self.assertEqual(copy['mode'], '0600')
        self.assertTrue({'provider-associations.before.json', 'provider-associations.after.json',
            'provider-associations-compatibility.json', 'db-sync-migrate.log'}.issubset(artifacts))
        log = artifacts['db-sync-migrate.log']['content']
        for token in ('db_sync_migrate.rc', 'db_sync_migrate.stdout', 'db_sync_migrate.stderr'):
            self.assertIn(token, log)

    def test_helper_runs_in_existing_neutron_image_after_api_freeze(self):
        migration_play = next(p for p in self.plays if 'provider_association_container_command' in p.get('vars', {}))
        command = migration_play['vars']['provider_association_container_command']
        self.assertIn('neutron_server_image.stdout', command)
        self.assertIn('/etc/kolla/neutron-server:/var/lib/kolla/config_files:ro', command)
        self.assertIn('provider_associations.py:/opt/provider_associations.py:ro', command)
        self.assertIn('kolla_set_configs', migration_play['vars']['provider_association_entrypoint'])
        self.assertEqual(migration_play['hosts'], 'control')
        self.assertEqual(self.plays[1]['tasks'][0]['ansible.builtin.systemd_service']['state'], 'stopped')
        for name in ('Snapshot ProviderResourceAssociation before authoritative migrate mode',
                     'Apply Caracal L3 provider-association compatibility migration',
                     'Snapshot ProviderResourceAssociation after compatibility conversion commits'):
            task = self.named(name)
            self.assertTrue(task['run_once'])
            self.assertIn('set -euo pipefail', task['ansible.builtin.shell'])

    def test_report_still_uses_historical_db_metric_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp); (root/'metrics').mkdir()
            (root/'metrics/db_migration.start').write_text('100')
            (root/'metrics/db_migration.end').write_text('125')
            (root/'provider-associations-verification.json').write_text('{"status":"PASS","legacy_provider_count":0,"legacy_providers":[]}')
            subprocess.run([sys.executable, str(ROOT/'scripts/migration_report.py'), str(root), 'run', 'inventory'],
                           check=True, stdout=subprocess.DEVNULL)
            self.assertEqual(json.loads((root/'migration-report.json').read_text())['db_migration_seconds'], 25)


if __name__ == '__main__':
    unittest.main()
