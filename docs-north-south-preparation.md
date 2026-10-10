# Work Item 4 (Việc 4): prepare the existing OVS North–South path

Status: **prepared; pending operator execution**. Reviewed source revision:
`3bb95d6fc84c657d65c2a18567af7a6c95e94a90` (`main`). This guide changes no runtime
code. No SSH, cloud API, endpoint, service or lab workflow was executed during its
preparation. Reset is not a prerequisite. Work Item 5 calibrates observers;
Work Item 6 runs migration and produces measurements. Neither starts here.

## Implementation boundaries

Read [Work Item 3 scope](docs-north-south.md) and
[the input schema](examples/ns-inputs.yml). Initial scope is one existing IPv4
flat/VLAN external network, one external subnet, one centralized non-HA/non-DVR
router gateway with SNAT, and optionally one exact FIP. Every other network must
be a tenant VXLAN network. No direct provider-attached VMs or FIP port forwarding.
The egress guest must not be the FIP guest. Two gateway hosts do not establish HA.

The current code requires **all inventory network hosts**, also exactly the
`ovn-controller-network` group, as eligible gateways. Computes stay
provider-disabled. Kolla 18.8 single-bridge mapping is `physnet1`; every gateway
must use the same explicitly configured external bridge and uplink names.
The uplink must be an UP system interface on that bridge with its observed MAC,
sufficient MTU and no host addresses. A VLAN uplink must already exist in the
reviewed configuration. Do not derive VLAN layering from its name: double-tagging
a Neutron VLAN segment through a VLAN subinterface is not an approved shortcut.

`eth0`, `eth1`, `neutron-ext`, `br-ex` and underlay MTU 1450 are reported lab
context, not allocation evidence. Read effective settings. The current generation
computes tenant VXLAN/Geneve MTUs 1400/1392; external flat/VLAN MTU is separately
assigned and preserved. Do not reduce it by tenant overlay overhead, change guest
MTUs here, or reuse an already reduced network as an original source baseline.

| Interface | Reads / writes / starts | Appropriate now? |
| --- | --- | --- |
| Batch A below | Host/API reads; root-private local discovery evidence | Yes, operator-run |
| `ns-inspect.yml` | Bootstrap/runtime and private source evidence; host/API reads | Batch E, after complete inputs |
| `ns_workflow.py inspect` | Exact API scope, source Kolla/OVS checks | Invoked by `ns-inspect.yml`; no standalone fake runtime |
| `ns_workflow.py start` | Installs/starts finite observation processes and anchors | No; phase 04 of later migration |
| `ready`, `retire`, `cleanup`, `takeover`, `recovery`, `collect`, `finalize` | Later migration gates, mutations or observer lifecycle | No |
| `ew-provision.yml verify` | Source precheck and real smoke tasks | Before attaching an EW router gateway only; see Batch E |
| `endpoint.py` | Dedicated HTTP listener; optional echo listener only if requested | Batch D, separately approved host/service |

Source inspection verifies exact resource identities and mappings. It does not
test HTTP, acquire upstream authorization or require target OVN NAT, redirect
bindings, localnet patches, recovery fences or measurement anchors. Those target
gates remain mandatory later. L3 service-provider associations are separate from
physical-network mappings.

## Batch A — read-only discovery, before choosing configuration

Run on the controller, as root. The known paths below are checked, not created or
assumed usable. Override environment variables if the actual retained paths
differ. `NS_ACCESS` must select the existing private EW access file, including
the active generation's guest trust/state overrides if relevant; never substitute
a historical generation. Do not print this file or an OpenRC/password file.

First inspect Git; this block does not update the checkout or contact hosts:

```bash
set -euo pipefail
NS_REPO=${NS_REPO:-/root/ovs-to-ovn-migration}
cd "$NS_REPO"
git status --short --untracked-files=all
git branch --show-current
git rev-parse HEAD
git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}'
git log -3 --format='%H %s'
git rev-list --left-right --count HEAD...'@{upstream}'
```

If this is clean `main` tracking `origin/main`, the remote is the stated GitHub
repository, and the operator has reviewed the state, this **separate checkout
update** can fast-forward to the reviewed revision. Fetch updates local tracking
refs; it changes no cloud resources. It refuses local changes, divergence, a
newer local HEAD or the wrong tracking branch; do not stash/reset around a refusal.
Inspect `git remote get-url origin` privately first (do not share a credential URL).

