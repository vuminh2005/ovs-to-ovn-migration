# Work Item 1 (Việc 1): persistent East–West provisioning

This is a separate extension, not another numbered migration phase. The intended
workflow is **separately authorized reset to OVS → provision EW → baseline →
migration → report**. This change does not implement/reset the lab, add external
connectivity, change Pair A/B/C, or extend checkpoint tooling.

## Source audit and current limitation

The six VM names/IPs/compute placements and endpoints come from
`ew_workload_config`. The user confirmed these additional original creation-log
values and subsequently verified the image SHA256, flavor and security group:

| Resource | Required configuration |
| --- | --- |
| ew-net-a / ew-subnet-a | VXLAN, 192.168.101.0/24, gateway 192.168.101.1, pool 192.168.101.100–199 |
| ew-net-b / ew-subnet-b | VXLAN, 192.168.102.0/24, gateway 192.168.102.1, pool 192.168.102.100–199 |
| Both subnets | DHCP enabled; empty DNS nameservers and host routes |
| ew-router | Centralized, non-HA; both interfaces; no external gateway |
| ew.2c2g | 2 vCPU, 2048 MiB, 10 GiB root, 0 ephemeral/swap; public/enabled; no extra specs |
| ew-sg | IPv4 ICMP and unrestricted TCP/UDP from both EW /24s; unrestricted IPv4/IPv6 egress |
| ew-key | Reuse matching keypair or create from /root/.ssh/ew-lab.pub after verifying it matches the private access key |
| Placement | Nova API 2.74 explicit host, AZ nova, config drive enabled |

Fixed workload IPs `.11/.12/.13` remain outside the dynamic pools. Empty TCP/UDP
port ranges are unrestricted and satisfy the checks; a matching reused security
group receives **no duplicate/narrower rules**. Inspect records its actual rules
so subsequent changes can be reviewed. Missing required permissions on an
existing group are a conflict, not an instruction to widen that group.

The prepared source is `/root/ew-image-build/ew-ubuntu-24.04-netfix.qcow2`, SHA256
`822f3f2e3c988c7560609cdf0469f708ff3f7c6ab678d3268c8809b8b93501a8`.
Its `.sha256` sidecar must agree. Glance requires the same content, qcow2/bare,
private visibility, min_disk 10, min_ram 2048, os_distro ubuntu, os_version 24.04.
These controller paths are configurable; they were not accessed from the coding
workspace. Retain the image, checksum, private inputs, SSH trust and original
bootstrap sources **outside every directory/volume removed by a lab reset**.
The provisioner refuses an image or persistent state under the migration backup
root; the operator must also check any separately authorized reset policy.

`workloads/ew-workload-app/setup.sh` deploys only the API/worker. Its `common.py`
initialization executes CREATE TABLE IF NOT EXISTS and CREATE INDEX IF NOT
EXISTS against database `ewlab` as `ewapp`; it preserves existing rows. Neither
script creates the PostgreSQL database/role/listening configuration or RabbitMQ
user/vhost/server configuration. The original RabbitMQ and PostgreSQL bootstrap
commands **have not yet been located**. A failed controller `rg` search was not
evidence of absence. Empty-cloud application provisioning is therefore not yet
complete or lab-validated. `apply` refuses before cloud mutation until audited
bootstrap adapters and original private password-file paths are provided.
Resource `inspect` and existing-application `verify` do not require those pending
bootstrap inputs.

## Inputs and behavior

Use the existing private `/root/ew-access.yml` mechanism for guest key/user,
guest known-hosts, host known-hosts and inventory SSH access. Its contents must
remain private and outside Git. The coding environment did not contain this
controller file; no credentials were read or logged. Required existing host
tools are Python/OpenStackSDK, OpenStack CLI, qemu-img, ssh/ssh-keygen, and the
repository's ordinary Kolla precheck dependencies. Guests must already contain
cloud-init, Python, sudo, ping and the netfix dependencies; ew-app also needs
gunicorn, psycopg2 and pika. No guest packages are installed or downloaded.

`ew-provision.yml` reuses bootstrap and read-only source prechecks for scope,
controller configuration and all chassis' actual tunnel-interface MTUs. It
defers only discovery of EW resources that may not exist yet. The current
1450 underlay calculates VXLAN **1400**, target Geneve **1392**; provisioning
uses the calculated **source** MTU and never sets guest/network MTU to 1392
early. Existing phase-06/manual preparation and final pre-freeze gates remain.

