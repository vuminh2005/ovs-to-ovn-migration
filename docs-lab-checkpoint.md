# Cold checkpoint and same-node restore

This is a standalone maintenance tool for the existing five-node Ubuntu 24.04,
Docker/Kolla 18.8.1, Caracal lab. It is **not migration rollback** and is never
imported by the migration, EW baseline, or destructive reset entrypoints. All six
EW guests are stopped and boot again. It does not preserve guest boot IDs or
application RAM/in-flight state. Finish/drain measurements before maintenance.

The tool discovers actual storage and fails closed: exactly one `control`, two
`network` and two `compute` inventory members; six exact configured EW server
UUIDs/ports/IPs/placements; plain local Docker volumes; Kolla-labelled containers
using host networking and persistent systemd units; file-backed libvirt disks
with contained, existing backing chains. Required Nova instances **and cache**,
libvirt volumes, OVSDB, MariaDB, RabbitMQ, Glance, Keystone keys, all `/etc/kolla`
(including passwords/openrc), source unit files/drop-ins and controller inventory
are included. Unknown volumes/binds, special files, external backing chains,
libvirt autostart, a different filesystem for durable storage, or unmanaged
containers stop planning. This deliberately narrow classifier must be reviewed
against the first plan; do not bypass it to obtain a checkpoint.

Host uniqueness requires five distinct, canonical DMI `product_uuid` values from
`/sys/class/dmi/id/product_uuid`; missing, malformed, all-zero and all-FF values
are refused. Cloned hosts may share `machine_id`. Both `machine_id` and hostname
remain recorded, and verification/restoration compare the complete identity
exactly, including product UUID. Older manifests without this identity are
refused rather than upgraded. Boot IDs remain separate: all five original hosts
must reboot after data restoration before finalization can start services.

Fluentd's exact read-only `/var/log/journal` to `/var/log/journal` bind is a host
input. Its resolved path is verified and its read-only mount is retained during
recreation. Host journal history is excluded from archive roots, capacity/writer
checks, quarantine and replacement because it belongs to the intact host OS.

Checkpoint interface identity supports explicit XML `interfaceid` and native OVS
`type="ethernet"` interfaces. Native TAPs require an exact current OVS Interface
name, full Neutron `iface-id` UUID and `attached-mac` matching XML. `vm-id` is not
required; TAP prefixes never prove UUIDs. Source creation cross-checks the API
catalog; recovery cross-checks current interfaces against API/ownership evidence.

