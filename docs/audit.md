# Audit bản sửa 16/10/2026

Cơ sở: `final.zip` hiện hành, SHA-256
`254e1635da61a6b652c1228af223d1005f15ec92ef6392c08679729cabc1d963`.
ZIP nguồn đính kèm từ commit cũ chưa có diagnostics/N-S nên không được dùng
để thay thế các sửa đổi reset và cutover đã hoàn tất.

## Kết quả rà soát

| Phần | Kết luận và xử lý |
|---|---|
| Host diagnostics | Tar là bản truyền trùng với thư mục đã giải nén; bỏ tar sau khi kiểm tra hash toàn bộ nội dung. Lỗi thì giữ tar. |
| Endpoint PCAP | Nguồn capture local trước đây được fetch thành bản thứ hai; run mới ghi thẳng đường dẫn chuẩn, không fetch lại chính file đó. |
| Checkpoint cũ | Giữ config/identity nguồn và journal; chuyển hoặc đối chiếu bản archive trước khi bỏ PCAP nguồn. Stop lặp lại được hỗ trợ. |
| Retry preparation | Nhận cả hai vị trí endpoint hợp lệ; đối chiếu cả cổng listener mới và ICMP, từ chối cấu hình đã đổi. |
| Console cuối | Chỉ in run và đường dẫn TXT/JSON; bỏ dòng RESULT. Không sửa kết quả hay điều kiện exit code trong JSON. |
| TXT | Toàn bộ phần định dạng và helper trước `terminal_summary()` giữ nguyên từng byte so với bản nền. |
| Reset/resume | Giữ logic dừng observer đang sống và thứ tự migration đã tối ưu. Không đưa điều kiện archive/remediation vào reset. |
| Cấu trúc mã nguồn | Giữ entrypoint ở gốc; gom sáu patch/ghi chú lịch sử vào `docs/history/`, thêm mục lục và cập nhật liên kết. |
| Tham chiếu | Không phát hiện path hỏng trong import/include/vars_files Ansible hoặc liên kết tài liệu hiện hành. |

Mã tính metric, phase timer, probe và report engine giữ nguyên. Các segment
diagnostics được giữ riêng vì không phải bản sao của cùng cửa sổ quan sát.
Không có cơ chế tự quét/xóa evidence của mọi run lịch sử trong bản sửa này.

Các kiểm tra dùng fixture, subprocess và Ansible local; Docker/OpenStack là
giả lập khi cần. Không chạy reset, migration, restart container hay API cloud
trên lab. Kết quả offline không xác nhận downtime thực tế của lần chạy sau.

## Kiểm chứng cuối

- 574 test và 417 subtest đạt; không có test bỏ qua. 140 cảnh báo deprecation
  có sẵn từ OpenStackSDK; không có lỗi kiểm thử.
- 12 entrypoint/helper Ansible qua kiểm tra cú pháp với ansible-core 2.16.19.
- 89 file Python parse AST và 37 file YAML parse thành công.
- 158 tham chiếu Ansible và 34 liên kết tài liệu hiện hành hợp lệ.
- Kiểm tra whitespace/diff thành công. TXT renderer và các helper định dạng
  có SHA-256 `3ca3e49f1507dfdf18bcfbfed48139051d98b194580be06be16344e588a62157`,
  trùng bản nền. Thay đổi console chỉ nằm trong `terminal_summary()`.

Fixture kiểm tra tar được xóa sau đối chiếu, tar hỏng/thiếu giữ nguyên,
PCAP canonical giữ nguyên inode, bản local cũ chuyển một lần, xung đột không
xóa dữ liệu, và transport Ansible inspect/stop lặp lại ở cả hai layout.
Bộ kiểm thử có sẵn tiếp tục kiểm tra lifecycle supervisor, signal/wait,
reset, resume, các gate và dữ liệu report. Phần định danh PID cần mô phỏng
ở một số test có sẵn do namespace của môi trường kiểm thử; mã runtime
không được nới điều kiện nhận dạng tiến trình.
