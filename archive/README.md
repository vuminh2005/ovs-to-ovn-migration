# Kịch bản tham khảo

| File | Tình huống ban đầu |
|---|---|
| `playbooks/retry-validation-preparation.yml` | Phase 04 lỗi trước khi có VM được ghi nhận; chưa staging/target/freeze |
| `playbooks/retry-ns-diagnostics-preparation.yml` | Lỗi phase 04 tại đúng endpoint capture launch intent; chưa có N-S guest/local probe |
| `docs/quota-fix-recovery.md` | Ghi chú xử lý sự cố quota theo phiên bản cũ |

Hai retry vẫn giữ guard và được kiểm tra cú pháp/test để đọc lại được. Chúng
không nằm trong luồng migrate/reset mặc định và không phải route retry chung.
Khi topology hoặc hệ thống thay đổi, đối chiếu toàn bộ điều kiện trong code
trước khi dùng lại. Không bỏ guard để áp dụng cho một lỗi khác.

Các hướng dẫn cài lẻ file từ ZIP cũ được giữ trong `docs/` ở đây để tham khảo;
lệnh hiện hành dùng source đầy đủ và các route trong
[playbooks/recovery/](../playbooks/recovery/README.md).
