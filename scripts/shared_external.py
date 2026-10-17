"""Read-only validation of a shared external network and fixed-IP reservations."""
import ipaddress


def network_shape(net):
    return dict(id=net.id, external=bool(net.is_router_external),
                network_type=net.provider_network_type, physnet=net.provider_physical_network,
                mtu=net.mtu)


def subnet_shape(sub):
    return dict(id=sub.id, network_id=sub.network_id, cidr=sub.cidr,
                gateway_ip=sub.gateway_ip, dhcp=bool(sub.is_dhcp_enabled),
                allocation_pools=sorted([dict(start=p['start'], end=p['end'])
                    for p in sub.allocation_pools], key=lambda p: p['start']))


def validate(net, sub, cfg):
    if (not net.is_router_external or net.provider_network_type != 'flat'
            or net.provider_physical_network != cfg['physnet'] or net.mtu != cfg['mtu']):
        raise RuntimeError('Shared external must be flat on the configured physnet/MTU')
    if (sub.network_id != net.id or sub.cidr != cfg['cidr']
            or sub.gateway_ip != cfg['gateway_ip'] or sub.is_dhcp_enabled):
        raise RuntimeError('Shared external subnet CIDR/gateway/DHCP differs')
    cidr = ipaddress.ip_network(sub.cidr)
    for key in ('snat_ip', 'fip_ip'):
        address = ipaddress.ip_address(cfg[key])
        if (address not in cidr or address in (cidr.network_address, cidr.broadcast_address,
                ipaddress.ip_address(sub.gateway_ip)) or not any(
                ipaddress.ip_address(p['start']) <= address <= ipaddress.ip_address(p['end'])
                for p in sub.allocation_pools)):
            raise RuntimeError('Reserved N-S '+key+' is outside the shared allocation pool')
    if cfg['snat_ip'] == cfg['fip_ip']:
        raise RuntimeError('N-S SNAT and FIP reservations must differ')


def select(proxy, cfg, networks=None):
    networks = list(proxy.networks()) if networks is None else networks
    external = [n for n in networks if n.is_router_external]
    if not external:
        return None  # Backward-compatible fresh lab: the run creates its own external.
    matches = [n for n in external if cfg['network'] in (n.id, n.name)]
    if len(matches) != 1 or len(external) != 1:
        raise RuntimeError('Use exactly one configured shared external network; selection is missing/ambiguous')
    net = matches[0]
    subs = list(proxy.subnets(network_id=net.id))
    if len(subs) != 1:
        raise RuntimeError('Shared external requires exactly one configured IPv4 subnet')
    sub = subs[0]
    validate(net, sub, cfg)
    return dict(network=network_shape(net), subnet=subnet_shape(sub), config=dict(cfg), owned=False)


def resolve(proxy, checkpoint):
    if checkpoint.get('owned') is not False:
        raise RuntimeError('Shared external checkpoint must explicitly have owned=false')
    net = proxy.get_network(checkpoint['network']['id'])
    sub = proxy.get_subnet(checkpoint['subnet']['id'])
    validate(net, sub, checkpoint['config'])
    if network_shape(net) != checkpoint['network'] or subnet_shape(sub) != checkpoint['subnet']:
        raise RuntimeError('Shared external identity/configuration changed since checkpoint')
    return net, sub


def require_free_ips(proxy, checkpoint, *, router=None, floating_ip=None, port=None, marker=None):
    """A retry may keep its own exact gateway/FIP; never adopt another allocation."""
    network = checkpoint['network']['id']
    cfg = checkpoint['config']
    ips = list(proxy.ips(floating_network_id=network))
    recovered = [f for f in ips if f.floating_ip_address == cfg['fip_ip']
                 and marker and f.description == marker and port and f.port_id == port]
    if not floating_ip and len(recovered) == 1:
        floating_ip = recovered[0].id
    for p in proxy.ports(network_id=network):
        for fixed in p.fixed_ips:
            if fixed['ip_address'] == cfg['snat_ip'] and p.device_id != router:
                raise RuntimeError('N-S SNAT IP already allocated to another owner: '+cfg['snat_ip'])
            if fixed['ip_address'] == cfg['fip_ip']:
                # Neutron represents allocated floating IPs as network:floatingip ports.
                if not floating_ip or p.device_id != floating_ip:
                    raise RuntimeError('N-S FIP IP already allocated to another owner: '+cfg['fip_ip'])
    for fip in ips:
        if fip.floating_ip_address in (cfg['snat_ip'], cfg['fip_ip']):
            if not (fip.floating_ip_address == cfg['fip_ip'] and fip.description == marker
                    and port and fip.port_id == port and (not floating_ip or fip.id == floating_ip)):
                raise RuntimeError('Reserved FIP already allocated to another owner')
