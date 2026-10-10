# Work Item 2 (Việc 2): reset to OVS and EW rebuild

This is an **explicitly destructive five-node lab workflow**, separate from
migration phases 00–13. It implements Work Item 2 of the six-Work-Item plan,
without North–South support or whole-cloud checkpointing. No controller commands
below were executed during development. **Reset and fresh provisioning remain
unverified in the lab.** Work Item 1's operator-reported existing-lab reuse checks
do not prove rebuilding from empty.

`reset-ew-lab.yml` is the entrypoint. `reset-lab-to-ovs.yml` imports it for
compatibility. Default action is now **preflight**; the old bare
`reset_lab_confirm=true` invocation alone cannot destroy anything. Select an
explicit generation and `reset_action=apply`. Neither entrypoint starts baseline,
TCP measurements, Pair A/B/C or migration.

## Resolve and retain inputs

| Input | Default/interface |
| --- | --- |
| Kolla environment/inventory | `reset_kolla_venv=/root/venvs/kolla-2024.1`; `reset_kolla_inventory` follows the invocation inventory |
| Live configuration | `reset_globals_path` follows `kolla_globals_path`; `reset_passwords_path=/etc/kolla/passwords.yml`; `reset_config_path=/etc/kolla/config` |
| Retained authoritative OVS source | `reset_source_globals=/root/ew-private/source-kolla/globals.yml`; `reset_source_config=/root/ew-private/source-kolla/config` |
| Private input mappings | `reset_access_file=/root/ew-access.yml`; `reset_credentials_file=/root/ew-private/ew-provision-inputs.yml`; loaded privately using existing interfaces |
| Image/bootstrap | Existing `ew_provision_spec` or `ew_provision_spec_file`: pinned prepared QCOW2/sidecar, reviewed reconstructed adapter paths/SHA256s |
| Prior state/trust | Existing `ew_provision_state_dir`, `ew_guest_known_hosts`; select actual previous state, not a new empty location |
| Journal | `reset_generation_root=/var/lib/ovs-to-ovn-reset`, explicit `reset_generation` |
| Additional retained artifacts | Controller `reset_required_retained_paths`; `reset_host_protected_paths: {inventory_host: [absolute_paths]}` for remote hosts |

Paths are configurable controller defaults, not verified live facts. Retain a
reviewed **complete source deployment globals file**, selected from authoritative
source records, with actual VIPs/interfaces/service settings and overrides.
Do not relabel current OVN globals as source. **117.1.28.69 is management access,
not an EW workload endpoint.** Original DB/broker scripts are not required:
reviewed reconstructed adapters remain accepted, with unchanged pins/interfaces.

Retained source custom configuration must include supported Kolla override paths:

```ini
# <reset_source_config>/neutron.conf
[DEFAULT]
global_physnet_mtu = 1450

# <reset_source_config>/neutron/ml2_conf.ini
[ml2]
path_mtu = 1450
overlay_ip_version = 4

# <reset_source_config>/neutron/openvswitch_agent.ini
[securitygroup]
firewall_driver = openvswitch
```

These are confirmed **underlay/configured path** limits, not tenant MTUs. Every
chassis tunnel interface must match `reset_expected_underlay_mtu` (1450). The
existing `mtu_plan.calculate` and installed Geneve template resolve header 38 to
**VXLAN 1400 → Geneve 1392**. Contradictory effective configuration fails; a
reduced 1392 network cannot become a new original source. Retain other reviewed
source overrides in the tree. Omitted ML2 mechanism/type uses source Kolla
generation; explicit contradictory OVN settings are refused.

Preflight validates the standalone QCOW2/hash/sidecar using existing read-only
checks, adapter pins, private password files and matching SSH private/public
keys. Credentials must already be retained; this workflow never recovers them
from guests it is about to delete, rotates them, downloads workload dependencies
or installs guest packages. Keep files root-private; do not print their contents.

Before any destructive task, all five hosts supply current DMI product UUID,
machine-id/hostname, tools, container/mount/volume paths, interfaces and underlay
evidence. Product UUIDs must be distinct; cloned machine-ids are supported.
Only Kolla containers and plain local Docker volumes are accepted. Unknown host
namespaces, ambiguous volumes, DPDK, Swift/Octavia and unsafe VIP cleanup fail.
Management SSH retains strict checking and existing trust. A local inventory
deployment alias is supported; destructive Kolla operations explicitly limit
their scope to the five inspected hosts.
Every reset-controlled child Ansible invocation receives the same
`management-trust-vars.json` as its final extra-vars input. This includes the
source-readiness imports of `02-precheck.yml`, EW first apply/verify/second apply,
and Kolla's child processes. Inventory/config/environment SSH arguments are
resolved and retained per host (including jump hosts); per-host private key,
user and port selection remain intact. Strict checking and the selected
management known-hosts file are placed first in SSH arguments, with both
Ansible host-key-checking flags enabled. Ambient permissive options cannot
override them. Existing multiplexed sessions are not reused, and automatic
management known-hosts updates are disabled. Missing policy or conflicting
keys fail; neither management nor guest trust is cleared/replaced. The policy
requires SSH/local connections and an absolute literal trust path (no SSH `%`
tokens or newlines). Guest-generation trust remains a separate EW transport input.
The resolved policy is bound to the generation; changed connection options on
continuation fail before any further reset stage instead of silently changing trust.
Pre-destructive preflight also refuses unmounted volumes that Kolla would leave
behind, and checks inspectable libvirt/QEMU state on computes. Orphan volumes
created during an interrupted redeploy are permitted only after the same journal
proves destroy completed. Read-only reboot reconciliation must also prove the
existing clean-network/interface gates, not merely observe a new boot ID.

