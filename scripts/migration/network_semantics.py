#!/usr/bin/env python3
"""API-visible Caracal segment conversion audit, no database writes."""

# Source helpers are grouped; remote/guest copies keep flat sibling imports.
import sys as _sys
from pathlib import Path as _Path
_source = _Path(__file__).resolve().parents[1]
if _source.name == 'scripts' and (_source / '_paths.py').is_file():
    _sys.path.insert(0, str(_source))
    from _paths import configure as _configure
    _configure()
from run_layout import run_path
import argparse
import json
import pathlib
import sys
import openstack


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('before', 'after'))
    parser.add_argument('root', type=pathlib.Path)
    args = parser.parse_args(argv)
    root = args.root
    c = openstack.connect()
    if args.action == 'before':
        rows = [dict(network_uuid=n.id, network_type_before=n.provider_network_type,
                     segmentation_id_before=n.provider_segmentation_id, physical_network_before=n.provider_physical_network, mtu_before=n.mtu) for n in c.network.networks()]
        (run_path(root, 'network-segments.before.json')).write_text(json.dumps(rows, indent=2))
    else:
        rows = json.loads((run_path(root, 'network-segments.before.json')).read_text())
        switches = json.loads((run_path(root, 'ovn-logical-switches.json')).read_text())
        encaps = json.loads((run_path(root, 'ovn-encaps.json')).read_text())
        names = {r[0] for r in switches['data']}
        types = {r[0] for r in encaps['data']}
        for row in rows:
            n = c.network.get_network(row['network_uuid'])
            row.update(network_type_after=n.provider_network_type,
                       segmentation_id_after=n.provider_segmentation_id,
                       ovn_logical_switch_exists='neutron-'+n.id in names,
                       observed_chassis_tunnel_encapsulation=sorted(types))
            converted = (row['network_type_before']=='vxlan' and
                row['network_type_after']=='geneve' and
                str(row['segmentation_id_before'])==str(row['segmentation_id_after']) and
                row['ovn_logical_switch_exists'] and types == {'geneve'})
            flat_preserved = (row['network_type_before']=='flat' and n.provider_network_type=='flat' and
                n.provider_physical_network==row.get('physical_network_before') and n.mtu==row.get('mtu_before') and
                str(n.provider_segmentation_id)==str(row['segmentation_id_before']) and row['ovn_logical_switch_exists'])
            row['status'] = 'PASS' if converted or flat_preserved else 'FAIL'
            row['scope'] = 'flat-preservation' if row['network_type_before']=='flat' else 'vxlan-to-geneve'
        (run_path(root, 'existing-network-semantics.json')).write_text(json.dumps(dict(
            status='PASS' if all(r['status']=='PASS' for r in rows) else 'FAIL', networks=rows,
            upstream='https://github.com/openstack/neutron/blob/24.0.0/neutron/plugins/ml2/drivers/ovn/db_migration.py'), indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
