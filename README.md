# Kolla-Ansible ML2/OVS -> ML2/OVN migration automation v2

V2 turns the lab-validated migration into a **single-invocation migration proof of concept** for its supported scope. It discovers the Kolla inventory path from the `-i` argument, finds `kolla-ansible` from the active execution environment, parses `/etc/kolla/globals.yml`, uses standard Kolla inventory groups to find control/network/compute/OVN hosts, and reads the generated ML2/OVN config to discover the real OVN NB/SB connection strings.

## Run

From the Kolla deployment host, with `ansible-playbook` and `kolla-ansible` available in the current environment:

```bash
ansible-playbook \
  -i /root/multinode \
  migrate-to-ovn.yml
```

Image and flavor preparation is automatic. The deployment host needs outbound HTTPS
to `cloud-images.ubuntu.com` only when the managed Ubuntu image is absent.
Existing image/flavor overrides remain optional; no manual upload or flavor creation is required.

If non-standard Kolla file locations are used, environment overrides are available without editing the repo:

```bash
KOLLA_GLOBALS=/custom/globals.yml \
KOLLA_OPENRC=/custom/admin-openrc.sh \
ansible-playbook -i /root/multinode migrate-to-ovn.yml
```

## Supported scope in v2

V2 intentionally auto-detects and **fails before downtime** if the cloud is outside the paths already validated in the lab:

- Kolla-Ansible 18.8.x
- OpenStack 2024.1 (Caracal)
- Ubuntu 24.04 on control/network/compute migration hosts
- source ML2/OVS
- VXLAN tenant networks
- OVS native firewall driver
- centralized L3
- DVR off
- Neutron agent HA off
- provider networks off
- no external/provider network
- no Floating IPs
- no router external gateway
- target ML2/OVN + Geneve
- native OVN DHCP
- OVN metadata agent

This is a deliberate safety property. V2 does **not** claim that FIP/provider/DVR/HA migration is supported merely because the generic OVN components can run.

## What is now auto-discovered

V1 required several environment values. V2 derives them automatically:

- Kolla inventory: from Ansible `-i`
- Kolla CLI binary: `command -v kolla-ansible`
- source Kolla variables: parsed from `/etc/kolla/globals.yml`
- controller/network/compute placement: standard Kolla inventory groups
- OVN CLI host: `ovn-northd` group, falling back to `control[0]`
- DB sync host: first control node
- OVN NB/SB endpoints: parsed from generated `/etc/kolla/neutron-server/ml2_conf.ini`
- source VM Neutron port UUIDs: OpenStack API snapshot
- existing network MTUs: OpenStack API; each VXLAN network is reduced by the configured VXLAN->Geneve overhead delta
- Port_Binding timeout: calculated from existing compute-port count and capped safely

The deployment host no longer has to be the same machine as the OpenStack controller. Kolla orchestration runs where Ansible is invoked, while Docker/systemd/DB operations run on the appropriate inventory hosts.

## Orchestration files

`migrate-to-ovn.yml` imports these files in order:

1. `00-bootstrap.yml`
2. `01-backup.yml`
3. `02-precheck.yml`
4. `03-validation-prerequisites.yml`
5. `04-validation-workloads.yml`
6. `05-stage-ovn-db.yml`
7. `06-target-config.yml`
8. `07-migrate-db.yml`
9. `08-cutover.yml`
10. `09-cleanup.yml`
11. `10-restore-neutron.yml`
12. `11-validate.yml`
13. `12-workload-validation.yml`
14. `13-report.yml`

All live under `playbooks/`. `resume-bootstrap.yml` loads late-phase checkpoints;
`validation-snapshot-tasks.yml` remains an unnumbered included helper. Filenames
express import order, while historical phase labels and metric/checkpoint IDs
(`phase06.start`, `phase07.start`, etc.) retain their existing meanings.
The migration operations and their order are unchanged.

## Automatic validation prerequisites