```bash
set -euo pipefail
cd "${NS_REPO:-/root/ovs-to-ovn-migration}"
test -z "$(git status --porcelain --untracked-files=all)"
test "$(git branch --show-current)" = main
test "$(git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}')" = origin/main
REVIEWED=3bb95d6fc84c657d65c2a18567af7a6c95e94a90
git fetch origin
git merge-base --is-ancestor "$REVIEWED" origin/main
git merge-base --is-ancestor HEAD "$REVIEWED"
git merge --ff-only "$REVIEWED"
test "$(git rev-parse HEAD)" = "$REVIEWED"
```

After Git review/synchronization, the following **read-only discovery batch** is
runnable without an external allocation. It uses the existing strict management
transport, preserves per-host key selection, and writes only a new private local
evidence directory. SSH conflicts/authentication errors stop the affected read;
no keyscan, trust replacement, permissive fallback or service startup is used.
It reads all five hosts, including both gateways. API/host failures retain partial
evidence and make the batch fail; discovery never declares the N-S path ready.

```bash
set -euo pipefail
umask 077
export NS_REPO=${NS_REPO:-/root/ovs-to-ovn-migration}
export NS_VENV=${NS_VENV:-/root/venvs/kolla-2024.1}
export NS_INVENTORY=${NS_INVENTORY:-/root/multinode}
export NS_ACCESS=${NS_ACCESS:-/root/ew-access.yml}
test "$(id -u)" -eq 0
test -f "$NS_VENV/bin/activate"
test -f "$NS_INVENTORY"
test -f "$NS_ACCESS"
source "$NS_VENV/bin/activate"
cd "$NS_REPO"
test "$(git rev-parse HEAD)" = 3bb95d6fc84c657d65c2a18567af7a6c95e94a90
# The preparation documentation may be present as a reviewed uncommitted patch.
git diff --exit-code -- scripts playbooks group_vars workloads '*.yml'
test -z "$(git diff --cached --name-only)"
export NS_EVIDENCE
NS_EVIDENCE=$(mktemp -d /root/ns-preparation-XXXXXXXX)
chmod 0700 "$NS_EVIDENCE"
python3 - <<'PY'
import importlib.metadata as md
import json, os, pathlib, subprocess, sys, yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

repo = pathlib.Path(os.environ['NS_REPO']).resolve()
out = pathlib.Path(os.environ['NS_EVIDENCE'])
sys.path.insert(0, str(repo/'scripts'))
from ew_transport import Transport
from ns_workflow import remote_code
from reset_workflow import management_trust

def save(name, value):
    path = out/name
    with path.open('x') as f:
        json.dump(value, f, indent=2); f.write('\n')
    path.chmod(0o600)

inventory_path = os.environ['NS_INVENTORY']
inventory = json.loads(subprocess.check_output(
    ['ansible-inventory', '-i', inventory_path, '--list',
     '-e', '@'+os.environ['NS_ACCESS']], text=True))
def members(name):
    group = inventory.get(name, {})
    return sorted(set(group.get('hosts', []) +
                      [h for child in group.get('children', []) for h in members(child)]))
groups = {g: members(g) for g in inventory if g != '_meta'}
for group in ('control', 'network', 'compute'):
    if not groups.get(group): raise RuntimeError('Missing inventory group: '+group)
hosts = sorted(set(groups['control'] + groups['network'] + groups['compute']))
values = yaml.safe_load((repo/'group_vars/all.yml').read_text())
values.update(yaml.safe_load(pathlib.Path(os.environ['NS_ACCESS']).read_text()) or {})
values.update(groups=groups, kolla_inventory_runtime=inventory_path)
template = Templar(loader=DataLoader(), variables=values)
cfg = template.template(values['ns_transport'], fail_on_undefined=True)
for key in ('host_known_hosts',):
    if not pathlib.Path(cfg[key]).is_file(): raise RuntimeError('Missing verified management trust file')
transport = Transport(cfg, {}, inventory=inventory)
save('management-trust-vars.json', management_trust(
    {'host_known_hosts': cfg['host_known_hosts'], 'hosts': hosts}, inventory))
globals_path = pathlib.Path(template.template(values['kolla_globals_path']))
openrc = template.template(values['openrc_path'])
state_dir = pathlib.Path(template.template(values['ew_provision_state_dir']))
save('context.json', dict(commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
    inventory=inventory_path, access=os.environ['NS_ACCESS'], globals=str(globals_path),
    openrc=openrc, ew_state=str(state_dir), groups=groups,
    versions={p: md.version(p) for p in ('ansible-core','openstacksdk','PyYAML')}))
save('transport.json', cfg)  # private paths/overrides, never publish this file
source = yaml.safe_load(globals_path.read_text())
keys = ('openstack_release','neutron_plugin_agent','neutron_tenant_network_types',
        'enable_neutron_dvr','enable_neutron_agent_ha','enable_neutron_provider_networks',
        'network_interface','api_interface','tunnel_interface','neutron_external_interface',
        'neutron_bridge_name','global_physnet_mtu','neutron_path_mtu',
        'neutron_physical_network_mtus','neutron_type_drivers','neutron_flat_networks',
        'neutron_vlan_ranges')
save('globals-selected.json', {k: source.get(k) for k in keys})
prior = state_dir/'resources.json'
if prior.is_file():
    state = json.loads(prior.read_text())
    save('ew-retained-state.json', {k: state.get(k) for k in
         ('schema_version','ownership','resources','guests','preserved_tasks')})
failures = []
basic = '''import json,pathlib,socket,subprocess,time
def read(a): return subprocess.check_output(a,text=True,timeout=15)
print(json.dumps(dict(collected_at=time.time(),hostname=socket.gethostname(),
 product_uuid=pathlib.Path('/sys/class/dmi/id/product_uuid').read_text().strip(),
 boot=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
 links=json.loads(read(['ip','-j','-d','link'])),
 addresses=json.loads(read(['ip','-j','address'])),
 routes=json.loads(read(['ip','-j','route','show','table','all'])),
 containers=read(['docker','ps','-a','--format','{{.Names}} {{.Status}}']))))'''
settings = '''import configparser,json,pathlib,sys
base=pathlib.Path(sys.argv[1]); result={}
for file,sections in {'neutron.conf':{'DEFAULT':['core_plugin','service_plugins','global_physnet_mtu']},
 'ml2_conf.ini':{'ml2':['type_drivers','tenant_network_types','mechanism_drivers','path_mtu','physical_network_mtus','overlay_ip_version'],
 'ml2_type_flat':['flat_networks'],'ml2_type_vlan':['network_vlan_ranges'],'ml2_type_geneve':['max_header_size']},
 'openvswitch_agent.ini':{'ovs':['bridge_mappings','local_ip'],'securitygroup':['firewall_driver']},
 'l3_agent.ini':{'DEFAULT':['agent_mode','interface_driver']}}.items():
 path=base/file
 if file=='ml2_conf.ini' and base==pathlib.Path('/etc/neutron'): path=base/'plugins/ml2'/file
 if not path.is_file(): result[file]={'missing':True}; continue
 p=configparser.ConfigParser(interpolation=None); p.read(path)
 result[file]={s:{k:p.get(s,k,fallback=None) for k in keys} for s,keys in sections.items()}
print(json.dumps(result))'''
for host in hosts:
    try:
        save(host+'-identity-links.json', json.loads(transport.host(host,['python3','-c',basic])))
        if host in groups['network'] + groups['compute']:
            code = remote_code(['ovn_workload_evidence','ns_scope','ns_host'],
                'm=sys.modules["ns_host"]; e=m.read(); '
                'e["router_paths"]={n:dict(addresses=json.loads(m.command(["ip","netns","exec",n,"ip","-j","address"])), '
                'routes=json.loads(m.command(["ip","netns","exec",n,"ip","-j","route"]))) '
                'for n in e["namespaces"] if n.startswith("qrouter-")}; print(json.dumps(e))')
            save(host+'-ovs.json', json.loads(transport.host(host,['python3','-c',code])))
        if host in groups['network']:
            save(host+'-agent-settings.json', json.loads(transport.host(host,
                 ['python3','-c',settings,'/etc/kolla/neutron-openvswitch-agent'])))
            save(host+'-l3-settings.json', json.loads(transport.host(host,
                 ['python3','-c',settings,'/etc/kolla/neutron-l3-agent'])))
        if host in groups['control']:
            save(host+'-generated-settings.json', json.loads(transport.host(host,
                 ['python3','-c',settings,'/etc/kolla/neutron-server'])))
            save(host+'-running-settings.json', json.loads(transport.host(host,
                 ['docker','exec','neutron_server','python3','-c',settings,'/etc/neutron'])))
    except Exception as exc:
        failures.append(dict(host=host,error_type=type(exc).__name__,
                             reason=str(exc) if type(exc) is RuntimeError else 'Read failed; retain partial evidence'))
save('host-read-results.json', dict(failures=failures, readiness='NOT EVALUATED'))
PY
NS_OPENRC=$(python3 -c 'import json,os,pathlib; print(json.loads((pathlib.Path(os.environ["NS_EVIDENCE"])/"context.json").read_text())["openrc"])')
test -r "$NS_OPENRC"
# No shell tracing; source credentials without displaying their contents.
set +x
source "$NS_OPENRC"
python3 - <<'PY'
import json, os, pathlib, time, openstack
cloud = openstack.connect(api_timeout=10)
out = pathlib.Path(os.environ['NS_EVIDENCE'])
def rows(resources, keys):
    return [{k:getattr(r,k,None) for k in keys} for r in resources]
catalog = dict(collected_at=time.time(),
 networks=rows(cloud.network.networks(), ['id','name','is_router_external','provider_network_type','provider_physical_network','provider_segmentation_id','mtu','subnet_ids']),
 subnets=rows(cloud.network.subnets(), ['id','name','network_id','ip_version','cidr','gateway_ip','allocation_pools','is_dhcp_enabled','host_routes']),
 routers=rows(cloud.network.routers(), ['id','name','status','is_distributed','is_ha','external_gateway_info','routes']),
 ports=rows(cloud.network.ports(), ['id','name','network_id','device_id','device_owner','mac_address','fixed_ips','status','binding_host_id','binding_vif_type','binding_vnic_type','security_group_ids']),
 fips=rows(cloud.network.ips(), ['id','floating_network_id','router_id','port_id','fixed_ip_address','floating_ip_address','status']),
 servers=rows(cloud.compute.servers(all_projects=True), ['id','name','status','compute_host','availability_zone']),
 network_agents=rows(cloud.network.agents(), ['id','host','agent_type','is_alive','is_admin_state_up']),
 compute_services=rows(cloud.compute.services(), ['id','host','binary','state','status']),
 security_groups=rows(cloud.network.security_groups(), ['id','name']),
 security_group_rules=rows(cloud.network.security_group_rules(), ['id','security_group_id','direction','ether_type','protocol','remote_ip_prefix','remote_group_id','port_range_min','port_range_max']))
path=out/'cloud.json'
with path.open('x') as f: json.dump(catalog,f,indent=2); f.write('\n')
path.chmod(0o600)
failures=json.loads((out/'host-read-results.json').read_text())['failures']
print('Private discovery evidence: '+str(out))
if failures: raise SystemExit('Host reads incomplete; review host-read-results.json before proceeding')
PY
```

