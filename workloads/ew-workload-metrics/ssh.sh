#!/usr/bin/env bash
# Source this helper, then ew_ssh <catalog VM name> <source|ovn> <command...>.
# EW_RUN_DIR must identify persisted catalog/lifecycle/access configuration.
ew_ssh() {
    local task_guest="$1" task_phase="$2"
    shift 2
    local task_repo
    task_repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
    : "${EW_RUN_DIR:?Set EW_RUN_DIR to the measurement run directory}"
    python3 "$task_repo/scripts/ew_transport.py" "$EW_RUN_DIR" "$task_guest" "$task_phase" "$@"
}
