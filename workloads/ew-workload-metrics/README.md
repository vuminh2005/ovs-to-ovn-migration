# EW baseline metrics

Gói chạy baseline 120 giây trên ew-client-a1, ew-client-a2 và ew-client-b.
Chạy start-baseline.sh rồi collect-baseline.sh trên root@lab2-controller.
Image/app/DB/RabbitMQ phải đã qua smoke test. Không cần Internet hoặc pip trong guest.

## Tải và chạy

Trên laptop: scp ew-workload-metrics.tar.gz root@117.1.28.69:/root/ew-image-build/
Trên controller:

    cd /root/ew-image-build
    tar -xzf ew-workload-metrics.tar.gz
    bash ew-workload-metrics/start-baseline.sh
    bash ew-workload-metrics/collect-baseline.sh

Các helper có sẵn hàm SSH dùng namespace router của môi trường hiện tại.
Client chạy bằng ubuntu trong transient systemd service, không phụ thuộc phiên SSH.
Run ID mới cho mỗi lần chạy. Không xóa database hoặc kết quả các run trước.
Thu thập chỉ thực hiện khi có đủ summary từ cả ba client; helper chờ và in tiến độ.
Log nằm trên từng client tại /var/lib/ew-load/<run-id> và được lấy về controller
tại /root/ew-image-build/baseline-runs/<run-id>.

## Những gì được đo

- Mỗi client giữ tối đa một task chưa xong, tối đa một task mới mỗi giây.
  Đây là closed-loop: tốc độ tạo task giảm khi xử lý chậm hoặc mất kết nối.
  Không dùng số liệu này như phép đo capacity hay workload có offered rate cố định.
- Payload khoảng 10 KiB, UUID mới mỗi task. Retry POST giữ nguyên UUID và input,
  kể cả khi response bị mất sau commit. Poll đến done hoặc hết thời gian drain.
- SLO ban đầu 10 giây. Task vượt SLO vẫn tiếp tục được theo dõi; slo_missed có
  thể đồng thời là completed. Latency đo từ lần gửi đầu đến quan sát kết quả đúng,
  dùng monotonic clock trong cùng client. /stats dùng run ID riêng từng client.
- Hai luồng độc lập probe IPv4 ping DF 1400 byte và TCP connect đến API :8080,
  nhắm chu kỳ 1 giây. Ghi timestamp và kết quả từng lần trong events.jsonl.
- failure_windows là khoảng giữa lần quan sát lỗi đầu tiên và lần quan sát
  thành công kế tiếp. Không phải downtime chính xác; bị giới hạn bởi chu kỳ
  probe và timeout. recovery_observed=false nghĩa là đo kết thúc khi vẫn lỗi.
- Sau 120 giây ngừng tạo task, cho task cuối tối đa 30 giây thêm để drain.
  unresolved nghĩa là chưa xác nhận kết quả đúng lúc kết thúc, không tự kết
  luận là task bị mất. server_stats là snapshot sau lượt chạy.
- Baseline passed yêu cầu created=completed>0, không HTTP/probe/integrity/SLO
  lỗi, DB accepted=done=processed=created, pending=0. Duplicate deliveries không
  làm fail nếu process_count vẫn đúng; đây là message giao lại, không phải
  kết quả bị xử lý trùng. Client samples ít nên p99 chỉ là mô tả lượt baseline.

## Giới hạn

Ping/TCP chỉ đo client→API. App→DB và app→RabbitMQ được kiểm tra qua task E2E
và log worker; gói chưa đo riêng downtime/reconnect của từng backend, UDP loss,
throughput iperf hoặc control-plane OpenStack. Chưa chạy migration ở bước này.
UTC giúp đối chiếu log; chưa có xác nhận NTP của tất cả guest, nên không tính
latency bằng phép trừ timestamp giữa nhiều máy. Metric đơn máy dùng monotonic.

Transient service không tự phục hồi qua reboot VM; persistent JSONL giữ dữ
liệu đã ghi. Với migration dài hơn, runner.py hỗ trợ --duration 0 và dừng bằng
SIGTERM, nhưng helper hiện tại cố ý chỉ chạy baseline 120 giây. Cần điều chỉnh
quy trình start/stop/thu thập trước lượt migration. SSH qua qrouter của ML2/OVS
có thể không dùng được trong migration; đo trong guest vẫn chạy, thu thập sau
khi đã có đường SSH phù hợp.

Môi trường tạo gói không truy cập lab. Unit test dùng backend mô phỏng; chạy
baseline trên lab để xác nhận hiệu quả thực tế.
