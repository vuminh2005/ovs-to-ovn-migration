# Tiếp tục sau lỗi kiểm tra topology cuối phase 07

Log `Mark DB migration end` thành công rồi task kiểm tra topology trả `141`
phù hợp với SIGPIPE ở pipeline `ovn-nbctl show | grep -Eq ...`. `grep -q` dừng
đọc khi thấy kết quả; producer có thể tiếp tục ghi và bị đóng pipe. `pipefail`
biến tình huống đó thành lỗi orchestration dù output đã có switch/router.
Điều này phụ thuộc lượng output và timing; không chứng minh DB topology thiếu.

Đã thay reader bằng `grep -E ... >/dev/null` để đọc hết stdout. Lỗi thật của
producer vẫn được giữ bởi pipefail, không bỏ guard hoặc dùng ignore-errors.
Các guard piped quiet-grep tương tự ở precheck/cutover/cleanup/validation cũng
được sửa; grep đọc trực tiếp file không cần đổi.

## Chạy tiếp đúng run

Tại thời điểm này Neutron đã freeze và DB migration đã chạy. Không chạy
`migrate-to-ovn.yml` lại từ đầu, không chạy lại phase 07 DB migrate, không reset
nếu mục tiêu là hoàn thành run và giữ evidence/phiên đo hiện có.

Tải ZIP hiện hành vào `/root/playbook-ns-ew-matrix.zip` trên controller, rồi
cài riêng các file cần sửa. Các group_vars, inventory, manifest, timestamp và
collector/probe process hiện tại được giữ.

```bash
cd /root/ovs-to-ovn-migration
source /root/venvs/kolla-2024.1/bin/activate
source /etc/kolla/admin-openrc.sh

python3 - <<'PY'
from pathlib import Path
from zipfile import ZipFile
files = [
    'resume-after-db.yml', 'scripts/post_db_recovery.py',
    'scripts/migration_report.py', 'scripts/report_format.py',
    'playbooks/02-precheck.yml', 'playbooks/07-migrate-db.yml',
    'playbooks/08-cutover.yml', 'playbooks/09-cleanup.yml',
    'playbooks/11-validate.yml',
]
with ZipFile('/root/playbook-ns-ew-matrix.zip') as archive:
    contents = {name: archive.read('ovs-to-ovn-migration-ns/' + name) for name in files}
for name, data in contents.items():
    Path(name).write_bytes(data)
print('POST_DB_RECOVERY_INSTALLED')
PY

ansible-playbook -i /root/multinode resume-after-db.yml --syntax-check
post_db_run_dir=$(python3 scripts/post_db_recovery.py select \
    /root/ovs-to-ovn-backup --inventory /root/multinode)
ansible-playbook -i /root/multinode resume-after-db.yml \
    -e "migration_resume_run_dir=$post_db_run_dir"
```

Dùng `set -euo pipefail` trong subshell khi chạy cả khối để dừng nếu một bước
không đạt. `select` chỉ chọn khi có đúng một checkpoint hợp lệ. Nếu trả zero
hoặc nhiều checkpoint, lệnh dừng và liệt kê trường hợp cần xác định, không đoán
thư mục theo timestamp. Nếu backup root đã override, thay đường dẫn parent
bằng root thực tế. Có thể dùng `inspect <đường-dẫn-run> --inventory ...` để xem
điều kiện của một checkpoint cụ thể.

## Guard và phạm vi tiếp tục

- Kiểm tra runtime/schema v2, inventory không đổi, các marker preparation/freeze/DB
  có thứ tự hợp lệ, log migrate `rc=0`, provider association đã PASS.
- Từ chối nếu phase 08/cutover, cleanup hoặc API restoration đã bắt đầu.
- Với workload bật, yêu cầu capture start ban đầu và TCP ARM/session cũng như
  baseline N-S có evidence; không tạo lại hoặc re-arm probe.
- Đọc đúng NB/SB endpoints từ `ml2_conf.ini.target` của run và phục hồi facts.
- Kiểm tra live `neutron_server` vẫn stopped trên mọi control node.
- Đọc lại topology NB/SB bằng reader không thoát sớm; chỉ khi đạt mới đóng phase
  07 và tiếp tục phase 08, 09, 10, 11, 12, 13.

Nếu checkpoint không hợp lệ hoặc topology thật không đạt, recovery dừng trước
cutover. Route `resume-after-cleanup.yml` cũ chỉ dành cho checkpoint đã takeover
sau phase 08, không dùng cho lỗi đang xảy ra.

## Metric sau recovery

Giữ nguyên `control_plane_downtime.start`, `db_migration.start/end`, capture
anchors và phiên TCP ban đầu. Không đổi mốc bắt đầu để làm số liệu ngắn lại.
`orchestration-recovery.json` ghi recovery; report TXT/JSON ghi nhận việc run
đã bị gián đoạn. Control-plane downtime và tổng thời gian run sẽ gồm thời gian
dừng xử lý lỗi; DB migration duration không bị chạy/ghi lại. Vì vậy cần phân
biệt run này với các lần chạy hoàn toàn liền mạch khi so sánh benchmark.

Kiểm tra offline: tái lập pipeline cũ `rc=141` với stdout 16 MiB, pipeline mới
PASS, producer rc=7 vẫn FAIL, thiếu NB/SB vẫn FAIL. Có tests checkpoint,
inventory/clock/phiên đo và Ansible thực tế cho phục hồi facts, server frozen,
bảo toàn timestamps và từ chối nếu server đã được khởi động lại.

Nguồn:
https://www.gnu.org/software/grep/manual/html_node/Usage.html
https://www.gnu.org/software/bash/manual/html_node/Exit-Status.html
