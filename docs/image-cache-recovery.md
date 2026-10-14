# Registry không còn trả manifest nhưng image vẫn còn local

Lỗi phase 05 `Pull OVN DB images only` xảy ra trước target config/MTU, API freeze
và DB migration. `docker manifest inspect` trả `no such manifest` trong khi
`docker image ls` vẫn có đúng tag là bằng chứng lỗi truy cập manifest hiện tại;
không chứng minh image local hỏng hoặc tag đã bị xóa vĩnh viễn.

## Chính sách image

Phase 05 kiểm tra toàn bộ image cần cho OVN DB, OVN controller và Neutron target
trên đúng host. Nếu đúng reference đã có local, dùng lại. Nếu thiếu, Kolla tải
image đó; lỗi registry vẫn chặn migration trước target config/MTU và downtime.
Không bỏ qua lỗi tải, không tự đổi release/distro/tag hoặc retag image khác.

Helper dùng một bản sao tạm của Ansible tree thuộc Kolla đang cài, thay đúng
action `pull_image` bằng `ensure_image` của Kolla 18.8. Native defaults, service
mapping, inventory overrides, globals.d và registry authentication vẫn do Kolla
xử lý. Installed Kolla không bị chỉnh; chỉ thực thi action pull với policy mới,
không deploy/restart container. Context OVN chỉ áp dụng cho lựa chọn image.

Mỗi lần kiểm tra ghi riêng vào `image-preflight/<invocation-id>/` trong backup:
`result.json`, `kolla-images.log` và `<host>.<role>.json` chứa image ID, tags,
repo digests, architecture, OS. Evidence cũng được đưa vào migration-report.json;
format TXT không đổi. Không lưu image environment hoặc registry password vào
JSON evidence.

Image cache là dữ liệu hữu ích trong trường hợp này. Nếu thiếu `ovn-controller`
trên network1/compute1/compute2 mà registry vẫn lỗi, cần khôi phục đúng image
từ bản lưu hoặc registry nội bộ đã xác minh trước khi tiếp tục. Có ba image DB
trên controller không chứng minh image trên chassis đã đủ.

## Tiếp tục run bị dừng ở phase 05

Cập nhật mã từ migration.zip vào thư mục đang dùng, sau đó chạy:

```bash
source /root/venvs/kolla-2024.1/bin/activate
cd /root/ovs-to-ovn-migration

if stage_run_dir=$(python3 scripts/pre_stage_recovery.py select \
    /root/ovs-to-ovn-backup --inventory /root/multinode); then
    ansible-playbook -i /root/multinode resume-before-target.yml \
        -e "migration_resume_run_dir=$stage_run_dir"
fi
```

Selector chỉ chọn khi có đúng một checkpoint hợp lệ; không mặc định lấy thư mục
mới nhất. Nếu có nhiều checkpoint, chỉ định run bạn đang xử lý:

```bash
ansible-playbook -i /root/multinode resume-before-target.yml \
    -e migration_resume_run_dir=/root/ovs-to-ovn-backup/<run-id>
```

Route này yêu cầu schema 2, phases 00–04 hoàn tất, phase 05 chưa hoàn tất,
inventory không đổi; từ chối mọi dấu vết phase 06 trở đi, đổi MTU, freeze,
DB migration hoặc cleanup. Kiểm tra trực tiếp Neutron server còn chạy ML2/OVS
VXLAN và OVS agent còn chạy trên mọi chassis trước khi staging lại.

Phases 00–04, VM, resource UUID, ICMP anchor, TCP/N-S baseline không được tạo
lại. Route chạy phase 05–13, dùng lại readiness gate ở phase 07 để kiểm tra
bằng chứng mới của workload trước freeze. Probe đã hết lifetime hoặc phiên cũ
đã hỏng có thể khiến gate này dừng; resume không tạo phiên mới để thay evidence.
Không dùng route này cho checkpoint đã vào phase 06 hay sau DB migration.

`orchestration-recovery.json` ghi thời gian phục hồi. Tổng thời gian run gồm
thời gian chờ xử lý lỗi; downtime control plane chỉ bắt đầu khi freeze thực sự.

## Kiểm tra mã

Tests chạy Ansible thực tế với Docker/registry được mô phỏng: cached image +
registry 404 vẫn PASS, chỉ image thiếu mới được tải, thiếu image + registry 404
vẫn FAIL; inventory image override và mapping trên ba chassis được giữ.
Recovery tests thực thi guard trên đủ bốn host, giữ nguyên timers/baselines,
từ chối inventory đổi, target live config, phase sau hoặc cleanup.
Không có reset/deploy/migration nào được chạy trên lab trong quá trình sửa.

Tham chiếu implementation đã đối chiếu: gói chính thức kolla-ansible 18.8.0,
`ansible/roles/service-images-pull/tasks/main.yml`,
`ansible/module_utils/kolla_docker_worker.py::ensure_image` và `tools/kolla-ansible`.
