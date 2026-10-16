# Lưu evidence report

Format `migration-report.txt`, công thức downtime và kết quả JSON được giữ
nguyên. Console cuối chỉ in run và hai đường dẫn report; không in dòng
`RESULT: SUCCESS_WITH_REMEDIATION` hoặc thay kết quả remediation bằng SUCCESS.
Exit code vẫn dựa trên kết quả đầy đủ trong JSON.

## Host diagnostics

`diagnostics/<host>/<role>/` là bản evidence được giữ. Tar nhận từ host chỉ
dùng để truyền: giải nén an toàn, so SHA-256 của từng file với tar, rồi xóa
tar khi toàn bộ nội dung khớp. Giải nén/đối chiếu lỗi thì tar được giữ để
điều tra. Các preparation segment vẫn giữ riêng, vì chúng là các cửa sổ quan
sát khác nhau. Khi stop lại một collector đã archive, tar cũ chỉ được bỏ
nếu vẫn đối chiếu khớp với thư mục evidence tương ứng.

## Endpoint N-S

Run mới ghi trực tiếp vào `ns-diagnostics/endpoint/tcp.pcap`. Helpers và
capture journal nằm cùng thư mục; metadata và log transport nằm trong
`ns-diagnostics/transport/<kind>/`. Guest captures vẫn được fetch từ compute
vào `ns-diagnostics/{guest-snat,guest-fip}/`.

Checkpoint cũ dùng `endpoint-agent/` vẫn đọc được. Chỉ sau khi capture đã
dừng và đúng identity, các file được chuyển sang `endpoint/` trên cùng
filesystem. Nếu đã có bản archive, phải khớp hash trước khi bỏ bản nguồn;
xung đột thì giữ dữ liệu và báo lỗi. Journal gốc nhỏ được giữ để stop/resume
có thể kiểm tra config cũ; PCAP chỉ có ở đường dẫn chuẩn.

Checkpoint endpoint có `archive_directory` và `pcap_path` trỏ tới evidence
chuẩn. `remote.config`/`remote.path` của checkpoint cũ tiếp tục ghi nhận nguồn
capture ban đầu để không thay đổi bằng chứng ownership. Bộ tính ICMP và tài
liệu Wireshark tiếp tục đọc `ns-diagnostics/endpoint/tcp.pcap` như trước.

Reset vẫn dừng observer/probe/capture theo cơ chế hiện hành. Không dùng
trạng thái remediation để quyết định có bỏ evidence hay không. Bản sửa
không tự quét/xóa report của các run đã hoàn tất trong backup.
