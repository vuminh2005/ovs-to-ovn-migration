#!/usr/bin/env python3
"""Owned EW matrix endpoints and SNAT/FIP continuity, checkpointed independently."""
import base64
import copy
import json
import ipaddress
import uuid
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace

from dataplane_capture import Capture, identity, alive
from workload_validation import read_evidence, save

FLOW_NAMES = ('ew_same_compute_different_subnet', 'ew_different_compute_same_subnet',
              'ew_same_compute_same_subnet')
DISPLAY_NAMES = {'measure': 'EW different compute / different subnet',
                 'existing': 'Existing workload DHCP/MTU/metadata',
                 'fresh': 'Fresh OVN provisioning', 'tcp': 'EW TCP continuity'}


def snat_source_port_status(stats):
    """Only successful echoes establish the guest and externally visible ports."""
    pairs = [(stats.get(edge+'_local_endpoint'), stats.get(edge+'_peer_endpoint'))
             for edge in ('first', 'last')]
    if not all(isinstance(endpoint, (list, tuple)) and len(endpoint) == 2 and
               isinstance(endpoint[0], str) and type(endpoint[1]) is int and 0 < endpoint[1] <= 65535
               for pair in pairs for endpoint in pair):
        return 'UNAVAILABLE'
    return 'PASS' if all(local[1] == peer[1] for local, peer in pairs) else 'FAIL'


def add_ping_files(data, cfg):
    data['write_files'].extend([
        dict(path='/usr/local/bin/aux-ping.py', content=(Path(__file__).parent/'aux_ping.py').read_text(), permissions='0700'),
        dict(path='/etc/aux-ping.json', content=json.dumps(cfg), permissions='0600'),
        dict(path='/etc/systemd/system/aux-ping.service', content='[Unit]\nAfter=network-online.target\n[Service]\nRestart=no\nExecStart=/usr/bin/python3 /usr/local/bin/aux-ping.py\n[Install]\nWantedBy=multi-user.target\n')])
    data['runcmd'].append(['systemctl', 'enable', '--now', 'aux-ping'])
    return data


def quota20(cloud, root):
    project = cloud.session.get_project_id()
    if not project:
        raise RuntimeError('Workload authentication must be project scoped')
    before = cloud.compute.get_quota_set(project)
    original = {k: getattr(before, k) for k in ('instances', 'cores', 'ram')}
    # SDK 3.x expects a QuotaSet/URI attribute; SDK 4.x accepts project IDs.
    # Explicit project_id retains the supported compatibility path in both.
    cloud.compute.update_quota_set(project, project_id=project, instances=20)
    after = cloud.compute.get_quota_set(project)
    row = dict(project=project, before=original,
               after={k: getattr(after, k) for k in ('instances', 'cores', 'ram')})
    row['status'] = 'PASS' if after.instances == 20 and after.cores == original['cores'] and after.ram == original['ram'] else 'FAIL'
    save(root/'quota-instances.json', row)
    if row['status'] != 'PASS':
        raise RuntimeError('Quota verification failed: only instances may change')