Hosts with guest interfaces require `ovsdb-tool` already available on the host.
The current standalone `Open_vSwitch` database is resolved uniquely within
discovered OVS storage, then read using `db-name` and
[`ovsdb-tool query`](https://www.openvswitch.org/support/dist-docs/ovsdb-tool.1.pdf).
No database filename/host path or sealed mapping is substituted for current
evidence. This works with OVS, libvirt and OpenStack services stopped, using
persistent libvirt XML and read-only OVSDB access. With libvirt running, active
and inactive XML must both resolve to identical current port/MAC mappings.
Missing tools, missing/duplicate database or interface evidence, conflicting
XML/MAC/UUIDs, or differing active/inactive mappings block replacement. No tools
are installed and no services/helper containers are started during discovery.

`lsof`, GNU tar with sparse/ACL/xattr support, Docker, the existing Kolla systemd
units, and `virsh`/`qemu-img` in `nova_libvirt` are required for source creation. SDK/API access uses the
existing admin openrc. Management SSH reuses the verified EW key/known-host paths;
no keys, access ports, rules or namespaces are created. Credentials are never
printed. Read-only commands use ordinary Ansible temporary files, but neither
create checkpoint artifacts nor mutate cloud/services/guests. The plan reads
DHCP leases/routes/MTUs/boots through SSH and tests ping and metadata; it does not
submit application jobs. Backup/restore recovery additionally runs the existing
EW E2E probe from each client, including integrity/idempotency/reconciliation.

## Commands

Run on the original controller with the original Kolla venv and inventory.
Keep verified access variables in an operator-owned file outside the repository
(for example `/root/ew-access.yml`, with `ew_guest_ssh_key`,
`ew_guest_known_hosts`, `ew_host_ssh_key`, and `ew_host_known_hosts`). Defaults,
timeouts and headroom are declared in `lab-checkpoint.yml`; migration defaults
are unchanged. Always use one shared, explicit checkpoint ID on every command.

First read-only discovery/plan:

```bash
ansible-playbook -i /root/multinode lab-checkpoint.yml \
  -e @/root/ew-access.yml -e lab_checkpoint_id=pre-step5-20261009
```

After reviewing that plan, maintenance creation (requires confirmation):

```bash
ansible-playbook -i /root/multinode lab-checkpoint.yml \
  -e @/root/ew-access.yml -e lab_checkpoint_id=pre-step5-20261009 \
  -e lab_checkpoint_action=create -e lab_checkpoint_confirm=pre-step5-20261009
```

Read-only full verification and restore planning:

```bash
ansible-playbook -i /root/multinode lab-checkpoint.yml \
  -e @/root/ew-access.yml -e lab_checkpoint_id=pre-step5-20261009 \
  -e lab_checkpoint_action=verify
ansible-playbook -i /root/multinode lab-checkpoint.yml \
  -e @/root/ew-access.yml -e lab_checkpoint_id=pre-step5-20261009 \
  -e lab_checkpoint_action=restore-plan \
  -e '{"lab_checkpoint_validation_runs":["/root/ovs-to-ovn-backup/ACTUAL-MIGRATION-RUN"]}'
```

Restore applies coordinated data replacement, **not** a reset/redeploy:

```bash
ansible-playbook -i /root/multinode lab-checkpoint.yml \
  -e @/root/ew-access.yml -e lab_checkpoint_id=pre-step5-20261009 \
  -e lab_checkpoint_action=restore-apply -e lab_checkpoint_confirm=pre-step5-20261009 \
  -e '{"lab_checkpoint_validation_runs":["/root/ovs-to-ovn-backup/ACTUAL-MIGRATION-RUN"]}'
```

Omit the validation-runs override when there are no additional validation
resources. Otherwise provide the exact run directories; live server metadata,
explicit owned ports/fixed IPs and role must match their schema-2 ownership
checkpoints. Additional networks/subnets/routers/SGs and managed prerequisites
must be journaled in those runs. Unrelated current resources, changed original
EW placement, missing images, corrupt archives or any failed node preflight
prevent **all** shutdown/replacement operations.

### Explicit API-independent recovery

The default `lab_checkpoint_recovery_mode=api` retains live, all-project resource
scope checks and rejects unrelated resources. An API failure never automatically
switches modes. Creation/plan still require healthy APIs, running primary
services and running EW guests. Recovery discovery allows stopped primary
services/guests without weakening storage, image, host or writer checks.
`verify` does not initialize the OpenStack SDK.

For a frozen/unavailable Neutron API, explicitly select
`lab_checkpoint_recovery_mode=offline` and supply a private
`lab_checkpoint_offline_scope_file`. This path is restricted to the same isolated,
exclusively owned lab. The declaration binds the sealed manifest SHA256 and
every supplied ownership journal SHA256, and asserts no unjournaled resources or
concurrent resource writers. This is an **operator trust boundary**, not a live
API inventory. Establish exclusive maintenance ownership, stop concurrent
automation, and consult the last trusted resource inventory and exact migration
ownership journals. If logical scope is unknown or other users created resources,
do not make the assertion: recover API access separately and use normal API mode.

Offline checks verify all five machine identities, exact cached source images,
archives/private inputs, storage/capacity, complete host libvirt UUID/placement
sets, original EW port UUIDs/MACs and exact journal-owned validation port UUIDs.
Every host QEMU process must match the running libvirt UUID set. Stopped libvirt
definitions are read from its classified persistent XML volume; no helper,
container or daemon is started. Unknown running QEMU, missing XML, ambiguous
ownership, paused domains, unexpected interfaces or extra domains stop all
shutdown/data replacement. Running QEMU with stopped libvirt is refused; repair
the known management channel under separate authorization, never force-kill guests.

API-only additions (IAM, images, flavors, unattached ports/networks), actual
validation IP leases and Nova ownership metadata cannot be independently verified
offline. Their scope is explicitly attested; journal identities are not presented
as live observations. Existing storage is retained in quarantine. Final success
still requires the restored source APIs, exact sealed resource inventory and full
guest/EW health checks.

After reviewing that scope, create the private declaration outside the repository.
Replace the ID, operator and migration run. Omit the final run argument if there
are no additional validation resources; include every applicable run. This command
writes only the authorization document:

```bash
python3 - /root/ovs-to-ovn-checkpoints/pre-step5-20261009 \
  /root/pre-step5-20261009-offline-scope.json ACTUAL-OPERATOR \
  /root/ovs-to-ovn-backup/ACTUAL-MIGRATION-RUN <<'PY'
import hashlib, json, os, pathlib, sys
os.umask(0o077)
root, destination = map(pathlib.Path, sys.argv[1:3])
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
manifest = json.loads((root / 'manifest.json').read_text())
assert manifest['state'] == 'SEALED'
assert json.loads((root / 'seal.json').read_text())['manifest_sha256'] == sha(root / 'manifest.json')
journals = {}
for directory in sys.argv[4:]:
    run = pathlib.Path(directory).resolve(strict=True)
    for name in ('validation-config.json', 'validation-resources.json', 'validation-prerequisites.json', 'pre-cleanup.json', 'post-cleanup.json'):
        p = run / name
        if name not in ('validation-config.json', 'validation-resources.json') and not p.exists():
            continue
        assert p.is_file() and not p.is_symlink()
        journals[str(p)] = sha(p)
data = dict(schema_version=1, checkpoint_manifest_sha256=sha(root / 'manifest.json'),
            operator=sys.argv[3], exclusive_lab_scope=True, no_unjournaled_resources=True,
            no_concurrent_writers=True, ownership_journals=journals)
with destination.open('x') as f:
    json.dump(data, f, indent=2)
PY
```

Run offline preflight, then explicitly confirmed apply:

```bash
ansible-playbook -i /root/multinode lab-checkpoint.yml \
  -e @/root/ew-access.yml -e lab_checkpoint_id=pre-step5-20261009 \
  -e lab_checkpoint_action=restore-plan -e lab_checkpoint_recovery_mode=offline \
  -e lab_checkpoint_offline_scope_file=/root/pre-step5-20261009-offline-scope.json \
  -e '{"lab_checkpoint_validation_runs":["/root/ovs-to-ovn-backup/ACTUAL-MIGRATION-RUN"]}'
ansible-playbook -i /root/multinode lab-checkpoint.yml \
  -e @/root/ew-access.yml -e lab_checkpoint_id=pre-step5-20261009 \
  -e lab_checkpoint_action=restore-apply -e lab_checkpoint_confirm=pre-step5-20261009 \
  -e lab_checkpoint_recovery_mode=offline \
  -e lab_checkpoint_offline_scope_file=/root/pre-step5-20261009-offline-scope.json \
  -e '{"lab_checkpoint_validation_runs":["/root/ovs-to-ovn-backup/ACTUAL-MIGRATION-RUN"]}'
```

Preflight identifies its scope as
`HOST_LIBVIRT_VERIFIED_API_SCOPE_OPERATOR_ATTESTED`; normal mode reports
`LIVE_API_AND_HOST_VERIFIED`. Restore state preserves this distinction. Neither
offline preflight nor apply initializes an SDK connection or calls OpenStack.
The existing local openrc/SSH/venv inputs remain necessary for later health;
sourcing an openrc itself makes no API call.
Completed/partial validation cleanup receipts are also bound to the declaration;
a journaled server deletion must agree with absence of that libvirt domain.
Missing or inconsistent cleanup receipts fail closed.

A successful apply returns `DATA_RESTORED_REBOOT_REQUIRED`, never restore success.
Reboot **all five hosts** under separate operator control before continuing.
Use the inventory to identify them; do not guess hostnames. For the four remote
network/compute nodes, the standard `ansible.builtin.reboot` maintenance module
may be invoked from the controller, then reboot the controller itself. This is
an explicit maintenance step because rebooting the controller would kill its
own orchestrator. Source units and container restart policies are held disabled
until finish; never start services/guests between apply and the reboot barrier.
A cold reboot clears OVN kernel flows, namespaces/taps, and stale libvirt runtime;
OVN containers are removed without deleting volumes, their units stay disabled,
and original source container definitions/configuration/OVSDB are reinstated.
Old OVN volumes remain retained and unused. Known metadata socket/PID residue
is removed only inside its exact recorded runtime volume.

Then, on the controller after all hosts return:

```bash
ansible-playbook -i /root/multinode lab-checkpoint.yml \
  -e @/root/ew-access.yml -e lab_checkpoint_id=pre-step5-20261009 \
  -e lab_checkpoint_action=restore-finish -e lab_checkpoint_confirm=pre-step5-20261009
```

Finish (also after offline recovery) verifies all five machine identities and **new host boot IDs**, starts
only recorded source containers/units in infrastructure order, restores original
restart/enabled states, starts the originally ACTIVE guests through Nova, and
checks container health, source ML2/OVS/VXLAN, resource UUIDs/MTUs/placement,
guest-observed DHCP/routing/MTU/metadata and existing EW application E2E health.
Only then can it write `RESTORED_HEALTHY`. Read the private health JSON for changed
guest boots. Run a **fresh** `ew-baseline.yml` baseline before any new migration.
Never resume an old migration/capture run after maintenance or reuse old anchors.

## Evidence, space, and failures

Every node keeps `/root/ovs-to-ovn-checkpoints/<ID>/data.tar`, private full Docker
restore inputs, a compact plan and node `operations.json`. The controller also
keeps copies under `nodes/<inventory-host>/`, `manifest.json`, `seal.json`,
`restore-inputs.json`, `controller-operations.json`, recovery health and restore
state. Directories are private and artifacts are mode 0600. They deliberately
contain credentials/key material; do not attach them to source reviews or Git.
No Docker image export is included: **all exact source image IDs must still be
cached on their original hosts**. Do not prune/delete containers' images, volumes,
source paths, host OS, venv, inventory, SSH trust/keys, or checkpoint files.

Space is discovered, not based on the observed lab totals. The conservative
budget uses twice apparent bytes per node (sparse tar plus verification/extract
staging), explicit headroom (default 5 GiB), and a complete extra central copy on
the controller. Allocated bytes are recorded separately. Durable storage must
share the checkpoint filesystem for this bounded implementation. Sparse files,
numeric ownership, permissions, ACLs/xattrs, contained symlinks and backing files
are preserved; sockets/PIDs are excluded. Cold QCOW2 checks are non-repairing;
tar content is compared to the stopped source, every archive is parsed/read and
hashed, and central copies are rechecked before sealing. Restore stages all roots
on each node then renames current roots into that node's bounded `quarantine/`;
no checkpoint/archive or volume is pruned. Budget current allocated data plus
staging before maintenance; quarantine is retained for operator investigation.

Any interrupted/failed operation is journaled before mutation. No entered data
replacement is automatically replayed; a partial restore is never successful.
Read-only verify and the journals can be rerun. `restore-finish` is a deliberate
continuation **only after a complete apply and all reboots**; it cannot finish a
partial apply. All five controller/node data-restore entries must be COMPLETE;
unknown/in-flight operations still block finalization.

For a temporary `restore-finish` health failure, rerun the same finish command
above with the same ID, inventory and access inputs. Offline scope arguments are
not needed for finish. No data replacement repeats. All five reboot checks run
again. Previously completed source starts reconcile actual container
names/images/mounts, restart policies and unit identities/enabled/active states.
Healthy starts are skipped; known inactive/failed services may be started again
in source order. Changed definitions, masked/activating units or INTENT operations
refuse. Start/health attempt history is retained. The SDK connects only after all
necessary source starts, then bounded health validation runs again. Failure keeps
`DATA_RESTORED_REBOOT_REQUIRED`; full health alone writes `RESTORED_HEALTHY`.
Do not rerun apply or edit journals to retry health.

Backup failures attempt original-service/guest recovery only after all
remote journals confirm there is no in-flight operation. If transport or remote
execution is ambiguous, leave everything intact and inspect node journals/PIDs;
`recovery-required.json` marks the need for operator recovery. Do not edit journals
to bypass guards or start writers while an archive/extract process may still run.
No automatic restore, force-destroy of guests, broad prune, reset playbook, SQL
workaround or silent crash-consistent fallback exists.

This has offline parser/filesystem/mock coverage only. A checksum/archive test
is **not proof of a real cloud restore**. The first plan must confirm this lab's
actual mount classification, systemd/container definitions, backing chains,
source image cache, SSH and capacity. A separately authorized lab checkpoint/
restore rehearsal must establish service startup, Nova/libvirt recovery,
OVS kernel reset, DHCP/metadata and application behavior before relying on it.
