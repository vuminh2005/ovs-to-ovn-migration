# B. Demo — File 2: có các bước Optional đã được reset thực hiện

Các bước tạo tài nguyên trước migration được đánh dấu **Optional** vì reset đã thực hiện.

Tài liệu dùng bộ tên trung tính dưới đây. Khi reset tạo bộ demo với tên mới,
các biến cấu hình cần khớp bảng này:

| File trong `/root/ovs-to-ovn-migration/` | Biến | Giá trị dùng trong hướng dẫn |
|---|---|---|
| `group_vars/reset.yml` | `reset_demo_prefix` | `demo` |
| `group_vars/all.yml` | `validation_name_prefix` | `validation` |
| `group_vars/ns-lab.yml` | `ns_shared_external_name` | `shared-external` |
| `group_vars/ns-lab.yml` | `ns_shared_external_subnet_name` | `shared-external-subnet` |

Nếu lab đã được reset với tên cũ, đặt `PREFIX=ovn-demo`,
`EXT_NET=ovn-shared-external`, `EXT_SUBNET=ovn-shared-external-subnet`
ở mục 1 để dùng tài nguyên đang có. Đổi biến trong terminal không đổi tên
tài nguyên. Image/flavor dùng đúng tên đã được reset chuẩn bị.

Chạy từng lệnh theo thứ tự và xem output trước khi sang lệnh tiếp theo.
Dùng cùng terminal controller A, trừ các lệnh ghi rõ console VM hoặc
terminal controller B. IP dưới đây là cấu hình lab hiện tại; thay tương ứng
nếu bạn đã đổi IP khi reset.

| Thành phần | Giá trị |
|---|---|
| External dùng chung | `shared-external`, flat `physnet1`, MTU `1450` |
| External subnet | `shared-external-subnet`, `10.1.0.0/24`, gateway `10.1.0.1` |
| Pool external | `10.1.0.4`–`10.1.0.10` |
| Network/subnet demo 1 | `demo-net1` / `demo-subnet1`, `10.234.0.0/24` |
| Network/subnet demo 2 | `demo-net2` / `demo-subnet2`, `10.234.1.0/24` |
| Router demo | `demo-router`, external IP `10.1.0.6` |
| VM1 / VM2 | `demo-vm1` / `demo-vm2`, IP `10.234.0.11` / `10.234.1.11` |
| Compute | VM1 trên compute1, VM2 trên compute2 |
| FIP → VM1 | `10.1.0.7` |
| Controller eth1 | `10.1.0.187` |
| Image / flavor | `ovn-validation-ubuntu-24.04` / `ovn-validation.small` |

Workload tự động của playbook vẫn chạy đầy đủ, dùng chung external nhưng có
router, private network, VM và FIP riêng. Các lệnh dưới đây dùng bộ demo riêng.

## 1. Controller A — nạp môi trường và tên tài nguyên

```bash
source /root/venvs/kolla-2024.1/bin/activate
source /etc/kolla/admin-openrc.sh
cd /root/ovs-to-ovn-migration

EXT_NET=shared-external
EXT_SUBNET=shared-external-subnet
IMAGE=ovn-validation-ubuntu-24.04
FLAVOR=ovn-validation.small
PREFIX=demo
NET_A=$PREFIX-net1
NET_B=$PREFIX-net2
SUBNET_A=$PREFIX-subnet1
SUBNET_B=$PREFIX-subnet2
ROUTER=$PREFIX-router
VM1=$PREFIX-vm1
VM2=$PREFIX-vm2
```

## 2. Optional — các phần reset đã chuẩn bị

**Bỏ qua toàn bộ mục 2 nếu reset đã kết thúc với `OVS_4NODE_BASELINE_READY`
và đã tạo bộ demo.** Các lệnh này chỉ dành cho trường hợp tài nguyên tương ứng
chưa tồn tại. Image/flavor, hai compute và kết nối external của lab phải đã sẵn sàng.

### 2.1. Optional — tạo external network/subnet dùng chung

