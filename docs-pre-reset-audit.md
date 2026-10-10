# Pre-reset audit: Work Item 1 and the existing migration

Audit date: 2026-10-10. Inspected branch: `main`; starting HEAD:
`61b0f38cd8f059b0dc5f3cc9fc576e732a9e8e69`. The index and working tree were
clean. No `AGENTS.md` was present in the repository or its ancestor directories.
This pass makes no reset, cloud, guest, service, measurement or checkpoint-schema
changes. It does not push. The review diff and cleanup commit are reported in the
handover, outside runtime evidence.

## Evidence boundary and extension plan

The operator reports that controller bootstrap checks, private credential
recovery, inspect, application verification and two existing-lab apply runs
passed. The second apply reported `changed=false`; resource UUIDs, six guest and
boot identities, and three original task receipts were preserved. The operator
also confirms the prepared QCOW2 contains the required pinned packages. These
statements were supplied for this audit, not independently recollected here.

Empty-cloud provisioning remains unverified. PostgreSQL/RabbitMQ adapters are
reviewed reconstructed implementations; the original bootstrap scripts remain
unresolved and are not a prerequisite for accepting the reviewed adapters.
Passing offline tests does not prove fresh provisioning, reset, migration,
post-OVN SSH, or application recovery on the lab.

Extensions are **Work Items (Việc)**, separate from canonical migration phases
00–13 and historical documentation's “Step 5” EW implementation label:

1. EW provisioning.
2. Reset to OVS and integrate provisioning.
3. Extend migration and measurement for North–South traffic.
4. Configure the North–South lab.
5. Baseline and measurement calibration.
6. Migration, validation and reporting.

This audit precedes Work Item 2; it implements none of Work Items 2–6.

## Execution paths and initialization ownership

| Path | Actual execution and scope |
| --- | --- |
| `reset-lab-to-ovs.yml` | Separate, explicitly confirmed destructive lab reset: snapshot configuration → stop libvirt guests → Kolla destroy → residue checks/reboots → write source configuration → redeploy/post-deploy → source service checks. No EW provisioning or baseline import. |
| `ew-provision.yml` | Validate action/placement → bootstrap runtime → source precheck (`ew_provisioning_precheck=true`) → trusted access/config → `ew_provision.py inspect/apply/verify`. Default `inspect` reads cloud state and writes controller evidence. `verify` also creates smoke tasks; only `apply` installs/configures owned workload components. |
| `ew-baseline.yml` | Enable EW → bootstrap/precheck → finite measurement start/wait/stop/drain/collect/report. Guest measurement units are started; it is not a purely read-only operation. No provisioning or migration import. |
| `ew-migrate.yml` | Force EW enabled and apply (reject bypassing extra-vars) → provisioning → baseline → `migrate-to-ovn.yml`. Reset is **not** integrated. Each imported bootstrap allocates an invocation run directory; persistent provisioning state is separate. |
| `migrate-to-ovn.yml` | Ordered phases 00–13. EW is opt-in for this entrypoint. Phase 04 verifies existing EW resources and starts measurements; 06 prepares network MTUs and pauses for separate guest preparation; 07 rechecks readiness and fresh generated files before freeze, then activates TCP port 18081 after API workers stop; 12 records target/recovery evidence; 13 collects, reports and finalizes validation-owned resources. |
| `resume-after-cleanup.yml` | Resume bootstrap/live-state guards → 09 cleanup → 10 restoration → 11/12 validation where eligible → 13 finalization. Not a general early-phase restart or provisioning/reset continuation interface. |
| `ew-collect-bootstrap.yml`, `ew-bootstrap-check.yml`, `ew-recover-credentials.yml` | Separate collection, check-only compatibility, and explicit private controller recovery. None is imported into migration downtime. Recovery reads existing guest secrets and writes private controller files; it does not generate/rotate credentials. |
| Manual utilities | `ew_workload.py` lifecycle/report commands; `ew_transport.py` and `ssh.sh` verified access; configured-run baseline wrappers; application `setup.sh`/`probe.py`; standalone `lab-checkpoint.yml`. Their CLI/docs consumers make them supported paths, not dead files. |

The full desired path is **separately authorized reset → provisioning → baseline
→ migration → report**. Today the first arrow is an operator handoff; the
remaining imports exist in `ew-migrate.yml`. Do not describe reset integration
or empty-cloud provisioning as tested merely because existing-resource reuse
passed.

