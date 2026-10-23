"""Validate actual OVSDB table encoding instead of JSON-quoting scalar get output."""
import json
import subprocess
import unittest
from unittest.mock import patch

import host_network


class ExternalIdTests(unittest.TestCase):
    def table(self, pairs):
        return json.dumps(dict(headings=['external_ids'], data=[[['map', pairs]]]))

    def test_uuid_ip_and_multiple_physnets_use_explicit_json_table(self):
        values = {'system-id': 'af410387-88e7-425f-a2a7-944fe8b01236',
                  'ovn-encap-ip': '10.1.0.160',
                  'ovn-bridge-mappings': 'physnet1:br-ex,edge:br-edge',
                  'special': 'spaces, "quotes" and \\backslash'}
        with patch.object(host_network, 'command', return_value=self.table(list(map(list, values.items())))) as command:
            for key, expected in values.items():
                with self.subTest(key=key):
                    self.assertEqual(host_network.external_id(key), expected)
            self.assertTrue(all(call.args == (
                'docker', 'exec', 'openvswitch_vswitchd', 'ovs-vsctl', '--timeout=10',
                '--format=json', '--columns=external_ids', 'list', 'Open_vSwitch', '.')
                for call in command.call_args_list))

    def test_realistic_unquoted_get_response_is_not_used(self):
        identity = 'network1'
        with self.assertRaises(json.JSONDecodeError):
            json.loads(identity)
        def ovs(*args):
            if args[0] == 'get':
                return identity
            self.assertEqual(args, ('--format=json', '--columns=external_ids',
                                    'list', 'Open_vSwitch', '.'))
            return self.table([['system-id', identity]])
        with patch.object(host_network, 'ovs', side_effect=ovs):
            self.assertEqual(host_network.external_id('system-id'), identity)

    def test_missing_key_and_empty_map_return_empty_value(self):
        for pairs in ([], [['other-key', 'value']], [['system-id', '']]):
            with self.subTest(pairs=pairs), patch.object(host_network, 'ovs', return_value=self.table(pairs)):
                self.assertEqual(host_network.external_id('system-id'), '')

    def test_non_json_output_is_an_explicit_inspection_error(self):
        for raw in ('', 'not JSON', 'af410387-88e7-425f-a2a7-944fe8b01236'):
            with self.subTest(raw=raw), patch.object(host_network, 'ovs', return_value=raw):
                with self.assertRaisesRegex(RuntimeError, 'external_ids JSON table'):
                    host_network.external_id('system-id')

    def test_missing_ambiguous_and_wrong_column_rows_fail(self):
        for table in (None, [], {}, dict(headings=['other'], data=[]),
                      dict(headings=['external_ids'], data=[]),
                      dict(headings=['external_ids'], data=[[], []]),
                      dict(headings=['external_ids'], data=[None]),
                      dict(headings=['external_ids'], data=[[]]),
                      dict(headings=['external_ids'], data=[[{}, {}]])):
            with self.subTest(table=table), patch.object(host_network, 'ovs', return_value=json.dumps(table)):
                with self.assertRaisesRegex(RuntimeError, 'exactly one'):
                    host_network.external_id('system-id')

    def test_map_tag_shape_and_string_types_are_checked(self):
        for encoded in (None, {}, 'value', ['set', []], ['map'], ['map', {}],
                        ['map', ['pair']], ['map', [[]]], ['map', [['key', 42]]],
                        ['map', [[42, 'value']]], ['map', [['key', 'value', 'extra']]]):
            raw = json.dumps(dict(headings=['external_ids'], data=[[encoded]]))
            with self.subTest(encoded=encoded), patch.object(host_network, 'ovs', return_value=raw):
                with self.assertRaisesRegex(RuntimeError, 'Invalid'):
                    host_network.external_id('system-id')

    def test_duplicate_keys_fail_instead_of_selecting_one_identity(self):
        with patch.object(host_network, 'ovs', return_value=self.table(
                [['system-id', 'first'], ['system-id', 'second']])):
            with self.assertRaisesRegex(RuntimeError, 'duplicate'):
                host_network.external_id('system-id')

    def test_docker_ovsdb_and_timeout_errors_propagate_without_writes(self):
        for exc in (subprocess.CalledProcessError(1, ['docker']),
                    subprocess.TimeoutExpired(['docker'], 20)):
            with self.subTest(error=type(exc).__name__), patch.object(host_network, 'command', side_effect=exc) as command:
                with self.assertRaises(type(exc)):
                    host_network.external_id('system-id')
                self.assertEqual(command.call_count, 1)
                self.assertIn('list', command.call_args.args)