Dùng tên và pool đúng với cấu hình migration. Giữ `.4/.5` cho N-S tự động;
bộ demo dùng router `.6` và FIP `.7`. Chỉ tạo nếu external này chưa có.

```bash
openstack network create --external --provider-network-type flat \
  --provider-physical-network physnet1 --mtu 1450 "$EXT_NET"
openstack subnet create --network "$EXT_NET" \
  --subnet-range 10.1.0.0/24 --gateway 10.1.0.1 --no-dhcp \
  --allocation-pool start=10.1.0.4,end=10.1.0.10 "$EXT_SUBNET"
```

### 2.2. Optional — tạo hai private network/subnet, router, hai VM và FIP

Chỉ chạy nếu bộ `demo-*` chưa được tạo. Dùng hai tên host Nova khác nhau
từ danh sách compute; thay `compute1/compute2` nếu lab đăng ký tên khác.

```bash
openstack compute service list --service nova-compute
COMPUTE1=compute1
COMPUTE2=compute2
NET_TYPE=vxlan
NET_MTU=1400
CIDR_A=10.234.0.0/24
CIDR_B=10.234.1.0/24
VM1_IP=10.234.0.11
VM2_IP=10.234.1.11
```

Đặt mật khẩu console cho hai VM sẽ tạo ở bước này. Nhớ mật khẩu để đăng
nhập user `demo`; user-data chỉ chứa hash mật khẩu.

```bash
umask 077
read -rsp 'Mật khẩu console cho hai VM: ' DEMO_PASSWORD
printf '\n'
PASSWORD_HASH=$(printf '%s\n' "$DEMO_PASSWORD" | openssl passwd -6 -stdin)
unset DEMO_PASSWORD
CLOUD_INIT=/root/demo-before-cloud-init.yaml
cat > "$CLOUD_INIT" <<EOF
#cloud-config
users:
  - default
  - name: demo
    groups: [sudo]
    shell: /bin/bash
    lock_passwd: false
    hashed_passwd: '$PASSWORD_HASH'
    sudo: 'ALL=(ALL) NOPASSWD:ALL'
ssh_pwauth: false
chpasswd:
  expire: false
EOF
unset PASSWORD_HASH
```

```bash
openstack network create --provider-network-type "$NET_TYPE" --mtu "$NET_MTU" "$NET_A"
openstack network create --provider-network-type "$NET_TYPE" --mtu "$NET_MTU" "$NET_B"
openstack subnet create --network "$NET_A" --subnet-range "$CIDR_A" --dhcp "$SUBNET_A"
openstack subnet create --network "$NET_B" --subnet-range "$CIDR_B" --dhcp "$SUBNET_B"

openstack security group create "$PREFIX-security"
openstack security group rule create --ingress --ethertype IPv4 \
  --protocol icmp --remote-ip 0.0.0.0/0 "$PREFIX-security"

openstack router create "$ROUTER"
openstack router add subnet "$ROUTER" "$SUBNET_A"
openstack router add subnet "$ROUTER" "$SUBNET_B"
openstack router set --external-gateway "$EXT_NET" --enable-snat \
  --fixed-ip subnet="$EXT_SUBNET",ip-address=10.1.0.6 \
  "$ROUTER"

openstack port create --network "$NET_A" \
  --fixed-ip subnet="$SUBNET_A",ip-address="$VM1_IP" \
  --security-group "$PREFIX-security" "$PREFIX-port1"
openstack port create --network "$NET_B" \
  --fixed-ip subnet="$SUBNET_B",ip-address="$VM2_IP" \
  --security-group "$PREFIX-security" "$PREFIX-port2"

openstack --os-compute-api-version 2.74 server create \
  --image "$IMAGE" --flavor "$FLAVOR" --host "$COMPUTE1" \
  --port "$PREFIX-port1" --user-data "$CLOUD_INIT" --wait "$VM1"
openstack --os-compute-api-version 2.74 server create \
  --image "$IMAGE" --flavor "$FLAVOR" --host "$COMPUTE2" \
  --port "$PREFIX-port2" --user-data "$CLOUD_INIT" --wait "$VM2"

PORT_VM1=$(openstack port show "$PREFIX-port1" -f value -c id)
openstack floating ip create --floating-ip-address 10.1.0.7 --port "$PORT_VM1" "$EXT_NET"
```

