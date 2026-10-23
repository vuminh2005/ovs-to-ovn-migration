# Chạy migration với A/B/C/D trên lab 4 node

Nền hiện tại là commit `b87da3d`, cùng [bản cập nhật ngày 18/10](history/b87-update-20261018.md).
Lab đã nghiệm thu: controller, network1, compute1, compute2; `eth1` MTU 1450,
transport ping 1450 byte qua ba node thành công. Profile workload là VXLAN
1400 → Geneve 1392. Network2 nằm ngoài inventory đang dùng.

## Thay đổi

| Group | Trước migration | Sau migration | Placement |
|---|---|---|---|
| A | Bốn VM EW ICMP: hai VM đo gốc và hai VM matrix | Xác nhận boot/UUID và packet recovery cho bốn trường hợp compute/subnet | Hai VM trên mỗi compute |
| B | Hai VM kiểm tra DHCP, metadata, MTU | Giữ UUID/port/IP, kiểm tra OVN DHCP/metadata | B.1 compute1; B.2 compute2 |
| C | Chưa tạo | Tạo riêng hai network/subnet Geneve, một router và hai VM | C.1 compute1; C.2 compute2 |
| D | Hai VM TCP dùng chung topology với A/B | Báo cáo riêng ba loại TCP | D.1 compute1; D.2 compute2 |

Với ma trận EW và N-S mặc định, workload tự động tối đa 12 VM, sáu mỗi compute.
Cộng hai VM demo do reset tạo là 14 VM, bảy mỗi compute.
Tên nội bộ trong checkpoint vẫn dùng `0`/`1`: `0` là member .1;
`1` là member .2. `pre.measure`, `pre.existing`, `pre.tcp`, `post.fresh`
tương ứng hai VM gốc của A, B, D, C. Hai VM matrix của A được lưu riêng trong
`extended-resources.json`; khóa checkpoint cũ không đổi.

Precheck kiểm tra full mesh tunnel với gói IP bằng source MTU + 50 byte,
tcpdump trên hai compute và mapping inventory/Nova/hypervisor/Neutron.
VM được yêu cầu đúng host/hypervisor qua Nova microversion 2.74; không fallback
về scheduler tự chọn. Mapping mơ hồ, compute/OVS chết hoặc placement sai sẽ dừng.
Capture A dùng alias inventory đã được xác nhận, không đo trên compute đoán từ IP.

## Ba phép thử TCP của D

| Luồng trong report | Cách chạy |
|---|---|
| `held` | Một kết nối D.1 → D.2:18080 đã trao đổi dữ liệu trước migration. Giữ nguyên socket và session ID; không reconnect sau FIN/RST/lỗi socket. Khi timeout vẫn giữ socket và chờ echo đang pending. |
| `new_old` | Mỗi probe mở một kết nối mới đến listener 18080, gửi echo có run/sequence rồi đóng. |
| `new_listener` | Sau khi Neutron API dừng, controller gửi ARM qua namespace chính xác `qrouter-<UUID>` của topology do run tạo. D.1 yêu cầu D.2 mở listener 18081 rồi bắt đầu kết nối mới đến port đó. |

Port điều khiển D.1 là 18082. SG cho phép TCP giữa các VM thuộc SG của run;
riêng điều khiển chỉ bổ sung CIDR subnet thứ nhất để gateway của router đi vào.
ARM không gọi Neutron API khi nó đang dừng. ARM được ghi checkpoint và idempotent:
gửi lại không đổi thời điểm bắt đầu, không mở listener thứ hai và không đổi socket.
Trước freeze, lệnh CHECK đi qua cùng namespace/port để chứng minh đường điều
khiển đang hoạt động, socket đúng baseline và listener mới chưa được ARM.

Các summary ghi vào serial console mỗi hai giây. Controller lưu lịch sử và dùng
summary cộng dồn để không phụ thuộc vào việc console còn toàn bộ dòng cũ hay không.
Mỗi report giữ attempts, successes, failures, failed-probe percent, thời điểm lỗi
đầu tiên, lần thành công gần nhất và đoạn lỗi đã phục hồi dài nhất theo đồng hồ
monotonic của D.1. Thời gian này là gián đoạn probe ứng dụng; không thay thế metric
PCAP của A và không phải benchmark throughput hay gói MTU lớn.

