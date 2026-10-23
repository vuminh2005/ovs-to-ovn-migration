# Kịch bản và tài liệu lịch sử

Các route resume thủ công đã ngừng hỗ trợ trong workflow metadata/capture hiện
hành. Callback thu thập khi run lỗi có thể đóng capture N-S gốc; chạy tiếp từ
checkpoint không khôi phục được phép đo liên tục. Các route cũ cũng chưa có
kiểm chứng đầy đủ cho handoff metadata mới ở mọi vị trí lỗi.

| Nội dung lưu trữ | Lý do |
|---|---|
| `playbooks/resume-before-target.yml` | Route phase 05 không bảo đảm capture gốc còn chạy sau failure collection |
| `playbooks/resume-metadata-preparation.yml` | Route phase 06 yêu cầu capture gốc còn chạy; run lỗi hiện tại không đủ điều kiện |
| `playbooks/resume-after-db.yml` | Chưa bảo đảm capture và toàn bộ handoff metadata sau interruption/freeze |
| `playbooks/resume-after-cleanup.yml`, `playbooks/resume-bootstrap.yml` | Bootstrap late phase chưa kiểm chứng đầy đủ metadata mới và phục hồi bằng chứng đo |
| `playbooks/metadata-preparation-suffix.yml` | Phần phase 06 cũ chỉ được body của route metadata archived tham chiếu |
| `scripts/recovery/` | Helper checkpoint của các route resume trên |
| `tests/` | Snapshot test resume cũ, không thuộc bộ regression hiện hành |
| `docs/*before-phase06-merge.md` | Hướng dẫn và lệnh cũ để đối chiếu; không dùng như hướng dẫn vận hành |
| `playbooks/retry-validation-preparation.yml` | Retry phase 04 cũ, chỉ trước khi VM đầu tiên được ghi nhận |
| `playbooks/retry-ns-diagnostics-preparation.yml` | Retry phase 04 cũ, chỉ trước khi N-S guest/local probe được khởi chạy |

Năm file `resume-*.yml` dừng ngay ở play đầu, trước khi đọc checkpoint hoặc thao
tác host. Body cũ giữ để đối chiếu; syntax-check archive không chứng nhận khả
năng recovery trên lab hoặc production. Hai retry phase 04 giữ nguyên guard
chuyên biệt trước đây và vẫn là tài liệu tham khảo, không phải route resume chung.

Migrate/reset hiện hành không import các file này. Xem [README chính](../README.md)
cho quy trình reset baseline lab và chạy một run mới, giữ backup/evidence cũ.