Tiếp tục khi cả hai VM `ACTIVE`; chờ cloud-init hoàn tất để đăng nhập
console. Nếu một lệnh tạo báo lỗi, xử lý lỗi đó trước khi chạy lệnh tiếp theo.

## 3. Trước migration — show ML2/OVS và tài nguyên demo

**Controller A:** show driver và các container trên controller/network/compute.

```bash
docker exec neutron_server sh -c \
  "grep -E '^[[:space:]]*(mechanism_drivers|tenant_network_types)[[:space:]]*=' /etc/neutron/plugins/ml2/ml2_conf.ini"
ansible -i /root/multinode control:network:compute -b -m shell \
  -a "docker ps -a | grep -E 'NAMES|neutron_|ovn_|openvswitch'"
```

Kỳ vọng `mechanism_drivers` có `openvswitch`, tenant network type có `vxlan`;
các agent `neutron_openvswitch_agent`, `neutron_l3_agent`, `neutron_dhcp_agent`
chạy trên các node tương ứng.

**Network — liệt kê tên/UUID và loại network:**

```bash
openstack network list --long
```

**External network dùng chung — xem tên, UUID, loại và MTU:**

```bash
openstack network show "$EXT_NET" -c id -c name -c provider:network_type -c mtu
```

**Private network của VM1:**

```bash
openstack network show "$NET_A" -c id -c name -c subnets -c provider:network_type -c mtu
```

**Private network của VM2:**

```bash
openstack network show "$NET_B" -c id -c name -c subnets -c provider:network_type -c mtu
```

**Subnet — liệt kê tên/UUID, network và CIDR:**

```bash
openstack subnet list --long
```

**Router — liệt kê tên/UUID và trạng thái:**

```bash
openstack router list --long
```

**Router demo — xem hai subnet kết nối và external gateway:**

```bash
openstack router show "$ROUTER" -c id -c name -c interfaces_info -c external_gateway_info
```

**Instance — liệt kê tên/UUID, trạng thái và IP:**

```bash
openstack server list --all-projects --long
```

**VM1 — xem UUID, trạng thái, IP và compute đang chạy:**

```bash
openstack server show "$VM1" -c id -c name -c status -c addresses -c OS-EXT-SRV-ATTR:host
```

**VM2 — xem UUID, trạng thái, IP và compute đang chạy:**

```bash
openstack server show "$VM2" -c id -c name -c status -c addresses -c OS-EXT-SRV-ATTR:host
```

**FIP — liệt kê UUID, floating IP, fixed IP và port được gán:**

```bash
openstack floating ip list --long
```

**FIP của VM1 — lấy ID từ port VM1 rồi xem đúng FIP đó:**

```bash
PORT_VM1=$(openstack port show "$PREFIX-port1" -f value -c id)
FIP_ID=$(openstack floating ip list --port "$PORT_VM1" -f value -c ID)
openstack floating ip show "$FIP_ID" -c id -c status -c floating_ip_address \
  -c floating_network_id -c fixed_ip_address -c port_id
```

**Lấy địa chỉ FIP VM1 để dùng khi ping từ controller:**

```bash
FIP_VM1=$(openstack floating ip show "$FIP_ID" -f value -c floating_ip_address)
printf 'FIP VM1: %s\n' "$FIP_VM1"
```

Ghi nhận trên màn hình UUID của network/subnet/router/VM/FIP, private IP,
FIP và compute của hai VM. Hai VM phải ở hai compute và hai subnet khác nhau.

## 4. Trước migration — mở console và chạy ba luồng ping

**Controller A:** lấy hai URL noVNC.

