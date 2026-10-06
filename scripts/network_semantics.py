#!/usr/bin/env python3
"""API-visible Caracal segment conversion audit, no database writes."""
import json
import pathlib
import sys
import openstack
root = pathlib.Path(sys.argv[2])
c = openstack.connect()
if sys.argv[1] == 'before':
    rows = [dict(network_uuid=n.id, network_type_before=n.provider_network_type,
                 segmentation_id_before=n.provider_segmentation_id) for n in c.network.networks()]
    (root/'network-segments.before.json').write_text(json.dumps(rows, indent=2))
else:
    rows = json.loads((root/'network-segments.before.json').read_text())
    switches = json.loads((root/'ovn-logical-switches.json').read_text())
    encaps = json.loads((root/'ovn-encaps.json').read_text())
    names = {r[0] for r in switches['data']}
    types = {r[0] for r in encaps['data']}
    for row in rows:
        n = c.network.get_network(row['network_uuid'])
        row.update(network_type_after=n.provider_network_type,
                   segmentation_id_after=n.provider_segmentation_id,
                   ovn_logical_switch_exists='neutron-'+n.id in names,
                   observed_chassis_tunnel_encapsulation=sorted(types))
        row['status'] = 'PASS' if (row['network_type_before']=='vxlan' and
            row['network_type_after']=='geneve' and
            str(row['segmentation_id_before'])==str(row['segmentation_id_after']) and
            row['ovn_logical_switch_exists'] and types == {'geneve'}) else 'FAIL'
    (root/'existing-network-semantics.json').write_text(json.dumps(dict(
        status='PASS' if all(r['status']=='PASS' for r in rows) else 'FAIL', networks=rows,
        upstream='https://github.com/openstack/neutron/blob/24.0.0/neutron/plugins/ml2/drivers/ovn/db_migration.py'), indent=2))
