# Offline regression suite

`validation/test_ping_only.py` kiểm tra profile chỉ ping: không gọi nhánh tạo
VM B/C/D, capacity 6 VM, recovery/rollback không đòi guest health, gate capture
vẫn chặn thiếu evidence, report không đòi phép đo đã tắt. Socket local kiểm tra
xuất log ICMP gốc qua control channel khi không có TCP application streams.

Các test chia thành `common/`, `contracts/`, `migration/`, `validation/`,
`diagnostics/`, `reporting/`, `reset/`, `recovery/`. `fixtures/` giữ dữ liệu mẫu;
`support/` giữ đường dẫn dùng chung của test.

```bash
source .venv/bin/activate
pytest -q
pytest -q tests/reset
pytest -q tests/diagnostics/test_filter_discovery.py
```

Test dùng API giả/local HTTP, SQLite, loopback TCP và Ansible local; không trỏ
vào inventory cloud thực. Một số test spawn process và mô phỏng ranh giới
PID/procfs của môi trường kiểm thử. Chúng không thay cho kiểm chứng ownership,
traffic và container trên lab thật.

Khi di chuyển source, chạy `tests/common/test_project_layout.py` cùng test
filter discovery, included-helper paths, callback timing và recovery. Giữ các
assertion về fail/UNAVAILABLE, ownership, UUID, mốc đo và nội dung report.

`archive/tests/` giữ nguyên snapshot các test resume cũ để đối chiếu lịch sử;
không nằm trong `testpaths` của bộ test hiện hành. Các test contract hiện hành
kiểm tra phase 06 duy nhất và route archived dừng trước mọi thao tác host.

`tests/recovery/test_rollback.py` có guard drift/container/inventory/VM,
checkpoint restore-test và Neutron-only import, cùng kiểm thử thứ tự barrier
qua Ansible local. DB/host transport được giả lập; chưa kiểm thử MariaDB image
hoặc dataplane trên lab. Chạy riêng bằng `pytest -q tests/recovery/test_rollback.py`.

Các ca Ansible chạy mặc định bằng run-dir duy nhất, tự lấy hash, preparation-only,
rerun tự verify, và chặn trước mutation khi disable execution/maintenance,
business drift hoặc hash sai. Inspect cần chọn rõ `rollback_mode=inspect`.

`test_nova_checkpoint.py` dùng SQLite mở read-only để kiểm tra toàn bộ mapping/
cell inventory và drift, mock MySQL session để xác minh READ ONLY trước SELECT,
bootstrap không RPC/DB configuration, và chặn rò DSN khi lỗi. Không thay thế
việc kiểm thử image Nova/MariaDB thật trong lab.

Test Nova còn chạy task stage phase 01 thật qua Ansible local, dùng reader đã
stage tạo receipt rồi kiểm tra preflight/freeze. Cách chép cũ làm mất newline
và tái hiện lỗi hash; cách mới giữ nguyên byte cả bốn helper. Gate preflight
thật được chạy với hash đúng/sai, xác nhận lỗi dừng trước sentinel stop API.
