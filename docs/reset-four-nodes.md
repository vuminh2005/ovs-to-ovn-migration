# Reset lặp lại cụm 4 node về ML2/OVS

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
cần chạy reset lại ngay. Các thay đổi Pair A/B/C/D trong migration chưa thuộc
phần sửa reset này.

Reset destroy toàn bộ Kolla/OpenStack nested trên 4 node. Instance (kể cả Pair D
tạo thủ công), network, subnet, router, security group và Glance image/flavor cũ
sẽ mất theo state của cloud bị destroy. Image/flavor Ubuntu validation được
chuẩn bị lại sau deploy. Không phải rollback bảo toàn workload.

Giữ VM lab bên ngoài Viettel Cloud, host NIC eth0/eth1/eth2, `neutron-ext`, SSH,
venv Kolla, Docker image cache, `/root/multinode`, `/etc/kolla/passwords.yml`,
cache Ubuntu trên host và migration evidence. Controller không reboot;
network1/compute1/compute2 reboot lần lượt. Mặc định
`reset_delete_migration_backups: false`; chỉ đổi khi thực sự muốn xóa evidence cũ.

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
