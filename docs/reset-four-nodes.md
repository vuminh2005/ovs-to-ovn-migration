# Reset lặp lại cụm 4 node về ML2/OVS

Cập nhật 15/10/2026: reset đặt Nova scheduler 1 worker và có một lượt phục hồi
giới hạn cho đúng lỗi Raft `scheduler` đã ghi nhận. Xem
[cấu hình, điều kiện và cách kiểm chứng](reset-rabbitmq-mitigation.md).

Bản reset này được sửa từ commit `a77af56`, dành cho lab hiện tại:

| Group Kolla | Host | eth0 |
|---|---|---|
| control | controller | 10.0.0.213/24 |
| network | network1 | 10.0.0.160/24 |
| compute | compute1 | 10.0.0.62/24 |
| compute | compute2 | 10.0.0.171/24 |

Dùng inventory Kolla đầy đủ tại `/root/multinode`. `inventory.example` chỉ là
phần minh họa host, không đủ service groups để dùng deploy. Network2 phải được
loại khỏi inventory đầy đủ; reset sẽ từ chối nếu còn host đó ở bất kỳ group nào.

## Khi nào chạy

Chạy sau một lần migration/demo khi muốn xóa workload và dựng lại baseline OVS.
Có thể chạy khi cụm đang OVS hoặc OVN. Lab vừa in `OVS_4NODE_BASELINE_READY` chưa
cần chạy reset lại ngay. Hướng dẫn mở rộng EW/N-S mới nằm trong `ns-ew-matrix.md`.

Reset destroy toàn bộ Kolla/OpenStack nested trên 4 node. Instance (kể cả Pair D
tạo thủ công), network, subnet, router, security group và Glance image/flavor cũ
sẽ mất theo state của cloud bị destroy. Image/flavor Ubuntu validation được
chuẩn bị lại sau deploy. Không phải rollback bảo toàn workload.

Giữ VM lab bên ngoài Viettel Cloud, host NIC eth0/eth1/eth2, uplink Linux bridge/veth N-S, SSH,
venv Kolla, Docker image cache, `/root/multinode`, `/etc/kolla/passwords.yml`,
cache Ubuntu trên host và migration evidence. Controller không reboot;
network1/compute1/compute2 reboot lần lượt. Mặc định
`reset_delete_migration_backups: false`; chỉ đổi khi thực sự muốn xóa evidence cũ.

Trước Kolla destroy, reset dừng các process `snat-server`/`fip-client` trên
controller có checkpoint N-S hợp lệ. Controller không reboot nên listener của
run FAIL có thể còn giữ cổng 18180. Bước này kiểm tra run/config/cmdline cùng
PID, boot và start_ticks; chỉ gửi SIGTERM tới đúng process qua pidfd. Không xóa
log/metrics run cũ. Evidence dừng process nằm trong snapshot reset ở
`local-probes-stop.json`; ownership không khớp sẽ dừng trước Kolla destroy.

Network1 giữ IP `10.1.0.90/24` và MAC của eth1 trên `br-underlay`;
Netplan và service veth tạo lại `ns-uplink ↔ ns-ex` qua reboot. Tunnel trên
network1 dùng `br-underlay`, hai compute dùng `eth1`. Sau deploy reset đặt
quota **instances=20**, giữ quota cores/RAM và kiểm tra lại.

## Lệnh chạy cho lần reset sau

Từ thư mục chứa bản playbook đã sửa:

```bash
source /root/venvs/kolla-2024.1/bin/activate

ansible-playbook -i /root/multinode reset-lab-to-ovs.yml --syntax-check

ansible-playbook -i /root/multinode reset-lab-to-ovs.yml \
    -e reset_lab_confirm=true
```

Không dùng `--limit`, `--check`, `--start-at-task` hoặc inventory trích đoạn.
Nếu đổi đường dẫn inventory, dùng cùng đường dẫn tuyệt đối cho cả hai:

```bash
ansible-playbook -i /root/my-multinode reset-lab-to-ovs.yml \
    -e reset_kolla_inventory=/root/my-multinode \
    -e reset_lab_confirm=true
```

`group_vars/reset.yml` chứa cấu hình riêng của reset. Không phụ thuộc thư mục
preparation của lần chuyển 5 → 4 node, không cần image export EW và không đổi
inventory giữa destroy/deploy.

## Trình tự và trạng thái cuối

1. Kiểm tra đúng 4 host, full service groups, hostname/IP và MTU eth1 1450.
2. Tạo snapshot riêng tại `/root/kolla-reset-snapshots/<UTC-run-id>/`, lưu globals,
   custom config, inventory và passwords với quyền truy cập giới hạn.
3. Dừng guest libvirt trên cả hai compute; destroy Kolla bằng cùng full inventory.
4. Kiểm tra RabbitMQ volume và Neutron namespace còn sót; reboot tuần tự, kiểm
   tra bridge/namespace residue. Lỗi bất kỳ host nào sẽ dừng toàn bộ playbook.
