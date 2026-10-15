#!/usr/bin/env python3
"""Read-only, bounded RabbitMQ readiness for the single-controller reset lab."""
import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
import uuid


RAFT_ERROR = re.compile(r'cluster_not_formed|already_started|failed to form a new cluster', re.I)
CSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
DEC_GRAPHICS = re.compile(r'\x1b\(0(.*?)\x1b\([AB]', re.S)


def utc():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, indent=2)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def parse_queues(output):
    rows = {}
    for line in output.splitlines():
        fields = line.split()
        if not fields or fields == ['name', 'type', 'state', 'consumers']:
            continue
        if len(fields) != 4 or fields[1] not in ('classic', 'quorum', 'stream'):
            raise ValueError('Unrecognized queue listing; cannot establish readiness')
        name, kind, state, consumers = fields
        if name in rows or not consumers.isdecimal():
            raise ValueError('Invalid or duplicate queue listing')
        rows[name] = dict(type=kind, state=state, consumers=int(consumers))
    if not rows:
        raise ValueError('Queue listing is empty')
    return rows


def queue_issues(rows):
    issues = []
    for name in ('conductor', 'scheduler'):
        row = rows.get(name)
        if not row or row['type'] != 'quorum' or row['state'] != 'running' or row['consumers'] < 1:
            issues.append(name + ' must be a running quorum queue with consumers')
    for name, row in rows.items():
        if row['type'] == 'quorum' and row['state'] != 'running':
            issues.append('Quorum queue is not running: ' + name)
    return issues


def terminal_text(output):
    # Captured RabbitMQ output can use DEC special graphics: ESC(0xESC(B
    # represents a vertical rule, not a literal 'x'. Translate inside those
    # explicitly marked spans only; never change characters in node names.
    output = DEC_GRAPHICS.sub(lambda match: match.group(1).replace('x', '|'), output)
    return CSI.sub('', output)


def parse_quorum(output, node):
    # Parse member rows after normalizing ANSI styling and DEC table rules.
    members = []
    for line in terminal_text(output).splitlines():
        fields = [part.strip() for part in re.split('[│|]', line) if part.strip()]
        if fields and fields[0].startswith('rabbit@'):
            if len(fields) < 3:
                raise ValueError('Malformed quorum member row')
            members.append(dict(node=fields[0], state=fields[1], membership=fields[2]))
    if (len(members) != 1 or members[0] != dict(node=node, state='leader', membership='voter')):
        raise ValueError('Expected one healthy leader/voter on the single-controller lab')
    return members


class LogCursor:
    """Only new errors count against stability; old deploy errors are evidence."""
    def __init__(self, path):
        self.path = Path(path)
        self.identity = None
        self.offset = 0
        self.pending = ''
        self._initial = True

    def poll(self):
        with self.path.open('rb') as stream:
            stat = os.fstat(stream.fileno())
            identity = (stat.st_dev, stat.st_ino)
            if self._initial:
                self.identity, self.offset, self._initial = identity, stat.st_size, False
                return dict(errors=[], continuity='START')
            rotated = identity != self.identity or stat.st_size < self.offset
            if rotated:
                self.offset, self.pending = 0, ''
            stream.seek(self.offset)
            # No historical full-file scan: track newly observed bytes only.
            text = self.pending + stream.read().decode('utf-8', errors='replace')
            self.identity, self.offset = identity, stream.tell()
        lines = text.splitlines(keepends=True)
        self.pending = lines.pop() if lines and not lines[-1].endswith(('\n', '\r')) else ''
        errors = [line.rstrip() for line in lines if RAFT_ERROR.search(line)]
        if self.pending and RAFT_ERROR.search(self.pending):
            errors.append(self.pending)
        return dict(errors=errors,
                    continuity='ROTATED_OR_TRUNCATED' if rotated else 'CONTINUOUS')


def command(args, deadline, timeout):
    remaining = deadline-time.monotonic()
    if remaining <= 0:
        return dict(rc=124, stdout='', stderr='Overall readiness deadline reached', timeout_scope='overall')
    budget = min(timeout, remaining)
    try:
        result = subprocess.run(['docker', 'exec', 'rabbitmq', *args],
            capture_output=True, text=True, timeout=budget)
        return dict(rc=result.returncode, stdout=result.stdout, stderr=result.stderr,
                    timeout_seconds=budget)
    except subprocess.TimeoutExpired:
        overall = remaining < timeout
        return dict(rc=124, stdout='', stderr='Overall readiness deadline reached during command'
                    if overall else 'RabbitMQ read-only command timed out',
                    timeout_scope='overall' if overall else 'command', timeout_seconds=budget)
    except OSError as error:
        return dict(rc=127, stdout='', stderr=type(error).__name__)


