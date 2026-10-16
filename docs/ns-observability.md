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
cần năm request liên tiếp có reply và đúng cadence. Kernel capture drop,
supervisor gap hoặc kết thúc chưa phục hồi tạo UNAVAILABLE, không phải 0 s.
Sequence/cadence gap vẫn làm raw packet-loss metric UNAVAILABLE; một trường hợp
đối chiếu local error rất cụ thể có thể chứng nhận riêng probe availability như
mô tả bên dưới. EW giữ nguyên quy tắc PCAP strict.
Các file cũ mang tên `tcp.pcap` được giữ để tương thích, nhưng filter mới bao gồm
TCP hai listener, ICMP và ARP. JSON `ns-icmp-results.json` lưu kết quả độc lập.

Client ICMP giữ nguyên một tiến trình `ping -D -O`, source/payload/cadence cũ.
Mỗi dòng stdout/stderr được ghi với run, label, boot, PID, log ID, event index,
timestamp nguồn khi có, thời gian quan sát wall/monotonic và sequence. Reply,
outstanding và lỗi `sendmsg`/unreachable được phân loại; toàn bộ dòng gốc vẫn giữ.
Giới hạn 64 MiB mỗi log, không quay vòng hoặc ghi đè. Lỗi ghi log không khởi động
lại ping hoặc thay đổi socket TCP; health summary ghi rõ `log_error`.

FIP ghi trực tiếp ở `ns/fip-icmp-sender.jsonl` trên controller. Guest SNAT ghi
`/var/lib/ovn-migration-icmp/<run>/snat-icmp-sender.jsonl`, đồng thời mirror qua
Nova console bằng prefix riêng `OVN_NS_ICMP_JSON `. Collector ghép các lần đọc
console vào `ns/snat-icmp-sender.jsonl`, deduplicate và kiểm tra event index,
boot/PID/log identity, monotonic clock và coverage giữa hai PCAP anchor.
Console mirror là nguồn phụ, không còn là bản SNAT bắt buộc phải liên tục.
Với run mới, `icmp_sender_export_enabled: true` được lưu trong validation config.
Phase 12 chỉ yêu cầu export **sau khi cả ba N-S captures đã dừng/fetch**:

1. Controller gửi yêu cầu có token/nonce qua listener SNAT cũ đang có trên
   endpoint, kèm run, boot, ping PID/log ID/probe ID từ baseline.
2. Echo server gắn thông báo export vào response tiếp theo. Client SNAT mở
   socket outbound riêng để gửi prefix hoàn chỉnh của file gốc trong guest.
   Không dùng lại held socket, không tăng connection_attempts của held, không
   restart ping hoặc listener. Không mở listener/VM/FIP/SG mới, không cần SSH,
   guest-agent hoặc qrouter namespace sau cutover.
3. Guest chờ native timestamp trong log vượt thời điểm stop capture SNAT +2 s,
   tối đa 60 s; tránh console/raw-reader lag làm thiếu đoạn cuối. File vẫn append
   trong lúc export, chỉ snapshot phần kết thúc bằng newline đầy đủ.
4. Receiver kiểm tra token/nonce/identity, giới hạn 64 MiB, SHA256, toàn bộ event
   index từ event 1 và monotonic ordering trước publish. Chỉ nhận đường dẫn cố
   định từ config, không nhận remote path. File đầy đủ là
   `ns/snat-icmp-sender.full.jsonl`, kèm `.full.receipt.json` và request checkpoint;
   mirror cũ được giữ riêng. File đã publish không bị ghi đè.

Collection có timeout 90 s, upload tối đa ba lượt với cùng nonce. Mỗi receiver
có timeout tổng 60 s, không lặp vô hạn. Hash/ownership được kiểm tra lại trước
phân tích. Lỗi export không được biến thành mirror PASS hoặc che network failure.
Log gốc cần vượt cả hai PCAP anchors; snapshot chưa đủ coverage vẫn UNAVAILABLE.

Sender stdout là bằng chứng thứ cấp về sequence/lỗi, không phải timestamp phát
packet ở wire. Sequence thiếu trong PCAP được mô tả trong `request_discontinuities`
và đối chiếu sender events cùng khoảng thời gian (tolerance 2 giây, cần kiểm tra
clock offset). `observed_window` giữ burst thấy trong PCAP với `certified: false`
khi coverage chưa đủ. Không tự điền request thiếu trong PCAP.