Before creating resources it checks the entire resource plan for duplicate
names, image content/properties, flavor, keypair/private-key correspondence,
network/subnet/router scope, SG permission, fixed IPs, explicit ports, config
drive and placement. Conflicts fail with the resource/property; no automatic
replacement/deletion. Ports and server UUIDs are saved immediately, before
Nova ACTIVE waits. A missing checkpointed UUID is a refusal, not a recreation.
A local lock prevents concurrent invocations sharing the provisioning state.
After a separately authorized destructive reset, archive the old provisioning
state and explicitly choose a new `ew_provision_state_dir` for the rebuilt cloud;
do not reuse/erase historical UUID evidence to hide missing resources. Automatic
reset/state rotation is outside this Work Item.

SSH uses the existing verified `qrouter-<exact UUID>` namespace transport, without
FIP or uplink. Existing guests need already trusted host keys. For a genuinely
new checkpointed server only, a missing guest trust entry can be populated from
cloud-init's public SSH host-key block returned by the authenticated Nova API
for that exact UUID. Existing entries are never replaced. Truncated/missing
console keys fail; supply independently verified keys rather than disabling SSH
verification. Guest DMI UUID, Neutron MAC/IP, source MTU, interface/routes and
boot continuity are verified before deployment; cloud-init must be cleanly done.

Persistent identity/boot state is `/var/lib/ovs-to-ovn-ew-provisioning` by default,
separate from any migration run and from `validation-resources.json`.
Per-invocation evidence is under the displayed run directory:
`ew-provision-plan.json` (including SG rules), `ew-resources.json` and, after
successful checks, `ew-provision-readiness.json`. These EW resources retain
`external-existing-never-validation-owned` catalog semantics and survive
migration validation cleanup. Reusable images/flavors remain retained.

Application code and systemd units are taken directly from the checked-in
authoritative files. The provisioner compares contents, writes atomically only
when changed, generates the original root-private app.env inside the guest and
refuses credential differences. It does **not** invoke setup.sh unconditionally.
Service handlers reload units only when changed; restart only the affected API
or worker, or both for shared code/environment changes; start inactive services
and enable missing boot activation. Unchanged active services are not restarted.
The controller records `application_deployment_pending` in persistent
`resources.json` **before** environment/code/unit writes. It binds pending work
to the exact server/port/IP and desired source digest, and tracks initialization,
daemon reload and each service handler separately. Successful steps are
checkpointed separately. If a response is lost, an uncertain step can repeat
(initialization remains CREATE IF NOT EXISTS); an acknowledged completed service
handler is not repeated just because another handler failed. Changed source or
identity during unfinished work is refused. Pending state is removed only after
the full three-client task, ICMP and TCP readiness checks and readiness evidence
persistence, never merely because installed files match or services are active.
Retry the same apply command with the same inputs/state directory; do not edit
the pending journal. A later completed, unchanged healthy apply remains a no-op
for application files and restarts.

Credential comparison uses the authoritative `.read_text().strip()` semantics
and validates 48 lowercase hex characters. Matching effective values preserve
the original bytes (including a trailing newline), private permissions and mtime.
Invalid, non-private or genuinely different files remain failures.

For interrupted provisioning, an empty port attachment is permitted in planning
only when **both** the BUILD server UUID and port UUID are already checkpointed.
Apply waits within one Nova/Neutron timeout and rechecks exact IDs/fixed IP,
ACTIVE state, host/zone/image/flavor/keypair/config drive and the final unique
ACTIVE attachment. Nonempty foreign attachment, missing identity or timeout
fails without rebind/recreation/deletion; all resources remain available for
investigation.

The original `common.initialize()` runs on deployment changes only. Measurement
helpers are copied without starting measurement processes. Each client executes
the original `probe.py` via verified SSH: dependency checks, committed task,
worker/hash/process_count, same-ID retry, conflict response and run stats; actual
ICMP and TCP checks are also required. These smoke probes append isolated tasks;
they do not reset/purge existing jobs or queues. The first successful probe's
completed task receipt from each client is retained in persistent state; later
runs GET and compare these original IDs/results before and after deployment.

The mentor TCP experiment remains separate: provisioning permits both configured
ports (defaults 18080/18081) through the original unrestricted tenant rules,
but starts **neither** listener. Existing phase-04 measurement starts 18080;
the existing post-freeze source-transport hook activates 18081. No package or
application deployment is imported into the downtime interval.

## Pending bootstrap input contract

Do not invent new initialization commands. After the original sources are
located/audited, supply two private local file paths in `ew_provision_secret_files`
(`db` and `mq`), retaining the existing 48 lowercase hex passwords. For a fresh
deployment, obtain credentials through that same separately managed private
mechanism. This tool does not generate/rotate credentials. Missing guest password
files may be installed from those inputs; existing differing files are refused.