If a command stops, retain the displayed/exported `NS_EVIDENCE` directory; rerun
into a new directory after resolving the error. A missing configuration field is
unknown, not a default. Share reviewed nonsecret files through a private channel;
exclude `transport.json`, `management-trust-vars.json`, access/key/password files
and any other private operational details from Git or public attachments.

Review the output before Batch B: running `mechanism_drivers=openvswitch` and
VXLAN tenant types; all six EW placements and exact ports; router interface and
gateway state; live L3/OVS/DHCP agents; explicit source bridge/interface globals;
source agent `bridge_mappings`; allowed flat/VLAN physnets/ranges and physical
MTU limit; actual system uplink MAC/MTU/addressing/master and OVS UUID/bridge;
the `ovn-controller-network` group. Running and generated configurations may
differ: neither is silently substituted for the other. If backend is already
OVN, stop this OVS preparation workflow.

## Required external allocation record — blocks B/C/D/E if unresolved

| Required fact | Discovery or authority | Evidence needed |
| --- | --- | --- |
| Uplink on **each** gateway, MAC, parent/VLAN layering, MTU, bridge and OVS identities | Host evidence plus upstream owner | Dedicated role confirmed; no management/tunnel addresses or conflicting bridge membership |
| Flat or VLAN and exact tag if VLAN | Upstream allocation owner | Authorized segment, tagging on each virtual/physical port, no double tagging |
| External IPv4 CIDR, upstream gateway, pool, reservations and permitted gateway/FIP addresses | Allocation owner | Written allocation; no management-address derivation, no pool/gateway/reservation overlap |
| External segment MTU and end-to-end usable path MTU | Upstream owner plus traffic checks | Consistent bridge/uplink/physnet limits; upstream confirmation and sized path diagnostics if permitted |
| `physnet1`, single bridge and uniform uplink names | Existing Kolla config + host evidence | Exact supported mapping and source driver/range support |
| Upstream routes/return path | Upstream router owner + external host | Approved connected/static routes and successful bidirectional TCP/HTTP at assigned addresses |
| Nested MAC/IP permissions | Outer hypervisor/network owner + captures | Anti-spoof/allowed-address/MAC forwarding settings admit Neutron router/FIP traffic on **both** gateways, with traffic observed across the boundary |
| Controlled external endpoint | Endpoint owner | Beyond nested OpenStack external path, approved service IP/port/identity, return route/firewall, no unknown intervening NAT |
| Optional FIP, guest service and external observer | Allocation/service owners | Exact FIP association; dedicated service/port, observer source IP/DMI/boot and independently verified SSH keys |

