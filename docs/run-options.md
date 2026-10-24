# Tùy biến một lượt migration

Mặc định là luồng gateway scheduling đã tối ưu metadata, cộng 6 VM ping để
đo downtime. Không chuẩn bị rollback, không backup MariaDB toàn cụm, không
monitor debug tổng quát và không tạo network smoke test. Capture ping bắt
buộc và các gate migration vẫn chạy. TCP, Pair B và VM tạo mới sau migration
đều tắt theo profile ping-only.

## Cách đặt biến

Chỉnh `group_vars/all.yml` hoặc `group_vars/ns-lab.yml` để đổi mặc định lâu dài.
Dùng `-e` để override riêng lần gọi hiện tại; giá trị này ưu tiên hơn file.
Chọn profile trước khi khởi chạy run mới. Không sửa config/evidence của run
đang đo để bật lại một tính năng đã bỏ qua.

Lệnh mặc định, chạy trong thư mục source với venv/Kolla credentials như trước:

```bash
ansible-playbook -i /root/multinode migrate-to-ovn.yml
```

| Biến | Mặc định | Ý nghĩa khi thay đổi |
|---|---|---|
| `migration_rollback_enabled` | `false` | `true` chuẩn bị đầy đủ snapshot host, Nova native, DB tạm, checkpoint Neutron và journal để có thể rollback |
| `run_mariadb_backup` | Theo cờ rollback | Backup vật lý MariaDB toàn cụm ở phase 01; có thể bật/tắt độc lập |
| `migration_diagnostics_enabled` | `false` | `true` bật thêm monitor/debug host, OpenFlow/OVSDB/OVN, events/logs; không điều khiển PCAP ping |
| `validation_ephemeral_network` | `false` | `true` tạo rồi xóa một network để kiểm tra Geneve qua API; không tạo VM |
| `validation_workloads_enabled` | `true` | `false` bỏ workload/probe VM; không có phép đo ping và không được coi là validation downtime đầy đủ |
| `validation_ping_only` | `true` | `false` bật lại VM DHCP/metadata riêng và cặp VM mới sau migration |
| `validation_pair_d_enabled` | `false` | Bật TCP E-W khi đồng thời đặt `validation_ping_only=false` |
| `ns_tcp_enabled` | `false` | Bật TCP SNAT/FIP khi đồng thời đặt `validation_ping_only=false` |
| `validation_ew_matrix_enabled` | `true` | `false` bỏ 2 VM ma trận và 3 luồng ping E-W bổ sung |
| `validation_ns_enabled` / `ns_enabled` | `true` | `false` bỏ 2 VM SNAT/FIP và các phép đo N-S |
| `ns_icmp_enabled` | `true` | Giữ `true` trong profile chỉ ping có N-S |
| `ns_diagnostics_enabled` | `true` | Capture N-S phục vụ số liệu ping; phải giữ `true` khi đo N-S ICMP |

Hai biến N-S/TCP và biến ma trận nằm trong `group_vars/ns-lab.yml`; các biến
khác nằm trong `group_vars/all.yml`. Quota nằm riêng trong `group_vars/quota.yml`,
reset/demo nằm trong `group_vars/reset.yml` và được giữ nguyên.

## Các cấu hình thường dùng

**Gọn nhất với đủ 6 VM ping:** dùng lệnh mặc định ở trên. Tổng 4 VM E-W và
2 VM N-S, chia đều 3 VM mỗi compute. Không tạo thêm VM ở phase 12.

**Có khả năng rollback:** chỉ cần bật một cờ từ đầu run:

```bash
ansible-playbook -i /root/multinode migrate-to-ovn.yml \
  -e migration_rollback_enabled=true
```

Mặc định của `run_mariadb_backup` sẽ thành `true` theo cờ trên. Có thể bỏ riêng
backup toàn MariaDB mà vẫn giữ rollback Neutron đầy đủ:

```bash
ansible-playbook -i /root/multinode migrate-to-ovn.yml \
  -e migration_rollback_enabled=true -e run_mariadb_backup=false
```

Checkpoint Neutron sau khi đóng API và restore-test vẫn bắt buộc trong chế độ
rollback, nên tăng thời gian API đóng so với profile gọn. Backup MariaDB toàn
cụm chạy trước downtime. Không có số giây tăng thêm được cam kết khi chưa đo.
Bật rollback không có nghĩa playbook tự rollback khi lỗi. Lệnh phục hồi vẫn là:

```bash
ansible-playbook -i /root/multinode rollback-to-ovs.yml \
  -e rollback_run_dir=/root/ovs-to-ovn-backup/<run-id>
```

Run ghi rollback tắt sẽ bị từ chối; không thể bật lại cờ để tạo checkpoint quá
khứ. Run cũ không có cờ mới vẫn dùng các guard/checkpoint lịch sử.

**Thêm thông tin để điều tra lỗi:**

```bash
ansible-playbook -i /root/multinode migrate-to-ovn.yml \
  -e migration_rollback_enabled=true \
  -e migration_diagnostics_enabled=true -e validation_ephemeral_network=true
```

**Validation đầy đủ, gồm TCP:**