| Initialization | Owner and boundaries |
| --- | --- |
| EW image/flavor/network/subnet/router/SG/keypair/explicit port/server | `Provisioner.plan/apply/ensure/wait_attachment`; pinned prepared QCOW2, source MTU calculation, exact fixed IPs and host placement; no download/package installation. |
| EW guest boot/network | Existing netfix image/config drive/cloud-init. EW server creation does not inject replacement application/network user-data. `ready` waits for clean cloud-init and checks actual identity, interface/routes/MTU. |
| PostgreSQL cluster/role/database/listen/auth | Reconstructed `PostgreSQL` adapter; fixed reviewed 16/main policy, strict existing ownership/auth checks and interrupted-creation/restart journals. No application tables or data reset. |
| RabbitMQ listener/user/vhost/permissions | Reconstructed `RabbitMQ` adapter; strict existing auth/config checks, no queue/message operations, no package installation or credential replacement. |
| API/worker environment/code/units | `Provisioner.deploy_app`, using original application files and unit definitions from `setup.sh`; atomic comparison, durable pending work and per-service handlers. Manual `setup.sh` remains an alternative that unconditionally restarts services, not the rerun implementation. |
| Application schema | Original `common.initialize()` creates missing tables/indexes with `IF NOT EXISTS`; existing rows remain. Provisioner calls it for required deployment changes; standalone setup also calls it. These paths are intentional/idempotent, not competing DB bootstrap implementations. |
| Application AMQP topology | Original `common.topology()` called by the worker. Bootstrap adapters never declare exchanges/queues. |
| Readiness/tasks and measurement | Original `probe.py` proves completion; provisioning retains original three task receipts. `ew_workload.py` + metrics `agent.py/runner.py` own finite measurement units and raw events. Provisioning copies helpers but starts neither TCP listener. |
| Pair A/B/C | `workload_validation.py` separately owns six validation VMs and cloud-init guest probe deployment. Pair A supplies compute-PCAP packet metrics; Pair B has narrowly scoped opt-in remediation; Pair C tests fresh OVN provisioning. EW resources are explicitly excluded from their reboot/cleanup paths. |

## Finding register

KEEP means retain the reviewed interface/behavior. FIX/REMOVE rows name the
completed cleanup. DEFER means no lifecycle/runtime change was made. MOVE OUT OF
SOURCE is a packaging disposition; no evidence was moved or deleted this pass.

