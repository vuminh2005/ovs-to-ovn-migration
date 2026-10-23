# Chờ OVS agent và br-int sau reset

Lỗi `no row "br-int" in table Bridge` cho biết bridge chưa có khi helper
OpenFlow chạy. Trước đây reset gọi helper đó ngay sau deploy/post-deploy, rồi
mới kiểm tra container agent. Container đã được start không chứng minh Neutron
OVS agent đã hoàn tất khởi tạo bridge. Log hiện có chưa phân biệt được startup
chậm với lỗi khởi tạo kéo dài của agent.

Reset hiện dùng `scripts/reset/reset_ovs_readiness.py` trên mọi network/compute:

1. Chờ `openvswitch_db`, `openvswitch_vswitchd` và `neutron_openvswitch_agent`
   cùng running, không ở trạng thái restarting.
2. Chờ `ovs-vsctl br-exists br-int` thành công. Bridge do OVS agent tạo trong
   quá trình khởi tạo; helper readiness chỉ đọc trạng thái.
3. Sau khi mọi host qua gate, gọi helper OpenFlow hiện có rồi kiểm tra dịch vụ
   và tiếp tục phần workload của reset.

Gate có deadline 120 giây, poll cách 2 giây. Mỗi lệnh Docker có timeout tối đa
5 giây và dùng phần thời gian còn lại của deadline. Không thêm lượt restart
container, xóa flow hoặc tự tạo bridge trong bước readiness.

Nếu gate không đạt, reset thu thập trạng thái container, Docker logs, OVS state
và đuôi log `neutron-openvswitch-agent`, rồi dừng trước OpenFlow/workload.
Thông tin này giúp xác định nguyên nhân nếu bridge không xuất hiện vì agent lỗi.

Evidence trong `/root/kolla-reset-snapshots/<reset-id>/`:

- `ovs-readiness-<host>.json`: kết quả PASS, số lần poll và trạng thái container.
- `ovs-readiness-<host>.log`: kết quả FAIL cùng chẩn đoán startup khi gate lỗi.

File evidence có mode `0600`. Nội dung log startup không được in ra stdout
Ansible. JSON readiness chỉ giữ trạng thái, không xuất environment của container.

## Cập nhật và chạy lại

Bản sửa áp dụng trực tiếp trên `multinode-playbook.zip` bạn cung cấp. Toàn bộ
file trong `group_vars/` được giữ nguyên từng byte, gồm quota `true`/30 và các
giá trị demo, timeout đã chỉnh. Các test quota kiểm tra cả profile hiện tại,
bật/tắt rõ ràng và override mức yêu cầu.

Sau khi cài đầy đủ source vào thư mục sạch, chạy lại toàn bộ reset để tạo đúng
snapshot/facts. Reset này phá hủy deployment/workload lab và giữ backup cũ:

```bash
cd /root/ovs-to-ovn-migration
source /root/venvs/kolla-2024.1/bin/activate
ansible-playbook -i /root/multinode reset-lab-to-ovs.yml \
  -e reset_lab_confirm=true -e increase_quota=true -e instance_quota_limit=30 \
  -e reset_delete_migration_backups=false
```

Chỉ chạy migration mới sau `OVS_4NODE_BASELINE_READY`:

```bash
ansible-playbook -i /root/multinode migrate-to-ovn.yml \
  -e increase_quota=true -e instance_quota_limit=30
```

Kiểm chứng offline mô phỏng bridge xuất hiện muộn, bridge không bao giờ xuất
hiện, agent stopped và Docker bị treo. Ansible integration chạy task reset thật
trên ba host giả lập, kiểm tra chẩn đoán được lưu và các bước sau bị chặn khi
một host không sẵn sàng. Kết quả trong
[reset-ovs-readiness-validation.json](reset-ovs-readiness-validation.json) không
thay cho lần chạy reset trên lab.
