# Demo ML2/OVS → ML2/OVN với tài nguyên do reset chuẩn bị

Bản cập nhật 17/10/2026. Dùng toàn bộ `final.zip` mới trên controller. Các
lệnh dưới đây chưa được chạy trên lab khi soạn tài liệu.

Reset dựng sẵn external và bộ demo. Migration tiếp tục chạy đầy đủ A/B/C/D,
EW matrix và N-S tự động; chúng dùng chung external nhưng có private topology,
router và FIP riêng. Migration không tạo probe, reboot hay cleanup VM demo.
Network demo vẫn được migrate VXLAN → Geneve và giảm MTU 1400 → 1392 theo
quy trình chung. Snapshot và kiểm tra bảo toàn bao gồm cả bộ demo.

## 1. Cấu hình và reset

Mặc định trong `group_vars/ns-lab.yml` và `group_vars/reset.yml`:

| Thành phần | Mặc định |
|---|---|
| External dùng chung | `ovn-shared-external`, flat `physnet1`, MTU 1450 |
| Subnet external | `10.1.0.0/24`, gateway `10.1.0.1`, DHCP tắt |
| Pool external | `10.1.0.4`–`10.1.0.10` trong dải lab được phép sử dụng |
| N-S tự động | Router `.4`, FIP `.5` |
| Bộ demo | Router `.6`, FIP `.7` → VM1 |
| Demo private | `10.234.0.0/24`, `10.234.1.0/24` |
| VM1 / VM2 | `ovn-demo-vm1` / `ovn-demo-vm2`, mỗi VM ở một compute/subnet |
| Console | User `demo`; mật khẩu sinh trong snapshot reset, mode 0600 |

Các IP/pool này thuộc lab on-link hiện có; không dùng cho một subnet khác
chưa được cấp. Nếu cấu hình lab thay đổi, sửa biến trước reset. Helper kiểm
tra pool, bốn IP riêng biệt và CIDR không trùng workload trước khi dừng guest.

**Reset xóa deployment/workload hiện tại và dựng lại bộ demo.** Chạy từ đầu
trước mỗi lần muốn quay một chu kỳ OVS → OVN mới:

```bash
source /root/venvs/kolla-2024.1/bin/activate
cd /root/ovs-to-ovn-migration
ansible-playbook -i /root/multinode reset-lab-to-ovs.yml \
  -e reset_lab_confirm=true
```

Chỉ tiếp tục khi có `OVS_4NODE_BASELINE_READY`. Marker này chỉ được ghi sau
khi image/flavor được kiểm tra, hai VM ACTIVE đúng compute, cloud-init đã
chuẩn bị tài khoản console, gateway port và FIP ACTIVE, và tập tài nguyên
khớp bộ demo. Nó chưa chứng nhận ba luồng traffic bạn sẽ ping thủ công.

## 2. Nạp đúng tài nguyên của lần reset vừa hoàn tất

Không lấy UUID từ lần reset cũ. Đoạn sau kiểm tra snapshot mới nhất; nếu
snapshot đó chưa hoàn tất, nó dừng thay vì chọn lùi về một bộ cũ.

```bash
source /root/venvs/kolla-2024.1/bin/activate
source /etc/kolla/admin-openrc.sh
umask 077
init_demo_session() {
RESET_DIR=$(python3 - <<'PY'
import json
from pathlib import Path
root = max(p for p in Path('/root/kolla-reset-snapshots').iterdir() if p.is_dir())
data = json.loads((root/'reset-complete.json').read_text())
assert data['status'] == 'OVS_4NODE_BASELINE_READY'
assert data['demo']['status'] == 'READY'
print(root)
PY
) || return
DEMO_ROOT="/root/ovn-demo-session-$(basename "$RESET_DIR")"
mkdir -p "$DEMO_ROOT"
printf 'RESET_DIR=%q\nDEMO_ROOT=%q\n' "$RESET_DIR" "$DEMO_ROOT" \
  > /root/ovn-demo-current.env

python3 - "$RESET_DIR" "$DEMO_ROOT" <<'PY' || return
import json, shlex, sys
from pathlib import Path
reset, output = map(Path, sys.argv[1:])
demo = json.loads((reset/'demo-resources.json').read_text())
done = json.loads((reset/'reset-complete.json').read_text())
config = json.loads((reset/'reset-demo-config.json').read_text())
a, b = demo['vms']
settings = dict(EXT_NET=demo['external']['network']['id'], ROUTER=demo['router_id'],
    NET_A=a['network_id'], NET_B=b['network_id'], SUBNET_A=a['subnet_id'], SUBNET_B=b['subnet_id'],
    VM1=a['id'], VM2=b['id'], VM1_IP=a['ip'], VM2_IP=b['ip'],
    FIP_ID=demo['floating_ip_id'], FIP_VM1=demo['floating_ip'],
    COMPUTE1=a['compute'], COMPUTE2=b['compute'], CONTROLLER_ETH1_IP=config['endpoint_ip'],
    IMAGE=done['image']['name'], FLAVOR=done['flavor']['name'])
(output/'settings.env').write_text(''.join(f'{k}={shlex.quote(str(v))}\n' for k,v in settings.items()))
print(json.dumps(demo, indent=2))
PY
source "$DEMO_ROOT/settings.env"
}
init_demo_session
```

