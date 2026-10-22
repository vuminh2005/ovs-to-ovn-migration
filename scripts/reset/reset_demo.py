"""Reset-owned shared external and console-only demonstration resources.

No migration workload checkpoint is created. Migration may read/migrate these
resources, but only a later full reset replaces them.
"""

# Source helpers are grouped; remote/guest copies keep flat sibling imports.
import sys as _sys
from pathlib import Path as _Path
_source = _Path(__file__).resolve().parents[1]
if _source.name == 'scripts' and (_source / '_paths.py').is_file():
    _sys.path.insert(0, str(_source))
    from _paths import configure as _configure
    _configure()
import base64
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import secrets
import subprocess
import time

from dataplane_capture import save
from shared_external import select
from validation_placement import resolve as resolve_placement, create_on_target, check


def validate_config(cfg):
    ext = cfg['external']
    cidr = ipaddress.ip_network(ext['cidr'])
    start, end = map(ipaddress.ip_address, (ext['pool_start'], ext['pool_end']))
    addresses = [ipaddress.ip_address(ext[k]) for k in ('snat_ip','fip_ip')]
    addresses += [ipaddress.ip_address(cfg[k]) for k in ('router_ip','floating_ip')]
    if cidr.version != 4 or not start <= end or start not in cidr or end not in cidr:
        raise ValueError('Invalid shared external IPv4 allocation pool')
    if len(set(addresses)) != 4 or any(not start <= a <= end for a in addresses):
        raise ValueError('Demo and automatic N-S need four distinct IPs inside the shared pool')
    gateway = ipaddress.ip_address(ext['gateway_ip'])
    if gateway not in cidr or start <= gateway <= end or any(a in (cidr.network_address, cidr.broadcast_address) for a in (start,end)):
        raise ValueError('External gateway/network/broadcast cannot be an allocation address')
    if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,30}', cfg['console_user']):
        raise ValueError('Invalid demo console username')
    tenants = [ipaddress.ip_network(c) for c in cfg['cidrs']]
    if len(tenants) != 2 or tenants[0].overlaps(tenants[1]) or any(
            n.overlaps(cidr) or n.version != 4 or n.num_addresses < 16 for n in tenants):
        raise ValueError('Demo needs two disjoint IPv4 tenant subnets')
    for n in tenants:
        if any(n.overlaps(ipaddress.ip_network(c)) for c in cfg['workload_cidrs']):
            raise ValueError('Demo private CIDR overlaps automatic workload topology')
    if len(cfg['compute_hosts']) != 2 or len(set(cfg['compute_hosts'])) != 2:
        raise ValueError('Demo needs two distinct compute targets')
    if not 576 <= cfg['source_mtu'] <= ext['mtu']-50 or cfg['timeout'] <= 0:
        raise ValueError('Invalid demo MTU or readiness timeout')
    if not isinstance(cfg.get('console_password', ''), str) or any(c in cfg.get('console_password', '') for c in ('\n','\r','\0')):
        raise ValueError('Console password must be a string or empty for generation')
    if start <= ipaddress.ip_address(cfg['endpoint_ip']) <= end:
        raise ValueError('Shared pool overlaps the controller endpoint address')
    return cfg


def marker(root):
    return 'ovn-reset-generation='+root.name


def existing_ids(cloud, root):
    """Permit a partially created same-reset journal, never unrelated remnants."""
    expected = {}
    for key, collection in (('network','networks'),('subnet','subnets'),('router','routers')):
        rows = list(getattr(cloud.network, collection)())
        if any(row.description != marker(root) for row in rows):
            raise RuntimeError('Unexpected resource during reset demo preparation: '+key)
        expected[key] = {row.id for row in rows}
    servers = list(cloud.compute.servers(all_projects=True))
    if any((s.metadata or {}).get('reset_demo_generation') != root.name for s in servers):
        raise RuntimeError('Unexpected server during reset demo preparation')
    expected['server'] = {s.id for s in servers}
    return expected


