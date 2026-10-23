# Sửa parser OVS external_ids — 23/10/2026

Nguồn: bản migration theo inventory, phiên bản 4 của
`ovs-to-ovn-migration-audited.zip`. Giữ các thay đổi không phụ thuộc số node,
quota opt-in và workflow migration của bản đó.

## Nguyên nhân

`host_network.py` dùng `json.loads()` trực tiếp với kết quả
`ovs-vsctl get Open_vSwitch . external_ids:system-id`. Đây là biểu diễn OVSDB
dạng text: chuỗi dạng tên host có thể không có dấu ngoặc kép. Ví dụ OVS 3.3.9
trả `network1`, khiến JSON decoder lỗi ngay ký tự đầu; UUID lại có thể được
trả trong dấu ngoặc kép nên cùng parser vẫn chạy được với dữ liệu đó.

Traceback người dùng xác nhận parser lỗi tại `system-id`; không có output thô
trong log để khẳng định system-id thực tế là giá trị nào. `network1` là giá trị
tái hiện trong môi trường kiểm thử, không phải kết quả đã đọc từ lab.

Tham chiếu: [ovs-vsctl manual](https://www.openvswitch.org/support/dist-docs/ovs-vsctl.8.html),
phần Table Formatting Options và Database Values. Tùy chọn format bảng áp dụng
cho `list`/`find`; không chỉ thêm `--format=json` vào lệnh `get` cũ.

## Thay đổi

Helper yêu cầu bảng JSON tường minh:

```bash
docker exec openvswitch_vswitchd ovs-vsctl --timeout=10 \
  --format=json --columns=external_ids list Open_vSwitch .
```

Parser kiểm tra một row duy nhất, cột `external_ids`, kiểu OVSDB map và
key/value dạng chuỗi; dữ liệu sai hoặc key trùng đều báo lỗi. Key thiếu trả
chuỗi rỗng để các kiểm tra system-id/tunnel/mapping hiện có tiếp tục từ chối
cấu hình thiếu. Lỗi Docker/OVSDB/timeout được giữ nguyên, không bị biến thành PASS.

Áp dụng cho `system-id`, `ovn-encap-ip`, `ovn-bridge-mappings` ở cả source
precheck Phase 02 và target validation Phase 11. Helper chỉ đọc dữ liệu;
không ghi external_ids hoặc sửa bridge/flow/interface.

## Kiểm chứng

- Toàn bộ bộ test: **680 PASS**, 587 cảnh báo deprecation từ dependency.
- Sau khi chỉnh fixture phản ánh chuỗi trần dạng tên host: **24 regression test PASS**.
- **16/16** Ansible entrypoint/transport qua kiểm tra cú pháp; static checks và Ruff PASS.
- OVS 3.3.9 / schema 8.5.1 thật với OVSDB tạm riêng: tái hiện JSONDecodeError
  với `system-id=network1`; parser mới đọc đúng tên host, UUID, IP, nhiều
  physnet và key thiếu. Source/target inspection đều PASS; đối chiếu cấu hình
  trước/sau helper không thay đổi.

Test topology chạy các task Ansible thực tế với mô phỏng lab 4 node và cụm
20 node. Kiểm tra OVS thật dùng ovs-vsctl/ovsdb-server cục bộ, mô phỏng IP
interface; không kết nối Docker, SSH hoặc OpenStack lab.
Chi tiết máy đọc: [host-network-validation.json](host-network-validation.json).

## Chạy lại khi dừng đúng tại precheck này

Lỗi Phase 02 này xảy ra trước tạo workload và control-plane downtime. Thay
source bằng toàn bộ bản ZIP mới rồi chạy migration từ đầu, không cần reset lab
chỉ vì lỗi parser này. Run cũ và backup giữ nguyên; lệnh dưới tạo run mới:

```bash
cd /root/ovs-to-ovn-migration
source /root/venvs/kolla-2024.1/bin/activate
ansible-playbook -i /root/multinode migrate-to-ovn.yml
```
