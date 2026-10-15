"""Traffic clock anchors and temporal task attribution; never infer causality."""
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import re


def phase(row):
    match = re.search(r'Phase\s+(\d\d)', row.get('play', ''), re.I)
    if not match:
        match = re.search(r'/(\d\d)-[^/]+\.yml', row.get('source', ''))
    return match.group(1) if match else None


def read_tasks(root):
    rows = {}
    for path in (Path(root)/'task-timings').glob('*.json*'):
        try:
            if path.suffix == '.json':
                items = json.loads(path.read_text()).get('tasks', [])
            elif path.suffix == '.jsonl':
                items = []
                for line in path.read_text().splitlines():
                    try:
                        row = json.loads(line)
                        if row.get('event') in ('task_start', 'task_end'):
                            items.append(row)
                    except ValueError:
                        continue  # A live callback may still be writing its last line.
            else:
                continue
            for row in items:
                key = row.get('execution_id')
                if key and (key not in rows or row.get('ended_at')):
                    rows[key] = row
        except (OSError, ValueError):
            continue
    return list(rows.values())


def task_at(ts, rows):
    if ts is None:
        return None
    candidates = [r for r in rows if r.get('started_at') and
        r['started_at']['epoch'] <= ts and
        (r.get('ended_at') is None or r['ended_at']['epoch'] >= ts)]
    if not candidates:
        return dict(status='UNAVAILABLE', phase=None, task=None)
    enclosing = sorted(candidates, key=lambda r: (r.get('ended_at') or {}).get('epoch', ts)-r['started_at']['epoch'])
    owner_phase = next((phase(r) for r in enclosing if phase(r)), None)
    best = enclosing[0]
    return dict(status='OBSERVED_OVERLAP', phase=phase(best) or owner_phase,
        task=best.get('task_name'), host=best.get('host'), source=best.get('source'))


def build(root, ns):
    tasks = read_tasks(root)
    result = {}
    for direction, value in ns.get('directions', {}).items():
        probes = dict(value.get('streams', {}))
        if value.get('icmp'):
            probes['icmp'] = value['icmp']
        for name, probe in probes.items():
            events = {}
            for field in ('start_timestamp', 'end_timestamp', 'first_failure_timestamp', 'recovery_timestamp'):
                ts = probe.get(field)
                events[field] = dict(timestamp=ts, task=task_at(ts, tasks))
            events['failure_burst_count'] = probe.get('failure_burst_count')
            result[direction+'/'+name] = events
    return result


def display(event):
    ts = event.get('timestamp')
    if ts is None:
        return 'UNAVAILABLE'
    clock = datetime.fromtimestamp(ts, timezone(timedelta(hours=7))).isoformat(timespec='milliseconds')
    task = event.get('task') or {}
    return clock+' | phase '+(task.get('phase') or '?')+' | '+(task.get('task') or 'task unavailable')