N-S có nhánh `measurement_kind: icmp-probe-availability` chỉ khi mọi sequence
thiếu đều khớp chính xác **một** sender error `From <source> ... Destination Host
Unreachable`, đúng original identity/continuous event coverage và timestamp.
Mỗi gap phải nằm bên trong một burst đã có captured failure và captured recovery;
không dùng timestamp báo lỗi để giả làm timestamp gửi probe. Identifier đổi,
capture drop, lỗi không map được, reply mâu thuẫn, thiếu log hoặc lỗi local mở
một burst mới mà không biết thời điểm gửi đều giữ UNAVAILABLE.

Nhánh này thêm local_send_failures/probe_attempts/probe_failures và probe failure
percent. Raw `packets_*` không đổi, `packet_loss_percent` vẫn null khi wire
coverage thiếu; toàn bộ kết quả strict cũ nằm trong `wire_capture_metrics`.
TXT hiển thị Probe failure rate / Local send failures / Longest probe outage /
Total probe downtime cho trường hợp đó, không gọi chúng là wire packet loss.
Log sender thiếu/đứt đoạn vẫn là N-S diagnostics UNAVAILABLE khi logging được bật.

`migration-report.txt` hiển thị ICMP và ba TCP stream cho cả SNAT/FIP: loss/probe
failure rate, failure burst count, longest outage, total downtime và bốn mốc bắt
đầu/kết thúc đo, first failure/recovery. Cả EW ICMP/TCP và N-S đều hiển thị các
mốc đó dưới dạng `phase | task`; TXT không in timestamp.
JSON giữ timestamp, toàn bộ burst, session identity, nguyên nhân lỗi và clock anchors.
Task attribution chỉ là thời gian trùng nhau, không phải kết luận nhân quả.

`Dataplane downtime:` ở đầu TXT chỉ là tiêu đề cho downtime thực tế từng luồng;
không hiện giá trị timer Port_Binding và không cộng downtime giữa các luồng.
Timer Port_Binding vẫn lưu đầy đủ trong JSON. EW ICMP dùng timestamp PCAP;
EW TCP dùng ARM đến summary cuối, quy đổi monotonic bằng wall clock trong chính
summary của client. `first_recovery_mono` lưu đúng lần phục hồi burst đầu tiên;
với summary cũ chỉ suy ra recovery từ duration khi có đúng một burst đã phục hồi.
Nếu có nhiều burst mà thiếu mốc recovery đầu tiên thì ghi UNAVAILABLE.

## Collector trên host

`migration_diagnostics_enabled: true` mặc định. Tham số ở `group_vars/all.yml`:

- `migration_diagnostics_directory`: `/var/lib/ovn-migration-diagnostics`.
- `migration_diagnostics_lifetime_seconds`: 18000.
- `migration_diagnostics_max_bytes`: 268435456 cho mỗi collector.
- `migration_diagnostics_reconnect_attempts`: 8 lần tối đa cho mỗi observer.
- `migration_diagnostics_reconnect_window_seconds`: 60 giây để thử nối lại mỗi gap.
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
   counters và conntrack trước freeze và sau traffic phục hồi. Sampling bổ sung
   mỗi khoảng 2 giây chỉ bật sau phục hồi; không xếp hàng vô hạn khi command chậm.

Collector chạy ngoài container, có PID/starttime/boot identity và run-owned
directory. Container-side monitor có token riêng cho từng lần launch; cleanup
kiểm tra token trước khi signal đúng PID. Không dùng pkill hoặc flush mạng.
Nếu mất PID acknowledgement, cleanup tìm đúng token của lần launch trong container
và phải chứng minh tiến trình cũ đã dừng trước khi tạo observer thay thế.
Byte/lifetime limit kết thúc với UNAVAILABLE và giữ dữ liệu, không quay vòng ghi
đè bằng chứng đầu run. Snapshot/command lỗi được giữ nguyên với timestamp/rc.

## Vòng đời và lỗi