Baseline ghi boot của cả hai VM, session ID, UUID/port/IP và placement. Nghiệm thu
yêu cầu summary mới hơn anchor, boot và identity cũ, socket cũ còn nguyên, listener
mới được ARM trong cửa sổ freeze và ít nhất năm lần thành công liên tiếp trên cả
ba luồng sau migration. Lỗi còn treo, reboot, mất evidence hoặc socket bị đóng
không được đổi thành PASS. Không reboot A hoặc D để cứu metric.

## Lệnh chạy trên controller

Cập nhật ZIP đầy đủ vào `/root/ovs-to-ovn-migration`, giữ inventory và biến lab:

```bash
cd /root/ovs-to-ovn-migration
source /root/venvs/kolla-2024.1/bin/activate
source /etc/kolla/admin-openrc.sh

ansible-playbook -i /root/multinode migrate-to-ovn.yml --syntax-check
```

Không cần file `migration-lab.yml`. `group_vars/all.yml` đặt sẵn Geneve MTU 1392,
D bật và hai cờ cho phép remediation Group B bằng `true`. Khi không có override
image/flavor qua biến môi trường, phase 03 tìm `ovn-validation-ubuntu-24.04` và
`ovn-validation.small` đang có; không dùng UUID từ lần reset cũ.

Chạy đầy đủ A/B/C/D cùng một lượt:

```bash
ansible-playbook -i /root/multinode migrate-to-ovn.yml
```

Hai cờ reboot mặc định `true`, chỉ cho Group B thuộc run, khi các guard cũ
chứng minh stale DHCP/MTU đủ điều kiện. B được reboot tuần tự và UUID/port/IP phải
giữ nguyên. `true` cho phép xử lý tự động nếu cần, không buộc reboot mọi lần.
Muốn tắt remediation, truyền riêng cờ tương ứng bằng `false`.

Khi demo D thủ công, chạy một **lượt mới trên OVS baseline** với biến cuối:

```bash
ansible-playbook -i /root/multinode migrate-to-ovn.yml \
    -e validation_pair_d_enabled=false
```

Không có chế độ manual trong playbook. D thủ công do bạn quản lý từ đầu đến cuối;
nên dùng topology riêng để không phụ thuộc vào network của A/B bị cleanup.
Biến enabled được giữ trong `validation-config.json` của từng run; resume không
thay đổi vai trò/UUID hoặc tự tạo một D khác.

## Retry lỗi tạo VM đầu tiên ở phase 04

SDK cũ có thể gửi trường phản hồi `OS-EXT-SRV-ATTR:hypervisor_hostname` vào POST
tạo VM và bỏ trường `host`, khiến Nova trả 400. Bản sửa gửi JSON đúng chuẩn
Nova 2.74 qua compute adapter đang xác thực; không cần nâng cấp SDK trên lab.

Nếu phase 04 dừng tại request tạo VM đầu tiên, cập nhật mã rồi chạy:

```bash
ansible-playbook -i /root/multinode archive/playbooks/retry-validation-preparation.yml \
    -e migration_resume_run_dir=/root/ovs-to-ovn-backup/<run-id>
```

Dùng đúng run của lỗi, không lấy run mới nhất tùy tiện. Lệnh kiểm tra marker của
phase 00–03, phase 04 chưa hoàn tất, chưa có VM ghi vào checkpoint và chưa có
staging/MTU change/freeze/cutover. Nó kiểm tra lại cloud vẫn là OVS và placement
không đổi, giữ cấu hình và các UUID đã chuẩn bị, rồi chạy phase 04 đến cuối.
Không tạo run mới, không lặp backup, không cleanup phần chuẩn bị để tạo lại.
Checkpoint đã sang bước sau hoặc đã có VM được ghi nhận sẽ bị từ chối.