Leave `upstream_routes_verified: false` until the owner confirms the actual
return path and it is exercised. Leave `nested_forwarding_verified: false` until
the outer owner confirms the relevant MAC/IP permissions and observed traffic
supports that configuration. A ping to a gateway, a bridge, carrier or one
interface listing alone does not establish either. Two available gateways must
each have an approved physical path; this is not a failover experiment. If the
upstream adds SNAT, the endpoint will not see the configured router gateway IP;
that topology cannot pass the current exact-peer contract without separate review.

## Batch B — reviewed physical/Kolla plan, not yet executable

Blocked until Batch A and the allocation record are reviewed. Compare existing
state to the requested segment before proposing any write. If they already match,
make no networking/service change. Otherwise prepare a file-level diff of the
actual `/etc/kolla/globals.yml` and any existing supported custom configuration:

- Keep management/API and tunnel interfaces, addresses, routes and MTUs intact.
  Preserve `enable_neutron_provider_networks: false`, DVR/agent HA off, OVS native
  firewall, VXLAN tenant types and authoritative MTU inputs.
- Use Kolla 18.8's installed Neutron/Open vSwitch configuration mechanism for
  explicit `neutron_external_interface` and `neutron_bridge_name`, type driver
  support and approved `physnet1` flat/VLAN range. Confirm the installed templates
  and inventory host overrides first. Do not hand-add an OVS port behind Kolla,
  reuse a management NIC, delete bridges or deploy the entire cloud as a shortcut.