`ew_provision_bootstrap_sources` must identify exactly `ew-db` and `ew-queue`,
each with `path` and its actual `sha256`. These are adapters around the original
audited sources, not replacement implementations. They run as root on only the
matching verified guest and receive `check` or `apply` as the first argument.
`check` must be read-only, exit zero and emit only JSON
`{"status":"PASS"}` or `{"status":"CHANGE_REQUIRED"}`. `apply` must preserve
existing credentials/database/queue contents and change only missing/mismatched
owned configuration. A nonzero exit is failure. The subsequent `check` must
PASS. Neither mode may print credentials. Refuse conflicting existing DB/broker
configuration rather than overwriting it. The check must cover dependency
installation, service/listen/auth configuration and the expected role/database
or user/vhost permissions. Application task completion is then checked separately.

Provide these dictionaries in a private extra-vars file outside the repository,
for example `/root/ew-provision-inputs.yml`, **after their real paths and hashes
are established**. No runnable bootstrap implementation or guessed paths are
included. `ew_provision_spec_file` optionally selects a complete JSON specification
instead of the defaults, including these dictionaries. Other configurable inputs
are `ew_provision_action` (inspect/apply/verify), `ew_provision_timeout_seconds`,
`ew_provision_state_dir`, `ew_provision_availability_zone`, and `ew_provision_spec`.

## One read-only controller collection batch

Run this only after reviewing the code and authorizing read-only lab access.
These commands were **not executed** during development:

```bash
set -euo pipefail
source /root/venvs/kolla-2024.1/bin/activate
cd /root/ovs-to-ovn-migration
ansible-playbook -i /root/multinode ew-collect-bootstrap.yml \
  -e @/root/ew-access.yml
```

This allocates a private controller evidence directory and reports its exact path.
It reuses `ew-config-tasks.yml`, exact-name/UUID discovery and trusted namespace
SSH, with guest DMI UUID/IP/MAC checks before reading. It installs no guest helper,
does not run baseline, provisioning, migration or readiness smoke tasks, and
changes no cloud resources, guest files, services, data or credentials. For
current VXLAN it uses the verified qrouter path; for already migrated Geneve it
uses the existing verified OVN metadata transport, without changing networking.

`ew-bootstrap-live-evidence.json` contains:

- PostgreSQL server version; effective listen/port/SSL/config paths and setting
  provenance; parsed HBA rule fields; ewapp role attributes/memberships; ewlab
  ownership/ACL and CONNECT/CREATE/TEMP permissions; public schema/table ownership,
  grants and default privileges. SQL executes in explicit read-only transactions
  using the existing local postgres account. It never reads authentication hashes
  or table rows. HBA options and raw parse-error messages are omitted because
  they may contain external-auth credentials.
  Cluster inventory and package versions are collected when their tools exist.
- RabbitMQ server version/listeners, users/tags, vhosts, ewapp/vhost/topic
  permissions and a whitelist of effective listener/auth-backend/mechanism/loopback
  settings. It never exports definitions, reads password hashes/cookies, or lists
  queue messages. Failed-command stdout/stderr is discarded because RabbitMQ
  diagnostics can expose cookie hashes; failure remains explicit UNAVAILABLE.
  Enabled plugin names and the installed RabbitMQ package version are also collected.
- Guest `/etc/ew-lab/db-password`, `mq-password` and `app.env` paths, ownership
  and permissions, without their contents; systemd state and configuration/drop-in
  paths, without environment values; RabbitMQ candidate config paths/permissions.
  ew-app dependency versions are read without running the application.
- A bounded controller `/root` search listing source candidate paths/permissions
  only (no `rg` dependency, history or file-content search), including an explicit
  truncation/unreadable-path indication. Private access-file contents are not printed.

The output is **recovered live configuration**, not proof of the original bootstrap
commands. `original_bootstrap_sources` stays UNRESOLVED and `adapters_ready` stays
false; no adapter is generated automatically. Preserve the private evidence and
inspect candidate original sources locally without pasting credentials. A missing
tool, stopped/unreachable dependency, unsupported diagnostic command or permission
failure remains UNAVAILABLE; do not start/install anything just to complete collection.

If original scripts cannot be recovered, reconstruction requires review of the
actual collected version, effective listen/auth settings and active config paths,
the exact ewapp/ewlab ownership/permissions on PostgreSQL and RabbitMQ, service
configuration/enablement, and the installed dependency/package provenance of the
netfix image. Any omitted external-auth options, TLS/private-key material, cluster
units, plugin/auth-backend configuration and config-file ordering must be reviewed
privately if the observed deployment uses them. Confirm that the existing private
password inputs match the current services without disclosing values. Only then
can compatible read-only `check` / idempotent `apply` adapters be authored and
reviewed against those observed settings; collection alone is not complete bootstrap.

