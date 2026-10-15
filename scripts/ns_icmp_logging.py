"""Durable sender output and an explicitly checked Nova-console mirror.

Ping output is secondary evidence of sequence/errors, not proof of wire sends.
"""
import fcntl
import json
import math
from pathlib import Path

PREFIX = 'OVN_NS_ICMP_JSON '


def mirror_console(root, output, run):
    rows = []
    for line in output.splitlines():
        if PREFIX not in line:
            continue
        try:
            row = json.JSONDecoder().raw_decode(line.split(PREFIX, 1)[1])[0]
            if row.get('run') == run and row.get('label') == 'snat':
                rows.append(row)
        except (TypeError, ValueError):
            continue  # Missing event indices make the final mirror incomplete.
    if not rows:
        return
    directory = Path(root)/'ns'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory/'snat-icmp-sender.jsonl'
    with path.open('a+') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0)
        seen = {line.strip() for line in stream if line.strip()}
        stream.seek(0, 2)
        for row in rows:
            data = json.dumps(row, sort_keys=True)
            if data not in seen:
                if stream.tell()+len((data+'\n').encode()) > 67108864:
                    raise ValueError('ICMP sender console mirror byte budget exhausted')
                stream.write(data+'\n')
                seen.add(data)
        stream.flush()
        path.chmod(0o600)


def sender_evidence(path, run, direction, baseline, start, end):
    result = dict(status='UNAVAILABLE', path=str(path),
        scope='Source ping stdout/stderr; replies, outstanding probes and local errors; not wire-send timestamps')
    try:
        expected = baseline.get('icmp', {}).get('log_id')
        if not expected:
            raise ValueError('No baseline sender log identity')
        rows = {}
        for line in Path(path).read_text().splitlines():
            row = json.loads(line)
            if row.get('run') != run or row.get('label') != direction or row.get('log_id') != expected:
                raise ValueError('Sender run/label/log identity changed')
            key = row['event_index']
            if type(key) is not int or key < 1:
                raise ValueError('Invalid sender event index')
            if key in rows and rows[key] != row:
                raise ValueError('Conflicting sender event index')
            rows[key] = row
        ordered = [rows[k] for k in sorted(rows)]
        if not ordered or not start or not end:
            raise ValueError('Missing sender log or PCAP time anchors')
        if any(type(r.get(k)) not in (int, float) or not math.isfinite(r[k])
               for r in ordered for k in ('observed_ts', 'observed_mono')):
            raise ValueError('Invalid sender observation clocks')
        if any(b['observed_mono'] <= a['observed_mono'] for a,b in zip(ordered, ordered[1:])):
            raise ValueError('Sender monotonic clock moved backwards or duplicated')
        if any(row.get('boot') != baseline.get('boot') or
               row.get('ping_pid') != baseline.get('icmp', {}).get('pid') for row in ordered):
            raise ValueError('Original sender boot/PID not preserved')
        before = [r for r in ordered if r['observed_ts'] <= start]
        after = [r for r in ordered if r['observed_ts'] >= end]
        if not before or not after:
            raise ValueError('Sender mirror does not cover PCAP measurement window')
        selected = [r for r in ordered if before[-1]['event_index'] <= r['event_index'] <= after[0]['event_index']]
        missing = sum(b['event_index']-a['event_index']-1 for a,b in zip(selected, selected[1:]))
        result.update(event_count=len(selected), missing_event_count=missing,
            start_event_index=selected[0]['event_index'], end_event_index=selected[-1]['event_index'],
            errors=[r for r in selected if r['kind'] == 'error'][-20:],
            outstanding_events=sum(r['kind']=='outstanding' for r in selected),
            sequence_observations=[dict(icmp_sequence=r.get('icmp_sequence'), native_ts=r.get('native_ts'),
                observed_ts=r['observed_ts'], kind=r['kind']) for r in selected if r.get('icmp_sequence') is not None])
        if missing:
            raise ValueError('Sender console mirror has missing event indices')
        result['status'] = 'PASS'
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result['reason'] = str(exc)
    return result
