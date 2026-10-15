# N-S ICMP, listener bổ sung và diagnostics xuyên migration

Bản cập nhật dựa trên `current-playbook.zip` do người dùng cung cấp. Không đổi
thuật toán migration, NAT profile, MTU hoặc thứ tự stop/deploy/restart hiện có.
Các collector chỉ đọc trạng thái mạng. Việc mở listener và gửi probe là workload
validation chủ động; chúng có thể ảnh hưởng ARP/neighbor refresh và thời gian đo.

## Tám luồng N-S

Mỗi chiều SNAT outbound và FIP inbound có:

| Luồng | Khởi chạy | Cách kiểm tra |
|---|---|---|
| ICMP | Phase 04 | Một process ping liên tục, source IP cố định, payload 56 byte, interval dùng chung EW |
| TCP held | Phase 04 | Một socket sau khi establish; không reconnect khi timeout hoặc gãy phiên |
| TCP new-old | Phase 04 | Mỗi probe tạo kết nối mới tới listener cũ |
| TCP new-listener | Phase 07 sau khi Neutron API đã dừng | Mỗi probe tạo kết nối mới tới listener bổ sung |

Các cổng mặc định trong `group_vars/ns-lab.yml`:

- Listener cũ: `ns_tcp_port: 18180`.
- Listener bổ sung: `ns_tcp_new_port: 18181`.
- Control client: `ns_tcp_control_port: 18182`.
- ICMP: `ns_icmp_enabled: true`.

Hai listener dùng cùng endpoint/FIP; không tạo thêm VM, FIP hoặc router.
Security group cho ICMP và cả hai cổng dịch vụ được chuẩn bị ở phase 04.
Control của guest SNAT chỉ cho source gateway nội bộ của router do run sở hữu.
Control FIP client chỉ bind loopback của controller.

Client có control token riêng của run. CHECK trước freeze phải chứng minh held
đã establish và client chưa ARM. Sau khi xác nhận neutron-server đã dừng,
controller gửi ARM qua qrouter namespace chính xác cho client SNAT và loopback
cho client FIP. Client dùng socket control riêng tới listener cũ để yêu cầu OPEN;
không dùng socket held làm control, không gọi Neutron API.

OPEN và ARM idempotent: retry giữ listener ID, boot ID, held connection ID và
ARM epoch. Task phải thấy echo thành công từ listener mới trước khi chuyển sang
dataplane cutover. Không tính các probe new-listener trước khi ARM vì lúc đó
thread chưa phát probe. Lỗi chuẩn bị không được biến thành downtime migration.

## Cách đo ICMP và TCP

TCP giữ cách đo application echo bằng client monotonic. JSON lưu từng burst
cùng source wall-clock timestamp, tổng downtime và trạng thái chưa phục hồi.
Counters trước baseline chỉ là dữ liệu startup, không cộng vào cửa sổ migration.

ICMP dùng raw sender-side PCAP:

- SNAT: capture trên tap VM SNAT, trước khi NAT có thể đổi ICMP identifier.
- FIP: capture eth1 endpoint, nơi phát request tới FIP.

Baseline và endpoint phải là các request/reply có thật trong PCAP. Endpoint cuối
cần năm request liên tiếp có reply và đúng cadence. Sequence gap, kernel capture
drop, supervisor gap hoặc kết thúc chưa phục hồi tạo UNAVAILABLE, không phải 0 s.
Các file cũ mang tên `tcp.pcap` được giữ để tương thích, nhưng filter mới bao gồm
TCP hai listener, ICMP và ARP. JSON `ns-icmp-results.json` lưu kết quả độc lập.

`migration-report.txt` hiển thị ICMP và ba TCP stream cho cả SNAT/FIP: loss/probe
failure rate, failure burst count, longest outage, total downtime, timestamp bắt
đầu/kết thúc đo và first failure/recovery kèm task/phase. Timestamp text là UTC+7.
JSON giữ toàn bộ burst, session identity, nguyên nhân lỗi và clock anchors.
Task attribution chỉ là thời gian trùng nhau, không phải kết luận nhân quả.

Nhãn lịch sử `Dataplane downtime` ở đầu TXT vẫn là timer Port_Binding cũ.
Downtime traffic thực tế nằm trong từng luồng; không cộng downtime giữa các luồng.

## Collector trên host

`migration_diagnostics_enabled: true` mặc định. Tham số ở `group_vars/all.yml`:

- `migration_diagnostics_directory`: `/var/lib/ovn-migration-diagnostics`.
- `migration_diagnostics_lifetime_seconds`: 18000.
- `migration_diagnostics_max_bytes`: 268435456 cho mỗi collector.
- `migration_diagnostics_external_interface`: `ns-ex` của lab hiện tại.