```bash
openstack console url show --novnc "$VM1"
openstack console url show --novnc "$VM2"
printf 'Controller → FIP VM1: ping -D -O -n -i 0.2 -W 1 %s\n' "$FIP_VM1"
```

Mở hai URL trong trình duyệt, đăng nhập user `demo`. Dùng mật khẩu đã đặt
khi tạo VM; nếu VM do reset tạo, đọc mật khẩu từ `credentials_file` mà reset
đã in ra (`demo-console-credentials.json`) trước khi quay demo.

**Console VM1 — EW, khác compute/subnet:**

```bash
ping -D -O -n -i 0.2 -W 1 10.234.1.11
```

**Console VM2 — NS outbound tới eth1 controller:**

```bash
ping -D -O -n -i 0.2 -W 1 10.1.0.187
```

**Controller B — NS inbound tới FIP của VM1:**

```bash
ping -D -O -n -i 0.2 -W 1 10.1.0.7
```

Cả ba luồng phải có phản hồi trước khi bắt đầu. Giữ chúng chạy suốt migration.
`-D` in timestamp Unix; `-O` báo request trước chưa nhận được phản hồi,
chưa phải kết luận packet loss cuối cùng. Ctrl+C mới hiện thống kê tổng kết.

## 5. Bật timer ngoài và chạy migration

1. Mở stopwatch trên điện thoại hoặc trình duyệt, đặt về `00:00`.
2. Bấm Start ngay trước khi chạy lệnh migration ở controller A.
3. Quan sát đồng thời ba cửa sổ ping và task Ansible đang chạy.

**Controller A:**

```bash
date -Ins
ansible-playbook -i /root/multinode migrate-to-ovn.yml
migration_rc=$?
printf 'Migration exit code: %s\n' "$migration_rc"
date -Ins
```

Trong migration, ghi thời điểm bắt đầu thiếu phản hồi và thời điểm phản hồi
trở lại ổn định cho từng luồng. Ghi cả timestamp ping, số giây trên stopwatch
và tên `TASK [...]` đang hiển thị. Nếu mất nhiều đợt, ghi thêm từng đợt.

| Luồng | Gián đoạn: timestamp / timer ngoài / task | Phục hồi: timestamp / timer ngoài / task | Gián đoạn quan sát được (s) |
|---|---|---|---|
| VM1 → VM2 (EW) | … | … | … |
| VM2 → controller eth1 (NS outbound) | … | … | … |
| Controller eth1 → FIP VM1 (NS inbound) | … | … | … |

Để ghi downtime thực tế, đánh dấu reply cuối trước đợt mất và reply đầu sau
đợt mất; lấy chênh lệch timestamp hai reply này làm khoảng gián đoạn quan sát.
Stopwatch đối chiếu hai mốc đó; độ phân giải ping là `0.2 s` và thao tác ghi
tay có sai số. Tên task là task quan sát cùng thời điểm, chưa kết luận nguyên nhân.

Các mốc để nhận biết trên terminal migration:

- `Mark phase 07 and control-plane downtime start`: bắt đầu timer control-plane.
- Phase 08: dừng legacy agents, `Deploy OVN controller`, chờ OVN takeover.
- Phase 09: dọn dataplane legacy.
- `Mark control-plane downtime end` ở phase 10: API Neutron dùng được trở lại.

Control-plane downtime và khoảng mất ping là hai phép đo riêng. Giữ ping
chạy cho tới khi cả ba luồng phục hồi ổn định; dừng stopwatch khi playbook kết thúc.

## 6. Sau migration — show ML2/OVN và đối chiếu bộ cũ

Chỉ làm phần này khi migration hoàn tất thành công. **Controller A:**

```bash
docker exec neutron_server sh -c \
  "grep -E '^[[:space:]]*(mechanism_drivers|tenant_network_types)[[:space:]]*=' /etc/neutron/plugins/ml2/ml2_conf.ini"
ansible -i /root/multinode control:network:compute -b -m shell \
  -a "docker ps -a | grep -E 'NAMES|neutron_|ovn_|openvswitch'"
```