class Extended:
    def __init__(self, validation):
        self.v = validation
        self.root = validation.root
        self.cfg = validation.cfg
        self.cloud = validation.cloud
        self.path = self.root/'extended-resources.json'
        self.state = read_evidence(self.root, self.path.name) or {'schema': 1, 'run': self.cfg['run']}
        if self.state['run'] != self.cfg['run']:
            raise RuntimeError('Extended workload run identity changed')

    @property
    def matrix(self):
        return self.cfg.get('ew_matrix_enabled', False)

    @property
    def ns(self):
        return self.cfg.get('ns', {}).get('enabled', False)

    def commit(self):
        save(self.path, self.state)

    def capacity(self):
        if not (self.matrix or self.ns):
            return
        quota20(self.cloud, self.root)
        flavor = self.cloud.compute.find_flavor(self.cfg['flavor'], ignore_missing=False)
        servers = list(self.cloud.compute.servers(all_projects=False))
        owned = [s for s in servers if (s.metadata or {}).get('ovn_migration_run') == self.cfg['run']]
        count = 6 + 2*bool(self.cfg.get('pair_d_enabled')) + 2*self.matrix + 2*self.ns
        remaining = count-len(owned)
        limits = self.cloud.compute.get_limits().absolute
        checks = {}
        for key, demand, used in [('instances', count, len(servers)),
                                  ('cores', remaining*flavor.vcpus, limits.total_cores_used),
                                  ('ram', remaining*flavor.ram, limits.total_ram_used)]:
            quota = getattr(self.cloud.compute.get_quota_set(self.cloud.session.get_project_id()), key)
            needed = used+remaining if key == 'instances' else used+demand
            checks[key] = dict(quota=quota, projected=needed, status='PASS' if quota == -1 or needed <= quota else 'FAIL')
        nodes = list(self.cloud.compute.hypervisors(details=True))
        per_host = count//2
        for target in self.cfg['placement'].values():
            node = next(h for h in nodes if h.name == target['hypervisor'])
            current_owned = sum(s.compute_host == target['nova_host'] for s in owned)
            slots = per_host-current_owned
            checks[target['inventory_host']] = dict(additional_instances=slots,
                free_ram_mb=node.memory_size-node.memory_used, required_ram_mb=slots*flavor.ram,
                free_disk_gb=node.local_disk_size-node.local_disk_used, required_disk_gb=slots*flavor.disk,
                status='PASS' if node.memory_size-node.memory_used >= slots*flavor.ram and
                node.local_disk_size-node.local_disk_used >= slots*flavor.disk else 'FAIL')
        save(self.root/'workload-capacity.json', dict(status='PASS' if all(c['status']=='PASS' for c in checks.values()) else 'FAIL',
              max_instances=count, max_per_compute=per_host, flavor=dict(id=flavor.id, ram=flavor.ram, vcpus=flavor.vcpus, disk=flavor.disk), checks=checks))
        if any(c['status'] != 'PASS' for c in checks.values()):
            raise RuntimeError('Insufficient quota/compute RAM or disk; inspect workload-capacity.json')

    def resource(self, state, key, kind, name, **attrs):
        proxy = self.cloud.network
        if state.get(key):
            return getattr(proxy, 'get_'+kind)(state[key])
        collection = {'network': 'networks', 'subnet': 'subnets', 'router': 'routers',
                      'security_group': 'security_groups', 'port': 'ports'}[kind]
        matches = [r for r in getattr(proxy, collection)(name=name) if r.name == name]
        marker = 'ovn-migration-run='+self.cfg['run']
        if len(matches) > 1 or matches and matches[0].description != marker:
            raise RuntimeError('Ambiguous/unowned '+kind+' name; refusing adoption')
        obj = matches[0] if matches else getattr(proxy, 'create_'+kind)(name=name, description=marker, **attrs)
        state[key] = obj.id
        self.commit()
        return obj

    def port(self, group, key, network, subnet, role, placement, security_group, ip=None):
        vm = group.setdefault(key, {})
        name = self.cfg['prefix']+'-'+self.cfg['run']+'-'+role
        attrs = dict(network_id=network, security_group_ids=[security_group])
        if ip:
            attrs['fixed_ips'] = [dict(subnet_id=subnet, ip_address=ip)]
        obj = self.resource(vm, 'port', 'port', name, **attrs)
        if obj.network_id != network or security_group not in obj.security_group_ids or len(obj.fixed_ips) != 1:
            raise RuntimeError('Checkpointed extended port changed')
        if ip and obj.fixed_ips != attrs['fixed_ips']:
            raise RuntimeError('Fixed N-S address changed')
        expected = dict(network=network, subnet=subnet, placement=placement, role=role,
                        name=name, record_vm=role, ip=obj.fixed_ips[0]['ip_address'], fixed_ips=obj.fixed_ips, owned=True)
        if any(k in vm and vm[k] != val for k, val in expected.items()):
            raise RuntimeError('Extended workload identity/placement changed')
        vm.update(expected)
        self.commit()
        return vm

    def prepare_matrix_ports(self):
        if not self.matrix:
            return
        group = self.state.setdefault('matrix', {})
        net = self.v.state['pre']['networks']['1']
        for key in ('0', '1'):
            self.port(group, key, net['network'], net['subnet'], 'ew-matrix-'+key,
                      self.cfg['placement'][key], self.v.state['pre']['security_group'])

    def ping_cfg(self, vm, peer=None):
        return dict(run=self.cfg['run'], vm=vm['record_vm'], ip=vm['ip'], peer=peer,
                    interval=self.cfg['interval'], lifetime=self.cfg['lifetime'])

    def decorate_measure(self, data, index):
        if self.matrix:
            vm = self.v.pair('measure')[index]
            add_ping_files(data, self.ping_cfg(vm, self.state['matrix'][index]['ip']))
        return data

    def server(self, vm, data):
        if vm.get('server'):
            self.v.wait_active(vm['server'])
            self.audit(vm)
            return
        matches = [s for s in self.cloud.compute.servers(name=vm['name']) if s.name == vm['name']]
        if len(matches) > 1:
            raise RuntimeError('Ambiguous extended server')
        if matches:
            s = matches[0]
            if (s.metadata or {}).get('ovn_migration_run') != self.cfg['run'] or (s.metadata or {}).get('ovn_validation_role') != vm['role'] or self.cloud.network.get_port(vm['port']).device_id != s.id:
                raise RuntimeError('Cannot adopt unowned extended server')
        else:
            from validation_placement import create_on_target
            import yaml
            s = create_on_target(self.cloud.compute, vm['placement'], name=vm['name'], image_id=self.cfg['image'],
                  flavor_id=self.cfg['flavor'], networks=[{'port': vm['port']}],
                  metadata={'ovn_migration_run': self.cfg['run'], 'ovn_validation_role': vm['role']},
                  user_data=base64.b64encode(('#cloud-config\n'+yaml.safe_dump(data)).encode()).decode())
        vm['server'] = s.id
        self.commit()
        self.v.wait_active(s.id)
        self.v.wait_placement(vm)

    def audit(self, vm):
        s = self.cloud.compute.get_server(vm['server'])
        p = self.cloud.network.get_port(vm['port'])
        if (vm.get('owned') is not True or s.id != vm['server'] or p.device_id != s.id or
            p.fixed_ips != vm['fixed_ips'] or p.status != 'ACTIVE' or s.status != 'ACTIVE' or
            (s.metadata or {}).get('ovn_migration_run') != self.cfg['run'] or
            (s.metadata or {}).get('ovn_validation_role') != vm['role'] or self.v.placement(vm, s, p)['status'] != 'PASS'):
            raise RuntimeError('Extended server/port/placement ownership changed')

    def create_matrix(self):
        if not self.matrix:
            return
        for key, vm in self.state['matrix'].items():
            peer = self.v.pair('measure')['1']['ip'] if key == '0' else None
            data = dict(write_files=[], runcmd=[])
            add_ping_files(data, self.ping_cfg(vm, peer))
            self.server(vm, data)

    def flows(self):
        a = self.v.pair('measure')
        x = self.state['matrix']
        return dict(zip(FLOW_NAMES, ((a['0'], x['0']), (x['0'], a['1']), (a['1'], x['1']))))

    def capture(self, name, pair):
        root = self.root/'flows'/name
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        cfg = dict(self.cfg, capture_directory=str(Path(self.cfg['capture_directory'])/name),
                   capture_role=pair[0].get('role', 'measure'))
        adapter = SimpleNamespace(root=root, cfg=cfg, cloud=self.cloud, pair=lambda _: {'0': pair[0], '1': pair[1]}, placement=self.v.placement)
        return Capture(adapter)

    def matrix_start(self):
        if not self.matrix:
            return
        self.matrix_boots(post=False)
        for name, pair in self.flows().items():
            cap = self.capture(name, pair)
            cap.start()
            cap.anchor()

    def matrix_ready(self):
        if not self.matrix:
            return
        self.matrix_boots(post=True)
        for name, pair in self.flows().items():
            cap = self.capture(name, pair)
            first = cap.snapshot().get('last_request_index', 0)
            deadline = time.monotonic()+self.cfg['timeout']
            while True:
                cp = cap.snapshot()
                if not cp.get('parse_problems') and cp.get('observed_endpoint', {}).get('index', 0) >= first+5:
                    break
                if time.monotonic() > deadline:
                    raise TimeoutError('Fresh EW matrix recovery unavailable: '+name)
                time.sleep(1)

    def console_rows(self, vm, prefix):
        output = self.v.console_output(vm['server'], time.monotonic()+self.cfg['timeout'])
        if prefix == 'OVN_NS_JSON ':
            from ns_icmp_logging import mirror_console
            mirror_console(self.root, output, self.cfg['run'])
        rows = []
        for line in output.splitlines():
            if prefix in line:
                try:
                    row = json.JSONDecoder().raw_decode(line.split(prefix, 1)[1])[0]
                    if row.get('run') == self.cfg['run']:
                        rows.append(row)
                except (ValueError, TypeError):
                    pass
        save(self.root/(vm['role']+'-extended-console.json'), rows)
        return rows

    def matrix_boots(self, post):
        if not self.matrix:
            return
        saved = read_evidence(self.root, 'matrix-boot-baseline.json')
        current = {}
        deadline = time.monotonic()+self.cfg['timeout']
        for key, vm in self.state['matrix'].items():
            self.audit(vm)
            while True:
                rows = self.console_rows(vm, 'OVN_AUX_JSON ')
                if rows:
                    anchor = rows[-1]
                    break
                if time.monotonic() > deadline:
                    raise TimeoutError('Matrix guest heartbeat unavailable')
                time.sleep(2)
            time.sleep(2)
            row = self.console_rows(vm, 'OVN_AUX_JSON ')[-1]
            if row['boot'] != anchor['boot'] or row['mono'] <= anchor['mono'] or not row['ping_alive']:
                raise RuntimeError('Matrix heartbeat/persistent ping not fresh')
            current[key] = dict(boot=row['boot'], server=vm['server'], port=vm['port'], fixed_ips=vm['fixed_ips'])
        if saved and current != saved:
            raise RuntimeError('Matrix endpoint rebooted or replaced')
        if post and not saved:
            raise RuntimeError('Missing matrix boot baseline')
        if not saved:
            save(self.root/'matrix-boot-baseline.json', current)

    def matrix_finish(self):
        if not self.matrix:
            return
        boot_error = None
        try:
            self.matrix_boots(post=True)
            primary = read_evidence(self.root, 'measure-post-checks.json').get('guests', {})
            if set(primary) != {'0', '1'} or any(row.get('boot_continuity') != 'PASS' for row in primary.values()):
                raise RuntimeError('Shared measurement endpoints lack boot continuity evidence')
        except Exception as exc:
            boot_error = str(exc)
        results = {}
        for name, pair in self.flows().items():
            try:
                cap = self.capture(name, pair)
                result = cap.finish()
                result.update(measurement_workload=name, measurement_guest=pair[0]['record_vm'],
                    boot_continuity='FAIL' if boot_error else 'PASS', source=pair[0], destination=pair[1])
                result.pop('pair_a_boot_continuity', None)
                if boot_error:
                    result.update(status='UNAVAILABLE', reason=boot_error)
                save(cap.v.root/'tenant-dataplane-probe.json', result)
                results[name] = result
            except Exception as exc:
                results[name] = dict(status='UNAVAILABLE', reason=str(exc))
        save(self.root/'ew-matrix-results.json', dict(status='PASS' if all(r['status']=='PASS' for r in results.values()) else 'UNAVAILABLE', flows=results))
        if any(r['status'] != 'PASS' for r in results.values()):
            raise RuntimeError('EW matrix evidence incomplete')

    def ns_config(self, **fields):
        cfg = dict(run=self.cfg['run'], port=self.cfg['ns']['port'],
            interval=self.cfg['tcp_interval'], timeout=self.cfg['tcp_timeout'],
            lifetime=self.cfg['lifetime'], **fields)
        ns = self.cfg['ns']
        if ns.get('new_port'):
            cfg.update(new_port=ns['new_port'], control_port=ns['control_port'],
                control_token=self.state['ns']['control_token'])
            if fields.get('label'):
                cfg.update(icmp_enabled=ns.get('icmp_enabled', True),
                    icmp_interval=self.cfg.get('interval', .2))
                cfg['icmp_output'] = (str(self.root/'ns/fip-icmp-sender.jsonl') if fields['label'] == 'fip'
                    else '/var/lib/ovn-migration-icmp/'+self.cfg['run']+'/snat-icmp-sender.jsonl')
                if fields['label'] == 'snat' and ns.get('icmp_sender_export_enabled'):
                    cfg['icmp_export_enabled'] = True
            if fields.get('server') == 'external-endpoint' and ns.get('icmp_sender_export_enabled'):
                cfg['icmp_receive_path'] = str(self.root/'ns/snat-icmp-sender.full.jsonl')
            if fields.get('label') == 'fip':
                cfg['control_bind'] = '127.0.0.1'
        return cfg

    def local_process(self, role, action, cfg):
        journal = self.state.setdefault('processes', {})
        if role in journal:
            cp = journal[role]
            if cp.get('config') != cfg or not alive(cp.get('identity')):
                raise RuntimeError('N-S endpoint process ended/changed; restart prohibited')
            return
        directory = self.root/'ns'
        directory.mkdir(mode=0o700, exist_ok=True)
        config = directory/(role+'.json')
        save(config, cfg)
        journal[role] = dict(status='START_INTENT', config=cfg)
        self.commit()
        with (directory/(role+'.log')).open('ab') as log:
            process = subprocess.Popen(['python3', str(Path(__file__).parent/'ns_probe.py'), action, str(config)],
                stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
        journal[role].update(status='RUNNING', identity=identity(process.pid))
        self.commit()

    def add_ns_guest(self, vm, action, cfg):
        unit = '[Unit]\nAfter=network-online.target\n[Service]\nRestart=no\nExecStart=/usr/bin/python3 /usr/local/bin/ns-probe.py '+action+' /etc/ns-probe.json\n[Install]\nWantedBy=multi-user.target\n'
        data = dict(write_files=[
            dict(path='/usr/local/bin/ns-probe.py', content=(Path(__file__).parent/'ns_probe.py').read_text(), permissions='0700'),
            dict(path='/etc/ns-probe.json', content=json.dumps(cfg), permissions='0600'),
            dict(path='/etc/systemd/system/ns-probe.service', content=unit)],
            runcmd=[['systemctl', 'enable', '--now', 'ns-probe']])
        if cfg.get('icmp_enabled'):
            data['write_files'].append(dict(path='/usr/local/bin/ns_icmp.py',
                content=(Path(__file__).parent/'ns_icmp.py').read_text(), permissions='0700'))
        if cfg.get('icmp_export_enabled'):
            data['write_files'].append(dict(path='/usr/local/bin/ns_log_transfer.py',
                content=(Path(__file__).parent/'ns_log_transfer.py').read_text(), permissions='0700'))
        self.server(vm, data)

    def create_ns(self):
        if not self.ns:
            return
        cfg = self.cfg['ns']
        if cfg.get('new_port'):
            ports = [cfg['port'], cfg['new_port'], cfg['control_port']]
            if len(set(ports)) != 3 or any(type(port) is not int or not 1 <= port <= 65535 for port in ports):
                raise ValueError('N-S old, new and control ports must be distinct valid TCP ports')
        if cfg.get('icmp_enabled') and not cfg.get('diagnostics_enabled'):
            raise ValueError('N-S ICMP requires sender-side packet capture diagnostics')
        s = self.state.setdefault('ns', {})
        if cfg.get('new_port') and not s.get('control_token'):
            s['control_token'] = uuid.uuid4().hex
            self.commit()
        prefix = self.cfg['prefix']+'-'+self.cfg['run']+'-ns'
        shared = cfg.get('shared_external')
        if shared:
            from shared_external import resolve, require_free_ips
            ext, ext_sub = resolve(self.cloud.network, shared)
            if (s.get('external_network', ext.id) != ext.id or s.get('external_subnet', ext_sub.id) != ext_sub.id
                    or s.get('external_owned', False) is not False):
                raise RuntimeError('Shared external workload checkpoint identity changed')
            require_free_ips(self.cloud.network, shared, router=s.get('router'),
                floating_ip=s.get('floating_ip'), port=s.get('vms', {}).get('1', {}).get('port'),
                marker='ovn-migration-run='+self.cfg['run'])
            s.update(external_network=ext.id, external_subnet=ext_sub.id, external_owned=False)
            self.commit()
        else:
            if s.get('external_owned') is False:
                raise RuntimeError('Shared external cannot become run-owned on resume')
            ext = self.resource(s, 'external_network', 'network', prefix+'-external',
                is_router_external=True, provider_network_type='flat', provider_physical_network=cfg['physnet'], mtu=cfg['mtu'])
            if ext.provider_network_type != 'flat' or ext.provider_physical_network != cfg['physnet'] or ext.mtu != cfg['mtu']:
                raise RuntimeError('N-S external network must retain configured flat physnet/MTU')
            ext_sub = self.resource(s, 'external_subnet', 'subnet', prefix+'-external', network_id=ext.id,
                cidr=cfg['external_cidr'], ip_version=4, gateway_ip=cfg['external_gateway'], is_dhcp_enabled=False,
                allocation_pools=[dict(start=cfg['snat_ip'], end=cfg['fip_ip'])])
            s['external_owned'] = True
            self.commit()
        net = self.resource(s, 'network', 'network', prefix+'-tenant')
        sub = self.resource(s, 'subnet', 'subnet', prefix+'-tenant', network_id=net.id, cidr=cfg['tenant_cidr'],
                            ip_version=4, is_dhcp_enabled=True)
        sg = self.resource(s, 'security_group', 'security_group', prefix+'-security')
        rules = list(self.cloud.network.security_group_rules(security_group_id=sg.id))
        allowed = [('icmp', None, cfg['endpoint_ip']+'/32'), ('tcp', cfg['port'], cfg['endpoint_ip']+'/32')]
        if cfg.get('new_port'):
            gateway = str(ipaddress.ip_network(cfg['tenant_cidr']).network_address+1)
            allowed += [('tcp', cfg['new_port'], cfg['endpoint_ip']+'/32'),
                        ('tcp', cfg['control_port'], gateway+'/32')]
        for protocol, port, remote in allowed:
            if not any(r.direction == 'ingress' and r.protocol == protocol and r.ether_type == 'IPv4' and
                       r.remote_ip_prefix == remote and (port is None or r.port_range_min == r.port_range_max == port) for r in rules):
                attrs = dict(security_group_id=sg.id, direction='ingress', ether_type='IPv4', protocol=protocol, remote_ip_prefix=remote)
                if port:
                    attrs.update(port_range_min=port, port_range_max=port)
                self.cloud.network.create_security_group_rule(**attrs)
        router = self.resource(s, 'router', 'router', prefix+'-router')
        existing = list(self.cloud.network.ports(device_id=router.id, network_id=net.id))
        if not existing:
            self.cloud.network.add_interface_to_router(router.id, subnet_id=sub.id)
        gateway = dict(network_id=ext.id, enable_snat=True,
                       external_fixed_ips=[dict(subnet_id=ext_sub.id, ip_address=cfg['snat_ip'])])
        current = self.cloud.network.get_router(router.id).external_gateway_info
        if not current:
            self.cloud.network.update_router(router.id, external_gateway_info=gateway)
        elif any(current.get(k) != val for k, val in gateway.items()):
            raise RuntimeError('Router external gateway changed; refusing reassignment')
        s['interface'] = True
        self.commit()
        vms = s.setdefault('vms', {})
        for key, role, ip in [('0', 'ns-snat-outbound', cfg['outbound_ip']), ('1', 'ns-fip-inbound', cfg['inbound_ip'])]:
            self.port(vms, key, net.id, sub.id, role, self.cfg['placement'][key], sg.id, ip)
        vm = vms['1']
        if not s.get('floating_ip'):
            candidates = list(self.cloud.network.ips(floating_ip_address=cfg['fip_ip']))
            marker = 'ovn-migration-run='+self.cfg['run']
            if len(candidates) > 1 or candidates and (candidates[0].description != marker or candidates[0].port_id != vm['port']):
                raise RuntimeError('Reserved FIP already allocated to another owner')
            fip = candidates[0] if candidates else self.cloud.network.create_ip(floating_network_id=ext.id,
                floating_ip_address=cfg['fip_ip'], port_id=vm['port'], fixed_ip_address=vm['ip'], description=marker)
            s['floating_ip'] = fip.id
            self.commit()
        directory = self.root/'ns'
        directory.mkdir(mode=0o700, exist_ok=True)
        from ns_diagnostics import Diagnostics
        diagnostics = Diagnostics(self)
        # Endpoint capture starts before either N-S client creates a session.
        diagnostics.start('endpoint')
        self.local_process('snat-server', 'serve', self.ns_config(bind=cfg['endpoint_ip'], server='external-endpoint',
            console=False, output=str(directory/'snat-server.jsonl')))
        deadline = time.monotonic()+self.cfg['timeout']
        while not (directory/'snat-server.jsonl').exists():
            if time.monotonic() > deadline:
                raise RuntimeError('Cannot bind external endpoint address/port on controller')
            time.sleep(1)
        self.add_ns_guest(vms['1'], 'serve', self.ns_config(bind=vms['1']['ip'], server='instance-id'))
        self.add_ns_guest(vms['0'], 'client', self.ns_config(source=vms['0']['ip'], target=cfg['endpoint_ip'],
            expected_peer=cfg['snat_ip'], expected_server='external-endpoint', label='snat'))
        deadline = time.monotonic()+self.cfg['timeout']
        while not self.console_rows(vms['1'], 'OVN_NS_JSON '):
            if time.monotonic() > deadline:
                raise TimeoutError('FIP listener startup unavailable')
            time.sleep(2)
        self.local_process('fip-client', 'client', self.ns_config(source=cfg['endpoint_ip'], target=cfg['fip_ip'],
            expected_peer=cfg['endpoint_ip'], expected_server=vms['1']['server'], label='fip',
            console=False, output=str(directory/'fip-client.jsonl')))
        # Exact Neutron UUIDs and placement are audited before selecting taps.
        diagnostics.start('guest-snat')
        diagnostics.start('guest-fip')
        # Diagnostic evidence identifies the MACs that an outer whitelist must permit.
        ports = list(self.cloud.network.ports(device_id=router.id, network_id=ext.id))
        save(self.root/'ns-outer-policy.json', dict(router_uuid=router.id,
            gateway_ports=[dict(id=p.id, mac=p.mac_address, fixed_ips=p.fixed_ips) for p in ports],
            required_ips=[cfg['snat_ip'], cfg['fip_ip']], scope='Reachability gate validates the mentor-managed outer policy'))

    def ns_control(self, action):
        if not self.ns or not self.cfg['ns'].get('new_port'):
            return {}
        from ns_control import request
        state, ns = self.state['ns'], self.cfg['ns']
        payload = dict(run=self.cfg['run'], action=action, token=state['control_token'],
            ip=state['vms']['0']['ip'], port=ns['control_port'], router=state['router'])
        ack_path = self.root/('ns-control-'+action.lower()+'.json')
        extra = self.root/'ns-control-transport.json'
        save(extra, dict(ns_control_payload=payload, ns_control_host=self.cfg['pair_d_network_host'],
            ns_control_ack_path=str(ack_path)))
        helper = Path(__file__).parent.parent/'playbooks/ns-control-tasks.yml'
        result = subprocess.run(['ansible-playbook', '-i', self.cfg['inventory'], str(helper),
            '-e', '@'+str(extra)], capture_output=True, text=True, timeout=self.cfg['timeout'])
        (self.root/('ns-control-'+action.lower()+'.log')).write_text(result.stdout+'\n'+result.stderr)
        if result.returncode:
            raise RuntimeError('N-S control failed; inspect ns-control log')
        snat = read_evidence(self.root, ack_path.name)
        fip = request(dict(payload, ip='127.0.0.1', router=None))
        rows = dict(snat=snat, fip=fip)
        baseline = read_evidence(self.root, 'ns-baseline.json')
        for direction, row in rows.items():
            original = baseline['clients'][direction]
            if (row.get('probe_id') != original['probe_id'] or row.get('boot') != original['boot'] or
                row.get('held_session') != original['streams']['held']['first_session'] or
                row.get('connection_attempts') != original['streams']['held']['connection_attempts']):
                raise RuntimeError('N-S control replaced the original held session')
        return rows

    def ns_arm(self):
        if not self.ns or not self.cfg['ns'].get('new_port'):
            return
        metrics = self.root/'metrics'
        if not (metrics/'control_plane_downtime.start').exists() or (metrics/'control_plane_downtime.end').exists():
            raise RuntimeError('N-S listeners can open only after Neutron API freeze')
        path = self.root/'ns-arm.json'
        previous = read_evidence(self.root, path.name)
        if previous.get('status') == 'PASS':
            return  # Never reset socket identities or measurement epoch on retry.
        save(path, dict(status='INTENT', run=self.cfg['run'], requested_ts=time.time()))
        clients = self.ns_control('ARM')
        save(path, dict(status='ARMED', run=self.cfg['run'], clients=clients))
        deadline = time.monotonic()+self.cfg['timeout']
        while time.monotonic() < deadline:
            rows = self.ns_rows()  # Nova console/local file only; no Neutron API query.
            ready = True
            for direction, row in rows.items():
                stats = row.get('streams', {}).get('new_listener', {})
                expected = clients[direction]
                held = row.get('streams', {}).get('held', {})
                ready = ready and (row.get('armed_mono') == expected['armed_mono'] and
                    stats.get('consecutive_successes', 0) >= 3 and
                    (stats.get('last_session') or {}).get('listener') == expected['listener_ack']['listener'] and
                    held.get('first_session') == expected['held_session'] and not held.get('held_broken', True))
            if ready:
                save(path, dict(status='PASS', run=self.cfg['run'], clients=clients,
                    ready_ts=time.time(), evidence=rows))
                return
            time.sleep(.5)
        raise RuntimeError('N-S new listener failed readiness before dataplane cutover')

    def ns_snapshot(self):
        s = self.state['ns']
        cfg = self.cfg['ns']
        net = self.cloud.network.get_network(s['network'])
        ext = self.cloud.network.get_network(s['external_network'])
        sub = self.cloud.network.get_subnet(s['external_subnet'])
        router = self.cloud.network.get_router(s['router'])
        fip = self.cloud.network.get_ip(s['floating_ip'])
        gateway_ports = list(self.cloud.network.ports(device_id=router.id, network_id=ext.id))
        if len(gateway_ports) != 1 or gateway_ports[0].fixed_ips != [dict(subnet_id=sub.id, ip_address=cfg['snat_ip'])]:
            raise RuntimeError('External gateway port identity/address is missing or ambiguous')
        for vm in s['vms'].values():
            self.audit(vm)
        expected = dict(network_id=ext.id, enable_snat=True,
                        external_fixed_ips=[dict(subnet_id=sub.id, ip_address=cfg['snat_ip'])])
        if any(router.external_gateway_info.get(k) != v for k, v in expected.items()) or (
            ext.provider_network_type != 'flat' or ext.provider_physical_network != cfg['physnet'] or
            ext.mtu != cfg['mtu'] or sub.cidr != cfg['external_cidr'] or sub.is_dhcp_enabled or
            fip.floating_ip_address != cfg['fip_ip'] or fip.port_id != s['vms']['1']['port'] or
            fip.fixed_ip_address != s['vms']['1']['ip'] or fip.router_id != router.id or
            fip.floating_network_id != ext.id or fip.status != 'ACTIVE'):
            raise RuntimeError('N-S flat network, gateway, SNAT or FIP mapping changed')
        return dict(tenant_type=net.provider_network_type, external_type=ext.provider_network_type,
            external_mtu=ext.mtu, ids={k:s[k] for k in ('network', 'subnet', 'external_network', 'external_subnet', 'router', 'floating_ip')},
            gateway_ports=[dict(id=p.id, mac=p.mac_address, fixed_ips=p.fixed_ips) for p in gateway_ports],
            gateway=expected, fip=dict(id=fip.id, floating_network_id=fip.floating_network_id,
            port_id=fip.port_id, fixed_ip_address=fip.fixed_ip_address, floating_ip_address=fip.floating_ip_address),
            vms={k:{f:vm[f] for f in ('server', 'port', 'fixed_ips', 'network', 'subnet', 'placement')} for k, vm in s['vms'].items()})

    def ns_rows(self):
        from ns_probe import PREFIX
        guest = self.console_rows(self.state['ns']['vms']['0'], PREFIX)
        local_path = self.root/'ns/fip-client.jsonl'
        local = []
        if local_path.exists():
            for line in local_path.read_text().splitlines():
                try:
                    local.append(json.loads(line.split(PREFIX, 1)[1]))
                except (ValueError, IndexError):
                    pass
        return dict(snat=guest[-1] if guest else {}, fip=local[-1] if local else {})

    def ns_wait(self, post=False):
        if not self.ns:
            return
        initial = self.ns_rows()
        baseline = read_evidence(self.root, 'ns-baseline.json')
        deadline = time.monotonic()+self.cfg['timeout']
        while True:
            rows = self.ns_rows()
            snapshot = self.ns_snapshot()
            errors = []
            for role, cp in self.state['processes'].items():
                if not alive(cp.get('identity')):
                    errors.append('External '+role+' process ended')
            for direction, row in rows.items():
                prior = baseline.get('clients', {}).get(direction, {})
                if not row or row.get('mono', 0) <= initial[direction].get('mono', 0):
                    errors.append(direction+' client summary not fresh')
                    continue
                if prior and any(row.get(k) != prior.get(k) for k in ('boot', 'probe_id')):
                    raise RuntimeError(direction+' client rebooted/restarted')
                expected_streams = {'held', 'new_old', 'new_listener'} if self.cfg['ns'].get('new_port') else {'held', 'new'}
                if set(row.get('streams', {})) != expected_streams:
                    errors.append(direction+'/probe schema mismatch')
                    continue
                for stream, stats in row['streams'].items():
                    if stream == 'new_listener' and not post:
                        if row.get('armed') or stats['attempts']:
                            raise RuntimeError('N-S additional listener was armed before API freeze')
                        continue
                    if stats['held_broken'] and not post:
                        raise RuntimeError(direction+' held socket closed; reconnect prohibited')
                    if stats['consecutive_successes'] < 5 or stats['open_failure_since'] is not None:
                        errors.append(direction+'/'+stream+' not recovered')
                    if not stats['first_session'] or stats['observed_peer_ips'] != [self.cfg['ns']['snat_ip'] if direction == 'snat' else self.cfg['ns']['endpoint_ip']]:
                        errors.append(direction+'/'+stream+' NAT/source identity not proven')
                    if direction == 'snat' and stream == 'held' and self.cfg['ns'].get('preserve_snat_source_port') and stats['first_session']:
                        port_status = snat_source_port_status(stats)
                        if port_status != 'PASS':
                            message = ('SNAT held source-port experiment '+port_status+
                                ': guest='+str(stats.get('first_local_endpoint'))+
                                ', translated='+str(stats.get('first_peer_endpoint')))
                            if not post:
                                raise RuntimeError(message+'; migration blocked before freeze. '
                                    'Source use_random_fully=false must be active before creating the session.')
                            errors.append(message)
                    if stream == 'held' and stats['first_session'] != stats['last_session'] and not post:
                        raise RuntimeError('Held session identity changed')
                    if prior:
                        before = prior['streams'].get(stream, {})
                        if stream == 'held' and (stats['first_session'] != before['first_session'] or stats['connection_attempts'] != before['connection_attempts']) and not post:
                            raise RuntimeError('Held session replaced or reconnected')
                        if stats['last_session'] and before.get('last_session') and any(stats['last_session'][k] != before['last_session'][k] for k in ('server', 'server_boot', 'listener')):
                            raise RuntimeError('N-S server listener restarted')
                if self.cfg['ns'].get('icmp_enabled'):
                    ping = row.get('icmp') or {}
                    if not ping.get('alive') or ping.get('replies', 0) < 5:
                        errors.append(direction+'/ICMP ping health unavailable')
                    if self.cfg['ns'].get('icmp_sender_logging_enabled') and (not ping.get('log_id') or ping.get('log_error')):
                        errors.append(direction+'/ICMP sender logging unavailable: '+str(ping.get('log_error')))
                    # Guest monotonic is a different clock; compare within its own summary.
                    if row.get('mono', 0)-(ping.get('last_reply_mono') or 0) > 3:
                        errors.append(direction+'/ICMP not recovered')
                if post and self.cfg['ns'].get('new_port'):
                    arm = read_evidence(self.root, 'ns-arm.json').get('clients', {}).get(direction, {})
                    if not arm or row.get('armed_mono') != arm.get('armed_mono'):
                        errors.append(direction+'/ARM continuity unavailable')
                    expected_listener = (arm.get('listener_ack') or {}).get('listener')
                    if (row.get('streams', {}).get('new_listener', {}).get('last_session') or {}).get('listener') != expected_listener:
                        errors.append(direction+'/new listener identity changed')
            before = baseline.get('resources')
            if before and any(snapshot.get(k) != before.get(k) for k in snapshot if k != 'tenant_type'):
                raise RuntimeError('N-S resource preservation failed')
            if snapshot['tenant_type'] != ('geneve' if post else 'vxlan'):
                errors.append('Unexpected N-S tenant network type')
            if post and not baseline:
                raise RuntimeError('N-S baseline missing; cannot validate held session across migration')
            if not errors:
                if not baseline:
                    save(self.root/'ns-baseline.json', dict(status='PASS', clients=rows, resources=snapshot))
                if post:
                    self.ns_results(rows, snapshot, baseline)
                else:
                    save(self.root/'ns-precutover.json', dict(status='PASS', clients=rows, resources=snapshot))
                return
            if time.monotonic() > deadline:
                if post:
                    self.ns_results(rows, snapshot, baseline, errors)
                raise TimeoutError('N-S baseline/recovery failed; check outer policy and ns logs: '+', '.join(errors))
            time.sleep(2)

    def ns_results(self, rows, snapshot, baseline, errors=None):
        results = {}
        for direction, row in rows.items():
            old_row = baseline['clients'][direction]
            continuity = all(row.get(k) == old_row.get(k) for k in ('boot', 'probe_id'))
            streams = {}
            for name, stats in row.get('streams', {}).items():
                old = old_row['streams'].get(name, dict(attempts=0, failures=0, first_session=None, last_session=None, connection_attempts=0))
                attempts = stats['attempts']-old['attempts'] if continuity else None
                failed = stats['failures']-old['failures'] if continuity else None
                bursts = [b for b in stats['recovered_failure_bursts'] if b['start_mono'] >= old_row['mono']] if continuity else []
                held = (not stats['held_broken'] and stats['first_session'] == old['first_session'] == stats['last_session'] and
                        stats['connection_attempts'] == old['connection_attempts']) if name == 'held' else True
                port_status = snat_source_port_status(stats)
                if direction == 'snat' and name == 'held' and self.cfg['ns'].get('preserve_snat_source_port'):
                    held = held and snat_source_port_status(old) == 'PASS' and port_status == 'PASS'
                ready = continuity and held and attempts and attempts > 0 and stats['consecutive_successes'] >= 5 and stats['open_failure_since'] is None
                measured = continuity and attempts is not None and attempts > 0
                streams[name] = dict(status='PASS' if ready else 'FAIL', attempts=attempts, failures=failed,
                    failed_probe_percent=100*failed/attempts if attempts else None,
                    longest_recovered_failure_seconds=max((b['duration_seconds'] for b in bursts), default=0.0) if measured and stats['open_failure_since'] is None else None,
                    recovered_failure_bursts=bursts, open_failure_since=stats['open_failure_since'],
                    total_downtime_seconds=sum(b['duration_seconds'] for b in bursts) if measured and stats['open_failure_since'] is None else None,
                    start_timestamp=(row.get('armed_ts') if name == 'new_listener' else old_row.get('ts')),
                    end_timestamp=row.get('ts'),
                    first_failure_timestamp=bursts[0].get('start_timestamp') if bursts else stats.get('open_failure_ts'),
                    recovery_timestamp=bursts[0].get('recovery_timestamp') if bursts else None,
                    failure_burst_count=(len(bursts) + int(stats['open_failure_since'] is not None
                        and stats['open_failure_since'] >= old_row['mono'])) if measured else None,
                    terminal_error=stats['terminal_error'], first_session=old['last_session'] or stats['first_session'], last_session=stats['last_session'],
                    held_broken=stats['held_broken'], terminal_event=stats.get('terminal_event'),
                    successful_echo_source_port_match=port_status,
                    **{key:stats.get(key) for key in ('first_local_endpoint', 'last_local_endpoint',
                        'first_peer_endpoint', 'last_peer_endpoint')},
                    observed_peer_ips=stats['observed_peer_ips'], connection_attempts=stats['connection_attempts'])
            results[direction] = dict(status='PASS' if len(streams)==(3 if self.cfg['ns'].get('new_port') else 2) and all(s['status']=='PASS' for s in streams.values()) else 'FAIL',
                held_session_preservation=streams.get('held', {}).get('status', 'UNAVAILABLE'),
                boot_continuity='PASS' if continuity else 'FAIL', start_mono=old_row['mono'], end_mono=row.get('mono'), streams=streams,
                icmp_health=row.get('icmp'), armed_mono=row.get('armed_mono'), listener_ack=row.get('listener_ack'))
        save(self.root/'ns-results.json', dict(status='PASS' if not errors and all(r['status']=='PASS' for r in results.values()) else 'FAIL',
            reason='; '.join(errors or []), directions=results, resources=snapshot,
            scope='Fresh pre-migration baseline summary to final recovered summary, client monotonic application probes; no held reconnect', baseline=baseline,
            source_snat_profile=dict(preserve_snat_source_port_requested=bool(self.cfg['ns'].get('preserve_snat_source_port')),
                conntrack_transfer=False, port_evidence_scope='First/last successful echoes only; a reset held socket still fails')))

    def cleanup(self):
        """Delete only checkpointed UUIDs; matrix has no shared network ownership."""
        if self.state.get('cleaned'):
            return
        ns = self.state.get('ns', {})
        if ns.get('external_owned') is False:
            from shared_external import resolve
            shared = self.cfg['ns'].get('shared_external')
            if not shared:
                raise RuntimeError('Missing shared external ownership checkpoint during cleanup')
            ext, sub = resolve(self.cloud.network, shared)
            if (ns['external_network'], ns['external_subnet']) != (ext.id, sub.id):
                raise RuntimeError('Shared external cleanup checkpoint identity changed')
        import os
        import signal
        for cp in self.state.get('processes', {}).values():
            if alive(cp.get('identity')):
                os.kill(cp['identity']['pid'], signal.SIGTERM)
        groups = [self.state.get('matrix', {}), self.state.get('ns', {}).get('vms', {})]
        for group in groups:
            for vm in group.values():
                s = self.cloud.compute.find_server(vm['server'])
                if s:
                    self.audit(vm)
                    self.cloud.compute.delete_server(vm['server'])
                    deadline = time.monotonic()+self.cfg['timeout']
                    while self.cloud.compute.find_server(vm['server']):
                        if time.monotonic() > deadline:
                            raise TimeoutError('Extended server deletion timeout')
                        time.sleep(2)
                p = self.cloud.network.find_port(vm['port'])
                if p:
                    if p.device_id or p.fixed_ips != vm['fixed_ips']:
                        raise RuntimeError('Extended port cleanup identity changed')
                    self.cloud.network.delete_port(vm['port'])
        ns = self.state.get('ns', {})
        if ns:
            fip = self.cloud.network.find_ip(ns['floating_ip'])
            if fip and fip.description != 'ovn-migration-run='+self.cfg['run']:
                raise RuntimeError('Floating IP cleanup ownership changed')
            self.cloud.network.delete_ip(ns['floating_ip'], ignore_missing=True)
            r = self.cloud.network.find_router(ns['router'])
            if r:
                if r.description != 'ovn-migration-run='+self.cfg['run']:
                    raise RuntimeError('Router cleanup ownership changed')
                self.cloud.network.update_router(r.id, external_gateway_info={})
                if list(self.cloud.network.ports(device_id=r.id, network_id=ns['network'])):
                    self.cloud.network.remove_interface_from_router(r.id, subnet_id=ns['subnet'])
                self.cloud.network.delete_router(r.id)
            for key, kind in [('subnet', 'subnet'), ('network', 'network'), ('external_subnet', 'subnet'),
                              ('external_network', 'network'), ('security_group', 'security_group')]:
                if key in ('external_network', 'external_subnet') and ns.get('external_owned') is False:
                    continue
                obj = getattr(self.cloud.network, 'find_'+kind)(ns[key])
                if obj and obj.description != 'ovn-migration-run='+self.cfg['run']:
                    raise RuntimeError(kind+' cleanup ownership changed')
                getattr(self.cloud.network, 'delete_'+kind)(ns[key], ignore_missing=True)
        self.state['cleaned'] = True
        self.commit()
        save(self.root/'extended-cleanup.json', dict(status='PASS',
            retained_shared_external={k: ns[k] for k in ('external_network','external_subnet')}
                if ns.get('external_owned') is False else {}))
