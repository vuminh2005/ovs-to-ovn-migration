# Migration theo inventory — cập nhật 23/10/2026

Nguồn sửa: `ovs-to-ovn-migration-audited.zip`, phiên bản 3 ngày 22/10/2026.
Đây là cập nhật luồng **migration**, không mở rộng destructive reset.
Số host được lấy theo vai trò, không có nhánh dành riêng 4 hoặc 20 node.
Các giới hạn topology, phiên bản và tính năng nguồn vẫn được giữ.

## Thay đổi

1. Phase 02 render cấu hình Kolla theo từng host, đọc `local_ip` và
   `bridge_mappings` từ cấu hình OVS agent đang có; xác minh interface thực sự
   sở hữu IP tunnel. Không ép network dùng `br-underlay`/`ns-ex` và không ép
   mọi network node mang IP `10.1.0.90`. Migration không sửa netplan/uplink.
2. Lưu `host-topology.json`: system-id, interface, IP/MTU tunnel và mapping
   external của từng host. Phát hiện system-id hoặc IP tunnel trùng trước
   freeze. Hash file này được chốt cùng checkpoint chuẩn bị cutover.
3. Pair D và N-S tìm **riêng từng router UUID** trên tất cả network host,
   yêu cầu một namespace nguồn duy nhất. Việc tìm diễn ra trước freeze, không
   dùng vị trí phần tử đầu inventory. `ARM` dùng checkpoint gốc và không gọi
   Neutron API hay chạy discovery mới sau freeze. Router di chuyển sau khi
   đã chốt làm run dừng, không tự nhận một vị trí khác vào checkpoint.
4. `validation_pair_compute_hosts: []` chọn tự động hai compute sẵn sàng theo
   thứ tự alias; có thể chỉ định cặp tường minh. Nova, hypervisor và OVS agent
   phải khớp danh tính. Quota/RAM/disk vẫn được kiểm tra trước workload. Việc
   chọn host được lưu trong placement checkpoint và giữ nguyên khi resume.
5. Phase 08 chờ từng system-id xuất hiện trong OVN với đúng Geneve tunnel IP;
   chassis dư không thay thế được host đang thiếu. Chờ binding đọc bảng một
   lần mỗi vòng, đối chiếu toàn bộ port gốc và giữ yêu cầu có chassis/up=true.
   Hash danh sách port, timeout theo số port và mốc đo cũ được giữ. Lỗi cutover
   dừng các play sau thay vì tiếp tục trên nhóm host khác.
6. Observer dùng interface theo host-topology. Phase 11 đối chiếu mạng gateway
   với checkpoint nguồn. Historical single-network checkpoints vẫn có nhánh
   tương thích, không đổi UUID, probe/session hoặc timestamp cũ. Không ghép
   phiên bản source này vào một run đang hoạt động; hoàn tất/dừng run trước.

## Cách cấu hình

Với lab 4 node hiện tại, giữ `/root/multinode`, cấu hình Kolla và các giá trị
trong `group_vars/ns-lab.yml`. Source profile vẫn là VXLAN 1400 → Geneve 1392,
tunnel transport 1450. Network1 được đọc thành `br-underlay` và IP của chính
nó; compute1/compute2 thành `eth1`. Hai router kiểm thử cùng tìm về network1.
Mặc định Pair D, N-S và các cờ remediation Pair B được giữ.

Trên cụm khác, tiếp tục dùng **inventory Kolla đầy đủ**. Nhóm `ovn-controller`
phải bao phủ tập hợp không trùng của network/compute; OVN DB/northd phải có
host; metadata group phải bao phủ compute. Inventory minh họa không thay cho
inventory triển khai đầy đủ của Kolla.

Interface được lấy từ cấu hình Kolla đã render theo host. Có thể đặt override
`migration_tunnel_interface`, `migration_external_interface` và
`migration_expected_tunnel_address` trong `host_vars` khi cần; precheck vẫn
đối chiếu với cấu hình hiệu lực. Đường dẫn OVS agent mặc định là
`/etc/kolla/neutron-openvswitch-agent/openvswitch_agent.ini`; biến
`migration_source_ovs_config` chỉ dành cho đường dẫn triển khai tùy chỉnh.

Chọn cặp compute cụ thể bằng `-e '{"validation_pair_compute_hosts":["host-a","host-b"]}'`.
Nếu không đủ hai compute sẵn sàng, validation cross-compute dừng trước freeze;
không tự bỏ kiểm thử và không báo PASS. Chỉ tắt toàn bộ workload bằng
`-e validation_workloads_enabled=false` khi chủ động chấp nhận không có bộ đo này.

