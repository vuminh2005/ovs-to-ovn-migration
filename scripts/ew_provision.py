#!/usr/bin/env python3
"""Work Item 1: explicit-input, persistent EW provisioning; never migration cleanup."""
import argparse
import base64
import fcntl
import hashlib
import ipaddress
import json
import pathlib
import re
import shlex
import subprocess
import sys
import time

from dataplane_capture import save
from ew_transport import Transport, verify_profile
from validation_prerequisites import digest, qcow_virtual_size
from workload_resources import image_flavor_compatibility, resolve_ew

APP = pathlib.Path(__file__).resolve().parents[1] / 'workloads/ew-workload-app'
METRICS = APP.parent / 'ew-workload-metrics'


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def unique(rows, name, kind):
    matches = [r for r in rows if r.name == name]
    require(len(matches) <= 1, f'{kind} {name}: duplicate exact names ({len(matches)})')
    return matches[0] if matches else None


def same(resource, expected, kind):
    for key, value in expected.items():
        require(getattr(resource, key, None) == value,
                f'{kind} {resource.name}: mismatched {key}; expected {value!r}')


def security_group_rule_evidence(rule):
    """Keep the evidence's Neutron field name, using the SDK attribute explicitly."""
    return dict(ethertype=rule.ether_type, **{key: getattr(rule, key, None) for key in
                ('direction', 'protocol', 'remote_ip_prefix', 'remote_group_id',
                 'port_range_min', 'port_range_max')})


def file_md5(path):
    value = hashlib.md5()
    with pathlib.Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def console_host_keys(text):
    """Only cloud-init's public host-key block from the exact Nova server."""
    blocks = re.findall(r'-----BEGIN SSH HOST KEY KEYS-----\s*(.*?)\s*-----END SSH HOST KEY KEYS-----', text, re.S)
    require(len(blocks) == 1, 'New guest needs one authenticated cloud-init host-key block; supply verified known-hosts if console history is unavailable')
    keys = []
    for line in blocks[0].splitlines():
        fields = line.strip().split()
        if len(fields) >= 2 and fields[0] in ('ssh-ed25519', 'ssh-rsa', 'ecdsa-sha2-nistp256'):
            raw = base64.b64decode(fields[1], validate=True)
            require(len(raw) > 32, 'Malformed Nova console SSH public key')
            keys.append(' '.join(fields[:2]))
    require(bool(keys) and len(keys) == len(set(keys)), 'Missing/ambiguous console host keys')
    return keys


def service_handlers(paths, env_changed):
    shared = env_changed or '/opt/ew-lab/common.py' in paths
    return {name: shared or '/opt/ew-lab/' + source in paths or
            '/etc/systemd/system/' + name in paths
            for name, source in (('ew-api.service', 'api.py'), ('ew-worker.service', 'worker.py'))}


