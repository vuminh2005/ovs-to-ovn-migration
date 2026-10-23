#!/usr/bin/env python3
"""Wait for the source OVS containers and their existing integration bridge."""
import argparse
import json
import math
import subprocess
import time


CONTAINERS = ('openvswitch_db', 'openvswitch_vswitchd', 'neutron_openvswitch_agent')


def command(args, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('OVS readiness deadline reached')
    return subprocess.run(args, text=True, capture_output=True, check=False,
                          timeout=min(5, remaining))


def sample(deadline):
    result = command(['docker', 'container', 'inspect', *CONTAINERS], deadline)
    if result.returncode:
        return dict(ready=False, reason='Source OVS containers are not all inspectable', containers={})
    containers = {}
    for row in json.loads(result.stdout):
        name = row.get('Name', '').lstrip('/')
        state = row.get('State', {})
        containers[name] = dict(status=state.get('Status'), running=state.get('Running'),
            restarting=state.get('Restarting', False), restart_count=row.get('RestartCount'),
            exit_code=state.get('ExitCode'), health=state.get('Health', {}).get('Status'))
    if set(containers) != set(CONTAINERS) or any(
            c['running'] is not True or c['restarting'] or c['status'] != 'running'
            for c in containers.values()):
        return dict(ready=False, reason='Source OVS containers are not all running', containers=containers)
    result = command(['docker', 'exec', 'openvswitch_vswitchd', 'ovs-vsctl',
                      '--timeout=3', 'br-exists', 'br-int'], deadline)
    return dict(ready=result.returncode == 0, containers=containers,
                reason='Source OVS containers and br-int are ready' if result.returncode == 0
                       else 'Source OVS integration bridge br-int is not ready')


def wait(timeout=120, interval=2):
    if not math.isfinite(timeout) or not math.isfinite(interval) or timeout <= 0 or interval <= 0:
        raise ValueError('Positive finite readiness timeout and interval are required')
    started = time.monotonic()
    deadline = started + timeout
    last = dict(ready=False, reason='No OVS readiness sample completed', containers={})
    attempts = 0
    while time.monotonic() < deadline:
        attempts += 1
        try:
            last = sample(deadline)
        except (OSError, ValueError, TypeError, subprocess.TimeoutExpired, TimeoutError) as exc:
            # Never include raw inspect output, environment or credentials.
            last = dict(ready=False, reason='OVS readiness sample failed: '+type(exc).__name__, containers={})
        if last['ready']:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))
    return dict(status='PASS' if last['ready'] else 'FAIL', attempts=attempts,
                elapsed_seconds=round(time.monotonic()-started, 3), **last,
                scope='Read-only readiness; bridge creation remains owned by neutron_openvswitch_agent')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout', type=float, default=120)
    parser.add_argument('--interval', type=float, default=2)
    args = parser.parse_args()
    result = wait(args.timeout, args.interval)
    print(json.dumps(result, sort_keys=True))
    return int(result['status'] != 'PASS')


if __name__ == '__main__':
    raise SystemExit(main())
