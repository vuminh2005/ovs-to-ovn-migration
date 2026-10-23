# Chuẩn bị metadata trước downtime — 23/10/2026

Bản này thay đổi riêng cách chuẩn bị/start OVN metadata. Gateway scheduling
vẫn theo luồng hiện có; chưa có helper gán gateway sớm trong bản này.

## Thứ tự thực thi

| Thời điểm | Công việc |
|---|---|
| Image preflight hiện có | Bảo đảm image metadata có sẵn theo cấu hình từng host |
| Phase 06 | Genconfig; tạo container OVN metadata và systemd unit bằng API Kolla, giữ container `created`, unit `disabled` |
| Phase 07 trước freeze | Xác minh container chưa chạy, unit chưa enabled và cấu hình/identity vẫn khớp |
| Đầu phase 08 | Kiểm tra mọi metadata host; trên từng host dừng/disable metadata cũ, xác minh nó không còn chạy rồi enable/start metadata mới ngay |
| Tiếp theo trong phase 08 | Stop agent OVS/DHCP/L3 còn lại, kiểm tra OpenFlow, deploy controller; metadata đã chạy và tự chờ chassis/binding |
| Phase 09–10 | Cleanup cũ rồi restart Neutron theo luồng hiện có |
| Phase 11 | Kiểm tra metadata đăng ký trên đúng `Chassis_Private`, container chạy và healthcheck đạt; phase 12 tiếp tục kiểm tra metadata từ VM như cũ |

Bước start metadata không chờ agent đăng ký hoặc HTTP metadata sẵn sàng,
vì controller chưa được start ở thời điểm đó. Các bước chờ metadata nằm sau
khôi phục API, không kéo dài đường điều phối cleanup/restart API.

Không chạy hai lệnh Kolla deploy song song. Không còn gọi `deploy --tags neutron
--limit neutron-ovn-metadata-agent` trong phase 08. Start container và controller
thuộc cùng giai đoạn chuyển giao; đây không phải cam kết hai tiến trình khởi
động đúng cùng một timestamp.

## Giữ đúng cấu hình Kolla

`prepare_metadata.py` sao chép tạm cây Ansible của Kolla đang cài. Nó giữ
nguyên genconfig, role defaults, globals/globals.d, inventory/host overrides,
và các tham số container từ handler metadata gốc: image, volume, dimension,
healthcheck, privileged và common options.

Adapter `plugins/modules/migration_metadata_container.py` gọi hàm xử lý tham
số và Docker/systemd worker của Kolla. Nó dùng thao tác **create**, không dùng
start/restart, và ngăn `create_unit_file()` tự enable service. Sau đó xác minh
unit disabled và Docker restart policy không thể tự start container. Source
Kolla đã cài không bị sửa. API/handler không tương thích sẽ làm preparation
thất bại trước freeze.

Image chưa local thì preparation dừng để sửa image preflight, không pull trong
cutover. Các host đích là `neutron-ovn-metadata-agent`; không ghi cứng network1
hay số compute, và không phụ thuộc cờ bật workload validation.

## Chuyển giao socket và checkpoint

Kolla 2024.1 mặc định cho metadata cũ/mới cùng dùng vùng socket. Vì vậy container
mới không được chạy trước cutover. Phase 08 dừng/disable unit cũ trên cùng host
và kiểm tra Docker đã dừng trước khi start container mới. Unit cũ còn tồn tại
nhưng container tạm vắng vẫn bị disable để tránh nó tự quay lại chiếm socket.
Compute không có metadata cũ thì bỏ qua thao tác stop tương ứng.

Preparation lưu label run/config SHA256, container ID, image ID và fingerprint
cấu hình. Không lưu nội dung password vào evidence. Config hoặc container bị
thay sau preparation sẽ chặn handoff. Container đã chạy hoặc thuộc run khác
không bị tự stop/xóa/adopt. Retry trước freeze chỉ tái sử dụng container chưa
chạy thuộc đúng run; nếu cấu hình chuẩn bị thay đổi thì tạo lại container đó.

