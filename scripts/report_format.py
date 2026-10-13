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


def render_text(report, root, *, modern):
    major = '=' * 70
    minor = '-' * 70
    lines = [major, 'OVS -> OVN MIGRATION REPORT'.center(70).rstrip(), major,
             f"Result : {report['result']}", f"Run    : {report['run_id']}", '']
    lines.append({
        'SUCCESS': 'Migration and validation passed.',
        'SUCCESS_WITH_REMEDIATION': 'Migration passed. Existing DHCP workload required guest reboot remediation.',
        'MIGRATED_VALIDATION_INCOMPLETE': 'Migration completed; validation or cleanup is incomplete.',
    }.get(report['result'], 'See JSON report for the recorded result.'))

    def section(title):
        lines.extend(['', minor, title, minor])

    def field(label, value, indent=0):
        lines.append(' ' * indent + f'{label:<32}: {value}')

    def result(label, key):
        field(label, check(report.get(key)), 2)

    section('1. MIGRATION TIMING')
    field('Total run duration', total_time(report.get('total_duration_seconds')))
    field('Neutron control-plane downtime', seconds(report.get('control_plane_downtime_seconds')))
    field('DB migration', seconds(report.get('db_migration_seconds')))
    field('OVN Port_Binding convergence', seconds(report.get('ovn_portbinding_convergence_seconds')))
    lines.append('')
    if report.get('phase_marker_schema_version') == 2:
        lines.append('Total duration includes preparation, validation, cleanup and resume waits.')
    else:
        lines.extend(['Legacy total duration includes preparation, validation and resume waits;',
                      'excludes report/finalization and capture cleanup.'])
    lines.append('Port_Binding convergence and workload interruption have different scopes.')
    if report.get('orchestration_recovery'):
        lines.append('Resumed after DB migration: control-plane downtime includes the recovery pause.')

    section('2. RESOURCE AND NETWORK VALIDATION')
    consistency = report.get('resource_consistency') or {}
    preserved = ('PASS' if all(row.get('unchanged') for row in consistency.values()) else 'FAIL') if consistency else 'UNAVAILABLE'
    field('Resource identity preservation', preserved)
    field('Existing VXLAN -> Geneve checks', check(report.get('existing_network_vxlan_geneve_semantics')))
    field('Cross-compute workload placement', check((report.get('workload_placement') or {}).get('status')))

    section('3. WORKLOAD VALIDATION')
    probe = report.get('dataplane_probe') or {}
    continuity = report.get('dataplane_continuity') or {}
    lines.extend(['', 'EW - Different compute / different subnet (ICMP)'])
    field('Result', check(continuity.get('status')), 2)
    field('Packet loss', packet_loss(probe), 2)
    field('Longest packet outage', seconds(probe.get('actual_dataplane_outage_seconds'))
          if probe.get('status') == 'PASS' else 'UNAVAILABLE', 2)
    field('Guest boot continuity', check(continuity.get('pair_a_boot_continuity')), 2)
    field('Evidence', 'Compute-tap PCAP' if probe.get('evidence_source') == 'compute-tap-pcap'
          else 'UNAVAILABLE', 2)

    existing = report.get('existing_workload_migration') or {}
    lines.extend(['', 'EXISTING WORKLOAD - DHCP / MTU / metadata' if modern
                  else 'EXISTING WORKLOAD - Legacy VM1/VM2 migration'])
    field('Result', check(existing.get('status')), 2)
    result('Initial validation on OVS', 'initial_ovs_workload_validation')
    result('Server UUID preservation', 'server_uuid_preserved')
    result('Port UUID preservation', 'port_uuid_preserved')
    result('Fixed IP preservation', 'fixed_ip_preserved')
    field('DHCP', combined_check(report.get('existing_workload_dhcp_availability'),
                                 report.get('existing_workload_dhcp_convergence')), 2)
    for label, key in [('MTU', 'existing_workload_mtu'), ('Metadata', 'existing_workload_metadata'),
                       ('Routed connectivity', 'existing_workload_post_migration_connectivity'),
                       ('Boot check against baseline', 'existing_workload_boot_continuity')]:
        result(label, key)

    lines.extend(['', '  Remediation:', '    Before cutover:'])
    field('Automatic MTU convergence', check(report.get('automatic_guest_mtu_convergence')), 6)
    field('Action', check(report.get('pre_cutover_remediation_action')), 6)
    field('MTU readiness after action', check(report.get('pre_cutover_mtu_readiness')), 6)
    lines.extend(['', '    After cutover:'])
    field('Automatic DHCP convergence', check(report.get('automatic_post_cutover_dhcp_convergence')), 6)
    field('Action', check(report.get('post_cutover_remediation_action')), 6)
    field('DHCP readiness after action', check(report.get('post_cutover_dhcp_ready')), 6)
    if report.get('pre_cutover_remediation_action') == 'soft reboot' or report.get('post_cutover_remediation_action') == 'soft reboot':
        lines.extend(['', '  Boot checks account for the recorded remediation reboots.'])

    lines.extend(['', 'FRESH OVN - Provisioning / DHCP / metadata'])
    field('Result', check((report.get('fresh_ovn_provisioning') or {}).get('status')), 2)
    for label, key in [('Provisioning', 'new_ovn_workload_provisioning'),
                       ('Geneve networks', 'new_ovn_workload_geneve'),
                       ('OVN binding', 'new_ovn_workload_bindings'),
                       ('DHCP', 'new_ovn_workload_dhcp'), ('MTU', 'new_ovn_workload_mtu'),
                       ('Metadata', 'new_ovn_workload_metadata'),
                       ('Routed connectivity', 'new_ovn_workload_connectivity')]:
        result(label, key)

    tcp = report.get('pair_d_tcp') or {}
    lines.extend(['', 'EW TCP - Session continuity and new connections'])
    field('Result', check(tcp.get('status')), 2)
    if tcp.get('status') == 'DISABLED':
        lines.append('  TCP validation disabled for this run.')
    else:
        streams = tcp.get('streams') or {}
        lines.extend(['', '  Stream                        Result        Longest recovered probe failure'])
        names = [('held', 'Existing TCP session'),
                 ('new_old', 'New connection / old listener'),
                 ('new_listener', 'New connection / new listener')]
        for key, label in names:
            stream = streams.get(key) or {}
            lines.append(f"  {label:<30}{check(stream.get('status')):<14}{seconds(stream.get('longest_recovered_failure_seconds'))}")
        open_failures = [label for key, label in names if (streams.get(key) or {}).get('open_failure_since') is not None]
        if open_failures:
            lines.append('  Unrecovered probe failure: ' + '; '.join(open_failures))
        if tcp.get('listener_opened_during_freeze') is True:
            lines.extend(['', '  The new listener was opened during the Neutron freeze window.'])
        elif tcp.get('listener_opened_during_freeze') is False:
            lines.extend(['', '  New listener opening during the Neutron freeze was not proven.'])
        lines.append('  TCP durations measure application probe failures.')

    matrix = report.get('ew_connectivity_matrix') or {}
    for name, flow in (matrix.get('flows') or {}).items():
        if name == 'ew_different_compute_different_subnet':
            continue
        lines.extend(['', name.replace('_', ' ').upper()+' (ICMP)'])
        field('Result', check(flow.get('status')), 2)
        field('Packet loss', packet_loss(flow), 2)
        field('Longest packet outage', seconds(flow.get('actual_dataplane_outage_seconds')) if flow.get('status')=='PASS' else 'UNAVAILABLE', 2)
        field('Boot continuity', check(flow.get('boot_continuity')), 2)
    ns = report.get('north_south') or {}
    lines.extend(['', 'NORTH-SOUTH - SNAT outbound / FIP inbound'])
    field('Result', check(ns.get('status')), 2)
    for direction, value in (ns.get('directions') or {}).items():
        lines.append('  '+direction.upper())
        field('Held session preservation', check(value.get('held_session_preservation')), 4)
        for name, stream in value.get('streams', {}).items():
            field(name+' recovered probe failure', seconds(stream.get('longest_recovered_failure_seconds')), 4)
            field(name+' failed application probes', str(stream.get('failures'))+' / '+str(stream.get('attempts')), 4)
    if ns.get('reason'):
        field('Reason', ns['reason'], 2)

    section('4. VALIDATION RESOURCE CLEANUP')
    cleanup_label = ('Existing ICMP / DHCP workloads' if tcp.get('status') == 'DISABLED'
                     else 'Existing ICMP / DHCP / TCP workloads') if modern else 'Existing workloads (legacy)'
    field(cleanup_label,
          check(report.get('existing_workload_cleanup')))
    field('Fresh OVN workloads' if modern else 'Fresh OVN workloads (legacy)',
          check(report.get('new_ovn_workload_cleanup')))

    field('Extended EW / NS cleanup', check(report.get('extended_cleanup')))

    section('5. REPORT FILES')
    field('Directory', str(root) + '/')
    field('Text', 'migration-report.txt')
    field('JSON', 'migration-report.json')
    lines.append(major)
    return '\n'.join(lines) + '\n'


def terminal_summary(report, root):
    return '\n'.join([f"RESULT: {report['result']}", f"Run: {report['run_id']}",
                      f'Text report: {root}/migration-report.txt',
                      f'JSON report: {root}/migration-report.json'])
