"""Read-only collection tests: mock every remote command and SSH operation."""
import contextlib
import io
import json
import pathlib
import sys
import tempfile
import unittest
import uuid
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import ew_collect_bootstrap as collect


class CollectionTests(unittest.TestCase):
    def remote(self, role, failed=False):
        output = io.StringIO(); commands = []
        def run(argv, **kw):
            commands.append(argv)
            return NS(returncode=2 if failed else 0,
                stdout='secret-cookie-value' if failed else '[]' if 'psql' in argv else 'read-only-value',
                stderr='secret-cookie-value')
        with patch('subprocess.run', side_effect=run), patch('sys.argv', ['-', role]), \
             patch.object(pathlib.Path, 'is_dir', return_value=False), contextlib.redirect_stdout(output):
            exec(compile(collect.REMOTE_CODE, '<guest-read-only-collector>', 'exec'), {})
        return json.loads(output.getvalue()), commands

    def test_postgresql_queries_are_read_only_and_exclude_credentials(self):
        result, commands = self.remote('ew-db')
        sql = [c[-1] for c in commands if 'psql' in c]
        self.assertGreater(len(sql), 6)
        self.assertTrue(all(s.startswith('BEGIN READ ONLY; SELECT ') and s.endswith('; COMMIT;') for s in sql))
        self.assertTrue(all('-X' in c and 'ON_ERROR_STOP=1' in c for c in commands if 'psql' in c))
        for forbidden in ('pg_authid', 'rolpassword', 'SELECT *', 'options FROM pg_hba', 'pg_dump'):
            self.assertNotIn(forbidden, '\n'.join(sql))
        self.assertIn('database_permissions', result); self.assertIn('authentication_rules', result)
        self.assertTrue(all(c[:2] == ['systemctl', 'show'] or c[0] in ('sudo', 'ss', 'pg_lsclusters', 'dpkg-query') for c in commands))

    def test_rabbitmq_queries_only_known_read_operations(self):
        result, commands = self.remote('ew-queue')
        self.assertIn('listeners', result); self.assertIn('vhost_permissions', result)
        text = repr(commands)
        for forbidden in ('export_definitions', 'set_permissions', 'add_user', 'change_password', 'purge_queue', 'reset', 'stop', 'start'):
            self.assertNotIn(forbidden, text)
        for command in commands:
            if 'rabbitmqctl' in command:
                self.assertIn(command[command.index('-q')+1], ('list_users', 'list_vhosts', 'list_user_permissions',
                    'list_permissions', 'list_topic_permissions', 'eval'))
                if 'eval' in command: self.assertTrue(command[-1].startswith('application:get_env(rabbit,'))

    def test_failure_output_never_exposes_cookie_diagnostics(self):
        for role in ('ew-db', 'ew-queue'):
            result, _ = self.remote(role, failed=True)
            self.assertNotIn('secret-cookie-value', json.dumps(result))
            self.assertEqual(result['version']['status'], 'UNAVAILABLE')

    def test_source_search_collects_paths_not_contents_or_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root/'original-bootstrap.sh').write_text('PASSWORD=never-print-this')
            (root/'.bash_history').write_text('PASSWORD=also-private')
            result = collect.source_candidates([root])
            self.assertEqual(len(result['paths']), 1)
            self.assertNotIn('never-print-this', json.dumps(result)); self.assertNotIn('also-private', json.dumps(result))
            self.assertTrue(collect.source_candidates([root], limit=0)['truncated'])

    def test_verified_namespace_collection_never_installs_or_changes_services(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root/'ew-measurement-config.json').write_text('{}'); (root/'ew-config.json').write_text('{}')
            names = ('ew-app', 'ew-db', 'ew-queue')
            catalog = dict(servers={name: dict(server=str(uuid.uuid4()), port=str(uuid.uuid4()),
                network=str(uuid.uuid4()), ip=f'192.168.101.{11+i}') for i, name in enumerate(names)})
            cloud = NS(network=Mock()); cloud.network.get_network.return_value = NS(provider_network_type='vxlan')
            cloud.network.get_port.return_value = NS(mac_address='fa:16:3e:00:00:01')
            transport = Mock(); transport.access.return_value = dict(host='network1', namespace='qrouter-exact')
            transport.profile.side_effect = lambda vm, access: dict(server=vm['server'], boot='observed-boot',
                interfaces=[dict(address='fa:16:3e:00:00:01', operstate='UP', addr_info=[dict(local=vm['ip'], family='inet')])],
                routes=[dict(dst='default',gateway='192.168.101.1')])
            transport.guest_argv.return_value = ['ssh', 'verified-guest']; transport.run.return_value = '{}'
            with patch.object(collect, 'resolve_ew', return_value=catalog), patch.object(collect, 'source_candidates', return_value={}):
                result = collect.collect(root, cloud, transport)
                with self.assertRaisesRegex(RuntimeError, 'already exists'): collect.collect(root, cloud, transport)
            self.assertFalse(result['adapters_ready'])
            evidence = json.loads((root/'ew-bootstrap-live-evidence.json').read_text())
            self.assertEqual(evidence['original_bootstrap_sources'], 'UNRESOLVED')
            self.assertEqual(set(evidence['guests']), set(names))
            transport.install.assert_not_called(); transport.operation.assert_not_called()
            self.assertEqual(transport.run.call_count, 3)
            self.assertEqual((root/'ew-bootstrap-live-evidence.json').stat().st_mode & 0o077, 0)

    def test_collection_entrypoint_has_no_provisioning_or_measurement_import(self):
        plays = yaml.safe_load((ROOT/'ew-collect-bootstrap.yml').read_text())
        self.assertEqual(len(plays), 1)
        self.assertTrue(plays[0]['any_errors_fatal'])
        self.assertFalse(any('import_playbook' in k for p in plays for k in p))
        source = (ROOT/'ew-collect-bootstrap.yml').read_text()
        self.assertNotIn('ew_workload.py', source); self.assertNotIn('ew_provision.py', source)
        self.assertIn('ew-config-tasks.yml', source)


if __name__=='__main__': unittest.main()
