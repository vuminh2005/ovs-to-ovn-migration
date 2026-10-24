# OpenStack ML2/OVS → ML2/OVN migration

Mặc định gọn: `migration_rollback_enabled: false`, 6 VM ping, TCP tắt.
Không snapshot/dump/rehearse để rollback; backup MariaDB theo mặc định này cũng
tắt. Monitor debug tổng quát và smoke test tạo network là tùy chọn. Capture
ping, kiểm tra migration và thứ tự metadata/gateway vẫn giữ. Xem
[cách tùy biến run](docs/run-options.md).

Mặc định validation chỉ đo ping E-W/N-S: 6 VM khi bật đủ ma trận E-W và N-S.
Không tạo VM TCP, VM DHCP/metadata riêng hoặc VM mới sau migration; không chạy
TCP application probes. Xem [validation chỉ ping](docs/ping-only-validation.md)
về số VM, report và các tùy chọn bật lại kiểm thử.

Cập nhật 23/10/2026: migration theo vai trò inventory, hỗ trợ nhiều network/compute
không khóa số node. Xem [thay đổi, thông tin còn thiếu và tương thích lab 4 node](docs/node-independent-migration.md).
Reset vẫn dành riêng lab 4 node; cấu hình N-S mặc định vẫn là profile lab.

Đã sửa lỗi đọc OVS `external_ids` ở precheck và kiểm tra sau cutover.
Xem [nguyên nhân và kiểm chứng](docs/host-network-fix.md).

Metadata được chuẩn bị trước downtime ở phase 06: container chưa chạy, service
bị disable. Phase 08 dừng metadata cũ trên từng host rồi start container đã
chuẩn bị, trước deploy controller và không chờ chassis tại bước start. Xem
[luồng chuyển giao metadata](docs/metadata-preparation.md).

Gateway được schedule sớm ở đầu phase 10, sau cleanup OVS cũ và trước start
Neutron API. Helper dùng scheduler gốc trong image Neutron, đã preflight trước
downtime ở phase 07. Lỗi tối ưu sẽ lưu evidence rồi tiếp tục bật API. Xem
[gateway scheduling sớm](docs/early-gateway-scheduling.md).

Rollback giữ VM cần bật `migration_rollback_enabled=true` từ đầu run.
Entrypoint riêng dùng một lệnh tự inspect, lấy hash của kế hoạch
vừa tạo và thực thi khi các gate đạt; có checkpoint riêng Neutron đã restore-test
và barrier dừng writer trước restore. Không cần sao chép SHA hay truyền cờ xác nhận.
Khi bật rollback, checkpoint sau khi đóng API làm tăng control-plane downtime; thời gian được
ghi riêng, chưa có số đo lab. Xem [rollback về OVS](docs/rollback.md) trước khi
chạy; run cũ thiếu checkpoint không được phục hồi tự động bằng workflow này.

Đã sửa lỗi snapshot phase 01 nhận nhầm container OVN chưa tồn tại: dùng inspect
riêng container và xử lý đúng biến thể `no such object/container`, vẫn chặn lỗi
Docker daemon/quyền truy cập. Xem [cách chạy lại sau lỗi này](docs/rollback.md).

Khi bật rollback, checkpoint phase 07 đọc Nova DB qua container `nova_api` bằng session READ ONLY,
không gọi Nova server-detail API sau khi Neutron đã dừng. Reader được preflight
trước freeze; kiểm tra mọi cell và mapping, UUID/host/task state vẫn giữ nguyên.
Helper được chép nguyên byte bằng `copy.src`; controller kiểm tra hash/identity
của receipt trước khi đóng API, và chỉ rõ trường sai nếu gate dừng.

Playbook cho lab Kolla-Ansible 2024.1, giữ workflow Phase 00–13 và bộ đo
E-W/N-S của bản `playbook new stage.zip`. Bản audit ngày 22/10/2026 sắp xếp
lại source, cập nhật import/path và bổ sung kiểm tra cấu trúc; kết quả kiểm
chứng nằm trong [báo cáo audit](docs/audit-current.md).

## Bắt đầu từ đâu

| Mục đích | File/thư mục |
|---|---|
| Migration đầy đủ | [migrate-to-ovn.yml](migrate-to-ovn.yml) |
| Rollback giữ VM bằng một lệnh | [rollback-to-ovs.yml](rollback-to-ovs.yml), [hướng dẫn](docs/rollback.md) |
| Reset lab về OVS | [reset-lab-to-ovs.yml](reset-lab-to-ovs.yml) |
| Phase theo thứ tự thực thi | [playbooks/](playbooks/) — `00-*.yml` đến `13-*.yml` |
| Recovery lịch sử, đã ngừng hỗ trợ | [archive](archive/README.md) |
| Task dùng lại / playbook vận chuyển helper | `playbooks/tasks/`, `playbooks/transport/` |
| Python theo chức năng | [scripts/](scripts/README.md) |
| Callback, filter và adapter tạo container | [plugins/](plugins/README.md) |
| Test theo chức năng | [tests/](tests/README.md) |
| Retry chuyên biệt và tài liệu cũ | [archive/](archive/README.md), `docs/history/` |
| Cấu trúc và bản đồ đường dẫn cũ → mới | [docs/structure.md](docs/structure.md), [docs/path-map.tsv](docs/path-map.tsv) |

