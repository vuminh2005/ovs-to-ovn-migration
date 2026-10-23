# Offline regression suite

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
