#!/usr/bin/env bash
# Finite baseline from an already configured run. For discovery use ew-baseline.yml.
set -euo pipefail
task_repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
: "${1:?Supply configured run directory}"
exec python3 "$task_repo/scripts/ew_workload.py" baseline "$1"
