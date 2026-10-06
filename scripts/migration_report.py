#!/usr/bin/env python3
import json, pathlib, re, sys
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
probe_target=sys.argv[4]
packet_loss=None
outage=None
probe_note='not configured; set MIGRATION_PROBE_TARGET to a tenant-reachable canary from the deployment host'
plog=m/'dataplane-probe.log'
if probe_target and plog.exists():
    txt=plog.read_text(errors='replace')
    mm=re.search(r'(\d+(?:\.\d+)?)% packet loss',txt)
    if mm: packet_loss=float(mm.group(1))
    ts=[]
    for line in txt.splitlines():
        q=re.match(r'\[(\d+\.\d+)\].*icmp_seq=',line)
        if q: ts.append(float(q.group(1)))
    if len(ts)>1:
        gap=max(b-a for a,b in zip(ts,ts[1:]))
        outage=round(max(0.0,gap-float(sys.argv[5])),3)
    probe_note='measured from deployment host; estimated outage is largest reply gap minus probe interval'
report={
  'result':'SUCCESS',
  'run_id':sys.argv[2],
  'inventory':sys.argv[3],
  'total_duration_seconds':dur('total.start','total.end'),
  'control_plane_downtime_seconds':dur('control_plane_downtime.start','control_plane_downtime.end'),
  'db_migration_seconds':dur('db_migration.start','db_migration.end'),
  'dataplane_convergence_seconds':dur('dataplane_convergence.start','dataplane_convergence.end'),
  'dataplane_probe':{
     'target':probe_target or None,
     'packet_loss_percent':packet_loss,
     'estimated_outage_seconds':outage,
     'note':probe_note,
  },
  'phase_durations_seconds':phases,
  'resources_before':before,
  'resource_consistency':consistency,
  'new_network_smoke':smoke,
  'limitations':[
     'Real packet loss/downtime requires a canary reachable from the deployment host; Port_Binding convergence is always measured as a control-side proxy.',
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
  'existing_workload_post_migration_validation':status('pre-workload-checks.json', ['identity','active','bound','dhcp','connectivity','metadata']),
  'existing_workload_post_migration_connectivity':status('pre-workload-checks.json', ['connectivity']),
  'existing_workload_identity_preservation':status('pre-workload-checks.json', ['identity']),
  'existing_workload_cleanup':(evidence('pre-cleanup.json') or {}).get('status','NOT TESTED'),
  'existing_workload_cleanup_evidence':evidence('pre-cleanup.json'),
  'existing_workload_dhcp':status('pre-workload-checks.json', ['dhcp']),
  'existing_workload_metadata':status('pre-workload-checks.json', ['metadata']),
  'new_ovn_workload_provisioning':status('post-workload-checks.json', ['identity','active','bound']),
  'new_ovn_workload_dhcp':status('post-workload-checks.json', ['dhcp']),
  'new_ovn_workload_connectivity':status('post-workload-checks.json', ['connectivity']),
  'new_ovn_workload_metadata':status('post-workload-checks.json', ['metadata']),
  'new_ovn_workload_cleanup':(evidence('post-cleanup.json') or {}).get('status','NOT TESTED'),
  'existing_network_vxlan_geneve_semantics':(evidence('existing-network-semantics.json') or {}).get('status','UNAVAILABLE'),
  'existing_network_semantics':evidence('existing-network-semantics.json'),
  'initial_ovs_workload_validation':status('initial-workload-checks.json', ['identity','active','bound','dhcp','connectivity','metadata']),
  'workload_checks':{'initial':evidence('initial-workload-checks.json'), 'existing':evidence('pre-workload-checks.json'), 'new':evidence('post-workload-checks.json')},
  'deployment_host_probe':dict(report['dataplane_probe'], role='legacy optional deployment-host canary; separate from tenant probe'),
  'ovn_portbinding_convergence_seconds':report['dataplane_convergence_seconds'],
  'validation_orchestration':evidence('validation-orchestration.json'),
})
report['new_ovn_workload_bindings']=(evidence('post-ovn-bindings.json') or {}).get('status','NOT TESTED')
report['dataplane_probe']=evidence('tenant-dataplane-probe.json') or {'status':'NOT TESTED'}
statuses=[v for k,v in report.items() if (k.startswith(('existing_','new_ovn_')) or k=='initial_ovs_workload_validation') and isinstance(v,str)]
if not validation_ready(root) or any(v!='PASS' for v in statuses) or report['dataplane_probe']['status']!='PASS':
    report['result']='MIGRATED_VALIDATION_INCOMPLETE'
orchestration=report['validation_orchestration'] or {}
if orchestration.get('semantics_rc',0) or orchestration.get('workload_rc',0):
    report['result']='MIGRATED_VALIDATION_INCOMPLETE'
report['limitations'][0]='Tenant ICMP outage requires complete guest serial records; missing sequences or unrecovered loss produces UNAVAILABLE. PortBinding convergence remains separate.'
(root/'migration-report.json').write_text(json.dumps(report,indent=2,sort_keys=True))
lines=[
  f"RESULT: {report['result']}",
  f"Run: {report['run_id']}",
  f"Total duration: {report['total_duration_seconds']} s",
  f"Control-plane downtime: {report['control_plane_downtime_seconds']} s",
  f"DB migration: {report['db_migration_seconds']} s",
  f"Dataplane Port_Binding convergence: {report['dataplane_convergence_seconds']} s",
  f"Legacy optional deployment-host packet loss: {packet_loss if packet_loss is not None else 'N/A'}",
  f"Legacy optional deployment-host estimated outage: {outage if outage is not None else 'N/A'}",
  "Resource preservation: " + ("PASS" if consistency and all(v.get('unchanged') for v in consistency.values()) else "FAIL"),
  "New Geneve network smoke test: " + ("PASS" if smoke and smoke.get('network_type')=='geneve' else "NOT RUN/FAIL"),
]
lines += ["Initial VM1/VM2 validation under ML2/OVS: " + report['initial_ovs_workload_validation'],
          "Same VM1/VM2 post-migration validation under ML2/OVN: " + report['existing_workload_post_migration_validation']]
lines += [k + ': ' + v for k,v in report.items() if k.startswith(('existing_', 'new_ovn_')) and isinstance(v,str)]
lines += ['Tenant dataplane probe: ' + json.dumps(report['dataplane_probe'], sort_keys=True)]
(root/'migration-report.txt').write_text('\n'.join(lines)+'\n')
print('\n'.join(lines))
