#!/usr/bin/env python3
"""POC validation orchestration; state is checkpointed after each owned creation."""
import argparse
import base64
import fcntl
import json
import math
import os
import pathlib
import signal
import re
import ipaddress
import subprocess
from dataplane_capture import Capture
import sys
import time

PREFIX = 'OVN_MIGRATION_JSON '
ROLES = {'measure': ('pre', 'measure'), 'pre': ('pre', 'existing'), 'post': ('post', 'fresh')}

def save(path, value):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True))
    os.chmod(tmp, 0o600)
    tmp.replace(path)

def records(text, run, vm):
    out = []
    for line in text.splitlines():
        if PREFIX not in line:
            continue
        try:
            item = json.JSONDecoder().raw_decode(line.split(PREFIX, 1)[1])[0]
            if isinstance(item, dict) and item.get('run') == run and item.get('vm') == vm:
                out.append(item)
        except (ValueError, TypeError):
            pass
    return out

def valid_marker(row):
    return (isinstance(row, dict) and isinstance(row.get('boot'), str) and bool(row['boot'])
            and type(row.get('seq')) is int and row['seq'] > 0)


def valid_packet(row):
    return (valid_marker(row) and row.get('kind') == 'packet' and
            type(row.get('success')) is bool and type(row.get('ts')) in (int, float) and
            math.isfinite(row['ts']) and type(row.get('mono', row['ts'])) in (int, float) and
            math.isfinite(row.get('mono', row['ts'])))


def sequence_anchor(rows):
    """Most recently observed packet boot and highest completed sequence on it."""
    packets = [r for r in rows if valid_packet(r)]
    if not packets:
        return None
    # rows retain console observation order, independent of guest wall clocks.
    boot = packets[-1]['boot']
    return {'boot': boot, 'seq': max(r['seq'] for r in packets if r['boot'] == boot)}


def freshness_anchor(rows):
    anchor = sequence_anchor(rows)
    if anchor:
        # Also fence out a health result associated with an in-flight packet.
        anchor['seq'] = max(r['seq'] for r in rows if valid_marker(r) and r['boot'] == anchor['boot'])
    return anchor


def latest_health(rows, anchor, interval):
    """Fence by boot/sequence; require health age within ten guest monotonic seconds."""
    current = sequence_anchor(rows)
    if not anchor or not current or current['boot'] != anchor['boot']:
        return None
    packet = max((r for r in reversed(rows) if valid_marker(r) and r.get('kind') == 'packet' and
                  r['boot'] == current['boot']), key=lambda r:r['seq'])
    mono = packet.get('mono')
    if not valid_packet(packet) or type(mono) not in (int, float) or not math.isfinite(mono):
        return None
    candidates = [r for r in rows if r.get('kind') == 'health' and valid_marker(r) and
                  r['boot'] == anchor['boot'] and anchor['seq'] < r['seq'] <= current['seq'] and
                  type(r.get('mono')) in (int, float) and math.isfinite(r['mono']) and
                  0 <= mono-r['mono'] <= 10.0]
    return max(candidates, key=lambda r:r['seq']) if candidates else None


def dhcp_convergence(health, expected_mtu, metadata_ip):
    if not health or expected_mtu is None or metadata_ip is None:
        return 'UNAVAILABLE'
    return 'PASS' if (health.get('dhcp') is True and health.get('mtu') == expected_mtu and
                      health.get('metadata_gateway') == metadata_ip) else 'FAIL'


def renewal_flags(health, anchor_mono, cfg):
    health = health or {}
    ack, t1, t2, cadence = (health.get(k) for k in ('dhcp_last_ack_monotonic', 'dhcp_t1_seconds',
                                                   'dhcp_t2_seconds', 'dhcp_last_renewal_interval_seconds'))
    def number(value):
        return type(value) in (int,float) and math.isfinite(value)
    return dict(fresh_renewal=number(ack) and anchor_mono is not None and ack>anchor_mono,
        timers_sane=number(t1) and number(t2) and 0<t1<=cfg.get('dhcp_t1',30) and t1<t2<=cfg.get('dhcp_t2',60),
        short_renewal_cadence=number(cadence) and 0<cadence<=cfg.get('dhcp_t1',30)+cfg.get('dhcp_renewal_tolerance',5))


def metadata_port_ip(ports, subnet_id):
    # Count matching ports/allocations, not distinct IP strings: duplicate
    # ports claiming the same address are still ambiguous resource evidence.
    addresses = [fixed['ip_address'] for port in ports if port.device_owner == 'network:distributed'
                 for fixed in port.fixed_ips if fixed['subnet_id'] == subnet_id]
    return addresses[0] if len(addresses) == 1 else None


def ovn_dhcp_health(raw, vm, subnet, metadata_ip, cfg):
    """Unique exact LSP reference, SB up/chassis and matching subnet DHCP options."""
    result=dict(status='FAIL',metadata_ip=metadata_ip,raw=raw)
    try:
        bindings=raw['bindings']; lsps=raw['lsps']; options=raw['dhcp_options']
        if len(bindings)!=1 or len(lsps)!=1 or len(options)!=1 or metadata_ip is None:
            return result
        b,l,d=bindings[0],lsps[0],options[0]
        up=b.get('up'); up=up[0] if isinstance(up,list) and len(up)==1 else up
        refs=l.get('dhcpv4_options'); refs=refs if isinstance(refs,list) else [refs]
        o=d['options']; external=d['external_ids']
        routes=re.findall(r'169\.254\.169\.254/32\s*,\s*([0-9.]+)',o.get('classless_static_route',''))
        good=(raw.get('port')==vm['port'] and b['logical_port']==vm['port'] and bool(b['chassis']) and up is True and
              l['name']==vm['port'] and refs==[d['_uuid']] and external.get('subnet_id')==vm['subnet'] and
              external.get('port_id',vm['port'])==vm['port'] and
              ipaddress.ip_network(d['cidr'])==ipaddress.ip_network(subnet.cidr) and
              ipaddress.ip_address(vm['ip']) in ipaddress.ip_network(subnet.cidr) and subnet.is_dhcp_enabled is True and
              int(o['mtu'])==cfg.get('target_mtu',1442) and int(o['T1'])==cfg.get('dhcp_t1',30) and
              int(o['T2'])==cfg.get('dhcp_t2',60) and int(o['lease_time'])>int(o['T2']) and
              o.get('router')==subnet.gateway_ip and routes==[metadata_ip])
        result['status']='PASS' if good else 'FAIL'
    except (KeyError,ValueError,TypeError,AttributeError):
        pass
    return result


def guest_checks(rows, anchor, interval):
    """Check packet recovery and shared monotonic health freshness on the anchored boot."""
    result = {'connectivity': 'UNAVAILABLE', 'dhcp': 'UNAVAILABLE', 'metadata': 'UNAVAILABLE'}
    if not anchor or sequence_anchor(rows) is None:
        return result
    if sequence_anchor(rows)['boot'] != anchor['boot']:
        return result
    packets = {r['seq']: r for r in rows if valid_packet(r)
               and r['boot'] == anchor['boot'] and r['seq'] > anchor['seq']
               and type(r.get('success')) is bool}
    ordered = sorted(packets.values(), key=lambda r: r['seq'])
    if not ordered:
        return result
    newest = ordered[-1]['seq']
    tail = ordered[-5:]
    if len(tail) >= 5:
        result['connectivity'] = 'PASS' if (all(r['success'] for r in tail) and
            [r['seq'] for r in tail] == list(range(newest-4, newest+1))) else 'FAIL'
    latest = latest_health(rows, anchor, interval)
    if latest:
        for key in ('dhcp', 'metadata'):
            result[key] = 'PASS' if latest.get(key) is True else 'FAIL'
    return result


