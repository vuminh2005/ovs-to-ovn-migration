"""Read-only configuration diagnostics; fixtures never access guest networking."""
import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location(
    'diagnostic_guest', pathlib.Path(__file__).parents[1]/'scripts/guest_probe.py')
guest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guest)


class NetworkDiagnosticsTests(unittest.TestCase):
    interface = {'ifname': 'ens3', 'ifindex': 2, 'address': 'fa:16:3e:11:22:33'}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)

    def write(self, path, content):
        target = self.root/path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return target

    def network(self, settings, name='10-guest.network', directory='run'):
        return self.write(directory+'/systemd/network/'+name,
                          '[Match]\nName=ens3\n'+settings)

    def diagnose(self):
        return guest.network_diagnostics(self.interface, self.root)

    def test_static_networkd_mtu_is_distinct_from_enabled_dhcp(self):
        self.network('[Link]\nMTUBytes=1450\n[DHCPv4]\nUseMTU=yes\n')
        result = self.diagnose()
        self.assertEqual(result['network_backend'], 'networkd')
        self.assertEqual(result['configured_static_mtu'], 1450)
        self.assertTrue(result['dhcp_use_mtu'])
        self.assertEqual(result['mtu_configuration'], 'static_mtu')

    def test_dhcp_use_mtu_disabled(self):
        self.network('[DHCP]\nUseMTU=no\n')
        result = self.diagnose()
        self.assertIsNone(result['configured_static_mtu'])
        self.assertFalse(result['dhcp_use_mtu'])
        self.assertEqual(result['mtu_configuration'], 'dhcp_mtu_disabled')

    def test_enabled_without_static_mtu(self):
        self.network('[DHCPv4]\nUseMTU=true\n')
        result = self.diagnose()
        self.assertIsNone(result['configured_static_mtu'])
        self.assertTrue(result['dhcp_use_mtu'])
        self.assertEqual(result['mtu_configuration'], 'dhcp_mtu_enabled')

    def test_unexposed_setting_stays_unknown(self):
        self.network('[Network]\nDHCP=ipv4\n')
        result = self.diagnose()
        self.assertEqual(result['network_backend'], 'networkd')
        self.assertIsNone(result['dhcp_use_mtu'])
        self.assertEqual(result['mtu_configuration'], 'unknown')

    def test_missing_configuration_or_interface_stays_unknown(self):
        self.assertEqual(self.diagnose()['mtu_configuration'], 'unknown')
        self.assertEqual(guest.network_diagnostics(None, self.root)['network_backend'], 'unknown')

    def test_runtime_selected_file_wins_over_match_guess(self):
        self.network('[Link]\nMTUBytes=9999\n', name='00-unrelated.network')
        self.network('[DHCPv4]\nUseMTU=yes\n', name='20-active.network', directory='etc')
        self.write('run/systemd/netif/links/2', 'NETWORK_FILE=/etc/systemd/network/20-active.network\n')
        result = self.diagnose()
        self.assertIsNone(result['configured_static_mtu'])
        self.assertTrue(result['dhcp_use_mtu'])
        self.assertEqual(result['network_config_selection'], 'networkd_active')

    def test_etc_network_file_shadows_run_with_same_name(self):
        self.network('[Link]\nMTUBytes=1450\n[DHCPv4]\nUseMTU=no\n')
        self.network('[DHCPv4]\nUseMTU=yes\n', directory='etc')
        self.assertEqual(self.diagnose()['mtu_configuration'], 'dhcp_mtu_enabled')

    def test_dropin_order_shadowing_and_empty_mtu_reset(self):
        self.network('[Link]\nMTUBytes=1450\n[DHCPv4]\nUseMTU=yes\n')
        self.write('run/systemd/network/10-guest.network.d/20-dhcp.conf', '[DHCPv4]\nUseMTU=no\n')
        self.write('etc/systemd/network/10-guest.network.d/20-dhcp.conf', '[DHCP]\nUseMTU=yes\n')
        self.write('etc/systemd/network/10-guest.network.d/90-mtu.conf', '[Link]\nMTUBytes=\n')
        self.assertEqual(self.diagnose()['mtu_configuration'], 'dhcp_mtu_enabled')
        self.write('run/systemd/network/10-guest.network.d/95-dhcp.conf', '[DHCPv4]\nUseMTU=no\n')
        self.assertEqual(self.diagnose()['mtu_configuration'], 'dhcp_mtu_disabled')

    def test_dropins_follow_runtime_selected_file(self):
        self.network('[DHCPv4]\nUseMTU=yes\n')
        self.write('run/systemd/netif/links/2', 'NETWORK_FILE=/run/systemd/network/10-guest.network\n')
        self.write('etc/systemd/network/10-guest.network.d/99-override.conf', '[Link]\nMTUBytes=1450\n')
        self.assertEqual(self.diagnose()['configured_static_mtu'], 1450)

    def test_other_nics_and_ipv6_use_mtu_are_ignored(self):
        self.write('etc/systemd/network/00-other.network', '[Match]\nName=eth9\n[Link]\nMTUBytes=9000\n')
        self.network('[DHCPv4]\nUseMTU=yes\n[IPv6AcceptRA]\nUseMTU=no\n')
        self.assertEqual(self.diagnose()['mtu_configuration'], 'dhcp_mtu_enabled')

    def test_unknown_match_criteria_do_not_guess_later_network(self):
        self.write('etc/systemd/network/00-driver.network', '[Match]\nDriver=virtio_net\n[Link]\nMTUBytes=9000\n')
        self.network('[DHCPv4]\nUseMTU=yes\n')
        result = self.diagnose()
        self.assertEqual(result['mtu_configuration'], 'unknown')
        self.assertEqual(result['network_config_note'], 'networkd_match_unknown')

    def test_netplan_mtu_and_mac_matched_interface(self):
        self.write('etc/netplan/50-cloud-init.yaml', '''network:
  version: 2
  renderer: networkd
  ethernets:
    validation-nic:
      match: {macaddress: "fa:16:3e:11:22:33"}
      set-name: ens3
      mtu: 1450
      dhcp4-overrides: {use-mtu: true}
    unrelated:
      mtu: 9000
''')
        result = self.diagnose()
        self.assertEqual(result['configured_static_mtu'], 1450)
        self.assertTrue(result['dhcp_use_mtu'])
        self.assertEqual(result['mtu_configuration'], 'static_mtu')

    def test_netplan_run_shadowing_and_later_yaml_merge(self):
        self.write('etc/netplan/50-guest.yaml', 'network: {renderer: networkd, ethernets: {ens3: {mtu: 9000}}}')
        self.write('run/netplan/50-guest.yaml', 'network: {renderer: networkd, ethernets: {ens3: {dhcp4: true}}}')
        self.write('etc/netplan/90-dhcp.yaml', 'network: {ethernets: {ens3: {dhcp4-overrides: {use-mtu: false}}}}')
        result = self.diagnose()
        self.assertIsNone(result['configured_static_mtu'])
        self.assertEqual(result['mtu_configuration'], 'dhcp_mtu_disabled')

    def test_generated_networkd_use_mtu_overrides_netplan_intent(self):
        self.write('etc/netplan/50-guest.yaml', 'network: {renderer: networkd, ethernets: {ens3: {dhcp4-overrides: {use-mtu: true}}}}')
        self.network('[DHCPv4]\nUseMTU=no\n')
        self.assertFalse(self.diagnose()['dhcp_use_mtu'])

    def test_networkmanager_renderer_is_identified_without_networkd_guess(self):
        self.write('etc/netplan/50-guest.yaml', 'network: {renderer: NetworkManager, ethernets: {ens3: {mtu: 1450}}}')
        self.network('[DHCPv4]\nUseMTU=yes\n')
        result = self.diagnose()
        self.assertEqual(result['network_backend'], 'NetworkManager')
        self.assertEqual(result['configured_static_mtu'], 1450)

    def test_malformed_netplan_does_not_hide_generated_networkd(self):
        self.write('etc/netplan/50-guest.yaml', 'network: [not: valid')
        self.network('[DHCPv4]\nUseMTU=yes\n')
        result = self.diagnose()
        self.assertTrue(result['dhcp_use_mtu'])
        self.assertEqual(result['network_config_note'], 'netplan_parse_unknown')

    def test_anonymize_disables_use_mtu_despite_yes(self):
        self.network('[DHCPv4]\nAnonymize=yes\nUseMTU=yes\n')
        self.assertEqual(self.diagnose()['mtu_configuration'], 'dhcp_mtu_disabled')

    def test_read_failure_returns_unknown_without_file_contents(self):
        with patch.object(guest, 'netplan_settings', side_effect=PermissionError('secret file contents')):
            result = self.diagnose()
        self.assertEqual(result['mtu_configuration'], 'unknown')
        self.assertNotIn('secret', json.dumps(result))

    def test_health_emits_diagnostics_without_changing_dhcp_or_mtu(self):
        lease = self.write('lease', 'ADDRESS=10.0.0.2')
        addresses = [dict(self.interface, flags=['UP'], mtu=1450, addr_info=[{'local':'10.0.0.2'}])]
        opener = Mock(); opener.open.side_effect = OSError('metadata unavailable')
        diagnostics = dict(network_backend='networkd', configured_static_mtu=1450,
                           dhcp_use_mtu=True, mtu_configuration='static_mtu')
        with patch.object(guest, 'CONFIG', {'ip':'10.0.0.2','server_id':'uuid'}), \
             patch.object(guest.pathlib.Path, 'glob', return_value=[lease]), \
             patch.object(guest.subprocess, 'check_output', side_effect=[json.dumps(addresses),
                         '[{"dst":"default","gateway":"10.0.0.1"}]', '[]']) as command, \
             patch.object(guest.urllib.request, 'build_opener', return_value=opener), \
             patch.object(guest, 'network_diagnostics', return_value=diagnostics) as parse, \
             patch.object(guest, 'emit') as emit:
            guest.health(10)
        record = emit.call_args.args[0]
        self.assertTrue(record['dhcp'])
        self.assertEqual(record['mtu'], 1450)
        self.assertEqual(record['mtu_configuration'], 'static_mtu')
        parse.assert_called_once_with(addresses[0])
        self.assertTrue(all(call.args[0][:2] == ['ip','-j'] for call in command.call_args_list))


if __name__ == '__main__':
    unittest.main()
