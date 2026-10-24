# Rollback về ML2/OVS, giữ VM

Entrypoint mới là `rollback-to-ovs.yml`, độc lập với reset và recovery trong
`archive/`. Mặc định một lệnh chọn run sẽ inspect, tạo kế hoạch, tự dùng đúng
hash vừa tạo và thực thi nếu các điều kiện đạt. Không cần lệnh inspect riêng,
sao chép SHA hay các cờ xác nhận. Lỗi guard sẽ dừng trước bước phục hồi tương ứng.
Nếu run đã rollback hoàn tất, cùng lệnh tự chuyển sang verify, không restore lại.

Phạm vi hiện tại là Kolla 2024.1, Docker và kernel OVS. Bản này đã kiểm thử
offline; chưa chạy MariaDB image hay rollback dataplane trên lab thật.

## Sửa lỗi snapshot phase 01 ngày 24/10/2026

Helper dùng `docker container inspect` và nhận chính xác thông báo thiếu
container theo tên, không phân biệt hoa/thường (`no such object/container`).
Baseline OVS chưa có `ovn_controller`/`neutron_ovn_metadata_agent` được chấp nhận;
lỗi daemon, socket/quyền truy cập, tên khác hoặc response hỏng vẫn STOP. Container
OVN đang có, kể cả đã dừng, vẫn chặn snapshot nguồn như trước.

Nếu run dừng ở task `Capture original per-host config and container identities`
như log `20261024-071718`, chưa đến bước đổi backend/MTU hay đóng API. Sau khi
cập nhật toàn bộ source bản sửa, có thể chạy lại entrypoint migration để tạo
run mới; không cần reset chỉ vì lỗi kiểm tra này. Giữ nguyên thư mục run lỗi,
không resume giữa phase hoặc chép checkpoint sang run khác. Kết luận này áp dụng
cho lỗi phase 01 đó; run đã tiến xa hơn phải kiểm tra trạng thái riêng.

## Sửa lỗi Nova checkpoint sau API freeze

Nova 29.2.1 dựng `GET /servers/detail` có gọi Neutron lấy security groups.
Vì vậy việc dùng API đó để chốt VM sau khi dừng neutron-server tạo phụ thuộc
vào chính API vừa dừng. Log `20261024-072541` chỉ có `HttpException`, thiếu mã
HTTP/request ID nên chưa đủ kết luận response cụ thể; thứ tự phụ thuộc này đã
được xác nhận trong source và được loại khỏi workflow.

Reader mới chạy bằng Python/native dependencies trong container `nova_api`,
đọc cấu hình ở đó, không đưa DSN/mật khẩu ra argv hoặc evidence. Nó đọc
`cell_mappings`/`instance_mappings` trong Nova API DB và `instances` trong mọi
cell, kể cả cell0/cell disabled. Mỗi session MySQL/MariaDB bật
`SET SESSION TRANSACTION READ ONLY`; không dùng Nova HTTP, Neutron, RPC hoặc
Nova objects có thể lazy-load/ghi DB. Đọc hai lượt phải khớp nhau, không bỏ qua
cell lỗi, mapping thiếu, VM đang thao tác hay trạng thái ngoài phạm vi hỗ trợ.

Phase 07 preflight reader trước khi đóng API, pin container/image/hash reader
và inventory. Sau đóng API đọc lại, yêu cầu khớp preflight và libvirt trước khi
dump Neutron. Rollback inspect cũng dùng reader này, nên kiểm tra Nova vẫn có
thể chạy khi Neutron API dừng. Profile hỗ trợ Nova 29/Kolla 2024.1 và cell DB
MySQL/MariaDB; không thêm dependency vào Python controller hoặc `group_vars`.

Với đúng lỗi trong run `20261024-072541`, task dump checkpoint chưa chạy và
chưa bắt đầu migrate DB. Không chạy migration mới hoặc start API trực tiếp:
config đã được sinh cho OVN. Có thể dùng nhánh rollback preparation-only để
khôi phục config nguồn rồi bật API, sau khi inspect xác nhận `db_restore=false`
và `flow_rebuild=false`. Nhánh này không cần Nova frozen checkpoint chưa tạo.
Giữ run lỗi để điều tra; không resume giữa phase hay nối capture đã dừng vào
lượt đo mới. Lượt migration mới cần baseline sạch như điều kiện bên dưới.

## Checkpoint thêm vào migration

