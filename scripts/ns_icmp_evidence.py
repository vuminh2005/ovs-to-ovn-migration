#!/usr/bin/env python3
"""N-S ICMP metrics from sender-side PCAP, avoiding translated ICMP identifiers."""
import json
from pathlib import Path

from dataplane_capture import echo_rows, pcap_metrics, recovered_endpoint, save


def finalize(root):
    root = Path(root)
    cfg = json.loads((root/'validation-config.json').read_text())
    ns = cfg.get('ns', {})
    if not ns.get('enabled') or not ns.get('icmp_enabled'):
        return {}
    path = root/'ns-results.json'
    result = json.loads(path.read_text()) if path.exists() else dict(status='UNAVAILABLE', directions={})
    baseline = json.loads((root/'ns-baseline.json').read_text())
    captures = json.loads((root/'ns-diagnostics.json').read_text()).get('captures', {})
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
            raw = root/'ns-diagnostics'/kind/'tcp.pcap'
            rows, problems = echo_rows(raw, source, peer)
            if problems:
                raise ValueError('; '.join(sorted(set(problems))))
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
        except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
            value.update(status='UNAVAILABLE', reason=str(exc))
        outcomes[direction] = value
        entry = result.setdefault('directions', {}).setdefault(direction, {})
        entry['icmp'] = value
        if value['status'] != 'PASS':
            entry['status'] = 'FAIL'
            result['status'] = 'FAIL'
    save(root/'ns-icmp-results.json', outcomes)
    save(path, result)
    return outcomes
