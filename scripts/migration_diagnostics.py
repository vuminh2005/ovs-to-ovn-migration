#!/usr/bin/env python3
"""Bounded host-side observers. No flow, NAT, binding or network configuration writes."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import glob
import json
import ipaddress
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
import uuid


def save(path, value):
    path = Path(path)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, indent=2))
    temp.chmod(0o600)
    temp.replace(path)


def identity(pid):
    try:
        path = '/proc/self/stat' if pid == os.getpid() else '/proc/'+str(pid)+'/stat'
        text = Path(path).read_text()
        stat = text.rsplit(')', 1)[1].split()
        if stat[0] == 'Z':
            return None
        return dict(pid=int(text.split(' ', 1)[0]), start_ticks=stat[19],
                    boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip())
    except (OSError, IndexError):
        return None


def alive(saved):
    return bool(saved) and identity(saved['pid']) == saved


def checked(cfg, root):
    for key in ('run', 'host'):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', cfg[key]):
            raise ValueError('Unsafe diagnostics '+key)
    if cfg['role'] not in ('core', 'central') or not Path(root).is_absolute():
        raise ValueError('Invalid collector role/directory')
    if not 30 <= cfg['lifetime'] <= 86400 or not 1048576 <= cfg['max_bytes'] <= 1073741824:
        raise ValueError('Invalid collector lifetime/byte budget')
    for key in ('tunnel_interface', 'external_interface'):
        if cfg.get(key) and not re.fullmatch(r'[A-Za-z0-9_.:-]{1,32}', cfg[key]):
            raise ValueError('Unsafe capture interface')
    if cfg.get('ns_enabled'):
        ipaddress.IPv4Address(cfg['endpoint_ip'])
    return cfg


def command(args, timeout=5):
    start = dict(ts=time.time(), mono=time.monotonic())
    try:
        result = subprocess.run(args, capture_output=True, text=True, errors='replace', timeout=timeout)
        return dict(start, end_ts=time.time(), rc=result.returncode,
            status='PASS' if result.returncode == 0 else 'UNAVAILABLE',
            stdout=result.stdout[:1048576], stderr=result.stderr[:16384])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return dict(start, end_ts=time.time(), status='UNAVAILABLE', error=str(exc))


NB_TABLES = {
    'Logical_Router': ['name', 'ports', 'nat', 'options'],
    'Logical_Router_Port': ['name', 'gateway_chassis', 'ha_chassis_group', 'options', 'mac', 'networks'],
    'Gateway_Chassis': ['name', 'chassis_name', 'priority'],
    'HA_Chassis_Group': ['name', 'ha_chassis'], 'HA_Chassis': ['chassis_name', 'priority'],
    'NAT': ['type', 'external_ip', 'logical_ip', 'logical_port', 'external_mac'],
    'NB_Global': ['nb_cfg', 'sb_cfg', 'hv_cfg'],
}
SB_TABLES = {
    'Chassis': ['name', 'hostname', 'other_config', 'encaps'],
    'Chassis_Private': ['name', 'chassis', 'nb_cfg', 'nb_cfg_timestamp'],
    'Port_Binding': ['logical_port', 'type', 'chassis', 'up', 'options', 'datapath', 'mac'],
    'MAC_Binding': ['logical_port', 'ip', 'mac', 'datapath'],
    'SB_Global': ['nb_cfg'],
}


def database_jobs(cfg):
    jobs, schemas = {}, {}
    for label, database, uri, wanted in (
        ('nb', 'OVN_Northbound', cfg['nb'], NB_TABLES),
        ('sb', 'OVN_Southbound', cfg['sb'], SB_TABLES),
    ):
        result = command(['docker', 'exec', 'ovn_northd', 'ovsdb-client', 'get-schema', uri, database])
        if result['status'] != 'PASS':
            raise RuntimeError(label+' schema unavailable: '+str(result))
        schema = json.loads(result['stdout'])
        schemas[label] = schema
        for table, columns in wanted.items():
            if table not in schema['tables']:
                continue
            available = schema['tables'][table]['columns']
            columns = ['_uuid']+[c for c in columns if c in available]
            jobs[label+'-'+table] = dict(container='ovn_northd', args=['ovsdb-client',
                '--timestamp', '--format=json', 'monitor', uri, database, table, ','.join(columns)])
    return jobs, schemas


def core_jobs(cfg, root):
    jobs = {'docker-events': dict(args=['docker', 'events', '--since', str(int(time.time())),
        '--format', '{{json .}}', '--filter', 'type=container'])}
    if shutil.which('journalctl'):
        units = ['kolla-neutron_server-container.service', 'kolla-neutron_openvswitch_agent-container.service',
            'kolla-neutron_l3_agent-container.service', 'kolla-ovn_controller-container.service',
            'kolla-openvswitch_vswitchd-container.service', 'kolla-openvswitch_db-container.service']
        jobs['systemd-journal'] = dict(optional=True, args=['journalctl', '--follow', '--since', 'now',
            '--no-pager', '--output=json']+[item for unit in units for item in ('-u', unit)])
    if cfg.get('chassis'):
        jobs['ovsdb-local'] = dict(container='openvswitch_vswitchd', args=['ovsdb-client',
            '--timestamp', '--format=json', 'monitor', 'unix:/run/openvswitch/db.sock', 'Open_vSwitch', 'ALL'])
        # Flow monitor is a service connection; it never becomes the primary controller.
        # OF1.0 Nicira watch works beside the OF1.5 OVN controller without changing Bridge.protocols.
        jobs['openflow-watch'] = dict(container='openvswitch_vswitchd', args=['ovs-ofctl',
            '--timestamp', '-O', 'OpenFlow10', 'monitor', 'br-int', 'watch:!initial'])
        jobs['tunnel-pcap'] = dict(args=['tcpdump', '-n', '-U', '-s', '0', '-i', cfg['tunnel_interface'],
            '-w', str(root/'tunnel.pcap'), 'udp port 4789 or udp port 6081'])
    if cfg.get('network') and cfg.get('ns_enabled'):
        endpoint = cfg['endpoint_ip']
        # Retain ARP/GARP plus application traffic on the actual external OVS port.
        bpf = 'arp or ((icmp or (tcp and (port '+str(cfg['old_port'])+' or port '+str(cfg['new_port'])+')))'+' and host '+endpoint+')'
        jobs['gateway-pcap'] = dict(args=['tcpdump', '-n', '-U', '-s', '0', '-i', cfg['external_interface'],
            '-w', str(root/'gateway.pcap'), bpf])
    return jobs


class Observer:
    def __init__(self, root, cfg):
        self.root, self.cfg = Path(root), cfg
        self.boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.children = {}
        self.offsets = {}
        self.errors = []
        self.events = self.root/'events.jsonl'
        self.started = time.monotonic()
        self.intensive = False
        self.last_sample = 0

    def record(self, name, **value):
        with self.lock, self.events.open('a') as f:
            f.write(json.dumps(dict(run=self.cfg['run'], host=self.cfg['host'],
                boot=self.boot, ts=time.time(), mono=time.monotonic(), event=name, **value))+'\n')

    def start_job(self, name, spec):
        token = uuid.uuid4().hex
        args = list(spec['args'])
        if spec.get('container'):
            args = ['docker', 'exec', '-e', 'MIGRATION_DIAG_TOKEN='+token, spec['container'],
                'sh', '-c', 'echo DIAG_PID=$$; exec "$@"', 'sh']+args
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors='replace', bufsize=1, start_new_session=True)
        row = dict(spec=spec, token=token, process=proc, identity=identity(proc.pid), inner_pid=None)
        self.children[name] = row
        self.record('observer-start', observer=name, command=spec['args'])
        def output():
            with (self.root/(name+'.jsonl')).open('a') as f:
                for line in proc.stdout:
                    if line.startswith('DIAG_PID=') and line.strip()[9:].isdigit():
                        row['inner_pid'] = int(line.strip()[9:])
                    else:
                        f.write(json.dumps(dict(ts=time.time(), mono=time.monotonic(), text=line.rstrip()))+'\n')
                        f.flush()
        thread = threading.Thread(target=output, daemon=True)
        row['thread'] = thread
        thread.start()

    def tail_logs(self):
        patterns = tuple('/var/log/kolla/'+service+'/'+pattern for service in
            ('neutron', 'ovn', 'openvswitch') for pattern in ('*.log', '*.log.[0-9]'))
        for pattern in patterns:
            for name in glob.glob(pattern):
                path = Path(name)
                try:
                    stat = path.stat()
                    key = (stat.st_dev, stat.st_ino)
                    if key not in self.offsets:
                        # Initial files start at EOF; new/rotated files start at byte zero.
                        self.offsets[key] = stat.st_size if not getattr(self, 'logs_initialized', False) else 0
                        self.record('log-cursor', path=name, inode=stat.st_ino, offset=self.offsets[key])
                    offset = self.offsets[key]
                    if stat.st_size < offset:
                        self.record('log-truncated', path=name, old_offset=offset)
                        offset = 0
                    with path.open('rb') as f:
                        f.seek(offset)
                        chunk = f.read(262144)
                        self.offsets[key] = f.tell()
                    if chunk:
                        self.record('service-log', path=name, inode=stat.st_ino, offset=offset,
                            text=chunk.decode(errors='replace'))
                except OSError as exc:
                    self.record('log-unavailable', path=name, error=str(exc))
        self.logs_initialized = True

    def snapshot(self, label):
        cmds = {'clock': ['date', '--iso-8601=ns'], 'clock-offset': ['chronyc', 'tracking'],
                'neighbors': ['ip', '-j', 'neigh'], 'links': ['ip', '-j', 'link'],
                'fdb': ['bridge', '-j', 'fdb', 'show']}
        if self.cfg.get('chassis'):
            base = ['docker', 'exec', 'openvswitch_vswitchd']
            cmds.update({
                'ovs-version': base+['ovs-vswitchd', '--version'],
                'ovs-flows': base+['ovs-ofctl', '-O', 'OpenFlow10,OpenFlow13,OpenFlow15', 'dump-flows', 'br-int'],
                'ovs-groups': base+['ovs-ofctl', '-O', 'OpenFlow13,OpenFlow15', 'dump-groups', 'br-int'],
                'ovs-ports': base+['ovs-ofctl', '-O', 'OpenFlow13,OpenFlow15', 'dump-ports', 'br-int'],
                'conntrack': base+['ovs-appctl', 'dpctl/dump-conntrack'],
                'ovn-version': ['docker', 'exec', 'ovn_controller', 'ovn-controller', '--version'],
            })
        if self.cfg.get('control'):
            cmds['neutron-version'] = ['docker', 'exec', 'neutron_server', 'python3', '-c',
                'import importlib.metadata; print(importlib.metadata.version("neutron"))']
        with ThreadPoolExecutor(max_workers=3) as pool:
            for key, value in zip(cmds, pool.map(command, cmds.values())):
                self.record('snapshot', label=label, source=key, result=value)

    def sample(self):
        cmds = {'neighbors': ['ip', '-j', 'neigh']}
        if self.cfg.get('chassis'):
            base = ['docker', 'exec', 'openvswitch_vswitchd']
            cmds['conntrack'] = base+['ovs-appctl', 'dpctl/dump-conntrack']
            cmds['port-counters'] = base+['ovs-ofctl', '-O', 'OpenFlow13,OpenFlow15', 'dump-ports', 'br-int']
        for name, args in cmds.items():
            result = command(args, timeout=2)
            if name == 'conntrack' and result.get('stdout'):
                endpoint = self.cfg.get('endpoint_ip', '')
                result['stdout'] = '\n'.join(line for line in result['stdout'].splitlines() if endpoint and endpoint in line)
            self.record('sample', source=name, result=result)

    def stop_job(self, name, row):
        proc, spec = row['process'], row['spec']
        if spec.get('container') and row.get('inner_pid'):
            # Guard against PID reuse: signal only the process bearing this launch's secret token.
            code = 'p="$1"; t="$2"; test -r /proc/"$p"/environ || exit 0; tr "\\000" "\\n" < /proc/"$p"/environ | grep -Fx "MIGRATION_DIAG_TOKEN=$t" >/dev/null && kill -TERM "$p"'
            result = command(['docker', 'exec', spec['container'], 'sh', '-c', code,
                              'sh', str(row['inner_pid']), row['token']])
            self.record('observer-inner-stop', observer=name, result=result)
        if proc.poll() is None:  # Unreaped Popen child cannot be replaced by an unrelated PID.
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
        row['thread'].join(timeout=2)
        if name.endswith('-pcap'):
            text = (self.root/(name+'.jsonl')).read_text()
            parsed = {}
            for key, pattern in (('captured_packets', r'(\d+) packets captured'), ('dropped_packets', r'(\d+) packets dropped by kernel')):
                matches = re.findall(pattern, text)
                parsed[key] = int(matches[-1]) if matches else None
            row['capture'] = parsed
            if parsed['dropped_packets'] != 0:
                self.errors.append(name+': capture drop count unavailable/nonzero')
        self.record('observer-stop', observer=name, rc=proc.poll())

    def status(self, state='RUNNING'):
        jobs = {name: dict(identity=row['identity'], rc=row['process'].poll(),
            inner_pid=row.get('inner_pid'), container=row['spec'].get('container'),
            optional=row['spec'].get('optional', False), capture=row.get('capture'))
            for name, row in self.children.items()}
        return dict(status=state, run=self.cfg['run'], config=self.cfg,
            identity=identity(os.getpid()), heartbeat_ts=time.time(), jobs=jobs, errors=list(self.errors),
            stopped=state != 'RUNNING', finished_ts=time.time() if state != 'RUNNING' else None)

    def run(self):
        signal.signal(signal.SIGTERM, lambda *_: self.stop.set())
        signal.signal(signal.SIGINT, lambda *_: self.stop.set())
        self.tail_logs()
        executor = ThreadPoolExecutor(max_workers=1)
        seen = set()
        try:
            if self.cfg['role'] == 'central':
                jobs, schemas = database_jobs(self.cfg)
                save(self.root/'schemas.json', schemas)
            else:
                jobs = core_jobs(self.cfg, self.root)
            for name, spec in jobs.items():
                self.start_job(name, spec)
            executor.submit(self.snapshot, 'collector-start')
            while not self.stop.is_set() and time.monotonic()-self.started < self.cfg['lifetime']:
                self.tail_logs()
                for request in self.root.glob('snapshot-*.request'):
                    if request.name not in seen:
                        seen.add(request.name)
                        label = request.read_text().strip()
                        if label == 'pre-freeze':
                            self.intensive = True
                        executor.submit(self.snapshot, label)
                if self.intensive and time.monotonic()-self.last_sample >= 2:
                    # Do not queue expensive work faster than it can complete.
                    if not getattr(self, 'sample_future', None) or self.sample_future.done():
                        self.sample_future = executor.submit(self.sample)
                        self.last_sample = time.monotonic()
                for name, row in self.children.items():
                    if row['process'].poll() is not None and not row.get('gap_reported'):
                        row['gap_reported'] = True
                        if not row['spec'].get('optional'):
                            self.errors.append(name)
                        self.record('observer-gap', observer=name, rc=row['process'].returncode)
                size = sum(p.stat().st_size for p in self.root.iterdir() if p.is_file())
                if size >= self.cfg['max_bytes']:
                    self.errors.append('byte-budget-exhausted')
                    break
                save(self.root/'state.json', self.status())
                if (self.root/'stop.request').exists():
                    break
                self.stop.wait(.5)
            if time.monotonic()-self.started >= self.cfg['lifetime']:
                self.errors.append('lifetime-exhausted')
        except Exception as exc:
            self.errors.append(str(exc))
            self.record('collector-error', error=str(exc))
        finally:
            for name, row in self.children.items():
                self.stop_job(name, row)
            executor.shutdown(wait=True, cancel_futures=True)
            self.tail_logs()
            save(self.root/'state.json', self.status('UNAVAILABLE' if self.errors else 'STOPPED'))


def operate(action, root, cfg=None, label='checkpoint'):
    root = Path(root)
    if action == 'start':
        checked(cfg, root)
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
        path = root/'config.json'
        if path.exists():
            if json.loads(path.read_text()) != cfg:
                raise RuntimeError('Collector ownership/config changed')
            return operate('check', root)
        if any(p.name not in ('migration_diagnostics.py',) for p in root.iterdir()):
            raise RuntimeError('Existing diagnostics evidence will not be overwritten')
        save(path, cfg)
        with (root/'supervisor.log').open('ab') as log:
            proc = subprocess.Popen([sys.executable, str(root/'migration_diagnostics.py'), 'run', str(root)],
                stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
        save(root/'launch.json', dict(identity=identity(proc.pid), ts=time.time()))
        deadline = time.monotonic()+20
        while not (root/'state.json').exists() and time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError('Collector exited during launch')
            time.sleep(.1)
        ready = operate('check', root)
        # The child reports its own /proc identity; do not assume PID namespace mappings.
        save(root/'launch.json', dict(identity=ready['identity'], ts=time.time()))
        return ready
    cfg = json.loads((root/'config.json').read_text())
    checked(cfg, root)
    if action == 'run':
        Observer(root, cfg).run()
        return {}
    if action == 'snapshot':
        (root/('snapshot-'+uuid.uuid4().hex+'.request')).write_text(label)
        return dict(status='REQUESTED', label=label, ts=time.time())
    if action == 'stop':
        (root/'stop.request').touch(mode=0o600)
        launch = json.loads((root/'launch.json').read_text())
        deadline = time.monotonic()+60
        while alive(launch['identity']) and time.monotonic() < deadline:
            time.sleep(.1)
        if alive(launch['identity']):
            raise RuntimeError('Original diagnostics collector did not stop')
    state = json.loads((root/'state.json').read_text())
    if action == 'check' and (state['status'] != 'RUNNING' or state['errors'] or
            not alive(state.get('identity')) or time.time()-state['heartbeat_ts'] > 15):
        raise RuntimeError('Diagnostics incomplete: '+str(state))
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['start', 'run', 'check', 'snapshot', 'stop'])
    parser.add_argument('root', type=Path)
    parser.add_argument('--config')
    parser.add_argument('--label', default='checkpoint')
    args = parser.parse_args()
    print(json.dumps(operate(args.action, args.root,
        json.loads(args.config) if args.config else None, args.label)))


if __name__ == '__main__':
    main()
