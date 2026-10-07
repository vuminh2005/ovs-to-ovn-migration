#!/usr/bin/env python3
import json, pathlib, sys
from workload_validation import validation_ready
root = pathlib.Path(sys.argv[1])
m = root/'metrics'
def n(name):
    p=m/name
    return float(p.read_text().strip()) if p.exists() else None
def dur(a,b):
    x,y=n(a),n(b)
    return round(y-x,3) if x is not None and y is not None else None
phases={}
for i in range(0,10):
    d=dur(f'phase{i:02d}.start',f'phase{i:02d}.end')
    if d is not None: phases[f'phase_{i:02d}']=d
consistency=json.loads((root/'resource-consistency.json').read_text()) if (root/'resource-consistency.json').exists() else {}
before=json.loads((root/'resource-counts.before.json').read_text()) if (root/'resource-counts.before.json').exists() else {}
smoke=json.loads((root/'new-network-smoke.json').read_text()) if (root/'new-network-smoke.json').exists() else None
report={
  'result':'SUCCESS',
  'run_id':sys.argv[2],
  'inventory':sys.argv[3],
  'total_duration_seconds':dur('total.start','total.end'),
  'control_plane_downtime_seconds':dur('control_plane_downtime.start','control_plane_downtime.end'),
  'db_migration_seconds':dur('db_migration.start','db_migration.end'),
  'dataplane_convergence_seconds':dur('dataplane_convergence.start','dataplane_convergence.end'),
  'phase_durations_seconds':phases,
  'resources_before':before,
  'resource_consistency':consistency,
  'new_network_smoke':smoke,
  'limitations':[
     'Tenant packet evidence is required; PortBinding convergence is separate.',
     'v2 intentionally fails on provider/external networks, floating IPs, router external gateways, DVR and Neutron agent HA because those paths are not yet validated.',
     'Guest static MTU cannot be changed automatically; existing VXLAN network MTUs are reduced by the configured VXLAN-to-Geneve delta.'
  ]
}
def evidence(name):
    p=root/name
    return json.loads(p.read_text()) if p.exists() else None
def status(name, keys):
    rows=evidence(name)
    if not rows: return 'NOT TESTED'
    values=[r.get(k, 'UNAVAILABLE') for r in rows.values() for k in keys]
    return 'FAIL' if 'FAIL' in values else ('PASS' if all(v=='PASS' for v in values) else 'UNAVAILABLE')
report.update({
  'existing_workload_post_migration_validation':status('pre-workload-checks.json', ['identity','active','bound','dhcp_availability','dhcp_convergence','connectivity','metadata']),
  'existing_workload_post_migration_connectivity':status('pre-workload-checks.json', ['connectivity']),
  'existing_workload_identity_preservation':status('pre-workload-checks.json', ['identity']),
  'existing_workload_cleanup':(evidence('pre-cleanup.json') or {}).get('status','NOT TESTED'),
  'existing_workload_cleanup_evidence':evidence('pre-cleanup.json'),
  'existing_workload_dhcp_availability':status('pre-workload-checks.json', ['dhcp_availability']),
  'existing_workload_dhcp_convergence':status('pre-workload-checks.json', ['dhcp_convergence']),
  'existing_workload_metadata':status('pre-workload-checks.json', ['metadata']),
  'new_ovn_workload_provisioning':status('post-workload-checks.json', ['identity','active','bound']),
  'new_ovn_workload_dhcp':status('post-workload-checks.json', ['dhcp','dhcp_convergence']),
  'new_ovn_workload_connectivity':status('post-workload-checks.json', ['connectivity']),
  'new_ovn_workload_metadata':status('post-workload-checks.json', ['metadata']),
  'new_ovn_workload_cleanup':(evidence('post-cleanup.json') or {}).get('status','NOT TESTED'),
  'existing_network_vxlan_geneve_semantics':(evidence('existing-network-semantics.json') or {}).get('status','UNAVAILABLE'),
  'existing_network_semantics':evidence('existing-network-semantics.json'),
  'initial_ovs_workload_validation':status('initial-workload-checks.json', ['identity','active','bound','dhcp','connectivity','metadata']),
  'workload_checks':{'initial':evidence('initial-workload-checks.json'), 'existing':evidence('pre-workload-checks.json'), 'new':evidence('post-workload-checks.json')},
  'ovn_portbinding_convergence_seconds':report['dataplane_convergence_seconds'],
  'validation_orchestration':evidence('validation-orchestration.json'),
})
report['new_ovn_workload_bindings']=(evidence('post-ovn-bindings.json') or {}).get('status','NOT TESTED')
report['dataplane_probe']=evidence('tenant-dataplane-probe.json') or {'status':'NOT TESTED'}
if report['dataplane_probe'].get('status')=='PASS' and (report['dataplane_probe'].get('measurement_workload')!='Pair A' or report['dataplane_probe'].get('evidence_source')!='compute-tap-pcap'):
    report['historical_dataplane_probe']=report['dataplane_probe']
    report['dataplane_probe']={'status':'NOT TESTED', 'reason':'Historical console probe cannot serve as authoritative compute-PCAP measurement'}
