# Giảm lỗi khởi tạo queue Nova khi reset lab

Bản này giữ toàn bộ N-S/diagnostics/report đã gửi trước đó, và bổ sung thay đổi
cho các lần **reset mới từ đầu**. Đây là biện pháp giảm khả năng tái diễn và
phục hồi có giới hạn, chưa phải bản vá nội bộ RabbitMQ hoặc cam kết hết lỗi.

## Cấu hình trước deploy

`group_vars/reset.yml` mặc định:

```yaml
reset_nova_scheduler_workers: 1
reset_nova_conductor_workers: 1
reset_rabbitmq_recovery_enabled: true
```

Reset ghi `nova_scheduler_workers: 1` vào `/etc/kolla/globals.yml` trước Kolla
deploy đầu tiên. Mã Kolla-Ansible 18.8.0 đã được đối chiếu: biến này đi vào
`[scheduler] workers` của nova.conf. Sau deploy, helper đọc cấu hình thực tế
`/etc/nova/nova.conf` trong nova_scheduler; không đúng số worker thì dừng.
Reset cũng ghi **`nova_cell_conductor_workers: 1`** (đúng tên biến Kolla 18.8.0,
không phải `nova_conductor_workers`) để render `[conductor] workers`. Sau deploy
thành công, helper `verify-conductor-workers` đọc cấu hình thật trong
`nova_conductor` và ghi `nova-conductor-workers.json`; sai worker thì dừng.
Không in transport_url hoặc credential. Phase 06 chỉ đổi các khóa Neutron/OVN
nên giữ nguyên cả hai số worker. API workers giữ cấu hình hiện có.

Thay đổi này giảm mức song song của scheduler/conductor; khi so sánh run cần dùng cùng
cấu hình. Con số 1 nhằm giảm cạnh tranh khởi tạo queue trong lab một controller,
không phải khuyến nghị hiệu năng cho production. Có thể thử A/B bằng
`-e reset_nova_scheduler_workers=5`, nhưng phải ghi rõ vào kết quả.

## Bằng chứng lần reset 20261015T131629018752058

Bundle `rabbitmq-investigation-20261015T134100Z.tar.gz` cho thấy:

- Deploy rc=2. Kiểm tra sau destroy ghi không còn named volume RabbitMQ cũ;
  mount hiện tại là named volume `rabbitmq`. Không có bằng chứng từ bundle rằng
  lỗi do tái sử dụng volume trước reset.
- RabbitMQ 3.13.7/OTP 26 running, Docker healthy, không OOM và không local alarm
  ở các mẫu đầy đủ. Scheduler quorum có leader/voter và 1 consumer.
- `nova-conductor.log`: `Starting 5 workers` lúc **20:27:25.122 +07:00**, cả
  5 workers khởi động lúc .130–.135. Lúc .210 driver ghi queue broken và thử
  delete; .255 RabbitMQ ghi `already_started`, .256 không form được Raft cluster,
  .262 declare conductor trả AMQP 541 `cluster_not_formed`. Lúc .275 còn lỗi
  bind 404 và driver thử delete/redeclare. Đây là bằng chứng cạnh tranh/churn
  khi startup; cơ chế lỗi nội bộ đầu tiên chưa được chứng minh đầy đủ.
- Trong 20 mẫu ban đầu **20:34:18–20:37:11** và 10 mẫu recheck đầy đủ
  **20:41:04–20:42:25**, 29/30 mẫu không thấy queue `conductor` trong list_queues
  và quorum_status trả rc=65 `Object ... was not found`. Một mẫu
  **20:35:40.888** thấy queue quorum running, leader/voter nhưng **0 consumers**:
  chưa có mẫu conductor sẵn sàng phục vụ. Việc queue xuất hiện tạm thời phù hợp
  với log delete/redeclare, chưa chứng minh đầy đủ cơ chế lỗi bên trong broker.
  `conductor.controller` vẫn tồn tại, không thay thế được queue RPC chung
  `conductor`.
- Mẫu cuối **20:42:30** có mọi command rc=124, timeout_scope=overall: đây là
  hết deadline của health helper, không chứng minh broker chết. Summary mới
  dùng issues của mẫu đầy đủ gần nhất cho `last_issues` trong trường hợp này;
  vẫn giữ `last_sample_issues`, `last_sample_incomplete` và toàn bộ samples.

Giảm conductor workers là biện pháp giảm cạnh tranh, không phải bản vá RabbitMQ.
Tăng health timeout không sửa được queue đã vắng liên tục nhiều phút.
Bản cũ chỉ giảm scheduler; nó chưa bao phủ conductor. Bundle không chứng minh
việc giảm scheduler gây ra lỗi conductor.

Deploy cũ chỉ lưu mã thoát nên **chưa xác định được task Kolla thất bại**.
Từ bản này, stdout/stderr đầy đủ được lưu trước health gate thành
`kolla-deploy.stdout.log`, `kolla-deploy.stderr.log` (0600, không dump console),
và `deploy-result.json` ghi rc, start/end/duration cùng tên hai log.

## Phục hồi chỉ trong reset lab

Gate ban đầu vẫn chờ 180 giây, cần 15 giây ổn định. Nếu FAIL, helper chỉ xem xét
phục hồi scheduler khi đáp ứng đủ:

- Policy của đúng reset đã confirm, một controller, inventory không thay đổi.
- Deploy rc=0; bằng chứng sau destroy không còn RabbitMQ volume.
- Cấu hình scheduler đúng số worker và data mount là named volume rabbitmq.
- Mẫu đầy đủ gần nhất không quá 10 phút, node running, không alarm, conductor
  leader/voter có consumer, không queue quorum khác bị down.
- Lỗi tập trung vào scheduler, có `already_started` và `cluster_not_formed`
  mới, log liên tục. Lỗi parser, timeout/transport, queue khác hoặc chỉ thiếu
  consumer không cho phép restart.
- Unit systemd đúng tên `kolla-<container>-container.service`, ExecStart/ExecStop
  thực sự điều khiển đúng Docker container. Thiếu/khác ownership thì dừng.

Helper lưu state/container identity và nén nguyên log RabbitMQ/Nova scheduler
trước thao tác. Mỗi log tối đa 128 MiB; không lưu được evidence thì không restart.
Nó kiểm tra live một lần nữa: nếu đã phục hồi, chỉ chạy lại gate, không restart.
Nếu lỗi thay đổi hoặc không còn chữ ký Raft mới thì dừng để đọc evidence.

Lượt can thiệp được ghi nhận bằng file tạo độc quyền và fsync **trước** thao tác:

1. Stop unit Nova scheduler; xác nhận unit inactive và container thật sự stopped.
2. Restart unit RabbitMQ; poll trạng thái Docker và check_running có timeout.
   Không gọi docker exec ngay khi container chưa running.
3. Kiểm tra alarm rồi start unit scheduler; đọc lại cấu hình worker.
4. Chạy đầy đủ health gate, lưu log/state sau thao tác. Chỉ PASS mới qua post-deploy.

Không tự delete queue, reset database, xóa volume, ép kill process hoặc thử restart
lần hai. Nếu bị ngắt sau khi đã dừng scheduler, report ghi rõ dịch vụ có thể vẫn
stopped; không tự che lỗi bằng cách tiếp tục migration. Mỗi snapshot chỉ có một
lần đánh giá/phục hồi; bằng chứng cũ không bị ghi đè khi gọi lại helper.

Tắt can thiệp tự động bằng `-e reset_rabbitmq_recovery_enabled=false`: cấu hình
worker và gate vẫn chạy. Logic này không được gọi trong migration đang có workload.

## Evidence và cách kiểm chứng

Trong `/root/kolla-reset-snapshots/<reset-id>/`:

- `reset-rabbitmq-policy.json`, `nova-scheduler-workers.json`.
- `nova-conductor-workers.json` sau deploy thành công.
- `deploy-result.json`, `kolla-deploy.stdout.log`, `kolla-deploy.stderr.log`.
- `rabbitmq-health.json`: lần kiểm tra ban đầu, giữ cả khi FAIL rồi phục hồi PASS.
- `rabbitmq-recovery.json`: kết quả, từng command/timestamp/rc, lượt can thiệp.
- `rabbitmq-recovery-attempt.json`: marker chặn lần can thiệp thứ hai.
- `rabbitmq-health-after-recovery.json`: toàn bộ lần kiểm tra sau phục hồi.
- `rabbitmq-recovery-before/`, `rabbitmq-recovery-after/`: log nén và container state.

Các file recovery không được tạo khi gate ban đầu PASS. Nếu tự phục hồi trước
can thiệp thì report có `attempted: false`, không có marker can thiệp.
Console health hiển thị cả sample cuối và issues/timestamp của sample đầy đủ gần
nhất, tránh hiểu queue null do overall deadline thành queue thật sự biến mất.

Lần lỗi conductor với deploy rc=2 không được tự restart bằng helper scheduler.
Không thay rc=2 thành rc=0 hoặc coi RabbitMQ PASS là hoàn tất Kolla deploy.
Để kiểm chứng thay đổi worker cho một lần reset sạch, cập nhật bản mới và chạy
lại toàn bộ reset bằng lệnh bên dưới. Reset mới sẽ dừng probe cũ, tạo snapshot
mới và áp dụng cả hai worker trước deploy; không sửa lại evidence snapshot cũ.

```bash
source /root/venvs/kolla-2024.1/bin/activate
cd /root/ovs-to-ovn-migration
ansible-playbook -i /root/multinode reset-lab-to-ovs.yml \
  -e reset_lab_confirm=true
```

Chỉ sau `OVS_4NODE_BASELINE_READY` mới chạy migration. Để đánh giá biện pháp giảm
lỗi, kiểm tra nhiều lần reset sạch: số worker thực tế, lần đầu gate PASS hay cần
recovery, số lỗi Raft mới và consumer scheduler. Không dùng một lần PASS để kết
luận hết lỗi. Không cần thực hiện lại reset chỉ để tiếp tục một reset cũ đã xong.

Không thêm đường resume reset trong bản này: người dùng chọn reset mới từ đầu.
Không dùng --start-at-task để bỏ qua các checkpoint của reset đang dở.

Nguồn đối chiếu:

- Nova 2024.1, `[scheduler] workers`:
  https://docs.openstack.org/nova/2024.1/configuration/config.html#scheduler.workers
- Kolla 2024.1, service configuration overrides:
  https://docs.openstack.org/kolla-ansible/2024.1/admin/advanced-configuration.html
- RabbitMQ maintainer phân tích process còn tồn tại khi redeclare quorum queue:
  https://github.com/rabbitmq/rabbitmq-server/discussions/15069
