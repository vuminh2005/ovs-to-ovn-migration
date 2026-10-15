#!/usr/bin/env python3
"""Independent TCP captures and read-only NAT snapshots; never reconnect probes."""
import argparse
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid

# Agent helpers live beside the PCAP. Imports must not generate extra files in
# that evidence directory; older staged helpers may already have a pyc cache.
sys.dont_write_bytecode = True
# Ansible's script module copies only this file for the read-only conntrack
# command. Capture operations stage this file together with its tap resolver.
if __name__ != '__main__' or len(sys.argv) < 2 or sys.argv[1] != 'conntrack':
    from dataplane_capture import alive, identity, resolve_tap, safe_interface_name, save


def checked_config(cfg):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', cfg['run']):
        raise ValueError('Unsafe diagnostic run ID')
    if cfg['kind'] not in ('endpoint', 'guest-snat', 'guest-fip'):
        raise ValueError('Unknown TCP capture role')
    for key in ('endpoint_ip', 'snat_ip', 'fip_ip', 'guest_ip'):
        ipaddress.IPv4Address(cfg[key])
    if type(cfg['tcp_port']) is not int or not 1 <= cfg['tcp_port'] <= 65535:
        raise ValueError('Invalid diagnostic TCP port')
    if not 0 < cfg['lifetime'] <= 86400:
        raise ValueError('Invalid diagnostic lifetime')
    if not Path(cfg['directory']).is_absolute():
        raise ValueError('Diagnostic directory must be absolute')
    if cfg['kind'] == 'endpoint' and not safe_interface_name(cfg['interface']):
        raise ValueError('Unsafe endpoint capture interface')
    if cfg['kind'] != 'endpoint':
        uuid.UUID(cfg['server']); uuid.UUID(cfg['port'])
    return cfg


def capture_filter(cfg):
    checked_config(cfg)
    peers = (cfg['snat_ip'], cfg['fip_ip']) if cfg['kind'] == 'endpoint' else (cfg['guest_ip'],)
    if not cfg.get('icmp_enabled') and not cfg.get('new_tcp_port'):
        return ('tcp and port '+str(cfg['tcp_port'])+' and host '+cfg['endpoint_ip']+
                ' and ('+' or '.join('host '+ip for ip in peers)+')')
    ports = [cfg['tcp_port']] + ([cfg['new_tcp_port']] if cfg.get('new_tcp_port') else [])
    if any(type(p) is not int or not 1 <= p <= 65535 for p in ports):
        raise ValueError('Invalid additional diagnostic TCP port')
    hosts = '('+' or '.join('host '+ip for ip in peers)+')'
    return ('((icmp or (tcp and ('+' or '.join('port '+str(p) for p in ports)+
        '))) and host '+cfg['endpoint_ip']+' and '+hosts+') or (arp and ('+
        ' or '.join('host '+cfg[k] for k in ('endpoint_ip', 'snat_ip', 'fip_ip', 'guest_ip'))+'))')


def capture_interface(cfg):
    if cfg['kind'] != 'endpoint':
        return resolve_tap(cfg)
    interface = cfg['interface']
    addresses = json.loads(subprocess.check_output(['ip', '-j', '-4', 'address', 'show', 'dev', interface], text=True))
    if not any(a.get('local') == cfg['endpoint_ip'] for nic in addresses for a in nic.get('addr_info', [])):
        raise RuntimeError('Endpoint IP is not assigned to configured diagnostic interface')
    return interface


def read_state(root):
    return json.loads((root/'tcp-capture-state.json').read_text())


def unexpected_capture_files(root):
    unexpected = []
    allowed = {'ns_diagnostics.py', 'dataplane_capture.py', 'start.lock'}
    for path in root.iterdir():
        if path.name in allowed and path.is_file() and not path.is_symlink():
            continue
        if path.name == '__pycache__' and path.is_dir() and not path.is_symlink():
            if all(p.is_file() and not p.is_symlink() and re.fullmatch(
                r'(?:ns_diagnostics|dataplane_capture)\.cpython-\d+(?:\.opt-\d+)?\.pyc', p.name)
                for p in path.iterdir()):
                continue
        unexpected.append(path.name)
    return sorted(unexpected)


