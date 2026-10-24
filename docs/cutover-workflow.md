# Chuẩn bị trước freeze và quan sát phục hồi — 16/10/2026

Bản sửa dựa trực tiếp trên `final.zip`. Format `migration-report.txt`, công
thức downtime và cửa sổ đo traffic được giữ nguyên. Không có cam kết số giây
downtime tiết kiệm được trước khi chạy lại lab.

## Thứ tự mới

| Vị trí | Công việc |
|---|---|
| Phase 01 | Baseline phục vụ migration; chỉ snapshot cấu hình/container nguồn từng host khi bật rollback |
| Phase 06, trước freeze | Tạo cấu hình và container OVN metadata bằng Kolla; container giữ trạng thái `created`, service bị disable |
| Phase 07, trước freeze | Lấy image Neutron, tạo thư mục helper có ownership, tính timeout binding, copy danh sách VM port đã chốt, thêm OpenFlow15; stage và preflight helper gateway |
| Ngay trước freeze | Chờ snapshot diagnostics `pre-freeze` thực hiện xong; kiểm tra readiness và capture/anchor Group A đang có |
| Phase 07, sau stop toàn bộ API, trước migrate | Mặc định đi vào DB migration; chỉ khi bật rollback mới kiểm VM/Nova native, dump Neutron và restore-test trong DB tạm đã chuẩn bị trước freeze |
| Phase 07–10 sau freeze | DB migration/provider verification; phase 08 dừng metadata cũ rồi start metadata đã chuẩn bị, stop các agent còn lại, deploy controller, chờ binding, cleanup; thử schedule gateway rồi restart Neutron. ARM listener chỉ khi chủ động bật TCP |
| Đầu phase 11, API đã phục hồi | Quan sát các probe đang chạy đến khi có thành công mới; sau đó bật sampling bổ sung và lấy snapshot diagnostics sau phục hồi |
| Tiếp theo | Snapshot tài nguyên AFTER, so sánh, smoke test, validation/workload mới và remediation |

OpenFlow được **bổ sung**, giữ các protocol cũ và UUID của `br-int`; không xóa
flow, controller hoặc port. Phase 08 chỉ kiểm tra protocol/negotiation. Cấu hình
OpenFlow có thể làm observer reconnect trước freeze; readiness yêu cầu observer
đã nối lại và xác nhận protocol, đồng thời giữ nguyên gap/error lịch sử.

`cutover-preparation.json` lưu run, inventory/VM-port SHA256, host được chọn,
image và timeout. Helper/port list ở `/tmp/ovs-to-ovn-preparation-<run>/` trên
DB sync/OVN CLI host, kèm `owner.json`. Retry không được đổi checkpoint; khi đã
có freeze marker, helper từ chối chuẩn bị lại. Phase 08 xác minh checkpoint và
hash danh sách port trước khi sử dụng.

## Diagnostics

PCAP ping vẫn bắt buộc để đo downtime. Monitor OpenFlow/OVSDB/NB/SB, Docker
events và service logs tổng quát chỉ chạy khi bật `migration_diagnostics_enabled`.
Snapshot conntrack N-S `post-cutover` trước legacy cleanup vẫn giữ
nguyên vị trí. Năm lần gọi snapshot tổng hợp giữa cutover được bỏ:
`before-legacy-agent-stop`, `before-ovn-takeover`, `vm-portbinding-ready`,
`before-legacy-cleanup`, `before-neutron-restart`.

Run mới lưu `sampling_scope: outside-cutover`. Snapshot `pre-freeze` phải hoàn
tất, kể cả những dump đã xếp hàng trước đó, rồi workflow mới được freeze.
Sampling neighbor/conntrack/counter định kỳ chưa bật trong cutover. Dump tổng
hợp do reconnect cũng được hoãn, gắn nhãn `deferred-after-recovery:*`; nhãn đó
chỉ trạng thái **sau phục hồi**, không giả làm snapshot tại thời điểm reconnect.
Initial OpenFlow dump của observer reconnect vẫn thuộc stream quan sát liên tục.
Sau `post-traffic-recovery` mới bật sampling bổ sung khoảng 2 giây/lần.

## Chờ traffic hiện có

`scripts/validation/live_recovery.py` đọc console của VM/probe và JSONL controller đang có,
đồng thời kiểm tra snapshot các capture EW matrix đang chạy. Mỗi stream phải
tiến lên sau lần đọc đầu, có chuỗi thành công và không còn failure mở. Kiểm tra
boot/probe/ping/session identity giữ nguyên; held bị gãy không được reconnect.
Không tạo probe, socket kiểm thử, listener, VM hoặc tài nguyên cloud mới.

`post_restore_observation_timeout_seconds` mặc định **300 giây**, chặn cả vòng
poll lẫn SDK/transport bị treo. Nếu hết hạn, identity đổi hoặc held bị gãy, task
dừng trước AFTER/smoke; evidence nằm ở `post-restore-traffic.json` và
`post-restore-observations/*.json`. Callback cố gắng dừng/fetch diagnostics như
với các lỗi sớm hiện có; lỗi trước phase 13 có thể chưa có final report. Không
đánh dấu timeout thành phục hồi và không tự khởi động lại probe. Khi resume
run đã hoàn tất phase 11, không chờ lại những capture có thể đã dừng ở phase 12.

