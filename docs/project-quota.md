# Tăng instance quota khi được bật rõ ràng

Mặc định trong `group_vars/quota.yml`:

```yaml
increase_quota: false
instance_quota_limit: 20
```

Hai biến dùng chung cho reset và migration đầy đủ. `false` bỏ qua task quota;
capacity của workload vẫn chỉ đọc và kiểm tra quota hiện có. `true` nâng quota
instance của project đang xác thực lên **ít nhất** mức yêu cầu. Quota hiện tại
cao hơn hoặc không giới hạn (`-1`) được giữ nguyên; cores/RAM không bị sửa.

## Lệnh chạy

```bash
cd /root/ovs-to-ovn-migration
source /root/venvs/kolla-2024.1/bin/activate

# Migration: bật tăng instance quota lên tối thiểu 20.
ansible-playbook -i /root/multinode migrate-to-ovn.yml -e increase_quota=true

# Ví dụ yêu cầu mức khác.
ansible-playbook -i /root/multinode migrate-to-ovn.yml \
  -e increase_quota=true -e instance_quota_limit=30
```

Nếu chạy lại toàn bộ reset lab, có thể bật ở lần reset đó:

```bash
ansible-playbook -i /root/multinode reset-lab-to-ovs.yml \
  -e reset_lab_confirm=true -e increase_quota=true
```

Mỗi lệnh là một lần chạy riêng. Không truyền flag thì không tăng quota;
quota đã được nâng trong reset vẫn giữ trong cloud khi chạy migration sau đó.
`instance_quota_limit` phải là số nguyên dương; chỉ đặt limit mà không bật
`increase_quota` không làm phát sinh cập nhật.

## Thực thi và evidence

Reset gọi helper ở phần final source-cloud validation. Migration gọi ở phase
04, trước khi tạo workload và trước downtime. Cả hai dùng cùng task include
và `scripts/common/project_quota.py`; không import helper từ Python nhúng.

Helper đọc project quota, chỉ PUT trường `instances` nếu cần nâng, rồi đọc lại
để xác minh mức mới và cores/RAM. Lỗi quyền API hoặc kết quả không khớp làm
task dừng; không tự retry hoặc đánh dấu PASS khi cập nhật bị từ chối.

Khi bật, evidence `quota-instances.json` được ghi trong snapshot reset hoặc
thư mục evidence của run migration. File ghi project, requested limit,
before/after, changed và status, không ghi credential. Chạy lại khi quota đã
đủ không gửi PUT. Quota networks/ports/routers/security groups không nằm trong
tùy chọn này.
