> Cập nhật 17/10/2026: external/subnet được reset tạo sẵn và dùng chung với
> bộ demo console. Workload N-S giữ router/FIP/VM riêng với `.4/.5`; demo dùng
> `.6/.7`. Automatic cleanup giữ external và toàn bộ bộ demo. Tổng tối đa 14 VM
> khi có hai VM demo cộng 12 VM validation. Xem [hướng dẫn hiện hành](demo.md).

# Mở rộng EW và N-S — hướng dẫn hiện hành

Bản nền là `playbook(1).zip` người dùng gửi ngày 13/10/2026. Workspace không
có patch/checkpoint sửa dở của lần yêu cầu bị lỗi. Các file review và patch reset
cũ trong ZIP là tài liệu lịch sử, không phải patch cần áp dụng lại.

## Thay đổi đã thực hiện

1. Reset dựng uplink N-S bền vững trên network1, trước reboot/deploy.
2. Quota project mặc định chỉ đọc. Tùy chọn `increase_quota=true` cho phép nâng
   instance quota lên tối thiểu `instance_quota_limit` (mặc định 20), trước workload.
   Pre-provision tính cả hai VM tạo sau
   migration và kiểm tra quota cores/RAM, RAM/disk còn cấp phát trên từng compute.
3. Thêm đúng hai VM để hoàn thiện ba tình huống ICMP còn thiếu; dùng chung hai
   VM đo liên tục, không dùng VM DHCP/MTU có thể reboot.
4. Thêm topology N-S sở hữu bởi từng run: một tenant network/subnet, router,
   external flat network/subnet, hai VM, gateway `.4` và FIP `.5`.
5. SNAT outbound và FIP inbound đều chạy TCP held/new trước, trong và sau
   migration. Phiên held đã mở không reconnect. Báo cáo kiểm tra guest boot,
   probe process, listener, connection, nguồn NAT và UUID/mapping tài nguyên.
6. Tên VM mới và tiêu đề report dùng mô tả nhiệm vụ. Các khóa/checkpoint A/B/C/D
   cũ vẫn là alias kỹ thuật để không phá lịch sử metric và recovery.

## Topology và số lượng

| Tên phép đo | Nguồn → đích | Compute | Subnet |
|---|---|---|---|
| `ew_different_compute_different_subnet` | VM đo 0 → VM đo 1 | khác | khác |
| `ew_same_compute_different_subnet` | VM đo 0 → VM matrix 0 | cùng compute1 | khác |
| `ew_different_compute_same_subnet` | VM matrix 0 → VM đo 1 | khác | cùng network/subnet 1 |
| `ew_same_compute_same_subnet` | VM đo 1 → VM matrix 1 | cùng compute2 | cùng network/subnet 1 |
| Existing DHCP/MTU/metadata | hai VM có guard remediation | khác | khác |
| Fresh OVN provisioning | hai VM chỉ tạo sau cutover | khác | khác |
| EW TCP continuity | hai VM TCP của thiết kế nền | khác | khác |
| NS SNAT outbound | VM outbound → controller | VM trên compute1 | qua router `.4` |
| NS FIP inbound | controller → `.5` → VM inbound | VM trên compute2 | DNAT vào tenant |

Hai VM matrix cùng network/subnet với VM đo 1: matrix 0 trên compute1,
matrix 1 trên compute2. Không tạo network/router riêng cho ba luồng ICMP bổ sung.
Placement dùng cùng resolver inventory → Nova host/hypervisor → Neutron binding
host và API Nova 2.74 như bản nền.

| Nhóm | compute1 | compute2 |
|---|---:|---:|
| ICMP liên tục + existing DHCP + EW TCP | 3 | 3 |
| Matrix bổ sung | 1 | 1 |
| N-S | 1 | 1 |
| Fresh OVN, tạo sau migration | 1 | 1 |
| **Tối đa** | **6** | **6** |

Trước cutover có 10 VM, sau khi tạo workload mới có 12 VM. Khi mọi kiểm tra
đạt, cleanup xóa tài nguyên của run; khi không đạt, giữ tài nguyên để điều tra.
Giữa các reset tạo UUID/MAC guest mới; trong một run giữ nguyên VM/port/network.
Địa chỉ `.4/.5` luôn là hai địa chỉ external đã được cấp cho bài lab.

## Uplink và cấu hình

Các địa chỉ nằm trong `group_vars/ns-lab.yml`, theo baseline đã kiểm chứng:

