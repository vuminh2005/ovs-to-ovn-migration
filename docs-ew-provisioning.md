# Work Item 1 (Việc 1): persistent East–West provisioning

This is a separate extension, not another numbered migration phase. The intended
workflow is **separately authorized reset to OVS → provision EW → baseline →
migration → report**. This change does not implement/reset the lab, add external
connectivity, change Pair A/B/C, or extend checkpoint tooling.

## Source audit and reconstructed bootstrap

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
workspace. Retain the image, checksum, reviewed adapter/application sources,
private inputs and SSH trust **outside every directory/volume removed by a lab reset**.
The provisioner refuses an image or persistent state under the migration backup
root; the operator must also check any separately authorized reset policy.

`workloads/ew-workload-app/setup.sh` deploys only the API/worker. Its `common.py`
initialization executes CREATE TABLE IF NOT EXISTS and CREATE INDEX IF NOT
EXISTS against database `ewlab` as `ewapp`; it preserves existing rows. Neither
script creates the PostgreSQL database/role/listening configuration or RabbitMQ
user/vhost/server configuration. The original RabbitMQ and PostgreSQL bootstrap
commands **have not yet been located**. They are no longer a mandatory blocker:
`workloads/ew-bootstrap/adapter.py` implements two **reconstructed, reviewed**
adapters from the supplied `ew-bootstrap-live-evidence.json` (2026-10-10).
These are not recovered original scripts. The operator reports successful
bootstrap checks, credential recovery, inspect, application verification and two
apply runs on the existing controller/lab. The second apply returned
`changed=false`, preserving resource UUIDs, all six guest/boot identities and the
three original task receipts. This audit did not repeat those live checks.
Fresh provisioning from an empty cloud remains unverified; offline regressions
do not prove that path. The prepared QCOW2's dependencies, including the pinned
PostgreSQL/RabbitMQ packages, were confirmed by the operator. `apply`
requires valid private password inputs and SHA256-pinned adapter sources before
cloud mutation; `inspect` and existing-application `verify` do not require them.

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

## Reconstructed reviewed bootstrap adapters

Defaults in `ew_provision_bootstrap_sources` identify both roles with the actual
`{{ playbook_dir }}/workloads/ew-bootstrap/adapter.py` path, pinned SHA256 and
`interpreter: python3`. The same audited file contains separate PostgreSQL and
RabbitMQ adapters. The interface remains `check` / `apply`; Python receives the
exact `ew-db` or `ew-queue` role as its second argument. Reviewed external bash
adapters retain the historical single-action/exit-zero interface.

`check` is read-only and emits only JSON. PASS means the observed dependency,
configuration, identity/permission and password-authentication checks passed.
CHANGE_REQUIRED explicitly lists missing owned configuration/resources or stopped
services (catalog inspection is marked pending when stopped). CONFLICT refuses
incompatible configuration/permissions/credentials; MISSING_DEPENDENCIES and
UNAVAILABLE never count as readiness. No install, write, service start/reload or
lock/journal creation occurs in check mode. Apply is separately authorized:

- PostgreSQL requires the baked 16.15-0ubuntu0.24.04.1 server package, runtime
  16.15, Ubuntu cluster tools and psql client. The adapter uses Python standard-library
  code; psycopg2 is required on ew-app by the existing application, not added as
  an extra requirement on ew-db. It manages only
  16/main/5432, `listen_addresses=127.0.0.1,192.168.102.12`, SCRAM, the observed
  Ubuntu snakeoil SSL paths, and the exact workload HBA rule for ewlab/ewapp from
  192.168.101.11/32. All seven standard local/loopback/replication rules remain
  intact. Missing ewapp is created LOGIN/INHERIT without elevated privileges;
  missing ewlab is UTF8/C.UTF-8 owned by ewapp. A wholly absent cluster can be
  created by the baked Ubuntu tool only if no other cluster and neither data nor
  config directory exists. Both local-peer and host-SCRAM initdb authentication
  methods are explicit; the standard admin-peer rule is finalized before start.
  A separate durable cluster intent permits interrupted finalization without
  repeating initdb on existing data. Partial/inconsistent cluster remnants are refused.
  Existing roles/memberships, credentials, DB/public-schema ACL/ownership and
  application object ownership must match. PostgreSQL 16's pg_database_owner
  public schema is retained. No application tables/indexes are created here.
