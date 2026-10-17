# Tài liệu

| Nội dung | Hướng dẫn |
|---|---|
| Cài đặt và chạy | [README chính](../README.md) |
| Reset bốn node | [reset-four-nodes.md](reset-four-nodes.md) |
| Thứ tự chuẩn bị, cutover và phục hồi | [cutover-workflow.md](cutover-workflow.md) |
| Workload EW/N-S và cấu hình lab | [ns-ew-matrix.md](ns-ew-matrix.md) |
| Pair A/B/C/D | [abcd-validation.md](abcd-validation.md) |
| Audit bản sửa | [audit.md](audit.md) |
| Evidence và đường dẫn report | [report-evidence.md](report-evidence.md) |
| Observers và giới hạn đo lường | [ns-observability.md](ns-observability.md) |
| PCAP TCP N-S | [ns-tcp-diagnostics.md](ns-tcp-diagnostics.md) |
| Phân tích session TCP held | [ns-held-session-diagnostics.md](ns-held-session-diagnostics.md) |
| Khôi phục image/phase 05 | [image-cache-recovery.md](image-cache-recovery.md) |
| Khôi phục sau DB migration | [post-db-recovery.md](post-db-recovery.md) |
| Quota | [quota-fix-recovery.md](quota-fix-recovery.md) |
| Scope Kolla globals | [globals-scope-fix.md](globals-scope-fix.md) |
| RabbitMQ khi reset | [reset-rabbitmq-mitigation.md](reset-rabbitmq-mitigation.md) |

`history/` chứa patch và ghi chú kiểm tra từ các bản trước. Đây là tài liệu
lịch sử; dùng hướng dẫn hiện hành ở trên để chạy bản này.

Các entrypoint migrate/reset/resume/retry vẫn ở thư mục gốc. `playbooks/`
chứa các phase và helper Ansible; `scripts/` chứa các module chạy cùng nhau;
`tests/fixtures/` chỉ chứa dữ liệu kiểm thử. Việc gom tài liệu không đổi
đường dẫn triển khai hoặc import Python.