5. Dựng lại OpenStack 2024.1/QEMU, ML2/OVS + VXLAN, centralized L3, native OVS
   firewall; không provider/DVR/agent HA. Giữ kiểm tra RabbitMQ của baseline.
6. Kiểm tra firewall trên cả 3 OVS chassis; rendered MTU và network VXLAN tạm
   phải có MTU 1400. Network tạm được xóa sau kiểm tra.
7. Kiểm tra cloud không có instance/network/subnet/router, đúng hai Nova computes
   enabled/up và sáu Neutron agents alive/enabled.
8. Chuẩn bị Ubuntu 24.04 bằng `scripts/validation_prerequisites.py` của baseline:
   tải từ Canonical khi cần, xác minh SHA256SUMS, upload image
   `ovn-validation-ubuntu-24.04`, chờ ACTIVE; tạo `ovn-validation.small`
   (1 vCPU, 1024 MB RAM, 8 GB disk). Không khôi phục image/flavor EW.
9. Đọc lại Glance state; kiểm tra format/size và content hash với download đã
   xác minh. Ghi `reset-complete.json` và `migration-lab.yml`, rồi in
   `OVS_4NODE_BASELINE_READY`.

Neutron override chung là `/etc/kolla/config/neutron.conf`; ML2 và OVS agent
overrides nằm trong `/etc/kolla/config/neutron/`. Transport MTU 1450, VXLAN
tenant MTU 1400, target Geneve MTU 1392. Không tăng MTU của NIC outer cloud.

UUID image/flavor thay đổi sau mỗi lần destroy. File `migration-lab.yml` trong
snapshot mới ghi UUID hiện tại cùng `target_geneve_mtu: 1392` để lưu bằng chứng.
Bản migration A/B/C/D đã có mặc định cho lab này và tìm image/flavor theo tên;
không cần truyền file snapshot đó:

```bash
ansible-playbook -i /root/multinode migrate-to-ovn.yml
```

Không dùng lại UUID hay override file của cloud đã bị destroy.

## Nếu lỗi ở cuối

Đọc lỗi và xử lý resource/service tương ứng. Nếu deploy, firewall và kiểm tra
MTU đã pass, chỉ còn lỗi tải/upload image hoặc tạo flavor, có thể retry riêng
phần prerequisites bằng snapshot của lần chạy đó:

```bash
source /root/venvs/kolla-2024.1/bin/activate
source /etc/kolla/admin-openrc.sh
python3 scripts/reset_validation.py finish \
    /root/kolla-reset-snapshots/<UTC-run-id>
```

Helper đọc lại image, chờ ACTIVE và từ chối duplicate/incompatible resource.
Mỗi lần reset toàn bộ có snapshot mới; không dùng checkpoint một lần của kit
5 → 4 node. Các kiểm tra offline không thay thế việc chạy thử trên lab.

## Nguồn

- Baseline ZIP người dùng cung cấp: `a77af56d69f170f7c79694f15266eb0f0cf39754`.
- https://docs.openstack.org/kolla-ansible/2024.1/user/operating-kolla.html
- https://docs.openstack.org/kolla-ansible/2024.1/admin/advanced-configuration.html
- https://cloud-images.ubuntu.com/releases/noble/release/

## RabbitMQ sau deploy: lỗi khởi tạo và trạng thái hiện tại

Gate cũ từ chối mọi run có dòng `cluster_not_formed`, `already_started` hoặc
`failed to form a new cluster` trong log kể từ trước deploy, kể cả queue đã
phục hồi. Điều đó không chứng minh dữ liệu Mnesia/Raft của cloud cũ còn sót.

Output ngày 2026-10-14 xác nhận: RabbitMQ 3.13.7 chỉ có `rabbit@controller`, không
alarm/partition; 69 quorum queue đều running; `scheduler` có 3 consumer,
`conductor` có 5 consumer. `scheduler` có một Raft leader/voter và commit/applied
index đều bằng 5. Như vậy lỗi declare lúc 17:57:04 đã phục hồi tại thời điểm
kiểm tra. Nguyên nhân trực tiếp khiến reset dừng là gate dựa trên lịch sử log.
Thiếu toàn bộ log quanh 17:57 nên chưa xác định được race/bug nội bộ nào gây
lần declare thất bại; không được gọi đó là cache cũ hay khẳng định đã vá RabbitMQ.

Gate mới dùng `scripts/reset_rabbitmq_health.py`, chỉ đọc trạng thái:

- RabbitMQ application phải running và không có local alarm.
- Tất cả quorum queue hiện có phải running; `conductor` và `scheduler` phải là
  quorum queue với ít nhất một consumer.
- Cả hai queue chính phải có đúng leader/voter của lab một controller.
- Các kiểm tra phải ổn định ít nhất 15 giây, không có lỗi Raft mới trong log.
  Lỗi mới hoặc log bị rotate/truncate làm tính lại khoảng ổn định.