- List the precise hosts and installed Kolla tags/roles affected before preparing
  a narrowly scoped `kolla-ansible reconfigure` command. Such reconfiguration can
  restart Neutron/OVS agents or interrupt existing traffic; schedule and authorize
  it separately. No universal reconfigure command is supplied while interface
  roles, template output and changes remain unknown.
- If required names differ by gateway, or provider computes/additional bridge
  mappings are necessary, stop: this exceeds the implemented scope.

After an approved change, repeat Batch A and review fresh running files, OVS
bridge/uplink membership and EW health. Preserve before/after evidence. Never set
the two verification attestations merely because reconfiguration completed.

## Batch C — approved external resources/SNAT, not yet executable

Blocked until B passes. Prepare a proposed resource table from `cloud.json` with
exact reused UUIDs, and a separately marked list of proposed creations. Query
each selected object again immediately before changes. Names alone are not proof
of ownership; duplicate names/conflicting CIDR/segment/pool/MTU are blockers.

1. Select the router actually containing the chosen egress guest's subnet. Its
   tenant interface UUIDs, addresses and routes must be preserved. `ew-router`
   is a candidate, not an automatic selection. Require centralized non-HA routing.
2. Reuse an exact approved external network/subnet, or separately authorize their
   creation with explicit flat/VLAN type, `physnet1`, VLAN ID only for VLAN,
   external flag, external MTU, IPv4 CIDR, upstream gateway and allocation pool.
   Review subnet DHCP policy explicitly; do not allocate guests on this network.
   Exclude reserved addresses. Do not mark a tenant network external or change its
   MTU to obtain an uplink. An existing incompatible object is not overwritten.
3. If no gateway exists, attach the approved external network to the selected
   router with SNAT enabled and an authorized external subnet/address. If a
   matching gateway/SNAT already exists, reuse it. A different gateway, multiple
   external addresses or disabled SNAT needs an explicit reviewed plan, not an
   automatic `router set` replacing configuration.
4. Record resulting network, subnet, router, `network:router_gateway` port UUID,
   gateway fixed IP/MAC, router tenant-interface UUIDs and source bindings. The
   router gateway IP is distinct from the upstream subnet gateway. No other
   external gateway/network/FIP is supported by this initial whole-cloud scope.
5. Optional ingress: separately approve one FIP on this external network and its
   association to the exact different tenant guest port/fixed IP. Reuse a matching
   association; refuse conflicting FIP associations and all port-forwarding rules.

Only after values are supplied can a concrete OpenStack create/set command batch
be reviewed. No placeholder-filled cloud mutation command is ready to execute.
Preserve operator resources; none are validation-owned or automatic-cleanup targets.

## Batch D — controlled endpoint/service, separate operator action

Blocked until the placement, address, route and dedicated test port are approved.
The egress endpoint must live **beyond** the nested OpenStack external uplink, not
on a controller management IP or tenant namespace. Copy the reviewed
`workloads/ns-probe/endpoint.py` to that approved host through its authorized
management process. It uses the standard library; no package installation is needed.

On the endpoint host only, after assigning shell variables from the approved
allocation, this is the actual interface (not a ready allocation):