def probe_metrics(rows, start, end, interval):
    """Measure (start.seq, end.seq] on one boot; wall clocks never select rows."""
    result = dict(status='UNAVAILABLE', packets_attempted=None, packets_successful=None,
                  packets_failed=None, packet_loss_percent=None, failure_burst_count=None,
                  first_failure_timestamp=None, recovery_timestamp=None,
                  maximum_consecutive_failed_probes=None, longest_outage_start_timestamp=None,
                  longest_outage_recovery_timestamp=None, actual_dataplane_outage_seconds=None,
                  coverage_complete=False, start_anchor=start, end_anchor=end,
                  measurement='small-packet routed tenant dataplane; longest recovered loss burst in (start.seq, end.seq]',
                  measurement_workload='Pair A', measurement_guest='measure0', evidence_source='guest-console-secondary')
    if not start or not end or start['boot'] != end['boot'] or end['seq'] <= start['seq']:
        return result
    indexed = {}
    conflict = False
    reboot = False
    start_positions = [i for i,r in enumerate(rows) if valid_packet(r) and r['boot']==start['boot'] and r['seq']==start['seq']]
    end_positions = [i for i,r in enumerate(rows) if valid_packet(r) and r['boot']==end['boot'] and r['seq']==end['seq']]
    left = min(start_positions) if start_positions else -1
    right = max(end_positions) if end_positions else len(rows)
    for position,r in enumerate(rows):
        if not valid_packet(r):
            continue
        if r['boot'] != start['boot']:
            # Only a discontinuity inside the anchored interval invalidates
            # packet measurement; later health failures are independent.
            if left < position <= right:
                reboot = True
            continue
        if start['seq'] < r['seq'] <= end['seq']:
            if r['seq'] in indexed and indexed[r['seq']] != r:
                conflict = True
            indexed[r['seq']] = r
    ordered = [indexed[k] for k in sorted(indexed)]
    if not ordered:
        return result
    complete = (not conflict and not reboot and
                len(ordered) == end['seq']-start['seq'] and
                ordered[0]['seq'] == start['seq']+1 and ordered[-1]['seq'] == end['seq'] and
                all(type(r.get('success')) is bool for r in ordered) and
                all(b['seq'] == a['seq']+1 for a,b in zip(ordered, ordered[1:])))
    # Monotonic guest launch times survive wall-clock adjustments. Older guest
    # records may use epochs only if internally monotonic, never host epochs.
    def clock(r):
        return r.get('mono', r['ts'])
    boundary = [rows[start_positions[0]]] if start_positions else []
    timed = boundary + ordered
    timing_valid = all(0 < clock(b)-clock(a) < 3*interval and
                       0 < b['ts']-a['ts'] < 3*interval for a,b in zip(timed, timed[1:]))
    failed = sum(r.get('success') is False for r in ordered)
    bursts = []
    burst = None
    for row in ordered:
        if row.get('success') is False:
            if burst is None:
                burst = {'start_timestamp': row['ts'], 'start_monotonic': clock(row),
                         'start_sequence': row['seq'], 'failed_probes': 0,
                         'recovery_timestamp': None, 'recovery_sequence': None, 'duration_seconds': None}
            burst['failed_probes'] += 1
        elif row.get('success') is True and burst is not None:
            burst.update(recovery_timestamp=row['ts'], recovery_sequence=row['seq'],
                         duration_seconds=row['ts']-burst['start_timestamp'])
            bursts.append(burst)
            burst = None
    if burst is not None:
        bursts.append(burst)
    recovered = [b for b in bursts if b['recovery_timestamp'] is not None]
    longest = max(recovered, key=lambda b:b['duration_seconds']) if recovered else None
    valid = complete and timing_valid and burst is None
    result.update(status='PASS' if valid else 'UNAVAILABLE', packets_attempted=len(ordered),
                  packets_successful=sum(r.get('success') is True for r in ordered), packets_failed=failed,
                  packet_loss_percent=100*failed/len(ordered), failure_burst_count=len(bursts),
                  first_failure_timestamp=bursts[0]['start_timestamp'] if bursts else None,
                  recovery_timestamp=bursts[0]['recovery_timestamp'] if bursts else None,
                  maximum_consecutive_failed_probes=max((b['failed_probes'] for b in bursts), default=0),
                  longest_outage_start_timestamp=longest['start_timestamp'] if longest and valid else None,
                  longest_outage_recovery_timestamp=longest['recovery_timestamp'] if longest and valid else None,
                  actual_dataplane_outage_seconds=longest['duration_seconds'] if longest and valid else (0.0 if valid else None),
                  coverage_complete=complete, timing_valid=timing_valid, failure_bursts=bursts)
    return result


def read_evidence(root, name):
    path = root/name
    return json.loads(path.read_text()) if path.exists() else {}


def workload_pass(rows, network_type):
    keys = ('identity', 'active', 'bound', 'dhcp', 'connectivity', 'metadata')
    if network_type == 'geneve':
        keys += ('dhcp_availability', 'dhcp_convergence')
    return (set(rows) == {'0','1'} and all(row.get('network_type') == network_type and
            all(row.get(k) == 'PASS' for k in keys) for row in rows.values()))


def validation_ready(root):
    """Single fail-closed cleanup gate shared by normal execution and retries."""
    orchestration = read_evidence(root, 'validation-orchestration.json')
    consistency = read_evidence(root, 'resource-consistency.json')
    state = read_evidence(root, 'validation-resources.json')
    role_ready = (state.get('schema_version') != 2 or state.get('historical_dual_pair') is True or (
        read_evidence(root, 'measure-readiness.json').get('status') == 'PASS' and
        read_evidence(root, 'measure-post-checks.json').get('status') == 'PASS' and
        read_evidence(root, 'tenant-dataplane-probe.json').get('pair_a_boot_continuity') == 'PASS' and
        read_evidence(root, 'dhcp-precutover-preparation.json').get('status') == 'PASS' and
        read_evidence(root, 'existing-migration-baseline.json') and
        all(row.get('boot_continuity') == 'PASS' and row.get('mtu') == 'PASS'
            for row in read_evidence(root, 'pre-workload-checks.json').values()) and
        all(row.get('mtu') == 'PASS' for row in read_evidence(root, 'post-workload-checks.json').values())))
    cfg=read_evidence(root,'validation-config.json')
    if cfg.get('post_cutover_dhcp_enabled'):
        role_ready = role_ready and read_evidence(root,'existing-post-cutover-readiness.json').get('status')=='PASS'
    if cfg.get('capture_enabled'):
        role_ready = role_ready and read_evidence(root,'tenant-dataplane-probe.json').get('evidence_source')=='compute-tap-pcap'
    return (role_ready and workload_pass(read_evidence(root, 'initial-workload-checks.json'), 'vxlan') and
            workload_pass(read_evidence(root, 'pre-workload-checks.json'), 'geneve') and
            workload_pass(read_evidence(root, 'post-workload-checks.json'), 'geneve') and
            read_evidence(root, 'post-ovn-bindings.json').get('status') == 'PASS' and
            read_evidence(root, 'existing-network-semantics.json').get('status') == 'PASS' and
            read_evidence(root, 'tenant-dataplane-probe.json').get('status') == 'PASS' and
            orchestration.get('semantics_rc') == 0 and orchestration.get('workload_rc') == 0 and
            not read_evidence(root, 'workload-errors.json') and bool(consistency) and
            all(row.get('unchanged') is True for row in consistency.values()))

