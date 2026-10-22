# Tiếp tục sau DB migration, trước cutover

Dùng `playbooks/recovery/resume-after-db.yml` khi DB migration đã hoàn tất
thành công nhưng phase 08 chưa bắt đầu. Dùng đầy đủ source của cùng phiên bản;
không ghép vài helper từ một ZIP khác vào run đang freeze.

```bash
cd /root/ovs-to-ovn-migration
source /root/venvs/kolla-2024.1/bin/activate
python3 scripts/recovery/post_db_recovery.py inspect \
  /root/ovs-to-ovn-backup/<run-id> --inventory /root/multinode
ansible-playbook -i /root/multinode playbooks/recovery/resume-after-db.yml \
  -e migration_resume_run_dir=/root/ovs-to-ovn-backup/<run-id>
```

Guard yêu cầu schema 2, inventory/topology không đổi, marker freeze/DB có thứ
tự hợp lệ, DB log `rc=0`, provider verification PASS và preparation gốc được
tạo trước freeze. Neutron server phải còn dừng. Capture, TCP ARM và N-S baseline
được kiểm tra khi workload tương ứng bật.

Route xác minh NB/SB rồi chạy phase 08–13; không chạy lại DB migrate, freeze
hoặc ARM. Timestamp gốc giữ nguyên; downtime và tổng thời gian run gồm cả
khoảng chờ xử lý lỗi. Từ chối nếu cutover, cleanup hoặc API restoration đã bắt đầu.

`select <backup-root> --inventory ...` chỉ trả run khi có đúng một checkpoint
phù hợp. Nếu không, chỉ định và kiểm tra run cụ thể; không chọn theo timestamp.

Ghi chú SIGPIPE và cách cài file cho bản cũ được giữ tại
[bản gốc lưu trữ](../../archive/docs/post-db-recovery-original.md).
Xem [toàn bộ route recovery](../../playbooks/recovery/README.md).
