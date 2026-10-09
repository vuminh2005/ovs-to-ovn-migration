# Existing EW application measurement

This extends the supplied runner; it uses the existing application's task IDs,
PostgreSQL outbox, retry semantics, result SHA-256 and process_count checks.
Application setup and service restart are separate prerequisites, never migration
hooks. No new probe guest, provider network or floating IP is created.

## Lifecycle and commands

Configure trusted existing guest/host SSH access in the repository variables, as
shown in the main README. Start a finite source baseline with:

```bash
ansible-playbook -i /root/multinode ew-baseline.yml -e @/path/to/ew-access.yml
```

The printed run directory contains catalog, effective MTU inputs and measurement
configuration. Full migration uses the usual entrypoint with that access variables
file and ew_workloads_enabled=true. Once a run is configured, explicit operations
are available (source the existing OpenStack admin openrc first):

```bash
python3 scripts/ew_workload.py start RUN_DIRECTORY
python3 scripts/ew_workload.py status RUN_DIRECTORY --transport-phase source
python3 scripts/ew_workload.py ready RUN_DIRECTORY
python3 scripts/ew_workload.py recovery RUN_DIRECTORY
python3 scripts/ew_workload.py stop RUN_DIRECTORY --transport-phase ovn
python3 scripts/ew_workload.py drain RUN_DIRECTORY --transport-phase ovn
python3 scripts/ew_workload.py collect RUN_DIRECTORY --transport-phase ovn
python3 scripts/ew_workload.py report RUN_DIRECTORY
```

`--mode baseline` selects an existing finite baseline session; `baseline` performs
start/wait/stop/drain/collect in one operation. start-baseline.sh and
collect-baseline.sh take an already-configured run directory. ssh.sh requires
EW_RUN_DIR and `ew_ssh <catalog-VM-name> <source|ovn> <explicit-command...>`;
it contains no hard-coded router UUID, private key or host address.

Each guest checkpoints immutable config and START_REQUESTED before systemd-run.
A global per-guest slot prevents overlapping runs. Unit names are scoped by run
and actor; PID/start identity and boot are checked. No Restart policy is enabled.
Crash/ambiguous launch evidence blocks another launch. Controller startup first
resolves *all* previous intents before launching a missing sibling. An interrupted
start cannot silently add another runner. Completed sessions are immutable and
reused; obtain a new discovered run directory for a new baseline.

Default baseline duration is 120 s; migration runs have no finite active-duration
limit but a 3600 s unattended lifetime. Both allow 30 s in-flight draining.
TimeoutStopSec is drain+30 s; RuntimeMaxSec is lifetime+drain+30 s. Durable stop
requests and SIGTERM stop new tasks and bound the last task; they never stop the
application or reboot a guest. Runners continue without controller SSH or Neutron
API availability. They intentionally do not resume after a guest reboot. A startup
retry retains the first completed coverage checkpoint and all raw guest events.

Default transport calls are bounded at 20 s, startup/readiness/recovery stages at
120 s, recovery observation at 30 s, and stop/drain/collection at a shared 300 s
budget (plus at most 10 s to finalize the controller API observer). Per-guest
collection has a 64 MiB byte limit. Partial files/failed collection remain honest
incomplete evidence; guest raw data is never deleted by this lifecycle.

## Independent measurements

Three client->app flows cover same-L2/same-compute, same-L2/different-compute and
routed/different-compute paths. ew-app probes queue and DB independently, covering
routed/same-compute and routed/different-compute dependencies. A source centralized
OVS router can forward a same-compute routed flow on a network node; placement is
not a claim that all forwarding remains local.

Each flow records source/destination IP, exact ports and actual placement, and:

- Small ICMP (56-byte payload), independent of task submission.
- Target-safe IPv4 DF probe: per-network target MTU minus 28, here 1392-28=1364.
- Separately labelled source-boundary DF probe: here 1400-28=1372. Expected failures
  after MTU reduction are diagnostics, not general connectivity-loss failures.
- Independent TCP connects to API 8080, queue 5672 and DB 5432 as appropriate.
- Independent HTTP /live and dependency /health sampling; /health is never used
  as proof that a worker processes jobs.
- E2E tasks on each client, same ID/input on uncertain acceptance, result integrity,
  process_count=1, per-run server reconciliation and nearest-rank latency percentiles.

