"""Opt-in, single-external-network centralized N-S scope. API reads only."""
import ipaddress
import math
import uuid


def require(ok, message):
    if not ok: raise RuntimeError('N-S: '+message)


def one(rows, message):
    rows=list(rows); require(len(rows)==1, message); return rows[0]


def identifier(value):
    require(isinstance(value,str) and str(uuid.UUID(value))==value, 'canonical exact UUID required')
    return value


def validate_config(cfg, network_hosts, compute_hosts):
    require(cfg.get('schema_version')==1, 'input schema_version=1 required')
    for key in ('network','subnet','router','gateway_port'): identifier(cfg[key])
    require(cfg['type'] in ('flat','vlan'), 'only flat/VLAN external networks supported')
    require(bool(cfg['physnet']) and isinstance(cfg['mtu'],int) and cfg['mtu']>=1280, 'explicit physnet/usable external MTU required')
    require((cfg['type']=='flat' and cfg['segmentation_id'] is None) or
            (cfg['type']=='vlan' and type(cfg['segmentation_id']) is int and 1<=cfg['segmentation_id']<=4094), 'invalid external segment')
    subnet=ipaddress.IPv4Network(cfg['cidr']); gateway=ipaddress.IPv4Address(cfg['gateway'])
    require(gateway in subnet, 'upstream gateway outside explicit external CIDR')
    require(cfg['allocation_pools'] and all(ipaddress.IPv4Address(p['start']) in subnet and
            ipaddress.IPv4Address(p['end']) in subnet and ipaddress.IPv4Address(p['start'])<=ipaddress.IPv4Address(p['end'])
            for p in cfg['allocation_pools']), 'explicit valid external allocation pools required')
    require(set(cfg['gateway_hosts'])==set(network_hosts) and not set(cfg['gateway_hosts'])&set(compute_hosts),
            'initial scope requires exactly the inventory network hosts as eligible gateways, no provider computes')
    require(set(cfg['gateways'])==set(cfg['gateway_hosts']), 'bridge/uplink declaration missing or ambiguous')
    require(len({g['chassis_hostname'] for g in cfg['gateways'].values()})==len(cfg['gateway_hosts']), 'gateway chassis hostname ambiguity')
    for host,g in cfg['gateways'].items():
        require(bool(g['chassis_hostname']) and bool(g['uplink_mac']), 'exact observed chassis hostname required')
        require(g['bridge']!='br-int' and g['uplink'] not in ('br-int',g['bridge']), 'unsafe external bridge/uplink')
        require(g['uplink_mtu']>=cfg['mtu'], 'external MTU exceeds declared uplink limit')
        require(g['upstream_routes_verified'] is True and g['nested_forwarding_verified'] is True,
                'operator verification of upstream routing and nested MAC/IP forwarding required')
    require(cfg['snat'] is True, 'outbound centralized SNAT must be enabled')
    for vm in [cfg['egress']['guest']] + ([cfg['ingress']['guest']] if cfg.get('ingress') else []):
        for key in ('server','port','network','subnet'): identifier(vm[key])
        ipaddress.IPv4Address(vm['ip'])
        require(vm['compute'] in compute_hosts and vm['mac'], 'exact guest compute/MAC required')
    if cfg.get('ingress'):
        require(cfg['egress']['guest']['port']!=cfg['ingress']['guest']['port'], 'SNAT guest must not be the FIP guest; keep outbound SNAT and inbound FIP paths distinct')
        identifier(cfg['ingress']['fip']); ipaddress.IPv4Address(cfg['ingress']['floating_ip'])
        observer=cfg['ingress']['observer']
        identifier(observer['product_uuid']); identifier(observer['boot'])
        ipaddress.IPv4Address(observer['ip'])
        require(all(observer.get(k) for k in ('address','user','key','known_hosts')), 'strict external observer SSH trust required')
    require(all(type(cfg[k]) in (int,float) and math.isfinite(cfg[k]) for k in ('interval','timeout','lifetime','readiness_timeout')) and cfg['lifetime']<=86400, 'finite bounded observer settings required')
    require(type(cfg['stable_samples']) is int and 3<=cfg['stable_samples']<=10, 'stable_samples must be an integer from 3 to 10')
    require(.1<=cfg['interval']<=10 and 0<cfg['timeout']<=10 and cfg['stable_samples']>=3 and
            cfg['lifetime']>cfg['readiness_timeout']>0, 'bounded probe cadence/lifetime/readiness required')
    for name in ('egress','ingress'):
        if not cfg.get(name): continue
        probe=cfg[name]['probe']; ipaddress.IPv4Address(probe['address'])
        require(1<=probe['port']<=65535 and probe['path'].startswith('/') and probe['endpoint_id'], 'controlled HTTP endpoint required')
        require(not any(c in probe['path'] for c in ('\r','\n')), 'invalid HTTP path')
        if probe.get('session_port'): require(1<=probe['session_port']<=65535, 'invalid optional echo port')
    if cfg.get('ingress'):
        require(cfg['ingress']['probe']['address']==cfg['ingress']['floating_ip'], 'ingress must address the exact configured FIP')
    return cfg


