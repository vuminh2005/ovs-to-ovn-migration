#!/usr/bin/env python3
"""One evidenced recovery attempt in the confirmed, freshly deployed reset lab."""

# Source helpers are grouped; remote/guest copies keep flat sibling imports.
import sys as _sys
from pathlib import Path as _Path
_source = _Path(__file__).resolve().parents[1]
if _source.name == 'scripts' and (_source / '_paths.py').is_file():
    _sys.path.insert(0, str(_source))
    from _paths import configure as _configure
    _configure()
import argparse
from datetime import datetime, timezone
import gzip
import json
import os
from pathlib import Path
import re
import subprocess
import time

import reset_rabbitmq_health as health


WORKERS_CODE = ('import configparser,json; c=configparser.ConfigParser(interpolation=None); '
                'c.read("/etc/nova/nova.conf"); '
                'print(json.dumps({"workers":c.getint("scheduler","workers")}))')
UNITS = {name: 'kolla-'+name+'-container.service' for name in ('rabbitmq', 'nova_scheduler')}
NOVA_LOG = Path('/var/log/kolla/nova/nova-scheduler.log')


def command(args, timeout=20):
    started = health.utc()
    try:
        proc = subprocess.run(args, capture_output=True, text=True, errors='replace', timeout=timeout)
        return dict(started_at=started, ended_at=health.utc(), rc=proc.returncode,
                    stdout=proc.stdout[:1048576], stderr=proc.stderr[:16384])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return dict(started_at=started, ended_at=health.utc(), rc=124,
                    stdout='', stderr=str(exc))


def read(root, name):
    return json.loads((root/name).read_text())


def verify_workers(root, expected, service='nova_scheduler'):
    if type(expected) is not int or expected < 1:
        raise ValueError('Expected Nova worker count must be a positive integer')
    if service not in ('nova_scheduler', 'nova_conductor'):
        raise ValueError('Unknown Nova worker service')
    section = 'scheduler' if service == 'nova_scheduler' else 'conductor'
    filename = 'nova-'+section+'-workers.json'
    result = command(['docker', 'exec', service, 'python3', '-c',
                      WORKERS_CODE.replace('"scheduler"', '"'+section+'"')])
    actual = None
    try:
        if result['rc'] == 0:
            actual = json.loads(result['stdout'])['workers']
    except (ValueError, KeyError, TypeError):
        pass
    value = dict(status='PASS' if type(actual) is int and actual == expected else 'FAIL',
                 expected_workers=expected, effective_workers=actual, observed_at=health.utc(), check=result)
    health.save(root/filename, value)
    if value['status'] != 'PASS':
        raise RuntimeError('Effective Nova '+section+' workers differ; inspect '+filename)
    return value


def last_complete(data):
    return health.last_complete(data)


def eligible(data):
    row = last_complete(data)
    if data.get('status') != 'FAIL' or not row:
        return False
    if any(row['checks'][n].get('rc') != 0 for n in ('running', 'alarms', 'queues', 'conductor')):
        return False
    allowed = {'scheduler check failed', 'scheduler must be a running quorum queue with consumers',
               'New quorum/Raft errors during readiness observation'}
    if set(row.get('issues', []))-allowed:
        return False
    if row.get('members', {}).get('conductor') != [dict(node='rabbit@controller', state='leader', membership='voter')]:
        return False
    queues = row.get('queues', {})
    if health.queue_issues(queues) not in ([], ['scheduler must be a running quorum queue with consumers']):
        return False
    q = queues.get('scheduler', {})
    if row['checks']['scheduler'].get('rc') == 0 and q.get('consumers', 0) > 0:
        return False
    errors = row.get('log', {}).get('errors', [])
    if row.get('log', {}).get('continuity') != 'CONTINUOUS':
        return False
    # Never restart for historical errors, another queue, alarms or parser/transport failures.
    if any('failed to form a new cluster' in s and '%2F_scheduler' not in s for s in errors):
        return False
    if any('cluster_not_formed' in s and "queue 'scheduler'" not in s for s in errors):
        return False
    return any('already_started' in s for s in errors) and any(
        'cluster_not_formed' in s and "queue 'scheduler'" in s for s in errors)