def validated_spec(spec, topology, excluded_root, require_bootstrap=True):
    """Reject missing authoritative inputs before any cloud mutation."""
    require(set(spec) == {'image', 'networks', 'security_group', 'keypair', 'keypair_public_key', 'secrets', 'bootstrap'},
            'Provisioning spec requires image, networks, security_group, keypair/public-key path, secrets and bootstrap')
    image = spec['image']; path = pathlib.Path(image['path']).resolve()
    require(path.is_file() and not path.is_relative_to(pathlib.Path(excluded_root).resolve()),
            'Retain the prepared netfix QCOW2 outside migration/reset backup roots')
    if image.get('sha256_file'):
        lines = pathlib.Path(image['sha256_file']).read_text().splitlines()
        require(len(lines) == 1, 'Netfix SHA256 sidecar must contain exactly one checksum')
        fields = lines[0].split()
        require(len(fields) in (1, 2) and (len(fields) == 1 or
                pathlib.Path(fields[1].lstrip('*')).name == path.name), 'Netfix SHA256 sidecar filename mismatch')
        require(not image.get('sha256') or image['sha256'] == fields[0].lower(), 'Netfix SHA256 sidecar differs from pinned SHA256')
        image['sha256'] = fields[0].lower()
    require(re.fullmatch('[0-9a-f]{64}', image['sha256']) is not None and digest(path) == image['sha256'],
            'Prepared netfix QCOW2 SHA256 mismatch')
    virtual = qcow_virtual_size(path)
    try:
        info = json.loads(subprocess.check_output(['qemu-img', 'info', '--output=json', str(path)], text=True, timeout=30))
        require(info.get('format') == 'qcow2' and not info.get('backing-filename') and
                info.get('virtual-size') == virtual, 'QCOW2 must be a standalone prepared netfix image')
        subprocess.run(['qemu-img', 'check', str(path)], check=True, capture_output=True, timeout=120)
    except (FileNotFoundError, subprocess.SubprocessError, ValueError):
        raise RuntimeError('Prepared QCOW2 requires successful read-only qemu-img info/check; no packages are installed') from None
    require(image['properties'] and isinstance(image['properties'], dict),
            'Explicit authoritative Glance properties are required')
    require(not set(image['properties']) & {'id', 'name', 'status', 'filename', 'data', 'disk_format', 'container_format'},
            'Image properties must not override image identity/upload parameters')
    require(set(spec['networks']) == {r['network'] for r in topology['servers']},
            'Specify exactly the two configured EW networks')
    require(len(topology['servers']) == 6 and len({r['name'] for r in topology['servers']}) == 6 and
            len({r['ip'] for r in topology['servers']}) == 6, 'Six unique EW names/IPs required')
    require({r['name'] for r in topology['servers']} == {'ew-app', 'ew-queue', 'ew-db', 'ew-client-a1', 'ew-client-a2', 'ew-client-b'},
            'Work Item 1 requires the authoritative six VM roles')
    require(len({n['subnet'] for n in spec['networks'].values()}) == 2, 'EW subnet names must be distinct')
    subnets = []
    for name, net in spec['networks'].items():
        cidr = ipaddress.IPv4Network(net['cidr']); gateway = ipaddress.IPv4Address(net['gateway_ip'])
        require(net['dns_nameservers'] == [] and net['host_routes'] == [], name + ': expected empty source DNS/host routes')
        require(gateway in cidr and gateway not in (cidr.network_address, cidr.broadcast_address),
                name + ': explicit gateway is outside usable subnet')
        require(net['subnet'] and isinstance(net['allocation_pools'], list) and net['allocation_pools'],
                name + ': explicit subnet name and allocation pools required')
        for pool in net['allocation_pools']:
            start, end = ipaddress.IPv4Address(pool['start']), ipaddress.IPv4Address(pool['end'])
            require(start in cidr and end in cidr and cidr.network_address < start <= end < cidr.broadcast_address
                    and not start <= gateway <= end, name + ': invalid DHCP allocation pool')
        for vm in (r for r in topology['servers'] if r['network'] == name):
            address = ipaddress.IPv4Address(vm['ip'])
            require(address in cidr and address not in (gateway, cidr.network_address, cidr.broadcast_address),
                    vm['name'] + ': fixed IP contradicts explicit subnet/gateway')
            require(all(not ipaddress.IPv4Address(p['start']) <= address <= ipaddress.IPv4Address(p['end'])
                        for p in net['allocation_pools']), vm['name'] + ': workload IP must remain outside the dynamic pool')
        require(all(not cidr.overlaps(other) for other in subnets), 'EW subnets overlap')
        subnets.append(cidr)
    require(spec['keypair'] and spec['security_group'], 'Explicit Nova keypair and security group names required')
    require(image['visibility'] == 'private' and image['min_disk'] == 10 and image['min_ram'] == 2048,
            'Netfix image must retain private visibility, min_disk=10 and min_ram=2048')
    for kind in (('db', 'mq') if require_bootstrap else ()):
        require(kind in spec['secrets'], kind + ': supply a matching private local password file path in the provisioning spec')
        secret = pathlib.Path(spec['secrets'][kind])
        require(secret.is_file() and secret.stat().st_mode & 0o077 == 0,
                kind + ': existing local password file must be private (0600)')
        require(re.fullmatch('[0-9a-f]{48}', secret.read_text().strip()) is not None,
                kind + ': password format must match authoritative setup.sh; value not logged')
    for role in (('ew-db', 'ew-queue') if require_bootstrap else ()):
        require(role in spec['bootstrap'], role + ': reviewed bootstrap source is missing; application provisioning is blocked')
        script = spec['bootstrap'][role]; path = pathlib.Path(script['path'])
        require(script.get('interpreter', 'bash') in ('bash', 'python3'), role + ': unsupported bootstrap interpreter')
        require(path.is_file() and digest(path) == script['sha256'],
                role + ': reviewed check/apply bootstrap adapter SHA256 mismatch or source missing')
    require(set(spec['bootstrap']) <= {'ew-db', 'ew-queue'}, 'Bootstrap adapters are allowed only for ew-db and ew-queue')
    # The authoritative setup script has fixed endpoints, user and service binds.
    expected = {'api': {'host': '192.168.101.11', 'port': 8080},
                'postgresql': {'host': '192.168.102.12', 'port': 5432},
                'rabbitmq': {'host': '192.168.102.11', 'port': 5672}}
    require(topology['endpoints'] == expected, 'Endpoints contradict authoritative application setup.sh')
    return virtual


