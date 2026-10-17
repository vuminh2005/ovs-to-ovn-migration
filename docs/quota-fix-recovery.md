# Tiếp tục validation sau lỗi quota project_id

Lỗi `KeyError: 'project_id'` xảy ra ở helper quota, sau khi Kolla deploy và
kiểm tra uplink đã thành công. SDK 3.x dùng QuotaSet/URI attributes khi update;
truyền project ID đơn lẻ không cung cấp URI `project_id`. SDK 4.21 nhận project
ID theo chữ ký mới, nên các kiểm tra trên phiên bản đó trước đây không bắt lỗi.

Helper đã sửa thành:

```python
cloud.compute.update_quota_set(project, project_id=project, instances=20)
```

Tham số `project_id` chỉ định URL của Nova. Request PUT thực tế vẫn chỉ gửi
`{"quota_set":{"instances":20}}`, không gửi cores/RAM hay project_id trong body.
Đã kiểm tra GET/PUT thật trên HTTP server localhost với SDK 3.0.0 và 4.21.0.

Trên controller, cập nhật một dòng trong `scripts/extended_validation.py`, hoặc
lấy script đã sửa từ ZIP. Sau đó tiếp tục từ task quota, truyền snapshot đã tạo
bởi lần reset đang bị dừng:

```bash
cd /root/ovs-to-ovn-migration
source /root/venvs/kolla-2024.1/bin/activate

ansible-playbook -i /root/multinode reset-lab-to-ovs.yml \
    --start-at-task "Set only instance quota to 20 in the authenticated workload project" \
    -e reset_run_dir=/root/kolla-reset-snapshots/20261013T075659131026384
```

Lệnh chạy chín task validation cuối cùng: quota, cấu hình MTU, smoke VXLAN,
agent/resource evidence và chuẩn bị image/flavor rồi ghi completion marker.
Nó sử dụng snapshot của run này; không tạo một reset run mới. Không cần nâng
cấp OpenStackSDK trên controller.

Chỉ chạy migration sau khi validation kết thúc `OVS_4NODE_BASELINE_READY` và
không còn task lỗi. Nếu `reset-complete.json` đã tồn tại, không chạy lại helper
finish; completion guard sẽ từ chối vì run đó đã hoàn tất.

Nguồn chữ ký SDK:
https://docs.openstack.org/openstacksdk/2024.1/user/proxies/compute.html
https://docs.openstack.org/openstacksdk/latest/user/proxies/compute.html