class Recovery:
    def __init__(self, root, workers, log, timeout=180, stable_seconds=15):
        self.root = Path(root).resolve()
        self.workers, self.log = workers, Path(log)
        self.timeout, self.stable_seconds = timeout, stable_seconds
        self.report = dict(status='PREFLIGHT', started_at=health.utc(), events=[],
                           automatic_attempt_limit=1, scheduler_may_be_stopped=False)

    def record(self, action, **values):
        self.report['events'].append(dict(action=action, at=health.utc(), **values))
        health.save(self.root/'rabbitmq-recovery.json', self.report)

    def run_command(self, action, args, timeout=20, required=True):
        result = command(args, timeout)
        self.record(action, result=result)
        if required and result['rc'] != 0:
            raise RuntimeError(action+' failed; inspect rabbitmq-recovery.json')
        return result

    def scope(self):
        policy, deploy = read(self.root, 'reset-rabbitmq-policy.json'), read(self.root, 'deploy-result.json')
        storage = read(self.root, 'rabbitmq-storage-after-destroy-controller.json')
        if (policy.get('scope') != 'four-node-lab-reset' or policy.get('confirmed') is not True or
            policy.get('control_hosts') != ['controller'] or policy.get('scheduler_workers') != self.workers or
            deploy.get('rc') != 0 or deploy.get('inventory') != policy.get('inventory') or
            storage.get('rc') != 0 or storage.get('host') != 'controller' or
            storage.get('remaining_rabbitmq_volumes') != []):
            raise RuntimeError('Recovery requires confirmed single-controller reset, successful deploy and clean storage evidence')
        if Path(policy['inventory']).read_bytes() != (self.root/'multinode.before').read_bytes():
            raise RuntimeError('Inventory changed after reset')
        initial = read(self.root, 'rabbitmq-health.json')
        ended = datetime.fromisoformat(initial['ended_at']).timestamp()
        if not -10 <= time.time()-ended <= 600 or not eligible(initial):
            raise RuntimeError('No recent, isolated scheduler already_started/cluster_not_formed failure')
        verify_workers(self.root, self.workers)
        self.record('scope-verified', inventory=policy['inventory'], initial_health='rabbitmq-health.json',
                    last_complete_issues=last_complete(initial)['issues'])

    def unit(self, name):
        result = self.run_command('inspect-unit-'+name, ['systemctl', 'show', UNITS[name],
            '--property=Id,LoadState,ActiveState,ExecStart,ExecStop'])
        fields = dict(s.split('=', 1) for s in result['stdout'].splitlines() if '=' in s)
        if (fields.get('Id') != UNITS[name] or fields.get('LoadState') != 'loaded' or
            '/usr/bin/docker' not in fields.get('ExecStart', '') or
            not re.search(r'\bstart -a '+re.escape(name)+r'(?:\s|;|$)', fields.get('ExecStart', '')) or
            not re.search(r'\bstop '+re.escape(name)+r'(?:\s|;|$)', fields.get('ExecStop', ''))):
            raise RuntimeError('Unrecognized Kolla systemd ownership for '+name)

    def state(self, name):
        result = self.run_command('container-state-'+name, ['docker', 'inspect', '--format',
            '{{json .State}}', name], timeout=8)
        return json.loads(result['stdout'])

    def wait_container(self, name, running, seconds=90):
        deadline = time.monotonic()+seconds
        while time.monotonic() < deadline:
            state = self.state(name)
            if state.get('Running') is running and not state.get('Restarting'):
                if not running:
                    return
                result = self.run_command('rabbit-running', ['docker', 'exec', name,
                    'rabbitmq-diagnostics', '-q', 'check_running'], timeout=8, required=False)
                if result['rc'] == 0:
                    return
            time.sleep(min(2, max(0, deadline-time.monotonic())))
        raise RuntimeError(name+' did not reach expected running='+str(running))

    def capture(self, label):
        directory = self.root/('rabbitmq-recovery-'+label)
        directory.mkdir(mode=0o700)
        for name in ('rabbitmq', 'nova_scheduler'):
            result = self.run_command('capture-'+label+'-'+name, ['docker', 'inspect', '--format',
                '{"image":{{json .Config.Image}},"image_id":{{json .Image}},"state":{{json .State}},"mounts":{{json .Mounts}}}', name])
            health.save(directory/(name+'.json'), json.loads(result['stdout']))
            if label == 'before' and name == 'rabbitmq':
                mounts = [m for m in json.loads(result['stdout']).get('mounts', [])
                          if m.get('Destination') == '/var/lib/rabbitmq']
                if len(mounts) != 1 or mounts[0].get('Type') != 'volume' or mounts[0].get('Name') != 'rabbitmq':
                    raise RuntimeError('Recovery requires the reset lab standard RabbitMQ named volume')
        for path in (self.log, NOVA_LOG):
            size = 0
            with path.open('rb') as source, gzip.open(directory/(path.name+'.gz'), 'wb') as target:
                while chunk := source.read(1048576):
                    size += len(chunk)
                    if size > 134217728:
                        raise RuntimeError('Recovery log exceeds 128 MiB evidence limit: '+str(path))
                    target.write(chunk)
            self.record('log-preserved', phase=label, source=str(path), bytes=size)

    def current_failure(self):
        cursor = health.LogCursor(self.log)
        cursor.poll()
        row = health.snapshot(time.monotonic()+45, 8, 'rabbit@controller')
        row['log'] = cursor.poll()
        self.record('live-recheck', sample=row)
        if not row['issues'] and not row['log']['errors']:
            return False
        if not eligible(dict(status='FAIL', samples=[row])):
            raise RuntimeError('Live failure changed or no new scheduler Raft errors; automatic restart refused')
        return True

    def recover(self):
        if (self.root/'rabbitmq-recovery-attempt.json').exists() or (self.root/'rabbitmq-recovery.json').exists():
            raise RuntimeError('Recovery already evaluated/attempted for this reset; evidence will not be overwritten')
        try:
            self.scope()
            for name in UNITS:
                self.unit(name)
            self.capture('before')
            if not self.current_failure():
                final = health.wait(self.root/'rabbitmq-health-after-recovery.json', self.log,
                                    timeout=self.timeout, stable_seconds=self.stable_seconds)
                self.report.update(status=final['status'], finished_at=health.utc(),
                    outcome='Recovered before service intervention', attempted=False,
                    health_after='rabbitmq-health-after-recovery.json')
                self.record('verification-without-restart')
                return self.report
            # Atomic, durable intent before the first service mutation. No second attempt, even after interruption.
            fd = os.open(self.root/'rabbitmq-recovery-attempt.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump(dict(started_at=health.utc(), scope='four-node-lab-reset', limit=1), stream)
                stream.flush()
                os.fsync(stream.fileno())
            self.report.update(status='RECOVERING', attempted=True, scheduler_may_be_stopped=True)
            self.record('attempt-consumed')
            self.run_command('stop-scheduler-unit', ['systemctl', 'stop', UNITS['nova_scheduler']], timeout=120)
            self.wait_container('nova_scheduler', False, seconds=20)
            active = self.run_command('scheduler-unit-state', ['systemctl', 'show', UNITS['nova_scheduler'],
                '--property=ActiveState', '--value'])
            if active['stdout'].strip() != 'inactive':
                raise RuntimeError('Scheduler systemd unit is not inactive')
            self.run_command('restart-rabbit-unit', ['systemctl', 'restart', UNITS['rabbitmq']], timeout=120)
            self.wait_container('rabbitmq', True)
            self.run_command('rabbit-local-alarms', ['docker', 'exec', 'rabbitmq',
                'rabbitmq-diagnostics', '-q', 'check_local_alarms'])
            self.run_command('start-scheduler-unit', ['systemctl', 'start', UNITS['nova_scheduler']], timeout=120)
            deadline = time.monotonic()+30
            while not self.state('nova_scheduler').get('Running'):
                if time.monotonic() >= deadline:
                    raise RuntimeError('Scheduler did not start')
                time.sleep(1)
            self.report['scheduler_may_be_stopped'] = False
            verify_workers(self.root, self.workers)
            final = health.wait(self.root/'rabbitmq-health-after-recovery.json', self.log,
                                timeout=self.timeout, stable_seconds=self.stable_seconds)
            self.capture('after')
            self.report.update(status=final['status'], finished_at=health.utc(),
                health_after='rabbitmq-health-after-recovery.json',
                last_issues=(last_complete(final) or {}).get('issues', []))
            self.record('recovery-finished')
            return self.report
        except Exception as exc:
            self.report.update(status='FAIL', finished_at=health.utc(), error=str(exc))
            self.record('recovery-failed')
            return self.report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['verify-workers', 'verify-conductor-workers', 'recover'])
    parser.add_argument('root', type=Path)
    parser.add_argument('--workers', type=int, required=True)
    parser.add_argument('--log', type=Path, default=Path('/var/log/kolla/rabbitmq/rabbit@controller.log'))
    parser.add_argument('--timeout', type=int, default=180)
    parser.add_argument('--stable-seconds', type=int, default=15)
    args = parser.parse_args()
    try:
        value = (verify_workers(args.root, args.workers,
                               'nova_conductor' if args.action == 'verify-conductor-workers' else 'nova_scheduler')
                 if args.action != 'recover' else
            Recovery(args.root, args.workers, args.log, args.timeout, args.stable_seconds).recover())
        print(json.dumps({k: v for k, v in value.items() if k not in ('events', 'check')}))
        return 0 if value['status'] == 'PASS' else 1
    except Exception as exc:
        print(json.dumps(dict(status='FAIL', error=str(exc))))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
