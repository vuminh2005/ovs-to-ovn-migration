"""Read-only verified guest selection for Work Item 1 recovery/check tools."""
import json
import pathlib

from ew_transport import Transport, verify_profile
from workload_resources import resolve_ew


def sources(root, reference_file, cloud, transport=None):
    root=pathlib.Path(root)
    reference=json.loads(pathlib.Path(reference_file).read_text())
    cfg=json.loads((root/'ew-measurement-config.json').read_text())
    catalog=resolve_ew(cloud,json.loads((root/'ew-config.json').read_text()),root)
    transport=transport or Transport(cfg,catalog,cloud=cloud)
    observed={}
    for name in ('ew-app','ew-db','ew-queue'):
        vm=catalog['servers'][name]; expected=reference.get('guests',{}).get(name)
        if not expected or any(expected.get(k)!=vm[k] for k in ('server','port','ip')) or not expected.get('boot'):
            raise RuntimeError(name+': current source identity differs from reviewed reference')
        network=cloud.network.get_network(vm['network'])
        if network.provider_network_type!=expected.get('network_type') or network.provider_network_type not in ('vxlan','geneve'):
            raise RuntimeError(name+': current network differs from reviewed reference')
        access=transport.access(vm,'source' if network.provider_network_type=='vxlan' else 'ovn')
        mac=cloud.network.get_port(vm['port']).mac_address
        profile=transport.profile(vm,access)
        verify_profile(profile,vm,expected['boot'],mac=mac)
        observed[name]=dict(vm=vm,access=access,mac=mac,boot=profile['boot'])
    return transport,observed


def recheck(transport, source):
    verify_profile(transport.profile(source['vm'],source['access']),source['vm'],
                   source['boot'],mac=source['mac'])