```bash
ansible-playbook -i /root/multinode migrate-to-ovn.yml \
  -e validation_ping_only=false \
  -e validation_pair_d_enabled=true -e ns_tcp_enabled=true
```

Lệnh này tạo Pair B, cặp VM sau migration và TCP Pair D; peak capacity trở lại
12 VM khi ma trận và N-S vẫn bật. Rollback vẫn tắt nếu không bật riêng.

**Ít VM ping hơn:** `-e validation_ew_matrix_enabled=false` còn 4 VM (E-W
chính và N-S); `-e validation_ns_enabled=false` còn 4 VM (chỉ E-W đầy đủ).
Tắt cả hai còn 2 VM E-W chính. Các luồng đã tắt không có số liệu downtime.

`validation_workloads_enabled=false` nằm ngoài profile đo downtime: không có
evidence ping nên report có thể là `MIGRATED_VALIDATION_INCOMPLETE` và exit gate
không báo validation thành công, dù các bước chuyển đổi đã chạy.

## Đặt vị trí, nhịp đo và timeout

| Biến | Mặc định hiện tại | Phạm vi |
|---|---|---|
| `validation_pair_compute_hosts` | `[]` | Tự chọn 2 compute sẵn sàng; có thể đặt danh sách tên inventory chính xác |
| `validation_image`, `validation_flavor` | Rỗng | Dùng image/flavor được chuẩn bị tự động; có thể đặt UUID hoặc tên duy nhất |
| `dataplane_probe_interval_seconds` | `0.2` | Chu kỳ ping; phải từ `0.1` giây trở lên |
| `validation_timeout_seconds` | `400` | Giới hạn chờ workload/readiness |
| `post_restore_observation_timeout_seconds` | `300` | Chờ traffic gốc phục hồi sau mở API |
| `binding_timeout_base_seconds` | `300` | Timeout binding động: base + số port × per-port, có trần |
| `binding_timeout_seconds_per_port` | `2` | Hệ số theo số compute port nguồn |
| `binding_timeout_max_seconds` | `1800` | Trần timeout binding |
| `binding_poll_interval_seconds` | `5` | Chu kỳ kiểm tra binding/chassis |
| `rollback_shadow_tmpfs_size` | `2g` | RAM cho DB rehearsal, chỉ khi bật rollback |

Ví dụ ép đúng cặp compute và đổi nhịp ping cho một run:

```bash
ansible-playbook -i /root/multinode migrate-to-ovn.yml \
  -e '{"validation_pair_compute_hosts":["compute1","compute2"]}' \
  -e dataplane_probe_interval_seconds=0.5
```

`ns_endpoint_ip`, `ns_router_ip`, `ns_floating_ip`, CIDR/physnet và các địa chỉ
VM nằm trong `group_vars/ns-lab.yml`. Chỉ đổi khi địa chỉ đã được dành riêng,
khớp shared external và policy outer cloud; guard ownership/xung đột IP vẫn
áp dụng. Profile hiện tại dùng endpoint `10.1.0.187`, router `10.1.0.4`, FIP
`10.1.0.5`. Không dùng nhầm các địa chỉ demo `.6/.7`.

`mtu_delta_vxlan_to_geneve=8` điều khiển giảm MTU network theo MTU nguồn thực
tế. `target_geneve_mtu=1392` là giá trị mong đợi của guest validation trong
lab nested hiện tại; không dùng nó để ép mọi network trong cloud thành 1392.

## Thứ tự tối thiểu và dữ liệu vẫn phải giữ

Baseline/6 VM ping/capture → OVN central → giảm MTU, sinh cấu hình và chuẩn bị
metadata → preflight gateway, kiểm tra ping/capture → dừng Neutron API → migrate
DB và router backend → dừng metadata cũ, start metadata đã chuẩn bị → dừng
legacy agents → deploy controller → chờ chassis/binding → cleanup legacy →
schedule gateway sớm → start/check API → chờ ping phục hồi → validation/report/cleanup.

Thứ tự migration giống bản gateway scheduling. Khi bật rollback, các nhóm
snapshot/preflight/checkpoint được chèn vào các vị trí cũ; các bước chuyển đổi
không bị đổi thứ tự. Khi tắt, không stage helper rollback, không chụp host,
không đọc Nova native, không tạo DB tạm/dump/rehearse và không ghi journal
rollback trong cutover. `runtime.json` lưu lựa chọn ban đầu; JSON report có
`rollback_preparation` để đối chiếu.

Globals/inventory nguồn, UUID/port list, manifest metadata/gateway, MTU journal,
PCAP và ping sender log vẫn cần cho chuyển đổi, ownership, bảo toàn tài nguyên
và tính downtime. Chúng không phải một bộ rollback được chuẩn bị đầy đủ.
Các kiểm tra API/container, chassis/binding, metadata service và mapping/uplink
vẫn chạy. Cờ tùy chọn không bỏ các gate này hay tự reset lab khi thất bại.

Các profile và thứ tự được kiểm thử offline; chưa chạy profile mới trên lab
thật. Giữ inventory đầy đủ và tạo run mới sau khi lựa chọn các biến.