Evidence trên deployment host:

- `evidence/metadata-preparation.json`: kết quả tổng hợp và identity từng host.
- `evidence/metadata-preparation/<attempt>/<host>.json`: checkpoint từng host.
- `evidence/metadata-preparation/<attempt>/kolla-prepare.log`: log Kolla preparation.

Mỗi lượt preparation có thư mục attempt riêng, không dùng nhầm evidence còn
lại từ lượt trước để chứng nhận host đã chuẩn bị.

Với run layout cũ, các file tương ứng vẫn ở layout phẳng. Trên metadata host,
`migration-preparation.json` nằm cùng cấu hình metadata, không tham gia config
hash. Start ghi activation timestamp vào checkpoint này.

`metadata_readiness_timeout_seconds` mặc định **300 giây**, sử dụng sau API
restore. Kiểm tra registration/container là readiness service, không thay cho
HTTP metadata từ guest. Probe/phiên TCP gốc, metric, công thức downtime và TXT
renderer không được thay đổi. Không có cam kết số giây tiết kiệm khi chưa chạy lab.

## Cập nhật và chạy

Dùng toàn bộ source cùng phiên bản trong thư mục sạch. Toàn bộ phase 06 nằm
trong `playbooks/06-target-config.yml`; không còn một file phase 06 riêng cho
metadata. Các route resume thủ công đã chuyển vào [archive](../archive/README.md).
Reset vẫn dùng Kolla destroy để xóa container/unit như các dịch vụ khác.

Sau khi baseline ML2/OVS đã sẵn sàng:

```bash
cd /root/ovs-to-ovn-migration
source /root/venvs/kolla-2024.1/bin/activate
ansible-playbook -i /root/multinode migrate-to-ovn.yml
```

Không cần cờ bật metadata preparation. Trước khi đưa lên production cần kiểm
chứng trên image/Kolla đang dùng: trước freeze metadata cũ vẫn phục vụ, container
mới chưa chạy; sau handoff controller tiếp tục start; request metadata từ VM
khôi phục và các probe E-W/N-S hiện có vẫn được thu như trước. Bộ kiểm thử
offline không xác nhận kết quả forwarding thực tế trên lab.

## Lỗi mount metadata và lượt đo mới

Kolla 18.8 dùng bind
`/etc/kolla/neutron-ovn-metadata-agent/:/var/lib/kolla/config_files/:ro`.
Helper hiện so sánh destination bằng `PurePosixPath`, nhận cả dạng có/không có
trailing slash; vẫn từ chối mount read-write hoặc nhiều bind cùng destination.
Test Ansible sử dụng đúng dạng trailing slash này. Lỗi parser cũ xảy ra trước
khi tạo/start container metadata; MTU đã giảm ở task trước đó trong phase 06.

Với run `20261023-155541`, guard đã phát hiện capture N-S gốc thiếu hoặc đã dừng.
Run đó không đủ điều kiện tiếp tục phép đo liên tục. Các route resume được lưu
ở archive để tham khảo và dừng ngay khi gọi; không xóa checkpoint hoặc tạo lại
capture để ép run cũ đi tiếp.

Sau khi cập nhật source trong thư mục sạch, reset lab rồi chạy run mới:

```bash
cd /root/ovs-to-ovn-migration
source /root/venvs/kolla-2024.1/bin/activate
ansible-playbook -i /root/multinode reset-lab-to-ovs.yml \
  -e reset_lab_confirm=true -e reset_delete_migration_backups=false
# Chỉ chạy lệnh tiếp theo sau OVS_4NODE_BASELINE_READY.
ansible-playbook -i /root/multinode migrate-to-ovn.yml
```

Reset phá hủy deployment/workload lab và dựng baseline OVS; tham số trên giữ
migration backup/evidence của run cũ. Mỗi run mới có UUID workload, capture,
phiên TCP và mốc đo riêng. Quy trình reset lab này không phải rollback production.