def agent_inspect(root, cfg):
    """An absent journal proves no launch only if no capture artifacts exist."""
    checked_config(cfg)
    if root.resolve() != Path(cfg['directory']).resolve():
        raise RuntimeError('Diagnostic directory mismatch')
    if not root.exists():
        return dict(status='NOT_STARTED', config=cfg)
    if (root/'tcp-capture-state.json').exists():
        return agent_snapshot(root, cfg)
    unexpected = unexpected_capture_files(root)
    if unexpected:
        raise RuntimeError('Unjournaled TCP capture files must be preserved: '+', '.join(unexpected))
    return dict(status='NOT_STARTED', config=cfg)


def agent_start(root, cfg):
    checked_config(cfg)
    if root.resolve() != Path(cfg['directory']).resolve():
        raise RuntimeError('Diagnostic directory mismatch')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root/'start.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (root/'tcp-capture-state.json').exists():
            state = read_state(root)
            if state['config'] != cfg or state['status'] != 'RUNNING' or not all(alive(state.get(k)) for k in ('supervisor', 'tcpdump')):
                raise RuntimeError('TCP capture changed/ended; replacement prohibited')
            return state
        if cfg.get('allow_create') is not True:
            raise RuntimeError('TCP capture launch is ambiguous; replacement prohibited')
        unexpected = unexpected_capture_files(root)
        if unexpected:
            raise RuntimeError('Existing TCP capture evidence must not be overwritten: '+', '.join(unexpected))
        interface = capture_interface(cfg)
        # All tools/interfaces are checked before recording a launch intent.
        subprocess.run(['tcpdump', '--version'], check=True, capture_output=True, timeout=10)
        state = dict(status='START_INTENT', config=cfg, interface=interface, filter=capture_filter(cfg),
                     intent_at=time.time(), path=str(root/'tcp.pcap'))
        save(root/'tcp-capture-state.json', state)
        with (root/'supervisor.log').open('ab') as log:
            subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'supervise', str(root)],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        deadline = time.monotonic()+10
        while time.monotonic() < deadline:
            state = read_state(root)
            if state['status'] == 'RUNNING':
                return state
            if state['status'] == 'FAILED':
                raise RuntimeError(state.get('error', 'TCP capture failed'))
            time.sleep(.1)
        raise TimeoutError('TCP capture launch uncertain; intent retained')


def supervise(root):
    state = read_state(root)
    child = None
    stopping = False
    state['supervisor'] = identity(os.getpid())
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        with (root/'tcpdump.log').open('ab') as log:
            child = subprocess.Popen(['tcpdump', '-U', '-n', '-s', '0', '-i', state['interface'],
                                      '-w', state['path'], state['filter']],
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=log)
            state.update(tcpdump=identity(child.pid), started_at=time.time(), supervisor_gap=False)
            started = time.monotonic()
            while not Path(state['path']).exists() or Path(state['path']).stat().st_size < 24:
                if child.poll() is not None or time.monotonic()-started > 5:
                    raise RuntimeError('TCP capture did not open a PCAP')
                time.sleep(.05)
            state['status'] = 'RUNNING'
            save(root/'tcp-capture-state.json', state)
            timed_out = False
            signaled = False
            stop_deadline = None
            heartbeat = time.monotonic()
            while child.poll() is None:
                now = time.monotonic()
                if now-heartbeat > 2:
                    state['supervisor_gap'] = True
                heartbeat = now
                timed_out = time.monotonic()-started >= state['config']['lifetime']
                if (stopping or timed_out or (root/'stop-request').exists()) and not signaled:
                    child.send_signal(signal.SIGINT)
                    signaled = True
                    stop_deadline = time.monotonic()+10
                if stop_deadline is not None and time.monotonic() >= stop_deadline:
                    child.kill()  # Popen still owns this unreaped child.
                    child.wait(timeout=5)
                    state['forced_stop'] = True
                    break
                state['heartbeat_at'] = time.time()
                save(root/'tcp-capture-state.json', state)
                time.sleep(.25)
            state.update(stopped_at=time.time(), returncode=child.returncode,
                         status='STOPPED' if signaled and child.returncode == 0 and not timed_out and not state.get('forced_stop') else 'FAILED')
            if state['status'] != 'STOPPED':
                state['error'] = 'Capture lifetime expired' if timed_out else 'tcpdump exited unexpectedly'
    except Exception as exc:
        state.update(status='FAILED', error=str(exc), stopped_at=time.time())
        if child and child.poll() is None:
            child.send_signal(signal.SIGINT)
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill(); child.wait(timeout=5)
                state['forced_stop'] = True
    finally:
        text = (root/'tcpdump.log').read_text() if (root/'tcpdump.log').exists() else ''
        for key, pattern in [('captured_packets', r'(\d+) packets captured'), ('dropped_packets', r'(\d+) packets dropped by kernel')]:
            match = re.search(pattern, text)
            state[key] = int(match[1]) if match else None
        save(root/'tcp-capture-state.json', state)


