# Cấu trúc source và quy tắc phát triển

## Luồng đọc code

1. `migrate-to-ovn.yml` liệt kê Phase 00–13. Tên `Phase NN` được callback và
   timeline sử dụng.
2. `playbooks/NN-*.yml` điều phối theo phase. `tasks/` chứa task include;
   `transport/` chứa playbook độc lập được Python gọi để copy/chạy/fetch helper.
3. `scripts/validation/workload_validation.py` điều phối workload; các nhóm
   migration, diagnostics, reporting, reset, recovery xử lý phần tương ứng.
4. `scripts/common/run_layout.py` quyết định vị trí dữ liệu run.
5. `scripts/reporting/migration_report.py` tạo báo cáo từ evidence.

## Vị trí code và test

| Source | Trách nhiệm | Test |
|---|---|---|
| `scripts/common/` | Run layout, phase schema, diagnostic policy, shared-external ownership | `tests/common/`, shared-external tests |
| `scripts/migration/` | Preparation/OpenFlow/image, provider associations, source topology, network semantics | `tests/migration/`, `tests/contracts/` |
| `scripts/validation/` | Workload orchestration, placement, prerequisites, TCP, readiness, chờ phục hồi | `tests/validation/`, `tests/recovery/` |
| `scripts/probes/` | Chương trình guest và TCP/ICMP control helpers | validation/diagnostics tests |
| `scripts/diagnostics/` | PCAP, host observers, log transfer/reconcile, OVN evidence | `tests/diagnostics/` |
| `scripts/reporting/` | Tạo report, timeline, TXT renderer, N-S summary | `tests/reporting/` |
| `scripts/reset/` | Demo/baseline, cleanup, underlay, RabbitMQ | `tests/reset/`, shared-demo tests |
| `scripts/recovery/` | Guard checkpoint và ghi nhận resume | `tests/recovery/` |

Danh sách từng file cũ/mới nằm trong [path-map.tsv](path-map.tsv).
`plugins/callback/` và `plugins/filter/` phân loại plugin theo convention
Ansible. Các shim cạnh playbook chỉ nạp filter chung, không sao chép logic.

## Source controller và helper được chép sang node

`scripts/_paths.py` chứa registry tên helper duy nhất. `configure()` nạp các
nhóm vào import path; `script_path()` và `playbook_path()` tìm source/transport
từ vị trí checkout, không dựa vào cwd. Helper vẫn chạy trực tiếp được từ
thư mục khác bằng đường dẫn tuyệt đối.

Đoạn bootstrap chỉ hoạt động trong source có `_paths.py`. Khi Ansible/cloud-init
chép file phẳng sang node/guest, module dùng sibling import như trước. Không
cần chép resolver hoặc toàn bộ project sang node.

Khi thêm helper:

1. Chọn nhóm phù hợp, đăng ký trong `SCRIPT_FILES`; basename phải duy nhất.
2. Module controller cần import chéo dùng bootstrap của module hiện có.
   Helper chỉ dùng standard library có thể giữ độc lập.
3. Dùng `script_path()` khi đọc source khác và `playbook_path()` khi gọi
   transport. Tránh `cwd` và chuỗi `parent.parent` phụ thuộc vị trí file.
4. Module vừa chạy controller vừa được staged chỉ import resolver trong
   nhánh controller. Giữ self-launch bằng `__file__` trên node để ownership khớp.
5. Stage từ source có nhóm nhưng giữ destination basename cũ. Cập nhật đủ
   companion files, allowlist và test bundle. Không đổi checkpoint/argv của
   process như một tác dụng phụ của việc di chuyển file.

Không có compatibility wrapper cho 47 source path cũ: script bên ngoài cần
cập nhật theo bảng đường dẫn. Riêng reset nhận diện probe ở đường dẫn cũ
của **cùng checkout**, vì process có thể còn chạy khi checkpoint bị xóa.
Điều đó không cho phép resume thiếu checkpoint.

## Đường dẫn Ansible

- `import_playbook`, `include_tasks`, `vars_files` và source của `script`
  được đối chiếu với vị trí file Ansible chứa chúng.
- `playbook_dir` thuộc play đang chạy, không đổi thành thư mục của task include.
  Reset ở gốc và phase trong `playbooks/` có context khác nhau. Giữ đúng
  context khi viết shell/Python inline.
- Giữ `group_vars/` theo convention; khai báo `vars_files` khi thêm recovery.
  Chạy từ gốc project để nạp đầy đủ `ansible.cfg`.
- Giữ `Phase NN`, nhãn `[SAVE]` cho ghi file và các khóa dữ liệu lịch sử.

## Tài liệu và kiểm tra

Thêm hướng dẫn hiện hành vào `docs/README.md`. Ghi chú sự cố/patch cũ vào
`docs/history/` hoặc `archive/docs/`. Retry chuyên biệt không trở thành route
chung chỉ vì còn được kiểm tra cú pháp.

Chạy bốn lệnh kiểm tra trong README. `check_project.py` kiểm tra mọi Python/YAML,
duplicate key, tham chiếu source tĩnh, Bash/Python nhúng và liên kết tài liệu.
Nó không đánh giá đầy đủ Jinja runtime hay trạng thái cloud; Ansible integration
và kiểm chứng lab giải quyết các lớp đó.