| ID / file and symbol or location | Classification / status | References and consumers inspected | Reason, action and validation |
| --- | --- | --- | --- |
| F01 `migrate-to-ovn.yml`; numbered playbooks; `phase_schema.py` | KEEP — reviewed | All root playbook imports, phase marker producers, `migration_report.py`, resume bootstrap, phase/ordering tests | Actual canonical 00–13 order and legacy interpretation are intentional. Keep dedicated DB/control-plane/PortBinding/PCAP boundaries and incomplete-phase semantics. Validate existing phase/resume regressions and syntax. |
| F02 `ew-migrate.yml`; `reset-lab-to-ovs.yml` | DEFER — Work Item 2 | Root imports, reset's complete tasks, `OrderingTests`, README/provisioning guide | Provisioning → baseline → migration exists; reset → provisioning does not. Implement only after scope/persistence/generation handoff is designed and reviewed. Need empty-cloud and reset lab validation. |
| F03 README EW section; provisioning guide source/status/validation sections | FIX — completed | Operator's current-context evidence, controller-validation helper, prior documented commands, provisioning/readiness tests | Removed stale claims that adapter execution and second no-op apply are pending. Attribute existing-lab success to operator reports; keep empty-cloud/reset/calibration/migration unverified. Correct duplicated retention wording; link this audit. Documentation review, no live verification. |
| F04 `ew-provision.yml` first task/fail message | FIX — completed | Default `ew_provision_spec`, optional JSON override, `validated_spec`, playbook ordering tests | Label incorrectly implied a mandatory explicit specification, despite reviewed defaults. Describe action/placement validation and required private access. Conditions/imports unchanged; syntax checks. |
| F05 `ew_provision.validated_spec` password input message; `ready` deployment comment | FIX — completed | Private recovery's `ew_provision_secret_files` output, default spec templates, `deploy_app`, credential/retry tests | A matching recovered local file is valid; it need not be an original filename. Comment must describe change-aware deployment rather than invocation of the standalone installer. Only wording changed; existing credential/deployment tests. |
| F06 `workloads/ew-bootstrap/adapter.py` and SHA256 descriptors | KEEP — reviewed | `Provisioner.bootstrap`, `ew_bootstrap_verify`, collection/recovery helpers, check/apply and interruption tests | Reconstructed adapters are accepted reviewed sources, not unresolved originals. Keep check/apply interfaces, exact package/configuration pins, ownership/auth refusal and no-install behavior. Source SHA256 unchanged; actual pins tested. |
| F07 Application `setup.sh`, `common.py`, `api.py`, `worker.py`, `probe.py` | KEEP — reviewed | Provisioner extracts units/deploys files/streams probes; setup calls initialize and worker calls topology; app README/manual commands and app tests | Standalone installer is an intentional manual alternative. Do not delete it because playbooks use change-aware deployment. Preserve commit/ACK, task IDs, data and queue semantics; app source unchanged and full tests retained. |
| F08 Provisioning `/opt/ew-provision/probe.py` copy versus streamed smoke probe | DEFER — retained | `ready`, original probe, application/manual workflow docs, deployment tests | The automatic smoke path streams original code; an installed copy is also a diagnostic artifact. Removing it changes guest filesystem behavior for little benefit. No obsolete runtime-file deletion claimed. |
| F09 `group_vars/all.yml` top-level settings | KEEP — reviewed | Every setting traced through Ansible includes/templates, serialized config, Python CLI consumers, docs and override paths | No top-level variable was proven unused. `ew_provision_secret_files`/bootstrap sources feed the nested spec dynamically; transport settings feed JSON. Keep all supported overrides. Reference search was only a candidate check, not a deletion criterion. |
| F10 `validation_console_tail_lines` / probe interval comments | FIX — completed | Phase-04 config, guest diagnostics, Pair-A `dataplane_capture.py`, report evidence tests | Old VM1/console-coverage wording misdescribed authoritative packet evidence. Mark consoles as secondary and Pair-A compute PCAP as authoritative. Values, coverage and metrics unchanged. |
| F11 `mtu_plan.calculate/prepare_networks/require_pre_freeze`; phases 02/06/07 | KEEP — reviewed | Effective source and generated controller files, tunnel interfaces, immutable MTU TSV/JSON, MTU/freeze tests | 1450 is observed underlay, not tenant MTU. Reviewed inputs produce VXLAN 1400 → Geneve 1392; minimum Geneve header 38 and fresh per-controller pre-freeze collection remain mandatory. No target default or MTU gate changed. |
| F12 EW endpoints/flavor sizing in provisioner, adapter, app and `workload_resources`; `validation_compute_hosts` | DEFER — keep reviewed constants | Default topology/spec, `validated_spec` endpoint checks, image sizing, placement and all four path cases, app source | Constants duplicate policy across compatibility boundaries, not necessarily redundant settings. Independent overrides cannot generalize the pinned app/adapters. Future parameterization needs coordinated source/config review, not consolidation in cleanup. Preserve compute1/compute2, six IPs and sizing. |
| F13 Pair-A/B/C prerequisites versus EW netfix image/flavor | KEEP — reviewed | `validation_prerequisites.py`, phase 03, image-size tests, README compatible overrides | Separate managed Ubuntu/default validation flavor and EW pinned QCOW2 are different supported workflows. Do not merge them or add downloads to EW provisioning. Netfix min_disk/min_ram requires a compatible validation override if explicitly reused. |
| F14 `Provisioner.rules/rules_ready/security_group_rule_evidence` | KEEP — reviewed | Actual SDK regressions, creation request serialization, partial owned-group retry, plan JSON, current SG reuse tests | Preserve SDK `ether_type` and explicit JSON `ethertype`. Reuse unrestricted tenant rules without narrower/duplicate rules; keep both egress families required. SDK 4.21.0 regressions execute, not SimpleNamespace-only evidence. |
| F15 `resources.json` UUIDs/created/guests/preserved_tasks/pending deployment | KEEP — reviewed | `known/checkpoint/ensure/ready/deploy_app`, immediate port/server checkpoint, BUILD/retry/task-preservation tests | Ordinary reruns must preserve UUIDs, exact port/IP/placement, guest boots and original receipts. Missing UUIDs/conflicts fail, never recreate/rebase. Pending steps remain identity/digest-bound and recoverable. |
| F16 Reset handoff of provisioning/measurement/MTU state | DEFER — Work Item 2 | `ew_provision_state_dir`, bootstrap run allocation, `resolve_ew`, lifecycle agent slots, reset deletion paths | Guest deletion invalidates old UUID/boot/task checkpoints. Archive old generation; explicitly create a new state/trust/run generation without rewriting old evidence. A late-phase resume is not this operation. Add restart/failure tests before implementing. |
| F17 `trust_new_guest`, `ssh_options`, `Transport.profile`; guest known-hosts | DEFER — Work Item 2 | Exact-new-server creation journal, authenticated Nova public-key block, strict SSH options, known-host refusal tests | Trust is currently address keyed. A reused IP can retain an old key; presence is not proof of a rebuilt guest, and mismatches fail SSH. Use explicit generation-specific guest trust after deletion; preserve management trust/keys, never disable host-key checks or silently replace entries. Console key truncation remains a clear failure. |
| F18 Private inputs/QCOW2/reset protected paths | DEFER — Work Item 2 | `private_tree`, local credential no-replace saves, source image checksum checks, reset globals/config/backup deletion, documented overrides | Default `/root/ew-private` and `/root/ew-image-build` survive current default paths, but arbitrary reset overrides are not proven safe. Upcoming reset needs a reviewed protected-path manifest and overlap checks before mutation. Do not rotate existing secrets; new empty-cloud credentials require separately prepared private inputs, not recovery from deleted guests. |
| F19 Phase-07 TCP activation and phase-12 independent recovery | KEEP — reviewed | `activate_tcp`, `tcp_recovery_evidence`, `ew_tcp_experiment.report`, restoration markers/sequence fences, focused TCP tests | 18080 binds before migration; 18081 activation is after all API workers stop through saved source transport without API calls. Target identity/MTU/Geneve evidence precedes application reconciliation. Keep TCP acceptance independent of E2E and Pair-A PCAP. |
| F20 `tests/test_ew_provision.py` three local helper subprocesses | FIX — completed | Real temporary environment/credential installer tests, embedded helper imports, test interpreter/venv | Use `sys.executable` for local-only helper execution instead of whichever `python3` happens to be on PATH. Guest command construction remains unchanged. Existing byte/mode/mtime and refusal tests still execute real helper code. |
| F21 `test_changed_api_only_has_api_handler` old dependency-command exclusion | REMOVE — completed | `baked_dependencies`, test's earlier mocked dependency branch, deployment handler tests | Removed obsolete exclusion for `import gunicorn,psycopg2,pika`; production now uses a structured missing-dependency probe. Preserve service-action assertions and dedicated missing-dependency tests, not a stale special case. Test module docstring now says no cloud API (real SDK resources are used). |
| F22 Offline account/tool/dependency portability | KEEP — reviewed | RabbitMQ deterministic group fixture (GID 4242), metrics fake ubuntu UID/GID, recovery fake ownership/root, SDK/tool/Ansible skip sites | No unmocked RabbitMQ/ubuntu OS account lookup found in exercised offline paths. Production still requires its actual guest accounts. Linux, optional SDK/Ansible/ovsdb-tool availability can affect coverage; report versions/skips rather than suppress tests. Full suite runs as UID 1000 here. |
| F23 `ew_collect_bootstrap` historical `adapters_ready=false` | KEEP — reviewed | Collector tests, reconstructed check helper, provisioning guide | This field describes unresolved original-source collection, not reconstructed adapter acceptance. It is historical schema, not a mandatory blocker. Removing/changing it would alter supported evidence; docs explain the distinction. |
| F24 `lab-checkpoint.yml`, `lab_checkpoint.py`, `lab_checkpoint_host.py` | DEFER — whole-cloud checkpoint scope retained | Standalone action CLI, docs, API/offline scope/TAP/OVSDB/product UUID/reboot/serialization tests | Not imported by migration/reset; not dead code. Preserve all guards and recovery interfaces. Even cosmetic import cleanup here is unnecessary to the reset-preparation objective; checkpoint source stays untouched. |
| F25 Baseline wrappers, `ssh.sh`, imported app/metrics tests and READMEs | KEEP — reviewed | Shell exec targets, documented manual configured-run CLI, Python imports/test collection | Manual scripts remain callable; baseline sessions/guest raw data remain immutable. Documentation is a consumer. No removal based solely on missing imports. |
| F26 Review packaging/patches/archives and caches | MOVE OUT OF SOURCE — already external; retained | Tracked/untracked/ignored file inventory, root entrypoints, `/tmp` review artifact names, `.gitignore` | Review ZIPs/patches/packaging scripts and test venv are outside the repository under `/tmp`; none is staged. Ignored pytest/Python caches remain untouched. No live config, runtime JSON, key or QCOW2 was found among tracked source. Preserve evidence wherever it lives; do not broadly delete generated files or ignore all JSON/patches. |

