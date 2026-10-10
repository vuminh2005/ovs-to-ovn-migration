# Work Item 3 (Việc 3): opt-in North–South migration and measurement

Implemented for review on top of `8d7649742d75885f93b17e4207facef606105a4d`.
Offline checks do not establish lab support. No host/cloud operation was executed
while implementing this Work Item. The current lab does **not** need resetting to
proceed. Work Items 1/2 still await empty-cloud/reset acceptance; reset is a later,
separately authorized recovery/repeat tool, not a Work Item 3 prerequisite.
The six-Work-Item project plan remains provisioning → reset integration → N–S
migration/measurement → N–S lab configuration → calibration → migration/report.

## Scope and inputs

`ns_enabled: false` preserves the existing tenant-only scope. Historical runtime
files without `ns_enabled` remain tenant-only, with N–S **NOT TESTED**. An enabled
run requires all input/evidence; missing prerequisites fail before staging/freeze.
There is no automatic provisioning of external resources, security rules, routes,
bridges, uplinks, probe endpoints, or floating IPs. No guest packages are installed.

The first implementation deliberately bounds the topology to **one** existing
IPv4 flat/VLAN external network, one IPv4 external subnet and one centralized
router external gateway with SNAT enabled. All other networks remain VXLAN
source/Geneve target. Optional ingress uses **one** exact existing FIP association.
The SNAT guest and FIP guest must have different ports: a guest with a FIP uses its
FIP for outbound NAT and would not prove the router's ordinary SNAT path.
DVR, Neutron L3 HA, multiple external gateways/subnets, FIP port forwarding,
provider-attached instances, unreviewed network types, br-migration, SR-IOV and
HA/failover experiments remain outside this Work Item. Two eligible gateway
chassis demonstrate assignment/binding, **not** failover or seamless sessions.
Whole-cloud checkpointing remains deferred and retains its tenant-only guards.
Reset/EW provisioning do not prepare this external topology.

Start with [examples/ns-inputs.yml](examples/ns-inputs.yml), copying it outside the
repository into a private reviewed input file. Every `REQUIRED`/`null` required
value must be replaced from operator evidence. No CIDR, gateway, pool, VLAN,
physnet, source address or workload endpoint is derived from `117.1.28.69`.
Management IP and `neutron-ext` existing in globals prove no upstream path.

The schema requires exact network/subnet/router/gateway-port UUIDs, segment
properties, external CIDR/gateway/pool/MTU, exact gateway inventory and observed
chassis hostnames, external bridge/uplink/MAC/MTU, and explicit attestations for
upstream routes and nested MAC/IP forwarding. The latter require operator
verification: software discovery cannot establish hypervisor/provider permissions.
Gateway hosts must equal the inventory's network hosts and
`ovn-controller-network`; computes remain provider-disabled. Kolla 18.8's
single-bridge `setup-ovs.yml` generates `physnet1`; other/custom mapping schemes
are refused instead of silently overwritten. The source globals must explicitly
contain the reviewed `neutron_bridge_name` and `neutron_external_interface`.
All gateway hosts must use those same Kolla names. VLAN uplinks must already
exist as the exact reviewed system interfaces. Their host addressing must be
empty; management-bearing uplinks are refused.

Each selected guest needs exact server/port/network/subnet UUID, fixed IP, MAC and
compute placement. Guests stay ACTIVE, with unchanged boots, normal bound ports,
and usable default routes. Select persistent existing workloads, including EW
guests if appropriate. They are never remediated/rebooted by N–S hooks. The
existing strict namespace SSH mechanism verifies DMI server identity, boot,
IP/MAC and MTU. It changes neither management nor guest known-hosts files.
`ns_transport` defaults to the existing EW key/trust path interfaces but does not
require EW measurement to be enabled. Supply those private paths explicitly.
Ingress additionally needs the external observer's exact DMI product UUID, boot,
locally assigned source IP, strict SSH key/trust and management address.

## Controlled endpoints and observations

A pre-existing controlled HTTP endpoint returns JSON with `endpoint_id`, the
request's `nonce` query argument, and the actual connection `peer` IP. The
observer validates all three and HTTP 200, opening a **fresh TCP connection for
every request**. Outbound observations require the peer to be the checkpointed
router gateway address, proving ordinary SNAT. Ingress addresses the exact FIP
and validates the intended endpoint identity and external observer source IP.
Endpoints must not be arbitrary public Internet sites or package repositories.

`workloads/ns-probe/endpoint.py` is an optional standard-library reference
endpoint for separately authorized **Work Item 4** preparation. It can run as an
additional dedicated test service; migration never starts it or restarts EW
applications. Example command to prepare *after separately reviewing actual
addresses, guest access and security rules*:

```bash
python3 endpoint.py --bind "$VERIFIED_SERVICE_IP" --port "$PERMITTED_TEST_PORT" \
  --identity "$REVIEWED_ENDPOINT_ID"
# Optional dedicated persistent echo service: --echo-port "$PERMITTED_ECHO_PORT"
```

No test port defaults overlap the mentor's TCP 18080/18081 experiment. Choose and
permit N–S HTTP/optional echo ports before migration. The existing 18081 listener
still activates only after Neutron API freeze. No N–S hook changes SG rules.

An optional `session_port` measures a separate long-lived TCP echo connection,
validating exact run/sequence payloads. It records opens, validated echoes and
observed connection breaks/reconnections separately from HTTP requests. A failed
connection establishment is not counted as an established session reset. Recovery
may use a new connection; conntrack/session preservation is **not promised**.
ICMP is not an acceptance requirement; operator ICMP diagnostics may be filtered.

## Order, MTU and cleanup

1. Phase 02 validates configuration and API scope, snapshots exact operator-owned
   resources, reads actual source bridge/uplink/namespace/OVS evidence, and checks
   every controller's source flat/VLAN type/ranges and physical MTU configuration.
2. Phase 04 starts independent finite observers and anchors only after fresh
   validated successes on both required directions. This precedes OVN staging,
   MTU reduction and any Pair-B remediation. Guest/observer identity, config/helper
   digest, run, process PID/start ticks and original boots are checkpointed.
3. Phase 06 retains the existing computed tenant MTU preparation. `mtu_plan`
   already updates VXLAN networks only; flat/VLAN external MTUs never receive an
   overlay overhead reduction. Phase 07 rechecks both endpoint guests against
   journaled target MTUs, boots and identities, fresh source attachments, current
   Kolla bridge/uplink settings, every controller's generated external support,
   and running observations **before** freeze. Existing final MTU/Pair-B/EW gates
   remain required, including separately authorized EW guest preparation.
4. After agents stop in Phase 08, freshly verified namespace ports (exact Neutron
   iface-id/MAC and OVS UUID/bridge/type) and reciprocal legacy OVS-agent patch
   pairs are detached **before** OVN activation. Leaving legacy qg interfaces
   attached can keep old gateway MAC/IP paths active. Namespace identities use
   kernel inode/device evidence; foreign/reused artifacts block mutation. Only
   the observed `int-<bridge>`/`phy-<bridge>` legacy pair is eligible, never arbitrary
   patch ports, physical uplinks, management paths or OVN localnet ports.
5. The post-deploy gate validates exact logical router/interface/gateway MAC/IP,
   default route, SNAT CIDRs/address, FIP UUID/port/fixed-IP/NAT, external logical
   switch/localnet physnet/VLAN, all intended eligible gateway chassis, bound
   `cr-lrp-*` chassisredirect and its active gateway's real reciprocal OVN localnet
   patch path. Localnet is not subjected to VM-style `up=true`/chassis checks.
   Caracal default routes may omit `output_port` without BFD; their external-gateway
   and subnet external IDs are still required.
   Every convergence attempt recollects both host/OVS and OVN DB evidence under
   one fixed deadline, including bounded host collection time. Missing target
   objects/bindings/localnet paths and collection timeouts can be retried;
   conflicting/duplicate identity, uplink, mapping, NAT or patch state, SSH
   trust/authentication errors and unclassified collection errors fail closed.
   `ns-takeover-attempt-*.json` records the paired evidence/result; its referenced
   `ns-host-attempt-*.json` also retains partial/rejected host reads. Attempt numbers
   continue across invocations. The takeover result references the successful
   attempt, and an older PASS never substitutes for fresh verification.
6. Phase 09 removes only checkpointed legacy namespaces after fresh takeover
   verification. Uplinks/br-ex, OVN and unrelated namespaces/ports remain. Existing
   tunnel cleanup and cleanup-before-restore order remain unchanged.
7. Phase 12, after ordered restoration markers, revalidates API identities,
   Geneve/MTU/boots and fresh OVN gateway state, records immutable per-observer
   sequence recovery fences, and waits for consecutive fresh validated successes.
   Application task reconciliation cannot supply or invalidate N–S observations.
8. Phase 13 snapshots append-only raw observer evidence, then builds the report.
   Only complete required acceptance and persisted successful reports authorize
   exact observer stop requests. Failure preserves raw files and observers until
   their finite lifetime. Operator resources are never deleted; raw evidence is
   retained on observer and controller even after success. Final stop diagnostics
   appear separately in `north_south_finalization`/failure evidence.

## Coverage, reporting and interruption

`north_south` is independent of `dataplane_probe` (Pair-A PCAP), EW application
and mentor TCP results. Required N–S failure/incomplete evidence yields
`MIGRATED_VALIDATION_INCOMPLETE`; existing SUCCESS/SUCCESS_WITH_REMEDIATION semantics
remain. Disabled/historical N–S is NOT TESTED. No missing N–S evidence becomes PASS.