| Vị trí | Nội dung | Thời gian |
|---|---|---|
| Phase 01 | Cấu hình nguồn thực tế, units/restart policies, ID/image/mount của container từng host, inventory | Trước downtime |
| Phase 07, trước freeze | Preflight Nova reader bằng native READ ONLY; MariaDB tạm từ đúng image ID đang chạy; chốt topology OVS, VM/VIF và system-id từng host | Trước downtime |
| Phase 07, tất cả neutron-server đã dừng | Kiểm tra libvirt/Nova DB không gọi HTTP; dump riêng Neutron, restore thử vào DB tạm, re-dump và đối chiếu toàn bộ schema/data | Tăng thời gian đóng API |
| Phase 07–10 | Journal intent/kết quả cho DB, agents, controller, cleanup, gateway và API | Phần lớn ghép vào invocation sẵn có |

Checkpoint sát cutover giữ network/router/port của workload tạo ở phase 04.
Backup MariaDB đầu run vẫn riêng biệt, không dùng để tự động restore Neutron
sau khi đã có thêm workload.

DB tạm dùng `--network=none`, socket riêng, không port và không mount volume
dữ liệu thật. Nó được chuẩn bị trước freeze; mặc định dùng tmpfs `2g`, có thể
đặt `-e rollback_shadow_tmpfs_size=4g` nếu đủ RAM và DB lớn hơn. Schema ngoài
InnoDB hoặc có trigger/event/routine tùy biến sẽ bị chặn trước freeze.
`rollback_database_name` mặc định `neutron`, được kiểm với cấu hình API nguồn.
Các tùy chọn này không ghi thêm vào `group_vars`.

`rollback/checkpoint-timing.json` ghi `elapsed_seconds` của helper DB, chưa gồm
toàn bộ kiểm tra Nova/host và fetch. Công thức report giữ nguyên; control-plane
downtime sẽ phản ánh khoảng API đóng dài hơn. Chưa thể chốt số giây tăng thêm
khi chưa đo trên DB thật. Legacy agents vẫn chạy trong lúc lấy checkpoint,
nên không đồng nghĩa N-S ping mất thêm đúng khoảng thời gian đó. Thứ tự tối ưu
metadata và gateway scheduling giữ nguyên.

## Điều kiện vận hành

- Dùng toàn bộ source cùng phiên bản và đúng inventory gốc, không `--limit`.
  Không reset/redeploy, thay image/container nguồn giữa migration và rollback.
- Dừng hẳn migration/Kolla đang chạy trước inspect. Giữ nguyên checkpoints và
  journal, không xóa marker để ép chạy.
- Thực thi maintenance từ trước freeze đến khi chấp nhận migration/rollback:
  chặn thay đổi Neutron, Nova và automation, kể cả endpoint trực tiếp và các
  thao tác còn đang chạy. Cho phép truy cập API nội bộ để kiểm tra và agents
  đồng bộ. Việc gọi lệnh rollback được coi là yêu cầu thực thi trong điều kiện
  maintenance này; mặc định không hỏi xác nhận lại. Playbook dừng các writer
  Neutron cần thiết nhưng không tự tạo ACL chặn thao tác Nova hoặc automation.
- Inventory bao phủ toàn bộ compute phục vụ VM. Nova và libvirt phải khớp,
  không có task đang chạy. VM ACTIVE, SHUTOFF, PAUSED, SUSPENDED được nhận diện;
  trạng thái khác chặn freeze.
- Chỉ áp dụng run mới có source snapshot/checkpoint do bản này tạo. Run cũ
  thiếu chúng sẽ STOP; không thể tạo ngược checkpoint trước migration.

## Lệnh vận hành

Chạy trên controller với quyền root và venv như migration. Thay `<run-id>`
bằng run cần phục hồi, không tự chọn run mới nhất.

```bash
cd /root/ovs-to-ovn-migration
source /root/venvs/kolla-2024.1/bin/activate
source /etc/kolla/admin-openrc.sh

ansible-playbook -i /root/multinode rollback-to-ovs.yml \
  -e rollback_run_dir=/root/ovs-to-ovn-backup/<run-id>
```

Lệnh trên thực hiện toàn bộ chuỗi inspect → kiểm tra kế hoạch → quiesce →
phục hồi theo trạng thái → validation. `rollback/plan.json` vẫn lưu đầy đủ
`db_restore`, `flow_rebuild`, host/container và `plan_sha256`; hash được truyền
nội bộ từ kế hoạch vừa tạo, không dùng nhầm `inventory_sha256`. Console chỉ
hiện tóm tắt hành động, không yêu cầu người dùng tìm/copy hash.