Nếu dùng `reset_snapshot_root` khác mặc định, thay thư mục trong đoạn chọn
snapshot. Mọi terminal controller mới đều nạp lại:

```bash
source /root/venvs/kolla-2024.1/bin/activate
source /etc/kolla/admin-openrc.sh
source /root/ovn-demo-current.env
source "$DEMO_ROOT/settings.env"
```

## 3. Mở console và kiểm tra baseline trước khi quay

Đọc mật khẩu trên terminal riêng trước khi quay; không đưa file này vào video
hoặc log migration. Có thể đặt mật khẩu riêng qua extra-vars file của reset
với biến `reset_demo_console_password`; không truyền mật khẩu trên command line.

```bash
python3 - "$RESET_DIR/demo-console-credentials.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
print('Username:', data['username'])
print('Password:', data['password'])
PY
openstack console url show --novnc "$VM1"
openstack console url show --novnc "$VM2"
printf 'VM1: ping -D -O -n -i 0.2 -W 1 %s\n' "$VM2_IP"
printf 'VM2: ping -D -O -n -i 0.2 -W 1 %s\n' "$CONTROLLER_ETH1_IP"
printf 'Controller: ping -D -O -n -i 0.2 -W 1 %s\n' "$FIP_VM1"
```

Đăng nhập hai console bằng user/mật khẩu vừa đọc. Thử cả ba hướng trước khi
quay: VM1 → VM2 (EW), VM2 → controller eth1 (SNAT), controller → FIP VM1.
MAC/IP gateway demo nằm trong `demo-resources.json` nếu cần đối chiếu uplink.
Chỉ bắt đầu migration sau khi baseline của cả ba luồng đã thông.

## 4. Show ML2/OVS và lưu UUID/IP

```bash
cd /root/ovs-to-ovn-migration
docker exec neutron_server sh -c \
  "grep -E '^[[:space:]]*(mechanism_drivers|tenant_network_types)[[:space:]]*=' /etc/neutron/plugins/ml2/ml2_conf.ini"
ansible -i /root/multinode control:network:compute -b -m shell \
  -a "docker ps -a | grep -E 'NAMES|neutron_|ovn_|openvswitch'"
openstack server list --all-projects --long
openstack network list --long
openstack subnet list
openstack router list
openstack floating ip list
openstack network show "$EXT_NET" -c id -c router:external \
  -c provider:network_type -c provider:physical_network -c mtu
```

Lưu hàm snapshot chọn trường nhận dạng; MTU/type tenant được xem riêng vì dự
kiến thay đổi. Không tạo bộ mới khi đang lấy snapshot before/after.

```bash
cat > "$DEMO_ROOT/snapshot-demo.sh" <<'BASH'
snapshot_demo() {
  local stage="$1" target="$DEMO_ROOT/$1"
  mkdir -p "$target"
  openstack server show "$VM1" -f json -c id -c status -c addresses \
    -c OS-EXT-SRV-ATTR:host > "$target/vm1.json"
  openstack server show "$VM2" -f json -c id -c status -c addresses \
    -c OS-EXT-SRV-ATTR:host > "$target/vm2.json"
  openstack router show "$ROUTER" -f json -c id -c interfaces_info \
    -c external_gateway_info > "$target/router.json"
  openstack floating ip show "$FIP_ID" -f json -c id -c floating_network_id \
    -c floating_ip_address -c port_id -c fixed_ip_address > "$target/fip.json"
  openstack network show "$NET_A" -f json -c id -c subnets > "$target/net-a.json"
  openstack network show "$NET_B" -f json -c id -c subnets > "$target/net-b.json"
  python3 - "$target" <<'PY'
import json, sys
from pathlib import Path
def canonical(value):
    if isinstance(value, dict): return {k: canonical(v) for k,v in value.items()}
    if isinstance(value, list): return sorted((canonical(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
    return value
for path in Path(sys.argv[1]).glob('*.json'):
    path.write_text(json.dumps(canonical(json.loads(path.read_text())), sort_keys=True, indent=2)+'\n')
PY
}
BASH
source "$DEMO_ROOT/snapshot-demo.sh"
snapshot_demo before
```

## 5. Ba luồng ping và terminal migration