class Provisioner:
    def __init__(self, cloud, root, spec, state, timeout=900, zone='nova', transport=None):
        self.cloud = cloud; self.root = pathlib.Path(root); self.spec = spec
        self.directory = pathlib.Path(state); self.timeout = timeout; self.zone = zone
        self.cfg = json.loads((self.root / 'ew-config.json').read_text())
        self.mtu = json.loads((self.root / 'mtu-calculation.json').read_text())['validation_source_mtu']
        self.access_cfg = json.loads((self.root / 'ew-measurement-config.json').read_text())
        self.state_path = self.directory / 'resources.json'
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {
            'schema_version': 1, 'ownership': 'persistent-ew-never-validation-owned', 'resources': {}}
        require(self.state.get('schema_version') == 1 and
                self.state.get('ownership') == 'persistent-ew-never-validation-owned', 'Invalid EW provisioning state')
        self.transport = transport; self.changed = False

    def checkpoint(self, kind, name, resource):
        key = kind + ':' + name
        prior = self.state['resources'].get(key)
        require(prior is None or prior == resource.id, f'{key}: UUID changed; no replacement/rebase permitted')
        self.state['resources'][key] = resource.id
        save(self.state_path, self.state)

    def known(self, kind, name, resource):
        prior = self.state['resources'].get(kind + ':' + name)
        require(prior is None or (resource and resource.id == prior),
                f'{kind} {name}: checkpointed resource missing/replaced; no automatic recreation')
        return resource

    def rules(self):
        # Only tenant CIDRs, never external access. Do not start any TCP listener.
        rules = []
        for net in self.spec['networks'].values():
            rules.append(dict(direction='ingress', ether_type='IPv4', protocol='icmp',
                              remote_ip_prefix=net['cidr'], port_range_min=None, port_range_max=None))
            rules.extend(dict(direction='ingress', ether_type='IPv4', protocol=protocol, remote_ip_prefix=net['cidr'],
                              port_range_min=None, port_range_max=None) for protocol in ('tcp', 'udp'))
        rules.extend(dict(direction='egress', ether_type=family, protocol=None, remote_ip_prefix=None,
                          port_range_min=None, port_range_max=None) for family in ('IPv4', 'IPv6'))
        return rules

    def rules_ready(self, existing):
        # Reused groups may have subsequent narrower rules; check effective
        # permission for required traffic without rewriting that group.
        ports = {22, *[e['port'] for e in self.cfg['endpoints'].values()], *self.access_cfg['tcp_ports']}
        def covers(rule, cidr, protocol, port=None):
            actual = getattr(rule, 'protocol', None)
            return (rule.direction == 'ingress' and rule.ether_type == 'IPv4' and
                    not getattr(rule, 'remote_group_id', None) and
                    actual in (None, protocol, {'tcp': '6', 'icmp': '1'}[protocol]) and
                    (not rule.remote_ip_prefix or ipaddress.ip_network(cidr).subnet_of(ipaddress.ip_network(rule.remote_ip_prefix))) and
                    (protocol != 'icmp' or actual is None or
                     rule.port_range_min in (None, -1) and rule.port_range_max in (None, -1)) and
                    (port is None or rule.port_range_min is None or rule.port_range_min <= port <= rule.port_range_max))
        return (all(any(covers(r, net['cidr'], 'icmp') for r in existing) and
                    all(any(covers(r, net['cidr'], 'tcp', port) for r in existing) for port in ports)
                    for net in self.spec['networks'].values()) and
                all(any(r.direction == 'egress' and r.ether_type == family and r.protocol is None and
                        r.remote_ip_prefix in (None, '0.0.0.0/0' if family == 'IPv4' else '::/0') and
                        not getattr(r, 'remote_group_id', None) for r in existing)
                    for family in ('IPv4', 'IPv6')))

    def plan(self, virtual_size):
        """Inspect the complete topology before creating any missing resource."""
        c = self.cloud; cfg = self.cfg; found = {}; missing = []
        def take(kind, name, rows):
            row = self.known(kind, name, unique(rows, name, kind))
            found[kind + ':' + name] = row
            if row is None: missing.append(kind + ':' + name)
            return row
        image = take('image', cfg['image'], c.image.images(name=cfg['image']))
        if image:
            same(image, dict(status='active', disk_format='qcow2', container_format='bare', visibility='private',
                             min_disk=10, min_ram=2048), 'image')
            # Verify actual content, not merely a matching friendly name.
            if getattr(image, 'hash_algo', None) == 'sha256':
                require(image.hash_value == self.spec['image']['sha256'], 'image: SHA256 content mismatch')
            else:
                require(getattr(image, 'checksum', None) == file_md5(self.spec['image']['path']), 'image: checksum missing/mismatched')
            for key, value in self.spec['image']['properties'].items():
                actual = (getattr(image, 'properties', None) or {}).get(key, getattr(image, key, None))
                require(actual == value, 'image: mismatched required property ' + key)
        flavor = take('flavor', cfg['flavor'], c.compute.flavors(details=True))
        if flavor:
            same(flavor, dict(vcpus=2, ram=2048, disk=10, ephemeral=0, is_public=True, is_disabled=False), 'flavor')
            require(getattr(flavor, 'swap', None) in (0, '', None), 'flavor: unexpected swap disk')
            require(c.compute.fetch_flavor_extra_specs(flavor).extra_specs == {}, 'flavor: unexpected extra specs')
        if flavor and image: image_flavor_compatibility(image, flavor, virtual_size)
        require(virtual_size <= 10 * 1024**3, 'Prepared image does not fit the authoritative 10 GB EW flavor')
        keypair = c.compute.find_keypair(self.spec['keypair'], ignore_missing=True)
        public = subprocess.check_output(['ssh-keygen', '-y', '-f', self.access_cfg['guest_key']], text=True, timeout=10)
        if keypair:
            require(public.split()[:2] == keypair.public_key.split()[:2], 'Nova keypair does not match configured guest SSH key')
        else:
            source = pathlib.Path(self.spec['keypair_public_key'])
            require(source.is_file() and source.read_text().split()[:2] == public.split()[:2],
                    'Missing keypair requires the original matching public-key file')
            missing.append('keypair:' + self.spec['keypair'])
        found['keypair:' + self.spec['keypair']] = self.known('keypair', self.spec['keypair'], keypair)
        sg = take('security_group', self.spec['security_group'], c.network.security_groups(name=self.spec['security_group']))
        if sg:
            existing = list(c.network.security_group_rules(security_group_id=sg.id))
            complete = self.rules_ready(existing)
            require(complete or self.state.get('created', {}).get('security_group:' + sg.name) == sg.id,
                    'security_group: required tenant ICMP/SSH/application/TCP rules missing; no mutation on reuse')
            if not complete: missing.append('security_group_rules:' + sg.name)
        router = take('router', cfg['router'], c.network.routers(name=cfg['router']))
        if router:
            require(not router.external_gateway_info, 'router: external gateway is outside Work Item 1 scope')
            same(router, dict(is_distributed=False, is_ha=False), 'router')
        for name, spec in self.spec['networks'].items():
            net = take('network', name, c.network.networks(name=name))
            subnet = take('subnet', spec['subnet'], c.network.subnets(name=spec['subnet']))
            if net: same(net, dict(provider_network_type='vxlan', mtu=self.mtu, is_router_external=False, is_shared=False), 'network')
            if subnet:
                require(net is not None, name + ': subnet exists without expected network')
                same(subnet, dict(network_id=net.id, cidr=spec['cidr'], gateway_ip=spec['gateway_ip'],
                                  ip_version=4, is_dhcp_enabled=True, allocation_pools=spec['allocation_pools'],
                                  dns_nameservers=spec['dns_nameservers'], host_routes=spec['host_routes']), 'subnet')
            if net:
                require(set(net.subnet_ids) == ({subnet.id} if subnet else set()), name + ': unexpected subnet association')
            if router and subnet:
                interfaces = [p for p in c.network.ports(network_id=net.id) if
                              p.device_owner.startswith('network:router_interface') and
                              any(f['subnet_id'] == subnet.id for f in p.fixed_ips)]
                require(len(interfaces) <= 1 and all(p.device_id == router.id and
                        p.fixed_ips == [dict(subnet_id=subnet.id, ip_address=spec['gateway_ip'])] for p in interfaces),
                        name + ': conflicting/ambiguous router interface')
                if not interfaces: missing.append('interface:' + name)
        for vm in cfg['servers']:
            name = vm['name']; net = found['network:' + vm['network']]
            subnet = found['subnet:' + self.spec['networks'][vm['network']]['subnet']]
            server = take('server', name, c.compute.servers(name=name, all_projects=True))
            if server:
                server = c.compute.get_server(server.id); found['server:' + name] = server
                require(server is not None, name + ': server disappeared during resolution')
            ports = list(c.network.ports(network_id=net.id)) if net else []
            hits = [p for p in ports if any(f['ip_address'] == vm['ip'] for f in p.fixed_ips)]
            named = [p for p in ports if p.name == name + '-port']
            require(len(hits) <= 1 and len(named) <= 1 and (not named or hits == named), name + ': fixed IP/port name conflict')
            port = hits[0] if hits else None
            self.known('port', name, port); found['port:' + name] = port
            if port:
                require(subnet is not None and sg is not None, name + ': port has missing expected subnet/SG')
                same(port, dict(fixed_ips=[dict(subnet_id=subnet.id, ip_address=vm['ip'])],
                                security_group_ids=[sg.id], is_port_security_enabled=True), 'port')
                require(not getattr(port, 'allowed_address_pairs', []), name + ': unexpected allowed address pairs')
                transitional = bool(server and server.status == 'BUILD' and not port.device_id and
                    self.state['resources'].get('server:' + name) == server.id and
                    self.state['resources'].get('port:' + name) == port.id)
                require(port.device_id == (server.id if server else '') or transitional,
                        name + ': fixed IP belongs to another server or attachment is not checkpointed')
            if server:
                require(image is not None and flavor is not None and port is not None, name + ': missing required image/flavor/port')
                require(server.status in ('ACTIVE', 'BUILD'), name + ': unexpected Nova state ' + server.status)
                require(server.image.get('id') == image.id and
                        server.flavor.get('id', server.flavor.get('original_name')) in (flavor.id, flavor.name),
                        name + ': image/flavor mismatch')
                require(server.key_name == self.spec['keypair'], name + ': Nova keypair mismatch')
                require(server.availability_zone == self.zone, name + ': availability zone mismatch')
                require(server.has_config_drive is True or str(server.has_config_drive).lower() == 'true', name + ': config drive is required')
                if server.status == 'ACTIVE': require(server.compute_host == vm['compute_host'], name + ': compute placement mismatch')
                attached = {p.id for p in c.network.ports(device_id=server.id)}
                require(attached == {port.id} or (transitional and not attached), name + ': unexpected additional server ports')
            if not port: missing.append('port:' + name)
        if router:
            allowed = {r.id for k, r in found.items() if k.startswith('network:') and r}
            require(all(p.network_id in allowed and p.device_owner.startswith('network:router_interface')
                        for p in c.network.ports(device_id=router.id)), 'router: unexpected additional interfaces')
        return found, missing

    def ensure(self, found, kind, name, create):
        row = found.get(kind + ':' + name)
        if row is None:
            row = create(); self.changed = True
            self.state.setdefault('created', {})[kind + ':' + name] = row.id
        self.checkpoint(kind, name, row)
        found[kind + ':' + name] = row
        return row

    def trust_new_guest(self, vm):
        """TOFU is not used: public keys arrive through authenticated Nova API.

        Existing known-host entries are never changed. Reused/non-checkpointed
        guests require operator-provided trust when console history is gone.
        """
        hosts = pathlib.Path(self.access_cfg['guest_known_hosts'])
        destination = self.access_cfg.get('direct_access', {}).get(vm['server'], vm['ip'])
        require(re.fullmatch(r'[A-Za-z0-9_.:-]+', destination) is not None, 'Unsafe guest SSH trust destination')
        check = subprocess.run(['ssh-keygen', '-F', destination, '-f', str(hosts)], capture_output=True, text=True)
        if check.returncode == 0: return
        require(check.returncode == 1 or not hosts.exists(), 'Cannot inspect guest known-hosts file')
        require(self.state.get('created', {}).get('server:' + vm['name']) == vm['server'],
                vm['name'] + ': reused guest needs operator-verified SSH host keys')
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                console = self.cloud.compute.get_server_console_output(vm['server'])
                text = console['output'] if isinstance(console, dict) else console
                keys = console_host_keys(text)
                break
            except Exception as exc:
                # Only incomplete cloud-init output/readiness conflicts retry.
                if not isinstance(exc, RuntimeError) and getattr(exc, 'status_code', None) != 409: raise
                if time.monotonic() >= deadline: raise RuntimeError(vm['name'] + ': authenticated SSH host-key collection timed out') from None
                time.sleep(2)
        hosts.parent.mkdir(parents=True, exist_ok=True)
        with hosts.open('a') as stream:
            stream.write(''.join(destination + ' ' + key + '\n' for key in keys))
        hosts.chmod(0o600)

    def apply(self, found):
        c = self.cloud; cfg = self.cfg; spec = self.spec
        self.ensure(found, 'keypair', spec['keypair'], lambda: c.compute.create_keypair(
            name=spec['keypair'], public_key=pathlib.Path(spec['keypair_public_key']).read_text().strip()))
        image = self.ensure(found, 'image', cfg['image'], lambda: c.image.create_image(
            name=cfg['image'], filename=spec['image']['path'], disk_format='qcow2', container_format='bare',
            visibility='private', min_disk=10, min_ram=2048,
            allow_duplicates=False, wait=True, timeout=self.timeout, **spec['image']['properties']))
        same(image, dict(status='active'), 'image')
        flavor = self.ensure(found, 'flavor', cfg['flavor'], lambda: c.compute.create_flavor(
            name=cfg['flavor'], vcpus=2, ram=2048, disk=10, ephemeral=0, swap=0, is_public=True))
        image_flavor_compatibility(image, flavor, qcow_virtual_size(pathlib.Path(spec['image']['path'])))
        sg = self.ensure(found, 'security_group', spec['security_group'], lambda: c.network.create_security_group(name=spec['security_group']))
        existing = list(c.network.security_group_rules(security_group_id=sg.id))
        if not self.rules_ready(existing):
            require(self.state.get('created', {}).get('security_group:' + sg.name) == sg.id,
                    'Existing security group permissions changed after inspection')
            for rule in self.rules():
                if not any(all(getattr(r, k, None) == v for k, v in rule.items()) for r in existing):
                    c.network.create_security_group_rule(security_group_id=sg.id, **rule); self.changed = True
        router = self.ensure(found, 'router', cfg['router'], lambda: c.network.create_router(
            name=cfg['router'], is_distributed=False, is_ha=False))
        for name, net_spec in spec['networks'].items():
            net = self.ensure(found, 'network', name, lambda: c.network.create_network(
                name=name, provider_network_type='vxlan', mtu=self.mtu, is_router_external=False, is_shared=False))
            subnet = self.ensure(found, 'subnet', net_spec['subnet'], lambda: c.network.create_subnet(
                name=net_spec['subnet'], network_id=net.id, cidr=net_spec['cidr'], gateway_ip=net_spec['gateway_ip'],
                allocation_pools=net_spec['allocation_pools'], dns_nameservers=net_spec['dns_nameservers'],
                host_routes=net_spec['host_routes'], ip_version=4, is_dhcp_enabled=True))
            attached = [p for p in c.network.ports(device_id=router.id, network_id=net.id)
                        if any(f['subnet_id'] == subnet.id for f in p.fixed_ips)]
            if not attached: c.network.add_interface_to_router(router.id, subnet_id=subnet.id); self.changed = True
        for vm in cfg['servers']:
            name = vm['name']; net = found['network:' + vm['network']]
            subnet = found['subnet:' + spec['networks'][vm['network']]['subnet']]
            port = self.ensure(found, 'port', name, lambda: c.network.create_port(
                name=name + '-port', network_id=net.id, fixed_ips=[dict(subnet_id=subnet.id, ip_address=vm['ip'])],
                security_group_ids=[sg.id], is_port_security_enabled=True))
            server = self.ensure(found, 'server', name, lambda: c.compute.create_server(
                name=name, image_id=image.id, flavor_id=flavor.id, networks=[{'port': port.id}],
                key_name=spec['keypair'], availability_zone=self.zone,
                host=vm['compute_host'], has_config_drive=True))
            # UUID is already durable before waiting; never recreate on timeout.
            self.wait_attachment(vm, server, port, image, flavor)

    def wait_attachment(self, vm, server, port, image, flavor):
        """One bounded Nova/Neutron recovery wait, never rebind an interface."""
        c = self.cloud; deadline = time.monotonic() + self.timeout
        server_id, port_id, network_id = server.id, port.id, port.network_id
        fixed = sorted((f['subnet_id'], f['ip_address']) for f in port.fixed_ips)
        c.compute.wait_for_server(server, status='ACTIVE', failures=['ERROR'], interval=2, wait=self.timeout)
        while True:
            live = c.compute.get_server(server_id); current = c.network.get_port(port_id)
            require(live is not None and current is not None, vm['name'] + ': server/port disappeared while waiting')
            require(live.id == server_id and current.id == port_id and current.network_id == network_id and
                    sorted((f['subnet_id'], f['ip_address']) for f in current.fixed_ips) == fixed and live.image.get('id') == image.id and
                    live.flavor.get('id', live.flavor.get('original_name')) in (flavor.id, flavor.name) and
                    live.key_name == self.spec['keypair'] and live.availability_zone == self.zone and
                    str(live.has_config_drive).lower() == 'true', vm['name'] + ': identity changed while waiting for attachment')
            require(live.status == 'ACTIVE' and live.compute_host == vm['compute_host'],
                    vm['name'] + ': Nova ACTIVE/placement check failed after wait')
            require(current.device_id in ('', server_id), vm['name'] + ': port attached to another server')
            attached = {p.id for p in c.network.ports(device_id=server_id)}
            require(attached <= {port_id}, vm['name'] + ': unexpected additional server ports')
            if current.device_id == server_id and current.status == 'ACTIVE' and attached == {port_id}:
                return
            require(time.monotonic() < deadline, vm['name'] + ': port attachment/ACTIVE timed out; resources retained')
            time.sleep(min(2, max(0, deadline - time.monotonic())))

    def guest(self, vm, access, argv, data=None, timeout=None):
        return self.transport.run(self.transport.guest_argv(vm, access, timeout) +
                                  [shlex.join(['sudo', '-n', *argv])], data, timeout)

    def install(self, vm, access, files, secrets=False, dry_run=False):
        """Compare first, atomic writes; credentials are immutable once present."""
        code = '''import base64,json,os,pathlib,re,sys,tempfile
os.umask(0o077)
d=json.load(sys.stdin); changed=[]
for name,value in d['files'].items():
 p=pathlib.Path(name); body=base64.b64decode(value,validate=True)
 if d['secrets']:
  value=body.decode().strip()
  if not re.fullmatch('[0-9a-f]{48}',value): raise RuntimeError('Invalid credential format')
  if p.exists():
   if not p.is_file() or p.stat().st_mode & 0o077: raise RuntimeError('Existing credential permissions are not private')
   previous=p.read_text().strip()
   if not re.fullmatch('[0-9a-f]{48}',previous): raise RuntimeError('Invalid existing credential format')
   if previous!=value: raise RuntimeError('Existing credential differs; refusing overwrite')
   continue
 if p.exists() and p.read_bytes()==body: continue
 if d['dry_run']: changed.append(name); continue
 p.parent.mkdir(parents=True,exist_ok=True)
 if not d['secrets']: p.parent.chmod(0o755)
 fd,tmp=tempfile.mkstemp(dir=p.parent)
 with os.fdopen(fd,'wb') as stream: stream.write(body)
 os.chmod(tmp,0o600 if d['secrets'] else 0o644); os.replace(tmp,p); changed.append(name)
print(json.dumps({'paths':changed}))'''
        payload = dict(files={str(k): base64.b64encode(v).decode() for k, v in files.items()}, secrets=secrets, dry_run=dry_run)
        changed = json.loads(self.guest(vm, access, ['python3', '-c', code], json.dumps(payload)))['paths']
        self.changed |= bool(changed) and not dry_run
        return changed

    def deploy_app(self, vm, access):
        """Use original files/units; only changed services receive handlers."""
        self.baked_dependencies(vm, access, ['gunicorn', 'psycopg2', 'pika'])
        setup = (APP / 'setup.sh').read_text()
        units = dict(re.findall(r"cat > (/etc/systemd/system/[^ ]+) <<'UNIT'\n(.*?)\nUNIT", setup, re.S))
        require(len(units) == 2, 'Authoritative application unit definitions missing')
        # Generate the same environment locally inside the guest. Never return
        # its contents, and never replace existing credentials with new values.
        env_code = '''import json,os,pathlib,re,sys,tempfile
os.umask(0o077); root=pathlib.Path('/etc/ew-lab')
passwords={k:(root/(k+'-password')).read_text().strip() for k in ('db','mq')}
if not all(re.fullmatch('[0-9a-f]{48}',v) for v in passwords.values()): raise RuntimeError('Invalid private credential format')
p=root/'app.env'
if p.exists():
 if p.stat().st_mode & 0o077: raise RuntimeError('Existing application environment permissions are not private')
 values=dict(line.split('=',1) for line in p.read_text().splitlines() if '=' in line)
 if any(values.get('EW_'+k.upper()+'_PASSWORD')!=v for k,v in passwords.items()): raise RuntimeError('Existing application credentials differ; refusing overwrite')
text='\\n'.join(['EW_DB_HOST=192.168.102.12','EW_MQ_HOST=192.168.102.11','EW_DB_PASSWORD='+passwords['db'],'EW_MQ_PASSWORD='+passwords['mq'],'PYTHONUNBUFFERED=1','PYTHONDONTWRITEBYTECODE=1'])+'\\n'
changed=not p.exists() or p.read_text()!=text
if changed and sys.argv[1]=='apply':
 fd,tmp=tempfile.mkstemp(dir=root)
 with os.fdopen(fd,'w') as stream: stream.write(text)
 os.chmod(tmp,0o600); os.replace(tmp,p)
print(json.dumps({'changed':changed}))'''
        files = {'/opt/ew-lab/' + f: (APP / f).read_bytes() for f in ('common.py', 'api.py', 'worker.py', 'probe.py')}
        files.update({path: (body + '\n').encode() for path, body in units.items()})
        env_changed = json.loads(self.guest(vm, access, ['python3', '-c', env_code, 'plan']))['changed']
        paths = self.install(vm, access, files, dry_run=True)
        identity = {key: vm[key] for key in ('server', 'port', 'ip')}
        target = hashlib.sha256(json.dumps({path: hashlib.sha256(body).hexdigest()
            for path, body in files.items()}, sort_keys=True).encode()).hexdigest()
        pending = self.state.get('application_deployment_pending')
        if pending is not None:
            require(isinstance(pending, dict) and pending.get('schema_version') == 1 and
                    type(pending.get('initialize')) is bool and type(pending.get('reload')) is bool and
                    isinstance(pending.get('services'), dict) and set(pending['services']) == set(service_handlers([], False)) and
                    all(type(value) is bool for value in pending['services'].values()),
                    'Malformed unfinished application deployment; refusing to discard pending actions')
            require(pending['identity'] == identity and pending['target'] == target,
                    'Unfinished application deployment identity/source changed; finish the original deployment first')
        else:
            pending = dict(schema_version=1, identity=identity, target=target, initialize=False, reload=False,
                           services=service_handlers([], False))
            self.state['application_deployment_pending'] = pending
        # Persist the union BEFORE either mutation. Once the files match, the
        # previous required actions remain durable across process/controller loss.
        pending['initialize'] |= bool(env_changed or paths)
        pending['reload'] |= any(p.startswith('/etc/systemd/') for p in paths)
        for name, needed in service_handlers(paths, env_changed).items(): pending['services'][name] |= needed
        save(self.state_path, self.state)
        self.guest(vm, access, ['python3', '-c', env_code, 'apply'])
        self.install(vm, access, files)
        # CREATE TABLE/INDEX IF NOT EXISTS only: original common.initialize()
        # never DROP/TRUNCATEs, resets jobs, purges queues or rotates passwords.
        if pending['initialize']:
            self.guest(vm, access, ['bash', '-c',
                'set -euo pipefail; set -a; source /etc/ew-lab/app.env; set +a; /usr/bin/python3 /opt/ew-lab/common.py'])
            pending['initialize'] = False; save(self.state_path, self.state)
        actions = []
        if pending['reload']:
            self.guest(vm, access, ['systemctl', 'daemon-reload'])
            pending['reload'] = False; save(self.state_path, self.state)
            actions.append('daemon-reload')
        code = '''import json,subprocess,sys
d=json.load(sys.stdin); actions=[]
def run(a): subprocess.run(a,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
for name,restart in d['services'].items():
 if subprocess.run(['systemctl','is-enabled','--quiet',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode:
  run(['systemctl','enable',name]); actions.append('enable:'+name)
 active=subprocess.run(['systemctl','is-active','--quiet',name]).returncode==0
 if not active or restart:
  verb='restart' if active else 'start'; run(['systemctl',verb,name]); actions.append(verb+':'+name)
 run(['systemctl','is-active','--quiet',name])
print(json.dumps({'actions':actions}))'''
        for name in pending['services']:
            actions.extend(json.loads(self.guest(vm, access, ['python3', '-c', code],
                json.dumps(dict(services={name: pending['services'][name]}))))['actions'])
            pending['services'][name] = False; save(self.state_path, self.state)
        self.changed |= env_changed or bool(actions)
        return dict(changed_files=paths, environment_changed=env_changed, service_actions=actions)

    def task_receipt(self, catalog, client, task_id):
        """Read an existing authoritative API result; never resubmit/reset it."""
        vm = catalog['servers'][client]; access = self.transport.access(vm, 'source')
        api = self.cfg['endpoints']['api']
        code = ('import json,sys; from urllib.request import build_opener,ProxyHandler; '
                'r=build_opener(ProxyHandler({})).open(sys.argv[1],timeout=10); '
                'print(json.dumps(json.load(r)["job"]))')
        receipt = json.loads(self.guest(vm, access, ['python3', '-c', code,
            f"http://{api['host']}:{api['port']}/jobs/{task_id}"]))
        require(receipt.get('task_id') == task_id and receipt.get('status') == 'done' and
                receipt.get('process_count') == 1, 'Previously completed EW task no longer intact')
        return {k: receipt[k] for k in ('task_id', 'run_id', 'client_id', 'status',
                'result_sha256', 'process_count', 'created_at', 'completed_at')}

    def baked_dependencies(self, vm, access, modules):
        code = '''# EW_BAKED_DEPENDENCIES
import importlib,json,shutil,sys
missing=[]
for name in json.loads(sys.argv[1]):
 try: importlib.import_module(name)
 except Exception: missing.append(name)
missing += [name for name in ('cloud-init','ping','python3','sudo','systemctl') if not shutil.which(name)]
print(json.dumps({'missing':missing}))'''
        outcome = json.loads(self.guest(vm, access, ['python3', '-c', code, json.dumps(modules)]))
        require(not outcome['missing'], 'Missing baked guest dependencies (no installation attempted): ' + ', '.join(outcome['missing']))

    def bootstrap(self, name, vm, access):
        descriptor = self.spec['bootstrap'][name]
        source = pathlib.Path(descriptor['path']).read_bytes()
        require(hashlib.sha256(source).hexdigest() == descriptor['sha256'], name + ': bootstrap source changed since preflight')
        interpreter = descriptor.get('interpreter', 'bash')
        path = '/opt/ew-provision/bootstrap.py' if interpreter == 'python3' else '/opt/ew-provision/bootstrap.sh'
        self.install(vm, access, {path: source})
        def command(action):
            return [interpreter, path, action] + ([name] if interpreter == 'python3' else [])
        outcome = json.loads(self.guest(vm, access, command('check'), timeout=self.timeout))
        require(outcome.get('status') in ('PASS', 'CHANGE_REQUIRED'),
                name + ': bootstrap inspection refused: ' + str(outcome.get('reason', outcome.get('status', 'invalid response'))))
        if outcome['status'] == 'CHANGE_REQUIRED':
            output = self.guest(vm, access, command('apply'), timeout=self.timeout)
            # Retain the historical bash adapter contract (exit zero). The
            # reconstructed Python adapters additionally return structured status.
            if interpreter == 'python3':
                applied = json.loads(output)
                require(applied.get('status') == 'PASS', name + ': bootstrap apply did not pass')
                self.changed |= applied.get('changed', True)
            else:
                self.changed = True
        require(json.loads(self.guest(vm, access, command('check'), timeout=self.timeout)).get('status') == 'PASS',
                name + ': reviewed DB/broker bootstrap check failed')

    def ready(self, deploy=False):
        catalog = resolve_ew(self.cloud, self.cfg, self.root)
        self.transport = self.transport or Transport(self.access_cfg, catalog, cloud=self.cloud)
        result = dict(status='PASS', guests={}, tasks={})
        for name, vm in catalog['servers'].items():
            if deploy: self.trust_new_guest(dict(vm, name=name))
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    access = self.transport.access(vm, 'source')
                    profile = self.transport.profile(vm, access)
                    mac = self.cloud.network.get_port(vm['port']).mac_address
                    prior = self.state.get('guests', {}).get(name, {})
                    require(not prior or all(prior[k] == vm[k] for k in ('server', 'port', 'ip')),
                            name + ': recorded guest resource identity changed')
                    verify_profile(profile, vm, prior.get('boot'), self.mtu, mac)
                    status = json.loads(self.guest(vm, access, ['cloud-init', 'status', '--format', 'json']))
                    require(status.get('status') == 'done' and not status.get('errors') and
                            not status.get('recoverable_errors'), name + ': cloud-init not completed cleanly')
                    break
                except (RuntimeError, subprocess.TimeoutExpired, ValueError) as exc:
                    if time.monotonic() >= deadline:
                        detail = str(exc) if type(exc) is RuntimeError else type(exc).__name__
                        raise RuntimeError(name + ': guest identity/SSH/cloud-init readiness timed out; ' + detail) from None
                    time.sleep(min(2, max(0, deadline-time.monotonic())))
            self.state.setdefault('guests', {})[name] = dict(boot=profile['boot'], server=vm['server'], port=vm['port'], ip=vm['ip'])
            save(self.state_path, self.state)
            result['guests'][name] = dict(server=vm['server'], port=vm['port'], ip=vm['ip'], boot=profile['boot'], cloud_init='PASS')
            if name in self.state.get('preserved_tasks', {}):
                prior = self.state['preserved_tasks'][name]
                require(self.task_receipt(catalog, name, prior['task_id']) == prior,
                        name + ': pre-existing task result changed before provisioning')
            if deploy:
                self.baked_dependencies(vm, access, [])
                kinds = ('db', 'mq') if name == 'ew-app' else ('db',) if name == 'ew-db' else ('mq',) if name == 'ew-queue' else ()
                self.install(vm, access, {'/etc/ew-lab/' + kind + '-password':
                             pathlib.Path(self.spec['secrets'][kind]).read_text().strip().encode() for kind in kinds}, secrets=True)
                self.install(vm, access, {'/opt/ew-load/' + f: (METRICS / f).read_bytes() for f in ('agent.py', 'runner.py')})
                if name in self.spec['bootstrap']:
                    self.bootstrap(name, vm, access)
                self.install(vm, access, {'/opt/ew-provision/probe.py': (APP / 'probe.py').read_bytes()})
        # Dependencies are prepared first; deploy the original app sources with
        # change-aware handlers, without invoking setup.sh on ordinary reruns.
        vm = catalog['servers']['ew-app']; access = self.transport.access(vm, 'source')
        if deploy:
            result['application_deployment'] = self.deploy_app(vm, access)
        # Existing authoritative smoke probe proves DB commit, queue/worker and
        # validated result, retry/conflict semantics from each real client.
        for name in ('ew-client-a1', 'ew-client-a2', 'ew-client-b'):
            vm = catalog['servers'][name]; access = self.transport.access(vm, 'source')
            output = self.guest(vm, access, ['python3', '-', '--client-id', name],
                                data=(APP / 'probe.py').read_text(), timeout=90)
            require('WORKLOAD_E2E_OK' in output.splitlines(), name + ': authoritative task-completion smoke failed')
            ids = re.findall(r'E2E_RESULT_OK client=' + re.escape(name) + r' task_id=([0-9a-f-]{36})\b', output)
            require(len(ids) == 1, name + ': authoritative task receipt missing/ambiguous')
            prior = self.state.setdefault('preserved_tasks', {}).get(name)
            if prior:
                require(self.task_receipt(catalog, name, prior['task_id']) == prior,
                        name + ': pre-existing task result changed during provisioning')
            else:
                self.state['preserved_tasks'][name] = self.task_receipt(catalog, name, ids[0])
                save(self.state_path, self.state)
            for endpoint in self.cfg['endpoints'].values():
                self.guest(vm, access, ['ping', '-c', '2', '-W', '2', endpoint['host']])
                self.guest(vm, access, ['python3', '-c',
                    'import socket,sys; socket.create_connection((sys.argv[1],int(sys.argv[2])),timeout=3).close()',
                    endpoint['host'], str(endpoint['port'])])
            result['tasks'][name] = dict(status='PASS', evidence=output)
        result['preserved_tasks'] = dict(status='PASS', receipts=self.state['preserved_tasks'])
        save(self.root / 'ew-provision-readiness.json', result)
        save(self.directory / 'readiness.json', result)
        if deploy:
            # Only the full authoritative client/ICMP/TCP readiness path may
            # finalize a deployment, never file equality or service ACTIVE alone.
            self.state.pop('application_deployment_pending', None)
            save(self.state_path, self.state)
        return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('inspect', 'apply', 'verify')); p.add_argument('root', type=pathlib.Path)
    p.add_argument('--spec', type=pathlib.Path, required=True); p.add_argument('--state', type=pathlib.Path, required=True)
    p.add_argument('--excluded-root', required=True); p.add_argument('--timeout', type=int, default=900)
    p.add_argument('--zone', default='nova'); args = p.parse_args()
    require(args.timeout > 0, 'Positive provisioning timeout required')
    require(args.state.is_absolute() and not args.state.resolve().is_relative_to(pathlib.Path(args.excluded_root).resolve()),
            'Persistent EW state must be outside the migration backup root')
    spec = json.loads(args.spec.read_text()); topology = json.loads((args.root / 'ew-config.json').read_text())
    virtual = validated_spec(spec, topology, args.excluded_root, require_bootstrap=args.action == 'apply')
    import openstack
    args.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (args.state / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        provision = Provisioner(openstack.connect(compute_api_version='2.74', api_timeout=min(30, args.timeout)),
                                args.root, spec, args.state, args.timeout, args.zone)
        found, missing = provision.plan(virtual)
        save(args.root / 'ew-provision-plan.json', dict(status='PASS', missing=missing,
             resources={k: r.id if r else None for k, r in found.items()}, source_mtu=provision.mtu,
             security_group_rules=[security_group_rule_evidence(r)
                 for r in provision.cloud.network.security_group_rules(security_group_id=found['security_group:' + spec['security_group']].id)]
                 if found.get('security_group:' + spec['security_group']) else []))
        if args.action == 'apply': provision.apply(found)
        if args.action in ('apply', 'verify'):
            require(args.action == 'apply' or not missing, 'verify requires all EW resources already present')
            provision.ready(deploy=args.action == 'apply')
        print(json.dumps(dict(status='PASS', action=args.action, changed=provision.changed,
                              scope='resource-plan-only' if args.action == 'inspect' else 'guest-and-application-readiness',
                              missing_before=missing, evidence=str(args.root), state=str(args.state))))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # SDK/SSH/bootstrap exception bodies may contain credentials; preserve
        # only our own explicit conflict/prerequisite messages.
        message = str(exc) if type(exc) is RuntimeError else type(exc).__name__
        print('EW provisioning refused: ' + message, file=sys.stderr)
        sys.exit(1)