**Network — liệt kê tên/UUID và loại network:**

```bash
openstack network list --long
```

**External network dùng chung — xem tên, UUID, loại và MTU:**

```bash
openstack network show "$EXT_NET" -c id -c name -c provider:network_type -c mtu
```

**Private network của VM1:**

```bash
openstack network show "$NET_A" -c id -c name -c subnets -c provider:network_type -c mtu
```

**Private network của VM2:**

```bash
openstack network show "$NET_B" -c id -c name -c subnets -c provider:network_type -c mtu
```

**Subnet — liệt kê tên/UUID, network và CIDR:**

```bash
openstack subnet list --long
```

**Router — liệt kê tên/UUID và trạng thái:**

```bash
openstack router list --long
```

**Router demo — xem hai subnet kết nối và external gateway:**

```bash
openstack router show "$ROUTER" -c id -c name -c interfaces_info -c external_gateway_info
```

**Instance — liệt kê tên/UUID, trạng thái và IP:**

```bash
openstack server list --all-projects --long
```

**VM1 — xem UUID, trạng thái, IP và compute đang chạy:**

```bash
openstack server show "$VM1" -c id -c name -c status -c addresses -c OS-EXT-SRV-ATTR:host
```

**VM2 — xem UUID, trạng thái, IP và compute đang chạy:**

```bash
openstack server show "$VM2" -c id -c name -c status -c addresses -c OS-EXT-SRV-ATTR:host
```

**FIP — liệt kê UUID, floating IP, fixed IP và port được gán:**

```bash
openstack floating ip list --long
```

**FIP của VM1 — lấy ID từ port VM1 rồi xem đúng FIP đó:**

```bash
PORT_VM1=$(openstack port show "$PREFIX-port1" -f value -c id)
FIP_ID=$(openstack floating ip list --port "$PORT_VM1" -f value -c ID)
openstack floating ip show "$FIP_ID" -c id -c status -c floating_ip_address \
  -c floating_network_id -c fixed_ip_address -c port_id
```

**Lấy địa chỉ FIP VM1 để dùng khi ping từ controller:**

```bash
FIP_VM1=$(openstack floating ip show "$FIP_ID" -f value -c floating_ip_address)
printf 'FIP VM1: %s\n' "$FIP_VM1"
```

Đối chiếu với phần trước migration ngay trên màn hình:

- Driver có `ovn`; tenant network type có `geneve`; OVN controller/metadata
  và OVN DB/northd chạy trên các node tương ứng. Legacy agents có thể còn ở
  trạng thái `Exited`; `openvswitch_db`/`openvswitch_vswitchd` vẫn chạy.
- UUID network/subnet/router/VM/FIP giữ nguyên; private IP, FIP, compute,
  router interfaces và gán FIP tới VM1 giữ nguyên. Hai VM `ACTIVE`.
- Private network đổi VXLAN/MTU `1400` → Geneve/MTU `1392`.
  External giữ flat/MTU `1450`.

Khi ba luồng đã có phản hồi ổn định, Ctrl+C ở từng cửa sổ ping để show thống kê.

## 7. Show downtime và đối chiếu timer

**Controller A:** dán đường dẫn `Text report` mà playbook vừa in cuối run,
rồi đọc report đó:

```bash
read -rp 'Đường dẫn Text report: ' REPORT_TXT
cat "$REPORT_TXT"
```

- `Total run duration`: đối chiếu thời gian chạy trên stopwatch, có sai lệch
  do thời điểm bấm Start/Stop thủ công.
- `Control-plane downtime`: đối chiếu hai task bắt đầu/kết thúc ở phase 07/10.
- `Dataplane downtime`: xem ICMP EW khác compute/subnet, ICMP NS outbound
  và ICMP NS inbound; đối chiếu khoảng mất/phục hồi đã ghi trên ba console.