Mở bốn cửa sổ: console VM1, console VM2, terminal controller cho FIP và
terminal controller để chạy migration. Lấy lệnh/IP thực tế từ bước 3.

Trong console VM1, với cấu hình mặc định:

```bash
ping -D -O -n -i 0.2 -W 1 10.234.1.11
```

Trong console VM2:

```bash
ping -D -O -n -i 0.2 -W 1 10.1.0.187
```

Terminal controller dành cho FIP:

```bash
ping -D -O -n -i 0.2 -W 1 "$FIP_VM1" 2>&1 | tee "$DEMO_ROOT/demo-fip-ping.log"
```

`-D` in timestamp; `-O` thông báo request trước chưa có phản hồi, không phải
kết luận cuối cùng rằng gói đã mất. Ctrl+C mới có thống kê tổng kết. Giữ ba
ping chạy suốt migration và dùng timer ngoài như bố cục demo đã chọn.

Ở terminal migration, nạp môi trường theo bước 2 rồi chạy lệnh bình thường;
không có biến tắt workload tự động:

```bash
cd /root/ovs-to-ovn-migration
set -o pipefail
ansible-playbook -i /root/multinode migrate-to-ovn.yml \
  2>&1 | tee "$DEMO_ROOT/migration.log"
migration_rc=${PIPESTATUS[0]}
printf 'Migration exit code: %s\n' "$migration_rc"
```

## 6. Sau migration: show OVN và đối chiếu

Chờ ba luồng demo thông ổn định rồi Ctrl+C để hiện thống kê. Tiếp tục dùng
đúng UUID/IP cũ trong `settings.env`:

```bash
docker exec neutron_server sh -c \
  "grep -E '^[[:space:]]*(mechanism_drivers|tenant_network_types)[[:space:]]*=' /etc/neutron/plugins/ml2/ml2_conf.ini"
ansible -i /root/multinode control:network:compute -b -m shell \
  -a "docker ps -a | grep -E 'NAMES|neutron_|ovn_|openvswitch'"
source "$DEMO_ROOT/snapshot-demo.sh"
snapshot_demo after
diff -ru "$DEMO_ROOT/before" "$DEMO_ROOT/after"
openstack network show "$NET_A" -c id -c provider:network_type -c mtu
openstack network show "$NET_B" -c id -c provider:network_type -c mtu
openstack network show "$EXT_NET" -c id -c provider:network_type -c mtu
```

Tenant phải là Geneve/1392, external vẫn flat/1450. Legacy agents có thể còn
container trạng thái Exited; OVS daemon vẫn chạy. Không cần trình bày timer
Port_Binding trong phần demo.

## 7. Report và bằng chứng giữ tài nguyên

Lấy run mới nhất, không lấy lùi về report của run thành công cũ:

```bash
RUN_DIR=$(python3 - <<'PY'
from pathlib import Path
import sys
sys.path.insert(0, 'scripts')
from run_layout import run_path
print(max(p for p in Path('/root/ovs-to-ovn-backup').iterdir()
          if p.is_dir() and run_path(p, 'runtime.json').is_file()))
PY
)
cat "$RUN_DIR/migration-report.txt"
python3 - "$RUN_DIR/migration-report.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
print(json.dumps(data['preexisting_resources_preservation'], indent=2))
PY
```

Kỳ vọng `preexisting_resources_preservation.status=PASS`, scope
`after-workload-cleanup`, với UUID demo còn nguyên. TXT vẫn là các phép đo
của workload tự động; các ping bạn mở thủ công không được gán thành metric
trong report. Gián đoạn demo được xem trực tiếp qua ba cửa sổ/timer và log.

Nếu migration lỗi, giữ run và log để điều tra; không chạy lại từ phase 00
sau cutover. Các entrypoint resume tiếp tục dùng shared external/checkpoint
của run gốc. Reset tiếp theo sẽ dựng lại cả demo và đổi UUID.

## 8. Tạo bộ mới trên OVN

Giữ bộ demo cũ để đối chiếu. Dùng prefix/subnet khác và external có sẵn;
chỉ tạo bộ mới sau khi migration hoàn tất. Phần dưới là thao tác demo thủ
công, không được migration nhận ownership.

Tạo user-data cho VM mới bằng cùng tài khoản console, chỉ lưu hash:

```bash
python3 - "$RESET_DIR/demo-console-credentials.json" "$DEMO_ROOT/cloud-init.yaml" <<'PY'
import json, os, subprocess, sys, yaml
data = json.load(open(sys.argv[1]))
digest = subprocess.run(['openssl','passwd','-6','-stdin'], input=data['password']+'\n',
    text=True, capture_output=True, check=True).stdout.strip()
config = dict(users=['default', dict(name=data['username'], groups=['sudo'], shell='/bin/bash',
    lock_passwd=False, hashed_passwd=digest, sudo='ALL=(ALL) NOPASSWD:ALL')],
    ssh_pwauth=False, chpasswd=dict(expire=False))
with open(sys.argv[2], 'w') as stream: stream.write('#cloud-config\n'+yaml.safe_dump(config))
os.chmod(sys.argv[2], 0o600)
PY
```