Đây là điều kiện điều phối workflow, không phải metric downtime. Report cuối
vẫn tính trên evidence gốc và giữ nguyên format TXT. Snapshot AFTER luôn đứng
trước smoke test hoặc tài nguyên workload mới.

## Reset và resume

Reset không yêu cầu migration cũ PASS, archive hoàn chỉnh hoặc config lịch sử
khớp tuyệt đối. Helper `reset_host_observers.py` kiểm tra process hiện tại trên
control/network/compute trong một lượt, nhận dạng đúng invocation observer dưới
`reset_diagnostics_directory`, rồi TERM qua pidfd. Observer của run cũ còn sống
cũng được dừng; không chọn riêng run mới nhất và không stop/archive lặp lại mọi
run trong backup. PID còn sống sau timeout hoặc lỗi truy cập host vẫn chặn destroy.
Lỗi coverage, metadata/archive hỏng hoặc thiếu không phải điều kiện chặn reset.
Observer sống có cơ hội chạy cleanup bình thường; các monitor còn trong container
sẽ kết thúc khi Kolla destroy container. Không yêu cầu chứng nhận forensic archive
trước một destructive reset. Các bước dừng N-S probes/captures hiện có được giữ.

Evidence mới nằm trong snapshot reset: `host-observers-stop.log` và
`host-observers-<host>.json`. Reset không viết lại kết quả diagnostics migration
cũ. Nếu đã override `migration_diagnostics_directory`, đặt
`reset_diagnostics_directory` cùng đường dẫn đó. Với run mới, công thức interface
lab được materialize trước khi lưu checkpoint: network1=`br-underlay`, các host
khác=`eth1`. Bộ đọc archive nhận biết đúng công thức cũ mà không sửa file lịch sử
hoặc bỏ kiểm tra run/host/role và các config khác của migration.

Sau Kolla destroy thành công, reset kiểm tra ownership của **mọi** thư mục
preparation trên từng control/network/compute trước khi xóa bất kỳ thư mục nào
trên host đó. Symlink, marker thiếu hoặc ownership sai làm bước dừng; không xóa
report/backup migration. Evidence cleanup ở snapshot reset. Sau OVS deploy,
reset thêm và xác minh OpenFlow15 trước khi dựng workload; chuẩn bị trước
freeze vẫn kiểm tra lại để hỗ trợ baseline OVS chưa chạy reset bản mới.

Các route resume thủ công trước đây đã chuyển vào [archive](../archive/README.md).
Workflow hiện hành dùng một file cho mỗi phase 00–13. Toàn bộ metadata
preparation và lấy endpoint ở phase 06 nằm trong `06-target-config.yml`.
Run bị gián đoạn và mất capture gốc cần được giữ làm evidence; với lab, sửa lỗi,
reset baseline rồi chạy một run đo mới.

Để kiểm chứng trong lab, cập nhật toàn bộ ZIP, rồi chạy reset và migrate như cũ:

```bash
ansible-playbook -i /root/multinode reset-lab-to-ovs.yml -e reset_lab_confirm=true -e increase_quota=true -e instance_quota_limit=30
# Chỉ chạy tiếp khi reset in OVS_4NODE_BASELINE_READY.
ansible-playbook -i /root/multinode migrate-to-ovn.yml
```

Giữ nguyên điều kiện xác nhận reset hiện có trong `group_vars/reset.yml`.
Metadata được chuyển sang luồng [chuẩn bị trước freeze](metadata-preparation.md).
Giữa cleanup và restart Neutron có bước [gateway scheduling sớm](early-gateway-scheduling.md)
với deadline và fallback tiếp tục bật API. MTU/DHCP remediation, NAT profile,
source-port preservation và các tiêu chí chứng nhận traffic không đổi.

Khi bật `migration_rollback_enabled`, checkpoint sau stop API tăng thời gian API đóng.
Thời gian helper nằm ở `rollback/checkpoint-timing.json`; không đổi công thức
downtime/report. Legacy agents còn chạy khi lấy checkpoint. Xem
[rollback.md](rollback.md) về maintenance, điều kiện restore và lệnh vận hành.

## External dùng chung và bộ demo (17/10/2026)

Reset dựng external và cặp demo riêng; migration vẫn tạo toàn bộ workload tự
động. Phase 02 lưu baseline chỉ đọc trong `source-topology-before.json`, kiểm
tra flat physnet/subnet/pool và hai IP dành cho N-S. Chỉ khi không có external
mới dùng luồng tạo external thuộc run như trước để tương thích baseline cũ.

`validation-config.json` lưu UUID/fingerprint external mượn dùng; checkpoint
N-S ghi `external_owned=false`. Retry/resume dùng nguyên checkpoint đó.
Cleanup xác minh fingerprint trước khi xóa workload, giữ external/subnet và
không nhận UUID demo vào `validation-resources.json` hay `extended-resources.json`.
Phase 11 và phase 13 kiểm tra tài nguyên có sẵn; JSON thêm
`preexisting_resources_preservation`, TXT và metric không đổi.