## Exact controller commands (not executed during development)

Inspect the current lab first; this performs cloud reads and source prechecks,
writes local evidence only, and does not deploy or start measurements:

```bash
source /root/venvs/kolla-2024.1/bin/activate
cd /root/ovs-to-ovn-migration
ansible-playbook -i /root/multinode ew-provision.yml \
  -e @/root/ew-access.yml -e ew_provision_action=inspect
```

Review the displayed run directory's `ew-provision-plan.json`, including current
SG rules and `missing`. Review code/diff before authorizing apply. Existing
application verification can be run without the pending bootstrap sources;
it creates three smoke-test tasks but does not install files/restart services:

```bash
ansible-playbook -i /root/multinode ew-provision.yml \
  -e @/root/ew-access.yml -e ew_provision_action=verify
```

Only after the pending original bootstrap/private inputs are supplied and reviewed:

```bash
ansible-playbook -i /root/multinode ew-provision.yml \
  -e @/root/ew-access.yml -e @/root/ew-provision-inputs.yml \
  -e ew_provision_action=apply
cp /var/lib/ovs-to-ovn-ew-provisioning/resources.json /root/ew-provision-before-second.json
cp /var/lib/ovs-to-ovn-ew-provisioning/readiness.json /root/ew-readiness-before-second.json
ansible-playbook -i /root/multinode ew-provision.yml \
  -e @/root/ew-access.yml -e @/root/ew-provision-inputs.yml \
  -e ew_provision_action=apply
ansible-playbook -i /root/multinode ew-provision.yml \
  -e @/root/ew-access.yml -e ew_provision_action=inspect
python3 - <<'PY'
import json, pathlib
base = pathlib.Path('/var/lib/ovs-to-ovn-ew-provisioning')
before = json.loads(pathlib.Path('/root/ew-provision-before-second.json').read_text())
after = json.loads((base/'resources.json').read_text())
assert before['resources'] == after['resources'], 'UUIDs changed'
assert before['guests'] == after['guests'], 'Guest UUID/port/IP/boot changed'
assert before['preserved_tasks'] == after['preserved_tasks'], 'Original task receipts changed'
assert len([k for k in after['resources'] if k.startswith('server:')]) == 6
ready = json.loads((base/'readiness.json').read_text())
assert ready['status'] == 'PASS' and len(ready['tasks']) == 3
assert ready['preserved_tasks']['status'] == 'PASS'
assert ready['application_deployment']['changed_files'] == []
assert ready['application_deployment']['service_actions'] == []
assert ready['application_deployment']['environment_changed'] is False
print('Six unchanged server identities; no application rewrite/restart; task checks PASS')
PY
```

The inspect step also refuses duplicate resource names/IP ownership and changed
checkpointed identities. Smoke evidence proves new tasks, same-ID retries and
preserved results for the original three checkpointed smoke tasks; it is not a
complete database-content audit or proof about tasks that existed before the
first provisioning/verification run. Once the original DB bootstrap
sources are supplied, review their data-preservation behavior and perform a
lab-specific existing-row/queue audit; mocks cannot establish preservation of
all application data.

The overall workflow is explicitly separate from independent provisioning:

```bash
ansible-playbook -i /root/multinode ew-migrate.yml \
  -e @/root/ew-access.yml -e @/root/ew-provision-inputs.yml
```

That command **runs migration** and is for later separate authorization, not a
provisioning test. It runs apply/readiness, then the finite source baseline,
then imports the unchanged numbered migration entrypoint. Failure at provisioning
or baseline prevents migration. No reset is included. Do not use
`migrate-to-ovn.yml` directly expecting automatic provisioning; its existing
behavior is preserved. Standalone source `verify` is intentionally not post-OVN
validation; after migration the existing phase-12 checks are authoritative.

## Offline validation limits

Tests use an in-memory cloud and mocked guest transport for empty-cloud creation,
matching reuse, UUID checkpointing, conflicts, readiness and entrypoint ordering.
Atomic writes/credential refusal and environment generation are also exercised
against temporary local files. No OpenStack API, lab SSH, service changes, reset
or migration ran in the coding environment. Real Nova 2.74 placement, netfix
cloud-init/host-key output, Glance content/properties, namespace access, original
DB/broker adapters, no-restart reruns and data preservation still require lab
verification. End-to-end empty-cloud provisioning remains blocked by the pending
authoritative bootstrap inputs, rather than reported as complete.
