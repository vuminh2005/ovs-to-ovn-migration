# Tài liệu hiện hành

| Nội dung | Hướng dẫn |
|---|---|
| Migration theo số host trong inventory / thông tin cần bổ sung | [node-independent-migration.md](node-independent-migration.md) |
| Migrate/reset và kiểm thử | [README chính](../README.md) |
| Bật tăng instance quota | [project-quota.md](project-quota.md) |
| Cấu trúc code và cách thêm file | [structure.md](structure.md) |
| Audit bản này | [audit-current.md](audit-current.md) |
| Đường dẫn cũ → mới | [path-map.tsv](path-map.tsv) |
| Reset / demo | [reset-four-nodes.md](reset-four-nodes.md), [demo.md](demo.md) |
| Kịch bản demo chi tiết | [huong-dan-demo-ovs-ovn.md](demo/huong-dan-demo-ovs-ovn.md), [demo-2-co-buoc-optional.md](demo/demo-2-co-buoc-optional.md) |
| Chuẩn bị và cutover | [cutover-workflow.md](cutover-workflow.md) |
| Workload và phép đo | [ns-ew-matrix.md](ns-ew-matrix.md), [abcd-validation.md](abcd-validation.md) |
| Evidence / observers | [report-evidence.md](report-evidence.md), [ns-observability.md](ns-observability.md) |
| TCP N-S | [ns-tcp-diagnostics.md](ns-tcp-diagnostics.md), [ns-held-session-diagnostics.md](ns-held-session-diagnostics.md) |
| Các route resume | [recovery](../playbooks/recovery/README.md) |
| Image/phase 05 và sau DB migrate | [image-cache-recovery.md](recovery/image-cache-recovery.md), [post-db-recovery.md](recovery/post-db-recovery.md) |
| RabbitMQ khi reset | [reset-rabbitmq-mitigation.md](reset-rabbitmq-mitigation.md) |
| Retry và ghi chú sự cố cũ | [archive](../archive/README.md) |

`history/` giữ README cũ, patch và báo cáo theo phiên bản để đối chiếu. Dùng
README chính khi chạy bản hiện hành. Hướng dẫn cài lẻ file từ ZIP cũ được lưu
để tham khảo, không áp dụng vào run đang dùng một phiên bản source khác.