Core collector chạy từ phase 04 trên control/network/compute/OVN CLI host.
Central collector bắt đầu cuối phase 06, sau khi đã có endpoint NB/SB chính xác
và trước DB migration phase 07. Không phụ thuộc neutron-server sống trở lại.

Thu thập:

1. Log file Neutron, OVN và Open vSwitch, bao gồm rotation dạng `.log.1`.
   JSON lưu inode/offset, source text và thời điểm observer nhận dữ liệu.
2. Docker lifecycle events; journal systemd nếu có. Journal là nguồn bổ sung,
   thiếu journal được ghi trong observer status, không coi là thiếu packet evidence.
3. OVSDB local change stream và OpenFlow flow-change watch trên br-int.
   Watch là service connection OF1.0/Nicira cạnh controller OVN OF1.5, không thêm
   primary controller và không đổi Bridge.protocols. Nếu không hỗ trợ, preflight
   chặn trước freeze; không tự sửa cấu hình để ép collector hoạt động.
4. NB Logical Router/Router Port, gateway chassis hoặc HA tables có trong schema,
   NAT, NB_Global; SB Chassis, Chassis_Private, Port_Binding, MAC_Binding, SB_Global.
   Schema runtime quyết định table/column được monitor.
5. Tunnel PCAP VXLAN/Geneve trên interface tunnel của từng chassis và PCAP
   ARP/ICMP/TCP tại external OVS port network1.
6. Snapshot version, clock/chrony, neighbors, link, FDB, OpenFlow flow/group/port
   counters và conntrack tại các checkpoint. Sau pre-freeze có sample nhẹ best
   effort mỗi khoảng 2 giây; không xếp hàng vô hạn khi command chạy chậm.

Collector chạy ngoài container, có PID/starttime/boot identity và run-owned
directory. Container-side monitor có token riêng cho từng lần launch; cleanup
kiểm tra token trước khi signal đúng PID. Không dùng pkill hoặc flush mạng.
Byte/lifetime limit kết thúc với UNAVAILABLE và giữ dữ liệu, không quay vòng ghi
đè bằng chứng đầu run. Snapshot/command lỗi được giữ nguyên với timestamp/rc.

## Vòng đời và lỗi

Trước freeze phải CHECK collector còn sống, không có observer bắt buộc bị mất.
Checkpoint được request bất đồng bộ quanh pre-freeze, stop legacy agent, OVN
takeover, VM binding ready, cleanup và Neutron restart. Các bước này có overhead
và vẫn được tính trong timer thực tế; không hứa hẹn instrumentation không tốn thời gian.

Phase 12 dừng/fetch host observers và N-S captures ngay cả khi workload validation
FAIL. Callback của workflow chính cố gắng dừng/fetch khi Ansible kết thúc do lỗi
sớm; SIGKILL/mất nguồn có thể ngăn callback chạy, khi đó có lifetime limit và
reset-stop để thu hồi. Không biến evidence thiếu thành PASS hoặc xóa workload.

Retry/resume trước phase 08 có thể tạo observation segment mới. Mọi segment cũ
và preparation gap được giữ, không nối giả thành một capture liên tục. Không tự
restart collector để che gap xảy ra trong/sau cutover. Reset dừng/archive các
collector thuộc checkpoint trước Kolla destroy, giữ các report cũ.

Trong thư mục run:

- `migration-diagnostics.json`: trạng thái, giới hạn, collector/segment index.
- `diagnostics/<host>/<role>/`: events, source streams, schemas, PCAP và state.
- `diagnostics/checkpoints/`: danh tính/cấu hình của collector được sở hữu.
- `ns-arm.json`: timestamp mở listener, ACK và session/ARM continuity.
- `ns-results.json`, `ns-icmp-results.json`, `migration-report.{json,txt}`.

Lịch sử này dùng để kiểm chứng ba câu hỏi: flow/controller change nào trùng
packet lỗi đầu tiên; EW phục hồi qua pipeline/tunnel nào; gateway binding/ARP/NAT
hoàn tất trước hay sau Neutron restart. Thu thập không đồng nghĩa luôn đủ bằng
chứng kết luận một nguyên nhân duy nhất; report không tự suy diễn quan hệ nhân quả.

## Yêu cầu và kiểm tra

Host cần Python 3, tar, tcpdump, ip/bridge và quyền root như workflow hiện có.
Controller/guest cần ping. OVS/OVN container cần ovsdb-client/ovs-ofctl.
Chrony và systemd journal là nguồn bổ sung, có thể UNAVAILABLE.

Không cần cài thư viện Python mới cho các probe/collector runtime. Các bài kiểm
tra phát triển dùng pytest, openstacksdk, SQLAlchemy và ansible-core. Bản này đã
được kiểm tra cục bộ; chưa thực thi migration trên lab của người dùng.
