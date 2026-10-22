#!/usr/bin/env python3
"""N-S ICMP metrics from sender-side PCAP, avoiding translated ICMP identifiers."""

# Source helpers are grouped; remote/guest copies keep flat sibling imports.
import sys as _sys
from pathlib import Path as _Path
_source = _Path(__file__).resolve().parents[1]
if _source.name == 'scripts' and (_source / '_paths.py').is_file():
    _sys.path.insert(0, str(_source))
    from _paths import configure as _configure
    _configure()
from run_layout import run_path
import json
from pathlib import Path

from dataplane_capture import echo_rows, pcap_metrics, recovered_endpoint, save
from ns_icmp_logging import sender_evidence
from ns_icmp_reconcile import reconcile


def finalize(root):
    root = Path(root)
    cfg = json.loads((run_path(root, 'validation-config.json')).read_text())
    ns = cfg.get('ns', {})
    if not ns.get('enabled') or not ns.get('icmp_enabled'):
        return {}
    path = run_path(root, 'ns-results.json')
    result = json.loads(path.read_text()) if path.exists() else dict(status='UNAVAILABLE', directions={})
    baseline = json.loads((run_path(root, 'ns-baseline.json')).read_text())
    captures = json.loads((run_path(root, 'ns-diagnostics.json')).read_text()).get('captures', {})
    outcomes = {}
    for direction, kind, source, peer in (
        ('snat', 'guest-snat', ns['outbound_ip'], ns['endpoint_ip']),
        ('fip', 'endpoint', ns['endpoint_ip'], ns['fip_ip']),
    ):
        value = dict(status='UNAVAILABLE', reason='No valid ICMP PCAP window')
        try:
            cp = captures[kind]
            if (cp.get('remote', {}).get('status') != 'STOPPED' or
                    cp['remote'].get('dropped_packets') != 0):
                raise ValueError('Capture completeness/drop count not proven')
            raw = run_path(root, 'ns-diagnostics')/kind/'tcp.pcap'
            rows, problems = echo_rows(raw, source, peer)
            start_ts = baseline['clients'][direction]['ts']
            before = [r for r in rows if r['ts'] <= start_ts and r['reply_timestamp'] is not None]
            if not before:
                raise ValueError('No successful PCAP anchor before N-S baseline')
            row = before[-1]
            start = dict(index=row['index'], timestamp=row['ts'], reply_timestamp=row['reply_timestamp'],
                         identifier=row['identifier'], icmp_sequence=row['icmp_sequence'])
            end = recovered_endpoint(rows, cfg['interval'])
            value = pcap_metrics(raw, dict(source_ip=source, peer_ip=peer,
                remote=dict(cp['remote'], supervisor_gap=cp['remote'].get('supervisor_gap', False))),
                                 dict(pcap_start=start, pcap_end=end), cfg['interval'])
            value.update(measurement_workload='N-S '+direction.upper(), measurement_guest=kind,
                evidence_source='sender-side-pcap', start_timestamp=start['timestamp'],
                end_timestamp=end['timestamp'] if end else None,
                scope='Successful sender-PCAP baseline anchor to final recovered sender-PCAP anchor; 56-byte ICMP')
            bursts = value.get('failure_bursts', [])
            value['total_downtime_seconds'] = (sum(b['duration_seconds'] for b in bursts)
                if value['status'] == 'PASS' else None)
            sender_path = run_path(root, 'ns')/(direction+'-icmp-sender.jsonl')
            full = run_path(root, 'ns/snat-icmp-sender.full.jsonl')
            if direction == 'snat' and (full.exists() or ns.get('icmp_sender_export_enabled')):
                sender_path = full
                from ns_log_transfer import verify_export
                verify_export(root, cfg.get('run'), baseline['clients'][direction])
            sender = sender_evidence(sender_path, cfg.get('run'), direction,
                baseline['clients'][direction], start['timestamp'], end['reply_timestamp'] if end else None)
            observations = sender.pop('sequence_observations', [])
            discontinuities = []
            for prior, current in zip(rows, rows[1:]):
                if current['ts'] < start['timestamp'] or (end and current['ts'] > end['timestamp']):
                    continue
                delta = (current['icmp_sequence']-prior['icmp_sequence']) % 65536
                if current['identifier'] == prior['identifier'] and delta == 1:
                    continue
                same_identifier = current['identifier'] == prior['identifier']
                gap = dict(previous_request=prior, next_request=current,
                    kind='sequence_gap_or_reset' if same_identifier else 'identifier_change',
                    missing_request_count=delta-1 if same_identifier and 1 < delta < 32768 else None,
                    sender_observations=[r for r in observations if same_identifier and 1 < delta < 32768 and
                        prior['ts']-2 <= r['observed_ts'] <= current['ts']+2 and
                        0 < (r['icmp_sequence']-prior['icmp_sequence']) % 65536 < delta])
                discontinuities.append(gap)
            value.update(sender_log=sender, request_discontinuities=discontinuities)
            value = reconcile(value, rows, start, end, sender, observations, cfg['interval'], source)
            if value['status'] != 'PASS' and bursts:
                value['observed_window'] = dict(certified=False,
                    scope='Captured requests/replies only; missing sender requests remain unaccounted',
                    first_failure_timestamp=value['first_failure_timestamp'], recovery_timestamp=value['recovery_timestamp'],
                    failure_bursts=bursts)
        except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
            value.update(status='UNAVAILABLE', reason=str(exc))
        outcomes[direction] = value
        entry = result.setdefault('directions', {}).setdefault(direction, {})
        if 'tcp_status' not in entry:
            streams = entry.get('streams', {})
            entry['tcp_status'] = ('PASS' if streams and all(s.get('status') == 'PASS' for s in streams.values())
                                   and entry.get('boot_continuity', 'PASS') == 'PASS'
                                   else entry.get('status', 'UNAVAILABLE'))
        entry['icmp'] = value
        entry['status'] = 'PASS' if value['status'] == 'PASS' and entry['tcp_status'] == 'PASS' else 'FAIL'
    result['status'] = ('PASS' if not result.get('reason') and result.get('directions') and
                        all(v.get('status') == 'PASS' for v in result['directions'].values()) else 'FAIL')
    save(run_path(root, 'ns-icmp-results.json'), outcomes)
    save(path, result)
    return outcomes
