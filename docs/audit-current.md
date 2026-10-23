> Sửa readiness OVS sau reset trên ZIP người dùng cung cấp: xem
> [reset-ovs-readiness.md](reset-ovs-readiness.md). Toàn bộ `group_vars` của ZIP
> đó được giữ nguyên; bản ghi kiểm chứng nằm trong
> [reset-ovs-readiness-validation.json](reset-ovs-readiness-validation.json).
>
> Cập nhật 23/10/2026: phase 06 đã gộp vào một file; các route resume thủ công
> chuyển vào [archive](../archive/README.md). Kết quả kiểm chứng bản hiện hành
> nằm trong [metadata-validation.json](metadata-validation.json). Phần dưới là
> hồ sơ audit nền ngày 22/10 và mô tả layout tại thời điểm đó.
>
> Cập nhật migration theo inventory ngày 23/10/2026 được ghi riêng tại
> [node-independent-migration.md](node-independent-migration.md). Các số liệu 655 test
> dưới đây thuộc bản nền ngày 22/10, không phải lượt kiểm tra của bản cập nhật.

# Audit source — 22/10/2026

## Bản được kiểm tra và phạm vi

Nguồn duy nhất: `playbook new stage.zip`, SHA-256
`af89b1e98b542e783aa6f21dd19ab7f129590c7d277af1e79c09a8b4adec4f0a`.
Archive đầu vào có 175 file, gồm 102 Python và 38 YAML. 
Audit bao gồm kiểm kê toàn bộ file, cú pháp/import, quan hệ source → Ansible →
helper được staged, đường dẫn resume/retry, plugin discovery, tên/thứ tự phase,
ownership khi cleanup, checkpoint/evidence, công thức đo và chạy test offline.
Các luồng có rủi ro cao được đối chiếu với test: stop process theo identity,
xóa tài nguyên sở hữu bởi run, resume giữ phiên/mốc đo và trạng thái thiếu
evidence phải FAIL/UNAVAILABLE. Đây không phải kết quả chạy migration trên lab.

## Kết quả và thay đổi

### Tùy chọn tăng quota theo yêu cầu mới

Thêm `group_vars/quota.yml`: `increase_quota: false`, `instance_quota_limit: 20`.
Reset và phase 04 gọi task quota chỉ khi bật cờ. Helper chung đọc/PUT/đọc lại
quota của project đang xác thực, chỉ sửa instances và không hạ mức cao hơn hoặc
không giới hạn. Capacity trong `extended_validation.py` vẫn chỉ đọc quota.
Evidence được ghi khi bật tùy chọn. Xem [lệnh chạy](project-quota.md).

Test có các trường hợp default tắt, bật mức mặc định/tùy chỉnh, quota đã đủ,
unlimited, thiếu project scope, lỗi quyền và xác minh cores/RAM. Các task reset
và migration thực tế được chạy qua Ansible local; SDK HTTP test kiểm tra payload
chỉ chứa instances và đúng project URL. OpenFlow và gateway workflow không đổi.

### Cập nhật sau lỗi reset trên lab

Reset dừng tại task quota do Python nhúng chỉ thêm `scripts/` vào `sys.path`
rồi import `extended_validation` theo layout phẳng. Đợt audit đầu chưa bắt
được dạng bootstrap truyền đường dẫn qua argv này. Theo yêu cầu mới, đã bỏ
toàn bộ task đổi quota khỏi reset và bỏ helper/đường gọi tự đổi quota trong
migration. Capacity chỉ đọc quota project một lần và vẫn dừng khi thiếu.

Static checker nay kiểm tra bootstrap trước mọi import helper trong Python
nhúng; regression test tái lập đúng mẫu lỗi argv. SDK/HTTP test chứng minh
capacity chỉ GET quota và không PUT, cả khi quota đủ và thiếu.