- RabbitMQ requires baked package 3.12.1-1ubuntu1.6, runtime 3.12.1, its existing
  CLI tools. Pika remains an application dependency on ew-app. It manages only the missing AMQP listener
  192.168.102.11:5672, ewapp (no tags), ewlab (tracing false), and its .* / .* / .*
  permissions. It preserves guest/administrator and loopback restriction,
  internal PLAIN/AMQPLAIN auth, no TLS listener and no enabled plugins. Other
  listener/auth settings, existing workload permissions/tags or credentials
  conflict. It neither declares application exchanges/queues nor inspects,
  consumes, publishes or purges messages.

The prepared image must contain these dependencies; provisioning never downloads,
installs or upgrades packages. A stock netfix guest lacking DB/broker packages
fails explicitly: prepare/review an appropriate baked image separately. Version
pins intentionally refuse unreviewed updates instead of silently upgrading.

Authentication is checked without credentials in argv/logs: PostgreSQL uses a
private stdin payload to a local peer-authenticated Python helper, then psql
opens a real read-only password-authenticated TCP connection. The helper supplies
PGPASSWORD only in that child process environment (as the established app uses
its own private environment); never in argv, SQL or logs. Missing roles receive a SCRAM
verifier inside the helper; existing passwords are never changed. RabbitMQ
`add_user ewapp` / `authenticate_user ewapp` consume a private stdin pipe, as
supported by the pinned [3.12.1 add-user source](https://github.com/rabbitmq/rabbitmq-server/blob/v3.12.1/deps/rabbitmq_cli/lib/rabbitmq/cli/ctl/commands/add_user_command.ex)
and [authentication source](https://github.com/rabbitmq/rabbitmq-server/blob/v3.12.1/deps/rabbitmq_cli/lib/rabbitmq/cli/ctl/commands/authenticate_user_command.ex).
PostgreSQL's verifier behavior follows [CREATE ROLE](https://www.postgresql.org/docs/16/sql-createrole.html).
Failed subprocess bodies and authentication hashes are never returned.

Configuration restart intent is root-private and durable before file changes
(`/var/lib/ew-provision/postgresql16.json` or `rabbitmq312.json`). A failed restart
remains pending; retries revalidate and finish it. Logical resource creation is
atomic per role/user/database/vhost operation, so interruption is recovered by
reinspection, without recreating or replacing completed resources. No response
from a restart is ambiguous and may require repeating that pending restart;
a completed unchanged healthy run rewrites nothing and restarts nothing.
Do not edit pending journals or run concurrent administrative changes.

## Private controller credential recovery and validation batch

`ew-recover-credentials.yml` is prepared for a separately authorized invocation.
It uses only existing verified namespace SSH, reads no original source secrets
from the reviewed JSON (none were collected), and installs nothing in guests.
It verifies current server/port/IP, MAC, placement and reviewed guest boot IDs,
reads only root:root 0600 regular password files, validates 48 lowercase hex,
privately compares ew-app/db against ew-db and ew-app/mq against ew-queue, then
rechecks guest identity/boot. No mismatch is adopted. If a legitimate reboot
occurred since collection, obtain/review a fresh reference; do not weaken boot
validation to bypass this refusal.

Matching values are atomically published without replacement under
`/root/ew-private` (0700), with files root:root 0600:
`db-password`, `mq-password`, `recovery-evidence.json` and
`ew-provision-inputs.yml`. The latter maps `ew_provision_secret_files.db/mq` to
those paths, containing no inline passwords. Existing different, invalid,
symlinked or non-private outputs are refused; equivalent normalized values retain
bytes/mtime/permissions. A crash between atomic link publication and temporary
file removal can leave a private temporary hard link; matching final files remain
retryable and the helper never deletes unrelated temporary files. A partial local save can retry with the same reviewed
sources; already matching files remain untouched. Passwords travel only through
encrypted SSH stdout into private controller memory; nothing prints passwords
or credential hashes, and the Ansible task has no_log enabled.

The current `reset-lab-to-ovs.yml` destroys Kolla containers/data, removes
`/etc/kolla/config` and optionally `/root/ovs-to-ovn-backup`; it does not remove
`/root/ew-private`. This survival statement assumes its default deletion paths.
Review any reset overrides/custom scripts and separately back up these files.
The helper rejects repository/SSH/backup destinations. Do not store private inputs
inside this repository, the migration backup tree or guest/Kolla volumes.
Controller access IP **117.1.28.69** is a management address, never a workload
endpoint. Run the following one complete batch in an already established
controller session only after review/authorization. It is also provided as
`workloads/ew-bootstrap/controller-validation.sh`; it was **not executed** here:

```bash
set -euo pipefail
source /root/venvs/kolla-2024.1/bin/activate
cd /root/ovs-to-ovn-migration
test -f /root/ew-bootstrap-live-evidence.json
ansible-playbook -i /root/multinode ew-bootstrap-check.yml --syntax-check
ansible-playbook -i /root/multinode ew-recover-credentials.yml --syntax-check
ansible-playbook -i /root/multinode ew-bootstrap-check.yml -e @/root/ew-access.yml
ansible-playbook -i /root/multinode ew-recover-credentials.yml -e @/root/ew-access.yml
ansible-playbook -i /root/multinode ew-provision.yml \
  -e @/root/ew-access.yml -e @/root/ew-private/ew-provision-inputs.yml \
  -e ew_provision_action=inspect
```

Place the reviewed evidence at `/root/ew-bootstrap-live-evidence.json` or override
`ew_bootstrap_reference_file`. Check failure stops the batch; inspect its private
`ew-bootstrap-check.json` evidence first. The batch reads guests/APIs and saves
private controller files only: no service changes, application deployment,
baseline, reset or migration. Review the result before any later apply. Actual
fresh provisioning remains an unexecuted lab test; the operator has since reported
the existing-lab no-op second apply described above.

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
commands. `original_bootstrap_sources` stays UNRESOLVED and the historical collector field `adapters_ready` stays
false; it describes collection, not acceptance of the new reconstructed adapters. Preserve the private evidence and
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
password inputs match the current services without disclosing values. The supplied reviewed collection now supports the reconstructed adapters above.
Missing/omitted fields still need private review if used; collection alone is not
proof that apply was executed or that the baked image contains its dependencies.

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
application verification can be run without recovering private bootstrap inputs;
it creates three smoke-test tasks but does not install files/restart services:

```bash
ansible-playbook -i /root/multinode ew-provision.yml \
  -e @/root/ew-access.yml -e ew_provision_action=verify
```

Only after the reconstructed adapters, private inputs and check results are reviewed and apply is separately authorized:

```bash
ansible-playbook -i /root/multinode ew-provision.yml \
  -e @/root/ew-access.yml -e @/root/ew-private/ew-provision-inputs.yml \
  -e ew_provision_action=apply
cp /var/lib/ovs-to-ovn-ew-provisioning/resources.json /root/ew-provision-before-second.json
cp /var/lib/ovs-to-ovn-ew-provisioning/readiness.json /root/ew-readiness-before-second.json
ansible-playbook -i /root/multinode ew-provision.yml \
  -e @/root/ew-access.yml -e @/root/ew-private/ew-provision-inputs.yml \
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
first provisioning/verification run. For the reconstructed adapters, review their data-preservation behavior and perform a
lab-specific existing-row/queue audit; mocks cannot establish preservation of
all application data.

The overall workflow is explicitly separate from independent provisioning:

```bash
ansible-playbook -i /root/multinode ew-migrate.yml \
  -e @/root/ew-access.yml -e @/root/ew-private/ew-provision-inputs.yml
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
cloud-init/host-key output, Glance content/properties, namespace access, reconstructed
DB/broker adapters, credential recovery, no-restart reruns and data preservation
still require lab verification. Original scripts remain unresolved; reviewed
reconstructed adapters are accepted. Missing baked dependencies or private inputs
remain real blockers. No end-to-end lab provisioning success is claimed.