def agent_snapshot(root, cfg, stop=False):
    state = read_state(root)
    # Config includes the exact run, server/port UUIDs, endpoints, and directory.
    if state['config'] != cfg:
        raise RuntimeError('TCP capture ownership/config changed')
    if state['status'] == 'RUNNING':
        if not all(alive(state.get(k)) for k in ('supervisor', 'tcpdump')):
            raise RuntimeError('Original TCP capture process lost; replacement prohibited')
        if stop:
            (root/'stop-request').touch(mode=0o600)
            deadline = time.monotonic()+15
            while state['status'] == 'RUNNING' and time.monotonic() < deadline:
                time.sleep(.1)
                state = read_state(root)
            if state['status'] == 'RUNNING':
                raise TimeoutError('TCP capture stop timed out')
    if state['status'] not in ('RUNNING', 'STOPPED', 'FAILED'):
        raise RuntimeError('TCP capture launch intent unresolved')
    if state['status'] == 'FAILED' and any(alive(state.get(k)) for k in ('supervisor', 'tcpdump')):
        raise RuntimeError('Failed TCP capture still has a live process; archive/inspect before reset')
    return state


def run_readonly(args, **kwargs):
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=15, **kwargs)
        return dict(rc=proc.returncode, stdout=proc.stdout, stderr=proc.stderr,
                    status='PASS' if proc.returncode == 0 else 'UNAVAILABLE')
    except (OSError, subprocess.TimeoutExpired) as exc:
        return dict(rc=None, stdout='', stderr=str(exc), status='UNAVAILABLE')


def conntrack_snapshot(cfg):
    uuid.UUID(cfg['router'])
    ipaddress.IPv4Address(cfg['guest_ip']); ipaddress.IPv4Address(cfg['endpoint_ip'])
    port = int(cfg['tcp_port'])
    if not 1 <= port <= 65535:
        raise ValueError('Invalid TCP port')
    # Kolla's service user cannot enter a qrouter netns. Grant namespace access
    # only to this read-only exec; do not change the container or its NAT rules.
    legacy = run_readonly(['docker', 'exec', '--privileged', '--user', 'root', 'neutron_l3_agent', 'ip', 'netns', 'exec',
                          'qrouter-'+cfg['router'], 'conntrack', '-L', '-p', 'tcp',
                          '-s', cfg['guest_ip'], '-d', cfg['endpoint_ip'], '--dport', str(port), '-o', 'extended'])
    ovs = run_readonly(['docker', 'exec', 'openvswitch_vswitchd', 'ovs-appctl', 'dpctl/dump-conntrack'])
    if ovs['rc'] == 0:
        # Preserve complete tuple and zone for every matching entry, across zones.
        ovs['stdout'] = '\n'.join(line for line in ovs['stdout'].splitlines()
            if 'src='+cfg['guest_ip']+',' in line and 'dst='+cfg['endpoint_ip']+',' in line
            and re.search(r'dport='+str(port)+r'(?:,|\))', line))
    parser = """import json
from oslo_config import cfg
from neutron.conf.agent import common
common.register_iptables_opts(cfg.CONF)
cfg.CONF([], project='neutron', default_config_files=['/etc/neutron/neutron.conf','/etc/neutron/l3_agent.ini'])
print(json.dumps({'use_random_fully':cfg.CONF.AGENT.use_random_fully}))
"""
    profile = run_readonly(['docker', 'exec', '-i', 'neutron_l3_agent', '/var/lib/kolla/venv/bin/python3', '-'], input=parser)
    return dict(ts=time.time(), mono=time.monotonic(), boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                router=cfg['router'], guest_ip=cfg['guest_ip'], endpoint_ip=cfg['endpoint_ip'],
                tcp_port=port, legacy=legacy, ovs=ovs, source_profile=profile, conntrack_transfer=False)


