#!/usr/bin/env python3
"""Caracal centralized router/NAT/localnet/chassis evidence; no uniform up assumption."""
import ipaddress
import json
import sys
from ovn_workload_evidence import query
from ns_scope import require, one


def refs(v): return v if isinstance(v,list) else [v]


class ConvergencePending(RuntimeError):
    """Required target state is absent, rather than conflicting or ambiguous."""


def present(rows, message):
    rows=list(rows)
    if not rows: raise ConvergencePending('N-S: '+message)
    return one(rows,message)  # duplicates remain a hard refusal


def collect(nb,sb):
    result={}
    for table,columns in {
        'Logical_Router':'_uuid,name,ports,nat,static_routes',
        'Logical_Router_Port':'_uuid,name,networks,mac,gateway_chassis',
        'NAT':'_uuid,type,logical_ip,external_ip,external_ids',
        'Logical_Router_Static_Route':'_uuid,ip_prefix,nexthop,output_port,external_ids',
        'Logical_Switch':'_uuid,name,ports',
        'Logical_Switch_Port':'_uuid,name,type,options,tag',
        'Gateway_Chassis':'_uuid,chassis_name,priority'}.items():
        result[table]=query('ovn-nbctl',nb,table,columns,'',operation='list')
    for table,columns in {'Chassis':'_uuid,name,hostname,other_config',
                          'Port_Binding':'logical_port,type,chassis,options'}.items():
        result[table]=query('ovn-sbctl',sb,table,columns,'',operation='list')
    return result


