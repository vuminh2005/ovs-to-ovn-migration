# Audit thay đổi reset/demo và external dùng chung — 17/10/2026

Bản nền: `final.zip` version 3, SHA-256
`310ea8e12272818e6b7826becc8472cb002f083c9db2aae6a5cc1996b05bf4de`.

- Reset giữ các guard trước destroy, dựng uplink như cũ, xác minh image/flavor,
  tạo shared external và hai VM demo khác compute/subnet với console account.
- Demo journal tách khỏi migration workload. Marker metadata/description,
  explicit ports và UUID giúp retry khôi phục reply bị mất mà không tạo bản thứ hai.
- Pool `.4`–`.10` thuộc profile lab; bốn IP `.4/.5/.6/.7` phải khác nhau và
  nằm trong pool. CIDR demo không đụng A/B/C/D/EW/N-S. Mật khẩu không vào stdout,
  cloud-init chỉ nhận hash; config/credential file có quyền 0600.
- Precheck chỉ nhận một external flat được cấu hình trên đúng physnet/MTU,
  một subnet IPv4 đúng CIDR/gateway/DHCP/pool; vẫn từ chối provider VM trực tiếp,
  HA/DVR hoặc external khác. Các IP dành cho N-S phải chưa bị chủ khác cấp phát.
- N-S ghi `external_owned=false` trước provisioning; cleanup đối chiếu UUID và
  fingerprint trước mọi thao tác xóa. Shared external không được chuyển thành
  run-owned khi resume. Luồng owned-per-run cũ vẫn hoạt động khi cloud chưa có external.
- Baseline demo/pre-existing chỉ dùng để đối chiếu. Phase 11 và sau cleanup
  xác minh identity, fixed IP/MAC/compute, router gateway, FIP association và
  chuyển đổi MTU/type dự kiến. Thiếu/sai evidence làm JSON validation incomplete.
- Toàn bộ workload và timer/PCAP/probe vẫn giữ. TXT renderer/helpers giữ nguyên
  từng byte so với bản nền; không gán kết quả ping demo thủ công vào metric tự động.

Chưa chạy reset, provisioning, migration, restart container hay API cloud trên
lab. Kiểm tra offline dùng SDK resources, API-state fixtures và Ansible local.
Xem lịch sử [audit 16/10](history/audit-20261016.md).

## Kiểm chứng cuối

- 597 test và 430 subtest PASS, không có test bỏ qua; 561 cảnh báo deprecation
  của OpenStackSDK khi dùng fixtures.
- 12 entrypoint/helper Ansible qua kiểm tra cú pháp, ansible-core 2.16.19.
- 93 file Python và 38 file YAML parse thành công.
- 159 tham chiếu import/include/vars_files Ansible và 39 liên kết tài liệu
  hiện hành hợp lệ. 14 khối Bash trong hướng dẫn và Python nhúng parse thành công.
- Defaults workload, toàn bộ `workload_validation.py`, TXT renderer,
  phase schema, capture/NS/TCP probe và diagnostics coordinator không đổi
  từng byte so với bản nền; whitespace/diff kiểm tra thành công.

Test mới bao gồm real-Ansible render cấu hình/password không lọt log, reset
completion với exact UUID set, hai compute, cloud-init account, lost Nova/FIP
reply, IP collision, external scope, shared ownership và cleanup giữ toàn bộ
bộ demo. Report test kiểm tra FAIL/UNAVAILABLE và baseline bị thiếu không được
biến thành PASS, đồng thời TXT vẫn giống nhau. Không có kiểm chứng traffic demo
thực tế trong các test offline này.
