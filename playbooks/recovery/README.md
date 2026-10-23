# Resume theo checkpoint

Chạy từ gốc project, với inventory đầy đủ như run ban đầu. Mỗi route yêu cầu
`-e migration_resume_run_dir=/đường/dẫn/tuyệt/đối`, kiểm tra checkpoint và từ
chối trạng thái không phù hợp.

| Entrypoint | Trạng thái có thể tiếp tục | Phần chạy tiếp |
|---|---|---|
| `resume-before-target.yml` | Phase 00–04 hoàn tất, phase 05 chưa hoàn tất; chưa đổi target/MTU, chưa freeze | Phase 05–13 |
| `resume-metadata-preparation.yml` | Phase 00–05 hoàn tất; phase 06 dừng tại preparation metadata sau MTU/genconfig; chưa freeze; probe/capture gốc còn hợp lệ | Preparation metadata và cuối phase 06, rồi phase 07–13 |
| `resume-after-db.yml` | DB migrate thành công, preparation gốc và provider verification hợp lệ; chưa cutover, Neutron còn dừng | Xác minh rồi Phase 08–13 |
| `resume-after-cleanup.yml` | Checkpoint late phase đã takeover hợp lệ; live OVN/legacy agent đúng trạng thái | Phase 09–13; tôn trọng guard cleanup hiện có |

`resume-bootstrap.yml` là phần bootstrap của route late phase, không phải
entrypoint thay thế migration đầy đủ.

```bash
ansible-playbook -i /root/multinode playbooks/recovery/resume-before-target.yml \
  -e migration_resume_run_dir=/root/ovs-to-ovn-backup/<run-id>
```

Giữ nguyên timestamp, inventory snapshot, UUID và probe/session gốc. Resume
không tái tạo probe để che mất một đoạn đo, không re-arm TCP và không chạy lại
DB migration đã hoàn tất. Probe hết lifetime hoặc mất identity vẫn làm guard
dừng. Tổng thời gian run/downtime có thể gồm thời gian gián đoạn để xử lý lỗi.

Xem thêm [phase 05/image](../../docs/recovery/image-cache-recovery.md) và
[sau DB migration](../../docs/recovery/post-db-recovery.md). Lỗi preparation
metadata và điều kiện resume nằm trong [hướng dẫn metadata](../../docs/metadata-preparation.md#khôi-phục-lỗi-preparation-ở-phase-06).
Hai retry phase 04 chuyên biệt được giữ ở [archive](../../archive/README.md).
