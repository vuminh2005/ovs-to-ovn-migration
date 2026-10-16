#!/usr/bin/env python3
"""Stop live migration observer supervisors before a destructive lab reset.

Run results, archive health and checkpoint configurations are irrelevant here.
Match the exact observer invocation beneath the configured private directory,
then bind TERM to that original process with a pidfd. Never match by name alone.
"""
import argparse
import json
import math
import os
from pathlib import Path
import re
import signal
import time


def identity(pid):
    try:
        path = '/proc/self/stat' if pid == os.getpid() else f'/proc/{pid}/stat'
        text = Path(path).read_text()
        fields = text.rsplit(')', 1)[1].split()
        if fields[0] == 'Z':
            return None
        return dict(pid=int(text.split(' ', 1)[0]), start_ticks=fields[19],
                    boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip())
    except (FileNotFoundError, ProcessLookupError):
        return None


def invocation(pid, base):
    try:
        args = [os.fsdecode(x) for x in Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0') if x]
    except (FileNotFoundError, ProcessLookupError):
        return None
    if len(args) != 4 or not Path(args[0]).name.startswith('python') or args[2] != 'run':
        return None
    root = Path(args[3])
    if not root.is_absolute() or Path(args[1]) != root/'migration_diagnostics.py':
        return None
    try:
        relative = root.relative_to(base)
    except ValueError:
        return None
    if len(relative.parts) != 2 or any(not re.fullmatch(r'[A-Za-z0-9_.-]+', p)
                                      or p in ('.', '..') for p in relative.parts):
        return None
    return dict(directory=str(root), run=relative.parts[0], collector=relative.parts[1])


def stop_observers(base, timeout=60):
    base = Path(base)
    if not base.is_absolute() or base == Path('/') or '..' in base.parts:
        raise ValueError('An absolute dedicated observer directory is required')
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('Stop timeout must be finite and positive')
    result = dict(status='IN_PROGRESS', directory=str(base), started_at=time.time(),
                  signaled=[], stopped=[], remaining=[], timeout_seconds=timeout,
                  scope='Current observer processes only; historical report/archive health is not a reset gate')
    owned = []
    try:
        # Inspect current processes once, irrespective of the number/health of old runs.
        for proc in Path('/proc').glob('[0-9]*'):
            pid = int(proc.name)
            original = identity(pid)
            if not original or not (owner := invocation(pid, base)):
                continue
            try:
                handle = os.pidfd_open(pid)
            except ProcessLookupError:
                continue
            if identity(pid) != original or invocation(pid, base) != owner:
                os.close(handle)
                continue
            owned.append((handle, dict(owner, identity=original)))
        for handle, item in owned:
            pid = item['identity']['pid']
            if identity(pid) != item['identity']:
                continue
            if invocation(pid, base) != {k:item[k] for k in ('directory','run','collector')}:
                raise RuntimeError('Observer invocation changed before stop: '+str(pid))
            try:
                signal.pidfd_send_signal(handle, signal.SIGTERM)
                result['signaled'].append(item)
            except ProcessLookupError:
                pass
        deadline = time.monotonic()+timeout
        while True:
            result['remaining'] = [item for _,item in owned
                                   if identity(item['identity']['pid']) == item['identity']]
            if not result['remaining']:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError('Live host observer did not stop before Kolla destroy')
            time.sleep(.1)
        result['status'] = 'PASS'
    except Exception as exc:
        result.update(status='FAIL', error=str(exc))
    finally:
        result['remaining'] = [item for _,item in owned
                               if identity(item['identity']['pid']) == item['identity']]
        result['stopped'] = [item for item in result['signaled']
                             if identity(item['identity']['pid']) != item['identity']]
        result['finished_at'] = time.time()
        for handle, _ in owned:
            os.close(handle)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--timeout', type=float, default=60)
    args = parser.parse_args()
    value = stop_observers(args.directory, args.timeout)
    print(json.dumps(value, sort_keys=True))
    raise SystemExit(value['status'] != 'PASS')