## Configuration and state handoff details

Settings resolve through `group_vars/all.yml`, entrypoint/play variables and
explicit operator extra-vars; Kolla path environment lookups supply fallback
defaults and do not prohibit Ansible overrides. The optional provisioning JSON
spec overrides the full provisioning spec, not the EW topology or reviewed app
endpoint invariants. Secrets and bootstrap descriptors feed that spec through
templates. `ew-config-tasks.yml` serializes guest/host SSH paths, inventory names
and bounded measurement settings; it never serializes private key contents.
Reset has separate play-local `reset_*` defaults and hardcoded VIP/interfaces/
service configuration; those do not automatically follow migration settings.
No reset default or deletion path changed in this audit.

| State | Ordinary rerun | After deletion/rebuild (future reset handoff) |
| --- | --- | --- |
| `/var/lib/ovs-to-ovn-ew-provisioning/resources.json` and readiness | Reuse exact UUIDs/boots, original three receipts and pending deployment steps. Refuse missing/replaced resources. | Archive as prior generation; select a new state directory explicitly. Never edit UUIDs/receipts to make old state accept replacements. |
| Invocation `ew-resources.json`, `ew-lifecycle.json`, guest run events and slots | Same-session retries preserve identities, sequence fences, launch/stop intent and raw evidence. A new measurement gets a new discovered run. | Retain evidence; establish a new catalog/run only after new guests are verified. Do not resume old runners against new boots. |
| MTU calculation/target snapshots/TSV journal | Preserve original/target values across interrupted updates and the operator pause; fresh pre-freeze gate still required. | Do not treat already-reduced networks as original VXLAN baselines. A rebuilt source must be independently prechecked before a new journal. |
| Guest known-hosts / management known-hosts | Existing guest entries are never replaced; exact new owned servers may acquire missing verified console keys. Management trust remains strict. | Preserve old trust as evidence; choose new guest-generation trust for reused addresses with authenticated UUID-key provenance. Do not clear management trust or private keys. |
| Controller private passwords, mapping, QCOW2/checksum, adapter/application source, inventory/Kolla passwords | Retain; matching values preserve original bytes/mode/mtime. | Copy/verify protected inputs before destruction. Retain private credentials deliberately; recovery cannot recover secrets from guests that no longer exist. |
| Whole-cloud checkpoint manifests/seals, run evidence, snapshots, PCAPs and reports | Never rewrite them as a cleanup convenience. | Retain outside any reset deletion scope; whole-cloud restore is a separate deferred workflow. |

