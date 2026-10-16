# Chuẩn bị trước freeze và quan sát phục hồi — 16/10/2026

Bản sửa dựa trực tiếp trên `final.zip`. Format `migration-report.txt`, công
thức downtime và cửa sổ đo traffic được giữ nguyên. Không có cam kết số giây
downtime tiết kiệm được trước khi chạy lại lab.

## Thứ tự mới

| Vị trí | Công việc |
|---|---|
| Phase 07, trước freeze | Lấy image Neutron, tạo thư mục helper có ownership, tính timeout binding, copy danh sách VM port đã chốt, thêm OpenFlow15 vào protocol set hiện có |
| Ngay trước freeze | Chờ snapshot diagnostics `pre-freeze` thực hiện xong; kiểm tra readiness và capture/anchor Pair A đang có |
| Phase 07–10 sau freeze | DB migration/provider verification, ARM listener, stop agents, deploy controller/metadata, chờ binding, cleanup rồi restart Neutron như cũ |
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

PCAP, OpenFlow/OVSDB/NB/SB monitors, Docker events và service logs vẫn chạy
liên tục. Snapshot conntrack N-S `post-cutover` trước legacy cleanup vẫn giữ
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

`scripts/live_recovery.py` đọc console của VM/probe và JSONL controller đang có,
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

Reset dừng/archive đúng observer/probe được checkpoint trước destroy; coverage
GAPPED không ngăn reset nếu mọi process đã chứng minh cleanup hoàn tất. Run chỉ
khởi tạo core collector rồi lỗi sớm cũng được dọn; không đòi central collector
chưa từng được tạo. Shutdown chưa được chứng minh vẫn chặn destroy.

Sau Kolla destroy thành công, reset kiểm tra ownership của **mọi** thư mục
preparation trên từng control/network/compute trước khi xóa bất kỳ thư mục nào
trên host đó. Symlink, marker thiếu hoặc ownership sai làm bước dừng; không xóa
report/backup migration. Evidence cleanup ở snapshot reset. Sau OVS deploy,
reset thêm và xác minh OpenFlow15 trước khi dựng workload; chuẩn bị trước
freeze vẫn kiểm tra lại để hỗ trợ baseline OVS chưa chạy reset bản mới.

`resume-after-db.yml` yêu cầu checkpoint preparation gốc và không lặp lại DB
migration, freeze hay ARM. Bản này dành cho **run mới**; không ghép code mới vào
run cũ đã freeze mà thiếu `cutover-preparation.json`. Các hướng dẫn cài riêng
vài file để cứu run cũ trong tài liệu lịch sử không áp dụng cho bản thay đổi
thứ tự này.

Để kiểm chứng trong lab, cập nhật toàn bộ ZIP, rồi chạy reset và migrate như cũ:

```bash
ansible-playbook -i /root/multinode reset-lab-to-ovs.yml -e reset_lab_confirm=true
# Chỉ chạy tiếp khi reset in OVS_4NODE_BASELINE_READY.
ansible-playbook -i /root/multinode migrate-to-ovn.yml
```

Giữ nguyên điều kiện xác nhận reset hiện có trong `group_vars/reset.yml`.
Metadata deploy, thứ tự cleanup → restart Neutron, MTU/DHCP remediation, NAT
profile, source-port preservation và các tiêu chí chứng nhận traffic không đổi.