class Demo:
    def __init__(self, cloud, root, cfg, prerequisites):
        self.cloud, self.root, self.cfg = cloud, root, validate_config(cfg)
        self.prerequisites = prerequisites
        self.path = root/'reset-demo-resources.json'
        self.state = json.loads(self.path.read_text()) if self.path.exists() else dict(
            schema=1, generation=root.name, migration_owned=False)
        digest = hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()
        if self.state.get('generation') != root.name or self.state.get('config_sha256', digest) != digest:
            raise RuntimeError('Reset demo generation/configuration changed')
        self.state['config_sha256'] = digest
        identity = dict(image_id=prerequisites['image']['id'], flavor_id=prerequisites['flavor']['id'])
        if self.state.get('prerequisites', identity) != identity:
            raise RuntimeError('Reset demo image/flavor checkpoint changed')
        self.state['prerequisites'] = identity
        self.commit()

    def commit(self):
        save(self.path, self.state)

    def resource(self, key, kind, name, **attrs):
        proxy = self.cloud.network
        if self.state.get(key):
            obj = getattr(proxy, 'get_'+kind)(self.state[key])
        else:
            collection = dict(network='networks',subnet='subnets',router='routers',
                              port='ports',security_group='security_groups')[kind]
            matches = [r for r in getattr(proxy, collection)(name=name) if r.name == name]
            if len(matches) > 1 or matches and matches[0].description != marker(self.root):
                raise RuntimeError('Reset demo name belongs to another resource: '+name)
            obj = matches[0] if matches else getattr(proxy, 'create_'+kind)(
                name=name, description=marker(self.root), **attrs)
            self.state[key] = obj.id
            self.commit()
        if obj.description != marker(self.root) or any(getattr(obj,k) != v for k,v in attrs.items()):
            raise RuntimeError('Reset demo ownership/configuration changed: '+name)
        return obj

    def credentials(self):
        path = self.root/'demo-console-credentials.json'
        if path.exists():
            value = json.loads(path.read_text())
            if value.get('generation') != self.root.name or value.get('username') != self.cfg['console_user']:
                raise RuntimeError('Demo console credentials belong to another generation')
        else:
            password = self.cfg.get('console_password') or secrets.token_urlsafe(16)
            value = dict(generation=self.root.name, username=self.cfg['console_user'], password=password)
            save(path, value)
        digest = subprocess.run(['openssl','passwd','-6','-stdin'], input=value['password']+'\n',
            capture_output=True, text=True, check=True, timeout=10).stdout.strip()
        if not digest.startswith('$6$'):
            raise RuntimeError('Console password hashing failed')
        return value['username'], digest

    def wait(self, function, message):
        deadline = time.monotonic()+self.cfg['timeout']
        while time.monotonic() < deadline:
            result = function()
            if result:
                return result
            time.sleep(2)
        raise TimeoutError(message)

    def server(self, index, port, placement, user, password_hash):
        cfg = self.cfg
        role = 'vm'+str(index+1)
        name = cfg['prefix']+'-'+role
        expected_metadata = dict(reset_demo_generation=self.root.name, reset_demo_role=role)
        if self.state.get(role):
            server = self.cloud.compute.get_server(self.state[role])
        else:
            matches = [s for s in self.cloud.compute.servers(name=name) if s.name == name]
            if len(matches)>1 or matches and (matches[0].metadata != expected_metadata or port.device_id != matches[0].id):
                raise RuntimeError('Demo server name/port belongs to another resource')
            ready = 'OVN_DEMO_READY_'+self.root.name+'_'+role
            data = dict(users=['default',dict(name=user, groups=['sudo'], shell='/bin/bash',
                lock_passwd=False, hashed_passwd=password_hash, sudo='ALL=(ALL) NOPASSWD:ALL')],
                ssh_pwauth=False, chpasswd=dict(expire=False),
                runcmd=[['sh','-c', 'echo '+ready+' > /dev/ttyS0']])
            import yaml
            user_data = base64.b64encode(('#cloud-config\n'+yaml.safe_dump(data,sort_keys=False)).encode()).decode()
            server = matches[0] if matches else create_on_target(self.cloud.compute, placement,
                name=name, image_id=self.prerequisites['image']['id'], flavor_id=self.prerequisites['flavor']['id'],
                networks=[dict(port=port.id)], metadata=expected_metadata, user_data=user_data)
            self.state[role] = server.id
            self.commit()
        def active():
            server = self.cloud.compute.get_server(self.state[role])
            current = self.cloud.network.get_port(port.id)
            if server.metadata != expected_metadata or current.device_id and current.device_id != server.id:
                raise RuntimeError('Reset demo server/port ownership changed')
            if server.status == 'ERROR':
                raise RuntimeError('Reset demo VM entered ERROR: '+server.id)
            return server if (server.status == 'ACTIVE' and current.status == 'ACTIVE'
                and current.binding_vif_type not in ('unbound','binding_failed')
                and check(server,current,placement)['status'] == 'PASS') else None
        server = self.wait(active, 'Demo VM did not become ACTIVE with the requested placement')
        sentinel = 'OVN_DEMO_READY_'+self.root.name+'_'+role
        self.wait(lambda: sentinel in self.cloud.compute.get_server_console_output(server.id,length=2000)['output'],
                  'Demo cloud-init console account was not ready')
        return server

    def prepare(self):
        cfg = self.cfg
        ext_cfg = cfg['external']
        user, password_hash = self.credentials()
        # Resolve computes before creating resources; normal Nova scheduler filters remain.
        placement = resolve_placement(self.cloud, dict(pair_hosts=cfg['compute_hosts'],
            compute_hosts=cfg['compute_hosts'], identities={h:[h] for h in cfg['compute_hosts']}))
        saved = self.state.get('placement')
        if saved and saved != placement:
            raise RuntimeError('Reset demo compute placement changed')
        self.state['placement'] = placement
        self.commit()
        ext = self.resource('external_network','network',ext_cfg['network'], is_router_external=True,
            provider_network_type='flat', provider_physical_network=ext_cfg['physnet'], mtu=ext_cfg['mtu'])
        ext_sub = self.resource('external_subnet','subnet',ext_cfg['subnet'], network_id=ext.id,
            cidr=ext_cfg['cidr'], ip_version=4, gateway_ip=ext_cfg['gateway_ip'], is_dhcp_enabled=False,
            allocation_pools=[dict(start=ext_cfg['pool_start'],end=ext_cfg['pool_end'])])
        shared = select(self.cloud.network, ext_cfg)
        sg = self.resource('security_group','security_group',cfg['prefix']+'-security')
        if not any(r.direction=='ingress' and r.ether_type=='IPv4' and r.protocol=='icmp' and
                r.remote_ip_prefix=='0.0.0.0/0' for r in self.cloud.network.security_group_rules(security_group_id=sg.id)):
            self.cloud.network.create_security_group_rule(security_group_id=sg.id, direction='ingress',
                ether_type='IPv4', protocol='icmp', remote_ip_prefix='0.0.0.0/0')
        router = self.resource('router','router',cfg['prefix']+'-router')
        gateway = dict(network_id=ext.id,enable_snat=True,
            external_fixed_ips=[dict(subnet_id=ext_sub.id,ip_address=cfg['router_ip'])])
        current = self.cloud.network.get_router(router.id).external_gateway_info
        if not current:
            self.cloud.network.update_router(router.id,external_gateway_info=gateway)
        elif any(current.get(k)!=v for k,v in gateway.items()):
            raise RuntimeError('Reset demo router gateway changed')
        vms = []
        for i, cidr in enumerate(cfg['cidrs']):
            suffix = str(i+1)
            net = self.resource('network'+suffix,'network',cfg['prefix']+'-net'+suffix,
                provider_network_type='vxlan', mtu=cfg['source_mtu'])
            sub = self.resource('subnet'+suffix,'subnet',cfg['prefix']+'-subnet'+suffix,
                network_id=net.id,cidr=cidr,ip_version=4,is_dhcp_enabled=True)
            interfaces = list(self.cloud.network.ports(device_id=router.id,network_id=net.id))
            if not interfaces:
                self.cloud.network.add_interface_to_router(router.id,subnet_id=sub.id)
            elif not any(any(p['subnet_id']==sub.id for p in interface.fixed_ips) for interface in interfaces):
                raise RuntimeError('Reset demo router interface changed')
            ip = str(ipaddress.ip_network(cidr).network_address+11)
            port = self.resource('port'+suffix,'port',cfg['prefix']+'-port'+suffix,network_id=net.id,
                security_group_ids=[sg.id],fixed_ips=[dict(subnet_id=sub.id,ip_address=ip)])
            server = self.server(i,port,placement[str(i)],user,password_hash)
            vms.append(dict(name=server.name,id=server.id,ip=ip,port_id=port.id,
                network_id=net.id,subnet_id=sub.id,compute=placement[str(i)]['nova_host']))
        if self.state.get('floating_ip'):
            fip = self.cloud.network.get_ip(self.state['floating_ip'])
        else:
            matches = list(self.cloud.network.ips(floating_ip_address=cfg['floating_ip']))
            if len(matches)>1 or matches and (matches[0].description!=marker(self.root) or matches[0].port_id!=vms[0]['port_id']):
                raise RuntimeError('Demo floating IP already allocated to another owner')
            fip = matches[0] if matches else self.cloud.network.create_ip(floating_network_id=ext.id,
                floating_ip_address=cfg['floating_ip'],port_id=vms[0]['port_id'],
                fixed_ip_address=vms[0]['ip'],description=marker(self.root))
            self.state['floating_ip'] = fip.id
            self.commit()
        def active_fip():
            ip = self.cloud.network.get_ip(fip.id)
            if (ip.floating_network_id!=ext.id or ip.floating_ip_address!=cfg['floating_ip'] or
                ip.port_id!=vms[0]['port_id'] or ip.fixed_ip_address!=vms[0]['ip'] or ip.description!=marker(self.root)):
                raise RuntimeError('Reset demo FIP identity/association changed')
            return ip.status == 'ACTIVE'
        self.wait(active_fip,'Demo FIP did not become ACTIVE')
        def gateway_ready():
            ports = list(self.cloud.network.ports(device_id=router.id,network_id=ext.id))
            if len(ports)>1:
                raise RuntimeError('Demo router has ambiguous external gateway ports')
            if not ports:
                return None
            port = ports[0]
            if port.fixed_ips != gateway['external_fixed_ips']:
                raise RuntimeError('Demo router external fixed IP changed')
            return port if port.status=='ACTIVE' else None
        gateway_port = self.wait(gateway_ready,'Demo external gateway port did not become ACTIVE')
        result = dict(status='READY',generation=self.root.name,migration_owned=False,
            external=shared,router_id=router.id,router_ip=cfg['router_ip'],floating_ip_id=fip.id,
            floating_ip=cfg['floating_ip'],floating_ip_vm=vms[0]['id'],vms=vms,console_user=user,
            credentials_file=str(self.root/'demo-console-credentials.json'),
            gateway_port=dict(id=gateway_port.id,mac=gateway_port.mac_address,fixed_ips=gateway_port.fixed_ips),
            expected_resources=dict(server=sorted(self.state[k] for k in ('vm1','vm2')),
                network=sorted(self.state[k] for k in ('external_network','network1','network2')),
                subnet=sorted(self.state[k] for k in ('external_subnet','subnet1','subnet2')),
                router=[router.id]),
            manual_ping=dict(ew=[vms[0]['ip'],vms[1]['ip']],snat=[vms[1]['ip'],cfg['endpoint_ip']],
                             fip=[cfg['endpoint_ip'],cfg['floating_ip']]))
        save(self.root/'demo-resources.json', result)
        return result


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['validate'])
    parser.add_argument('config', type=Path)
    args = parser.parse_args()
    cfg = json.loads(args.config.read_text())
    if cfg.get('enabled'):
        validate_config(cfg)
    print('RESET_DEMO_SETTINGS_VERIFIED')
