#!/usr/bin/env bash
# Idempotent bounded stop/drain/collection; retains all guest evidence.
set -euo pipefail
task_repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
: "${1:?Supply configured run directory}"
exec python3 "$task_repo/scripts/ew_workload.py" collect "$1" --mode baseline --transport-phase source