`group_vars/` và `inventory.example` giữ vị trí quy ước của Ansible.
Mỗi phase 00–13 có đúng một file đánh số trong `playbooks/`, gồm toàn bộ phần chuẩn bị metadata trong `06-target-config.yml`.
Các entrypoint migrate/reset/rollback ở gốc; lệnh migrate/reset giữ nguyên.
Các file trong `archive/` không được import bởi migration/reset/rollback mặc định.

## Chạy trên controller

Dùng đầy đủ source của cùng một phiên bản trong `/root/ovs-to-ovn-migration`.
Khi thay bản ZIP, dùng thư mục source sạch; giải nén đè sẽ để lại các file cũ
đã được di chuyển. Giữ inventory, cấu hình Kolla và thư mục backup/run hiện có.
Hoàn tất hoặc dừng hẳn run đang chạy trước khi thay source.

```bash
cd /root/ovs-to-ovn-migration
source /root/venvs/kolla-2024.1/bin/activate

ansible-playbook -i /root/multinode reset-lab-to-ovs.yml --syntax-check
ansible-playbook -i /root/multinode migrate-to-ovn.yml --syntax-check
ansible-playbook -i /root/multinode rollback-to-ovs.yml --syntax-check
```

Reset xóa deployment/workload lab hiện tại và dựng baseline OVS. Cờ xác nhận
reset và việc giữ migration backup hoạt động như bản gốc:

```bash
ansible-playbook -i /root/multinode reset-lab-to-ovs.yml \
  -e reset_lab_confirm=true -e increase_quota=true -e instance_quota_limit=30
```

Sau khi reset kết thúc với `OVS_4NODE_BASELINE_READY`:

```bash
source /etc/kolla/admin-openrc.sh
ansible-playbook -i /root/multinode migrate-to-ovn.yml
```

Reset chờ các container OVS chạy và `br-int` tồn tại, tối đa 120 giây trên
mỗi network/compute, rồi bật và xác minh OpenFlow15 trước khi tạo workload.
Xem [gate OVS và chẩn đoán startup](docs/reset-ovs-readiness.md). Phase 07 chỉ bổ sung protocol còn thiếu trước downtime; nếu đã
đủ thì không ghi cấu hình. Phase 08 chỉ xác minh, không thay đổi protocol.

Trong profile bạn cung cấp, `group_vars/quota.yml` giữ `increase_quota: true`
và `instance_quota_limit: 30`. Các lệnh reset ở đây cũng truyền rõ hai giá trị
đó. Đặt `-e increase_quota=false` để bỏ qua tăng quota trong một lần chạy.
Helper chỉ nâng quota instance nếu cần; giữ quota cao hơn/không giới hạn và
không đổi cores/RAM. Xem [hướng dẫn quota](docs/project-quota.md).
Capacity vẫn kiểm tra sức chứa workload theo quota thực tế.

Các giá trị mặc định giữ profile lab trong `group_vars/all.yml`, `group_vars/ns-lab.yml` và
`group_vars/reset.yml`: Kolla 18.8, Ubuntu 24.04, một controller, network1 và
hai compute. Hướng dẫn [reset](docs/reset-four-nodes.md), [demo](docs/demo.md)
và [workflow](docs/cutover-workflow.md) giải thích các điều kiện cụ thể.

## Kết quả và resume

Hai báo cáo chính vẫn ở gốc thư mục run:

```text
/root/ovs-to-ovn-backup/<run-id>/migration-report.txt
/root/ovs-to-ovn-backup/<run-id>/migration-report.json
```

Run mới dùng `run-layout.json` và các nhóm `config/`, `backups/`, `resources/`,
`checkpoints/`, `evidence/`, `captures/`, `metrics/` cùng các thư mục diagnostics.
Run lịch sử không có marker tiếp tục dùng layout cũ. Source reorganization
không chuyển file dữ liệu run, đổi schema/checkpoint hay đổi công thức downtime.

Các route resume thủ công đã chuyển vào [archive](archive/README.md), cùng helper
checkpoint, hướng dẫn và test snapshot cũ. Chúng dừng ngay khi được gọi; không
còn là entrypoint được hỗ trợ cho workflow metadata/capture hiện tại.

Nếu run lab lỗi trước freeze và capture N-S gốc đã dừng, giữ nguyên run cũ để
điều tra. Sau khi sửa nguyên nhân, reset lab để có baseline OVS và lượt đo mới:

```bash
ansible-playbook -i /root/multinode reset-lab-to-ovs.yml \
  -e reset_lab_confirm=true -e increase_quota=true -e instance_quota_limit=30 \
  -e reset_delete_migration_backups=false
```

Chỉ chạy `ansible-playbook -i /root/multinode migrate-to-ovn.yml` sau khi reset
kết thúc với `OVS_4NODE_BASELINE_READY`. Reset phá hủy deployment/workload lab;
tham số trên giữ toàn bộ migration backup/evidence, gồm run lỗi trước đó.
Các mốc đo và phiên TCP cũ không được khởi tạo lại để nối vào một run đã gián đoạn.

## Phát triển và kiểm tra offline

Dependency cho helper controller nằm trong `requirements/runtime.txt`.
Kolla và dependency bên trong container tiếp tục do môi trường triển khai
quản lý. Bộ dependency kiểm thử dưới đây dành cho một venv riêng:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements/dev.txt

python tools/check_project.py
python tools/check_ansible.py
ruff check .
pytest -q
```

Phải activate venv vì một số helper gọi `ansible-playbook` qua `PATH`.
Các kiểm tra trên dùng fixture/local process, không thực hiện migration/reset
trên lab. [Hướng dẫn phát triển](docs/structure.md) giải thích source resolver,
agent được chép sang node và vị trí thêm code/test mới.