Reset khai báo rõ chế độ chuẩn bị OpenFlow (`openflow_check_only: false`) cho
network/compute trước workload. Helper giữ bridge/protocol cũ, chỉ ghi protocol
khi thiếu; test bổ sung chứng minh prepare chạy lặp không ghi lại. Phase 08
vẫn chỉ kiểm tra, không thay đổi tên task hoặc thứ tự.

### Đợt sắp xếp source ban đầu

| Mức | Phát hiện | Xử lý |
|---|---|---|
| Vừa | `migration_report.py` thực thi đọc/ghi ngay khi import; `network_semantics.py` đọc argv và có thể mở kết nối cloud ngay khi import | Đưa thực thi vào `main()` và guard `__main__`; giữ phép tính/điều kiện và CLI hợp lệ. Kiểm tra import cả 48 module từ cwd khác, thêm test before/after semantics |
| Vừa | Đường dẫn source phụ thuộc sibling/`parent.parent`, rải trong Python, YAML, plugin và Python inline; dễ gãy khi thêm/di chuyển file | Registry `_paths.py`, cập nhật call site và static path checks; test thật chuỗi Python → Ansible → staged helper |
| Vừa | Đổi vị trí `ns_probe.py` có thể làm reset bỏ sót orphan được khởi chạy ở vị trí cũ khi checkpoint đã bị xóa | Chỉ nhận thêm đường dẫn cũ của cùng checkout; vẫn kiểm tra argv/config/backup scope, identity và pidfd; không nhận probe của checkout khác |
| Nhẹ | 47 helper và 52 module test ban đầu nằm phẳng; tên file khó thể hiện trách nhiệm | Chia tám nhóm source và tám nhóm test; dùng đường dẫn ROOT chung cho test và import liên nhóm rõ ràng |
| Nhẹ | Resume và retry chuyên biệt cùng nằm ở gốc; tài liệu có lệnh cài lẻ file từ ZIP cũ | Resume có guard vào `playbooks/recovery/`; hai retry vào `archive/playbooks/`; giữ guard và cập nhật tham chiếu. Tách hướng dẫn hiện hành khỏi bản ghi lịch sử |
| Nhẹ | Plugin theo loại nằm rời ở gốc; child playbook cần cơ chế discovery riêng | Gom implementation vào `plugins/`; giữ hai shim nhỏ cạnh playbook để nạp cùng filter khi chạy ngoài project cwd |
| Nhẹ | Thiếu manifest dependency kiểm thử và lệnh kiểm tra source/path dùng lại được | Thêm `requirements/`, pytest/ruff config, `tools/check_project.py`, `tools/check_ansible.py` và hướng dẫn venv riêng |

128 file đầu vào được di chuyển. [Bản đồ đường dẫn](path-map.tsv) ghi từng file;
[structure.md](structure.md) giải thích vị trí thêm code/test mới. Không xóa
retry hoặc lịch sử chỉ vì chúng không nằm trong luồng mặc định.

## Các hợp đồng được giữ

- `group_vars/all.yml`, `group_vars/ns-lab.yml`, `group_vars/reset.yml` và
  `inventory.example` giống bản gốc từng byte.
- Tên play của cả 14 phase và thứ tự các task cũ giữ nguyên, gồm task OpenFlow
  phase 08. Phase 04 thêm một task tăng quota có cờ bật rõ ràng trước workload.
  Không thêm gateway synchronization/readiness gate.
- `run_layout.py` giống bản gốc từng byte: không chuyển dữ liệu run hay nâng
  schema của checkpoint lịch sử.
- `report_format.py` giống bản gốc từng byte. Các khóa Pair A–D, format TXT,
  JSON evidence và điều kiện PASS/FAIL/UNAVAILABLE giữ nguyên.
- AST của `probe_metrics`, `pcap_metrics` và các lớp `Stats`, `Client`, `Server`
  trong N-S probe không đổi. `ns_icmp.py` và `ns_icmp_reconcile.py` không đổi
  từng byte. Việc bọc CLI vào `main()` không đổi thuật toán report/semantics.
