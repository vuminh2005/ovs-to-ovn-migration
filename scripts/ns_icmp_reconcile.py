"""Certify probe availability for exactly explained, internal local-error gaps.

Never invent wire packets or source send timestamps. Missing attempts must be
local Host Unreachable errors inside a burst already anchored by captured loss
and recovery. Every unexplained gap, identity/capture failure stays unavailable.
"""
import copy
import math
import re


ALLOWED = {'ICMP identifier/sequence discontinuity', 'Unexplained request cadence/capture gap'}


def reconcile(value, rows, start, end, sender, observations, interval, source):
    if value.get('status') == 'PASS' or not end or sender.get('status') != 'PASS':
        return value
    reasons = set(value.get('reason', '').split('; '))
    if not reasons or reasons-ALLOWED:
        return value
    timed = rows[start['index']-1:end['index']]
    if len(timed) < 2 or len({r['identifier'] for r in timed}) != 1:
        return value
    bursts = copy.deepcopy(value.get('failure_bursts', []))
    if not bursts or any(b.get('duration_seconds') is None for b in bursts):
        return value
    missing = 0
    explanations = []
    for prior, current in zip(timed, timed[1:]):
        delta = (current['icmp_sequence']-prior['icmp_sequence']) % 65536
        elapsed = current['ts']-prior['ts']
        if delta == 1 and 0 <= elapsed < 3*interval:
            continue
        if not (1 < delta < 32768 and elapsed > 0):
            return value
        # A local error cannot supply the missing attempt's send time. Only
        # fill counts inside a loss burst with independently captured bounds.
        burst = next((b for b in bursts if b['start_timestamp'] <= prior['ts'] and
                      current['ts'] <= b['recovery_timestamp']), None)
        if prior['reply_timestamp'] is not None or burst is None:
            return value
        candidates = [o for o in observations if prior['ts']-2 <= o['observed_ts'] <= current['ts']+2]
        for offset in range(1, delta):
            seq = (prior['icmp_sequence']+offset) % 65536
            found = [o for o in candidates if o.get('icmp_sequence') == seq]
            errors = [o for o in found if o.get('kind') == 'error']
            if len(errors) != 1 or any(o.get('kind') == 'reply' for o in found):
                return value
            expected = r'^\[\d+\.\d+\] From '+re.escape(source)+r' icmp_seq='+str(seq)+r' Destination Host Unreachable$'
            if not re.fullmatch(expected, errors[0].get('text', '')):
                return value
            stamp = errors[0].get('native_ts')
            if type(stamp) not in (int, float) or not math.isfinite(stamp) or abs(stamp-errors[0]['observed_ts']) > 2:
                return value
        missing += delta-1
        burst['failed_probes'] += delta-1
        explanations.append(dict(previous_sequence=prior['icmp_sequence'], next_sequence=current['icmp_sequence'],
            local_send_failures=delta-1, classification='source-host-unreachable',
            gap_start_timestamp=prior['ts'], gap_end_timestamp=current['ts']))
    if not missing:
        return value
    wire = copy.deepcopy(value)
    attempts = value['packets_attempted']+missing
    failures = value['packets_failed']+missing
    longest = max(bursts, key=lambda b:b['duration_seconds'])
    result = dict(value, status='PASS', metric_schema_version=2,
        measurement_kind='icmp-probe-availability', coverage_complete=True,
        coverage_scope='All probe attempts classified; raw PCAP wire coverage remains separate',
        measurement_basis='sender-pcap-and-verified-local-errors', wire_capture_metrics=wire,
        local_send_failures=missing, probe_attempts=attempts, probe_failures=failures,
        probe_failure_percent=100*failures/attempts, reconciled_gaps=explanations,
        failure_bursts=bursts, failure_burst_count=len(bursts),
        maximum_consecutive_failed_probes=max(b['failed_probes'] for b in bursts),
        actual_dataplane_outage_seconds=longest['duration_seconds'],
        longest_outage_start_timestamp=longest['start_timestamp'],
        longest_outage_recovery_timestamp=longest['recovery_timestamp'],
        total_downtime_seconds=sum(b['duration_seconds'] for b in bursts))
    result.pop('reason', None)
    return result
