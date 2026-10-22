# OpenStack ML2/OVS → ML2/OVN migration

Playbook cho lab Kolla-Ansible 2024.1, giữ workflow Phase 00–13 và bộ đo
E-W/N-S của bản `playbook new stage.zip`. Bản audit ngày 22/10/2026 sắp xếp
lại source, cập nhật import/path và bổ sung kiểm tra cấu trúc; kết quả kiểm
chứng nằm trong [báo cáo audit](docs/audit-current.md).

## Bắt đầu từ đâu

| Mục đích | File/thư mục |
|---|---|
| Migration đầy đủ | [migrate-to-ovn.yml](migrate-to-ovn.yml) |
| Reset lab về OVS | [reset-lab-to-ovs.yml](reset-lab-to-ovs.yml) |
| Phase theo thứ tự thực thi | [playbooks/](playbooks/) — `00-*.yml` đến `13-*.yml` |
| Resume có kiểm tra checkpoint | [playbooks/recovery/](playbooks/recovery/README.md) |
| Task dùng lại / playbook vận chuyển helper | `playbooks/tasks/`, `playbooks/transport/` |
| Python theo chức năng | [scripts/](scripts/README.md) |
| Callback và filter Ansible | [plugins/](plugins/README.md) |
| Test theo chức năng | [tests/](tests/README.md) |
| Retry chuyên biệt và tài liệu cũ | [archive/](archive/README.md), `docs/history/` |
| Cấu trúc và bản đồ đường dẫn cũ → mới | [docs/structure.md](docs/structure.md), [docs/path-map.tsv](docs/path-map.tsv) |

`group_vars/` và `inventory.example` giữ vị trí quy ước của Ansible.
Hai entrypoint migrate/reset ở gốc để giữ nguyên lệnh vận hành quen thuộc.
Các file trong `archive/` không được import bởi migration/reset mặc định.

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
```

Reset xóa deployment/workload lab hiện tại và dựng baseline OVS. Cờ xác nhận
reset và việc giữ migration backup hoạt động như bản gốc:

```bash
ansible-playbook -i /root/multinode reset-lab-to-ovs.yml \
  -e reset_lab_confirm=true
```

Sau khi reset kết thúc với `OVS_4NODE_BASELINE_READY`:

```bash
source /etc/kolla/admin-openrc.sh
ansible-playbook -i /root/multinode migrate-to-ovn.yml
```

Reset bật và xác minh OpenFlow15 trên `br-int` của network/compute trước khi
tạo workload. Phase 07 chỉ bổ sung protocol còn thiếu trước downtime; nếu đã
đủ thì không ghi cấu hình. Phase 08 chỉ xác minh, không thay đổi protocol.

Mặc định reset và migration không thay đổi quota project (`increase_quota: false`).
Để nâng instance quota lên tối thiểu 20, thêm `-e increase_quota=true` vào lệnh.
Đổi mức yêu cầu bằng `-e instance_quota_limit=30`. Không hạ quota cao hơn hoặc
không giới hạn, không đổi cores/RAM. Xem [hướng dẫn quota](docs/project-quota.md).
Capacity vẫn kiểm tra sức chứa workload theo quota thực tế.

Giữ nguyên profile lab trong `group_vars/all.yml`, `group_vars/ns-lab.yml` và
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

Resume đã chuyển vào `playbooks/recovery/`. Ví dụ với checkpoint phù hợp:

```bash
ansible-playbook -i /root/multinode playbooks/recovery/resume-after-db.yml \
  -e migration_resume_run_dir=/root/ovs-to-ovn-backup/<run-id>
```

Chọn đúng route trong [hướng dẫn recovery](playbooks/recovery/README.md).
Các route giữ inventory, UUID, mốc đo và phiên probe cũ; không tự chọn run mới
nhất hoặc chạy lại DB migration. Reset còn nhận diện probe dùng đường dẫn
`scripts/ns_probe.py` cũ của cùng checkout khi checkpoint đã mất.

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