Report đo các VM/probe tự động; ba ping demo là luồng riêng, nên số giây
có thể khác. Dùng số đo ICMP để đối chiếu ICMP; các mục TCP là phép đo khác.

## 8. Sau migration — tạo bộ mới trên OVN và kiểm tra EW/NS

Giữ bộ cũ để trình diễn tài nguyên trước migration còn hoạt động. Tạo bộ
`demo-new-*` với hai subnet mới và dùng lại external đã có.
**Controller A:** lấy đúng tên host Nova của hai compute từ hai VM cũ,
rồi đổi biến sang bộ mới.

```bash
COMPUTE1=$(openstack server show "$VM1" -f value -c OS-EXT-SRV-ATTR:host)
COMPUTE2=$(openstack server show "$VM2" -f value -c OS-EXT-SRV-ATTR:host)
PREFIX=demo-new
NET_A=$PREFIX-net1
NET_B=$PREFIX-net2
SUBNET_A=$PREFIX-subnet1
SUBNET_B=$PREFIX-subnet2
ROUTER=$PREFIX-router
VM1=$PREFIX-vm1
VM2=$PREFIX-vm2
NET_TYPE=geneve
NET_MTU=1392
CIDR_A=10.235.0.0/24
CIDR_B=10.235.1.0/24
VM1_IP=10.235.0.11
VM2_IP=10.235.1.11
```

Đặt mật khẩu console cho hai VM sẽ tạo ở bước này. Nhớ mật khẩu để đăng
nhập user `demo`; user-data chỉ chứa hash mật khẩu.

```bash
umask 077
read -rsp 'Mật khẩu console cho hai VM: ' DEMO_PASSWORD
printf '\n'
PASSWORD_HASH=$(printf '%s\n' "$DEMO_PASSWORD" | openssl passwd -6 -stdin)
unset DEMO_PASSWORD
CLOUD_INIT=/root/demo-after-cloud-init.yaml
cat > "$CLOUD_INIT" <<EOF
#cloud-config
users:
  - default
  - name: demo
    groups: [sudo]
    shell: /bin/bash
    lock_passwd: false
    hashed_passwd: '$PASSWORD_HASH'
    sudo: 'ALL=(ALL) NOPASSWD:ALL'
ssh_pwauth: false
chpasswd:
  expire: false
EOF
unset PASSWORD_HASH
```

```bash
openstack network create --provider-network-type "$NET_TYPE" --mtu "$NET_MTU" "$NET_A"
openstack network create --provider-network-type "$NET_TYPE" --mtu "$NET_MTU" "$NET_B"
openstack subnet create --network "$NET_A" --subnet-range "$CIDR_A" --dhcp "$SUBNET_A"
openstack subnet create --network "$NET_B" --subnet-range "$CIDR_B" --dhcp "$SUBNET_B"

openstack security group create "$PREFIX-security"
openstack security group rule create --ingress --ethertype IPv4 \
  --protocol icmp --remote-ip 0.0.0.0/0 "$PREFIX-security"

openstack router create "$ROUTER"
openstack router add subnet "$ROUTER" "$SUBNET_A"
openstack router add subnet "$ROUTER" "$SUBNET_B"
openstack router set --external-gateway "$EXT_NET" --enable-snat \
  "$ROUTER"

openstack port create --network "$NET_A" \
  --fixed-ip subnet="$SUBNET_A",ip-address="$VM1_IP" \
  --security-group "$PREFIX-security" "$PREFIX-port1"
openstack port create --network "$NET_B" \
  --fixed-ip subnet="$SUBNET_B",ip-address="$VM2_IP" \
  --security-group "$PREFIX-security" "$PREFIX-port2"

openstack --os-compute-api-version 2.74 server create \
  --image "$IMAGE" --flavor "$FLAVOR" --host "$COMPUTE1" \
  --port "$PREFIX-port1" --user-data "$CLOUD_INIT" --wait "$VM1"
openstack --os-compute-api-version 2.74 server create \
  --image "$IMAGE" --flavor "$FLAVOR" --host "$COMPUTE2" \
  --port "$PREFIX-port2" --user-data "$CLOUD_INIT" --wait "$VM2"

PORT_VM1=$(openstack port show "$PREFIX-port1" -f value -c id)
openstack floating ip create --port "$PORT_VM1" "$EXT_NET"
```

