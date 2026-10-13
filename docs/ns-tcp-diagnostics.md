# Lần thử tiếp theo: thu bằng chứng TCP SNAT khi migrate OVS sang OVN

Bản này xây từ `playbook-ns-ew-matrix.zip` đã có lưu lỗi socket đầu tiên và
`reset_local_probes.py`. ZIP nguồn ngày 07/10 không có phần N-S mới nên không
được dùng thay cho phiên bản đang chạy. Workflow vẫn chạy đủ phase 00–13.

## Những gì đã biết về run 20261013-182231

- Guest SNAT held dùng cổng `37529`, nhận `Connection reset by peer`, errno
  `104`, khi receive lúc khoảng 19:02:40 (+07:00).
- SNAT mới và FIP held phục hồi. SNAT held chỉ có một lần connect, vẫn FAIL.
- Controller còn socket từ `10.1.0.4:35728`, nhưng bằng chứng cũ không nối
  socket này với ID phiên held. Không coi chênh lệch hai cổng là nguyên nhân
  đã chứng minh.
- PCAP cũ chỉ bắt ICMP EW. Log server cũ chỉ có heartbeat, không có cổng peer.

## Những gì được bổ sung

1. Echo trả thêm `peer_port`. Client lưu `first/last_local_endpoint` (trước
   NAT) và `first/last_peer_endpoint` (server nhìn thấy), cùng ID phiên.
2. SNAT server ghi sự kiện connection-open/close, peer/local tuple, ID
   connection và lỗi nếu có. Heartbeat và tiêu chí held không reconnect giữ
   nguyên. Thông tin cổng lấy từ các echo thành công; không suy ra ánh xạ
   sau cutover của phiên đã gãy bằng cách lấy lại echo cũ.
3. TCP PCAP đầy đủ tại `eth1` controller, tap chính xác của VM SNAT và VM FIP.
   Controller capture bắt đầu trước các client. Guest capture bắt đầu khi VM
   đã chạy, trước freeze. Việc chọn tap dùng đầy đủ UUID Neutron và libvirt,
   không đoán tên tap theo tiền tố. Capture trên host nên không phụ thuộc
   việc restart container Neutron.
4. Snapshot conntrack **chỉ đọc** trước freeze, sau phase 08/trước phase 09 và
   sau validation. Đọc tất cả host trong nhóm network, giữ tuple và zone cho
   luồng SNAT. Lưu setting use_random_fully bằng parser của Neutron khi L3
   container còn chạy. Tool/container/namespace không có được ghi UNAVAILABLE;
   không diễn giải output trống hoặc lỗi command thành mất conntrack.
5. Phase 12 dừng/fetch PCAP dù workload PASS hay FAIL. Khi FAIL workload được
   giữ lại như trước. Reset dừng/fetch capture theo checkpoint trước Kolla
   destroy; không xóa bằng chứng cũ. Không reconnect held, đổi NAT hoặc flush
   conntrack. Nếu đã dừng và lưu đủ file từ trước thì reset không cần đọc lại
   host của generation cũ.

## Giới hạn và điều kiện

- Controller cần `tcpdump`; compute cần các công cụ capture đã dùng cho Pair A.
  `ns_capture_interface: eth1` phải có IP endpoint 10.1.0.187. Sai interface/IP
  hoặc không mở được capture sẽ chặn trước freeze.
- `ns_diagnostics_enabled: true` mặc định. Không đổi use_random_fully, không
  đặt cổng nguồn cố định, không chuyển trạng thái conntrack giữa hai dataplane.
- Snapshot chỉ là ảnh chụp thời điểm, không phải lịch sử đầy đủ; packet capture
  và event ID giúp đối chiếu đúng phiên. Cần phân biệt cổng OVS trước cutover
  và packet được gửi sau takeover. IP nguồn gói RST không tự chứng minh thiết
  bị vật lý phát nó.
- Diagnostics có thể tăng thời gian chuẩn bị và thời gian workflow do các lệnh
  chỉ đọc. Định nghĩa outage của application và ICMP không thay đổi. Marker
  control-plane vẫn bao phủ toàn bộ cửa sổ Neutron API bị freeze.
