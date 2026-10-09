#!/usr/bin/env bash
# Run on root@lab2-controller. Creates an independent systemd service per client.
set -euo pipefail
test "$(id -u)" -eq 0
task_src=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "$task_src/ssh.sh"
task_root=/root/ew-image-build
task_archive="$task_root/ew-workload-metrics.tar.gz"
test -f "$task_archive"
test -r /root/.ssh/ew-lab
test -r /root/.ssh/kolla_lab_ed25519
# Prevent overlapping baseline runs from changing the offered load.
for task_ip in 192.168.101.12 192.168.101.13 192.168.102.13; do
    task_running=$(ew_ssh "$task_ip" \
        "systemctl list-units --type=service --state=running --no-legend --no-pager 'ew-load-*'" </dev/null)
    if [ -n "$task_running" ]; then
        echo "WORKLOAD_ALREADY_RUNNING $task_ip"
        printf '%s\n' "$task_running"
        exit 1
    fi
done
task_run="base-$(date -u +%Y%m%dT%H%M%SZ)-$(python3 -c 'import secrets; print(secrets.token_hex(3))')"
task_dir="$task_root/baseline-runs/$task_run"
install -d -m 700 "$task_dir"
printf '%s\n' "$task_run" > "$task_dir/run-id.txt"
date -u '+CONTROLLER_START_UTC=%Y-%m-%dT%H:%M:%SZ' > "$task_dir/controller-time.txt"
printf '%s\n' "$task_run" > "$task_root/baseline-current-run.txt"

for task_ip in 192.168.101.12 192.168.101.13 192.168.102.13; do
    case "$task_ip" in
        192.168.101.12) task_client=ew-client-a1 ;;
        192.168.101.13) task_client=ew-client-a2 ;;
        192.168.102.13) task_client=ew-client-b ;;
    esac
    echo "=== START_BASELINE: $task_client ==="
    ew_ssh "$task_ip" \
        'sudo -n sh -c "umask 077; cat > /root/ew-workload-metrics.tar.gz"' \
        < "$task_archive"
    ew_ssh "$task_ip" 'sudo -n bash -c "
        set -e
        tar -xzf /root/ew-workload-metrics.tar.gz -C /root
        install -d -m 755 /opt/ew-load
        install -m 644 /root/ew-workload-metrics/runner.py /opt/ew-load/runner.py
        install -d -o ubuntu -g ubuntu -m 750 /var/lib/ew-load
    "' </dev/null
    ew_ssh "$task_ip" "sudo -n systemd-run \
        --unit=ew-load-$task_run \
        --property=Type=exec \
        --property=User=ubuntu \
        --property=Group=ubuntu \
        --property=RemainAfterExit=yes \
        --property=TimeoutStopSec=45 \
        /usr/bin/python3 /opt/ew-load/runner.py \
        --client-id $task_client --run-id $task_run \
        --duration 120 --drain 30 --max-rate 1 --slo 10 \
        --output /var/lib/ew-load/$task_run" </dev/null
    printf '%s %s\n' "$task_client" "$task_ip" >> "$task_dir/clients.txt"
done

echo "BASELINE_RUN_ID=$task_run"
echo "BASELINE_OUTPUT=$task_dir"
echo BASELINE_STARTED