Per-direction JSON includes path identity, cadence/timeouts, attempts/successes/
failures, observed failure percentage (not packet loss), sampled outage windows,
longest recovered outage, latency min/mean/p95/max, optional session observations,
start/end anchors, recovery fences, coverage and unavailable reasons. Text includes
this separate N–S object. Pair-A packet loss/outage remain unchanged.

All durations come from one observer's monotonic clock. Windows select
`start.seq < observation.seq <= end.seq`; controller time orders controller
restoration markers only. Outage is sampled: first failing request start to first
following validated response, with cadence/timeout uncertainty, not exact packet
downtime. Initial/recovered samples do not erase earlier failures. Open-ended
failure windows remain in observed evidence with null recovery/duration.
Unrecovered/no-fence/missing-end windows yield UNAVAILABLE, never zero outage.
A valid failure-free window can report zero. Coverage includes the transition
from the exact start-anchor record to the first selected observation. Missing
observations, boot/identity changes, invalid time or unexplained gaps within that
interval invalidate coverage. Timing gaps wholly before the start or after the
immutable end appear in `outside_window_anomalies`; they cannot change completed
counts, outages, latency or acceptance. Raw evidence remains intact. Ambiguous
sequence ordering, invalid anchors or changed observer configuration/boot still
fail closed; outside record anomalies are reported separately.

Per-direction diagnostic extraction precedes restoration/takeover/recovery
acceptance. Trustworthy `observed_samples` and `sampled_outage_windows` remain
visible when recovery is PENDING/failed, the end anchor is missing, or target
acceptance evidence is incomplete. One missing/corrupt direction does not erase
another direction's observations. `diagnostics_only: true` distinguishes these
observations from accepted authoritative metrics, which remain null/UNAVAILABLE.
When target acceptance gates fail, `measurement_status` retains the window's
independent evaluation and `acceptance_reason` explains the refusal. Open windows
retain null recovery/duration. Complete required gates and every configured
direction must pass before overall PASS or any owned-resource finalization.

Durable controller/remote start intents precede launch. The exact live process is
reused; unknown/dead/expired/interrupted starts are never silently relaunched.
An unchanged helper/config digest and exact boot/process identity are required.
Start/end anchors and recovery fences survive retries. Late migration resume
recovers `ns_enabled` from runtime and rechecks live gateway state before cleanup;
it neither creates probes nor resets anchors. Config changes are refused.
An explicit resume override cannot change the recorded N–S mode or schema and
therefore cannot select the broad tenant-only cleanup path for an external run.
Partial legacy detach can continue only for missing original artifacts or exact remaining
UUIDs; replacements/ambiguous namespaces stop. No rollback/reset is triggered.
The append-only measurement is API/controller-independent after launch, so API
freeze or collection failure cannot stop guest/external observations. Source
namespace transport may disappear during takeover; restored OVN metadata namespace
transport is rediscovered only afterward. Collection requires restored management
access; local raw evidence remains if that access is unavailable.

## Staged controller checklist — prepared, not executed

For executable source discovery and the separate allocation/configuration/traffic
checks, use [Work Item 4 preparation](docs-north-south-preparation.md). In particular,
`ns-inspect.yml` checks configured source resources and mappings; it neither proves
HTTP/SNAT reachability nor starts observers. OVS preparation requires no OVN takeover
evidence. Keep unresolved inputs unresolved until the operator supplies evidence.

1. **Information:** confirm uplink assignment, physical/VLAN mode/tag, physnet,
   external subnet/gateway/pool/MTU, real upstream routes, nested MAC/IP forwarding,
   exact existing router/gateway/FIP/guest ports, all gateway hostnames/MACs and
   controlled endpoint/observer identities. Preserve current evidence/private
   mappings. Do not reset the lab to obtain these facts.
2. **Separately authorized read-only source checks:** inspect `openstack network
   show`, `subnet show`, `router show`, `port show`, `floating ip show`, source ML2
   and OVS mappings, actual bridge/uplink/VLAN/address/MTU and upstream path. Then,
   once complete private inputs exist, run the implemented inspection entrypoint:

   ```bash
   source /root/venvs/kolla-2024.1/bin/activate
   cd /root/ovs-to-ovn-migration
   ansible-playbook -i /root/multinode ns-inspect.yml \
     -e @/root/ew-access.yml -e @/root/ew-private/ns-inputs.yml
   ```

   This reads host/cloud state and writes private controller inspection evidence;
   it starts no probes and changes no cloud/guest/network resources. Input paths
   are examples; resolve the actual retained private files. Review missing explicit
   source globals and all failed checks; never synthesize upstream configuration.
