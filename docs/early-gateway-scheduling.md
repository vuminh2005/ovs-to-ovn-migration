# Gateway scheduling sớm

Helper gọi `OVNL3RouterPlugin.schedule_unhosted_gateways()` trong image Neutron
đang dùng, sau khi chassis đã đăng ký và cleanup OVS cũ hoàn tất. Mục tiêu là
bắt đầu khôi phục SNAT/FIP trước khi chờ API worker khởi động và IDL của worker
chạy scheduler. Luồng metadata, probe và công thức/format report giữ nguyên.

## Thứ tự thực thi

| Phase | Hành động |
|---|---|
| 06 | Chuẩn bị metadata như luồng hiện tại |
| 07, trước freeze | Stage helper và topology; pin image ID, hash config/source; chạy preflight import/config, chưa tạo IDL/plugin manager hay schedule |
| 07, sau freeze | DB migrate và chuyển router provider association sang OVN như hiện tại |
| 08 | Start metadata đã chuẩn bị, deploy controller, chờ chassis và VM port binding như hiện tại |
| 09 | Cleanup namespace/port/patch OVS cũ; xác minh gateway attachment cũ và L3 agent đã rút trên từng network host |
| 10, trước start API | Kiểm tra mọi controller còn dừng API; thử scheduler native trong container riêng, rồi start API |
| 11–13 | Validation, đo outage và report như hiện tại |

Phase 09 phải cleanup thành công trên mọi host; lỗi cleanup sẽ chặn các phase
sau trên toàn inventory. Không dựa riêng vào việc container L3 agent đã dừng
để kết luận gateway cũ được rút. Ở phase 10, nếu thiếu fact xác nhận cleanup,
helper sẽ không chạy. Đây là fallback bảo vệ bước tối ưu; không phải route bỏ
qua lỗi cleanup của phase 09.

## Helper thực hiện gì

`gateway_scheduling.py` chạy trên control host đã stage helper ở phase 07.
`gateway_native.py` chạy bằng Python trong image Neutron đó. Bootstrap sử dụng
ML2/OVN-sync driver và MaintenanceWorker IDL như tiện ích db-sync native của
Neutron 24.x, sau đó chỉ gọi scheduler L3. Không chạy lại `do_sync()` hoặc
`migrate_neutron_database_to_ovn()`, không mở listener HTTP/RPC worker.

Scheduler native quyết định chassis, physnet, AZ và thứ tự HA. Helper kiểm tra
toàn bộ gateway trước khi gọi: gateway thuộc OVN provider, LRP đã tồn tại,
có candidate phù hợp, system-id và Geneve IP khớp topology inventory đã lưu.
Không tự tạo gateway port hoặc gán chassis bằng lệnh NB thủ công.

Sau transaction, helper kiểm tra assignment NB, primary đang hợp lệ phải được
giữ và gateway IP/MAC/UUID/external gateway info trong Neutron DB không đổi.
SB `cr-lrp-*` binding được lưu như quan sát tại thời điểm đó; không chờ SB hoặc
guest ping trong lúc API đang dừng. Trường `up` chỉ được đọc nếu schema hỗ trợ.
PASS ở bước này nghĩa là assignment NB hợp lệ, không thay thế validation N-S.

## Deadline và fallback

Preflight có giới hạn attach 60 giây trước downtime. Khi schedule, alarm trong
helper là 25 giây; supervisor giới hạn attach 30 giây và luôn force-remove
container theo ID do chính nó tạo, với giới hạn 5 giây cho lệnh remove. Image
được pin bằng ID và `--pull=never`. Không dùng lại container của Neutron API.
Image/config/helper/topology thay đổi sau prepare sẽ làm bước tối ưu fallback.

Nếu scheduling lỗi, timeout, thiếu candidate hoặc cleanup chưa được xác nhận,
phase 10 ghi `FALLBACK` rồi tiếp tục start API. Scheduler thông thường của Neutron
và validation hiện có vẫn quyết định kết quả khôi phục. Không rollback các
assignment hợp lệ đã commit chỉ vì kiểm tra sau đó lỗi; API có thể tiếp tục từ
trạng thái đó. Chuẩn bị lỗi trước freeze sẽ dừng playbook trước downtime để
có thể sửa nguyên nhân.

Thời gian bootstrap helper nằm trong phase 10/control-plane downtime hiện có.
Không có cam kết giảm thêm bao nhiêu giây; cần chạy lab và đối chiếu phép đo
N-S. Không thêm khoảng chờ SB vào đường khôi phục API.

## Evidence và sử dụng

- Trong run: `evidence/gateway-preparation.json`, `evidence/gateway-scheduling.json`.
- Trên control host: `/tmp/ovs-to-ovn-preparation-<run-id>/gateway-preparation.json`,
  `gateway-prepare-result.json`, `gateway-schedule-result.json`,
  `gateway-preflight.log`, `gateway-schedule.log` (chỉ có nếu đã chạy).
- Manifest/evidence/log helper có quyền `0600`; directory có quyền `0700`.
  Chỉ lưu hash cấu hình trong manifest, không lưu giá trị connection/password.
- Reset đã có cơ chế dọn directory preparation theo ownership sau Kolla destroy.

Giữ nguyên toàn bộ `group_vars/` bạn cung cấp, bao gồm quota bật và limit 30.
Chạy entrypoint migration bình thường trên baseline OVS đã sẵn sàng:

```bash
source /root/venvs/kolla-2024.1/bin/activate
cd /root/ovs-to-ovn-migration
ansible-playbook -i /root/multinode migrate-to-ovn.yml --syntax-check
source /etc/kolla/admin-openrc.sh
ansible-playbook -i /root/multinode migrate-to-ovn.yml
```

Nếu lab đã migrate xong, reset về baseline OVS bằng
[lệnh reset có quota](../README.md#chạy-trên-controller) trước lần thử mới.
Không chèn helper vào một run đang chạy bằng các route resume đã archive.
Thay source bằng cách giải nén bản ZIP đầy đủ vào thư mục source sạch.
