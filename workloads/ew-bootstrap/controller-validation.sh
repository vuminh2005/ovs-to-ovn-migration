#!/usr/bin/env bash
# Prepared for later review/authorization; never executed by the coding agent.
# Controller management address: 117.1.28.69; not a workload endpoint.
# Guest/API reads + private controller output only; no apply/E2E/service operations.
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
