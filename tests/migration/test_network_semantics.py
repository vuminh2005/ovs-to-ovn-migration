"""The semantics CLI keeps the original API checks behind an explicit entrypoint."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import network_semantics


class NetworkSemanticsTests(unittest.TestCase):
    def test_before_after_preserves_segment_checks_without_import_time_cloud_calls(self):
        original = SimpleNamespace(id='net-one', provider_network_type='vxlan',
            provider_segmentation_id=7, provider_physical_network=None, mtu=1400)
        migrated = SimpleNamespace(id='net-one', provider_network_type='geneve',
            provider_segmentation_id=7, provider_physical_network=None, mtu=1392)
        network = Mock()
        network.networks.return_value = [original]
        network.get_network.return_value = migrated
        cloud = SimpleNamespace(network=network)
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(network_semantics.openstack, 'connect', return_value=cloud) as connect:
            root = Path(directory)
            self.assertEqual(network_semantics.main(['before', directory]), 0)
            before = (root/'network-segments.before.json').read_bytes()
            (root/'ovn-logical-switches.json').write_text(json.dumps({'data': [['neutron-net-one']]}))
            (root/'ovn-encaps.json').write_text(json.dumps({'data': [['geneve']]}))
            self.assertEqual(network_semantics.main(['after', directory]), 0)
            self.assertEqual((root/'network-segments.before.json').read_bytes(), before)
            self.assertEqual(json.loads((root/'existing-network-semantics.json').read_text())['status'], 'PASS')
            migrated.provider_segmentation_id = 8
            network_semantics.main(['after', directory])
            self.assertEqual(json.loads((root/'existing-network-semantics.json').read_text())['status'], 'FAIL')
            self.assertEqual(connect.call_count, 3)


if __name__ == '__main__':
    unittest.main()