```bash
: "${NS_SERVICE_IP:?approved local service IPv4 address required}"
: "${NS_SERVICE_PORT:?approved dedicated HTTP port required}"
: "${NS_ENDPOINT_ID:?reviewed nonsecret identity required}"
: "${NS_ENDPOINT_SOURCE:?absolute path to reviewed endpoint.py required}"
python3 "$NS_ENDPOINT_SOURCE" --bind "$NS_SERVICE_IP" --port "$NS_SERVICE_PORT" \
  --identity "$NS_ENDPOINT_ID"
```

Run in a supervised foreground/operator-managed service for preparation, recording
its PID/service, bind address/port, source revision and identity. Do not claim a
requested start is a successful bind: inspect `ss -lntp` and request HTTP locally
first, then from the actual tenant guest (E). GET `/probe?nonce=<fresh-value>` must
return HTTP 200 JSON `endpoint_id`, the same `nonce`, and connection `peer` IP.
The tenant request's peer must equal the router gateway port's external fixed IP,
not the tenant IP or the controller management address. This proves ordinary SNAT
only when the endpoint is trusted and no additional NAT obscures that peer.

For ingress, place another dedicated instance of this service on the selected
tenant guest's fixed IP and approved free port, **without** restarting EW services
or modifying networking. Use a distinct guest from egress and an independently
managed external observer beyond the uplink. Record its DMI UUID/boot, locally
assigned source IP and verified SSH trust. The observer requests the exact FIP;
the returned peer must be that source IP. If nested forwarding or another NAT
prevents that contract, leave ingress disabled and investigate; do not fake it.

Review existing effective SG coverage first. For ingress, permit only IPv4 TCP
to the dedicated guest service port from the approved observer `/32` (or separately
approved exact source CIDR); ensure guest firewall and external firewall permit
the same traffic. For egress, permit IPv4 TCP to the external service IP/port and
its return traffic; keep existing stateful egress rules if already sufficient.
Do not add duplicate narrower rules when unrestricted protocol rules already
cover the required path. Tenant-only `ew-sg` ingress does not automatically permit
external-source ingress. ICMP is optional diagnostic traffic, not HTTP acceptance.
No blanket Internet ingress, FIP port forwarding or provider-attached VMs.

Leave `session_port: null` in both directions. The optional persistent TCP echo
experiment belongs to later calibration unless a separate approved service already
exists. Never use EW API 8080, DB 5432, RabbitMQ 5672 or mentor 18080/18081 for these
listeners. In particular, provisioning/preparation must not activate 18081.

## Batch E — complete private inputs and finite OVS verification

Copy `examples/ns-inputs.yml` to an owner-only location outside the repository,
for example `/root/ew-private/ns-inputs.yml` (directory 0700, file 0600). Populate
from fresh A–D evidence, not names or examples: exact UUIDs, router gateway port,
external segment/addressing/MTU, both gateway identities and attestations, guest
port/IP/MAC/placement, and controlled endpoint identity/port. Keep `ingress: null`
unless its entire path has passed preparation; required unresolved values remain
`REQUIRED`/`null` and **block execution**. Cadence/lifetime defaults in the template
are initial settings, not calibration results. Keep private transport/key paths
in the private access mapping, preserving management trust separately from guest
trust. Do not edit old guest known-hosts entries or provisioning identities.

With complete inputs, activate the same venv and source the verified OpenRC from
Batch A's `context.json`. Set `NS_EVIDENCE` to that directory and `NS_INPUTS` to
the actual private input path. Run the implemented inspection (no observer start):

```bash
set -euo pipefail
umask 077
: "${NS_EVIDENCE:?Batch A directory required}"
: "${NS_INPUTS:?complete private N-S input file required}"
test -r "$NS_INPUTS"
ansible-playbook -i "$NS_INVENTORY" ns-inspect.yml \
  -e @"$NS_ACCESS" -e @"$NS_INPUTS" \
  -e @"$NS_EVIDENCE/management-trust-vars.json"
```

The extra management vars reuse the reviewed strict Ansible trust construction,
including imported control plays; transport paths alone do not configure Ansible
SSH. If inventory/trust/options change since A, recollect A. Record the displayed
new inspection run directory as `NS_INSPECTION_RUN`. It contains `runtime.json`,
`ns-config.json`, `source-globals-ns.json`, `ns-before.json`,
`ns-host-inspection.json` and `ns-controller-source-<host>.json`. It starts no
migration observers and is **not a migration run to resume**. Keep it as preparation
evidence; do not copy its files into a future migration run to satisfy gates.

The following finite verification uses the existing verified source namespace SSH
transport. Run only after D and inspection pass. It installs nothing, starts no
continuous probe and creates no anchors; HTTP requests are preparation evidence,
not calibrated downtime. It checks exact guest identity/MAC/current tenant MTU
and boot continuity around each request and records the actual peer response.

