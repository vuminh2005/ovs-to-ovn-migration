import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('reset_validation_under_test', ROOT / 'scripts/reset_validation.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


def inventory():
    data = {key: {'hosts': sorted(value)} for key, value in r.EXPECTED.items()}
    data['all'] = {'children': list(r.EXPECTED) + ['deployment']}
    data['deployment'] = {'hosts': ['localhost']}
    for key in ('ovn-controller', 'ovn-database', 'neutron-ovn-metadata-agent'):
        data[key] = {'children': ['control'] if key == 'ovn-database' else ['network', 'compute']}
    return data


class ResetScopeTests(unittest.TestCase):
    def test_exact_four_nodes_with_full_kolla_groups(self):
        r.validate_inventory(inventory())

    def test_three_nodes_five_nodes_or_other_auxiliary_hosts_rejected(self):
        for update in ('missing-compute2', 'network2', 'auxiliary'):
            data = inventory()
            if update == 'missing-compute2':
                data['compute']['hosts'].remove('compute2')
            elif update == 'network2':
                data['network']['hosts'].append('network2')
            else:
                data['deployment']['hosts'].append('network2')
            with self.subTest(update=update), self.assertRaises(RuntimeError):
                r.validate_inventory(data)

    def test_inventory_excerpt_without_service_groups_is_rejected(self):
        data = inventory()
        del data['ovn-database']
        with self.assertRaisesRegex(RuntimeError, 'full Kolla'):
            r.validate_inventory(data)

    def test_play_guards_precede_guest_stop_and_destroy(self):
        plays = yaml.safe_load((ROOT / 'reset-lab-to-ovs.yml').read_text())
        self.assertTrue(all(p['any_errors_fatal'] for p in plays))
        names = [p['name'] for p in plays]
        self.assertLess(names.index('Verify all four lab identities and tunnel MTU before stopping guests'),
                        names.index('Stop running libvirt guests before Kolla destroy'))
        first = plays[0]['tasks']
        self.assertEqual(first[0]['name'], 'Require explicit destructive reset confirmation')
        stop = next(p for p in plays if p['hosts'] == 'compute')
        self.assertIn('reset_scope_verified', str(stop['tasks'][0]))
        reboot = next(p for p in plays if 'Reboot network' in p['name'])
        self.assertEqual(reboot['hosts'], 'network:compute')
        self.assertEqual(reboot['serial'], 1)

    def test_mtu_and_native_firewall_override_paths(self):
        plays = yaml.safe_load((ROOT / 'reset-lab-to-ovs.yml').read_text())
        copies = [t['ansible.builtin.copy'] for p in plays for t in p.get('tasks', []) if 'ansible.builtin.copy' in t]
        neutron = next(c for c in copies if c['dest'] == '{{ reset_config_path }}/neutron.conf')
        self.assertIn('global_physnet_mtu = {{ reset_tunnel_mtu }}', neutron['content'])
        self.assertFalse(any(c['dest'] == '{{ reset_config_path }}/neutron/neutron.conf' for c in copies))
        ml2 = next(c for c in copies if c['dest'] == '{{ reset_config_path }}/neutron/ml2_conf.ini')
        self.assertIn('path_mtu = {{ reset_tunnel_mtu }}', ml2['content'])
        self.assertIn('max_header_size = 38', ml2['content'])
        chassis = next(p for p in plays if p['name'] == 'Verify source ML2 OVS service containers')
        self.assertTrue(any('firewall' in t['name'] for t in chassis['tasks']))


class ResetCloudTests(unittest.TestCase):
    def cloud(self, *args):
        if args[:3] == ('compute', 'service', 'list'):
            return [{'Binary': 'nova-compute', 'Host': host, 'Status': 'enabled', 'State': 'up'} for host in ('compute1', 'compute2')]
        if args[:3] == ('network', 'agent', 'list'):
            pairs = [('Open vSwitch agent', host) for host in ('network1', 'compute1', 'compute2')]
            pairs += [(kind, 'network1') for kind in ('L3 agent', 'DHCP agent', 'Metadata agent')]
            return [{'Agent Type': kind, 'Host': host, 'Alive': True, 'State': 'UP'} for kind, host in pairs]
        return []

    def test_empty_healthy_four_node_cloud(self):
        with patch.object(r, 'cloud_json', side_effect=self.cloud):
            r.validate_cloud()

    def test_workload_remnant_or_unhealthy_compute_is_rejected(self):
        with patch.object(r, 'cloud_json', return_value=[{'ID': 'leftover'}]):
            with self.assertRaisesRegex(RuntimeError, 'no workload'):
                r.validate_cloud()
        def disabled(*args):
            rows = self.cloud(*args)
            if args[:3] == ('compute', 'service', 'list'):
                rows[0]['Status'] = 'disabled'
            return rows
        with patch.object(r, 'cloud_json', side_effect=disabled):
            with self.assertRaisesRegex(RuntimeError, 'enabled/up'):
                r.validate_cloud()


class ResetCompletionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cache = self.root / 'ubuntu.img'
        self.cache.write_bytes(b'verified official image bytes')
        self.provenance = {'id': 'new-image-id', 'name': 'ovn-validation-ubuntu-24.04',
                           'cache_path': str(self.cache), 'sha256': hashlib.sha256(self.cache.read_bytes()).hexdigest()}
        self.image = {'id': 'new-image-id', 'status': 'active', 'disk_format': 'qcow2',
                      'container_format': 'bare', 'size': self.cache.stat().st_size,
                      'os_hash_algo': 'sha512', 'os_hash_value': hashlib.sha512(self.cache.read_bytes()).hexdigest()}
        self.result = {'image': self.provenance, 'flavor': {'id': 'new-flavor-id', 'name': 'ovn-validation.small'}}

    def cfg(self, root):
        root.mkdir()
        r.save(root / 'validation-prerequisites-config.json', {'source_mtu': 1400, 'target_geneve_mtu': 1392})

    def test_each_reset_has_independent_completion_and_new_uuid_file(self):
        roots = [self.root / 'cycle-1', self.root / 'cycle-2']
        with patch.object(r, 'validate_cloud'), patch.object(r.prerequisites, 'prepare', return_value=self.result), patch.object(r, 'cloud_json', return_value=self.image):
            for root in roots:
                self.cfg(root)
                r.finish(root, object())
                variables = json.loads((root / 'migration-lab.yml').read_text())
                self.assertEqual(variables, {'validation_image': 'new-image-id', 'validation_flavor': 'new-flavor-id', 'target_geneve_mtu': 1392})
                self.assertTrue((root / 'reset-complete.json').exists())
            with self.assertRaisesRegex(RuntimeError, 'already completed'):
                r.finish(roots[0], object())

    def test_wrong_glance_hash_cannot_mark_success(self):
        root = self.root / 'failed-cycle'
        self.cfg(root)
        with patch.object(r, 'validate_cloud'), patch.object(r.prerequisites, 'prepare', return_value=self.result), patch.object(r, 'cloud_json', return_value=dict(self.image, os_hash_value='wrong')):
            with self.assertRaisesRegex(RuntimeError, 'Glance content differs'):
                r.finish(root, object())
        self.assertFalse((root / 'reset-complete.json').exists())

    def test_queued_create_response_is_not_used_for_final_check(self):
        root = self.root / 'retry-cycle'
        self.cfg(root)
        result = dict(self.result, image=dict(self.provenance, status='queued'))
        with patch.object(r, 'validate_cloud'), patch.object(r.prerequisites, 'prepare', return_value=result), patch.object(r, 'cloud_json', return_value=self.image) as show:
            r.finish(root, object())
        show.assert_called_once_with('image', 'show', 'new-image-id')

    def test_ew_image_metadata_is_not_used(self):
        cfg = yaml.safe_load((ROOT / 'group_vars/reset.yml').read_text())
        self.assertEqual(cfg['reset_managed_image_name'], 'ovn-validation-ubuntu-24.04')
        self.assertEqual(cfg['reset_managed_flavor_name'], 'ovn-validation.small')
        self.assertNotIn('ew-ubuntu', (ROOT / 'scripts/reset_validation.py').read_text())


if __name__ == '__main__':
    unittest.main()