state = evidence('validation-resources.json') or {}
modern = state.get('schema_version') == 2 and not state.get('historical_dual_pair')
automatic = evidence('existing-mtu-automatic.json') or {}
remediation = evidence('existing-mtu-remediation.json') or {}
readiness = evidence('dhcp-precutover-preparation.json') or {}
post_automatic = evidence('existing-post-cutover-automatic.json') or {}
post_remediation = evidence('existing-post-cutover-remediation.json') or {}
post_readiness = evidence('existing-post-cutover-readiness.json') or {}
measurement = evidence('measure-post-checks.json') or {}
def geneve_result():
    rows=evidence('post-workload-checks.json') or {}
    return ('PASS' if set(rows)=={'0','1'} and all(r.get('network_type')=='geneve' for r in rows.values())
            else 'FAIL' if rows else 'NOT TESTED')
report.update({
  'automatic_mtu_convergence':automatic.get('status','NOT TESTED'),
  'remediation_required':remediation.get('remediation_required'),
  'remediation_action':remediation.get('remediation_action','NOT TESTED'),
  'pre_cutover_mtu_readiness':readiness.get('status','NOT TESTED'),
  'existing_workload_mtu':status('pre-workload-checks.json',['mtu']),
  'existing_workload_boot_continuity':status('pre-workload-checks.json',['boot_continuity']),
  'new_ovn_workload_mtu':status('post-workload-checks.json',['mtu']),
  'new_ovn_workload_geneve':geneve_result(),
})
report.update({
  'automatic_guest_mtu_convergence':report['automatic_mtu_convergence'],
  'pre_cutover_remediation_required':report['remediation_required'],
  'pre_cutover_remediation_action':report['remediation_action'],
  'automatic_post_cutover_dhcp_convergence':post_automatic.get('status','NOT TESTED'),
  'post_cutover_remediation_required':post_remediation.get('remediation_required'),
  'post_cutover_remediation_action':post_remediation.get('remediation_action','NOT TESTED'),
  'post_cutover_dhcp_ready':post_readiness.get('dhcp_ready','UNAVAILABLE'),
  'post_cutover_metadata_route_ready':post_readiness.get('metadata_route_ready','UNAVAILABLE'),
  'post_cutover_metadata_ready':post_readiness.get('metadata_ready','UNAVAILABLE'),
  'server_uuid_preserved':status('pre-workload-checks.json',['server_uuid_preserved']),
  'port_uuid_preserved':status('pre-workload-checks.json',['port_uuid_preserved']),
  'fixed_ip_preserved':status('pre-workload-checks.json',['fixed_ip_preserved']),
  'pair_a_capture':evidence('pair-a-capture.json'),
})
report.update({
  'dataplane_continuity':{
    'measurement_workload':report['dataplane_probe'].get('measurement_workload','NOT TESTED'),
    'measurement_type':'small-packet routed tenant dataplane',
    'packet_loss_percent':report['dataplane_probe'].get('packet_loss_percent'),
    'actual_dataplane_outage_seconds':report['dataplane_probe'].get('actual_dataplane_outage_seconds'),
    'pair_a_boot_continuity':report['dataplane_probe'].get('pair_a_boot_continuity','UNAVAILABLE'),
    'pair_a_checks':measurement,
  },
  'existing_workload_migration':{
    'resource_preservation':report['existing_workload_identity_preservation'],
    'dhcp_renewal_delivery':(evidence('dhcp-initial-preparation.json') or {}).get('status','NOT TESTED'),
    'automatic_mtu_convergence':automatic.get('status','NOT TESTED'),
    'remediation_required':remediation.get('remediation_required'),
    'remediation_action':remediation.get('remediation_action','NOT TESTED'),
    'pre_cutover_mtu_readiness':readiness.get('status','NOT TESTED'),
    'post_migration_mtu':report['existing_workload_mtu'],
    'metadata':report['existing_workload_metadata'],
    'routed_connectivity':report['existing_workload_post_migration_connectivity'],
    'migration_boot_continuity':report['existing_workload_boot_continuity'],
    'remediation_evidence':remediation,
    'post_cutover_automatic':post_automatic,
    'post_cutover_remediation':post_remediation,
    'post_cutover_readiness':post_readiness,
    'post_cutover_baseline':evidence('existing-post-cutover-baseline.json'),
    'migration_baseline':evidence('existing-migration-baseline.json'),
  },
  'fresh_ovn_provisioning':{
    'provisioning':report['new_ovn_workload_provisioning'], 'geneve':geneve_result(),
    'dhcp':report['new_ovn_workload_dhcp'], 'mtu':report['new_ovn_workload_mtu'],
    'metadata':report['new_ovn_workload_metadata'], 'routed_connectivity':report['new_ovn_workload_connectivity'],
    'ovn_binding':report['new_ovn_workload_bindings'],
  },
})
if modern:
    report['existing_workload_post_migration_validation']=status('pre-workload-checks.json',
        ['identity','active','bound','dhcp_availability','dhcp_convergence','connectivity','metadata','mtu','boot_continuity'])
