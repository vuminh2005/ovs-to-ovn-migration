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
VM1 and VM2 are created while OVS is active on two VXLAN networks joined by a
centralized router. Each guest probes its peer, so traffic crosses tenant L3.
After initial guest checks pass, the preservation snapshot is refreshed to include
these resources. No Floating IP, provider network, SSH key or tenant SSH is used.

Cloud-init installs a Python service on both guests. VM1 sends one ICMP request
approximately every 0.2 seconds, with up to one second per-request timeout;
concurrent requests keep the cadence during loss. Every completion emits a JSON
record to ttyS0 containing run, VM, sequence, request timestamp and result.
VM2 also probes VM1. Every five seconds both emit health records with the associated probe sequence,
DHCP lease/address/route evidence
and an HTTP metadata check requiring their own instance UUID. Nova console output
is polled every two seconds and accumulated under the run directory, including
while Neutron is frozen. The guests require no package downloads.

The guest probes run through staging, but staging is excluded from migration
packet-loss accounting. The `anchor-start` action at the end of Phase 6's opening
localhost play, immediately before legacy agents stop, saves VM1's latest
completed `(boot, seq)` in `validation-window.json`. That start anchor is never
rewritten on retry. After BOTH pre-existing guests pass fresh post-migration
checks, the first recovery end anchor is saved. Packet metrics use precisely
`start.seq < packet.seq <= end.seq` on the same boot. The start boundary is the
latest API-observed completed attempt immediately before cutover, with Nova
console delivery and the probe interval limiting its resolution. OVN PortBinding
convergence remains an independent metric.

Each readiness invocation first snapshots per-VM sequence fences in
`pre-freshness-anchors.json` or `post-freshness-anchors.json`. Only higher packet
sequences and higher associated health sequences can pass; a guest with no
records initially must first establish a fence, then emit newer evidence. At
least five consecutive successful new attempts plus new DHCP/metadata health
are required. Guest/controller epoch offsets do not affect coverage or readiness;
wall-clock synchronization is no longer a correctness requirement.

Every consecutive failure burst is recorded. `first_failure_timestamp` and the
compatibility `recovery_timestamp` describe the FIRST burst. The two
`longest_outage_*_timestamp` fields describe the LONGEST recovered burst, and
`actual_dataplane_outage_seconds` is exactly its recovery timestamp minus its
first-failure timestamp, from the same guest. No loss yields zero outage with
null longest-burst timestamps. Missing sequences, conflicting records, guest
reboots, internal guest timestamp jumps, or an unrecovered tail yield UNAVAILABLE
and null actual outage. Guest monotonic launch markers also check internal timing.
This is sampled ICMP outage at approximately 0.2-second resolution.

After restore, the same server/port IDs and fixed IPs are checked alongside fresh
bidirectional packet, DHCP lease, and metadata evidence. VM3/VM4 are then created
on separate routed Geneve networks and must pass the same fresh guest checks.
Cleanup is gated on ALL initial/surviving/fresh workload checks, VM3/VM4 Southbound
bindings, VXLAN -> Geneve semantics, valid tenant probe coverage, orchestration,
and resource preservation. Guest/console evidence and a preliminary report are
saved before cleanup. On complete success, cleanup removes VM3/VM4 first, then
VM1/VM2, and saves `post-cleanup.json` and `pre-cleanup.json`, including exact
owned UUIDs deleted. The report is rebuilt afterwards; SUCCESS requires both
cleanup results to pass.

FAIL/UNAVAILABLE preserves BOTH pairs for debugging and writes
MIGRATED_VALIDATION_INCOMPLETE; no ML2/OVN rollback is attempted. Cleanup API
failures journal the completed deletions and report incomplete rather than claiming
success. Retrying a cleanup checkpoint reuses the preserved validation evidence
and resumes journaled deletion instead of trying to validate already deleted VMs.
The resume entrypoint checks the complete evidence gate before allowing this
cleanup-only validation path. No new instances are created on such a retry.
All deletion operations use only IDs in `validation-resources.json`.

Guest probes end when successful resources are deleted, or after the configurable
four-hour lifetime on retained debugging VMs. Missing records, expiration, or
reboot prevent a complete metric. Nova console polling merges unique JSON records
from a bounded tail, default 20,000 lines per guest (`validation_console_tail_lines`).
At five attempts/second plus health records this is roughly 65 minutes of probe
records, before other guest console output. Nova may retain less. Increase the
tail or reduce the poll interval for the lab if needed; missed required sequences
always yield UNAVAILABLE, never assumed packet success.

Configuration in `group_vars/all.yml`:

| Variable | Purpose |
| --- | --- |
| `validation_workloads_enabled` | Enable guest validation (default true) |
| `validation_image`, `validation_flavor` | Optional existing image/flavor UUID or exact name; empty selects managed defaults |
| `validation_name_prefix` | Unique resource names also include run ID and stage |
| `validation_pre_cidrs`, `validation_post_cidrs` | Two tenant CIDRs per pair |
| `validation_timeout_seconds` | Readiness/recovery/deletion timeout |
| `validation_probe_lifetime_seconds` | Guest service lifetime |
| `validation_console_poll_seconds` | Nova serial evidence collection interval |
| `validation_console_tail_lines` | Bounded console tail per API call; default 20,000 |
| `dataplane_probe_interval_seconds` | Guest ICMP interval |

Use an Ubuntu cloud image containing cloud-init, Python3, ping, iproute2,
systemd, DHCP lease files in `/run/systemd/netif/leases` or `/var/lib/dhcp`,
and writable ttyS0. Unsupported images fail before downtime with console
instructions. DHCP evidence proves a real lease and usable configuration; it
also proves new OVN DHCP for VM3/VM4, but VM1/VM2 may retain their source lease.
Nova must expose serial
console output through the API, and the deployment Python environment must contain
`openstacksdk` and PyYAML. Credentials must permit the existing admin snapshots
and creating/deleting the dedicated resources. Supply non-overlapping lab CIDRs.

The optional `MIGRATION_PROBE_TARGET` remains a separate deployment-host canary
under `deployment_host_probe`; it is not used for tenant workload conclusions.

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
- `existing_workload_dhcp`
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
`coverage_complete`, `timing_valid`, `start_anchor`, `end_anchor`,
`failure_bursts`, and `measurement`. An unavailable probe keeps numeric fields
null when no records exist; incomplete captures retain observed counts but must
not be treated as complete packet-loss measurement. `initial_ovs_workload_validation` records the pre-downtime guest gate.
`workload_checks` contains independent initial and post-migration per-VM checks, `existing_network_semantics` contains the segment audit,
and `validation_orchestration` retains subprocess failure details. The previous
host-canary evidence is retained separately as `deployment_host_probe`.

On late retries, resource-preservation comparisons exclude additions only for
checkpointed UUIDs belonging to this run's fresh VM3/VM4 topology. Original
resource deletions and unrelated additions still fail the existing guard.
