"""Presentation only: preserve report data and render its selected text fields."""
import math


# These are existing report keys, ordered by purpose without changing the schema.
# Unknown future keys are appended so presentation never discards metric data.
JSON_GROUPS = (
    ('result', 'run_id', 'inventory'),
    ('total_duration_seconds', 'total_duration_scope',
     'control_plane_downtime_seconds', 'db_migration_seconds',
     'ovn_portbinding_convergence_seconds', 'dataplane_convergence_seconds',
     'phase_durations_seconds', 'phase_timings', 'phase_timing_scope',
     'phase_marker_schema_version', 'phase_marker_schema_source'),
    ('resources_before', 'resource_consistency',
     'existing_network_vxlan_geneve_semantics', 'existing_network_semantics',
     'new_network_smoke', 'workload_placement'),
    ('dataplane_continuity', 'dataplane_probe', 'pair_a_capture',
     'historical_dataplane_probe'),
    ('existing_workload_migration', 'initial_ovs_workload_validation',
     'existing_workload_post_migration_validation',
     'existing_workload_identity_preservation',
     'server_uuid_preserved', 'port_uuid_preserved', 'fixed_ip_preserved',
     'existing_workload_dhcp_availability', 'existing_workload_dhcp_convergence',
     'existing_workload_mtu', 'existing_workload_metadata',
     'existing_workload_post_migration_connectivity',
     'existing_workload_boot_continuity',
     'automatic_guest_mtu_convergence', 'automatic_mtu_convergence',
     'pre_cutover_remediation_required', 'remediation_required',
     'pre_cutover_remediation_action', 'remediation_action',
     'pre_cutover_mtu_readiness', 'automatic_post_cutover_dhcp_convergence',
     'post_cutover_remediation_required', 'post_cutover_remediation_action',
     'post_cutover_dhcp_ready', 'post_cutover_metadata_route_ready',
     'post_cutover_metadata_ready'),
    ('fresh_ovn_provisioning', 'new_ovn_workload_provisioning',
     'new_ovn_workload_geneve', 'new_ovn_workload_bindings',
     'new_ovn_workload_dhcp', 'new_ovn_workload_mtu',
     'new_ovn_workload_metadata', 'new_ovn_workload_connectivity'),
    ('pair_d_tcp', 'ew_connectivity_matrix', 'north_south', 'workload_capacity', 'instance_quota'),
    ('existing_workload_cleanup', 'existing_workload_cleanup_evidence',
     'new_ovn_workload_cleanup'),
    ('workload_checks', 'validation_orchestration', 'orchestration_recovery', 'limitations'),
)


def ordered_report(report):
    ordered = {key: report[key] for group in JSON_GROUPS
               for key in group if key in report}
    ordered.update({key: value for key, value in report.items() if key not in ordered})
    return ordered


