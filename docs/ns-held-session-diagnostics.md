# Chẩn đoán phiên TCP SNAT bị mất

Run `20261013-153114` đã chuyển đổi DB và cutover sang OVN. EW ICMP, EW TCP,
VM cũ và VM tạo mới trên OVN đều PASS. N-S FIP giữ được phiên cũ; kết nối SNAT
mới phục hồi, nhưng socket SNAT đã mở trước migration bị đóng. Toàn bộ run giữ
kết quả `MIGRATED_VALIDATION_INCOMPLETE` vì không bảo toàn được phiên này.
Cleanup workload được giữ lại để phục vụ kiểm tra; không hạ tiêu chí PASS.

## Bằng chứng hiện có

- Client SNAT chỉ có `connection_attempts: 1`, `held_broken: true` và boot/probe
  không đổi. Listener/server phía controller không đổi.
- Cả held và new bắt đầu mất phản hồi gần như đồng thời: khoảng
  `2026-10-13 16:40:24.928 +07:00` và `16:40:24.921 +07:00`.
- Kết nối mới phục hồi khoảng `16:41:41.160 +07:00`, sau 76.239 giây.
- Held vẫn chưa phục hồi ở snapshot validation khoảng `16:59:41.585 +07:00`,
  tức 1156.657 giây kể từ đầu chuỗi lỗi chưa phục hồi. Đây là thời gian lỗi đang
  mở tại snapshot, không phải outage đã phục hồi.
- Những giờ trên được quy đổi từ monotonic của cùng VM và cặp ts/mono trong
  console. Giờ wall-clock giữa các máy chỉ dùng đối chiếu gần đúng; số đo
  duration của client vẫn dùng monotonic.
- Summary đầu tiên *còn trong bản console được gửi* ghi socket đã đóng lúc
  `16:58:57.183 +07:00`. Không coi đây là thời điểm socket thực sự đóng.

Marker bổ sung do người dùng cung cấp: phase 08 chạy từ `16:39:32.217` đến
`16:41:23.288`; phase 09 từ `16:41:23.520` đến `16:41:26.058` (+07:00).
Đợt mất phản hồi bắt đầu trong phase 08, khoảng 59 giây trước phase 09. Do đó
xóa qrouter ở phase 09 không gây đợt mất phản hồi ban đầu. Thời điểm đóng
socket thực sự chưa biết; chưa loại trừ tác động của cleanup lên khả năng
phục hồi của phiên cũ.

Lỗi probe cũ: sau khi socket đóng, mỗi vòng lặp tiếp theo ghi đè terminal_error
bằng `held socket already closed; reconnect prohibited`. Vì vậy bản evidence
cũ không xác định được lỗi đầu tiên là reset, broken pipe, EOF hay lỗi kiểm tra
echo. Không được suy đoán errno/thời điểm đóng để bổ sung ngược vào run cũ.

NAT/conntrack khi bỏ qrouter và chuyển sang OVN là hướng kiểm tra, chưa phải
nguyên nhân đã chứng minh. Playbook không chuyển trạng thái conntrack từ
namespace qrouter sang OVN; kết nối mới PASS không chứng minh trạng thái phiên
cũ được bảo toàn. Cũng chưa có bằng chứng đủ để kết luận outer policy là thủ phạm.

## Bản sửa chẩn đoán

`scripts/ns_probe.py` giữ nguyên lỗi terminal đầu tiên và lưu `terminal_event`
với operation, exception, errno, sequence, epoch/monotonic và endpoints của
socket nếu còn đọc được. Các vòng lặp sau vẫn tăng bộ đếm lỗi, không reconnect
held và không ghi đè sự kiện này. Timeout echo vẫn có thể phục hồi trên cùng
socket như trước.

`scripts/extended_validation.py` giữ held_broken/terminal_event trong kết quả.
Báo cáo TXT hiển thị lỗi đang mở và lỗi socket. Khi đọc probe cũ chưa có event,
báo cáo chỉ gọi đó là lỗi đã ghi lại, không khẳng định là lỗi đầu tiên.

Thay code trên controller không cập nhật process đang chạy trong guest và
không thể phục hồi một socket đã đóng. Bản sửa được dùng khi dựng probe ở lần
thử tiếp theo. Không chạy lại migration hoặc restart probe để đổi kết quả
lịch sử của run này.

Khi quyết định thử một run mới, dùng bản reset mới: nó dừng đúng probe N-S
controller theo checkpoint hoặc xác minh lệnh đang chạy khi checkpoint đã mất,
trước Kolla destroy. Controller không reboot nên
run FAIL có thể để lại listener cũ chiếm cổng 18180. Reset giữ nguyên evidence
migration cũ và lưu `local-probes-stop.json` trong snapshot reset mới.

## Đọc marker đúng của run hiện tại

Schema 2 dùng `phase08.start`, `phase08.end`, `phase09.start`, `phase09.end`.
Lệnh trước đã dùng nhầm dấu gạch dưới (`phase_08.start`); MISSING từ lệnh đó
không chứng minh run thiếu timestamp.

```bash
cd /root/ovs-to-ovn-backup/20261013-153114
python3 - <<'PY'
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
for name in ('phase08.start', 'phase08.end', 'phase09.start', 'phase09.end'):
    path = Path('metrics') / name
    if path.exists():
        raw = path.read_text().strip()
        stamp = datetime.fromtimestamp(float(raw), ZoneInfo('Asia/Ho_Chi_Minh'))
        print(name, raw, stamp.isoformat(timespec='milliseconds'))
    else:
        print(name, 'MISSING')
PY
```

Chỉ đọc marker và evidence. Không chạy reset, DB migration, conntrack flush,
reconnect hay xóa workload trong bước chẩn đoán này.
