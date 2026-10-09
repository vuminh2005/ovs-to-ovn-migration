#!/usr/bin/env bash
# Wait for summaries, retrieve client event logs and capture API/worker logs.
set -euo pipefail
task_src=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "$task_src/ssh.sh"
task_root=/root/ew-image-build
task_run=${1:-$(cat "$task_root/baseline-current-run.txt")}
[[ "$task_run" =~ ^base-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$ ]]
task_dir="$task_root/baseline-runs/$task_run"
test -f "$task_dir/clients.txt"
test "$(wc -l < "$task_dir/clients.txt")" -eq 3

task_ready=false
for task_attempt in $(seq 1 40); do
    task_done=0
    while read -r task_client task_ip; do
        if ew_ssh "$task_ip" "test -f /var/lib/ew-load/$task_run/summary.json" </dev/null; then
            task_done=$((task_done + 1))
        fi
    done < "$task_dir/clients.txt"
    echo "BASELINE_PROGRESS completed=$task_done/3 check=$task_attempt/40"
    if [ "$task_done" -eq 3 ]; then
        task_ready=true
        break
    fi
    sleep 5
done

if [ "$task_ready" != true ]; then
    while read -r task_client task_ip; do
        echo "=== UNIT_DIAGNOSTIC: $task_client ==="
        ew_ssh "$task_ip" "sudo -n systemctl status ew-load-$task_run --no-pager -l" </dev/null || true
        ew_ssh "$task_ip" "sudo -n journalctl -u ew-load-$task_run -n 30 --no-pager" </dev/null || true
    done < "$task_dir/clients.txt"
    echo BASELINE_COLLECTION_TIMEOUT
    exit 1
fi

while read -r task_client task_ip; do
    install -d -m 700 "$task_dir/$task_client"
    ew_ssh "$task_ip" "tar -czf - -C /var/lib/ew-load $task_run" </dev/null \
        > "$task_dir/$task_client.tar.gz"
    tar -xzf "$task_dir/$task_client.tar.gz" \
        -C "$task_dir/$task_client" --strip-components=1
    ew_ssh "$task_ip" "sudo -n systemctl show ew-load-$task_run \
        -p ActiveState -p SubState -p Result -p ExecMainStatus" </dev/null \
        > "$task_dir/$task_client/unit-state.txt"
done < "$task_dir/clients.txt"

ew_ssh 192.168.101.11 \
    'sudo -n journalctl -u ew-api.service -u ew-worker.service -n 3000 --no-pager' \
    </dev/null > "$task_dir/app-worker-journal.txt"
date -u '+CONTROLLER_COLLECT_UTC=%Y-%m-%dT%H:%M:%SZ' >> "$task_dir/controller-time.txt"
echo "BASELINE_OUTPUT=$task_dir"
python3 "$task_src/report.py" "$task_dir" | tee "$task_dir/report.txt"