def snapshot(deadline, timeout, node):
    checks, issues, members, queues = {}, [], {}, {}
    for name, args in (
        ('running', ['rabbitmq-diagnostics', '-q', 'check_running']),
        ('alarms', ['rabbitmq-diagnostics', '-q', 'check_local_alarms']),
        ('queues', ['rabbitmqctl', '-q', 'list_queues', '-p', '/', 'name', 'type', 'state', 'consumers']),
        ('conductor', ['rabbitmq-queues', '-t', str(timeout), 'quorum_status', '--vhost', '/', 'conductor']),
        ('scheduler', ['rabbitmq-queues', '-t', str(timeout), 'quorum_status', '--vhost', '/', 'scheduler']),
    ):
        checks[name] = command(args, deadline, timeout)
        if checks[name]['rc'] != 0:
            issues.append(name + ' check failed')
    if checks['queues']['rc'] == 0:
        try:
            queues = parse_queues(checks['queues']['stdout'])
            issues.extend(queue_issues(queues))
        except ValueError as error:
            issues.append(str(error))
    for name in ('conductor', 'scheduler'):
        if checks[name]['rc'] == 0:
            try:
                members[name] = parse_quorum(checks[name]['stdout'], node)
            except ValueError as error:
                issues.append(name + ': ' + str(error))
    return dict(observed_at=utc(), checks=checks, queues=queues, members=members, issues=issues)


def wait(evidence, log, *, timeout=180, stable_seconds=15, interval=5, command_timeout=15, node='rabbit@controller'):
    values = (timeout, stable_seconds, interval, command_timeout)
    if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in values) or timeout <= stable_seconds:
        raise ValueError('Use positive finite timings and timeout greater than stable_seconds')
    start = time.monotonic()
    deadline = start+timeout
    cursor, stable_since, samples = LogCursor(log), None, []
    result = dict(schema_version=1, status='RUNNING', started_at=utc(), node=node,
                  timeout_seconds=timeout, stable_seconds_required=stable_seconds,
                  scope='Read-only live readiness and a quiet Raft log window; earlier deploy errors do not prove current failure.',
                  samples=samples)
    save(evidence, result)  # Never leave a previous PASS after an interrupted retry.
    while time.monotonic() < deadline:
        try:
            log_state = cursor.poll()
        except OSError as error:
            log_state = dict(errors=[], continuity='UNAVAILABLE', error=type(error).__name__)
        row = snapshot(deadline, command_timeout, node)
        try:
            after = cursor.poll()
            log_state['errors'].extend(after['errors'])
            if after['continuity'] != 'CONTINUOUS':
                log_state['continuity'] = after['continuity']
        except OSError as error:
            log_state.update(continuity='UNAVAILABLE', error=type(error).__name__)
        row['log'] = log_state
        if log_state['errors']:
            row['issues'].append('New quorum/Raft errors during readiness observation')
        if log_state['continuity'] not in ('START', 'CONTINUOUS'):
            row['issues'].append('RabbitMQ log continuity unavailable; restart stability window')
        now = time.monotonic()
        if row['issues']:
            stable_since = None
        elif stable_since is None:
            stable_since = now
        row['stable_seconds'] = now-stable_since if stable_since is not None else 0
        samples.append(row)
        result.update(elapsed_seconds=now-start, observed_at=utc())
        if stable_since is not None and now < deadline and now-stable_since >= stable_seconds:
            result.update(status='PASS', ended_at=utc())
            save(evidence, result)
            return result
        save(evidence, result)
        if now < deadline:
            time.sleep(min(interval, deadline-now))
    result.update(status='FAIL', ended_at=utc(), elapsed_seconds=time.monotonic()-start,
                  reason='RabbitMQ did not reach stable live readiness before timeout')
    save(evidence, result)
    return result


def last_complete(data):
    """A deadline-only sample does not describe the queues' actual state."""
    names = ('running', 'alarms', 'queues', 'conductor', 'scheduler')
    return next((s for s in reversed(data.get('samples', []))
                 if all(n in s.get('checks', {}) for n in names) and not any(
                     c.get('timeout_scope') == 'overall' for c in s['checks'].values())), None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('evidence', type=Path)
    parser.add_argument('--log', type=Path, default=Path('/var/log/kolla/rabbitmq/rabbit@controller.log'))
    parser.add_argument('--timeout', type=int, default=180)
    parser.add_argument('--stable-seconds', type=int, default=15)
    args = parser.parse_args()
    result = wait(args.evidence, args.log, timeout=args.timeout, stable_seconds=args.stable_seconds)
    complete = last_complete(result) or {}
    last_sample = result['samples'][-1] if result['samples'] else {}
    incomplete = any(c.get('timeout_scope') == 'overall' for c in last_sample.get('checks', {}).values())
    print(json.dumps(dict(status=result['status'], evidence=str(args.evidence), samples=len(result['samples']),
        last_issues=(complete if incomplete and complete else last_sample).get('issues', []),
        last_sample_issues=last_sample.get('issues', []), last_sample_incomplete=incomplete,
        last_complete_at=complete.get('observed_at'), last_complete_issues=complete.get('issues'))))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
