# Validation chỉ ping

Mặc định mới áp dụng cho run mới: `validation_ping_only: true`,
`validation_pair_d_enabled: false` và `ns_tcp_enabled: false`. Lệnh migration
giữ nguyên, không cần thêm tham số:

```bash
ansible-playbook -i /root/multinode migrate-to-ovn.yml
```

## VM được tạo

| Mục đích | Số VM mặc định | Nội dung |
|---|---:|---|
| E-W chính | 2 | Hai compute, hai subnet, ping liên tục qua router |
| Ma trận E-W | 2 | Dùng chung VM E-W chính để đo thêm ba trường hợp compute/subnet |
| N-S | 2 | Ping outbound qua SNAT và inbound qua FIP |
| TCP Pair D | 0 | Không tạo VM hoặc chạy held/new connection probes |
| DHCP/metadata Pair B | 0 | Không tạo cặp riêng, không kiểm thử/remediate guest MTU hay metadata |
| VM mới sau migration | 0 | Phase 12 chỉ kiểm tra các luồng đã có |

Tổng cộng 6 VM (3 trên mỗi compute) với cấu hình ma trận E-W và N-S hiện tại.
Nếu tắt ma trận E-W hoặc N-S thì giảm tương ứng 2 VM. Capacity gate tính theo
profile này; quota/reset không thay đổi. Các VM vẫn cần DHCP/cloud-init để
boot như bình thường, nhưng profile không chạy kiểm thử DHCP/metadata riêng.
Guest E-W chỉ phát ping, không chạy vòng health HTTP metadata hay DHCP observer.

## Luồng thực thi

Phase 04 tạo VM ping và chốt baseline/capture. Phase 06 vẫn giảm MTU network
để migration an toàn, nhưng bỏ gate DHCP/MTU của Pair B. Trước đóng API,
phase 07 yêu cầu ping, capture và identity còn hợp lệ; TCP ARM là no-op.
Phase 12 xác minh ping/PCAP và sự bảo toàn tài nguyên, không tạo VM mới.
Cleanup chỉ xóa tài nguyên có UUID/ownership trong chính run và chỉ sau khi
các gate đạt. VM kiểm thử của run cũ không bị xóa chỉ vì đổi default.

Backup/checkpoint rollback chỉ chạy khi bật tùy chọn tương ứng; xem
[cách cấu hình run](run-options.md). Migrate DB, metadata, gateway scheduling,
API health và kiểm tra backend vẫn thực hiện; smoke test tạo network là tùy chọn. Chế độ chỉ
ping không phải chứng nhận DHCP/metadata hay tính liên tục của phiên TCP.

N-S không tạo TCP echo/held/new-listener probes hay mở listener TCP trong VM
FIP. Một listener trên controller và socket control của VM SNAT vẫn phục vụ
việc xuất log ICMP gốc sau khi dừng capture. Đây là đường truyền evidence, không
phải workload TCP và không được đưa vào metric TCP. Nó giữ kiểm tra hash,
run/boot/PID/log identity và time fence; không restart ping để lấy log.
Capture N-S của profile này chỉ lấy ICMP/ARP, không lấy TCP control.

## Report và rollback

TXT chỉ hiện các phép đo ping. JSON ghi `DISABLED` cho các nhóm workload B/C/D
và TCP N-S, không dùng `PASS` thay cho kiểm thử đã tắt. Đo packet loss/outage,
boot/UUID continuity, timestamp, PCAP/sender-log completeness và bảo toàn tài
nguyên vẫn được kiểm tra. Thiếu bằng chứng ping/capture vẫn chặn thành công
và cleanup. Công thức downtime không thay đổi.

Rollback đọc profile của run: với ping-only, kiểm tra phục hồi chức năng dùng
ping E-W và N-S, không yêu cầu Pair B hoặc guest health. Guard host/container,
VM, inventory, DB/config và topology không thay đổi. Run cũ không có cờ
`ping_only` tiếp tục theo workflow cũ; không sửa config của run đang đo để
chuyển profile giữa chừng.

## Bật lại khi cần

`validation_ping_only: false` bật lại Pair B và cặp VM mới sau migration.
TCP vẫn tắt mặc định. Muốn chạy TCP, đồng thời bật `validation_pair_d_enabled`
(E-W) hoặc `ns_tcp_enabled` (N-S). Các biến nằm trong `group_vars/all.yml` và
`group_vars/ns-lab.yml`; cũng có thể override bằng `-e`.

Đã kiểm thử offline, gồm orchestration, report, rollback reader và socket local
để xuất log ICMP không cần TCP application streams. Chưa chạy profile mới
trên lab thật; không suy ra thời gian outage của lần chạy tiếp theo.