| Thành phần | Giá trị |
|---|---|
| Network1 underlay | `10.1.0.90/24`, MTU 1450 |
| Controller endpoint | `10.1.0.187` |
| External subnet/gateway | `10.1.0.0/24`, gateway `10.1.0.1` |
| Router SNAT / Floating IP | `10.1.0.4` / `10.1.0.5` |
| Physnet / external type | `physnet1` / `flat` |
| Tenant subnet | `10.233.0.0/24` |
| VM outbound / inbound | `10.233.0.11` / `10.233.0.20` |
| TCP port | `18180`, riêng với port 18080 của baseline thủ công |

Reset kiểm tra IP/MAC/MTU trước khi dừng guest. Network1 giữ MAC của eth1 và
chuyển IP underlay sang `br-underlay`; `eth1` là member của Linux bridge.
Cặp `ns-uplink ↔ ns-ex` nối Linux bridge tới `br-ex`, do Kolla cắm `ns-ex` vào OVS.
Netplan giữ cấu hình eth0 và các NIC khác; snapshot đầy đủ Netplan được lưu dưới
`/var/lib/ns-underlay/<timestamp>/`. Cloud-init được chặn tái sinh cấu hình NIC
cũ, còn Netplan hiện tại của eth0 được giữ. Service `ns-underlay-veth.service`
tạo lại veth sau boot.

Globals Kolla dùng biểu thức theo host: tunnel network1 là `br-underlay`, tunnel
compute1/compute2 là `eth1`; external network1 là `ns-ex`. Precheck render globals
theo từng host và kiểm tra cấu hình OVS `local_ip`/`bridge_mappings` thực tế.
Sau cutover kiểm tra `ovn-bridge-mappings` và gateway chassis.

External flat MTU 1450 được bảo toàn. Phase 06 chỉ giảm MTU network VXLAN thêm
8 byte; phase 12 audit VXLAN → Geneve và flat → flat riêng.

**Outer policy vẫn do mentor quản lý trên cloud bên ngoài.** Playbook này không
có credentials/API của cloud outer để sửa policy đó. Sau mỗi lần dựng, baseline
SNAT/FIP thực tế phải đạt trước freeze. Nếu policy whitelist MAC không còn phù
hợp, playbook dừng trước migration; `ns-outer-policy.json` ghi gateway port/MAC
và `.4/.5` để mentor đối chiếu. Việc đổi UUID/MAC inner không bị che bằng cách
reboot hay tự tạo lại VM đo.

## Metric và mốc đo

- ICMP gốc giữ nguyên công thức PCAP. Ba luồng mới có capture trên tap nguồn
  riêng, thư mục riêng và bộ lọc nguồn/đích riêng. Bộ đọc vẫn kiểm tra ICMP
  identifier/sequence, request cadence, capture drop/lifetime và loss cuối chưa
  phục hồi. Capture thiếu chứng cứ trả `UNAVAILABLE`, không suy ra downtime 0.
- Cửa sổ các luồng ICMP được neo vào request/reply thành công trước các bước
  thay đổi migration, kết thúc bằng request/reply phục hồi mới sau validation.
  Mỗi stream có anchor riêng, không khẳng định timestamp bắt đầu/kết thúc các
  stream bằng nhau tuyệt đối. Capture nguồn đo liên tục gốc được khởi động trước
  các workload khác như bản nền. Cả bộ luồng cùng chạy xuyên MTU preparation,
  freeze, DB migration, cutover và validation.
- N-S lấy snapshot fresh khi hai stream đã ổn định, giữ cùng held session qua
  migration, rồi lấy snapshot fresh sau cutover và validation workload mới.
  Counter sau trừ counter baseline; các failure burst trước baseline bị loại.
  Thời gian gián đoạn ứng dụng tính trên clock monotonic của từng client, từ
  attempt lỗi đầu tiên tới echo phục hồi. Không so clock giữa guest/controller.
- TCP held chỉ thử kết nối lại trong quá trình tìm listener ban đầu. Khi socket
  đầu tiên đã kết nối, đóng socket là lỗi phiên cuối cùng, không mở socket thay
  thế. TCP new vẫn mở connection độc lập mỗi attempt.
- Held có thể mất qua migration NAT. Report khi đó ghi FAIL, giữ kết quả new,
  thời gian lỗi cuối chưa phục hồi và terminal error. Không gán PASS bằng reconnect.
