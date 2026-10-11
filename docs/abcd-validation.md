# Chạy migration với A/B/C/D trên lab 4 node

Bản sửa dựa trên ZIP commit `7de41d3`; reset 4 node được giữ nguyên.
Lab đã nghiệm thu: controller, network1, compute1, compute2; `eth1` MTU 1450,
transport ping 1450 byte qua ba node thành công. Profile workload là VXLAN
1400 → Geneve 1392. Network2 nằm ngoài inventory đang dùng.

## Thay đổi

| Pair | Trước migration | Sau migration | Placement |
|---|---|---|---|
| A | Hai VM đo ping và PCAP liên tục | Xác nhận boot/UUID và packet recovery | A.1 compute1; A.2 compute2 |
| B | Hai VM kiểm tra DHCP, metadata, MTU | Giữ UUID/port/IP, kiểm tra OVN DHCP/metadata | B.1 compute1; B.2 compute2 |
| C | Chưa tạo | Tạo riêng hai network/subnet Geneve, một router và hai VM | C.1 compute1; C.2 compute2 |
| D | Hai VM TCP dùng chung topology với A/B | Báo cáo riêng ba loại TCP | D.1 compute1; D.2 compute2 |

Khi D bật, tối đa tám VM, bốn network/subnet và hai router thuộc run.
Tên nội bộ trong checkpoint vẫn dùng `0`/`1`: `0` là member .1;
`1` là member .2. `pre.measure`, `pre.existing`, `pre.tcp`, `post.fresh`
tương ứng A, B, D, C.

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

Đưa `ovs-to-ovn-migration-abcd.zip` vào `/root`, rồi giải nén sang thư mục mới:

```bash
python3 -m zipfile -e /root/ovs-to-ovn-migration-abcd.zip /root/ovn-migration-abcd
source /root/venvs/kolla-2024.1/bin/activate
source /etc/kolla/admin-openrc.sh
cd /root/ovn-migration-abcd/ovs-to-ovn-migration

ansible-playbook -i /root/multinode migrate-to-ovn.yml --syntax-check
```

Không cần file `migration-lab.yml`. `group_vars/all.yml` đặt sẵn Geneve MTU 1392,
D bật và hai cờ cho phép remediation Pair B bằng `true`. Khi không có override
image/flavor qua biến môi trường, phase 03 tìm `ovn-validation-ubuntu-24.04` và
`ovn-validation.small` đang có; không dùng UUID từ lần reset cũ.

Chạy đầy đủ A/B/C/D cùng một lượt:

```bash
ansible-playbook -i /root/multinode migrate-to-ovn.yml
```

Hai cờ reboot mặc định `true`, chỉ cho Pair B thuộc run, khi các guard cũ
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

## Báo cáo, cleanup và resume

Playbook in chính xác thư mục `/root/ovs-to-ovn-backup/<run-id>` ở cuối:

```bash
cat '/root/ovs-to-ovn-backup/<run-id>/migration-report.txt'
```

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

Nếu đã qua cutover và gate late-resume hiện có chấp nhận checkpoint, tiếp tục:

```bash
ansible-playbook -i /root/multinode resume-after-cleanup.yml \
    -e 'migration_resume_run_dir=/root/ovs-to-ovn-backup/<run-id>'
```

Entrypoint này không lặp freeze/DB sync/ARM. Nếu dừng trước freeze/cutover,
không chạy lại full migration hoặc late-resume tùy tiện; kiểm tra log/checkpoint
trước. Run mới luôn tạo run ID mới, không tiếp quản workload của run trước.

## Kiểm tra bản sửa

Regression chạy offline gồm socket TCP thật trên loopback, SDK 4.21.0 request
serialization/mapping, placement sai, retry/lost reply, D bật/tắt, boot/session/
UUID/freshness fences, ARM và cleanup topology chung. Các entrypoint Ansible
được syntax-check. Đây là kiểm chứng mã; lần migration thực tế trên lab vẫn cần
chạy bằng các lệnh bên trên.

Nguồn về requested destination:
[Nova 2024.1: select host/hypervisor with microversion 2.74](https://docs.openstack.org/nova/2024.1/admin/availability-zones.html#using-availability-zones-to-select-hosts).
