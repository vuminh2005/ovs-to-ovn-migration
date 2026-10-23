# Python helpers

| Nhóm | Chức năng |
|---|---|
| `common/` | Layout run, phase schema, diagnostic policy, shared ownership, instance quota opt-in |
| `migration/` | Chuẩn bị cutover/OpenFlow/image, topology, provider associations, network semantics |
| `validation/` | Điều phối workload, placement/capacity, DHCP/MTU, TCP, readiness và chờ phục hồi |
| `probes/` | Chương trình chạy trong guest và helper điều khiển probe TCP/ICMP |
| `diagnostics/` | Capture, host observers, log transfer/reconcile và OVN evidence |
| `reporting/` | Đọc evidence, timeline, render TXT/JSON, tóm tắt N-S |
| `reset/` | Cleanup có ownership, baseline/demo, underlay, RabbitMQ recovery/health |
| `recovery/` | Helper retry phase 04 lịch sử còn được archive sử dụng |

Chạy helper bằng đường dẫn mới, ví dụ:

```bash
python3 scripts/common/run_layout.py path /root/ovs-to-ovn-backup/<run-id> runtime.json
```

`_paths.py` là đầu mối tìm source và playbook transport từ vị trí checkout.
Các module cần import chéo tự nạp resolver khi chạy từ source; không phụ thuộc
vào thư mục hiện tại hoặc cài project thành package.

Helper chép sang node/guest vẫn có tên file phẳng như `dataplane_capture.py`,
`run_layout.py`, `ns_probe.py`. Chúng dùng sibling import và chạy độc lập với
source controller. Vì vậy không đổi các import đó thành package import hoặc
chép cả cây source vào thư mục evidence. Xem [quy tắc phát triển](../docs/structure.md).

Helper checkpoint của các route resume thủ công đã chuyển vào
`archive/scripts/recovery/`, cùng playbook và hướng dẫn tham khảo. Chúng không
nằm trong registry source hiện hành.