- Hết 180 giây chưa đạt, lỗi đọc/parse CLI, thiếu log, hoặc deploy rc khác 0 vẫn
  làm reset fail. Bản 15/10 bổ sung một lượt restart có điều kiện trong reset
  lab; trạng thái sau phục hồi vẫn phải qua toàn bộ gate. Không delete queue
  hay xóa volume để làm PASS.

`group_vars/reset.yml` có timeout/stability/log path. Bằng chứng nằm trong
snapshot reset tại `/root/kolla-reset-snapshots/<id>/`:

- `deploy-result.json`: exit code deploy và inventory.
- `rabbitmq-health.json`: từng sample, queue/consumer, Raft membership, alarm,
  lỗi CLI, lỗi Raft mới và khoảng ổn định.
- `rabbitmq-deploy-raft-errors.log`: các dòng lỗi phát hiện từ giai đoạn deploy;
  dữ liệu chẩn đoán, không thay thế kiểm tra live.
- `rabbitmq-storage-after-destroy-<host>.json`: kết quả kiểm tra named volume
  RabbitMQ sau destroy trên từng host. Docker lỗi sẽ dừng kiểm tra, không bị
  `grep ... || true` che thành kết quả rỗng.

Reset vẫn dùng Kolla destroy rồi chặn nếu còn named volume RabbitMQ; lần fail
vừa phân tích đã qua gate đó. Chưa có bằng chứng cần thêm lệnh xóa. Kiểm tra
named volume không chứng minh một custom bind mount bất kỳ đã được xóa. Nếu
đổi `rabbitmq_datadir_volume` khỏi cấu hình chuẩn, phải xem mount thực tế trước
khi kết luận storage sạch:

```bash
docker inspect --format '{{json .Mounts}}' rabbitmq
```

Không xóa volume/queue của deployment mới chỉ vì log có lỗi cũ đã phục hồi.
Lỗi declare lúc khởi tạo vẫn có thể tái diễn; bản sửa tránh fail do lịch sử lỗi
khi đã chứng minh live recovery, đồng thời vẫn chặn lỗi còn tiếp diễn. Chưa có
live reset trên lab để cam kết tần suất hay khẳng định lỗi RabbitMQ hết hẳn.

Run đang dừng tại gate RabbitMQ chưa chạy `post-deploy` và các bước hoàn thiện
baseline phía sau; RabbitMQ healthy chưa đồng nghĩa đã đạt `OVS_4NODE_BASELINE_READY`.
Không dùng `--start-at-task` để bỏ qua các checkpoint của reset.

Nguồn đối chiếu:

- RabbitMQ 3.13 monitoring (check_running/check_local_alarms):
  https://www.rabbitmq.com/docs/3.13/monitoring
- RabbitMQ 3.13 quorum_status:
  https://www.rabbitmq.com/docs/3.13/man/rabbitmq-queues.8
- oslo.messaging có retry cho lỗi queue declare AMQP 541; đây là cơ chế có thể
  phục hồi lỗi, không chứng minh chính xác race của lab:
  https://github.com/openstack/oslo.messaging/blob/master/oslo_messaging/_drivers/impl_rabbit.py

### Sửa parser ANSI/DEC sau run 20261014T112120609523272

Trong run này deploy rc=0, RabbitMQ running, không local alarm hay lỗi Raft mới.
Các queue chính đều running với 5 consumer. `quorum_status` trả rc=0 và dòng
leader/voter hợp lệ, nhưng stdout dùng ANSI styling và DEC special graphics
(`ESC(0x ESC(B`) thay vì ký tự Unicode `│`. Parser ban đầu không nhận ra ranh giới
cột nên trả danh sách member rỗng và làm gate FAIL sai. Đây là lỗi helper,
không phải bằng chứng cache cũ hay Raft không có leader.

Parser mới chuẩn hóa DEC vertical rules và bỏ ANSI CSI styling trước khi tách
cột; chỉ chuyển ký tự trong vùng DEC, không sửa tên node. Ba stdout thành công
nguyên bản được giữ trong `tests/fixtures/rabbitmq-quorum-terminal.json` và được
chạy qua kiểm thử parser cùng gate Ansible. Follower, sai node/member và lỗi CLI
vẫn bị từ chối. Timeout command giờ ghi rõ `timeout_scope` là `command` hoặc
`overall`, tránh nhầm ngân sách cuối của toàn bộ vòng chờ với queue bị treo.

Sau khi cập nhật code, kiểm tra lại riêng helper trên deployment hiện tại,
lưu evidence mới để giữ nguyên file FAIL cũ:

```bash
cd /root/ovs-to-ovn-migration
python3 scripts/reset_rabbitmq_health.py \
  /root/kolla-reset-snapshots/20261014T112120609523272/rabbitmq-health-recheck.json
```

Lệnh này chỉ đọc RabbitMQ/log và ghi file bằng chứng. PASS xác nhận RabbitMQ
readiness tại lần kiểm tra mới; không tự chạy `post-deploy`, các bước hoàn thiện
baseline, hay đánh dấu toàn bộ reset hoàn tất.