## Báo cáo, cleanup và resume

Playbook in kết quả, run ID và đường dẫn hai report ở cuối. Mở bản text:

```bash
cat '/root/ovs-to-ovn-backup/<run-id>/migration-report.txt'
```

Bản text chỉ hiện ba thời gian `Total run duration`, `Control-plane downtime`,
`Dataplane downtime`, rồi hai nhóm East–West và North–South. Nhãn `Dataplane downtime`
giữ giá trị convergence/Port_Binding hiện có; gián đoạn lưu lượng được đo riêng
ở từng luồng. ICMP hiện result, packet loss, failure burst count và longest packet
outage. TCP dùng probe failure rate và longest probe failure, vì đây là phép đo
application probe. Lỗi chưa phục hồi hiện `UNRECOVERED`; counter cũ chưa có hiện
`UNAVAILABLE`. Thời gian làm tròn 3 chữ số thập phân, tỷ lệ 4 chữ số thập phân.
JSON vẫn lưu đầy đủ DHCP/MTU/provisioning/remediation/cleanup, key, độ chính xác,
phase timing, counter và evidence. Điều kiện PASS/FAIL và cleanup giữ nguyên.

Timestamp từng task được lưu riêng trong `<run>/task-timings/`, mỗi lần chạy/resume
có cặp `<invocation-id>.jsonl` và `.json`. JSONL ghi ngay start/end; JSON tổng hợp
theo host khi Ansible kết thúc, gồm cả task lỗi/skip và handler. Có UTC, Unix epoch,
thời lượng monotonic, tên task, host, trạng thái; không lưu args/output/fact.
Chạy từ thư mục repo để nạp `ansible.cfg` mới. Xem chi tiết và giới hạn phép đo
trong phần “Per-task timestamps” của README.

Thay `<run-id>` bằng giá trị vừa in. Không chọn tùy tiện run cũ. Evidence bổ sung:

- `validation-placement.json`, `workload-placement.json`.
- `pair-d-readiness.json`, `pair-d-baseline.json`, `pair-d-precutover.json`.
- `pair-d-trigger.json`, `pair-d-trigger-ack.json`, `pair-d-trigger.log`.
- `pair-d-control-precheck.json`, `pair-d-control-precheck.log`.
- `tcp0-console-records.json`, `tcp1-console-records.json`, `pair-d-tcp.json`.
- `migration-report.json`: nhóm `workload_placement` và `pair_d_tcp`.

Sau khi toàn bộ validation thành công, cleanup xóa C rồi xóa D/A/B cùng topology
trước migration bằng UUID trong checkpoint. Image/flavor dùng chung được giữ.
D bị tắt được báo `DISABLED`; không được báo là TCP PASS. Nếu kiểm tra không đạt,
report là `MIGRATED_VALIDATION_INCOMPLETE`, tài nguyên/evidence được giữ để điều tra,
không tự rollback. SG/network/router bên ngoài checkpoint không bị xóa.

Các route resume cũ đã chuyển vào [archive](../archive/README.md) và không còn
được hỗ trợ cho workflow hiện tại. Giữ nguyên evidence khi run lỗi. Với lab
cần đo lại, reset baseline sau khi sửa nguyên nhân rồi chạy run mới; capture,
UUID và phiên TCP của run mới được tạo riêng.

## Kiểm tra bản sửa

Regression chạy offline gồm socket TCP thật trên loopback, SDK 4.21.0 request
serialization/mapping, HTTP request với SDK 1.5.0 và 4.21.0, guard retry phase 04,
placement sai, retry/lost reply, D bật/tắt, boot/session/
UUID/freshness fences, ARM và cleanup topology chung. Các entrypoint Ansible
được syntax-check. Đây là kiểm chứng mã; lần migration thực tế trên lab vẫn cần
chạy bằng các lệnh bên trên.

Nguồn về requested destination:
[Nova 2024.1: select host/hypervisor with microversion 2.74](https://docs.openstack.org/nova/2024.1/admin/availability-zones.html#using-availability-zones-to-select-hosts).