def verify(cfg, before, db, hosts):
    router=present([r for r in db['Logical_Router'] if r['name']=='neutron-'+cfg['router']], 'exact OVN logical router missing')
    ext=present([p for p in db['Logical_Router_Port'] if p['name']=='lrp-'+cfg['gateway_port']], 'exact gateway LRP missing')
    require(ext['_uuid'] in refs(router['ports']), 'gateway LRP belongs to another router')
    gateway=before['gateway']; address=gateway['fixed_ips'][0]['ip_address']; prefix=ipaddress.ip_network(cfg['cidr']).prefixlen
    require(refs(ext['networks'])==[address+'/'+str(prefix)] and ext['mac'].lower()==gateway['mac'].lower(), 'gateway LRP address/MAC mismatch')
    for interface in before['interfaces']:
        lrp=present([p for p in db['Logical_Router_Port'] if p['name']=='lrp-'+interface['id']], 'tenant router LRP missing')
        require(lrp['_uuid'] in refs(router['ports']) and lrp['mac'].lower()==interface['mac'].lower(), 'router interface identity mismatch')
        require({str(ipaddress.ip_interface(n).ip) for n in refs(lrp['networks'])}=={f['ip_address'] for f in interface['fixed_ips']}, 'tenant router interface IP changed')
    host_names={g['chassis_hostname']:host for host,g in cfg['gateways'].items()}
    chassis=[c for c in db['Chassis'] if c['hostname'] in host_names]
    require(len({c['hostname'] for c in chassis})==len(chassis), 'ambiguous eligible gateway chassis')
    for c in chassis:
        require('enable-chassis-as-gw' in c['other_config'].get('ovn-cms-options','').split(','), 'SB chassis not gateway eligible')
        require(hosts[host_names[c['hostname']]]['external_ids'].get('system-id')==c['name'], 'chassis system-id/host mismatch')
    if len(chassis)!=len(cfg['gateway_hosts']): raise ConvergencePending('N-S: missing eligible gateway chassis')
    gc=[g for g in db['Gateway_Chassis'] if g['_uuid'] in refs(ext['gateway_chassis'])]
    names=[g['chassis_name'] for g in gc]
    require(len(set(names))==len(names) and set(names)<={c['name'] for c in chassis}, 'gateway chassis assignment identity mismatch')
    if len(gc)!=len(chassis): raise ConvergencePending('N-S: gateway chassis assignment incomplete')
    redirect=present([p for p in db['Port_Binding'] if p['logical_port']=='cr-lrp-'+cfg['gateway_port']], 'gateway chassisredirect binding missing')
    require(redirect['type']=='chassisredirect' and redirect['options'].get('distributed-port')==ext['name'], 'gateway chassisredirect identity mismatch')
    if redirect['chassis']==[]: raise ConvergencePending('N-S: gateway chassisredirect is not yet bound')
    require(isinstance(redirect['chassis'],str) and redirect['chassis'] in {c['_uuid'] for c in chassis}, 'gateway chassisredirect not bound to intended eligible chassis')
    # localnet has no VM-style up/chassis contract. Verify LS membership, physnet/tag and SB type.
    switch=present([s for s in db['Logical_Switch'] if s['name']=='neutron-'+cfg['network']], 'external logical switch missing')
    local=present([p for p in db['Logical_Switch_Port'] if p['_uuid'] in refs(switch['ports']) and p['type']=='localnet'], 'external localnet missing/ambiguous')
    tag=refs(local['tag'])
    require(local['options'].get('network_name')==cfg['physnet'] and tag==([] if cfg['type']=='flat' else [cfg['segmentation_id']]), 'localnet physnet/VLAN mismatch')
    gateway_lsp=present([p for p in db['Logical_Switch_Port'] if p['name']==cfg['gateway_port']], 'external gateway router LSP missing')
    require(gateway_lsp['_uuid'] in refs(switch['ports']) and gateway_lsp['type']=='router' and gateway_lsp['options'].get('router-port')==ext['name'], 'external switch/router gateway attachment mismatch')
    gateway_sb=present([p for p in db['Port_Binding'] if p['logical_port']==cfg['gateway_port']], 'gateway router SB patch missing')
    require(gateway_sb['type']=='patch' and gateway_sb['options'].get('peer')==ext['name'], 'gateway router SB patch peer mismatch')
    sb_local=present([p for p in db['Port_Binding'] if p['logical_port']==local['name']], 'SB localnet missing')
    require(sb_local['type']=='localnet', 'SB localnet type mismatch')
    active=one([c for c in chassis if c['_uuid']==redirect['chassis']], 'active gateway chassis identity missing')
    active_host=host_names[active['hostname']]; paths=hosts[active_host]['ports']
    tagged=[p for p in paths if p['external_ids'].get('ovn-localnet-port')==local['name'] or
            any(i['external_ids'].get('ovn-localnet-port')==local['name'] for i in p['interfaces'])]
    if not tagged: raise ConvergencePending('N-S: active gateway has no observed OVN localnet patch path')
    pairs=set()
    for p in tagged:
        i=one(p['interfaces'], 'ambiguous OVN localnet interface')
        require(i['type']=='patch' and i['options'].get('peer'), 'OVN localnet patch identity mismatch')
        peer=present([r for r in paths if r['name']==i['options'].get('peer')], 'OVN localnet patch peer missing')
        j=one(peer['interfaces'], 'ambiguous OVN patch peer')
        require(i['type']==j['type']=='patch' and j['options'].get('peer')==p['name'] and
                {p['bridge'],peer['bridge']}=={'br-int',cfg['gateways'][active_host]['bridge']}, 'OVN localnet physical patch path mismatch')
        pairs.add(tuple(sorted([p['name'],peer['name']])))
    require(len(pairs)==1, 'ambiguous OVN localnet physical patch paths')
    route=present([r for r in db['Logical_Router_Static_Route'] if r['_uuid'] in refs(router['static_routes']) and r['ip_prefix']=='0.0.0.0/0'], 'default external route missing/ambiguous')
    require(route['nexthop']==cfg['gateway'] and refs(route['output_port']) in ([],[ext['name']]) and
            route['external_ids'].get('neutron:is_ext_gw')=='true' and route['external_ids'].get('neutron:subnet_id')==cfg['subnet'], 'default route next-hop/output mismatch')
    for expected in before['router_settings']['routes']:
        require(len([r for r in db['Logical_Router_Static_Route'] if r['_uuid'] in refs(router['static_routes']) and r['ip_prefix']==expected['destination'] and r['nexthop']==expected['nexthop']])==1, 'preserved tenant static route missing/ambiguous')
    nat=[n for n in db['NAT'] if n['_uuid'] in refs(router['nat'])]
    require(len(nat)==len(before['tenant_cidrs'])+len(before['fips']), 'unreviewed or duplicate router NAT entries')
    for cidr in before['tenant_cidrs']:
        require(len([n for n in nat if n['type']=='snat' and n['logical_ip']==cidr and n['external_ip']==address])==1, 'exact tenant SNAT missing/ambiguous')
    for f in before['fips']:
        require(len([n for n in nat if n['type']=='dnat_and_snat' and n['logical_ip']==f['fixed_ip'] and n['external_ip']==f['floating_ip'] and
                    n['external_ids'].get('neutron:fip_id')==f['id'] and n['external_ids'].get('neutron:fip_port_id')==f['port']])==1, 'exact FIP DNAT/SNAT mapping missing')
    return dict(status='PASS',gateway_redirect=redirect,localnet=local,eligible_gateway_chassis=gc,nat=nat,
                claim='centralized gateway binding only; no HA/failover or conntrack preservation claim')


if __name__=='__main__': print(json.dumps(collect(sys.argv[1],sys.argv[2])))
