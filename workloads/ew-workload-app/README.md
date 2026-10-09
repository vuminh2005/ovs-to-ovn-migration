# EW workload app — Ubuntu 24.04 / Kolla 2024.1 lab

Ứng dụng HTTP chạy trên ew-app (192.168.101.11:8080), sử dụng PostgreSQL
trên ew-db (192.168.102.12:5432) và RabbitMQ trên ew-queue (192.168.102.11:5672).
Không cần tải package hoặc Internet trong guest. Image netfix đã chứa dependencies.

## Cài đặt

Chạy setup.sh bằng root bên trong ew-app sau khi giải nén archive.
Script yêu cầu /etc/ew-lab/db-password và mq-password đã được tạo ở các bước trước.
Script giữ database và task hiện có; không DROP/TRUNCATE, không đổi mật khẩu.
Có thể chạy lại; API và worker sẽ được restart để nạp code.

Code: /opt/ew-lab. Environment: /etc/ew-lab/app.env (root, 0600).
Hai dịch vụ ew-api.service và ew-worker.service chạy bằng ubuntu, tự bật khi boot.

## Luồng và giới hạn

1. Client POST /jobs với UUID riêng, run_id, client_id và payload.
2. API commit task pending vào PostgreSQL rồi trả HTTP 202. Task pending chính là
   outbox: nhận task không phụ thuộc việc RabbitMQ đang kết nối được.
3. Worker quét tối đa 32 task pending mỗi 2 giây, publish message persistent
   vào exchange durable ew.tasks, queue durable ew.jobs, routing key jobs.
   Mỗi publish dùng mandatory và publisher confirms.
4. Worker đọc payload từ PostgreSQL, tính SHA-256, khóa row FOR UPDATE,
   commit kết quả rồi mới ACK. Row đã done không tăng process_count lần nữa.
5. Client GET /jobs/<UUID> để lấy kết quả. Retry POST cùng ID và input giữ nguyên
   kết quả; đổi input cho một ID đã tồn tại trả HTTP 409.

Message có thể được giao hoặc publish nhiều lần. process_count đo số lần chuyển
pending sang done (kết quả database), delivery_count đo các lần nhận đã commit.
Đây không phải bảo đảm message được giao exactly-once hay đếm mọi delivery đã
thất bại. DB/broker là single-node; mất disk không được xử lý bằng cơ chế retry.
Worker dùng cùng một process để publish và consume; DB lỗi làm reconnect broker
để các message chưa ACK được giao lại. API trả 503 nếu không commit DB được;
client cần retry bằng cùng task_id, vì commit có thể đã thành công trước khi mất
response. Task chờ trong DB sẽ được gửi lại sau khi dependencies phục hồi.

API /health kiểm tra kết nối DB và broker, không bảo đảm worker đang xử lý;
probe.py kiểm tra cả luồng xử lý. /live chỉ kiểm tra HTTP process.
API chỉ phục vụ mạng lab; không có HTTP authentication.

## API

- GET /live
- GET /health
- POST /jobs, Content-Type application/json:
  {"task_id":"<UUID>","run_id":"<1–64 ký tự>","client_id":"<1–64 ký tự>","payload":"<text>"}
- GET /jobs/<UUID>
- GET /stats?run_id=<run>

Body tối đa 128 KiB; payload tối đa 64 KiB UTF-8.
Stats: accepted, pending, done, processed, duplicate_deliveries.
probe.py dùng monotonic clock của chính client để đo latency, không lấy chênh
lệch timestamp giữa máy. Probe kiểm tra dependency health, kết quả hash,
process_count=1, HTTP retry, conflict và stats riêng của từng run.

## Kiểm tra sau cài

python3 probe.py --client-id ew-client-a1
python3 probe.py --client-id ew-client-a2
python3 probe.py --client-id ew-client-b

Chạy từng lệnh trên đúng VM client tương ứng. Marker thành công: WORKLOAD_E2E_OK.
Đây là smoke test, chưa phải kết quả throughput hoặc downtime migration.

## Log và lưu lại

systemctl status ew-api ew-worker --no-pager
journalctl -u ew-api -u ew-worker -n 80 --no-pager

Muốn dựng lại sau xóa instance: giữ image netfix, archive này, các lệnh bootstrap
PostgreSQL/RabbitMQ, key và file secrets trên controller. Nếu reset cả controller,
phải sao lưu các tệp đó sang nơi khác trước. Archive không chứa mật khẩu.

## Kiểm tra trong môi trường tạo file

Cú pháp Python/Bash và unit test với DB/broker giả được kiểm tra trước khi đóng
gói. Không có PostgreSQL/RabbitMQ/Gunicorn đang chạy ở môi trường tạo file;
kiểm tra tích hợp thật cần chạy setup.sh và probe.py trên lab.