Tiếp tục khi cả hai VM `ACTIVE`; chờ cloud-init hoàn tất để đăng nhập
console. Nếu một lệnh tạo báo lỗi, xử lý lỗi đó trước khi chạy lệnh tiếp theo.

External IP của router mới và FIP mới được Neutron cấp từ pool dùng chung.
Các biến hiện đang trỏ tới bộ `demo-new-*`. **Controller A:** chạy các lệnh
sau để show bộ mới:

**Network — liệt kê tên/UUID và loại network:**

```bash
openstack network list --long
```

**External network dùng chung — xem tên, UUID, loại và MTU:**

```bash
openstack network show "$EXT_NET" -c id -c name -c provider:network_type -c mtu
```

**Private network của VM1:**

```bash
openstack network show "$NET_A" -c id -c name -c subnets -c provider:network_type -c mtu
```

**Private network của VM2:**

```bash
openstack network show "$NET_B" -c id -c name -c subnets -c provider:network_type -c mtu
```

**Subnet — liệt kê tên/UUID, network và CIDR:**

```bash
openstack subnet list --long
```

**Router — liệt kê tên/UUID và trạng thái:**

```bash
openstack router list --long
```

**Router demo — xem hai subnet kết nối và external gateway:**

```bash
openstack router show "$ROUTER" -c id -c name -c interfaces_info -c external_gateway_info
```

**Instance — liệt kê tên/UUID, trạng thái và IP:**

```bash
openstack server list --all-projects --long
```

**VM1 — xem UUID, trạng thái, IP và compute đang chạy:**

```bash
openstack server show "$VM1" -c id -c name -c status -c addresses -c OS-EXT-SRV-ATTR:host
```

**VM2 — xem UUID, trạng thái, IP và compute đang chạy:**

```bash
openstack server show "$VM2" -c id -c name -c status -c addresses -c OS-EXT-SRV-ATTR:host
```

**FIP — liệt kê UUID, floating IP, fixed IP và port được gán:**

```bash
openstack floating ip list --long
```

**FIP của VM1 — lấy ID từ port VM1 rồi xem đúng FIP đó:**

```bash
PORT_VM1=$(openstack port show "$PREFIX-port1" -f value -c id)
FIP_ID=$(openstack floating ip list --port "$PORT_VM1" -f value -c ID)
openstack floating ip show "$FIP_ID" -c id -c status -c floating_ip_address \
  -c floating_network_id -c fixed_ip_address -c port_id
```

**Lấy địa chỉ FIP VM1 để dùng khi ping từ controller:**

```bash
FIP_VM1=$(openstack floating ip show "$FIP_ID" -f value -c floating_ip_address)
printf 'FIP VM1: %s\n' "$FIP_VM1"
```

**Mở console VM1 mới:**

```bash
openstack console url show --novnc "$VM1"
```

**Mở console VM2 mới:**

```bash
openstack console url show --novnc "$VM2"
```

**In lệnh ping FIP mới để nhập ở controller B:**

```bash
printf 'ping -D -O -n -i 0.2 -W 1 %s\n' "$FIP_VM1"
```

Đăng nhập hai console mới bằng user `demo` và mật khẩu vừa đặt.

**Console VM1 mới — EW:**

```bash
ping -D -O -n -i 0.2 -W 1 10.235.1.11
```

**Console VM2 mới — NS outbound:**

```bash
ping -D -O -n -i 0.2 -W 1 10.1.0.187
```

**Controller B — NS inbound:** chạy đúng lệnh chứa FIP mà controller A vừa in.
Cả ba luồng phải có phản hồi. Ctrl+C để show thống kê và kết thúc demo.