`03-validation-prerequisites.yml` runs before workload creation and destructive
migration phases. It reuses an explicitly configured image by exact name or UUID
when available, otherwise reuses `ovn-validation-ubuntu-24.04`. If that managed
image is absent, it downloads the official Noble amd64 released cloud image,
verifies its SHA256 against the [official release SHA256SUMS](https://cloud-images.ubuntu.com/releases/noble/release/SHA256SUMS),
and uploads it as qcow2/bare with x86_64 architecture before waiting for ACTIVE.
Existing inactive images are awaited or fail clearly; duplicate names fail instead
of creating another image. A deployment-host lock serializes preparation calls
sharing the cache directory, and the managed name is rechecked before upload.
Coordinate preparation if using multiple deployment hosts concurrently.

The image URL is
`https://cloud-images.ubuntu.com/releases/noble/release/ubuntu-24.04-server-cloudimg-amd64.img`.
Verified downloads are cached in `/var/cache/ovs-to-ovn-validation`, outside run
directories. No guest internet access is needed. Existing ACTIVE images require
no Ubuntu download or checksum request. Preparation errors fail before migration.

`ovn-validation.small` is reused only if it has exactly 1 vCPU, 1024 MB RAM and
8 GB root disk; an incompatible managed flavor fails rather than being altered.
If absent, it is created. Optional `validation_image`/`validation_flavor` overrides
(or their existing environment variables) can select existing resources. A missing
explicit flavor fails clearly; clear that override to use automatic preparation.
The default credentials need Glance upload and Nova flavor-create permissions.

`validation-prerequisites.json` records resolved image/flavor UUIDs, names, and
preparation details. Workload configuration automatically consumes these UUIDs.
Managed image/flavor resources remain available after VM/network cleanup.
`validation_cleanup_prerequisites` defaults to false; true is rejected because
normal cleanup must not delete shared prerequisites. Remove them explicitly if
needed. Image names, URL, cache directory, download timeout and activation timeout
are configurable in `group_vars/all.yml`.

## Metrics

Every run creates a unique directory such as:

```text
/root/ovs-to-ovn-backup/20260930-161200/
```

It contains `migration-report.json` and `migration-report.txt` with:

- overall migration duration
- control-plane downtime: from neutron-server freeze until the first successful Neutron API request after restore
- `neutron-ovn-db-sync-util` migration duration
- dataplane convergence window: from stopping legacy agents until all pre-existing VM `Port_Binding` rows have a chassis and `up=true`
- duration of each migration phase
- resource counts before migration
- resource identity preservation for networks, subnets, routers, servers and compute ports
- result of a temporary post-migration Geneve network smoke test

### Guest packet loss and outage

Workload validation is enabled by default and automatically prepares reusable
Ubuntu image/flavor prerequisites.
Three explicit pairs have separate purposes:

| Pair | Guests | Purpose |
| --- | --- | --- |
| A | `measure0`, `measure1` | Continuous small-packet routed tenant dataplane measurement; no reboot or guest-network remediation |
| B | `existing0`, `existing1` | Pre-existing workload identity, DHCP, MTU, metadata and routed connectivity validation |
| C | `fresh0`, `fresh1` | Fresh OVN provisioning on new routed Geneve networks after migration |

Pairs A and B share two dedicated VXLAN networks and a centralized router,
with separate explicit ports per guest. Pair C uses two new networks and a new
router. Names include the run ID and role. No Floating IP, provider network,
SSH key or tenant SSH is used. The preservation snapshot includes all four
pre-migration guests.

Cloud-init installs a Python service in every guest. Pair A uses one persistent
`ping -n -D -O -s 56 -i <interval>` session per guest. The authoritative direction
is measure0 -> measure1; reverse traffic remains a recovery diagnostic. Pair B/C
retain their existing per-attempt small-packet health probes. Compact serial JSON
still carries boot, DHCP, MTU and metadata diagnostics, requiring the instance UUID
for metadata success. Guest probes require no package downloads.

Phase 04 starts a detached compute-local tcpdump on measure0's exact tap, resolved
from exactly one local OVSDB Interface with the full checkpointed `iface-id` and
`vm-uuid`, existing Linux interface and expected integration bridge (`br-int` in
this POC). Libvirt XML cross-checks positive UUID/tap evidence; absent XML port
annotations are acceptable, conflicting identities fail. No truncated UUID tap
guess is used. The exact tap is checkpointed before launch and reverified on resume.
Capture uses the existing inventory and become
access, host Python3/tcpdump and Kolla's `nova_libvirt` container. It writes
packet-buffered Ethernet PCAP under `validation_capture_directory/<run-id>/` (default
`/var/lib/ovn-migration-validation`), outside the controller backup tree. After launch
it needs no Nova console or Neutron API. The root supervisor records process identities,
heartbeat gaps, clean completion and tcpdump kernel-drop counters.

After both Pair-A guests are ACTIVE/bound, exact identities/boots are saved and
capture observes five continuous request/reply pairs, `validation-window.json`
receives its immutable `pcap_start`. This happens before Pair-B creation, OVN staging,
MTU changes, remediations or freeze/cutover. Phase 08 reuses it. Capture continues
through all post-cutover Pair-B/Pair-C validation and final OVN binding checks.
Only then a request-index recovery fence is saved; five stable recovered requests
newer than that fence establish `pcap_end`. The measured interval
is `(pcap_start.index, pcap_end.index]`, indexed by captured measure0 echo requests.
Metadata and stale guest MTU never gate the packet endpoint. Pair A is never rebooted
or remediated. Its boot/resource preservation is reported independently.

Each readiness invocation first snapshots per-VM sequence fences in
`pre-freshness-anchors.json` or `post-freshness-anchors.json`. Only higher packet
sequences and higher associated health sequences can pass; a guest with no
records initially must first establish a fence, then emit newer evidence. At
least five consecutive successful new attempts plus new DHCP/metadata health
are required. Guest/controller epoch offsets do not affect coverage or readiness;
wall-clock synchronization is no longer a correctness requirement.

Each measure0 echo request counts as an attempt; its corresponding echo reply
matches ICMP identifier, sequence and echoed payload. Sequence rollover is supported.
Every consecutive loss burst is recorded. `first_failure_timestamp` and
`recovery_timestamp` describe the first burst; the `longest_outage_*` fields describe
the longest recovered burst. Actual outage is the first following successful echo
reply's compute timestamp minus the first failed request's compute timestamp in
that burst. Zero loss yields zero outage.

Validity requires both endpoints in the raw PCAP, unchanged ICMP session/continuous
sequence progression, positive request cadence gaps smaller than three configured
intervals, capture lifetime spanning the window, no supervisor heartbeat gap over
three seconds, clean stop and zero kernel drops. Missing/truncated evidence, an
unexplained capture gap or unrecovered final loss produces UNAVAILABLE for both loss
percentage and outage; raw counts may remain for diagnostics. Guest console
truncation does not invalidate a complete PCAP. Neither controller/guest clock
comparisons nor PortBinding state select or synthesize packet observations.

Pair B must initially pass source VXLAN, source guest MTU, ACTIVE/bound identity,
DHCP, metadata, routed connectivity and short-T1 renewal checks. Phase 06 first
attempts automatic target MTU convergence without reboot. If it fails solely
because of stale guest MTU, optional controlled remediation is described below.
Both Pair-B guests must be healthy at the target MTU before phase 07 begins;
a fresh guard checks this immediately before downtime metrics and Neutron freeze.
After OVN restoration, Pair B must retain the authoritative pre-cutover boot and
server/port/IP baseline, use the target MTU and the actual OVN metadata route,
and pass DHCP, metadata and routed connectivity. Pair C is created only after
existing networks demonstrate Geneve semantics and must pass provisioning,
DHCP, target MTU, metadata, routed connectivity and Southbound binding checks.

Cleanup requires all independent pair checks, Pair-C Southbound bindings,
VXLAN -> Geneve semantics, valid Pair-A coverage/boot continuity, orchestration
and resource preservation. Complete success deletes Pair C first, then both
pre-existing pairs, then their shared topology. `post-cleanup.json` and
`pre-cleanup.json` journal exact UUIDs; the final report requires both cleanups.
Reusable image/flavor prerequisites remain retained.

FAIL/UNAVAILABLE preserves resources and evidence for debugging and writes
MIGRATED_VALIDATION_INCOMPLETE; no rollback is attempted. Cleanup-only resumes
reuse the complete validation evidence and journal rather than validating deleted
guests. All deletion operations use only IDs in `validation-resources.json`.

Guest probes end when successful resources are deleted, or after the configurable
four-hour lifetime on retained debugging VMs. Nova console polling still merges
compact diagnostic records from a bounded tail (`validation_console_tail_lines`),
but is never the authoritative packet-loss transport. Guest health still requires
fresh serial evidence. A failed validation retains the raw PCAP and all owned
resources. After clean capture stop the PCAP, supervisor state and tcpdump diagnostics
are fetched into the controller run directory. The original compute copy is deleted
only after successful final JSON/text report persistence and resource cleanup; the
controller copy remains. Failed/ambiguous captures are never silently replaced.

Configuration in `group_vars/all.yml`:

| Variable | Purpose |
| --- | --- |
| `validation_workloads_enabled` | Enable guest validation (default true) |
| `validation_image`, `validation_flavor` | Optional existing image/flavor UUID or exact name; empty selects managed defaults |
| `validation_name_prefix` | Unique resource names also include run ID and stage |
| `validation_pre_cidrs`, `validation_post_cidrs` | Two tenant CIDRs per topology (A/B share pre topology) |
| `validation_timeout_seconds` | Readiness/recovery/deletion timeout |
| `validation_probe_lifetime_seconds` | Guest service lifetime |
| `validation_console_poll_seconds` | Nova serial evidence collection interval |
| `validation_console_tail_lines` | Bounded console tail per API call; default 20,000 |
| `dataplane_probe_interval_seconds` | Guest ICMP interval |
| `validation_capture_directory` | Persistent compute evidence base; default `/var/lib/ovn-migration-validation` |
| `validation_allow_post_cutover_guest_reboot` | Independent explicit Pair-B post-cutover opt-in; default false |
| `validation_post_cutover_dhcp_timeout_seconds` | Automatic OVN DHCP convergence bound; default 180 |

Use an Ubuntu cloud image containing cloud-init, Python3, ping, iproute2,
systemd, DHCP lease files in `/run/systemd/netif/leases` or `/var/lib/dhcp`,
and writable ttyS0. Unsupported images fail before downtime with console
instructions. DHCP evidence proves a real lease and usable configuration; it
also checks that Pair B has received the target MTU and the actual OVN metadata
next-hop after migration; a stale source lease is insufficient for convergence.
Nova must expose serial
console output through the API, and the deployment Python environment must contain
`openstacksdk` and PyYAML. Credentials must permit the existing admin snapshots
and creating/deleting the dedicated resources. Supply non-overlapping lab CIDRs.

### Existing VXLAN segment semantics

Caracal's [migration utility](https://github.com/openstack/neutron/blob/24.0.0/neutron/cmd/ovn/neutron_ovn_db_sync_util.py)
calls [migrate_neutron_database_to_ovn](https://github.com/openstack/neutron/blob/24.0.0/neutron/plugins/ml2/drivers/ovn/db_migration.py).
It converts VXLAN network segments to Geneve while preserving the VNI, updates
allocations, and repairs binding details. The automation never issues manual SQL.
`existing-network-semantics.json` records every pre-existing network UUID,
before/after type and segmentation ID, logical-switch existence, and observed
Southbound encapsulation types. PASS requires VXLAN -> Geneve, the same VNI,
`neutron-<network UUID>` in NB and registered Geneve encapsulation. Existing
chassis-side tunnel checks remain in place.

### Caracal L3 provider associations

Neutron 24.2.x migrate mode does not update legacy L3
`ProviderResourceAssociation` rows. Phase 07 now snapshots these associations,
runs the authoritative `neutron-ovn-db-sync-util --ovn-neutron_sync_mode migrate`,
and applies the [newer upstream in-place conversion](https://github.com/openstack/neutron/blob/master/neutron/plugins/ml2/drivers/ovn/db_migration.py)
of `single_node`, `ha`, `dvr`, and `dvrha` to `ovn`. The helper uses Neutron's
model and writer session in the existing neutron-server image with its generated
Kolla configuration while API workers remain stopped. This is idempotent; it
does not add DVR or HA support to the POC.

The run directory contains `provider-associations.before.json`,
`provider-associations.after.json`, `provider-associations-compatibility.json`
(changed row count), `provider-associations-verification.json`, and
`db-sync-migrate.log` (stdout, stderr, and return code). A nonzero sync return code
or any remaining legacy association stops Phase 07 before cutover. The historical
`db_migration.start` marker remains unchanged; `db_migration.end` is recorded
after compatibility verification, before the existing OVN topology check.
Cleanup does not repair the database.

Local compatibility tests use SQLite and require pytest, PyYAML, and SQLAlchemy;
run the full suite with `python -m pytest -q` in a test virtual environment.

## Scale

VM/network/router/port UUIDs and counts are not hard-coded. The automation snapshots and loops over the real cloud state. Scale therefore changes **duration**, not migration logic. Port_Binding convergence timeout grows with the number of existing compute ports up to a safety cap.

## Important MTU boundary

V2 updates the Neutron MTU of existing VXLAN networks by subtracting the validated VXLAN-to-Geneve overhead delta. It cannot safely log into arbitrary guests and rewrite static interface configuration. DHCP-managed guests should obtain the advertised MTU according to their DHCP client behavior; statically configured guests remain an operator responsibility and should be handled before the cutover.

## Output used to judge success

A run is successful only when the automation reaches the final report after all guards pass. The key gates are:

- source cloud matches the supported scope
- DB migration exits successfully and OVN NB/SB topology exists
- every old VM port is bound to an OVN chassis with `up=true`
- legacy `br-tun` is gone and an OVN Geneve interface exists
- neutron-server comes back healthy on ML2/OVN
- pre-existing critical resource IDs are preserved
- a new temporary network is created as Geneve after migration

Guest DHCP, metadata and routed packet evidence is required for workload PASS. ACTIVE instances, allocated fixed IPs, running containers and PortBinding alone cannot establish it.

## v2.2 fix

All phase playbooks explicitly load `../group_vars/all.yml` through `vars_files`.
This avoids relying on Ansible implicit `group_vars` discovery for imported playbooks
stored under the `playbooks/` subdirectory.


## v2.2 fix

MariaDB backup capability is validated by executing `kolla-ansible mariadb_backup`; the precheck no longer requires an explicit `enable_mariabackup` key in `globals.yml`, avoiding false negatives when the backup command is available and succeeds.

## Resume after a late-phase failure

If a run has already completed OVN takeover/cleanup and fails only in a validation guard,
do **not** restart the migration from Phase 0. Resume from the existing run directory:

```bash
ansible-playbook \
  -i /root/multinode \
  resume-after-cleanup.yml \
  -e migration_resume_run_dir=/root/ovs-to-ovn-backup/<run-id>
```

`ovnmeta-*` namespaces are not required to exist on every network node. Their presence is
local-datapath/chassis dependent. The cleanup safety rule is instead: never delete
`ovnmeta-*`; remove only `qrouter-*` and `qdhcp-*`, then validate the OVN metadata agent
containers separately.

## Resume compatibility fix (v2.4)

`resume-after-cleanup.yml` accepts either `metrics/phase06.end` or `metrics/phase07.start` as checkpoint evidence. This supports interrupted older runs that already entered Phase 7 but do not contain the newer Phase 6 marker. Before resuming it verifies live `ovn-controller` containers and confirms legacy Neutron agents remain stopped.

## First workload integration test

Run from the deployment host with the Kolla environment activated:

```bash
ansible-playbook -i /root/multinode migrate-to-ovn.yml
```

This is a POC, with static verification only until tested on your Kolla lab.
There is no production rollback framework. Partial creation checkpoints are
saved after each API response; a process crash between resource creation and
checkpoint persistence can leave an orphan requiring manual inspection of this
run's unique names. Cleanup only uses checkpointed UUIDs. Disabled guest validation
is reported NOT TESTED and cannot produce an overall workload-validation SUCCESS.
Old checkpoints without segment/guest evidence may resume service recovery but
cannot retrospectively measure guest outage or report workload PASS.

## Workload report fields

The existing timing, phase, preservation and Geneve smoke fields remain.
`ovn_portbinding_convergence_seconds` also names the existing convergence metric
explicitly. New top-level results are:

- `existing_workload_post_migration_validation`
- `existing_workload_post_migration_connectivity`
- `existing_workload_dhcp_availability`
- `existing_workload_dhcp_convergence`
- `existing_workload_metadata`
- `existing_workload_identity_preservation`
- `existing_workload_cleanup`
- `new_ovn_workload_provisioning`
- `new_ovn_workload_dhcp`
- `new_ovn_workload_connectivity`
- `new_ovn_workload_metadata`
- `new_ovn_workload_bindings`
- `new_ovn_workload_cleanup`
- `existing_network_vxlan_geneve_semantics`

`dataplane_probe` contains `status`, `packets_attempted`, `packets_successful`,
`packets_failed`, `packet_loss_percent`, `failure_burst_count`, `first_failure_timestamp`,
`recovery_timestamp`, `maximum_consecutive_failed_probes`,
`longest_outage_start_timestamp`, `longest_outage_recovery_timestamp`, and
`actual_dataplane_outage_seconds`; available evidence also includes
`coverage_complete`, `evidence_source: compute-tap-pcap`, `start_anchor`, `end_anchor`,
`failure_bursts`, and `measurement`. An unavailable probe keeps numeric fields
null when no records exist; incomplete captures keep loss/outage null and may retain observed counts, which must
not be treated as complete packet-loss measurement. `initial_ovs_workload_validation` records the pre-downtime guest gate.
`workload_checks` contains independent initial and post-migration per-VM checks, `existing_network_semantics` contains the segment audit,
and `validation_orchestration` retains subprocess failure details.

On late retries, resource-preservation comparisons exclude additions only for
checkpointed UUIDs belonging to this run's fresh Pair-C topology. Original
resource deletions and unrelated additions still fail the existing guard.

## Owned validation DHCP preparation

Before Pair-B guests boot, their owned explicit ports receive
DHCP option 58 (T1, default 30 seconds) and option 59 (T2, default 60 seconds)
through Neutron `extra_dhcp_opts`. Caracal supports these options in both its
[dnsmasq agent](https://github.com/openstack/neutron/blob/24.0.0/neutron/agent/linux/dhcp.py)
and [OVN option mapping](https://github.com/openstack/neutron/blob/24.0.0/neutron/common/ovn/constants.py).
No database SQL, forced lease renewal or unrelated tenant DHCP configuration is used.
Guest reboot is limited to the opt-in, validation-owned Pair-B remediation below. Short T2 bounds broadcast rebinding when the old OVS DHCP
server disappears; T1 alone would keep renewing against that old server.

The root guest service passively observes guest DHCP renewal REQUESTs and their
matching ACKs with an Ethernet packet socket filtered to IPv4 DHCP. Initial preparation requires at least two guest renewal REQUEST/ACK exchanges
with sane effective T1/T2 values, including a renewal observed after the initial
preparation anchor, a usable lease, fresh packet success, working OVS metadata
and the same boot. Source VXLAN MTU (normally 1450) and the existing OVS metadata
route are valid in phase 04; target MTU and OVN metadata next-hop are not checked
at this stage. This proves the
owned guests are renewing rather than merely having Neutron-assigned addresses.
The interval between matched renewal ACKs must be positive and no greater than
configured T1 plus `validation_dhcp_renewal_tolerance_seconds` (default 5).
Effective ACK timers may be smaller than configured values: preparation accepts
`0 < observed T1 <= configured T1` and `observed T1 < observed T2 <= configured T2`.
The same checks apply to the phase-06 gate; exact timer equality is not required.
Guest health records also include read-only MTU diagnostics for the interface
holding the validation IP: `network_backend`, `configured_static_mtu`,
`dhcp_use_mtu`, and `mtu_configuration` (`static_mtu`, `dhcp_mtu_disabled`,
`dhcp_mtu_enabled`, or `unknown`). A selected networkd file and its drop-ins
take precedence over netplan intent; the parser follows
[systemd's file/drop-in ordering](https://github.com/systemd/systemd/blob/v255/man/systemd.network.xml)
and [netplan's YAML ordering](https://github.com/canonical/netplan/blob/1.0/doc/netplan-generate.md).
Only compact settings and source paths are emitted, never whole files. Missing
or unsupported configuration remains unknown. These on-disk diagnostics do not
prove that a daemon has reloaded edited files, and they do not change networking
or migration gates. Netplan parsing uses optional PyYAML; networkd diagnostics
remain available if that parser is absent.
`dhcp-initial-preparation.json` retains the evidence. The official Ubuntu image
must permit AF_PACKET and its DHCP client must request/honor these options.
Unsupported or missing ACK evidence fails before migration rather than guessing.

`06-target-config.yml` retains the existing VXLAN MTU reduction and then polls
Pair-B guest evidence until both report the advertised target MTU, usable leases,
short-T1/T2 renewals, continuing packet probes and unchanged boot IDs. This guard
runs with a new sequence/guest-monotonic anchor captured after the network MTU
update: the last matched renewal ACK must be newer than that anchor. Old phase-04
renewal evidence cannot satisfy it. OVS metadata routing is still valid here.
The guard
runs before `07-migrate-db.yml` freezes Neutron or changes the database. It writes
`dhcp-precutover-preparation.json`; failure includes a reason and stops the run.
Defaults are `validation_dhcp_t1_seconds: 30`, `validation_dhcp_t2_seconds: 60`,
`validation_dhcp_convergence_timeout: 180`, and `target_geneve_mtu: 1442`.
For a different underlay MTU, configure the validation target to match the existing
per-network VXLAN-minus-overhead MTU calculation. There are no long fixed sleeps.

After migration, DHCP availability means a DHCP lease, expected guest IP, usable
interface and basic default routing. DHCP convergence additionally requires the
guest interface MTU and Neutron network MTU to equal the configured target MTU, and its selected
route to 169.254.169.254 to use the fixed IP of the actual `network:distributed`
port on the correct network/subnet. Missing or ambiguous port evidence yields
UNAVAILABLE. No address offset or .2/.3 assumption is used. These checks and the
expected MTU/metadata IP are saved independently in per-VM workload evidence.
Post-migration checks take another fresh sequence anchor after OVN takeover;
pre-cutover health records cannot satisfy them. Metadata access must also pass
for full workload validation.

Full health failure still preserves validation resources and reports
MIGRATED_VALIDATION_INCOMPLETE, even when tenant packet recovery and outage
measurement are valid. Reports print `Packet loss: ... %` and
`Actual dataplane outage: ... s`, or UNAVAILABLE when packet evidence is invalid.
Historical phase checkpoint names and late-resume resource IDs are unchanged.
Older run guests without the new DHCP evidence cannot claim DHCP convergence;
resume does not fabricate preparation or silently reboot them.

## Pair-B controlled pre-cutover remediation and checkpoints

`validation_allow_pre_cutover_guest_reboot` defaults to **false**. To permit the
planned reboot of validation-owned Pair B on a fresh lab run:

```bash
ansible-playbook -i /root/multinode migrate-to-ovn.yml \
  -e validation_allow_pre_cutover_guest_reboot=true
```

After the bounded automatic attempt fails, a guest is REBOOT_REQUIRED only if
Neutron MTU is the target, its DHCP lease is usable, a real matched renewal is
newer than the phase-06 anchor, cadence and effective T1/T2 are sane, metadata
and routed packets work, diagnostics show no static MTU and enabled DHCP MTU
consumption, and its boot is still the original boot. Any other problem stops
before freeze. Diagnostics that are unknown cannot authorize reboot.

Only exact Pair-B UUIDs in the checkpoint with owned ports, matching server
ownership metadata and unchanged identity may receive a Nova
[SOFT reboot](https://docs.openstack.org/openstacksdk/2024.1/user/proxies/compute.html).
The guests are rebooted sequentially. Each must reach ACTIVE and demonstrate a
new boot, unchanged server/port/IP, target MTU, usable DHCP, metadata and fresh
routed success before the next guest is handled. Pair A is explicitly excluded.
The final Pair-B migration baseline is established only after both are healthy;
post-migration boot continuity compares to this baseline, allowing the planned
pre-cutover reboot without allowing a migration-time reboot.

`validation-resources.json` uses schema version 2:

```json
{
  "schema_version": 2,
  "pre": {
    "router": "UUID", "security_group": "UUID",
    "networks": {"0": {"network": "UUID", "subnet": "UUID", "interface": true}, "1": {}},
    "measure": {"0": {"server": "UUID", "port": "UUID", "fixed_ips": [], "record_vm": "measure0", "owned": true}, "1": {}},
    "existing": {"0": {"server": "UUID", "port": "UUID", "fixed_ips": [], "record_vm": "existing0", "owned": true}, "1": {}}
  },
  "post": {"router": "UUID", "security_group": "UUID", "networks": {}, "fresh": {"0": {}, "1": {}}}
}
```

Server and explicit port UUIDs are saved immediately after creation. Existing
checkpoints are reused; an uncheckpointed port/server response is recoverable
only through its unique role name and owned network/port association. Ambiguous
matches fail. Historical dual-purpose pairs retain their UUIDs and old console
labels as `existing`/`fresh`; they cannot be relabeled Pair A or authorize reboot.
Missing Pair-A evidence cannot be reconstructed after takeover.

The separate evidence checkpoints are:

- `measure-readiness.json`, `measure-baseline.json`, `validation-window.json`
  (immutable PCAP start and final recovered end), `measure-post-checks.json`.
- `existing-initial-baseline.json`, `dhcp-initial-preparation.json`.
- `existing-mtu-automatic.json`, `existing-mtu-remediation.json`
  (per-guest request and completion), `existing-migration-baseline.json`.
- `dhcp-precutover-preparation.json` and existing per-pair workload checks.
- `post.fresh` in `validation-resources.json`, plus `post-ovn-bindings.json`.

Reboot requests are journaled **before** Nova is called. A crash after journaling
never causes a second request. On retry, requested guests are observed until a
new healthy boot is proven; completed guests are checked against their saved
boot. If a crash occurred before Nova accepted the request, the request is
ambiguous and the gate times out safely for operator investigation. There is no
automatic resend. Do not run concurrent validation orchestration commands.
Network MTU original/target pairs are retained in the existing TSV journal so
phase-06 preparation retries cannot subtract the overhead delta twice. For a
pre-cutover retry, use the persisted config and sourced OpenRC with
`python3 scripts/workload_validation.py prepare-dhcp <run-directory>`; the late
resume entrypoint still starts after verified takeover and never redoes freeze.

Reports retain existing timing/validation fields and add `dataplane_continuity`,
`existing_workload_migration`, `fresh_ovn_provisioning`,
`automatic_mtu_convergence`, `remediation_required`, `remediation_action`,
`pre_cutover_mtu_readiness`, `existing_workload_mtu`,
`existing_workload_boot_continuity`, `new_ovn_workload_mtu`,
`new_ovn_workload_geneve`, plus Pair-A measurement/boot labels in `dataplane_probe`.
Automatic MTU convergence can be FAIL while overall SUCCESS follows proven,
controlled remediation, full post-migration validation and successful cleanup.


## Pair-A compute capture journal and post-cutover Pair-B convergence

`pair-a-capture.json` has schema version 1 and records `compute_host`, `binding_host`,
measure0 `server`/`port`, `source_ip`/`peer_ip`, `tap`, `directory`, `path`, `intent_at`,
`status`, and `remote`. Remote state records the supervisor/tcpdump PID, Linux process
start ticks and compute boot ID, `started_at`, `stopped_at`, heartbeat/gap state,
return code and dropped-packet count. Controller and compute start intents precede
launch; a compute lock prevents duplicate capture. An exact live capture is reused.
A missing journal, dead/mismatched process or uncertain launch fails safely rather
than replacing its PCAP. A stopped capture can be fetched again after an interrupted
finalization; start/end indices are never reset. Historical console measurements
remain historical diagnostics and cannot become a current authoritative PCAP result.
A new measurement run is necessary if capture was not started before migration.

After Neutron restoration, Pair B first has a bounded automatic DHCP convergence
attempt. `existing-post-cutover-anchor.json` is latched after restoration (therefore
later than takeover), with guest sequence and monotonic fences; pre-cutover renewals
cannot pass. Identity, baseline boot, ACTIVE/binding, routed traffic, usable DHCP,
target MTU, fresh matched renewal with the unchanged sane timer/cadence semantics,
actual distributed metadata next-hop and metadata access must all pass.

Only a validation-owned Pair-B guest with unchanged identity/boot, target MTU,
healthy routed traffic, usable lease, a running renewal observer and **no** fresh
renewal may be POST_CUTOVER_REBOOT_REQUIRED. Its route must be demonstrably stale.
A read-only query must prove an exact LSP with a unique referenced IPv4 DHCP_Options
row for that subnet, target MTU, configured T1/T2, gateway and correct metadata route,
plus an exact SB Port_Binding with chassis and `up=true`. Missing/ambiguous distributed
ports, incorrect DHCP options/bindings, changed identity or generic metadata failure
with an already-correct route cannot authorize reboot.

`validation_allow_post_cutover_guest_reboot: false` is independent of the pre-cutover
opt-in. To allow both narrowly owned lab remediations on a fresh run:

```bash
ansible-playbook -i /root/multinode migrate-to-ovn.yml \
  -e validation_allow_pre_cutover_guest_reboot=true \
  -e validation_allow_post_cutover_guest_reboot=true
```

Each SOFT reboot is sequential and journaled before Nova is called. New boot,
unchanged server/port/IP, genuine OVN renewal, target MTU, correct metadata route,
metadata access and routed traffic must pass. Pair A/C are explicitly excluded.
An ambiguous request is never resent; resume either proves completion on the new
boot or fails safely. Completed boots cannot be rebased to an unexpected third boot.
`existing-migration-baseline.json` remains immutable; a separate
`existing-post-cutover-baseline.json` records the expected intentional new boots.

Post-cutover evidence is stored in `existing-post-cutover-automatic.json`,
`existing-post-cutover-remediation.json` (per-guest request/completion and identity),
`existing-post-cutover-readiness.json`, the anchor/baseline, and each exact LSP's
`existing0/1-ovn-dhcp.json`. Automatic FAIL is retained after successful remediation.
Reports add `automatic_guest_mtu_convergence`, `pre_cutover_remediation_required`,
`pre_cutover_remediation_action`, `automatic_post_cutover_dhcp_convergence`,
`post_cutover_remediation_required`, `post_cutover_remediation_action`,
`post_cutover_dhcp_ready`, `post_cutover_metadata_route_ready`,
`post_cutover_metadata_ready`, `server_uuid_preserved`, `port_uuid_preserved`,
`fixed_ip_preserved`, and `pair_a_capture`, plus grouped remediation evidence.
Existing field aliases remain compatible. With all final validations and cleanup
complete, seamless runs report SUCCESS; a required post-cutover reboot reports
SUCCESS_WITH_REMEDIATION and Pair-B `PASS AFTER REMEDIATION`. Pre-cutover-only
remediation retains SUCCESS with its explicit fields. Any unresolved validation
failure reports MIGRATED_VALIDATION_INCOMPLETE, preserves evidence/resources and
never rolls back. Pair-A capture counts every actual Pair-A loss during either
remediation; Pair-B packets never enter that metric.