```bash
set -euo pipefail
umask 077
: "${NS_INSPECTION_RUN:?successful ns-inspect run directory required}"
export NS_INSPECTION_RUN NS_EVIDENCE
python3 - <<'PY'
import json, os, pathlib, shlex, sys, openstack
sys.path.insert(0, str(pathlib.Path('scripts').resolve()))
from ns_workflow import Workflow
from ew_transport import verify_profile
w=Workflow(os.environ['NS_INSPECTION_RUN'], openstack.connect(api_timeout=10))
w.api_snapshot()  # require current source identities still match inspection
code='''import http.client,json,secrets,sys,urllib.parse
c=json.loads(sys.argv[1]); p=c['probe']; nonce=secrets.token_hex(16)
h=http.client.HTTPConnection(p['address'],p['port'],timeout=c['timeout'],source_address=(c['source_ip'],0))
path=p['path']+('&' if '?' in p['path'] else '?')+urllib.parse.urlencode({'nonce':nonce})
h.request('GET',path,headers={'Connection':'close'}); r=h.getresponse(); body=json.loads(r.read(16385)); h.close()
ok=r.status==200 and body.get('endpoint_id')==p['endpoint_id'] and body.get('nonce')==nonce and body.get('peer')==c['expected_peer']
print(json.dumps(dict(success=ok,status=r.status,body=body)))
'''
def guest_profile(d):
    vm=w.guest(d); access=w.transport.access(vm,'source')
    profile=w.transport.profile(vm,access)
    verify_profile(profile,vm,mtu=w.cloud.network.get_network(vm['network']).mtu,mac=vm['mac'])
    return vm,access,profile
out=pathlib.Path(os.environ['NS_EVIDENCE'])
for direction in ('egress','ingress'):
    if not w.cfg.get(direction): continue
    vm,access,before=guest_profile(direction)
    observer_before=w.profile('ingress','source')[0] if direction=='ingress' else None
    request=dict(probe=w.cfg[direction]['probe'],timeout=w.cfg['timeout'],
        source_ip=vm['ip'] if direction=='egress' else w.cfg['ingress']['observer']['ip'],
        expected_peer=w.before['gateway']['fixed_ips'][0]['ip_address'] if direction=='egress' else w.cfg['ingress']['observer']['ip'])
    argv=['python3','-c',code,json.dumps(request)]
    result=json.loads(w.transport.run(w.transport.guest_argv(vm,access)+[shlex.join(argv)])
                      if direction=='egress' else w.external(argv))
    after=w.transport.profile(vm,access)
    verify_profile(after,vm,before['boot'],w.cloud.network.get_network(vm['network']).mtu,vm['mac'])
    observer_after=w.profile('ingress','source')[0] if direction=='ingress' else None
    if observer_before and observer_before['boot']!=observer_after['boot']: raise RuntimeError('Observer boot changed')
    with (out/(direction+'-source-http.json')).open('x') as f:
        json.dump(dict(direction=direction,request=request,response=result,guest_before=before,
                       guest_after=after,observer_before=observer_before,observer_after=observer_after),f,indent=2)
    if result.get('success') is not True: raise RuntimeError('Controlled endpoint identity/nonce/peer mismatch; response retained')
w.api_snapshot()
print('Finite source HTTP/SNAT checks passed; this is not a migration measurement')
PY
```

Review qrouter gateway/interface addresses and routes on the **actual hosting
network node** against `ns-before.json`. Retain before/after NAT counters or a
short, separately authorized capture of this exact flow on the router qg/uplink
and external endpoint where available. External HTTP `peer` plus the selected
tenant guest identity/source binding and gateway port address are required SNAT
evidence; a deployment-host HTTP request is insufficient. Repeat against both
approved physical paths only through an explicitly reviewed plan, not router
rescheduling or an HA test in this Work Item.

### Recheck EW without provisioning, baseline or application restart

`ew-provision.py plan()` intentionally refuses an EW router external gateway.
Therefore **do not run `ew-provision.yml verify` after adding that gateway** and
do not loosen its Work Item 1 guard. Use existing application `probe.py` through
the source transport as below. `resolve_ew` supports the exact inspected opt-in
N-S router and writes only the inspection directory's EW resource evidence.
This check submits three bounded smoke tasks (one per original client) and reads
the original task receipts; it is not a baseline. No deployments/services are run.

