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
import sys
import time

PREFIX = 'OVN_MIGRATION_JSON '

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
    current = sequence_anchor(rows)
    if not anchor or not current or current['boot'] != anchor['boot']:
        return None
    candidates = [r for r in rows if r.get('kind') == 'health' and valid_marker(r) and
                  r['boot'] == anchor['boot'] and anchor['seq'] < r['seq'] <= current['seq'] and
                  current['seq']-r['seq'] <= int(10/interval)+1]
    return max(candidates, key=lambda r:r['seq']) if candidates else None


def dhcp_convergence(health, expected_mtu, metadata_ip):
    if not health or expected_mtu is None or metadata_ip is None:
        return 'UNAVAILABLE'
    return 'PASS' if (health.get('dhcp') is True and health.get('mtu') == expected_mtu and
                      health.get('metadata_gateway') == metadata_ip) else 'FAIL'


def metadata_port_ip(ports, subnet_id):
    # Count matching ports/allocations, not distinct IP strings: duplicate
    # ports claiming the same address are still ambiguous resource evidence.
    addresses = [fixed['ip_address'] for port in ports if port.device_owner == 'network:distributed'
                 for fixed in port.fixed_ips if fixed['subnet_id'] == subnet_id]
    return addresses[0] if len(addresses) == 1 else None


