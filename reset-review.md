# Reset-only review — 2026-10-11

Baseline: user-supplied `a77af56d69f170f7c79694f15266eb0f0cf39754` ZIP.
Original archive SHA256:
`2a325736197a9fef264d8166051cca623608353d5985861664dcab9a19b5d85c`.

Updated existing files: `reset-lab-to-ovs.yml`, `inventory.example`, `README.md`.
Added implementation/docs/tests: `group_vars/reset.yml`,
`scripts/reset_validation.py`, `docs/reset-four-nodes.md`,
`tests/test_reset_four_nodes.py`, `tests/fixtures/reset-four-nodes.ini`.
`reset-four-nodes.patch` contains these eight file changes relative to baseline.

Reset now requires exactly controller/network1/compute1/compute2, preserves one
full inventory throughout destroy/deploy, aborts across plays on host failure,
snapshots each reset separately, rebuilds native OVS/VXLAN MTU 1400 and prepares
official Ubuntu validation prerequisites. New image/flavor UUIDs and Geneve
target MTU 1392 are recorded only after source-cloud checks complete.

Offline verification:

- Full unittest suite: **282 passed**, no skips (includes 11 reset regressions).
- Ansible-core 2.16.14: reset, migrate and resume entry-point syntax checks passed.
- `--list-hosts`: both computes included; network2 excluded.
- Actual guard executions: omitted confirmation, `--check`, and `--limit localhost`
  each failed with `changed=0`, before any destructive or remote operation.
- Python AST and embedded Bash syntax checks passed.
- All migration playbooks, `group_vars/all.yml`, and existing scripts including
  `validation_prerequisites.py` remain byte-identical to baseline.

No reset operation, guest creation, image download/upload or lab API mutation
was run while producing this archive. Real four-node reset validation remains
pending until the next deliberate reset cycle on the user's controller.

Scope: this update handles repeatable four-node reset. It does not yet implement
cross-compute placement or Pair D in the migration workflow.