```bash
cat > "$DEMO_ROOT/create-demo-pair.sh" <<'BASH'
create_demo_pair() {
  local prefix="$1" cidr_a="$2" cidr_b="$3"
  local ip_a="$4" ip_b="$5" net_type="$6" net_mtu="$7"

  NET_A="$prefix-net-a"
  NET_B="$prefix-net-b"
  SUBNET_A="$prefix-subnet-a"
  SUBNET_B="$prefix-subnet-b"
  ROUTER="$prefix-router"
  SG="$prefix-sg"
  VM1="$prefix-vm1"
  VM2="$prefix-vm2"

  openstack network create --provider-network-type "$net_type" \
    --mtu "$net_mtu" "$NET_A" || return
  openstack network create --provider-network-type "$net_type" \
    --mtu "$net_mtu" "$NET_B" || return
  openstack subnet create --network "$NET_A" --subnet-range "$cidr_a" \
    --dhcp "$SUBNET_A" || return
  openstack subnet create --network "$NET_B" --subnet-range "$cidr_b" \
    --dhcp "$SUBNET_B" || return

  openstack security group create "$SG" || return
  openstack security group rule create --ingress --ethertype IPv4 \
    --protocol icmp --remote-ip 0.0.0.0/0 "$SG" || return
  openstack security group rule list "$SG" || return

  openstack router create "$ROUTER" || return
  openstack router add subnet "$ROUTER" "$SUBNET_A" || return
  openstack router add subnet "$ROUTER" "$SUBNET_B" || return
  openstack router set --external-gateway "$EXT_NET" --enable-snat "$ROUTER" || return

  openstack port create --network "$NET_A" \
    --fixed-ip subnet="$SUBNET_A",ip-address="$ip_a" \
    --security-group "$SG" "$prefix-port1" || return
  openstack port create --network "$NET_B" \
    --fixed-ip subnet="$SUBNET_B",ip-address="$ip_b" \
    --security-group "$SG" "$prefix-port2" || return

  openstack --os-compute-api-version 2.74 server create \
    --image "$IMAGE" --flavor "$FLAVOR" --host "$COMPUTE1" \
    --port "$prefix-port1" --user-data "$DEMO_ROOT/cloud-init.yaml" \
    --wait "$VM1" || return
  openstack --os-compute-api-version 2.74 server create \
    --image "$IMAGE" --flavor "$FLAVOR" --host "$COMPUTE2" \
    --port "$prefix-port2" --user-data "$DEMO_ROOT/cloud-init.yaml" \
    --wait "$VM2" || return

  PORT_VM1=$(openstack port show "$prefix-port1" -f value -c id) || return
  openstack floating ip create --port "$PORT_VM1" "$EXT_NET" || return
  FIP_ID=$(openstack floating ip list --port "$PORT_VM1" -f value -c ID) || return
  FIP_VM1=$(openstack floating ip show "$FIP_ID" -f value -c floating_ip_address) || return
  VM1_IP="$ip_a"
  VM2_IP="$ip_b"
}
BASH

source "$DEMO_ROOT/create-demo-pair.sh"
create_demo_pair ovn-demo-new 10.235.0.0/24 10.235.1.0/24 \
  10.235.0.11 10.235.1.11 geneve 1392
declare -p VM1 VM2 VM1_IP VM2_IP ROUTER NET_A NET_B FIP_ID FIP_VM1 \
  > "$DEMO_ROOT/new.env"
openstack console url show --novnc "$VM1"
openstack console url show --novnc "$VM2"
printf 'New VM1 → new VM2: ping -D -O -n -i 0.2 -W 1 %s\n' "$VM2_IP"
printf 'New VM2 → controller: ping -D -O -n -i 0.2 -W 1 %s\n' "$CONTROLLER_ETH1_IP"
printf 'Controller → new FIP: ping -D -O -n -i 0.2 -W 1 %s\n' "$FIP_VM1"
```

Hàm tạo mới; không chạy lại trên cùng prefix nếu đã tạo một phần. Các IP
external được Neutron cấp từ pool dùng chung. Sau khi automatic cleanup kết
thúc, `.4/.5` được trả lại pool; `.6/.7` của bộ demo cũ vẫn được giữ. Nếu cần
quay lại bộ cũ, source lại `"$DEMO_ROOT/settings.env"`; bộ mới nằm trong
`new.env`. Không tạo external flat thứ hai trên cùng physnet.