Thay container/VM, thiếu checkpoint, lệch dữ liệu hoặc writer đang chạy sẽ
chặn các bước cần điều kiện đó. File kế hoạch bị thay đổi hoặc hash không khớp
cũng bị chặn. Một host quiesce lỗi chặn restore DB; import DB lỗi chặn start API.
Không tự mở maintenance sau khi phục hồi.

Chỉ xem trạng thái, không thực thi rollback, khi bạn chủ động muốn inspect:

```bash
ansible-playbook -i /root/multinode rollback-to-ovs.yml \
  -e rollback_run_dir=/root/ovs-to-ovn-backup/<run-id> \
  -e rollback_mode=inspect
```

Sau phục hồi, cùng lệnh mặc định tự verify. Có thể yêu cầu verify rõ ràng:

```bash
ansible-playbook -i /root/multinode rollback-to-ovs.yml \
  -e rollback_run_dir=/root/ovs-to-ovn-backup/<run-id> \
  -e rollback_mode=verify
```

Mặc định nằm ở đầu `rollback-to-ovs.yml`, không sửa `group_vars`:

| Tham số | Mặc định | Khi override |
|---|---|---|
| `rollback_mode` | `execute`, tự `verify` nếu run đã hoàn tất rollback | `inspect` chỉ đọc, `verify` kiểm tra lại |
| `rollback_execute` | `true` | `false` chặn bước thực thi |
| `rollback_maintenance_confirm` | `true` | `false` chặn thực thi khi maintenance chưa sẵn sàng |
| `rollback_plan_sha256` | Hash kế hoạch vừa inspect | Chỉ truyền nếu chủ động muốn pin một hash; sai hash vẫn STOP |
| `rollback_validation_timeout` | `180` giây | Tối đa `1800` giây |

`rollback_run_dir` vẫn cần chỉ định đúng run; không tự chọn run mới nhất trong
backup để tránh phục hồi nhầm deployment. Không cần thay các mặc định để chạy
nhánh rollback thông thường.

`rollback_validation_timeout` mặc định 180 giây, tối đa 1800. Chỉ tăng khi
probe đang sống cần thêm thời gian; timeout không thay được probe đã dừng.
Không dùng `--start-at-task`, `--skip-tags` hay gọi helper lẻ để bỏ qua barrier.

## Phục hồi theo thay đổi thực tế

| Trạng thái | Hành động |
|---|---|
| Chỉ chuẩn bị central/config/MTU | Trả config nguồn; giữ/bật source API và agents, không restore DB hay xóa flow |
| API đã dừng, chưa migrate | Trả config ML2/OVS thực tế rồi bật API, không restore DB |
| Đã bắt đầu migrate, kể cả lỗi giữa chừng | Bắt buộc checkpoint đã restore-test; dừng mọi writer rồi restore riêng Neutron |
| Controller đã deploy hoặc cleanup một phần | Cộng thêm dừng OVN controller/metadata mọi host, trả quyền OVSDB/OpenFlow rồi để agents nguồn dựng topology |
| API OVN đã mở | Chỉ restore khi Neutron/Nova/VM khớp checkpoint sau khi bỏ qua các biến đổi nội bộ đã biết của migration |

Journal ghi intent trước thao tác; task lỗi không chứng minh chưa thay đổi gì.
Kế hoạch dùng trạng thái thực tế theo host, không chỉ số phase. Tạo/xóa VM/port,
đổi MAC/IP/binding/SG hoặc live migration sau checkpoint sẽ chặn restore tự động.
Smoke test/workload do phase 11–12 tạo cũng được tính. Thay đổi table/field chưa
biết sẽ STOP để đối soát, không tự bỏ qua.

Execute quiesce mọi host trước restore; một host lỗi chặn bước DB. Chỉ schema
Neutron được DROP/import, không restore cả MariaDB hoặc Nova/Keystone. Dump
phải đúng hash và image đã restore-test. Import lỗi giữ API/agents dừng.

Config restore giới hạn vào Neutron/globals và units nguồn đã lưu, không ghi
đè passwords.yml hay cấu hình dịch vụ khác. Chỉ bật đúng container nguồn có
ID/image/mount khớp snapshot; thiếu hoặc thay container sẽ STOP.