`chassis_registration_timeout_seconds` mặc định 300. Binding timeout vẫn là
`min(300 + 2 * số_VM_port_gốc, 1800)` giây; có thể override các biến hiện có
`binding_timeout_base_seconds`, `binding_timeout_seconds_per_port`,
`binding_timeout_max_seconds`. Không suy ra downtime thực tế từ số node.

## Thông tin còn cần cho một cụm mới

| Thông tin | Mục đích |
|---|---|
| Inventory Kolla đầy đủ, alias/hostname, địa chỉ quản trị và cách SSH | Kết nối đúng host; phân nhóm control/network/compute và dịch vụ OVN |
| Cấu hình Kolla hiệu lực, interface/IP tunnel từng host | Đọc và kiểm chứng tự động khi đã truy cập được host |
| MTU transport và MTU tenant dự kiến | Chọn `target_geneve_mtu` đúng môi trường; mặc định 1392 chỉ là profile lab hiện tại |
| External network/subnet, physnet, bridge/uplink trên mỗi gateway | Hiện hỗ trợ một external flat đúng cấu hình; không tự dựng uplink trong migration |
| Endpoint N-S, interface capture tại endpoint và khả năng định tuyến | Thay `ns_endpoint_ip`, `ns_capture_interface` theo nơi chạy endpoint/controller |
| IP SNAT/FIP dành riêng cho probe và pool cấp phát | Thay `ns_router_ip`, `ns_floating_ip`; kiểm tra allocation thực tế, không suy ra IP trống từ ping |
| CIDR tenant kiểm thử không xung đột; image/flavor/quota | Chuẩn bị workload A–D, EW matrix và N-S có sẵn |
| DVR/agent HA/provider VM ports, loại tenant network và phiên bản thực tế | Đối chiếu phạm vi hỗ trợ trước cutover |
| Chính sách mạng ngoài nếu chạy nested | Xác nhận MAC/IP router và FIP được outer cloud cho phép |

Các giá trị N-S hiện vẫn là **default của lab**, được cấu hình bằng biến và
có thể override; không thể tự đoán địa chỉ external, endpoint hay dải IP được
phép cấp khi chưa có thông tin hạ tầng. Cờ `ns_preserve_snat_source_port` giữ
profile cũ: nếu bật, các L3 agent nguồn phải có `use_random_fully=false` trước
khi tạo workload; không có bước chuyển conntrack state.

## Lab 4 node và giới hạn kiểm chứng

`reset-lab-to-ovs.yml` và cấu hình reset giữ nguyên giới hạn 4 node. Không dùng
reset này với inventory nhiều node hơn. Workflow Phase 00–13, OpenFlow phase
08, phép đo, format report và quota opt-in không đổi.

Khi lab đang ở trạng thái ML2/OVS và không có run đang hoạt động, thay toàn bộ
source trong thư mục sạch, giữ cấu hình/inventory/run data ngoài source:

```bash
cd /root/ovs-to-ovn-migration
source /root/venvs/kolla-2024.1/bin/activate
source /etc/kolla/admin-openrc.sh
ansible-playbook -i /root/multinode migrate-to-ovn.yml --syntax-check
ansible-playbook -i /root/multinode migrate-to-ovn.yml
```

Không cần cờ về số node. `increase_quota=false` và mức yêu cầu opt-in 20 VM
được giữ. Đây là quota VM của project, độc lập với số node hạ tầng.

Kiểm chứng tự động gồm real-Ansible local với profile 4 node và 20 node,
interface/IP/config OVS mô phỏng theo từng host; discovery ba network host;
router riêng biệt, missing/duplicate/moved namespace; chassis đúng danh tính;
hash checkpoint và binding theo lô; cùng toàn bộ regression suite.
Các test không kết nối OpenStack/SSH thật và không chứng minh traffic hay
thời gian downtime trên lab. Cần một lượt migration lab mới để xác nhận hành
vi OVS/OVN, gateway scheduling, MTU/DHCP và TCP continuity thực tế.

Kết quả: **672 test PASS; 45 test checkpoint/recovery liên quan PASS ở lượt kiểm tra cuối; 16/16 syntax-check PASS; static checks và Ruff PASS.** Chi tiết nằm trong [node-independent-validation.json](node-independent-validation.json).
