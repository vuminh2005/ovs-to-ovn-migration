#!/usr/bin/env python3
"""API-visible Caracal overlay conversion and unchanged external segment audit."""
import json
import pathlib
import sys


def audit(row, network, names, types):
    row.update(network_type_after=network.provider_network_type,
               segmentation_id_after=network.provider_segmentation_id,
               ovn_logical_switch_exists='neutron-'+network.id in names,
               observed_chassis_tunnel_encapsulation=sorted(types))
    if row.get('external'):
        good=(network.is_router_external and row['network_type_before'] in ('flat','vlan') and
              network.provider_network_type==row['network_type_before'] and
              network.provider_physical_network==row['physical_network_before'] and network.mtu==row['mtu_before'])
        row['semantics']='external segment and MTU preserved; no overlay overhead subtraction'
    else:
        good=row['network_type_before']=='vxlan' and network.provider_network_type=='geneve'
    row['status']='PASS' if (good and str(row['segmentation_id_before'])==str(network.provider_segmentation_id) and
                            row['ovn_logical_switch_exists'] and types=={'geneve'}) else 'FAIL'
    return row


def main():
    import openstack
    root=pathlib.Path(sys.argv[2]); cloud=openstack.connect()
    if sys.argv[1]=='before':
        rows=[dict(network_uuid=n.id,network_type_before=n.provider_network_type,
                   segmentation_id_before=n.provider_segmentation_id,external=bool(n.is_router_external),
                   physical_network_before=n.provider_physical_network,mtu_before=n.mtu) for n in cloud.network.networks()]
        (root/'network-segments.before.json').write_text(json.dumps(rows,indent=2))
    else:
        rows=json.loads((root/'network-segments.before.json').read_text())
        switches=json.loads((root/'ovn-logical-switches.json').read_text()); encaps=json.loads((root/'ovn-encaps.json').read_text())
        names={r[0] for r in switches['data']}; types={r[0] for r in encaps['data']}
        for row in rows: audit(row,cloud.network.get_network(row['network_uuid']),names,types)
        (root/'existing-network-semantics.json').write_text(json.dumps(dict(
            status='PASS' if all(r['status']=='PASS' for r in rows) else 'FAIL',networks=rows,
            upstream='https://github.com/openstack/neutron/blob/24.0.0/neutron/plugins/ml2/drivers/ovn/db_migration.py'),indent=2))


if __name__=='__main__': main()
