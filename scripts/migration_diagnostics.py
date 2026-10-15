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
    if (type(cfg.get('reconnect_attempts', 8)) is not int or
            not 0 <= cfg.get('reconnect_attempts', 8) <= 20 or
            not 1 <= cfg.get('reconnect_window_seconds', 60) <= 300):
        raise ValueError('Invalid observer reconnect budget')
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
            jobs[label+'-'+table] = dict(container='ovn_northd', reconnect=True, args=['ovsdb-client',
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
        jobs['ovsdb-local'] = dict(container='openvswitch_vswitchd', reconnect=True, args=['ovsdb-client',
            '--timestamp', '--format=json', 'monitor', 'unix:/run/openvswitch/db.sock', 'Open_vSwitch', 'ALL'])
        # Flow monitor is a service connection; it never becomes the primary controller.
        # Bridge/controller reconfiguration can close this service connection.
        jobs['openflow-watch'] = dict(container='openvswitch_vswitchd', reconnect=True, args=['ovs-ofctl',
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
        self.segments = {}
        self.gaps = {}
        self.reconnect_counts = {}
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

    def start_job(self, name, spec, gap=None):
        token = uuid.uuid4().hex
        args = list(spec['args'])
        if gap and name == 'openflow-watch':
            args[-1] = 'watch:'  # Retain the new connection's initial flow snapshot.
        if spec.get('container'):
            args = ['docker', 'exec', '-e', 'MIGRATION_DIAG_TOKEN='+token, spec['container'],
                'sh', '-c', 'echo DIAG_PID=$$; exec "$@"', 'sh']+args
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors='replace', bufsize=1, start_new_session=True)
        row = dict(spec=spec, token=token, process=proc, identity=identity(proc.pid), inner_pid=None,
            started_at=time.time(), started_mono=time.monotonic(), connected_at=None,
            last_output_at=None, gap=gap, segment=len(self.segments.get(name, [])))
        self.children[name] = row
        self.segments.setdefault(name, []).append(row)
        self.record('observer-start', observer=name, segment=row['segment'], command=args[-len(spec['args']):])
        def output():
            with (self.root/(name+'.jsonl')).open('a') as f:
                for line in proc.stdout:
                    if line.startswith('DIAG_PID=') and line.strip()[9:].isdigit():
                        row['inner_pid'] = int(line.strip()[9:])
                    else:
                        now, mono = time.time(), time.monotonic()
                        row['last_output_at'] = now
                        f.write(json.dumps(dict(ts=now, mono=mono, segment=row['segment'], text=line.rstrip()))+'\n')
                        f.flush()
                        ready = ('FLOW_MONITOR reply' in line if name == 'openflow-watch'
                                 else line.lstrip().startswith(('{', '[')))
                        if spec.get('reconnect') and ready and row['connected_at'] is None:
                            row['connected_at'] = now
                            if gap and gap.get('end_timestamp') is None:
                                gap.update(end_timestamp=now, duration_seconds=mono-gap['detected_mono'])
                                self.record('observer-reconnected', observer=name, segment=row['segment'], gap=dict(gap))
        thread = threading.Thread(target=output, daemon=True)
        row['thread'] = thread
        thread.start()

    def reconnect_jobs(self, executor):
        """Restart only observer connections; retain every gap as incomplete coverage."""
        for name, row in list(self.children.items()):
            if row['process'].poll() is None:
                if row.get('gap') and row.get('connected_at') is None and time.monotonic()-row['started_mono'] >= 10:
                    self.stop_job(name, row, verify=True)
                    self.record('observer-reconnect-ack-timeout', observer=name, segment=row['segment'])
                    continue
                if row.get('gap') and row.get('connected_at') and not row.get('snapshot_queued'):
                    row['snapshot_queued'] = True
                    executor.submit(self.snapshot, 'observer-reconnected-'+name)
                continue
            gap = row.get('gap')
            if not row.get('gap_reported'):
                row['gap_reported'] = True
                if not gap or gap.get('end_timestamp') is not None:
                    gap = dict(detected_at=time.time(), detected_mono=time.monotonic(),
                        last_output_at=row.get('last_output_at'), end_timestamp=None, duration_seconds=None,
                        rc=row['process'].returncode, segment=row['segment'])
                    self.gaps.setdefault(name, []).append(gap)
                    row['gap'] = gap
                if not row['spec'].get('optional') and name not in self.errors:
                    self.errors.append(name)
                self.record('observer-gap', observer=name, gap=dict(gap))
            if not row['spec'].get('reconnect') or row.get('reconnect_exhausted'):
                continue
            count = self.reconnect_counts.get(name, 0)
            if (count >= self.cfg.get('reconnect_attempts', 8) or
                    time.monotonic()-gap['detected_mono'] >= self.cfg.get('reconnect_window_seconds', 60)):
                row['reconnect_exhausted'] = True
                self.record('observer-reconnect-exhausted', observer=name, attempts=count, gap=dict(gap))
                continue
            if time.monotonic() < row.setdefault('retry_at', time.monotonic()+min(8, 2**count)):
                continue
            if not self.stop_job(name, row, verify=True):
                row['retry_at'] = time.monotonic()+min(8, 2**count)
                self.reconnect_counts[name] = count+1
                self.record('observer-reconnect-cleanup-unproven', observer=name, attempts=count+1)
                continue
            self.reconnect_counts[name] = count+1
            self.start_job(name, row['spec'], gap=gap)

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

    def stop_job(self, name, row, verify=False):
        proc, spec = row['process'], row['spec']
        retired = True
        if spec.get('container') and (row.get('inner_pid') or verify):
            # A lost docker exec may leave an orphan. Match only this launch's
            # unique environment token, even when its PID acknowledgement was lost.
            code = '''t="$2"
matches() { test -r "/proc/$1/environ" && tr '\\000' '\\n' 2>/dev/null < "/proc/$1/environ" | grep -Fx "MIGRATION_DIAG_TOKEN=$t" >/dev/null; }
owned=""
if test -n "$1"; then
 p="$1"; test -d "/proc/$p" || exit 0
 current_env=$(tr '\\000' '\\n' 2>/dev/null < "/proc/$p/environ") || exit 1
 printf '%s\\n' "$current_env" | grep -Fx "MIGRATION_DIAG_TOKEN=$t" >/dev/null || exit 0
 owned="$p"
else
 for d in /proc/[0-9]*; do p="${d##*/}"; if matches "$p"; then owned="$owned $p"; fi; done
fi
for p in $owned; do if matches "$p"; then kill -TERM "$p" 2>/dev/null || true; fi; done
for n in 1 2 3 4 5 6 7 8 9 10; do
 left=0; for p in $owned; do if matches "$p"; then left=1; fi; done
 test "$left" = 0 && exit 0
 sleep 0.1
done
for p in $owned; do if matches "$p"; then kill -KILL "$p" 2>/dev/null || true; fi; done
sleep 0.1
for p in $owned; do if matches "$p"; then exit 1; fi; done
exit 0'''
            result = command(['docker', 'exec', spec['container'], 'sh', '-c', code,
                              'sh', str(row.get('inner_pid') or ''), row['token']])
            self.record('observer-inner-stop', observer=name, result=result)
            retired = result.get('rc') == 0
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
        return retired and not row['thread'].is_alive()

    def status(self, state='RUNNING'):
        jobs = {name: dict(identity=row['identity'], rc=row['process'].poll(),
            inner_pid=row.get('inner_pid'), container=row['spec'].get('container'),
            optional=row['spec'].get('optional', False), capture=row.get('capture'),
            reconnect_attempts=self.reconnect_counts.get(name, 0), gaps=list(self.gaps.get(name, [])),
            segments=[dict(segment=s['segment'], identity=s['identity'], started_at=s['started_at'],
                connected_at=s.get('connected_at'), rc=s['process'].poll()) for s in self.segments.get(name, [])])
            for name, row in self.children.items()}
        return dict(status=state, run=self.cfg['run'], config=self.cfg,
            identity=identity(os.getpid()), heartbeat_ts=time.time(), jobs=jobs, errors=list(self.errors),
            stopped=state != 'RUNNING', finished_ts=time.time() if state != 'RUNNING' else None,
            cleanup_complete=getattr(self, 'cleanup_complete', False),
            coverage='GAPPED' if self.gaps else 'NO_OBSERVED_GAPS')

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
                if not self.stop.is_set() and not (self.root/'stop.request').exists():
                    self.reconnect_jobs(executor)
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
            self.cleanup_complete = True
            for name, row in self.children.items():
                if not self.stop_job(name, row, verify=True):
                    self.cleanup_complete = False
                    self.errors.append(name+': observer cleanup not proven')
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