- Helper trên node và guest giữ basename/argv cũ, companion files và ownership
  allowlist. Không cần chép cả source controller vào thư mục evidence.

## Kiểm chứng

**655 test PASS, không có test bỏ qua; 15/15 entrypoint/transport qua kiểm tra
cú pháp Ansible.** 575 cảnh báo là deprecation của dependency trong bộ test.
Bản mới có 122 file Python và 40 YAML; static checks đối chiếu 270 tham chiếu
source, 62 liên kết tài liệu hiện hành, 114 khối Bash và 13 khối Python nhúng.

Kết quả máy đọc nằm trong [audit-validation.json](audit-validation.json).
Lệnh tái lập từ venv đã activate:

```bash
python tools/check_project.py
python tools/check_ansible.py
ruff check .
pytest -q
```

- Bộ gốc có 636 test. Lượt đầu thiếu `ansible-playbook` trong PATH gây ba lỗi
  môi trường; chạy lại các ca tương ứng với đúng PATH đều PASS.
- Đợt audit đầu bổ sung bảy test. Đợt sửa quota thay bốn test chỉnh quota
  bằng test chỉ đọc và bổ sung ba regression test. Đợt quota opt-in bổ sung
  chín test policy/Ansible/SDK HTTP. Tổng hiện tại là 655.
  Phạm vi gồm isolated import,
  remote/guest bundles, old-path orphan ownership, callback abort cho mọi route,
  network semantics CLI và task include OpenFlow ở cả root/reset lẫn phase.
- 15 entrypoint/transport qua Ansible syntax check; migration entrypoint import
  đầy đủ Phase 00–13. Task include và YAML còn lại được parse/kiểm tra path,
  các luồng nhạy với context còn có real-Ansible local integration.
- Ruff kiểm tra undefined/unbound names (`F821`, `F822`, `F823`), không áp một
  đợt format/lint style lên toàn bộ source cũ.
- Static checks kiểm tra Python/YAML, duplicate YAML key, source path, Bash và
  Python nhúng. Liên kết lịch sử trong `docs/history/`, `archive/docs/` được
  giữ làm bản ghi, không tính như hướng dẫn hiện hành.

Test dùng local HTTP/SQLite, loopback sockets, process và fake cloud/container
commands. Một số test mô phỏng PID/procfs theo môi trường executor. Chưa chạy
reset, migration, SSH hay thao tác OpenStack API trên lab thật trong lần audit này.

## Phần nên phát triển tiếp

1. `workload_validation.py` vẫn là module điều phối lớn, khoảng 1.450 dòng;
   `extended_validation.py` khoảng 830 dòng. Nên tách state/evidence validation
   khỏi cloud resource I/O theo từng thay đổi có test, tránh tăng thêm nhánh vào
   hai module này. Đợt này chỉ nhóm module, không viết lại workflow đang dùng.
2. Một số test vẫn tái sử dụng fixture từ module test khác. Có thể chuyển dần
   fixture sang `tests/support/`; chưa di chuyển toàn bộ trong cùng đợt để giữ
   việc đối chiếu test cũ rõ ràng.
3. Khi thêm tính năng tiếp theo, chạy lại chu kỳ reset → migrate trên lab và
   đối chiếu metric/evidence; test offline chưa chứng minh được hành vi thực
   của OVS/OVN, container, MTU/DHCP hoặc gateway trên deployment.

## Dùng bản đã sắp xếp

Dùng toàn bộ source cùng phiên bản trong thư mục sạch; giải nén đè để lại các
file source cũ. Giữ cấu hình, inventory và thư mục run/backup ngoài source.
Hai lệnh migrate/reset ở gốc không đổi. Các route resume cũ đã chuyển archive;
script ngoài project cần đối chiếu bảng path. Hướng dẫn hiện hành tại
[README](../README.md).