def guest_checks(rows, anchor, interval):
    """Only sequence growth on the anchored boot can establish fresh evidence."""
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
    health = [r for r in rows if r.get('kind') == 'health' and valid_marker(r)
              and r['boot'] == anchor['boot'] and anchor['seq'] < r['seq'] <= newest
              and newest-r['seq'] <= int(10/interval)+1]
    if health:
        latest = max(health, key=lambda r: r['seq'])
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
                  measurement='longest recovered consecutive-loss burst in (start sequence, end sequence]; VM1 ICMP')
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
    return (workload_pass(read_evidence(root, 'initial-workload-checks.json'), 'vxlan') and
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
    def commit(self):
        save(self.path, self.state)
    def create(self, stage):
        c, cfg = self.cloud, self.cfg
        s = self.state.setdefault(stage, {})
        prefix = cfg['prefix'] + '-' + cfg['run'] + '-' + stage
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
            vm = s.setdefault(str(i), {})
            name = prefix + '-' + str(i+1)
            if not vm.get('network'):
                vm['network'] = c.network.create_network(name=name).id
                self.commit()
            if not vm.get('subnet'):
                vm['subnet'] = c.network.create_subnet(name=name, network_id=vm['network'],
                    ip_version=4, cidr=cfg['cidrs'][stage][i], enable_dhcp=True).id
                self.commit()
            if not vm.get('interface'):
                existing = list(c.network.ports(device_id=s['router'], network_id=vm['network']))
                if not existing:
                    c.network.add_interface_to_router(s['router'], subnet_id=vm['subnet'])
                vm['interface'] = True
                self.commit()
            if not vm.get('port'):
                port = c.network.create_port(name=name, network_id=vm['network'],
                                             security_group_ids=[sg])
                vm.update(port=port.id, fixed_ips=port.fixed_ips, ip=port.fixed_ips[0]['ip_address'])
                self.commit()
        if stage == 'pre':
            for i in range(2):
                vm = s[str(i)]
                self.cloud.network.update_port(vm['port'], extra_dhcp_opts=[
                    {'opt_name': '58', 'opt_value': str(cfg.get('dhcp_t1', 30)), 'ip_version': 4},
                    {'opt_name': '59', 'opt_value': str(cfg.get('dhcp_t2', 60)), 'ip_version': 4}])
        for i in range(2):
            vm = s[str(i)]
            if vm.get('server'):
                # Missing checkpointed IDs fail closed; never silently replace
                # a VM whose identity is part of the preservation evidence.
                self.wait_active(vm['server'])
                continue
            guest = (pathlib.Path(__file__).parent/'guest_probe.py').read_text()
            config = dict(run=cfg['run'], vm=stage+str(i), peer=s[str(1-i)]['ip'],
                          ip=vm['ip'], interval=cfg['interval'], lifetime=cfg['lifetime'], dhcp_t1=cfg.get('dhcp_t1',30))
            # Guest obtains its own immutable instance UUID from cloud-init's datasource.
            launcher = "import json,pathlib; p=pathlib.Path('/etc/migration-probe.json'); c=json.loads(p.read_text()); c['server_id']=pathlib.Path('/var/lib/cloud/data/instance-id').read_text().strip(); p.write_text(json.dumps(c))"
            user_data = '#cloud-config\n' + __import__('yaml').safe_dump(dict(
                write_files=[dict(path='/usr/local/bin/migration-probe.py', content=guest, permissions='0700'),
                             dict(path='/etc/migration-probe.json', content=json.dumps(config), permissions='0600'),
                             dict(path='/etc/systemd/system/migration-probe.service', content='[Unit]\nAfter=network-online.target\n[Service]\nExecStart=/usr/bin/python3 /usr/local/bin/migration-probe.py\n[Install]\nWantedBy=multi-user.target\n')],
                runcmd=[['python3', '-c', launcher], ['systemctl', 'enable', '--now', 'migration-probe']]))
            # Stable name + explicit port lets retry recover a server if API reply was lost.
            matches = list(c.compute.servers(name=prefix+'-'+str(i+1)))
            matches = [v for v in matches if v.name == prefix+'-'+str(i+1)]
            if len(matches) > 1:
                raise RuntimeError('Ambiguous owned server name; inspect checkpoint')
            server = matches[0] if matches else c.compute.create_server(name=prefix+'-'+str(i+1),
                image_id=image.id, flavor_id=flavor.id, networks=[{'port': vm['port']}],
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
        s = self.state[stage]
        all_rows = {}
        for i in range(2):
            vm = stage+str(i)
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
        if window.get('start_anchor'):
            return window['start_anchor']
        anchor = sequence_anchor(self.collect('pre')['0'])
        if not anchor:
            raise RuntimeError('No valid VM1 sequence available before dataplane cutover')
        save(path, {'start_anchor': anchor, 'window': '(start sequence, recovery end sequence]'})
        return anchor

    def check(self, stage, anchors, rows):
        checks = {}
        for i in range(2):
            vm = self.state[stage][str(i)]
            server = self.cloud.compute.get_server(vm['server'])
            port = self.cloud.network.get_port(vm['port'])
            checks[str(i)] = dict(identity='PASS' if port.device_id == vm['server'] and port.fixed_ips == vm['fixed_ips'] else 'FAIL',
                active='PASS' if server.status == 'ACTIVE' else 'FAIL',
                bound='PASS' if port.status == 'ACTIVE' and port.binding_host_id and port.binding_vif_type not in ('unbound','binding_failed') else 'FAIL')
            checks[str(i)].update(guest_checks(rows[str(i)], anchors[str(i)], self.cfg['interval']))
            network = self.cloud.network.get_network(vm['network'])
            checks[str(i)]['network_type'] = network.provider_network_type
            checks[str(i)]['dhcp_availability'] = checks[str(i)]['dhcp']
            if network.provider_network_type == 'geneve':
                ports = list(self.cloud.network.ports(network_id=vm['network'], device_owner='network:distributed'))
                expected_ip = metadata_port_ip(ports, vm['subnet'])
                health = latest_health(rows[str(i)], anchors[str(i)], self.cfg['interval'])
                checks[str(i)]['dhcp_convergence'] = dhcp_convergence(health, network.mtu, expected_ip)
                checks[str(i)]['dhcp_expected'] = {'mtu': network.mtu, 'metadata_ip': expected_ip}
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
            if stage == 'pre' and not self.cfg.get('initial'):
                self.checkpoint_recovery(rows, anchors)
            checks = self.check(stage, anchors, rows)
            save(self.root/(stage+'-workload-checks.json'), checks)
            network_type = 'vxlan' if stage == 'pre' and self.cfg.get('initial') else 'geneve'
            if workload_pass(checks, network_type):
                return rows
            if time.monotonic() >= deadline:
                raise RuntimeError('Guest validation failed/unavailable: require NEW sequence-based packet and health records; use Ubuntu cloud image with cloud-init, Python3, iproute2, ping, DHCP leases and ttyS0; inspect console evidence')
            time.sleep(5)

    def checkpoint_recovery(self, rows, anchors):
        if not all(guest_checks(rows[k], anchors[k], self.cfg['interval'])['connectivity'] == 'PASS' for k in ('0','1')):
            return False
        window = read_evidence(self.root, 'validation-window.json')
        end = sequence_anchor(rows['0'])
        if window.get('start_anchor') and end and not window.get('end_anchor'):
            window['end_anchor'] = end
            save(self.root/'validation-window.json', window)
        if window.get('end_anchor'):
            save(self.root/'tenant-dataplane-probe.json', probe_metrics(
                rows['0'], window['start_anchor'], window['end_anchor'], self.cfg['interval']))
        return bool(window.get('end_anchor'))

    def prepare_dhcp(self, target=False):
        name = 'dhcp-precutover-preparation.json' if target else 'dhcp-initial-preparation.json'
        deadline = time.monotonic()+self.cfg.get('dhcp_timeout', 180)
        try:
            baseline = self.anchors('pre', deadline=deadline)
            initial = read_evidence(self.root, 'initial-freshness-anchors.json') or baseline
            while True:
                rows = self.collect('pre', deadline=deadline)
                evidence = {}
                for key in ('0','1'):
                    health = latest_health(rows[key], baseline[key], self.cfg['interval'])
                    current = sequence_anchor(rows[key])
                    same_boot = bool(current and initial[key] and current['boot'] == initial[key]['boot'])
                    good = bool(health and health.get('dhcp') is True and
                                health.get('dhcp_t1_seconds') == self.cfg.get('dhcp_t1',30) and
                                health.get('dhcp_t2_seconds') == self.cfg.get('dhcp_t2',60) and
                                health.get('dhcp_ack_count',0) >= 2 and same_boot and
                                guest_checks(rows[key], baseline[key], self.cfg['interval'])['connectivity']=='PASS')
                    if target:
                        network = self.cloud.network.get_network(self.state['pre'][key]['network'])
                        good = good and network.mtu == self.cfg.get('target_mtu',1442) and health.get('mtu') == network.mtu
                    evidence[key] = {'status':'PASS' if good else 'UNAVAILABLE', 'health':health, 'same_boot':same_boot}
                save(self.root/name, {'status':'PASS' if all(r['status']=='PASS' for r in evidence.values()) else 'IN_PROGRESS', 'guests':evidence})
                if all(r['status']=='PASS' for r in evidence.values()): return
                if time.monotonic() >= deadline:
                    raise TimeoutError('DHCP preparation did not converge: require observed short-T1 renewal ACKs, usable lease, running probe, unchanged boot and target MTU before DB freeze')
                time.sleep(2)
        except Exception as exc:
            save(self.root/name, {'status':'FAIL', 'reason':str(exc), 'guests':locals().get('evidence',{})})
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
            for i in range(2):
                vm = s[str(i)]
                def delete_server():
                    self.cloud.compute.delete_server(vm['server'], ignore_missing=True)
                    deadline = time.monotonic()+self.cfg['timeout']
                    while self.cloud.compute.find_server(vm['server']) is not None:
                        if time.monotonic()>deadline:
                            raise RuntimeError('Server deletion timeout')
                        time.sleep(2)
                remove('server', vm['server'], delete_server)
                remove('port', vm['port'], lambda: self.cloud.network.delete_port(vm['port'], ignore_missing=True))
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
    p.add_argument('action', choices=['pre', 'post', 'collect', 'anchor-start', 'finalize', 'cleanup-ready', 'prepare-dhcp'])
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
                v.collect('pre')
            except Exception as exc:
                with (v.root/'console-collector-errors.log').open('a') as f:
                    f.write(str(exc)+'\n')
            time.sleep(v.cfg['console_interval'])
    elif args.action == 'anchor-start':
        v.checkpoint_start()
    elif args.action == 'prepare-dhcp':
        v.prepare_dhcp(target=True)
    elif args.action == 'finalize':
        v.finalize()
    elif args.action == 'pre':
        v.cfg['initial'] = True
        v.create('pre')
        v.wait('pre')
        save(v.root/'initial-freshness-anchors.json', read_evidence(v.root, 'pre-freshness-anchors.json'))
        v.prepare_dhcp()
    else:
        failures = []
        recovered = None
        try:
            recovered = v.wait('pre')
        except Exception as exc:
            failures.append(str(exc))
        window = read_evidence(v.root, 'validation-window.json')
        try:
            rows = v.collect('pre')['0']
        except Exception as exc:
            failures.append('Final console collection: '+str(exc))
            # API failure cannot erase a fully captured, anchored measurement.
            rows = read_evidence(v.root, 'pre0-console-records.json') or []
        save(v.root/'tenant-dataplane-probe.json', probe_metrics(rows, window.get('start_anchor'),
             window.get('end_anchor'), v.cfg['interval']))
        try:
            if not v.state.get('post', {}).get('cleaned'):
                v.create('post')
                v.wait('post')
        except Exception as exc:
            failures.append(str(exc))
        save(v.root/'workload-errors.json', failures)
        return int(bool(failures))
    return 0


if __name__ == '__main__':
    sys.exit(main())