Installed destroy tasks/scripts are pinned to reviewed
[Kolla 18.8.0 source](https://github.com/openstack/kolla-ansible/tree/18.8.0/ansible/roles/destroy/tasks).
[Cleanup-host](https://github.com/openstack/kolla-ansible/blob/18.8.0/tools/cleanup-host)
removes configured data paths/generated host configuration, and
[cleanup-containers](https://github.com/openstack/kolla-ansible/blob/18.8.0/tools/cleanup-containers)
removes Kolla-mounted volumes. Preflight resolves installed defaults, inventory,
current globals/passwords, then binds those same storage values into destroy.
Different installed scripts require review, with no runtime download/fallback.
Image/dev-repository deletion is disabled. Unconsolidated `globals.d`, Kolla
`EXTRA_OPTS`/`ANSIBLE_SERIAL`, partial inventories/tags/limits and Kolla shell-CLI
paths with whitespace/metacharacters are refused.

Protected scope includes QCOW2/build directory, sidecar, private recovery inputs,
mapping/passwords, SSH keys/management and guest trust, inventory/Kolla passwords,
source configuration/repository/application/adapters, prior provisioning state,
configured and known backup/snapshot/PCAP roots and all reset generations.
Symlink targets and filesystem mount aliases are checked, not just text prefixes;
ownership/modes are recorded. Declare other retained locations explicitly.
Required missing paths and retained/deleted overlap fail before destruction,
including reset overrides. `reset_delete_migration_backups=true` is refused.

This reset deletes old guests/cloud data. Already collected evidence and original
task receipts survive; uncollected data inside deleted guest disks cannot be
promised preserved. Finish/review collection and exclude concurrent operators
before authorizing reset. No evidence pruning or automatic rollback occurs.

## Generations, stages and retry

The 0700 generation directory contains 0600 journal/log/input/evidence files.
The root lock excludes concurrent workflows. Fsynced `journal.json` binds inputs,
source/private file fingerprints and exact five host identities. Treat the entire
directory as private; do not publish runtime manifests/logs in review packages.
Changing inputs cannot rebind an existing generation.
An existing old provisioning-state lock is also held during reset, preventing a
concurrent ordinary apply from changing the state being archived. Final local
acceptance locks the new provisioning state. The preflight intent itself is
bound before remote collection; a failed collection cannot silently rebind inputs.

Stages: archive → guest stop → destroy → residue → sequential network/compute
reboots → source config → Kolla bootstrap/prechecks/deploy/post-deploy → source
readiness → EW apply → EW verify → second EW apply → acceptance.

- `STARTED` is durable before operations; acknowledged success becomes `COMPLETE`.
  Interruption is incomplete. Completed destructive/config stages are skipped;
  entering deploy/provisioning can never authorize another destroy.
- Interrupted Kolla redeployment uses its existing reconciliation operations.
  Before incomplete provisioning continuation, source API/service/placement/MTU
  checks run again. Unknown servers/networks/routers fail: the cloud must be
  empty or contain only exact generation-checkpointed partial provisioning.
  Existing RabbitMQ quorum, residue, namespaces and OVS service gates remain.
- Interrupted stop/destroy/reboot is **not replayed**. `reconcile` only reads
  current hosts: it proves all guest/QEMU processes stopped, or all destroyed
  containers/volumes/namespaces/generated configs/QEMU gone, or the indicated
  host rebooted. Only proven completion is marked complete. Partial/ambiguous
  destruction needs separate operator investigation; do not edit journals or
  pick another ID to bypass it.
- A different reset cannot apply while the active generation is incomplete.
  Resetting an accepted generation requires a new ID and new confirmation;
  select that accepted generation's provisioning state and trust as the old
  inputs, using its `generation-vars.yml` rather than pre-first-reset defaults.

`historical/*.tar` preserves old state (UUIDs/boots/receipts/pending deployment),
guest trust and controller globals/custom configuration with modes/ownership.
Originals stay untouched. New guests use `<generation>/provisioning` and
`<generation>/guest-known-hosts`. Only authenticated Nova keys for exact newly
checkpointed servers populate missing new trust. Management trust is never
cleared, and ordinary same-generation identities remain strict.

`generation-vars.yml` exposes the new state/trust for later baseline/migration.
Continuation must retain the original reset inputs; passing these new selected
paths to continuation would incorrectly change the old-state binding.

Acceptance requires six validated guests, three real app→DB/queue→worker results,
unchanged new UUID/port/IP/boot/receipt state through verify/second apply,
`changed=false`, no pending deployment, and empty app file/service change lists.
Adapter/BUILD/retry/interrupted-bootstrap behavior and SDK `ether_type` mapping
remain unchanged. Nova keypair IDs are names: reusing `ew-key` is intentional;
UUID-backed resources must be new. Old receipts are historical evidence, not
expected rows in the freshly initialized database.

## Complete controller procedure — prepared, not executed

After reviewing/transferring the patch on top of audit commit
`0cb32b19ba7e2918048d013a8a7f04adc9e223da`, open an established controller session.
Resolve actual paths and review retained **complete** source files first. Preserve
any existing staged controller work. The batch needs no `rg` and prints no secrets.

```bash
set -euo pipefail
cd /root/ovs-to-ovn-migration
source /root/venvs/kolla-2024.1/bin/activate
git status --short --branch
git log -3 --oneline
INV=/root/multinode
GEN=ovs-ew-rebuild-20261010-01
ROOT=/var/lib/ovs-to-ovn-reset
ACCESS=/root/ew-access.yml
INPUTS=/root/ew-private/ew-provision-inputs.yml
test -r "$INV" && test -r "$ACCESS" && test -r "$INPUTS"
test -r /root/ew-private/source-kolla/globals.yml
test -r /root/ew-private/source-kolla/config/neutron.conf
test -r /root/ew-private/source-kolla/config/neutron/ml2_conf.ini
test -r /root/ew-private/source-kolla/config/neutron/openvswitch_agent.ini
ansible-playbook -i "$INV" reset-ew-lab.yml --syntax-check
ansible-playbook -i "$INV" ew-provision.yml --syntax-check
RESET_ARGS=(-i "$INV" reset-ew-lab.yml
  -e "reset_generation=$GEN" -e "reset_generation_root=$ROOT"
  -e "reset_access_file=$ACCESS" -e "reset_credentials_file=$INPUTS")
# Add identical reviewed reset overrides to RESET_ARGS for all invocations.
# Declare extra protected artifacts through a private reviewed YAML extra-vars file.
ansible-playbook "${RESET_ARGS[@]}" -e reset_action=preflight
```

Privately inspect `<ROOT>/<GEN>/retained-inputs.json`, `hosts/*`,
`source-mtu-plan.json` and `journal.json`: five identities, every deletion/mount
path, extra evidence, actual source VIP/interfaces and calculated 1400/1392.
Preflight reads hosts and writes controller evidence, without OpenStack API
calls or guest shutdown. It is not a reset success claim.

**Only after explicit destructive authorization:**

```bash
ansible-playbook "${RESET_ARGS[@]}" -e reset_action=apply -e reset_lab_confirm=true
```

**On interruption**, retain the same ID/inputs/resources/journal:

```bash
ansible-playbook "${RESET_ARGS[@]}" -e reset_action=continue
# Only if the journal identifies an interrupted destructive boundary:
ansible-playbook "${RESET_ARGS[@]}" -e reset_action=reconcile
# If reconciliation refuses, investigate separately; do not replay destruction.
ansible-playbook "${RESET_ARGS[@]}" -e reset_action=continue
```

**Final acceptance**, after workflow completion (local evidence only, no SSH/API):

```bash
ansible-playbook "${RESET_ARGS[@]}" -e reset_action=accept
python3 - "$ROOT/$GEN" <<'PY'
import json,pathlib,sys
p=pathlib.Path(sys.argv[1]); a=json.loads((p/'acceptance.json').read_text())
assert a['status']=='PASS' and a['second_apply_changed'] is False
j=json.loads((p/'journal.json').read_text())
assert all(s['status']=='COMPLETE' for s in j['stages'].values())
before=json.loads((p/'first-state.json').read_text())
after=json.loads((p/'provisioning/resources.json').read_text())
for key in ('resources','guests','preserved_tasks'): assert before[key]==after[key]
assert len(after['guests'])==6 and len(after['preserved_tasks'])==3
print('PASS: six identities and three receipts preserved; second apply unchanged')
PY
```

Review first/verify/second readiness and private logs. A further ordinary rerun
uses `ew-provision.yml -e @"$ROOT/$GEN/generation-vars.yml" -e ew_provision_action=apply`;
it must remain unchanged. Later separately authorized `ew-baseline.yml` or
`ew-migrate.yml` uses the same generation-vars extra file. Neither is part of
reset acceptance. The latter executes baseline and migration; TCP 18080/18081 and
pre-freeze target MTU gates retain their original timing.

Pending lab facts: installed cleanup hashes/default layout, inventory/strict
management access, available Kolla images/tools, retained source/effective MTU,
fresh netfix cloud-init/host-key publication, namespace access, genuinely fresh
DB/broker initialization, interrupted deploy/BUILD recovery and no-op acceptance.
Passing offline mocks and syntax cannot establish these facts.