Trước freeze phải CHECK collector còn sống, không có observer bắt buộc bị mất.
Run mới chờ snapshot `pre-freeze` thực hiện xong trước freeze. Năm checkpoint
tổng hợp giữa cutover được bỏ; dump reconnect bổ sung hoãn đến sau phục hồi.
PCAP, log/change streams và snapshot conntrack N-S chuyên biệt vẫn giữ.
Xem [thứ tự và đánh đổi](cutover-workflow.md). Overhead còn lại vẫn được tính
trong timer thực tế; instrumentation không được coi là miễn phí.

Phase 12 dừng/fetch host observers và N-S captures ngay cả khi workload validation
FAIL. Callback của workflow chính cố gắng dừng/fetch khi Ansible kết thúc do lỗi
sớm; SIGKILL/mất nguồn có thể ngăn callback chạy, khi đó có lifetime limit và
reset-stop để thu hồi. Không biến evidence thiếu thành PASS hoặc xóa workload.

Retry/resume trước phase 08 có thể tạo observation segment mới. Mọi segment cũ
và preparation gap được giữ, không nối giả thành một capture liên tục. Không tự
restart supervisor để che gap xảy ra trong/sau cutover. Reset dừng/archive các
collector thuộc checkpoint trước Kolla destroy, giữ các report cũ.

Riêng các connection observer OpenFlow/OVSDB có reconnect giới hạn trong chính
supervisor đang chạy: backoff 1, 2, 4 rồi tối đa 8 giây, giới hạn tổng số lần thử.
Mỗi lần có segment/PID/token riêng; log cũ được append, không ghi đè. Reconnect
chỉ được xác nhận khi có monitor reply/initial JSON, không chỉ vì process sống.
Sau reconnect lấy initial flow state; dump tổng hợp hoãn đến sau phục hồi theo
policy mới. Lỗi hoặc handshake im lặng
không được coi là phục hồi. PCAP và tiến trình traffic không được restart.

`observer-gap`, `observer-reconnected`, `observer-reconnect-exhausted` và các
segment được giữ trong events/state và report JSON. Gap có thời gian phát hiện,
output cuối và thời gian xác nhận kết nối lại; duration là từ phát hiện đến ACK,
không phải phép đo downtime traffic. Coverage là `GAPPED` nếu từng bị ngắt.
Reconnect không xóa lỗi cũ. `migration-diagnostics.json.status` vẫn UNAVAILABLE
khi coverage thiếu; `diagnostic_coverage` trong report giữ GAPPED/UNAVAILABLE,
warning theo host/observer và tổng gap duration. Format TXT hiện hành không in
phần diagnostics; đọc các trường này trong JSON. Đây không phải số downtime
traffic và không chứng nhận dữ liệu trong gap.

Functional traffic validation không yêu cầu forensic stream liên tục nữa.
Collector vẫn phải dừng/fetch và chứng minh `cleanup_complete`, được tổng hợp
thành `collection_status: STOPPED`; không chứng minh được shutdown thì cleanup
vẫn bị chặn. PCAP/N-S sender/TCP/VM identity/cấu hình/boot/network checks không
được nới lỏng. Run có thể SUCCESS với diagnostics GAPPED được công khai nếu mọi
kiểm chứng traffic/workload và cleanup đều đạt. Archive legacy hoàn chỉnh vẫn
được đọc; archive legacy UNAVAILABLE thiếu proof cleanup không tự trở thành PASS.

Trong thư mục run:

- `migration-diagnostics.json`: trạng thái, giới hạn, collector/segment index.
- `diagnostics/<host>/<role>/`: events, source streams, schemas, PCAP và state.
- `diagnostics/checkpoints/`: danh tính/cấu hình của collector được sở hữu.
- `ns-arm.json`: timestamp mở listener, ACK và session/ARM continuity.
- `ns-results.json`, `ns-icmp-results.json`, `migration-report.{json,txt}`.
- `ns/{snat,fip}-icmp-sender.jsonl`: log sender gốc hoặc mirror có kiểm tra index.
- `ns/snat-icmp-sender.full.jsonl`: bản export nguyên byte từ file sender guest.
- `ns/snat-icmp-sender.full.receipt.json`: identity, số byte, SHA256 và event index.
- `ns/snat-icmp-export-request.json`: nonce, identity và capture time fence.

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
