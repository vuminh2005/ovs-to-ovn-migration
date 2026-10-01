# Kolla-Ansible ML2/OVS -> ML2/OVN migration automation v2

V2 turns the lab-validated migration into a **single-invocation migration framework** for its supported scope. It discovers the Kolla inventory path from the `-i` argument, finds `kolla-ansible` from the active execution environment, parses `/etc/kolla/globals.yml`, uses standard Kolla inventory groups to find control/network/compute/OVN hosts, and reads the generated ML2/OVN config to discover the real OVN NB/SB connection strings.

## Run

From the Kolla deployment host, with `ansible-playbook` and `kolla-ansible` available in the current environment:

```bash
ansible-playbook \
  -i /root/multinode \
  migrate-to-ovn.yml
```

There are **no mandatory per-cloud edits in `group_vars/all.yml`** for a standard Kolla layout inside the supported scope.

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

## Migration phases

1. Bootstrap / auto-discovery
2. Backup and machine-readable source snapshot
3. Precheck supported scope
4. Stage OVN NB/SB/northd while ML2/OVS remains active
5. Generate target ML2/OVN config and adjust existing VXLAN MTUs
6. Freeze all neutron-server workers and run `neutron-ovn-db-sync-util --ovn-neutron_sync_mode migrate`
7. Stop legacy DHCP/L3/metadata/OVS agents, enable OpenFlow15, deploy `ovn-controller` and OVN metadata, wait for every old compute port to reach `chassis != []` and `up=true`
8. Remove `br-tun`, VXLAN path, `qrouter-*`, `qdhcp-*`, namespace-owned OVS ports and the old OVS-agent br-int/br-ex patch pair; preserve `ovnmeta-*`
9. Enable/start neutron-server with the target ML2/OVN config
10. Validate resource preservation and create/delete a temporary Geneve network
11. Generate migration metrics report

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

### Real packet loss / dataplane downtime

A generic deployment host usually cannot reach isolated tenant VM addresses, so V2 does **not fabricate a packet-loss number**. Without a reachable canary, the JSON reports packet loss as `null` and always records the Port_Binding convergence window as a control-side proxy.

If a continuously reachable canary exists, add only an environment variable; no repository edit is required:

```bash
MIGRATION_PROBE_TARGET=192.0.2.10 \
ansible-playbook -i /root/multinode migrate-to-ovn.yml
```

V2 runs `ping -D -i 0.2` across the cutover and reports packet-loss percentage plus an estimated outage based on the largest reply gap. The probe target must genuinely be reachable from the deployment host for this metric to mean anything.

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

For guest-level DHCP, metadata and end-to-end tenant packet validation, a dedicated test VM/canary is still the strongest validation. Those checks cannot be made universally without credentials/access inside a guest, so V2 reports what it can measure truthfully rather than inferring success from a Neutron fixed-IP allocation.

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