The offered load is closed loop, one in-flight job per client, with a default
1 job/s ceiling. Slow or unavailable dependencies reduce attempted load. Reports
include attempted/accepted/completed task rates (attempted means unique task intents,
not HTTP retries), HTTP attempts/failures, SLO misses,
unresolved jobs and corruption/repeated processing. Redeliveries are distinct from
repeat processing; unresolved means unconfirmed, not a proven permanently lost job.

Durations and sampled outages use one process's monotonic clock. UTC aligns logs
only; clocks from different guests/controller are not subtracted for exact outage.
An outage runs from a failed sample to its following successful sample; an open
final failure window has no invented recovery duration. These sampled metrics do
not replace the authoritative Pair-A compute-PCAP packet metric.

A controller-local observer independently attempts bounded Neutron network reads
at a 2 s cadence, with a 6 s child-process request bound. API/auth errors, sample
timeouts and process crashes are recorded without credentials. Its availability
windows remain distinct from the orchestration freeze timer.

## Evidence and acceptance

New runtime.json files declare ew_measurement_schema_version=1 and the enabled
flag; an enabled run missing its measurement config reports UNAVAILABLE. Old runs
without EW metadata remain NOT TESTED. ew-action-errors.json retains sanitized
orchestration failures without SDK error bodies or tokens.

Controller ew-lifecycle.json records sessions, all six boots/identities, runner
launch intent, readiness attempts/task UUIDs, recovery sequence fences and transport
/collection states. ew/baseline or ew/migration holds per-actor config, state,
events.jsonl, summary.json, diagnostics and controller API observer raw events.
Guest files remain under /var/lib/ew-load/RUN_ID. Relevant phase markers remain in
metrics/. No key, OpenStack token, app.env, password file or arbitrary archive is
collected. Diagnostics use the guest-local run time window, with bounded journal
output and allowlisted API/worker events; truncated/failed diagnostics are reported.

Coverage requires complete run endpoints, contiguous event sequence, finite ordered
monotonic stamps, matching boot/config identity and sufficiently continuous samples
for every expected stream. A crash, missing events or an unexplained sample gap is
UNAVAILABLE, never an inferred packet/application success. A measured failure with
complete coverage remains a failure window. Recovery requires consecutive fresh
successes fenced by guest event sequence, never by unrelated clocks.

Baseline acceptance requires reconciled tasks and zero required probe, HTTP,
integrity, unresolved or SLO failures (including the source-boundary probe).
Migration acceptance permits reported transient outages/SLO misses but requires
all tasks reconciled with no corruption/repeat processing/unresolved results,
target-safe probes and dependencies stably recovered, six preserved guest identities
and target MTUs, successful fresh E2E readiness and complete collection/API coverage.
Missing evidence never produces PASS. Collection failure is not SSH-derived
application downtime. Global migration result becomes MIGRATED_VALIDATION_INCOMPLETE
when enabled EW evidence does not pass; Pair-A packet fields remain independent.

## Access and remaining lab checks

Source access discovers qrouter-<catalog-router-UUID> on inventory hosts. Post-OVN
access discovers ovnmeta-<exact-network-UUID>, checks unique distributed-port MAC/IP
for the guest subnet and namespace SSH connectivity. Every operation verifies actual
guest server UUID, port MAC/fixed IP and checkpointed boot. Explicit existing direct
access is optional. No namespace/interface/port is created or changed for transport.
No guest agent is assumed; UUID verification uses readable DMI under existing sudo.

If EW guests retain MTU 1400 after network preparation, phase 07 blocks freeze.
Arrange guest DHCP/MTU readiness separately; this automation never modifies EW
networking, reboots/rebuilds/deletes EW VMs, or runs their application setup script.
Lab verification is still needed for namespace SSH, guest identity, privileges,
real worker reconciliation, guest MTU convergence, journal visibility, collector
interruptions and measurement budgets. Offline tests do not validate Step 5 on a
real cloud or establish migration readiness.

The additional migration-only two-port echo experiment shares the existing
ew-app/ew-client-b measurement units and raw stream, but has its own report and
activation/sequence fences. It does not change application acceptance or Pair-A
metrics. See the main README's dedicated TCP experiment and same-run MTU
preparation workflow; prepare both configured ports' access before migration.