3. **Separately authorized preparation/calibration (Work Items 4/5):** configure
   the reviewed external path/endpoints/permissions outside migration, prove both
   HTTP directions under OVS, and calibrate timeout/cadence/session behavior.
   Existing EW baseline remains independently invokable with `ew-baseline.yml`.
   N–S migration observers start automatically in Phase 04, not during provisioning.
4. **Only after separate migration authorization:** invoke existing migration:

   ```bash
   ansible-playbook -i /root/multinode migrate-to-ovn.yml \
     -e @/root/ew-access.yml -e @/root/ew-private/ns-inputs.yml
   ```

   Include the existing EW enable/private inputs when EW measurement is intended.
   The new opt-in does not waive any existing MTU/identity/application gate.
5. **Post-cutover:** review `ns-before/after.json`, `ns-host-*`, `ns-controller-*`,
   `ns-ovn-raw.json`, `ns-ovn-takeover.json`, `ns-takeover-attempt-*`,
   `ns-host-attempt-*`, `ns-guest-baselines.json`,
   `ns-lifecycle.json`, `ns-post-identities.json`, `ns-recovery.json`, direction raw
   files and independent report results. Verify source/target NAT and real traffic,
   gateway binding/localnet paths, open failures, session resets and boot continuity.
6. **Recovery only if subsequently needed:** preserve evidence; use existing
   late-phase resume only when its takeover/live-state guards permit it. Reset
   requires its separate reviewed generation/preflight/destructive authorization.
   Neither N–S failure nor this checklist authorizes reset or automatic rollback.

## Audit findings and remaining lab checks

| Old assumption/location | Implemented change | Offline evidence | Pending lab check |
| --- | --- | --- | --- |
| `02-precheck`: all networks VXLAN, no gateway/FIP | Default guards retained; exact opt-in scope/SDK snapshots/controller/host checks | `ScopeTests`, controller syntax/parsing | Real external segment, upstream and permissions |
| Kolla target mapping implicit | Explicit single-bridge Kolla mapping, network-only gateway group, fresh pre-freeze settings | Kolla scope and ordering tests; pinned upstream source inspection | Generated config and OVS mappings on both gateways |
| `network_semantics`, MTU preparation | Overlay conversion vs unchanged external flat/VLAN/MTU | SDK round-trip and separate external MTU tests | Actual existing external segment remains unchanged |
| `08-cutover`: chassis/VM bindings sufficient | LR ports/default route/NAT/localnet, gateway assignment/redirect and physical OVN patch checks | `OVNTests` binding/NAT/path failures | Scheduler and selected gateway convergence timing |
| `09-cleanup`: all qrouter/qdhcp and fixed patch names | Fresh exact legacy port/namespace evidence; detach before OVN, namespace deletion after takeover | `CleanupTests`: foreign/OVN/uplink retention, inode/UUID reuse, idempotency | Actual source qg iface-id and old agent patch metadata |
| `validation-snapshot`, backups: coarse resources | Separate detailed N–S snapshot; existing full backups/count preservation unchanged | Exact gateway/FIP/identity comparisons | Source backup usability and operator-owned associations |
| `provider_associations.py` | KEEP: L3 service provider compatibility, not physnet mapping | Existing provider association regressions unchanged | Nonempty provider conversion remains lab-dependent |
| Measurement had no independent N–S observer | Finite guest/external runners, strict trust, immutable lifecycle/fences/raw evidence | `MetricTests`, `LifecycleTests`, `IntegrationTests` | API/control outage survival, SSH rediscovery, calibrated sampling |
| Resume/report/cleanup could omit N–S | Runtime flag, live resume takeover check, independent acceptance and owned stop | Historical/disabled/partial/source-only/failure tests | Operator-run interrupted late resume and finite expiry |
| Reset/provision/checkpoint external scope | DEFER: separate existing interfaces preserved; no automatic external preparation/reset | Full existing offline regression suite | Work Items 4/5 and reset/fresh-provisioning acceptance |

Upstream references used: [Caracal DB migration](https://github.com/openstack/neutron/blob/24.0.0/neutron/plugins/ml2/drivers/ovn/db_migration.py),
[Caracal gateway/NAT construction](https://github.com/openstack/neutron/blob/24.0.0/neutron/plugins/ml2/drivers/ovn/mech_driver/ovsdb/ovn_client.py),
[Kolla 18.8 OVN setup](https://github.com/openstack/kolla-ansible/blob/18.8.0/ansible/roles/ovn-controller/tasks/setup-ovs.yml).
The installed lab image/revision, OVS/OVN schemas and actual source artifacts still
need operator verification. No fresh provisioning, external migration, HA/failover,
reset or real-lab measurement success is claimed by this implementation.