report['existing_workload_migration']['status']=report['existing_workload_post_migration_validation']
fresh_values=list(report['fresh_ovn_provisioning'].values())
report['fresh_ovn_provisioning']['status']=('PASS' if all(v=='PASS' for v in fresh_values) else
                                           'FAIL' if 'FAIL' in fresh_values else 'UNAVAILABLE')
report['dataplane_continuity']['status']=report['dataplane_probe']['status']
statuses=[v for k,v in report.items() if (k.startswith(('existing_','new_ovn_')) or k=='initial_ovs_workload_validation') and isinstance(v,str)]
if not validation_ready(root) or any(v!='PASS' for v in statuses) or report['dataplane_probe']['status']!='PASS':
    report['result']='MIGRATED_VALIDATION_INCOMPLETE'
orchestration=report['validation_orchestration'] or {}
if orchestration.get('semantics_rc',0) or orchestration.get('workload_rc',0):
    report['result']='MIGRATED_VALIDATION_INCOMPLETE'
if report['result']=='SUCCESS' and post_remediation.get('remediation_action')=='soft reboot' and post_remediation.get('status')=='PASS':
    report['result']='SUCCESS_WITH_REMEDIATION'
    report['existing_workload_migration']['status']='PASS AFTER REMEDIATION'
report['limitations'][0]='Pair-A metrics require a continuous drop-free compute-tap PCAP, endpoints and request cadence; gaps or unrecovered loss yield UNAVAILABLE. Console packet history is secondary only. PortBinding convergence remains separate.'
(root/'migration-report.json').write_text(json.dumps(report,indent=2,sort_keys=True))
lines=[
  f"RESULT: {report['result']}",
  f"Run: {report['run_id']}",
  f"Total duration: {report['total_duration_seconds']} s",
  f"Control-plane downtime: {report['control_plane_downtime_seconds']} s",
  f"DB migration: {report['db_migration_seconds']} s",
  f"Dataplane Port_Binding convergence: {report['dataplane_convergence_seconds']} s",
  'Dataplane continuity:',
  'Measurement workload: ' + report['dataplane_continuity']['measurement_workload'],
  'Measurement type: small-packet routed tenant dataplane',
  'Authoritative evidence: compute-tap PCAP',
  'Packet loss: ' + (str(report['dataplane_probe'].get('packet_loss_percent')) + ' %' if report['dataplane_probe']['status']=='PASS' else 'UNAVAILABLE'),
  'Actual dataplane outage: ' + (str(report['dataplane_probe'].get('actual_dataplane_outage_seconds')) + ' s' if report['dataplane_probe']['status']=='PASS' else 'UNAVAILABLE'),
  'Pair-A boot continuity: ' + report['dataplane_continuity']['pair_a_boot_continuity'],
  "Resource preservation: " + ("PASS" if consistency and all(v.get('unchanged') for v in consistency.values()) else "FAIL"),
  "New Geneve network smoke test: " + ("PASS" if smoke and smoke.get('network_type')=='geneve' else "NOT RUN/FAIL"),
]
lines += ['Existing workload migration (Pair B):',
          ('Initial Pair-B validation under ML2/OVS: ' if modern else 'Initial VM1/VM2 validation under ML2/OVS: ') + report['initial_ovs_workload_validation'],
          'Automatic guest MTU convergence: ' + report['automatic_mtu_convergence'],
          'Pre-cutover remediation required: ' + ('YES' if report['remediation_required'] is True else 'NO' if report['remediation_required'] is False else 'UNAVAILABLE'),
          'Remediation: ' + report['remediation_action'],
          'Pre-cutover MTU readiness: ' + report['pre_cutover_mtu_readiness'],
          'Automatic post-cutover DHCP convergence: ' + report['automatic_post_cutover_dhcp_convergence'],
          'Post-cutover remediation required: ' + ('YES' if report['post_cutover_remediation_required'] is True else 'NO' if report['post_cutover_remediation_required'] is False else 'UNAVAILABLE'),
          'Post-cutover remediation: ' + report['post_cutover_remediation_action'],
          'Post-cutover DHCP readiness: ' + report['post_cutover_dhcp_ready'],
          'Post-cutover metadata route readiness: ' + report['post_cutover_metadata_route_ready'],
          'Post-cutover metadata readiness: ' + report['post_cutover_metadata_ready'],
          'Existing workload migration: ' + report['existing_workload_migration']['status'],
          'Pair-B resource preservation: ' + report['existing_workload_identity_preservation'],
          'Pair-B DHCP renewal/delivery: ' + report['existing_workload_migration']['dhcp_renewal_delivery'],
          'Pair-B post-migration MTU: ' + report['existing_workload_mtu'],
          'Pair-B metadata: ' + report['existing_workload_metadata'],
          'Pair-B routed connectivity: ' + report['existing_workload_post_migration_connectivity'],
          'Pair-B migration boot continuity: ' + report['existing_workload_boot_continuity'],
          'Fresh OVN provisioning (Pair C): ' + report['fresh_ovn_provisioning']['status'],
          'Pair-C Geneve networks: ' + report['new_ovn_workload_geneve'],
          'Pair-C DHCP: ' + report['new_ovn_workload_dhcp'],
          'Pair-C MTU: ' + report['new_ovn_workload_mtu'],
          'Pair-C metadata: ' + report['new_ovn_workload_metadata'],
          'Pair-C routed connectivity: ' + report['new_ovn_workload_connectivity'],
          'Pair-C OVN binding: ' + report['new_ovn_workload_bindings'],
          'Individual check fields:']
lines += [k + ': ' + v for k,v in report.items() if k.startswith(('existing_', 'new_ovn_')) and isinstance(v,str)]
lines += ['Tenant dataplane probe: ' + json.dumps(report['dataplane_probe'], sort_keys=True)]
(root/'migration-report.txt').write_text('\n'.join(lines)+'\n')
print('\n'.join(lines))