def numeric(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def seconds(value):
    return f'{value:.3f} s' if numeric(value) else 'UNAVAILABLE'


def total_time(value):
    if not numeric(value):
        return 'UNAVAILABLE'
    milliseconds = round(value * 1000)
    minutes, remainder = divmod(milliseconds, 60000)
    return f'{seconds(value)} ({minutes}m {remainder / 1000:.3f}s)'


def check(value):
    return value if isinstance(value, str) and value else 'UNAVAILABLE'


def combined_check(*values):
    if 'FAIL' in values:
        return 'FAIL'
    if values and all(value == 'PASS' for value in values):
        return 'PASS'
    if values and all(value == 'NOT TESTED' for value in values):
        return 'NOT TESTED'
    return 'UNAVAILABLE'


def packet_loss(probe):
    value = probe.get('packet_loss_percent')
    if probe.get('status') != 'PASS' or not numeric(value) or value > 100:
        return 'UNAVAILABLE'
    output = f'{value:.4f}%'
    failed, attempted = probe.get('packets_failed'), probe.get('packets_attempted')
    if type(failed) is int and type(attempted) is int and 0 <= failed <= attempted and attempted > 0:
        output += f' ({failed} / {attempted} probes)'
    return output


def counter(value):
    return str(value) if type(value) is int and value >= 0 else 'UNAVAILABLE'


def probe_failure_rate(stream):
    value = stream.get('failed_probe_percent')
    failed, attempted = stream.get('failures'), stream.get('attempts')
    counts_valid = (type(failed) is int and type(attempted) is int
                    and 0 <= failed <= attempted and attempted > 0)
    if not numeric(value) or value > 100:
        value = 100 * failed / attempted if counts_valid else None
    if value is None:
        return 'UNAVAILABLE'
    output = f'{value:.4f}%'
    if counts_valid:
        output += f' ({failed} / {attempted} probes)'
    return output


def render_text(report, root, *, modern):
    """Traffic summary only; the JSON remains the complete validation report.

    Dataplane downtime is a heading for the per-flow traffic interruption metrics.
    Port_Binding convergence and all event timestamps remain in JSON only.
    """
    lines = []

    def field(label, value, indent=0):
        lines.append(' ' * indent + f'{label:<26}: {value}')

    def heading(label, indent=0):
        lines.extend(['', ' ' * indent + label])

    def timeline(key):
        events = report.get('traffic_timeline', {}).get(key)
        if not events:
            return
        from traffic_timeline import display
        for field_name, label in (('start_timestamp', 'Start measurement'), ('end_timestamp', 'End measurement'),
            ('first_failure_timestamp', 'First failure'), ('recovery_timestamp', 'First recovery')):
            event = events[field_name]
            value = 'NONE (no failure)' if field_name in ('first_failure_timestamp', 'recovery_timestamp') and events.get('failure_burst_count') == 0 else display(event, include_timestamp=False)
            field(label, value, 6)

    def icmp(label, flow, result=None, timeline_key=None):
        heading(label, 2)
        field('Result', check(result or flow.get('status')), 4)
        reconciled = flow.get('measurement_kind') == 'icmp-probe-availability'
        if reconciled:
            probe = dict(status=flow.get('status'), attempts=flow.get('probe_attempts'), failures=flow.get('probe_failures'))
            field('Probe failure rate', probe_failure_rate(probe), 4)
            field('Local send failures', counter(flow.get('local_send_failures')), 4)
            field('Measurement basis', 'PCAP + verified local errors', 4)
        else:
            field('Packet loss', packet_loss(flow), 4)
        field('Failure burst count', counter(flow.get('failure_burst_count'))
              if flow.get('status') == 'PASS' else 'UNAVAILABLE', 4)
        field('Longest probe outage' if reconciled else 'Longest packet outage', seconds(flow.get('actual_dataplane_outage_seconds'))
              if flow.get('status') == 'PASS' else 'UNAVAILABLE', 4)
        if 'total_downtime_seconds' in flow:
            field('Total probe downtime' if reconciled else 'Total packet downtime', seconds(flow.get('total_downtime_seconds')), 4)
        if timeline_key:
            timeline(timeline_key)

    def tcp(label, value, names, direction=None):
        heading(label, 2)
        status = value.get('status')
        if status in ('DISABLED', 'NOT TESTED'):
            field('Result', status, 4)
            return
        for key, name in names:
            stream = (value.get('streams') or {}).get(key) or {}
            heading(name, 4)
            field('Result', check(stream.get('status')), 6)
            field('Probe failure rate', probe_failure_rate(stream), 6)
            field('Failure burst count', counter(stream.get('failure_burst_count')), 6)
            field('Longest probe failure', 'UNRECOVERED'
                  if stream.get('open_failure_since') is not None
                  else seconds(stream.get('longest_recovered_failure_seconds')), 6)
            if 'total_downtime_seconds' in stream:
                field('Total probe downtime', 'UNRECOVERED' if stream.get('open_failure_since') is not None else seconds(stream.get('total_downtime_seconds')), 6)
            if direction:
                timeline(direction+'/'+key)

    field('Total run duration', total_time(report.get('total_duration_seconds')))
    field('Control-plane downtime', seconds(report.get('control_plane_downtime_seconds')))
    heading('Dataplane downtime:')

    heading('1. EAST - WEST')
    matrix = report.get('ew_connectivity_matrix') or {}
    flows = matrix.get('flows') or {}
    primary = report.get('dataplane_probe') or flows.get('ew_different_compute_different_subnet') or {}
    icmp('Different compute / different subnet (ICMP)', primary,
         (report.get('dataplane_continuity') or {}).get('status'),
         timeline_key='ew_different_compute_different_subnet')
    for key, name in (
        ('ew_different_compute_same_subnet', 'Different compute / same subnet (ICMP)'),
        ('ew_same_compute_different_subnet', 'Same compute / different subnet (ICMP)'),
        ('ew_same_compute_same_subnet', 'Same compute / same subnet (ICMP)'),
    ):
        flow = flows.get(key) or {}
        if matrix.get('status') in ('DISABLED', 'NOT TESTED'):
            flow = {'status': matrix['status']}
        icmp(name, flow, timeline_key=key)
    tcp('Different compute / different subnet (TCP)', report.get('pair_d_tcp') or {}, (
        ('held', 'Existing TCP session'),
        ('new_old', 'Old listener / new connection every probe'),
        ('new_listener', 'New listener / new connection every probe'),
    ), direction='ew_tcp')

    heading('2. NORTH - SOUTH')
    ns = report.get('north_south') or {}
    for direction, name in (('snat', 'Outbound (SNAT, TCP)'), ('fip', 'Inbound (FIP, TCP)')):
        value = (ns.get('directions') or {}).get(direction) or {}
        if ns.get('status') in ('DISABLED', 'NOT TESTED'):
            value = {'status': ns['status']}
        if ns.get('icmp_enabled'):
            icmp('Outbound (SNAT, ICMP)' if direction == 'snat' else 'Inbound (FIP, ICMP)',
                value.get('icmp') or {}, timeline_key=direction+'/icmp')
        old_key = 'new_old' if 'new_old' in value.get('streams', {}) else 'new'
        names = [('held', 'Existing TCP session'),
                 (old_key, 'Old listener / new connection every probe')]
        if ns.get('new_listener_enabled') or 'new_listener' in value.get('streams', {}):
            names.append(('new_listener', 'New listener / new connection every probe'))
        tcp(name, value, names, direction=direction)

    return '\n'.join(lines) + '\n'


def terminal_summary(report, root):
    return '\n'.join([f"RESULT: {report['result']}", f"Run: {report['run_id']}",
                      f'Text report: {root}/migration-report.txt',
                      f'JSON report: {root}/migration-report.json'])