- Capture hết lifetime, rơi packet, bị thay process hoặc không lưu đủ file được
  ghi UNAVAILABLE. Khi diagnostics bật, cleanup và kết quả SUCCESS cần capture
  PASS; capture PASS không làm phiên held FAIL thành PASS.
- Chỉ chạy qua test/syntax ở môi trường phát triển; chưa chạy bản này trong lab.

## Chạy trên controller sau khi đưa ZIP vào repo và push

Reset xóa cloud/workload hiện tại và dựng lại OVS. Cờ false giữ backup cũ.
Nếu chưa có tcpdump, cài trước reset: `apt-get install -y tcpdump`.

```bash
(
    set -euo pipefail
    source /root/venvs/kolla-2024.1/bin/activate
    cd /root/ovs-to-ovn-migration
    git pull --ff-only
    test -f scripts/ns_diagnostics.py
    test -f scripts/ns_evidence_summary.py
    command -v tcpdump
    ansible-playbook -i /root/multinode reset-lab-to-ovs.yml --syntax-check
    ansible-playbook -i /root/multinode migrate-to-ovn.yml --syntax-check
    ansible-playbook -i /root/multinode reset-lab-to-ovs.yml \
        -e reset_lab_confirm=true \
        -e reset_delete_migration_backups=false
    source /etc/kolla/admin-openrc.sh
    ansible-playbook -i /root/multinode migrate-to-ovn.yml
)
```

Nếu kết thúc MIGRATED_VALIDATION_INCOMPLETE, giữ run đó và đọc bằng chứng;
không tự reset tiếp. Điền thư mục run mới đúng như báo cáo in ra:

```bash
python3 scripts/ns_evidence_summary.py /root/ovs-to-ovn-backup/RUN_MOI
```

File quan trọng trong thư mục run:

- `ns-diagnostics/{endpoint,guest-snat,guest-fip}/tcp.pcap`
- `ns-diagnostics.json`, `ns-diagnostics-checkpoint.json`
- `ns-conntrack-{pre-freeze,post-cutover,post-validation}.json`
- `ns/snat-server.jsonl`, `ns-results.json`, `migration-report.json`

Các bản capture trên compute được giữ lại; khi reset, bản archive tại controller
được ưu tiên nếu checkpoint xác nhận đã dừng. Không xóa backup báo cáo cũ.

## Sửa lỗi khởi động capture và tiếp tục run 20261013-233510

Bản đầu chặn mọi file ngoài hai helper và start.lock. Import helper tạo
`__pycache__` trước khi agent_start kiểm tra thư mục, khiến chính cache Python
bị coi là dữ liệu lạ. Bản sửa tắt tạo bytecode ở agent và chỉ chấp nhận cache
.pyc của hai helper nếu đã tồn tại. PCAP/log/symlink và file lạ vẫn bị chặn.

`retry-ns-diagnostics-preparation.yml` dành riêng cho phase 04 chưa hoàn tất:
Pair A/EW matrix có thể đã chạy, nhưng capture endpoint, VM N-S và local N-S
probe phải chưa từng khởi động. Guard chặn mọi marker phase 05 trở đi, freeze,
DB migration và cutover. Agent inspect kiểm tra không có journal/PCAP/log
khởi động trước khi cho phép một lần launch. Intent lỗi được lưu riêng,
không xóa file cũ hoặc nối lại held. Run, config, port/VM EW và capture ICMP
được giữ nguyên. Playbook tiếp tục phase 04–13, không bootstrap run mới hay reset.

Không dùng retry-validation-preparation.yml cũ: nó chỉ hỗ trợ lỗi tạo VM
đầu tiên. Không chạy lại precheck fresh-cloud vì run này đã tạo external
network/FIP được sở hữu bởi checkpoint. Retry mới kiểm tra OVS source, API,
inventory, placement/config cũ và source agent; phase 04 kiểm tra lại VM/port.

Sau khi cập nhật repo:

```bash
(
    set -euo pipefail
    source /root/venvs/kolla-2024.1/bin/activate
    cd /root/ovs-to-ovn-migration
    git pull --ff-only
    ansible-playbook -i /root/multinode retry-ns-diagnostics-preparation.yml --syntax-check
    ansible-playbook -i /root/multinode retry-ns-diagnostics-preparation.yml \
        -e migration_resume_run_dir=/root/ovs-to-ovn-backup/20261013-233510
)
```

Guard từ chối nếu có bằng chứng process đã launch hoặc nguồn đã đổi; không
tự gỡ guard hay xóa checkpoint để ép retry.

## Cơ chế đã xác nhận và chế độ thử giữ cổng nguồn

Run 20261013-233510 đã ghi cùng ID phiên held:

- Guest: 10.233.0.11:51547. Server trước cutover: peer 10.1.0.4:45042.
- Source profile bằng parser Neutron: use_random_fully=true.
- 1791912608.637558: packet TCP [P.] từ 10.1.0.4:51547 đến
  10.1.0.187:18180, seq 3358413950:3358414039, ack 1859599645.
- 1791912608.637576: RST trả về 10.1.0.4:51547, seq 1859599645.
- Guest tap ghi RST có cùng sequence, đích 10.233.0.11:51547.

Tuple ở ngoài đổi từ cổng 45042 sang 51547. Phiên cũ phía server không thể
được bảo toàn bằng một tuple khác. Lỗi không phải do cổng đích 18180 thay đổi.
Legacy conntrack snapshot thiếu dữ liệu vì quyền vào namespace, sau cutover
container L3 dừng; không được diễn giải UNAVAILABLE thành entry không tồn tại.
Lệnh snapshot mới dùng docker exec --privileged --user root cho đúng lệnh
conntrack -L; không đổi quyền container lâu dài, flush hoặc ghi NAT/conntrack.

Cờ ns_preserve_snat_source_port=true là thử nghiệm có điều kiện, mặc định
false. Reset ghi [AGENT] use_random_fully=false trước Kolla deploy. Reset và
phase 02 đọc cấu hình effective bằng parser của Neutron trước khi tạo workload.
Probe kiểm tra cổng guest và cổng server thấy từ các echo thành công; nếu
không bằng nhau thì chặn migration trước freeze. Báo cáo ghi rõ chế độ thử.

Tắt random-fully không bảo đảm giữ cổng khi có collision. Đây không phải
chuyển trạng thái conntrack, không phải giải pháp chung bảo toàn mọi NAT
session đã tồn tại. Phải bật trước khi tạo session mới; run cũ vẫn FAIL.
Tiêu chí giữ cùng connection ID, một lần connect, không reboot N-S và phục
hồi echo đều giữ nguyên. Không reconnect held để làm PASS. Khi so sánh báo
cáo, phải phân biệt profile mặc định và profile thử này.

### Lệnh cho lần thử mới

Pass cờ cho cả reset và migration. Cờ reset_delete_migration_backups=false
giữ bằng chứng run cũ dù cloud/workload hiện tại bị xóa.

```bash
(
    set -euo pipefail
    source /root/venvs/kolla-2024.1/bin/activate
    cd /root/ovs-to-ovn-migration
    git pull --ff-only
    test -f playbooks/ns-source-profile-tasks.yml
    ansible-playbook -i /root/multinode reset-lab-to-ovs.yml --syntax-check
    ansible-playbook -i /root/multinode migrate-to-ovn.yml --syntax-check
    ansible-playbook -i /root/multinode reset-lab-to-ovs.yml \
        -e reset_lab_confirm=true \
        -e reset_delete_migration_backups=false \
        -e ns_preserve_snat_source_port=true
    source /etc/kolla/admin-openrc.sh
    ansible-playbook -i /root/multinode migrate-to-ovn.yml \
        -e ns_preserve_snat_source_port=true
)
```

Chế độ thử vẫn đi hết phase 00–13. Nếu source-port gate, precheck, DB hoặc
cutover lỗi thì dừng tại task lỗi. Nếu held tiếp tục FAIL sau takeover, lưu
report và PCAP như trước; không thay tiêu chí validation hay tự rollback.