class Validation:
    def __init__(self, root):
        import openstack
        self.cloud = openstack.connect()
        self.root = root
        self.cfg = json.loads((root/'validation-config.json').read_text())
        self.path = root/'validation-resources.json'
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {}
        if not self.state:
            self.state['schema_version'] = 2
        elif self.state.get('schema_version') != 2:
            # Preserve old UUIDs and console labels. Never reinterpret the old
            # dual-purpose pre pair as an independent measurement workload.
            for stage, role in (('pre','existing'), ('post','fresh')):
                s = self.state.get(stage)
                if not s:
                    continue
                s['networks'] = {k: {x:s[k][x] for x in ('network','subnet','interface') if x in s[k]}
                                 for k in ('0','1') if k in s}
                s[role] = {k:s.pop(k) for k in ('0','1') if k in s}
                for k, vm in s[role].items():
                    vm['record_vm'] = stage+k
                    vm['owned'] = False  # old ownership cannot authorize reboot
            self.state.update(schema_version=2, historical_dual_pair=True)
            self.commit()
    def commit(self):
        save(self.path, self.state)
    def pair(self, stage):
        topology, role = ROLES[stage]
        s = self.state[topology]
        if role in s:
            return s[role]
        if stage != 'measure' and '0' in s:  # historical readers/tests
            return s
        raise RuntimeError(f'Missing {role} pair; resources preserved; no role substitution allowed')

    def create(self, stage):
        c, cfg = self.cloud, self.cfg
        topology, role = ROLES[stage]
        s = self.state.setdefault(topology, {})
        modern = self.state.get('schema_version') == 2
        if modern and stage=='post' and any(self.cloud.network.get_network(vm['network']).provider_network_type!='geneve'
                                           for key,vm in self.pair('pre').items() if key in ('0','1')):
            raise RuntimeError('Pair C creation requires active OVN/Geneve tenant networks')
        pair = s.setdefault(role, {}) if modern else s
        networks = s.setdefault('networks', {}) if modern else s
        prefix = cfg['prefix'] + '-' + cfg['run'] + '-' + topology
        image = c.image.find_image(cfg['image'], ignore_missing=False)
        flavor = c.compute.find_flavor(cfg['flavor'], ignore_missing=False)
        # Before any writes, verify image and flavor resolution and API availability.
        if not s.get('security_group'):
            sg = c.network.create_security_group(name=prefix)
            s['security_group'] = sg.id
            self.commit()
        sg = s['security_group']
        rules = list(c.network.security_group_rules(security_group_id=sg))
        if not any(r.direction == 'ingress' and r.protocol == 'icmp' for r in rules):
            c.network.create_security_group_rule(security_group_id=sg, direction='ingress',
                                                ether_type='IPv4', protocol='icmp')
        if not s.get('router'):
            s['router'] = c.network.create_router(name=prefix).id
            self.commit()
        for i in range(2):
            net = networks.setdefault(str(i), {})
            name = prefix + '-network-' + str(i)
            if not net.get('network'):
                net['network'] = c.network.create_network(name=name).id
                self.commit()
            if not net.get('subnet'):
                net['subnet'] = c.network.create_subnet(name=name, network_id=net['network'],
                    ip_version=4, cidr=cfg['cidrs'][topology][i], enable_dhcp=True).id
                self.commit()
            if not net.get('interface'):
                existing = list(c.network.ports(device_id=s['router'], network_id=net['network']))
                if not existing:
                    c.network.add_interface_to_router(s['router'], subnet_id=net['subnet'])
                net['interface'] = True
                self.commit()
            vm = pair.setdefault(str(i), {})
            vm.update(network=net['network'], subnet=net['subnet'])
            name = prefix + '-' + role + str(i) if modern else prefix + '-' + str(i+1)
            vm.setdefault('record_vm', role+str(i) if modern else stage+str(i))
            vm.setdefault('name', name)
            if not vm.get('port'):
                if vm.get('server'):
                    raise RuntimeError('Checkpointed server has no explicit owned port; refusing duplicate/replacement port')
                candidates = [p for p in c.network.ports(network_id=vm['network'], name=name) if p.name == name]
                if len(candidates)>1 or (candidates and sg not in candidates[0].security_group_ids):
                    raise RuntimeError('Ambiguous owned validation port; refusing duplicate creation')
                port = candidates[0] if candidates else c.network.create_port(name=name, network_id=vm['network'],
                                                                             security_group_ids=[sg])
                vm.update(port=port.id, fixed_ips=port.fixed_ips, ip=port.fixed_ips[0]['ip_address'], owned=True)
                self.commit()
        if stage == 'pre':
            for i in range(2):
                vm = pair[str(i)]
                self.cloud.network.update_port(vm['port'], extra_dhcp_opts=[
                    {'opt_name': '58', 'opt_value': str(cfg.get('dhcp_t1', 30)), 'ip_version': 4},
                    {'opt_name': '59', 'opt_value': str(cfg.get('dhcp_t2', 60)), 'ip_version': 4}])
        for i in range(2):
            vm = pair[str(i)]
            if vm.get('server'):
                # Missing checkpointed IDs fail closed; never silently replace
                # a VM whose identity is part of the preservation evidence.
                self.wait_active(vm['server'])
                continue
            guest = (pathlib.Path(__file__).parent/'guest_probe.py').read_text()
            config = dict(run=cfg['run'], vm=vm['record_vm'], peer=pair[str(1-i)]['ip'],
                          ip=vm['ip'], interval=cfg['interval'], lifetime=cfg['lifetime'], dhcp_t1=cfg.get('dhcp_t1',30), continuous_ping=(stage=='measure'))
            # Guest obtains its own immutable instance UUID from cloud-init's datasource.
            launcher = "import json,pathlib; p=pathlib.Path('/etc/migration-probe.json'); c=json.loads(p.read_text()); c['server_id']=pathlib.Path('/var/lib/cloud/data/instance-id').read_text().strip(); p.write_text(json.dumps(c))"
            user_data = '#cloud-config\n' + __import__('yaml').safe_dump(dict(
                write_files=[dict(path='/usr/local/bin/migration-probe.py', content=guest, permissions='0700'),
                             dict(path='/etc/migration-probe.json', content=json.dumps(config), permissions='0600'),
                             dict(path='/etc/systemd/system/migration-probe.service', content='[Unit]\nAfter=network-online.target\n[Service]\nExecStart=/usr/bin/python3 /usr/local/bin/migration-probe.py\n[Install]\nWantedBy=multi-user.target\n')],
                runcmd=[['python3', '-c', launcher], ['systemctl', 'enable', '--now', 'migration-probe']]))
            # Stable name + explicit port lets retry recover a server if API reply was lost.
            matches = list(c.compute.servers(name=vm['name']))
            matches = [v for v in matches if v.name == vm['name']]
            if len(matches) > 1:
                raise RuntimeError('Ambiguous owned server name; inspect checkpoint')
            if matches and c.network.get_port(vm['port']).device_id != matches[0].id:
                raise RuntimeError('Recovered server does not own the checkpointed port')
            if modern and matches and (matches[0].metadata.get('ovn_migration_run')!=cfg['run'] or
                                       matches[0].metadata.get('ovn_validation_role')!=role):
                raise RuntimeError('Recovered server ownership metadata does not match this validation role')
            server = matches[0] if matches else c.compute.create_server(name=vm['name'],
                image_id=image.id, flavor_id=flavor.id, networks=[{'port': vm['port']}],
                metadata={'ovn_migration_run': cfg['run'], 'ovn_validation_role': role},
                user_data=base64.b64encode(user_data.encode()).decode())
            vm['server'] = server.id
            self.commit()
            self.wait_active(server.id)

    def wait_active(self, server_id):
        server = self.cloud.compute.get_server(server_id)
        if server.status == 'ERROR':
            raise RuntimeError(f'Validation server {server_id} is ERROR; resources preserved; Nova fault: {getattr(server, "fault", None)}')
        try:
            return self.cloud.compute.wait_for_server(server, status='ACTIVE', failures=['ERROR'],
                                                     wait=self.cfg['timeout'], interval=2)
        except Exception as exc:
            raise RuntimeError(f'Validation server {server_id} failed to become ACTIVE within {self.cfg["timeout"]}s; resources preserved: {exc}') from exc

    def console_output(self, server_id, deadline):
        while True:
            try:
                return self.cloud.compute.get_server_console_output(
                    server_id, length=int(self.cfg.get('console_tail_lines', 20000)))['output']
            except Exception as exc:
                status = getattr(exc, 'status_code', getattr(exc, 'http_status', None))
                message = str(exc).lower()
                if status != 409 or 'instance' not in message or 'not ready' not in message:
                    raise
                # Recheck Nova after a readiness conflict; permanent failures
                # (including a disappearing instance) must not be retried.
                server = self.cloud.compute.get_server(server_id)
                if server.status == 'ERROR':
                    raise RuntimeError(f'Validation server {server_id} entered ERROR while collecting console; resources preserved; Nova fault: {getattr(server, "fault", None)}') from exc
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f'Nova console for validation server {server_id} remained not ready within validation timeout; resources preserved') from exc
                time.sleep(min(2, remaining))

    def collect(self, stage, deadline=None):
        if deadline is None:
            deadline = time.monotonic()+self.cfg.get('timeout', 300)
        s = self.pair(stage)
        all_rows = {}
        for i in range(2):
            vm = s[str(i)].get('record_vm', stage+str(i))
            path = self.root/(vm+'-console-records.json')
            text = self.console_output(s[str(i)]['server'], deadline)
            with (self.root/(vm+'-records.lock')).open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                old = json.loads(path.read_text()) if path.exists() else []
                (self.root/(vm+'-console-latest.log')).write_text(text)
                merged = {json.dumps(r, sort_keys=True): r for r in old + records(text, self.cfg['run'], vm)}
                rows = list(merged.values())
                save(path, rows)
            all_rows[str(i)] = rows
        return all_rows
    def anchors(self, stage, deadline=None):
        rows = self.collect(stage, deadline=deadline)
        return {key: freshness_anchor(value) for key,value in rows.items()}

    def checkpoint_start(self):
        path = self.root/'validation-window.json'
        window = read_evidence(self.root, path.name)
        if getattr(self,'cfg',{}).get('capture_enabled'):
            Capture(self).start()
            Capture(self).anchor()
            return read_evidence(self.root,'validation-window.json')['pcap_start']
        if window.get('start_anchor'):
            if getattr(self,'state',{}).get('schema_version')==2 and window.get('measurement_workload')!='Pair A':
                raise RuntimeError('Historical measurement window cannot be relabeled Pair A; use a new run')
            return window['start_anchor']
        if getattr(self,'state',{}).get('schema_version')==2 and read_evidence(self.root,'measure-readiness.json').get('status')!='PASS':
            raise RuntimeError('Pair A must be fully ready before establishing the measurement start')
        rows = self.collect('measure')
        anchor = sequence_anchor(rows['0'])
        if not anchor:
            raise RuntimeError('No valid Pair-A sequence available before migration-affecting work')
        save(path, {'start_anchor': anchor, 'pair_anchors':{k:sequence_anchor(r) for k,r in rows.items()},
                    'measurement_workload':'Pair A', 'window': '(start sequence, recovery end sequence]'})
        return anchor

    def check(self, stage, anchors, rows):
        checks = {}
        for i in range(2):
            vm = self.pair(stage)[str(i)]
            server = self.cloud.compute.get_server(vm['server'])
            port = self.cloud.network.get_port(vm['port'])
            identity = port.device_id == vm['server'] and port.fixed_ips == vm['fixed_ips']
            if self.state.get('schema_version') == 2:
                identity = identity and server.id == vm['server'] and port.id == vm['port']
            checks[str(i)] = dict(identity='PASS' if identity else 'FAIL',
                active='PASS' if server.status == 'ACTIVE' else 'FAIL',
                bound='PASS' if port.status == 'ACTIVE' and port.binding_host_id and port.binding_vif_type not in ('unbound','binding_failed') else 'FAIL')
            checks[str(i)].update(server_uuid_preserved='PASS' if getattr(server,'id',None)==vm['server'] else 'FAIL',
                                  port_uuid_preserved='PASS' if getattr(port,'id',None)==vm['port'] and port.device_id==vm['server'] else 'FAIL',
                                  fixed_ip_preserved='PASS' if port.fixed_ips==vm['fixed_ips'] else 'FAIL')
            checks[str(i)].update(guest_checks(rows[str(i)], anchors[str(i)], self.cfg['interval']))
            network = self.cloud.network.get_network(vm['network'])
            checks[str(i)]['network_type'] = network.provider_network_type
            checks[str(i)]['dhcp_availability'] = checks[str(i)]['dhcp']
            health = latest_health(rows[str(i)], anchors[str(i)], self.cfg['interval'])
            expected_mtu = self.cfg.get('source_mtu',1450) if self.cfg.get('initial') else self.cfg.get('target_mtu',1442)
            checks[str(i)]['mtu'] = 'PASS' if health and health.get('mtu') == expected_mtu and network.mtu==expected_mtu else 'FAIL'
            baseline = (read_evidence(self.root, 'existing-post-cutover-baseline.json') or read_evidence(self.root, 'existing-migration-baseline.json')) if stage == 'pre' and not self.cfg.get('initial') else {}
            if self.state.get('schema_version') == 2 and stage == 'pre' and not self.cfg.get('initial'):
                old = baseline.get(str(i), {})
                current = sequence_anchor(rows[str(i)])
                checks[str(i)]['boot_continuity'] = 'PASS' if current and current['boot'] == old.get('boot') else 'FAIL'
                checks[str(i)]['identity'] = 'PASS' if identity and all(vm.get(k)==old.get(k) for k in ('server','port','fixed_ips')) else 'FAIL'
                checks[str(i)].update(server_uuid_preserved='PASS' if server.id==vm['server']==old.get('server') else 'FAIL',
                    port_uuid_preserved='PASS' if port.id==vm['port']==old.get('port') and port.device_id==old.get('server') else 'FAIL',
                    fixed_ip_preserved='PASS' if port.fixed_ips==vm['fixed_ips']==old.get('fixed_ips') else 'FAIL')
            if network.provider_network_type == 'geneve':
                ports = list(self.cloud.network.ports(network_id=vm['network'], device_owner='network:distributed'))
                expected_ip = metadata_port_ip(ports, vm['subnet'])
                health = latest_health(rows[str(i)], anchors[str(i)], self.cfg['interval'])
                target_mtu = self.cfg.get('target_mtu', 1442)
                checks[str(i)]['dhcp_convergence'] = dhcp_convergence(health, target_mtu, expected_ip)
                if network.mtu != target_mtu:
                    checks[str(i)]['dhcp_convergence'] = 'FAIL'
                checks[str(i)]['dhcp_expected'] = {'mtu': target_mtu, 'metadata_ip': expected_ip}
        return checks

    def wait(self, stage):
        # Snapshot the console at the beginning of EACH validation invocation.
        # When a guest has not emitted yet, latch its first batch and require
        # later sequence growth; that batch itself can never satisfy the gate.
        deadline = time.monotonic()+self.cfg['timeout']
        anchors = self.anchors(stage, deadline=deadline)
        save(self.root/(stage+'-freshness-anchors.json'), anchors)
        while True:
            rows = self.collect(stage, deadline=deadline)
            for key in anchors:
                if anchors[key] is None:
                    anchors[key] = freshness_anchor(rows[key])
                    save(self.root/(stage+'-freshness-anchors.json'), anchors)
            checks = self.check(stage, anchors, rows)
            save(self.root/(stage+'-workload-checks.json'), checks)
            network_type = 'vxlan' if stage == 'pre' and self.cfg.get('initial') else 'geneve'
            modern_ready = (self.state.get('schema_version') != 2 or
                            all(r.get('mtu') == 'PASS' and (stage != 'pre' or self.cfg.get('initial') or
                                r.get('boot_continuity') == 'PASS') for r in checks.values()))
            if workload_pass(checks, network_type) and modern_ready:
                return rows
            if time.monotonic() >= deadline:
                phase = ('Initial OVS workload validation' if self.cfg.get('initial') else
                         'DHCP/metadata convergence after OVN migration and workload validation')
                raise RuntimeError(phase + ' timed out: require NEW sequence-based packet and health records; use Ubuntu cloud image with cloud-init, Python3, iproute2, ping, DHCP leases and ttyS0; inspect console evidence')
            time.sleep(5)

    def baseline(self, stage, rows):
        return {key:dict(server=vm['server'], port=vm['port'], fixed_ips=vm['fixed_ips'],
                         boot=sequence_anchor(rows[key])['boot']) for key,vm in self.pair(stage).items() if key in ('0','1')}

    def measure_checks(self, rows, anchors):
        baseline = read_evidence(self.root, 'measure-baseline.json')
        checks = {}
        for key in ('0','1'):
            vm = self.pair('measure')[key]
            server = self.cloud.compute.get_server(vm['server'])
            port = self.cloud.network.get_port(vm['port'])
            current = sequence_anchor(rows[key])
            old = baseline.get(key, {})
            identity = (server.id == vm['server'] and port.id == vm['port'] and
                        port.device_id == vm['server'] and port.fixed_ips == vm['fixed_ips'] and
                        (not old or all(vm[k] == old.get(k) for k in ('server','port','fixed_ips'))))
            checks[key] = dict(identity='PASS' if identity else 'FAIL',
                active='PASS' if server.status == 'ACTIVE' else 'FAIL',
                bound='PASS' if port.status == 'ACTIVE' and port.binding_host_id and port.binding_vif_type not in ('unbound','binding_failed') else 'FAIL',
                boot_continuity='PASS' if current and (not old or current['boot']==old.get('boot')) else 'FAIL',
                connectivity=guest_checks(rows[key], anchors[key], self.cfg['interval'])['connectivity'])
        return checks

    def wait_measure(self, initial=False):
        deadline = time.monotonic()+self.cfg['timeout']
        anchors = self.anchors('measure', deadline=deadline)
        while True:
            rows = self.collect('measure', deadline=deadline)
            for key in anchors:
                if anchors[key] is None:
                    anchors[key] = freshness_anchor(rows[key])
            checks = self.measure_checks(rows, anchors)
            good = all(all(v=='PASS' for v in row.values()) for row in checks.values())
            if not initial and not self.cfg.get('capture_enabled'):
                # Packet-only recovery boundary: health/MTU never enter this gate.
                self.checkpoint_recovery(rows, anchors)
            save(self.root/('measure-readiness.json' if initial else 'measure-post-checks.json'),
                 {'status':'PASS' if good else 'IN_PROGRESS', 'guests':checks})
            if good:
                if initial and not read_evidence(self.root, 'measure-baseline.json'):
                    save(self.root/'measure-baseline.json', self.baseline('measure',rows))
                return rows
            if time.monotonic()>=deadline:
                raise TimeoutError('Pair-A small-packet recovery/identity/boot continuity unavailable; no MTU or metadata remediation allowed')
            time.sleep(2)

    def checkpoint_recovery(self, rows, anchors):
        if self.cfg.get('capture_enabled'):
            return False  # only final compute capture establishes authoritative recovery
        if not all(guest_checks(rows[k], anchors[k], self.cfg['interval'])['connectivity'] == 'PASS' for k in ('0','1')):
            return False
        window = read_evidence(self.root, 'validation-window.json')
        original = window.get('pair_anchors',{})
        if original and any(not sequence_anchor(rows[k]) or sequence_anchor(rows[k])['boot']!=a['boot'] for k,a in original.items()):
            self.save_measurement(rows)
            return False
        end = sequence_anchor(rows['0'])
        if window.get('start_anchor') and end and not window.get('end_anchor'):
            window['end_anchor'] = end
            save(self.root/'validation-window.json', window)
        if window.get('end_anchor'):
            self.save_measurement(rows)
        return bool(window.get('end_anchor'))

    def save_measurement(self, rows):
        if self.cfg.get('capture_enabled'):
            return  # compute PCAP is finalized after all guest and OVN checks
        window = read_evidence(self.root, 'validation-window.json')
        result = probe_metrics(rows['0'], window.get('start_anchor'), window.get('end_anchor'), self.cfg['interval'])
        expected = window.get('pair_anchors', {})
        continuity = all(sequence_anchor(rows[k]) and sequence_anchor(rows[k])['boot']==a['boot']
                         for k,a in expected.items()) if len(expected)==2 else None
        result['pair_a_boot_continuity'] = 'PASS' if continuity is True else ('FAIL' if continuity is False else 'UNAVAILABLE')
        if continuity is False:
            result.update(status='UNAVAILABLE', actual_dataplane_outage_seconds=None)
        if getattr(self,'state',{}).get('schema_version')==2 and (continuity is not True or window.get('measurement_workload')!='Pair A'):
            result.update(status='UNAVAILABLE',actual_dataplane_outage_seconds=None)
        save(self.root/'tenant-dataplane-probe.json', result)

    def prepare_dhcp(self, target=False):
        name = 'dhcp-precutover-preparation.json' if target else 'dhcp-initial-preparation.json'
        deadline = time.monotonic()+self.cfg.get('dhcp_timeout', 180)
        try:
            if target and self.state.get('schema_version')==2:
                prior = read_evidence(self.root, 'existing-mtu-remediation.json')
                if any(r.get('reboot_requested') for r in prior.get('guests',{}).values()):
                    self.remediate(prior)
                    return
                if read_evidence(self.root, 'existing-migration-baseline.json'):
                    self.verify_precutover()
                    return
            # Phase 06 invokes this AFTER updating network MTUs. Snapshot anew
            # for each gate; phase-04 renewals cannot satisfy phase 06.
            baseline_rows = self.collect('pre', deadline=deadline)
            baseline = {key: freshness_anchor(baseline_rows[key]) for key in ('0','1')}
            anchor_mono = {}
            for key, anchor in baseline.items():
                times = [r['mono'] for r in baseline_rows[key] if anchor and valid_marker(r)
                         and r['boot'] == anchor['boot'] and r['seq'] <= anchor['seq']
                         and type(r.get('mono')) in (int, float) and math.isfinite(r['mono'])]
                anchor_mono[key] = max(times) if times else None
            initial = (read_evidence(self.root, 'existing-initial-baseline.json') or
                       read_evidence(self.root, 'initial-freshness-anchors.json') or baseline)
            while True:
                rows = self.collect('pre', deadline=deadline)
                evidence = {}
                for key in ('0','1'):
                    health = latest_health(rows[key], baseline[key], self.cfg['interval'])
                    current = sequence_anchor(rows[key])
                    same_boot = bool(current and initial[key] and current['boot'] == initial[key]['boot'])
                    flags = renewal_flags(health,anchor_mono[key],self.cfg)
                    good = bool(health and health.get('dhcp') is True and
                                all(flags.values()) and
                                health.get('dhcp_ack_count',0) >= 2 and same_boot and
                                guest_checks(rows[key], baseline[key], self.cfg['interval'])['connectivity']=='PASS')
                    if target:
                        network = self.cloud.network.get_network(self.pair('pre')[key]['network'])
                        common = good
                        source_network = self.state.get('schema_version')!=2 or network.provider_network_type=='vxlan'
                        good = good and source_network and network.mtu == self.cfg.get('target_mtu',1442) and health.get('mtu') == network.mtu
                    else:
                        # Source OVS metadata and MTU remain valid here; OVN
                        # metadata next-hop checks belong to post-migration.
                        good = good and health.get('metadata') is True
                    evidence[key] = {'status':'PASS' if good else 'UNAVAILABLE', 'health':health,
                                     'same_boot':same_boot, **flags}
                    if target:
                        eligible = bool(common and source_network and network.mtu==self.cfg.get('target_mtu',1442) and
                            health.get('mtu') is not None and health['mtu']!=network.mtu and health.get('metadata') is True and
                            health.get('mtu_configuration')=='dhcp_mtu_enabled' and
                            'configured_static_mtu' in health and health['configured_static_mtu'] is None and health.get('dhcp_use_mtu') is True)
                        evidence[key]['classification'] = 'PASS' if good else ('REBOOT_REQUIRED' if eligible else 'FAIL')
                save(self.root/name, {'status':'PASS' if all(r['status']=='PASS' for r in evidence.values()) else 'IN_PROGRESS',
                                     'anchors':baseline, 'anchor_monotonic':anchor_mono, 'guests':evidence})
                if all(r['status']=='PASS' for r in evidence.values()):
                    if target and self.state.get('schema_version')==2:
                        self.complete_automatic(evidence, rows)
                    return
                if time.monotonic() >= deadline:
                    if target and self.state.get('schema_version')==2:
                        save(self.root/'existing-mtu-automatic.json', {'status':'FAIL','guests':evidence,
                            'anchors':baseline, 'anchor_monotonic':anchor_mono})
                        journal = {'automatic_mtu_convergence':'FAIL', 'remediation_required':any(r.get('classification')=='REBOOT_REQUIRED' for r in evidence.values()),
                                   'remediation_action':'none', 'status':'IN_PROGRESS', 'guests':{}}
                        for key,row in evidence.items():
                            vm = self.pair('pre')[key]
                            journal['guests'][key] = dict(row, original_boot=initial[key]['boot'],
                                server=vm['server'], port=vm['port'], fixed_ips=vm['fixed_ips'],
                                guest_mtu_before=row['health'].get('mtu') if row['health'] else None,
                                target_mtu=self.cfg.get('target_mtu',1442), reboot_requested=False)
                        save(self.root/'existing-mtu-remediation.json', journal)
                        if all(r.get('classification') in ('PASS','REBOOT_REQUIRED') for r in evidence.values()):
                            self.remediate(journal)
                            return
                    message = ('Guest MTU convergence before cutover timed out: require target MTU, usable lease, fresh renewal, running probe and unchanged boot' if target else
                               'Short-T1 renewal preparation timed out: require fresh guest DHCPREQUEST/ACK renewal, usable lease, running probe, unchanged boot and source OVS metadata')
                    raise TimeoutError(message)
                time.sleep(2)
        except Exception as exc:
            save(self.root/name, {'status':'FAIL', 'reason':str(exc), 'anchors':locals().get('baseline',{}),
                                 'anchor_monotonic':locals().get('anchor_mono',{}), 'guests':locals().get('evidence',{})})
            raise

    def complete_automatic(self, evidence, rows):
        save(self.root/'existing-mtu-automatic.json', {'status':'PASS','guests':evidence})
        baseline = self.baseline('pre',rows)
        details = {k:dict(row, original_boot=baseline[k]['boot'],post_remediation_boot=baseline[k]['boot'],
                         guest_mtu_before=row['health']['mtu'], guest_mtu_after=row['health']['mtu'],
                         target_mtu=self.cfg.get('target_mtu',1442), **self.identity_evidence(self.pair('pre')[k])) for k,row in evidence.items()}
        identity_ok = all(row[k]=='PASS' for row in details.values() for k in
                          ('server_uuid_preservation','port_uuid_preservation','fixed_ip_preservation'))
        save(self.root/'existing-mtu-remediation.json', {'automatic_mtu_convergence':'PASS',
             'remediation_required':False, 'remediation_action':'none', 'status':'PASS' if identity_ok else 'FAIL', 'guests':details})
        if not identity_ok:
            raise RuntimeError('Pair-B resource preservation failed after automatic MTU convergence; refusing DB freeze')
        save(self.root/'existing-migration-baseline.json', baseline)

    def identity_evidence(self, vm):
        server = self.cloud.compute.get_server(vm['server'])
        port = self.cloud.network.get_port(vm['port'])
        return dict(server_uuid_preservation='PASS' if server.id==vm['server'] else 'FAIL',
                    port_uuid_preservation='PASS' if port.id==vm['port'] and port.device_id==vm['server'] else 'FAIL',
                    fixed_ip_preservation='PASS' if port.fixed_ips==vm['fixed_ips'] else 'FAIL')

    def assert_reboot_owner(self, key, entry, require_ready=True):
        if self.state.get('schema_version')!=2 or (require_ready and self.cfg.get('allow_pre_cutover_guest_reboot') is not True):
            raise RuntimeError('Pair-B reboot remediation disabled; pre-cutover MTU readiness failed; resources preserved')
        if any((self.root/'metrics'/name).exists() for name in ('phase05.start','phase06.start','control_plane_downtime.start')):
            raise RuntimeError('Pair-B reboot prohibited after DB freeze/cutover checkpoint')
        vm = self.pair('pre')[key]
        if vm.get('owned') is not True or any(vm['server']==v['server'] or vm['port']==v['port'] for v in self.pair('measure').values()):
            raise RuntimeError('Reboot requires distinct validation-owned Pair-B UUIDs; Pair A is protected')
        if any(vm.get(k)!=entry.get(k) for k in ('server','port','fixed_ips')):
            raise RuntimeError('Pair-B checkpoint identity changed; refusing reboot')
        if require_ready and self.cloud.network.get_network(vm['network']).provider_network_type!='vxlan':
            raise RuntimeError('Pair-B reboot prohibited after OVN activation; source VXLAN required')
        server = self.cloud.compute.get_server(vm['server'])
        port = self.cloud.network.get_port(vm['port'])
        entry.update(server_uuid_preservation='PASS' if server.id==entry['server'] else 'FAIL',
                     port_uuid_preservation='PASS' if port.id==entry['port'] and port.device_id==entry['server'] else 'FAIL',
                     fixed_ip_preservation='PASS' if port.fixed_ips==entry['fixed_ips'] else 'FAIL')
        if (server.id!=entry['server'] or port.id!=entry['port'] or port.device_id!=server.id or port.fixed_ips!=entry['fixed_ips'] or
            (require_ready and (server.status!='ACTIVE' or port.status!='ACTIVE' or not port.binding_host_id or port.binding_vif_type in ('unbound','binding_failed'))) or
            server.metadata.get('ovn_migration_run')!=self.cfg['run'] or server.metadata.get('ovn_validation_role')!='existing'):
            raise RuntimeError('Live Pair-B ownership/server/port/fixed IP changed; refusing reboot')

    def wait_remediated(self, key, entry):
        deadline = time.monotonic()+self.cfg['timeout']
        self.wait_active(entry['server'])
        anchor = None
        while True:
            rows = self.collect('pre', deadline=deadline)
            current = sequence_anchor(rows[key])
            if entry.get('reboot_completed') and current and current['boot']!=entry.get('post_remediation_boot'):
                entry['observed_boot'] = current['boot']
                raise RuntimeError('Completed Pair-B guest boot changed; refusing further remediation')
            if current and current['boot']!=entry['original_boot'] and anchor is None:
                anchor = freshness_anchor(rows[key])
            health = latest_health(rows[key], anchor, self.cfg['interval'])
            if not entry.get('reboot_completed') and current and current['boot']!=entry['original_boot']:
                entry['post_remediation_boot'] = current['boot']
            if health:
                entry['guest_mtu_after'] = health.get('mtu')
            self.assert_reboot_owner(key, entry, require_ready=False)
            server = self.cloud.compute.get_server(entry['server'])
            port = self.cloud.network.get_port(entry['port'])
            good = (current and anchor and current['boot']==anchor['boot'] and
                    guest_checks(rows[key],anchor,self.cfg['interval'])['connectivity']=='PASS' and health and
                    health.get('dhcp') is True and health.get('metadata') is True and health.get('mtu')==entry['target_mtu'] and
                    server.status=='ACTIVE' and port.status=='ACTIVE' and port.binding_host_id and port.binding_vif_type not in ('unbound','binding_failed'))
            if good:
                entry.update(reboot_completed=True, post_remediation_boot=current['boot'], guest_mtu_after=health['mtu'],
                    server_uuid_preservation='PASS', port_uuid_preservation='PASS', fixed_ip_preservation='PASS')
                return
            if time.monotonic()>=deadline:
                raise TimeoutError('Pair-B soft reboot completion/target MTU/DHCP/metadata/connectivity not proven; request will not be repeated')
            time.sleep(2)

    def remediate(self, journal):
        path = self.root/'existing-mtu-remediation.json'
        try:
            for key in ('0','1'):
                entry = journal['guests'][key]
                if entry.get('reboot_completed'):
                    self.wait_remediated(key,entry)
                    save(path,journal)
                    continue
                if entry.get('classification')=='PASS':
                    continue
                self.assert_reboot_owner(key, entry, require_ready=not entry.get('reboot_requested'))
                if not entry.get('reboot_requested'):
                    # Re-evaluate all eligibility conditions immediately before
                    # requesting this UUID's reboot, including a fresh renewal.
                    attempt = read_evidence(self.root, 'existing-mtu-automatic.json')
                    rows = self.collect('pre')
                    a = attempt['anchors'][key]
                    health = latest_health(rows[key],a,self.cfg['interval'])
                    flags = renewal_flags(health, attempt['anchor_monotonic'][key], self.cfg)
                    current = sequence_anchor(rows[key])
                    network = self.cloud.network.get_network(self.pair('pre')[key]['network'])
                    if not (all(flags.values()) and health and health.get('dhcp') is True and health.get('metadata') is True and
                        health.get('dhcp_ack_count',0)>=2 and current and current['boot']==entry['original_boot'] and
                        guest_checks(rows[key],a,self.cfg['interval'])['connectivity']=='PASS' and network.mtu==entry['target_mtu'] and
                        health.get('mtu') is not None and health['mtu']!=entry['target_mtu'] and
                        health.get('mtu_configuration')=='dhcp_mtu_enabled' and 'configured_static_mtu' in health and
                        health['configured_static_mtu'] is None and health.get('dhcp_use_mtu') is True):
                        raise RuntimeError('Pair-B reboot eligibility no longer proven; refusing remediation')
                    entry['reboot_requested'] = True
                    journal['remediation_action'] = 'soft reboot'
                    save(path,journal)  # at-most-once; ambiguous API reply never resends
                    self.cloud.compute.reboot_server(entry['server'], reboot_type='SOFT')
                self.wait_remediated(key,entry)
                save(path,journal)
            expected = {k:dict(server=r['server'],port=r['port'],fixed_ips=r['fixed_ips'],
                              boot=r.get('post_remediation_boot') or r['original_boot']) for k,r in journal['guests'].items()}
            self.verify_precutover(expected)
            save(self.root/'existing-migration-baseline.json',expected)
            ready = read_evidence(self.root,'dhcp-precutover-preparation.json')['guests']
            for key,entry in journal['guests'].items():
                entry.update(post_remediation_boot=expected[key]['boot'], guest_mtu_after=ready[key]['health']['mtu'],
                             server_uuid_preservation='PASS',port_uuid_preservation='PASS',fixed_ip_preservation='PASS')
            journal['status']='PASS'
            save(path,journal)
        except Exception as exc:
            journal.update(status='FAIL', reason=str(exc))
            save(path,journal)
            raise

    def verify_precutover(self, expected=None):
        expected = expected or read_evidence(self.root,'existing-migration-baseline.json')
        if set(expected)!= {'0','1'}:
            raise RuntimeError('Missing authoritative Pair-B pre-cutover baseline; refusing DB freeze')
        deadline = time.monotonic()+self.cfg['timeout']
        anchors = self.anchors('pre',deadline=deadline)
        while True:
            rows = self.collect('pre',deadline=deadline)
            ready = {}
            for key in ('0','1'):
                vm = self.pair('pre')[key]
                current = sequence_anchor(rows[key])
                health = latest_health(rows[key],anchors[key],self.cfg['interval'])
                server = self.cloud.compute.get_server(vm['server'])
                port = self.cloud.network.get_port(vm['port'])
                network = self.cloud.network.get_network(vm['network'])
                good = (all(vm.get(k)==expected[key][k] for k in ('server','port','fixed_ips')) and
                    server.id==expected[key]['server'] and port.id==expected[key]['port'] and port.device_id==server.id and
                    port.fixed_ips==expected[key]['fixed_ips'] and server.status=='ACTIVE' and port.status=='ACTIVE' and
                    port.binding_host_id and port.binding_vif_type not in ('unbound','binding_failed') and
                    current and current['boot']==expected[key]['boot'] and health and health.get('dhcp') is True and
                    health.get('metadata') is True and health.get('mtu')==self.cfg.get('target_mtu',1442) and
                    network.mtu==self.cfg.get('target_mtu',1442) and network.provider_network_type=='vxlan' and
                    guest_checks(rows[key],anchors[key],self.cfg['interval'])['connectivity']=='PASS')
                ready[key]={'status':'PASS' if good else 'FAIL','health':health,'boot':current}
            if all(r['status']=='PASS' for r in ready.values()):
                save(self.root/'dhcp-precutover-preparation.json',{'status':'PASS','guests':ready})
                return
            if time.monotonic()>=deadline:
                save(self.root/'dhcp-precutover-preparation.json',{'status':'FAIL','guests':ready})
                raise TimeoutError('Pair-B pre-cutover identity/boot/MTU/DHCP/metadata readiness failed; refusing DB freeze')
            time.sleep(2)

    def ovn_evidence(self, key):
        vm=self.pair('pre')[key]
        path=self.root/('existing'+key+'-ovn-dhcp.json')
        extra=self.root/'ovn-evidence-transport.json'
        save(extra,dict(evidence_port=vm['port'],evidence_nb=self.cfg['ovn_nb'],evidence_sb=self.cfg['ovn_sb'],
                        evidence_host=self.cfg['ovn_cli_host'],evidence_path=str(path)))
        helper=pathlib.Path(__file__).parent.parent/'playbooks/workload-ovn-evidence-tasks.yml'
        try:
            subprocess.run(['ansible-playbook','-i',self.cfg['inventory'],str(helper),'-e','@'+str(extra)],
                           check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=min(60,self.cfg.get('timeout',300)))
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f'workload-ovn-evidence-tasks.yml failed with return code {exc.returncode}; '
                               f'stderr: {exc.stderr or ""}; stdout: {exc.stdout or ""}') from None
        return read_evidence(self.root,path.name)

    def post_ovn_health(self, key):
        vm=self.pair('pre')[key]
        ports=list(self.cloud.network.ports(network_id=vm['network'],device_owner='network:distributed'))
        metadata_ip=metadata_port_ip(ports,vm['subnet'])
        raw=self.ovn_evidence(key)
        subnet=self.cloud.network.get_subnet(vm['subnet'])
        return ovn_dhcp_health(raw,vm,subnet,metadata_ip,self.cfg)

    def post_owner(self, key, entry, request=False):
        if self.state.get('schema_version')!=2 or key not in ('0','1'):
            raise RuntimeError('Post-cutover reboot requires modern checkpointed Pair B')
        if request and self.cfg.get('allow_post_cutover_guest_reboot') is not True:
            raise RuntimeError('Post-cutover guest reboot disabled; convergence failed; resources preserved')
        vm=self.pair('pre')[key]
        excluded=[p for stage,role in (('pre','measure'),('post','fresh')) for p in self.state.get(stage,{}).get(role,{}).values()]
        if (vm.get('owned') is not True or any(vm.get(k)!=entry.get(k) for k in ('server','port','fixed_ips')) or
            any(vm['server']==p.get('server') or vm['port']==p.get('port') for p in excluded)):
            raise RuntimeError('Only exact owned Pair-B UUIDs may be rebooted; Pair A and C protected')
        identity=self.identity_evidence(vm)
        entry.update(identity)
        server=self.cloud.compute.get_server(vm['server'])
        network=self.cloud.network.get_network(vm['network'])
        if (any(v!='PASS' for v in identity.values()) or network.provider_network_type!='geneve' or
            server.metadata.get('ovn_migration_run')!=self.cfg['run'] or server.metadata.get('ovn_validation_role')!='existing' or
            (request and server.status!='ACTIVE')):
            raise RuntimeError('Live Pair-B identity/ownership/OVN activation changed; no reboot')

    def post_guest_evidence(self, key, rows, anchors, anchor_mono, expected, ovn):
        vm=self.pair('pre')[key]
        health=latest_health(rows[key],anchors[key],self.cfg['interval'])
        current=sequence_anchor(rows[key])
        checks=self.check('pre',anchors,rows)[key]
        flags=renewal_flags(health,anchor_mono[key],self.cfg)
        same_boot=bool(current and current['boot']==expected[key]['boot'])
        preserved=all(vm.get(k)==expected[key].get(k) for k in ('server','port','fixed_ips'))
        common=bool(health and preserved and same_boot and checks['identity']=='PASS' and
            checks['active']=='PASS' and checks['bound']=='PASS' and checks['mtu']=='PASS' and
            checks['connectivity']=='PASS' and health.get('dhcp') is True and ovn.get('status')=='PASS')
        route=bool(health and health.get('metadata_gateway')==ovn.get('metadata_ip') and ovn.get('metadata_ip'))
        good=common and route and health.get('metadata') is True and all(flags.values())
        # Missing observer evidence, generic failures or a fresh renewal with
        # broken metadata are never converted into guest-state reboot requests.
        ack=health.get('dhcp_last_ack_monotonic') if health else None
        observer=bool(health and health.get('dhcp_ack_count',0)>=2 and health.get('dhcp_observer_running') is True and
                      type(ack) in (int,float) and math.isfinite(ack) and 0<=ack<=anchor_mono[key] and
                      flags['timers_sane'] and flags['short_renewal_cadence'])
        gateway=health.get('metadata_gateway') if health else None
        try:
            stale_route=bool(gateway and ipaddress.ip_address(gateway).version==4 and gateway!=ovn.get('metadata_ip'))
        except ValueError:
            stale_route=False
        # A correct route with metadata failure may be a metadata-service fault;
        # absence of renewal alone cannot prove DHCP caused it.
        stale=common and observer and not flags['fresh_renewal'] and stale_route
        return dict(status='PASS' if good else 'FAIL',classification='PASS' if good else
                    'POST_CUTOVER_REBOOT_REQUIRED' if stale else 'FAIL',health=health,boot=current,
                    same_boot=same_boot,checks=checks,ovn=ovn,metadata_route_ready='PASS' if route else 'FAIL',**flags)

    def post_cutover_dhcp(self):
        journal=read_evidence(self.root,'existing-post-cutover-remediation.json')
        if any(e.get('reboot_requested') for e in journal.get('guests',{}).values()):
            self.post_remediate(journal)
            return
        expected=read_evidence(self.root,'existing-migration-baseline.json')
        if set(expected)!={'0','1'}: raise RuntimeError('Missing authoritative Pair-B migration baseline')
        anchor_path=self.root/'existing-post-cutover-anchor.json'
        fence=read_evidence(self.root,anchor_path.name)
        deadline=time.monotonic()+self.cfg.get('post_cutover_timeout',self.cfg.get('dhcp_timeout',180))
        if not fence:
            rows=self.collect('pre',deadline=deadline)
            anchors={k:freshness_anchor(rows[k]) for k in ('0','1')}
            mono={k:max((r['mono'] for r in rows[k] if anchors[k] and valid_marker(r) and
                       r['boot']==anchors[k]['boot'] and type(r.get('mono')) in (int,float)),default=None) for k in ('0','1')}
            if any(a is None for a in anchors.values()) or any(m is None for m in mono.values()):
                raise RuntimeError('Post-cutover DHCP freshness anchor unavailable')
            fence=dict(anchors=anchors,anchor_monotonic=mono,established_after_neutron_restoration=True)
            save(anchor_path,fence)  # immutable across retry, later than takeover
        evidence={}
        while True:
            rows=self.collect('pre',deadline=deadline)
            for key in ('0','1'):
                # Ownership is required even for classification; no discovered VMs.
                entry=dict(server=expected[key]['server'],port=expected[key]['port'],fixed_ips=expected[key]['fixed_ips'])
                self.post_owner(key,entry)
                ovn=self.post_ovn_health(key)
                evidence[key]=self.post_guest_evidence(key,rows,fence['anchors'],fence['anchor_monotonic'],expected,ovn)
            good=all(e['status']=='PASS' for e in evidence.values())
            path=self.root/'existing-post-cutover-automatic.json'
            if not read_evidence(self.root,path.name).get('status') in ('PASS','FAIL'):
                save(path,dict(status='PASS' if good else 'IN_PROGRESS',guests=evidence,**fence))
            if good:
                save(self.root/'existing-post-cutover-readiness.json',dict(status='PASS',dhcp_ready='PASS',metadata_route_ready='PASS',metadata_ready='PASS',guests=evidence))
                if not journal:
                    save(self.root/'existing-post-cutover-remediation.json',dict(status='PASS',remediation_required=False,remediation_action='none',guests=evidence))
                return
            if time.monotonic()>=deadline:
                if read_evidence(self.root,path.name).get('status')!='FAIL':
                    save(path,dict(status='FAIL',guests=evidence,**fence))
                journal=dict(status='FAIL',automatic_post_cutover_dhcp_convergence='FAIL',
                    remediation_required=any(e['classification']=='POST_CUTOVER_REBOOT_REQUIRED' for e in evidence.values()),
                    remediation_action='none',guests={k:dict(e,server=expected[k]['server'],port=expected[k]['port'],
                        fixed_ips=expected[k]['fixed_ips'],original_boot=expected[k]['boot'],
                        metadata_gateway_before=(e.get('health') or {}).get('metadata_gateway'),
                        guest_mtu_before=(e.get('health') or {}).get('mtu'),target_mtu=self.cfg.get('target_mtu',1442),reboot_requested=False) for k,e in evidence.items()})
                save(self.root/'existing-post-cutover-remediation.json',journal)
                if all(e['classification'] in ('PASS','POST_CUTOVER_REBOOT_REQUIRED') for e in evidence.values()):
                    self.post_remediate(journal)
                    return
                save(self.root/'existing-post-cutover-readiness.json',dict(status='FAIL',reason='Post-cutover DHCP/metadata convergence failed; OVN/identity/guest safety conditions not proven',guests=evidence))
                raise TimeoutError('DHCP/metadata convergence after OVN migration timed out; not eligible for guest reboot')
            time.sleep(2)

    def wait_post_reboot(self, key, entry):
        self.post_owner(key,entry)
        self.wait_active(entry['server'])
        deadline=time.monotonic()+self.cfg['timeout']; anchor=None
        while True:
            self.post_owner(key,entry)
            rows=self.collect('pre',deadline=deadline); current=sequence_anchor(rows[key])
            if entry.get('reboot_completed') and current and current['boot']!=entry['post_remediation_boot']:
                raise RuntimeError('Completed post-cutover reboot boot changed; request will not be repeated')
            if current and current['boot']!=entry['original_boot'] and anchor is None:
                anchor=freshness_anchor(rows[key])
            health=latest_health(rows[key],anchor,self.cfg['interval'])
            ovn=self.post_ovn_health(key)
            flags=renewal_flags(health,0,self.cfg)  # new boot; genuine renewal since boot
            server=self.cloud.compute.get_server(entry['server'])
            good=bool(anchor and current and current['boot']==anchor['boot'] and health and
                all(flags.values()) and health.get('dhcp_ack_count',0)>=2 and health.get('dhcp') is True and
                health.get('mtu')==self.cfg.get('target_mtu',1442) and health.get('metadata') is True and
                health.get('metadata_gateway')==ovn.get('metadata_ip') and ovn.get('status')=='PASS' and
                server.status=='ACTIVE' and guest_checks(rows[key],anchor,self.cfg['interval'])['connectivity']=='PASS')
            entry.update(observed_boot=current,health=health,ovn=ovn,guest_mtu_after=(health or {}).get('mtu'),
                         metadata_gateway_after=(health or {}).get('metadata_gateway'))
            if good:
                entry.update(reboot_completed=True,post_remediation_boot=current['boot'],status='PASS',
                    dhcp_ready='PASS',metadata_route_ready='PASS',metadata_ready='PASS')
                return
            if time.monotonic()>=deadline:
                raise TimeoutError('Post-cutover soft reboot DHCP/metadata convergence timed out; intent preserved, no duplicate request')
            time.sleep(2)

    def post_remediate(self, journal):
        path=self.root/'existing-post-cutover-remediation.json'
        try:
            if set(journal.get('guests',{}))!={'0','1'} or any(e.get('classification') not in ('PASS','POST_CUTOVER_REBOOT_REQUIRED') for e in journal['guests'].values()):
                raise RuntimeError('Both Pair-B classifications must be safe before any post-cutover reboot')
            for key in ('0','1'):
                entry=journal['guests'][key]
                if entry.get('classification')=='PASS' and not entry.get('reboot_requested'): continue
                if entry.get('classification')!='POST_CUTOVER_REBOOT_REQUIRED':
                    raise RuntimeError('Arbitrary failure cannot authorize post-cutover reboot')
                self.post_owner(key,entry,request=not entry.get('reboot_requested'))
                if not entry.get('reboot_requested'):
                    automatic=read_evidence(self.root,'existing-post-cutover-automatic.json')
                    expected=read_evidence(self.root,'existing-migration-baseline.json')
                    rows=self.collect('pre')
                    latest=self.post_guest_evidence(key,rows,automatic['anchors'],automatic['anchor_monotonic'],expected,self.post_ovn_health(key))
                    if latest['classification']!='POST_CUTOVER_REBOOT_REQUIRED':
                        raise RuntimeError('Post-cutover guest reboot eligibility no longer proven')
                    entry['reboot_requested']=True
                    journal['remediation_action']='soft reboot'; save(path,journal)
                    self.cloud.compute.reboot_server(entry['server'],reboot_type='SOFT')
                self.wait_post_reboot(key,entry)
                save(path,journal)
            # Verify both siblings again against their expected boots, and keep
            # the immutable pre-cutover baseline for separate continuity evidence.
            expected={k:dict(server=e['server'],port=e['port'],fixed_ips=e['fixed_ips'],
                             boot=e.get('post_remediation_boot') or e['original_boot']) for k,e in journal['guests'].items()}
            old=read_evidence(self.root,'existing-post-cutover-baseline.json')
            if old and old!=expected: raise RuntimeError('Post-cutover baseline changed; refusing rebase')
            save(self.root/'existing-post-cutover-baseline.json',expected)
            self.wait('pre')
            journal.update(status='PASS'); save(path,journal)
            save(self.root/'existing-post-cutover-readiness.json',dict(status='PASS',dhcp_ready='PASS',metadata_route_ready='PASS',metadata_ready='PASS',guests=journal['guests']))
        except Exception as exc:
            journal.update(status='FAIL',reason=str(exc)); save(path,journal)
            save(self.root/'existing-post-cutover-readiness.json',dict(status='FAIL',reason=str(exc),guests=journal['guests']))
            raise

    def cleanup(self, stage):
        s = self.state[stage]
        evidence_path = self.root/(stage+'-cleanup.json')
        evidence = read_evidence(self.root, evidence_path.name) or {'status': 'IN_PROGRESS', 'deleted': []}
        evidence.setdefault('deleted', [])
        # Journal a completed operation before moving to the next owned UUID.
        # Only exact IDs in this stage of validation-resources.json are used.
        def remove(kind, resource_id, operation):
            token = {'kind': kind, 'id': resource_id}
            if token not in evidence['deleted']:
                operation()
                evidence['deleted'].append(token)
                save(evidence_path, evidence)
        try:
            if s.get('cleaned'):
                return
            s['cleanup_started'] = True
            self.commit()
            roles = ('measure','existing') if stage=='pre' else ('fresh',)
            vms = [vm for role in roles for vm in s.get(role,{}).values()] if 'networks' in s else [s[k] for k in ('0','1')]
            for vm in vms:
                def delete_server():
                    self.cloud.compute.delete_server(vm['server'], ignore_missing=True)
                    deadline = time.monotonic()+self.cfg['timeout']
                    while self.cloud.compute.find_server(vm['server']) is not None:
                        if time.monotonic()>deadline:
                            raise RuntimeError('Server deletion timeout')
                        time.sleep(2)
                remove('server', vm['server'], delete_server)
                remove('port', vm['port'], lambda: self.cloud.network.delete_port(vm['port'], ignore_missing=True))
            networks = s.get('networks',s)
            for key in ('0','1'):
                vm = networks[key]
                if vm.get('interface'):
                    # Query only the checkpointed router/network/subnet. This
                    # handles a crash after detach but before journal commit.
                    if any(any(f['subnet_id'] == vm['subnet'] for f in port.fixed_ips)
                           for port in self.cloud.network.ports(device_id=s['router'], network_id=vm['network'])):
                        self.cloud.network.remove_interface_from_router(s['router'], subnet_id=vm['subnet'])
                    vm['interface'] = False
                    self.commit()
                remove('subnet', vm['subnet'], lambda: self.cloud.network.delete_subnet(vm['subnet'], ignore_missing=True))
                remove('network', vm['network'], lambda: self.cloud.network.delete_network(vm['network'], ignore_missing=True))
            remove('router', s['router'], lambda: self.cloud.network.delete_router(s['router'], ignore_missing=True))
            remove('security_group', s['security_group'], lambda: self.cloud.network.delete_security_group(s['security_group'], ignore_missing=True))
            evidence.update(status='PASS')
            evidence.pop('error', None)
            save(evidence_path, evidence)
            s['cleaned'] = True
            self.commit()
        except Exception as exc:
            evidence.update(status='FAIL', error=str(exc))
            save(evidence_path, evidence)
            raise

    def finalize(self):
        if not validation_ready(self.root):
            return False
        self.cleanup('post')
        self.cleanup('pre')
        return True