def snapshot(cloud, cfg, target=False):
    networks=list(cloud.network.networks()); routers=list(cloud.network.routers()); ports=list(cloud.network.ports())
    fips=list(cloud.network.ips())
    for n in networks:
        expected=cfg['type'] if n.id==cfg['network'] else ('geneve' if target else 'vxlan')
        require(n.provider_network_type==expected and bool(n.is_router_external)==(n.id==cfg['network']),
                'unreviewed provider/direct attachment or network type: '+n.id)
    net=one([n for n in networks if n.id==cfg['network']], 'exact external network missing')
    require(net.provider_physical_network==cfg['physnet'] and net.provider_segmentation_id==cfg['segmentation_id'] and net.mtu==cfg['mtu'], 'external segment/MTU mismatch')
    sub=cloud.network.get_subnet(cfg['subnet']); require(sub and sub.network_id==net.id, 'external subnet missing/network mismatch')
    require(sub.cidr==cfg['cidr'] and sub.gateway_ip==cfg['gateway'] and sorted(sub.allocation_pools,key=str)==sorted(cfg['allocation_pools'],key=str) and sub.ip_version==4,
            'external CIDR/gateway/allocation pool mismatch')
    require(set(net.subnet_ids)=={sub.id}, 'initial scope supports exactly one external IPv4 subnet')
    require(all(getattr(r,'is_distributed',None) is False and getattr(r,'is_ha',None) is False for r in routers), 'DVR/L3 HA unsupported')
    require({r.id for r in routers if r.external_gateway_info}=={cfg['router']}, 'unreviewed router external gateway')
    router=one([r for r in routers if r.id==cfg['router']], 'exact router missing')
    require(isinstance(router.routes,list) and all(ipaddress.ip_network(r['destination']).version==4 and ipaddress.ip_address(r['nexthop']).version==4 for r in router.routes), 'explicit IPv4 router route evidence required')
    gw=router.external_gateway_info
    require(gw['network_id']==net.id and gw.get('enable_snat') is True, 'gateway/SNAT mismatch')
    require({p.id for p in ports if p.device_id==router.id and p.device_owner=='network:router_gateway'}=={cfg['gateway_port']}, 'multiple/unknown router gateway ports')
    gateway=one([p for p in ports if p.id==cfg['gateway_port']], 'exact gateway port missing')
    require(gateway.network_id==net.id and gateway.device_id==router.id and gateway.device_owner=='network:router_gateway', 'gateway port identity mismatch')
    require(len(gateway.fixed_ips)==1 and gateway.fixed_ips[0]['subnet_id']==sub.id and gateway.fixed_ips==gw['external_fixed_ips'], 'ambiguous gateway address')
    require(not any(p.device_owner.startswith('compute:') and p.network_id==net.id for p in ports), 'direct provider-attached instances unsupported')
    interfaces=[p for p in ports if p.device_id==router.id and p.device_owner in ('network:router_interface','network:router_interface_distributed')]
    require(interfaces and all(p.device_owner=='network:router_interface' for p in interfaces), 'centralized router interfaces required')
    guests={}
    for direction in ('egress','ingress'):
        if not cfg.get(direction): continue
        vm=cfg[direction]['guest']; port=one([p for p in ports if p.id==vm['port']], 'exact guest port missing')
        server=cloud.compute.get_server(vm['server'])
        fixed=[dict(subnet_id=vm['subnet'],ip_address=vm['ip'])]
        require(server and server.id==vm['server'] and server.status=='ACTIVE' and server.compute_host==vm['compute'], 'guest server identity/status/placement mismatch')
        require(port.device_id==vm['server'] and port.network_id==vm['network'] and port.fixed_ips==fixed and
                port.mac_address.lower()==vm['mac'].lower() and port.status=='ACTIVE' and port.binding_host_id==vm['compute'] and
                port.binding_vnic_type=='normal' and port.binding_vif_type not in (None,'unbound','binding_failed'),
                'guest port/IP/MAC/binding mismatch')
        require(any(p.network_id==vm['network'] and any(f['subnet_id']==vm['subnet'] for f in p.fixed_ips) for p in interfaces), 'probe guest is not routed through selected router')
        guests[direction]=dict(vm)
    wanted={cfg['ingress']['fip']} if cfg.get('ingress') else set()
    require({f.id for f in fips}==wanted, 'unreviewed or missing Floating IP association')
    floating=[]
    for f in fips:
        require(not list(cloud.network.floating_ip_port_forwardings(f.id)), 'FIP port-forwarding outside initial scope')
        vm=cfg['ingress']['guest']
        require(f.floating_network_id==net.id and f.router_id==router.id and f.port_id==vm['port'] and
                f.fixed_ip_address==vm['ip'] and f.floating_ip_address==cfg['ingress']['floating_ip'], 'FIP-to-fixed-port identity mismatch')
        floating.append(dict(id=f.id,network=f.floating_network_id,router=f.router_id,port=f.port_id,fixed_ip=f.fixed_ip_address,floating_ip=f.floating_ip_address))
    tenant_cidrs=sorted({cloud.network.get_subnet(f['subnet_id']).cidr for p in interfaces for f in p.fixed_ips})
    require(all(ipaddress.ip_network(c).version==4 for c in tenant_cidrs), 'initial N-S scope requires IPv4 router interfaces')
    def pvalue(p): return dict(id=p.id,network=p.network_id,device=p.device_id,owner=p.device_owner,mac=p.mac_address,fixed_ips=p.fixed_ips)
    return dict(network=dict(id=net.id,external=True,type=net.provider_network_type,physnet=net.provider_physical_network,
            segmentation_id=net.provider_segmentation_id,mtu=net.mtu),subnet=dict(id=sub.id,cidr=sub.cidr,gateway=sub.gateway_ip,allocation_pools=sub.allocation_pools),
            tenant_cidrs=tenant_cidrs,router=router.id,router_settings=dict(routes=router.routes,distributed=router.is_distributed,ha=router.is_ha,external_gateway_info=dict(network_id=gw['network_id'],enable_snat=gw['enable_snat'],external_fixed_ips=gw['external_fixed_ips'])),gateway=pvalue(gateway),interfaces=sorted([pvalue(p) for p in interfaces],key=lambda p:p['id']),
            guest_networks={d:dict(network=vm['network'],type=one([n for n in networks if n.id==vm['network']], 'guest network missing').provider_network_type,mtu=one([n for n in networks if n.id==vm['network']], 'guest network missing').mtu) for d,vm in guests.items()},
            fips=floating,guests=guests,legacy_ports=[pvalue(p) for p in ports if p.device_owner in ('network:router_gateway','network:router_interface','network:dhcp')],
            legacy_namespaces=sorted(['qrouter-'+r.id for r in routers]+['qdhcp-'+n.id for n in networks]))


def preserved(before, after):
    # OVS DHCP/router implementation ports may change; operator gateway/interfaces must not.
    keys=('network','subnet','router','router_settings','gateway','interfaces','fips','guests')
    require(all(before[k]==after[k] for k in keys), 'operator-owned N-S identity/address/association changed')
    return True
