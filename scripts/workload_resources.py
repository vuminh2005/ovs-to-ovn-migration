"""Shared image sizing and protected, non-owned East-West resource evidence."""
import argparse
import ipaddress
import json
import math
import pathlib

from dataplane_capture import save


def image_flavor_compatibility(image, flavor, virtual_size=None):
    def number(value, name):
        if value is None: return None
        if isinstance(value, bool): raise RuntimeError(f'Invalid image/flavor {name}')
        try: value = float(value)
        except (TypeError, ValueError): raise RuntimeError(f'Invalid image/flavor {name}') from None
        if not math.isfinite(value) or value < 0: raise RuntimeError(f'Invalid image/flavor {name}')
        return value
    min_disk = number(getattr(image, 'min_disk', None), 'min_disk')
    min_ram = number(getattr(image, 'min_ram', None), 'min_ram')
    available_virtual = getattr(image,'virtual_size',None)
    properties = getattr(image,'properties',None)
    if available_virtual is None and isinstance(properties,dict):
        available_virtual = properties.get('virtual_size',properties.get('virtual-size'))
    virtual = number(virtual_size if virtual_size is not None else available_virtual, 'virtual_size')
    disk = number(getattr(flavor, 'disk', None), 'disk')
    ram = number(getattr(flavor, 'ram', None), 'ram')
    if disk is None or disk <= 0 or ram is None or ram <= 0:
        raise RuntimeError('Validation flavor must expose positive root disk and RAM for image sizing')
    if (min_disk is not None and min_disk > disk) or (virtual is not None and virtual > disk*1024**3) or (min_ram is not None and min_ram > ram):
        raise RuntimeError('Image does not fit validation flavor: check min_disk, virtual_size and min_ram; use a compatible existing flavor')
    return dict(status='PASS', min_disk_gb=min_disk, min_ram_mb=min_ram, virtual_size_bytes=virtual,
                virtual_size_availability='AVAILABLE' if virtual is not None else 'UNAVAILABLE',
                flavor_disk_gb=disk, flavor_ram_mb=ram)


def exact(rows, name, kind):
    matches = [r for r in rows if r.name == name]
    if len(matches) != 1: raise RuntimeError(f'Expected exactly one {kind} named {name!r}, found {len(matches)}')
    return matches[0]


def resolve_ew(cloud, cfg, root):
    definitions=cfg['servers']
    if (len(definitions)!=6 or len({row['name'] for row in definitions})!=6 or
        len({row['ip'] for row in definitions})!=6):
        raise RuntimeError('EW topology must define six distinct server names and fixed IPs')
    path = root/'ew-resources.json'
    prior = json.loads(path.read_text()) if path.exists() else None
    result = dict(schema_version=1, ownership='external-existing-never-validation-owned',
                  configuration=cfg, servers={}, networks={})
    image = exact(cloud.image.images(name=cfg['image']), cfg['image'], 'EW image')
    flavor = exact(cloud.compute.flavors(details=True), cfg['flavor'], 'EW flavor')
    if (int(flavor.vcpus), int(flavor.ram), int(flavor.disk)) != (2,2048,10):
        raise RuntimeError('EW flavor must match the configured 2 vCPU/2048 MB/10 GB lab baseline')
    result['image_flavor'] = dict(image=image.id, flavor=flavor.id, sizing=image_flavor_compatibility(image, flavor))
    router = exact(cloud.network.routers(name=cfg['router']), cfg['router'], 'EW router')
    if router.external_gateway_info: raise RuntimeError('EW router external gateway is outside supported scope')
    result['router'] = router.id
    for name in sorted({v['network'] for v in cfg['servers']}):
        net = exact(cloud.network.networks(name=name), name, 'EW network')
        if net.is_router_external or net.provider_network_type not in ('vxlan','geneve'):
            raise RuntimeError('EW network outside tenant-overlay scope')
        interfaces = list(cloud.network.ports(device_id=router.id, network_id=net.id))
        subnets = sorted({f['subnet_id'] for p in interfaces for f in p.fixed_ips})
        if not subnets: raise RuntimeError(f'EW network {name} is not attached to the exact EW router')
        result['networks'][name] = dict(network=net.id, subnets=subnets)
    for row in cfg['servers']:
        server = exact(cloud.compute.servers(name=row['name'], all_projects=True), row['name'], 'EW server')
        server = cloud.compute.get_server(server.id)
        net = result['networks'][row['network']]
        ports = [p for p in cloud.network.ports(device_id=server.id, network_id=net['network'])
                 if any(f['ip_address']==row['ip'] and f['subnet_id'] in net['subnets'] for f in p.fixed_ips)]
        if len(ports)!=1: raise RuntimeError(f'EW server {row["name"]} must have one exact expected network/IP port')
        actual = getattr(server,'compute_host',None)
        if actual != row['compute_host'] or server.status!='ACTIVE' or server.image['id']!=image.id or server.flavor.get('id',server.flavor.get('original_name')) not in (flavor.id,flavor.name):
            raise RuntimeError(f'EW server {row["name"]} placement/status/image/flavor contradicts configured baseline')
        result['servers'][row['name']] = dict(server=server.id, port=ports[0].id,
            network=net['network'], fixed_ips=ports[0].fixed_ips, expected_host=row['compute_host'], actual_host=actual,
            ip=str(ipaddress.IPv4Address(row['ip'])), security_groups=list(getattr(ports[0],'security_group_ids',[])), owned=False)
    if prior and prior != result:
        raise RuntimeError('Existing EW checkpoint identity/configuration changed; refusing replacement or rebase')
    save(path, result)
    return result


def assert_not_ew(root, cfg, kind, resource_id, name=None):
    path = root/'ew-resources.json'
    if cfg.get('ew_workloads_enabled') and not path.exists():
        raise RuntimeError('Missing protected EW resource checkpoint; destructive validation action prohibited')
    state = json.loads(path.read_text()) if path.exists() else {}
    protected = set()
    for vm in state.get('servers',{}).values():
        protected.update([vm['server'],vm['port'],vm['network']])
        protected.update(f['subnet_id'] for f in vm['fixed_ips'])
        protected.update(vm.get('security_groups',[]))
    for net in state.get('networks',{}).values():
        protected.add(net['network']); protected.update(net['subnets'])
    if state.get('router'): protected.add(state['router'])
    config = cfg.get('ew_workload_config',state.get('configuration',{}))
    names = {v['name'] for v in config.get('servers',[])} | {v['network'] for v in config.get('servers',[])}
    if config.get('router'): names.add(config['router'])
    if resource_id in protected or (isinstance(name,str) and name in names):
        raise RuntimeError(f'Existing EW {kind} is protected from validation reboot/rebuild/cleanup: {resource_id}')


def main():
    import openstack
    p = argparse.ArgumentParser(); p.add_argument('root',type=pathlib.Path)
    args = p.parse_args()
    cfg = json.loads((args.root/'ew-config.json').read_text())
    resolve_ew(openstack.connect(),cfg,args.root)


if __name__=='__main__': main()