The four topology cases remain: app → client-a1 is same subnet/same compute;
app → client-a2 is same subnet/different compute; app → queue is different subnet/
same compute; app → client-b or DB is different subnet/different compute. A
centralized router may forward routed traffic through a network node; VM
co-placement alone does not prove local forwarding.

## Offline verification

The following checks ran after cleanup. They execute only local tests/parsing/
syntax checks, not cloud playbooks.
The full suite includes temporary-file-only Ansible and OVSDB tests with mocked
cloud/SSH/service operations. No dependency upgrade is part of this change.

Environment: Python **3.12.3**, OpenStackSDK **4.21.0**, ansible-core **2.16.19**,
pytest **8.3.5**, pytest-subtests **0.15.0**, PyYAML **6.0.2**, SQLAlchemy **2.0.41**.
Tests ran as UID 1000. Host `ovsdb-tool` **3.3.9** was available for the synthetic,
temporary-file-only OVSDB query regression.

```bash
PYTHONDONTWRITEBYTECODE=1 /tmp/ew-work-item1-review-venv/bin/python -m pytest -q -rs -p no:cacheprovider tests/test_ew_provision.py tests/test_ew_reconstructed_bootstrap.py tests/test_ew_private_recovery.py tests/test_ew_bootstrap_collection.py tests/test_ew_workload.py tests/test_ew_tcp_followup.py tests/test_mtu_and_ew.py tests/test_mtu_prefreeze.py tests/test_lab_checkpoint.py
PYTHONDONTWRITEBYTECODE=1 /tmp/ew-work-item1-review-venv/bin/python -m pytest -q -rs -p no:cacheprovider
PYTHONDONTWRITEBYTECODE=1 /tmp/ew-work-item1-review-venv/bin/python -m pytest -q -rs -p no:cacheprovider tests/test_ew_provision.py
```