class Diagnostics:
    def __init__(self, extended):
        self.x = extended
        self.cfg = extended.cfg
        self.root = extended.root
        self.path = self.root/'ns-diagnostics-checkpoint.json'
        self.state = json.loads(self.path.read_text()) if self.path.exists() else dict(schema=1, run=self.cfg['run'], captures={})
        if self.state['run'] != self.cfg['run']:
            raise RuntimeError('Diagnostic checkpoint run mismatch')

    @property
    def enabled(self):
        return self.cfg.get('ns', {}).get('enabled') and self.cfg['ns'].get('diagnostics_enabled', False)

    def transport(self, action, cp):
        dest = self.root/'ns-diagnostics'/cp['kind']
        dest.mkdir(parents=True, exist_ok=True, mode=0o700)
        extra = dest/'transport.json'
        save(extra, dict(ns_diag_action=action, ns_diag_checkpoint=cp, ns_diag_local_directory=str(dest)))
        helper = Path(__file__).resolve().parents[1]/'playbooks/ns-diagnostics-tasks.yml'
        proc = subprocess.run(['ansible-playbook', '-i', self.cfg['inventory'], str(helper), '-e', '@'+str(extra)],
                              capture_output=True, text=True, timeout=max(60, self.cfg.get('timeout', 300)))
        (dest/('transport-'+action+'.log')).write_text(proc.stdout+'\n'+proc.stderr)
        if proc.returncode:
            raise RuntimeError('N-S diagnostic '+action+' failed on '+cp['host']+'; see '+str(dest))
        return json.loads((dest/'operation.json').read_text())

    def start(self, kind):
        if not self.enabled:
            return
        ns = self.cfg['ns']
        if kind not in self.state['captures']:
            cfg = dict(run=self.cfg['run'], kind=kind, endpoint_ip=ns['endpoint_ip'], snat_ip=ns['snat_ip'],
                       fip_ip=ns['fip_ip'], guest_ip=ns['outbound_ip'] if kind != 'guest-fip' else ns['inbound_ip'],
                       tcp_port=ns['port'], lifetime=self.cfg['lifetime'], allow_create=True)
            if ns.get('new_port'):
                cfg.update(new_tcp_port=ns['new_port'], icmp_enabled=ns.get('icmp_enabled', False))
            host = 'localhost'
            if kind == 'endpoint':
                cfg['interface'] = ns['capture_interface']
                directory = self.root/'ns-diagnostics'/'endpoint-agent'
            else:
                vm = self.x.state['ns']['vms']['0' if kind == 'guest-snat' else '1']
                self.x.audit(vm)
                cfg.update(server=vm['server'], port=vm['port'], integration_bridge='br-int')
                host = vm['placement']['inventory_host']
                directory = Path(self.cfg['capture_directory'])/self.cfg['run']/('ns-'+kind)
            cfg['directory'] = str(directory)
            checked_config(cfg)
            self.state['captures'][kind] = dict(kind=kind, host=host, config=cfg, status='START_INTENT')
            save(self.path, self.state)
        cp = self.state['captures'][kind]
        if cp['status'] == 'START_INTENT' and cp.get('launch_attempted'):
            raise RuntimeError('Ambiguous diagnostic launch; inspect evidence before retry')
        cp['launch_attempted'] = True
        save(self.path, self.state)
        remote = self.transport('start' if cp['status'] == 'START_INTENT' else 'snapshot', cp)
        if remote['status'] != 'RUNNING':
            raise RuntimeError('N-S TCP capture is not running')
        if cp.get('remote') and any(cp['remote'].get(k) != remote.get(k) for k in ('supervisor', 'tcpdump', 'started_at', 'interface')):
            raise RuntimeError('N-S capture process identity changed')
        cp.update(status=remote['status'], remote=remote)
        save(self.path, self.state)

    def recover_unstarted_endpoint(self):
        if set(self.state['captures']) != {'endpoint'}:
            raise RuntimeError('Retry requires only the pre-launch endpoint checkpoint')
        cp = self.state['captures']['endpoint']
        if cp['status'] != 'START_INTENT' or cp.get('remote') or cp['host'] != 'localhost':
            raise RuntimeError('Retry cannot replace any launched TCP capture')
        expected = self.root/'ns-diagnostics'/'endpoint-agent'
        if Path(cp['config']['directory']).resolve() != expected.resolve():
            raise RuntimeError('Retry endpoint directory identity changed')
        row = self.transport('inspect', cp)
        if row.get('status') != 'NOT_STARTED' or row.get('config') != cp['config']:
            raise RuntimeError('TCP capture has launch evidence; automatic retry prohibited')
        # Preserve the failed intent and inspection before enabling one launch.
        save(self.root/'ns-diagnostics-launch-recovery.json', dict(
            before=json.loads(json.dumps(cp)), inspection=row, recovered_at=time.time()))
        cp['launch_attempted'] = False
        save(self.path, self.state)

    def snapshot_conntrack(self, label):
        if not self.enabled:
            return
        ns = self.cfg['ns']
        rows = []
        for index, host in enumerate(ns.get('network_hosts') or [self.cfg['pair_d_network_host']]):
            cp = dict(kind='conntrack-'+label+'-'+str(index), host=host, config=dict(
                router=self.x.state['ns']['router'], guest_ip=ns['outbound_ip'], endpoint_ip=ns['endpoint_ip'], tcp_port=ns['port']))
            try:
                row = self.transport('conntrack', cp)
            except Exception as exc:
                row = dict(status='UNAVAILABLE', error=str(exc))
            rows.append(dict(inventory_host=host, **row))
        save(self.root/('ns-conntrack-'+label+'.json'), dict(hosts=rows, conntrack_transfer=False))

    def ready(self):
        if not self.enabled:
            return
        if set(self.state['captures']) != {'endpoint', 'guest-snat', 'guest-fip'}:
            raise RuntimeError('Required diagnostic TCP capture missing before freeze')
        for cp in self.state['captures'].values():
            row = self.transport('snapshot', cp)
            if row['status'] != 'RUNNING':
                raise RuntimeError('TCP diagnostic capture ended before freeze')
            if any(cp.get('remote', {}).get(k) != row.get(k) for k in ('supervisor', 'tcpdump', 'started_at', 'interface')):
                raise RuntimeError('Original TCP capture identity changed before freeze')

    def finish(self):
        if not self.enabled:
            return
        errors = []
        for kind, cp in self.state['captures'].items():
            try:
                remote = self.transport('stop', cp)
                if cp.get('remote') and any(cp['remote'].get(k) != remote.get(k) for k in ('supervisor', 'tcpdump', 'started_at', 'interface')):
                    raise RuntimeError('Original TCP capture identity changed')
                cp.update(status=remote['status'], remote=remote)
                if remote['status'] != 'STOPPED' or remote.get('dropped_packets') != 0 or not remote.get('captured_packets'):
                    errors.append(kind+': capture incomplete/empty/dropped packets')
            except Exception as exc:
                errors.append(kind+': '+str(exc))
        save(self.path, self.state)
        expected = {'endpoint', 'guest-snat', 'guest-fip'}
        if set(self.state['captures']) != expected:
            errors.append('Missing required diagnostic capture')
        save(self.root/'ns-diagnostics.json', dict(status='PASS' if not errors else 'UNAVAILABLE', errors=errors,
             captures=self.state['captures'], conntrack_transfer=False,
             scope='TCP/ICMP/ARP captures identify tuples, resets and packet loss; TCP application outage remains client monotonic.'))
        if self.cfg['ns'].get('icmp_enabled'):
            from ns_icmp_evidence import finalize
            try:
                finalize(self.root)
            except Exception as exc:
                errors.append('N-S ICMP evidence: '+str(exc))
                result = json.loads((self.root/'ns-diagnostics.json').read_text())
                result.update(status='UNAVAILABLE', errors=errors)
                save(self.root/'ns-diagnostics.json', result)
        return errors


