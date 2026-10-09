#!/usr/bin/env bash
ew_ssh() {
    local task_ip="$1"
    shift
    ssh -i /root/.ssh/ew-lab \
        -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=10 \
        -o StrictHostKeyChecking=accept-new \
        -o UserKnownHostsFile=/root/ew-image-build/guest-known-hosts \
        -o 'ProxyCommand=ssh -T -i /root/.ssh/kolla_lab_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new root@10.0.0.160 ip netns exec qrouter-7c696b40-be7b-4524-b202-53bba33f7978 nc %h %p' \
        "ubuntu@${task_ip}" "$@"
}