| Check | Actual result |
| --- | --- |
| Focused Work Item 1/EW/TCP/MTU/checkpoint regressions | **330 passed**, 265 subtests passed; no failures/skips; 24.02 s |
| Full offline suite, including imported app/metrics tests | **636 passed**, 513 subtests passed; no failures/skips; 26.87 s |
| Provisioning recheck after final error-message wording | **46 passed**, 25 subtests passed; no failures/skips; 50 SDK deprecation warnings; 1.29 s |
| SDK warnings | Both test runs emitted 62 `RemovedInSDK50Warning` notices from SDK `_compute_attributes` (50 provisioning, 12 checkpoint). Tests were not suppressed or weakened. |
| Python AST/compile checks | **55 files** plus **4 embedded helpers** (PG_CODE, READ_CODE, APP_CHECK, collector REMOTE_CODE) passed; no bytecode artifacts written. |
| YAML safe parsing | **32 YAML files** passed. |
| Bash parse-only checks | **5 shell helpers** passed: controller-validation, standalone app setup, baseline start/collect and SSH helper. None executed. |
| Bootstrap source pins | Both descriptors match unchanged adapter SHA256 `39ab4c721383f1798a0903b4b99a5fd81920bcfd31aaa4252ce0af1474e26f9c`. |
| Ansible syntax checks | All **10 root entrypoints** passed with `tests/fixtures/syntax-inventory.ini`; no injected runtime facts. Existing hyphenated-group warning remains; no remote tasks executed. |
| Behavior-preservation review | Parsed defaults identical to starting HEAD; provisioning play conditions/imports identical after stripping labels/messages; production Python AST identical except the intended error string. Reset, adapter and migration/resume entrypoints byte-identical to starting HEAD. |
| Diff review/whitespace | `git diff --check` and staged `git diff --cached --check` passed; only focused cleanup/audit files staged. |

Every root entrypoint was checked with this command pattern:

```bash
PYTHONDONTWRITEBYTECODE=1 /tmp/ew-work-item1-review-venv/bin/ansible-playbook -i tests/fixtures/syntax-inventory.ini ENTRYPOINT --syntax-check
```

`ENTRYPOINT` was each of `ew-baseline.yml`, `ew-bootstrap-check.yml`,
`ew-collect-bootstrap.yml`, `ew-migrate.yml`, `ew-provision.yml`,
`ew-recover-credentials.yml`, `lab-checkpoint.yml`, `migrate-to-ovn.yml`,
`reset-lab-to-ovs.yml`, and `resume-after-cleanup.yml`. This inventory is a syntax
fixture, not a substitute for the real five-host lab. Source checks and mocks
cannot verify guest package/configuration behavior, admin placement privileges,
SSH/cloud-init on newly built guests, or actual reset/deletion scope.

## Concrete Work Item 2 recommendations

1. Define a reset generation and non-replayable destructive intent before wiring
   any provisioning import. Verify inventory identities and explicit lab scope;
   do not borrow late-phase migration resume as a reset retry mechanism.
2. Build a protected-input/evidence manifest first. Check actual resolved reset
   overrides against QCOW2/checksum, private inputs, both SSH trust sets, inventory,
   Kolla passwords, repository, checkpoint seals, snapshots and measurement data.
   Existing broad destroy/libvirt-stop behavior and hardcoded host settings need
   separate review. Do not delete evidence to obtain a “clean” run.
3. Archive prior provisioning state and use generation-specific state and guest
   known-hosts paths. Preserve old UUIDs, boot identities and task receipts as
   history; never silently rebase them or accept a new key at a reused IP.
4. Verify a healthy OVS source and actual effective MTUs after reset, then invoke
   provisioning with the retained pinned netfix image and private inputs. Create
   no measurement/baseline until all six guests and real task completion pass.
5. Test fresh creation, interrupted creation/BUILD recovery, conflicting existing
   resources, empty-cloud dependencies/auth, SSH keys at reused IPs, reset retry
   barriers, and a no-op second apply before claiming integration complete.
6. Preserve the 18081 post-freeze activation, final fresh target-file/MTU gates,
   Pair-A capture and Pair-B/C ownership. Work Item 2 must not add provider/FIP,
   external/NAT or North–South scope; those remain later Work Items.