def reset_stop(backup_root, evidence):
    rows = []
    errors = []
    for path in sorted(Path(backup_root).glob('*/ns-diagnostics-checkpoint.json')):
        root = path.parent
        cfg = json.loads((root/'validation-config.json').read_text())
        x = type('Checkpoint', (), dict(root=root, cfg=cfg))()
        diag = Diagnostics(x)
        if diag.state.get('schema') != 1 or diag.state['run'] != root.name:
            raise RuntimeError('Reset diagnostic run identity mismatch')
        # Stop/fetch every checkpoint without deleting historical evidence.
        for kind, cp in diag.state['captures'].items():
            try:
                if cp['status'] == 'START_INTENT' and not cp.get('remote'):
                    inspected = diag.transport('inspect', cp)
                    if inspected.get('status') == 'NOT_STARTED':
                        rows.append(dict(run=root.name, kind=kind, status='NEVER_STARTED'))
                        continue
                archived = root/'ns-diagnostics'/kind
                # A successfully archived stopped capture needs no remote
                # service or interface from a later lab generation.
                if cp['status'] == 'STOPPED' and (archived/'tcp.pcap').exists() and (archived/'tcp-capture-state.json').exists():
                    prior = json.loads((archived/'tcp-capture-state.json').read_text())
                    if prior.get('status') == 'STOPPED' and prior.get('config') == cp['config'] and all(
                        prior.get(k) == cp.get('remote', {}).get(k) for k in ('supervisor', 'tcpdump', 'started_at', 'interface')):
                        rows.append(dict(run=root.name, kind=kind, status='ALREADY_STOPPED_AND_ARCHIVED'))
                        continue
                remote = diag.transport('stop', cp)
                if remote['status'] not in ('STOPPED', 'FAILED'):
                    raise RuntimeError('Capture is not stopped')
                cp.update(status=remote['status'], remote=remote)
                rows.append(dict(run=root.name, kind=kind, status=remote['status']))
            except Exception as exc:
                errors.append(dict(run=root.name, kind=kind, error=str(exc)))
        save(diag.path, diag.state)
    save(Path(evidence), dict(status='FAIL' if errors else 'PASS', captures=rows, errors=errors))
    if errors:
        raise RuntimeError('Owned TCP captures could not be stopped/archived; reset must not destroy nodes')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['pre-freeze', 'post-cutover', 'post-validation', 'finish', 'reset-stop', 'recover-unstarted',
                                        'agent-start', 'agent-snapshot', 'agent-stop', 'agent-inspect', 'supervise', 'conntrack'])
    parser.add_argument('root', type=Path)
    parser.add_argument('config', nargs='?')
    args = parser.parse_args()
    if args.action == 'reset-stop':
        reset_stop(args.root, args.config)
    elif args.action == 'supervise':
        supervise(args.root)
    elif args.action == 'conntrack':
        print(json.dumps(conntrack_snapshot(json.loads(args.config))))
    elif args.action.startswith('agent-'):
        cfg = json.loads(args.config)
        if args.action == 'agent-inspect':
            result = agent_inspect(args.root, cfg)
        else:
            result = agent_start(args.root, cfg) if args.action == 'agent-start' else agent_snapshot(args.root, cfg, stop=args.action == 'agent-stop')
        print(json.dumps(result))
    else:
        # No OpenStack API is used during freeze or when finalizing captures.
        cfg = json.loads((args.root/'validation-config.json').read_text())
        x = type('Checkpoint', (), dict(root=args.root, cfg=cfg,
            state=json.loads((args.root/'extended-resources.json').read_text())))()
        diag = Diagnostics(x)
        if not diag.enabled:
            return
        if args.action == 'recover-unstarted':
            diag.recover_unstarted_endpoint()
            return
        if args.action == 'pre-freeze':
            diag.ready()
        if args.action == 'finish':
            diag.finish()
        else:
            diag.snapshot_conntrack(args.action)


if __name__ == '__main__':
    main()