- Bài này đo packet nhỏ/TCP echo. Không phải throughput, large-packet MTU hay
  truy cập Internet. Endpoint ngoài tenant là controller trên underlay của lab.

## Triển khai và chạy

Dùng source đầy đủ trong `/root/ovs-to-ovn-migration` theo [README](../README.md).
ZIP hiện hành có thư mục gốc `ovs-to-ovn-migration`. Kiểm tra cú pháp trước khi chạy:

```bash
cd /root/ovs-to-ovn-migration
source /root/venvs/kolla-2024.1/bin/activate
ansible-playbook -i /root/multinode reset-lab-to-ovs.yml --syntax-check
ansible-playbook -i /root/multinode migrate-to-ovn.yml --syntax-check
```

Khi lab đang là OVN và cần một chu kỳ mới, reset trước. Reset xóa workload inner hiện tại và
dựng lại nguồn OVS; các migration backup cũ được giữ mặc định. Evidence baseline
N-S đã được người dùng lưu ngoài lab.

```bash
ansible-playbook -i /root/multinode reset-lab-to-ovs.yml \
    -e reset_lab_confirm=true
```

Chỉ tiếp tục khi reset kết thúc `OVS_4NODE_BASELINE_READY`, không còn task lỗi:

```bash
source /etc/kolla/admin-openrc.sh
ansible-playbook -i /root/multinode migrate-to-ovn.yml
```

Một terminal controller là đủ; probe/capture chạy nền. Không cần copy script
qua SSH jump ns-in/ns-out hay mở listener thủ công.

Không dùng checkpoint của run A/B/C/D cũ để bổ sung stream mới giữa migration.
Các stream mới phải chạy từ đầu một run OVS mới. Retry preparation cũ giữ config
features đã lưu; retry không tự bật N-S/EW cho một run không có baseline.

## Evidence và cleanup

Evidence giữ trong migration run directory, gồm:

- `ew-matrix-results.json`: ba luồng ICMP bổ sung.
- `flows/<tên-luồng>/`: raw PCAP, log tcpdump, process identity, measurement anchors.
- `ns-baseline.json`, `ns-precutover.json`, `ns-results.json`: TCP/NAT/resource evidence.
- `extended-resources.json`: UUID/role/placement và local process identity.
- `ns/`: log listener SNAT và cumulative summaries client FIP trên controller.
- `ns-outer-policy.json`: thông tin gateway phục vụ kiểm tra policy outer.
- `workload-capacity.json`: quota hiện có và dự toán cấp phát. `quota-instances.json`
  ghi before/after khi bật tùy chọn tăng quota, hoặc có thể tồn tại trong run lịch sử.
- `migration-report.txt/.json`: kết quả chung với tên mô tả phép đo.

Collector dùng Nova console cho NS guest, không phụ thuộc SSH/ProxyJump đang
được đo. Guest chỉ được xóa sau khi đã lấy snapshot cumulative cuối. Cleanup
xóa VM/port matrix trước topology chia sẻ, xóa N-S theo UUID/run ownership và
dừng đúng local PID/start-time/boot được ghi. Cleanup của topology nền vẫn chỉ
chạy sau khi toàn bộ phép đo bật trong run đạt. Raw PCAP đã lấy về controller
được giữ; chỉ bản remote được xóa sau report thành công.

## Kiểm tra đã thực hiện và giới hạn

Unit/integration offline dùng OpenStackSDK 4.21.0 và Ansible-core 2.17.14.
Có TCP thật localhost cho normal, timeout-phục hồi cùng socket, socket đóng
không reconnect và sai nguồn; có kiểm tra bằng resource SDK cho provisioning
N-S, retry mất reply, mapping/MTU/ownership, ma trận compute/subnet và quota.
Kết quả kiểm tra cụ thể nằm trong [lịch sử kiểm tra](history/VALIDATION.txt).

**Chưa chạy reset/deploy/migration trên lab thật.** Bản code này cung cấp workflow
và kiểm tra offline; không phải bằng chứng N-S đã giữ phiên qua migration.

Nguồn API/cấu hình đã đối chiếu:

- https://docs.openstack.org/kolla-ansible/2024.1/reference/networking/neutron.html
- https://docs.openstack.org/openstacksdk/latest/user/proxies/compute.html
- https://docs.openstack.org/openstacksdk/latest/user/resources/compute/v2/limits.html
- https://docs.openstack.org/openstacksdk/latest/user/resources/network/v2/floating_ip.html