OVS được trả quyền tại chỗ: giữ UUID br-int/br-ex, tap/VIF, uplink, system-id
và underlay. Chỉ loại artifact được chứng minh thuộc OVN; có takeover thì bỏ
pipeline OVN ở br-int để OVS agent lập trình lại. Không replay flow dump.
API nguồn được bật cho nội bộ trước agents; OVS/L3/DHCP/metadata agents dựng
lại tunnel bridge, patch ports và namespaces từ DB.

Frozen DB chứa MTU an toàn đã giảm ở phase 06. Rollback giữ mức đó khi cứu mạng;
chưa tự tăng lại. Sau khi OVS ổn định mới xử lý MTU gốc theo journal và xác minh
cả network lẫn guest.

Import bị gián đoạn để lại marker `RESTORING`, giữ nguyên dump được duyệt và
backup lỗi đầu tiên. Inspect/execute lại có thể retry chính checkpoint đó;
không dùng DB đang import dở làm baseline mới. Chạy lại cùng lệnh sẽ inspect
lại và tự dùng hash mới; các gate checkpoint/drift vẫn phải đạt.
Không xóa marker.

## Kiểm chứng và chấp nhận

Kiểm tra gồm source containers/driver, UUID/system-id, VM placement/VIF,
underlay, Neutron port/IP/MAC/binding/SG và probe E-W/N-S/DHCP/metadata/MTU đang
có. Validator đợi thành công mới, không tạo tài nguyên, restart probe, nối lại
held TCP hoặc ghi lại report/evidence migration cũ.

Coverage traffic phụ thuộc probe của run. Probe đã dừng/mất console/thiếu
stream giữ kết quả STOP dù OVS có thể đã phục hồi. Packet test SG deny ghi
`NOT_RUN`; so sánh SG trong DB không thay thế kiểm chứng chặn gói. Operator cần
kiểm thử policy/đường đi quan trọng trước khi chấp nhận và mở maintenance.
Không cam kết giữ phiên TCP/NAT cũ. Kết quả cuối là
`RESTORED_REQUIRES_ACCEPTANCE`; playbook không tự bỏ maintenance.

OVN central và volume NB/SB được giữ làm evidence, còn controller/metadata host
bị dừng/disable. Kolla 18.8 có guard ngăn OVS deploy/reconfigure khi còn
container/volume OVN: sau chấp nhận cần decommission artifact OVN có kiểm soát
và backup trước khi trở lại vận hành Kolla bình thường. Bản này không tự xóa
central/volume hay chạy blanket destroy. Không resume migration cũ sau rollback;
lượt migrate tiếp theo cần baseline OVS sạch và run mới.

## Evidence

Controller: `/root/ovs-to-ovn-backup/<run-id>/rollback/`:

- `spec.json`, `source/<host>.json`/`.tar`: config và danh tính nguồn.
- `frozen/<host>.json`, `nova-frozen.json`: topology/VM sát trước migrate.
- `database/neutron.sql`, `database-checkpoint.json`, `database-business.json`:
  SQL đã restore-test và hash; `checkpoint-timing.json`: thời gian helper DB.
- `inspection/`, `database/database-inspection.json`, `nova-inspection.json`,
  `plan.json`, `execution.json`, `blocked.json`: trạng thái và quyết định.
- `verified/`, `functional.json`, `complete.json`: kết quả sau phục hồi.

Từng host: `/var/lib/ovs-to-ovn-rollback/<run-id>/` có source backup, journal và
helpers. MariaDB host còn giữ `neutron-before-rollback.sql`, `database-restore.json`;
central host có NB/SB backup nếu inspect được. Thư mục rollback private;
archive/SQL chứa cấu hình hoặc dữ liệu nhạy cảm, không chia sẻ công khai.
Reset không làm checkpoint cũ hợp lệ cho deployment vừa được tạo lại.

## Kiểm thử

`tests/recovery/test_rollback.py` kiểm tra inventory/container/VM/drift guards,
restore rehearsal, Neutron-only DROP/import, retry import dở, archive traversal,
validation không ghi evidence cũ và barrier bằng Ansible local với transport giả.
Cần thử trên lab các mốc: trước migrate, giữa migrate, sau controller, sau
cleanup, sau mở API có/không có thay đổi tài nguyên trước khi dùng trong prod.

Đối chiếu: [Kolla MariaDB backup/restore](https://docs.openstack.org/kolla-ansible/2024.1/admin/mariadb-backup-and-restore.html),
[Kolla operation](https://docs.openstack.org/kolla-ansible/2024.1/user/operating-kolla.html),
[Neutron 2024.1](https://docs.openstack.org/neutron/2024.1/configuration/samples/ml2-conf.html).