def main():
    p = argparse.ArgumentParser()
    p.add_argument('action', choices=['pre', 'post', 'collect', 'anchor-start', 'finalize', 'cleanup-ready', 'prepare-dhcp', 'precutover-ready', 'capture-finish', 'capture-cleanup'])
    p.add_argument('root', type=pathlib.Path)
    args = p.parse_args()
    if args.action == 'cleanup-ready':
        return 0 if validation_ready(args.root) else 1
    v = Validation(args.root)
    if args.action == 'collect':
        running = True
        def stop(*_):
            nonlocal running
            running = False
        signal.signal(signal.SIGTERM, stop)
        while running:
            try:
                v.collect('measure')
            except Exception as exc:
                with (v.root/'console-collector-errors.log').open('a') as f:
                    f.write(str(exc)+'\n')
            time.sleep(v.cfg['console_interval'])
    elif args.action == 'capture-finish':
        return int(Capture(v).finish()['status'] != 'PASS')
    elif args.action == 'capture-cleanup':
        report = read_evidence(v.root,'migration-report.json')
        if report.get('result') in ('SUCCESS','SUCCESS_WITH_REMEDIATION') and validation_ready(v.root):
            capture = Capture(v)
            cp = capture.checkpoint()
            if cp and cp.get('status') == 'STOPPED':
                capture.transport('remove',cp)
        return 0
    elif args.action == 'anchor-start':
        v.checkpoint_start()
    elif args.action == 'prepare-dhcp':
        v.prepare_dhcp(target=True)
    elif args.action == 'precutover-ready':
        v.verify_precutover()
    elif args.action == 'finalize':
        v.finalize()
    elif args.action == 'pre':
        v.cfg['initial'] = True
        v.create('measure')
        v.wait_measure(initial=True)
        v.checkpoint_start()
        v.create('pre')
        rows = v.wait('pre')
        if not read_evidence(v.root,'existing-initial-baseline.json'):
            save(v.root/'existing-initial-baseline.json',v.baseline('pre',rows))
        save(v.root/'initial-freshness-anchors.json', read_evidence(v.root, 'pre-freshness-anchors.json'))
        v.prepare_dhcp()
    else:
        failures = []
        try:
            v.wait_measure()
        except Exception as exc:
            failures.append(str(exc))
        try:
            rows = v.collect('measure')
        except Exception as exc:
            failures.append('Final console collection: '+str(exc))
            # API failure cannot erase a fully captured, anchored measurement.
            rows = {k:read_evidence(v.root,'measure'+k+'-console-records.json') or [] for k in ('0','1')}
        v.save_measurement(rows)
        try:
            if v.cfg.get('post_cutover_dhcp_enabled'):
                v.post_cutover_dhcp()
            v.wait('pre')
        except Exception as exc:
            failures.append(str(exc))
        try:
            if not v.state.get('post', {}).get('cleaned'):
                v.create('post')
                v.wait('post')
        except Exception as exc:
            failures.append(str(exc))
        if getattr(v,'state',{}).get('schema_version')==2 and not v.state.get('historical_dual_pair'):
            try:
                # Keep Pair A running throughout B/C validation, and confirm
                # continuity again without changing either measurement anchor.
                v.wait_measure()
            except Exception as exc:
                failures.append(str(exc))
        save(v.root/'workload-errors.json', failures)
        return int(bool(failures))
    return 0


if __name__ == '__main__':
    sys.exit(main())
