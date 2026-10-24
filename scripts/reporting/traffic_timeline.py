"""Traffic clock anchors and temporal task attribution; never infer causality."""

# Source helpers are grouped; remote/guest copies keep flat sibling imports.
import sys as _sys
from pathlib import Path as _Path
_source = _Path(__file__).resolve().parents[1]
if _source.name == 'scripts' and (_source / '_paths.py').is_file():
    _sys.path.insert(0, str(_source))
    from _paths import configure as _configure
    _configure()
from run_layout import run_path
from datetime import datetime, timezone, timedelta
import json
import math
from pathlib import Path
import re


def phase(row):
    match = re.search(r'Phase\s+(\d\d)', row.get('play', ''), re.I)
    if not match:
        match = re.search(r'/(\d\d)-[^/]+\.yml', row.get('source', ''))
    return match.group(1) if match else None


def read_tasks(root):
    rows = {}
    for path in (run_path(Path(root), 'task-timings')).glob('*.json*'):
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


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def tcp_clock_events(tcp, probe):
    """Map the ARM-scoped client monotonic summary to its own wall clock.

    The wall clock is for task correlation only; outage durations stay monotonic.
    Old summaries can recover the first recovery only for one completed burst.
    """
    client = (tcp.get('snapshot') or {}).get('0') or {}
    wall, mono = client.get('ts'), client.get('mono')

    def clock(value):
        return wall + (value-mono) if all(finite(v) for v in (value, wall, mono)) else None

    recovery = probe.get('first_recovery_mono')
    recovery_source = 'client-first-recovery-monotonic'
    if recovery is None and probe.get('failure_burst_count') == 1 and probe.get('open_failure_since') is None:
        first, duration = probe.get('first_failure_mono'), probe.get('longest_recovered_failure_seconds')
        if finite(first) and finite(duration) and duration >= 0:
            recovery = first + duration
            recovery_source = 'legacy-single-recovered-burst-duration'
    return dict(start_timestamp=clock(client.get('armed_mono')),
        end_timestamp=wall if finite(wall) else None,
        first_failure_timestamp=clock(probe.get('first_failure_mono')),
        recovery_timestamp=clock(recovery), failure_burst_count=probe.get('failure_burst_count'),
        clock_scope='Client snapshot wall-minus-monotonic mapping; task correlation only',
        recovery_source=recovery_source if recovery is not None else 'UNAVAILABLE')


def build(root, ns, *, ew=None, dataplane=None, tcp=None):
    tasks = read_tasks(root)
    result = {}

    def add(key, probe):
        events = {}
        for field in ('start_timestamp', 'end_timestamp', 'first_failure_timestamp', 'recovery_timestamp'):
            ts = probe.get(field)
            if ts is None and field in ('start_timestamp', 'end_timestamp'):
                anchor = 'start_anchor' if field == 'start_timestamp' else 'end_anchor'
                ts = (probe.get(anchor) or {}).get('timestamp')
            events[field] = dict(timestamp=ts, task=task_at(ts, tasks))
        events['failure_burst_count'] = probe.get('failure_burst_count')
        for field in ('clock_scope', 'recovery_source'):
            if field in probe:
                events[field] = probe[field]
        result[key] = events

    if ew is not None or dataplane is not None:
        flows = (ew or {}).get('flows') or {}
        for key in ('ew_different_compute_different_subnet', 'ew_different_compute_same_subnet',
                    'ew_same_compute_different_subnet', 'ew_same_compute_same_subnet'):
            probe = (dataplane or flows.get(key) or {}) if key == 'ew_different_compute_different_subnet' else flows.get(key) or {}
            add(key, probe)
    if tcp is not None and tcp.get('status')!='DISABLED':
        for name in ('held', 'new_old', 'new_listener'):
            probe = (tcp.get('streams') or {}).get(name) or {}
            add('ew_tcp/'+name, tcp_clock_events(tcp, probe))
    for direction, value in ns.get('directions', {}).items():
        probes = dict(value.get('streams', {}))
        if value.get('icmp'):
            probes['icmp'] = value['icmp']
        for name, probe in probes.items():
            add(direction+'/'+name, probe)
    return result


def display(event, *, include_timestamp=True):
    ts = event.get('timestamp')
    if ts is None:
        return 'UNAVAILABLE'
    task = event.get('task') or {}
    value = 'phase '+(task.get('phase') or '?')+' | '+(task.get('task') or 'task unavailable')
    if include_timestamp:
        clock = datetime.fromtimestamp(ts, timezone(timedelta(hours=7))).isoformat(timespec='milliseconds')
        value = clock+' | '+value
    return value