```bash
set -euo pipefail
umask 077
python3 - <<'PY'
import json, os, pathlib, shlex, sys, openstack
sys.path.insert(0,str(pathlib.Path('scripts').resolve()))
from ew_transport import Transport, verify_profile
from workload_resources import resolve_ew
root=pathlib.Path(os.environ['NS_INSPECTION_RUN']); out=pathlib.Path(os.environ['NS_EVIDENCE'])
cfg=json.loads((root/'ns-config.json').read_text())
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar
values=yaml.safe_load(pathlib.Path('group_vars/all.yml').read_text())
values.update(yaml.safe_load(pathlib.Path(os.environ['NS_ACCESS']).read_text()) or {})
topology=Templar(loader=DataLoader(),variables=values).template(values['ew_workload_config'],fail_on_undefined=True)
cloud=openstack.connect(api_timeout=10); catalog=resolve_ew(cloud,topology,root)
transport=Transport(cfg['transport'],catalog,cloud=cloud)
prior=json.loads((out/'ew-retained-state.json').read_text())
if prior['ownership']!='persistent-ew-never-validation-owned': raise RuntimeError('Wrong EW state ownership')
profiles={}; tasks={}
for name,vm in catalog['servers'].items():
    previous=prior['guests'][name]
    if any(previous[k]!=vm[k] for k in ('server','port','ip')): raise RuntimeError('EW identity changed: '+name)
    access=transport.access(vm,'source'); profile=transport.profile(vm,access)
    verify_profile(profile,vm,previous['boot'],cloud.network.get_network(vm['network']).mtu,cloud.network.get_port(vm['port']).mac_address)
    profiles[name]=dict(vm=vm,profile=profile)
for name in ('ew-client-a1','ew-client-a2','ew-client-b'):
    vm=catalog['servers'][name]; access=transport.access(vm,'source')
    api=topology['endpoints']['api']; url=f"http://{api['host']}:{api['port']}"
    receipt=prior['preserved_tasks'][name]
    read='import json,sys; from urllib.request import build_opener,ProxyHandler; print(json.dumps(json.load(build_opener(ProxyHandler({})).open(sys.argv[1],timeout=10))["job"]))'
    def check_receipt():
        job=json.loads(transport.run(transport.guest_argv(vm,access)+[shlex.join(['python3','-c',read,url+'/jobs/'+receipt['task_id']])]))
        if {k:job[k] for k in receipt}!=receipt: raise RuntimeError('Original EW receipt changed: '+name)
    check_receipt()
    output=transport.run(transport.guest_argv(vm,access)+[shlex.join(['python3','-','--url',url,'--client-id',name])],
                         data=pathlib.Path('workloads/ew-workload-app/probe.py').read_text(),timeout=90)
    if 'WORKLOAD_E2E_OK' not in output.splitlines(): raise RuntimeError('EW task completion failed: '+name)
    check_receipt(); tasks[name]=output
for name,entry in profiles.items():
    vm=entry['vm']; verify_profile(transport.profile(vm,transport.access(vm,'source')),vm,
       entry['profile']['boot'],cloud.network.get_network(vm['network']).mtu,cloud.network.get_port(vm['port']).mac_address)
with (out/'ew-source-health.json').open('x') as f: json.dump(dict(guests=profiles,tasks=tasks,status='PASS'),f,indent=2)
print('Six EW identities/boots and original receipts preserved; three real tasks completed')
PY
```

If a generation uses an additional private topology override, include it in the
selected `NS_ACCESS` mapping or resolve it explicitly before running this check.
Missing original state/receipts is a blocker; never fabricate them or adopt new
UUIDs/boots. On failure preserve partial evidence and original state unchanged.

## Completion and handoff

Work Item 4 is lab-complete only after reviewed code is deployed, external
allocation/permissions and each uplink/segment/MTU are confirmed, source mappings
and selected router gateway/SNAT agree, finite egress HTTP proves SNAT, optional
FIP ingress passes if enabled, exact private inputs pass source inspection, and
all six EW identities/boots/application tasks remain healthy. Retain raw responses,
source identity/config/namespace evidence, upstream confirmations and a manifest
of newly created versus reused operator resources, with timestamps and commit.
None are migration success/outage evidence. No automatic cleanup, rollback or
reset follows a preparation failure.

Still required from the operator: the full external allocation record above,
actual gateway uplink role/parent identities, whether existing bridge mappings
already agree, chosen egress guest/router, endpoint placement/IP/free port and
return route, plus optional ingress FIP/service/observer identities and source
permissions. Controller access IP `117.1.28.69` supplies none of those values.
Reset and empty-cloud rebuild acceptance also remain unverified, independently.

The next action is Batch A only. Return reviewed nonsecret discovery evidence
and the allocation owner’s facts before producing executable B/C mutations.
After Work Item 4 acceptance, authorize Work Item 5 calibration separately; leave
Work Item 6 migration, OVN takeover gates and all Pair A/B/C/EW/TCP measurement
anchors to their existing later workflow.
